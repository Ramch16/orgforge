import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from vittics_builder.cli import main
from vittics_builder.db import DB
from vittics_builder.memory import Memory
from vittics_builder.server import create_app
from vittics_builder.tools import Workspace
from test_parallel import add_tickets, plan_approved


def test_memory_survives_reopen_and_searches_old_decisions(tmp_path):
    path = tmp_path / 'company.db'
    db = DB(path)
    memory = Memory(db)
    memory.remember('PostgreSQL was chosen for transactions instead of MongoDB.', 'Sony', 1,
                    category='architecture', source='docs/ARCHITECTURE.md')
    for i in range(350):
        memory.remember(f'Unrelated convention number {i} for logs.', 'Hari', 1, category='code')
    db.conn.close()
    reopened = DB(path)
    [decision] = Memory(reopened).recall(1, 'Why PostgreSQL instead of MongoDB?', category='architecture')
    assert decision['author'] == 'Sony' and decision['source'] == 'docs/ARCHITECTURE.md'
    assert 'PostgreSQL' in decision['text']
    assert Memory(reopened).search(1, '" OR * ) ;DROP TABLE memories;') == []


def test_existing_company_memories_are_indexed_without_loss(tmp_path):
    path = tmp_path / 'old.db'
    connection = sqlite3.connect(path)
    connection.execute('CREATE TABLE memories(id INTEGER PRIMARY KEY, project_id INTEGER, author TEXT, text TEXT, created_at TEXT)')
    connection.execute("INSERT INTO memories VALUES(1,2,'Sony','Chose PostgreSQL for ACID transactions.','2025-01-01')")
    connection.commit(); connection.close()
    db = DB(path)
    [memory] = Memory(db).recall(2, 'PostgreSQL')
    assert memory['category'] == 'decision' and memory['id'] == 1
    db.run('UPDATE memories SET text=? WHERE id=1', 'Chose SQLite for embedded deployments.')
    assert Memory(db).search(2, 'PostgreSQL') == []
    assert len(Memory(db).recall(2, 'SQLite')) == 1


def test_memory_scope_categories_and_provenance(co):
    co.memory.remember('Use cookies for authentication.', 'Hari', 1, category='security', source='ticket:T-12')
    co.memory.remember('Use cookies for customer preferences.', 'Ram', 2, category='customer')
    co.memory.remember('Use cookies consistently company wide.', 'Niki', None, 'company', category='business')
    assert len(co.memory.recall(1, 'cookies')) == 2
    assert len(co.memory.recall(1, 'cookies', include_history=True)) == 3
    [security] = co.memory.recall(1, 'cookies', category='security')
    assert security['source'] == 'ticket:T-12'
    assert co.memory.search(1, 'notfound') == []
    with pytest.raises(ValueError):
        co.memory.remember('Invalid scope must fail.', 'Hari', 1, scope='private')
    with pytest.raises(ValueError):
        co.memory.recall(1, 'cookies', category='invented')
    assert co.memory.remember('Use cookies for authentication.', 'Hari', 1, category='security') == 'Already remembered.'


def test_dynamic_team_uses_configured_capability_and_keeps_agents_distinct(co):
    p = co.pipeline.create_project('Application', 'Build it')
    frontend = co.org.staff(role='frontend_engineer')[0]
    co.s.raw['swarms'] = {'capabilities': {'frontend_engineer': ['backend_engineer']}}
    tasks = add_tickets(co, p['id'], 2, role='frontend_engineer')
    batch = co.pipeline._claim_batch(p['id'])
    assert len(batch) == 2 and batch[0]['assignee_id'] == frontend['id']
    assert len({t['assignee_id'] for t in batch}) == 2
    snapshot = co.swarms.snapshot(p['id'])
    assert len(snapshot['members']) == 2
    assert {m['task_id'] for m in snapshot['members']} == {t['id'] for t in tasks}


def test_adaptive_staffing_is_opt_in_capped_and_repeatable(co):
    p = co.pipeline.create_project('Application', 'Build it')
    add_tickets(co, p['id'], 25, role='frontend_engineer')
    co.pipeline._check_staffing(p['id'])
    assert len(co.org.staff(role='frontend_engineer')) == 1
    assert co.pipeline.inbox('cto')[0]['kind'] == 'hire'
    co.s.raw['swarms'] = {'auto_staff': True}
    co.s.max_per_role = 3
    co.pipeline._check_staffing(p['id'])
    co.pipeline._check_staffing(p['id'])
    assert len(co.org.staff(role='frontend_engineer')) == 3
    assert co.swarms.snapshot(p['id'])['queues'][0]['bottleneck']


def test_bounded_work_resumes_without_bypassing_approvals(co):
    p = plan_approved(co)
    add_tickets(co, p['id'], 4, role='backend_engineer')
    stage = co.pipeline.advance(p['id'], max_cycles=1)['stage']
    assert stage == 'build'
    assert co.db.one("SELECT COUNT(*) AS n FROM tasks WHERE project_id=? AND origin!='stage' AND status='todo'", p['id'])['n'] > 0
    while co.pipeline.project(p['id'])['stage'] == 'build':
        co.pipeline.advance(p['id'], max_cycles=1)
    assert co.pipeline.project(p['id'])['stage'] == 'release_approval'
    approvals = co.pipeline.inbox()
    co.pipeline.advance(p['id'], max_cycles=10)
    assert co.pipeline.inbox() == approvals
    assert co.pipeline.project(p['id'])['stage'] == 'release_approval'


@pytest.mark.parametrize('kwargs', [{'max_cycles': 0}, {'max_cycles': 1.5}, {'max_cycles': True},
                                    {'max_seconds': 0}, {'max_seconds': float('nan')}])
def test_invalid_autonomy_limits_are_rejected(co, kwargs):
    p = co.pipeline.create_project('App', 'Build it')
    with pytest.raises(ValueError):
        co.pipeline.advance(p['id'], **kwargs)


def test_routing_learns_from_reviewed_outcomes_and_records_actual_model(co):
    co.s.raw['routing'] = {'enabled': True, 'rules': {'builder': ['claude-haiku-4-5', 'claude-sonnet-5-5']}}
    agent = co.org.staff(kind='builder')[0]
    for model, success, quality in [('claude-haiku-4-5', False, 20), ('claude-sonnet-5-5', True, 95)]:
        co.s.raw['routing']['rules']['builder'] = [model]
        routed, decision, start = co.router.start(agent, 1, {'ticket_id': 1}, 'builder')
        co.router.finish(decision, start, 'completed', None)
        co.router.evaluate(decision, success, quality)
    co.s.raw['routing']['rules']['builder'] = ['claude-haiku-4-5', 'claude-sonnet-5-5']
    model, _ = co.router.select(agent, 'builder', {})
    assert model == 'claude-sonnet-5-5'
    result = co.runtime.run(agent, 'Work', Workspace(co.s.root / 'routing-test'))
    selected = co.db.one('SELECT * FROM routing_decisions WHERE id=?', result.route_id)
    assert selected['model'] == model and selected['status'] == 'completed'
    assert selected['success'] is None  # completion alone is not review evidence
    assert selected['cost'] > 0 and selected['priced'] == 1
    assert co.db.one('SELECT model FROM usage ORDER BY id DESC')['model'] == model


def test_independent_review_requires_another_configured_model(co):
    co.s.raw['routing'] = {'enabled': True, 'independent_reviews': True,
                          'rules': {'reviewer': ['claude-haiku-4-5', 'claude-sonnet-5-5']}}
    agent = co.org.staff(kind='reviewer')[0]
    model, _ = co.router.select(agent, 'reviewer', {'author_model': 'claude-sonnet-5-5'})
    assert model == 'claude-haiku-4-5'
    co.s.raw['routing']['rules']['reviewer'] = ['claude-sonnet-5-5']
    with pytest.raises(ValueError, match='Independent review'):
        co.router.select(agent, 'reviewer', {'author_model': 'claude-sonnet-5-5'})


def test_review_evidence_and_failure_memory_are_recorded(make_company):
    co = make_company(bad_agents=('Hari', 'Sandy'))
    p = plan_approved(co)
    co.pipeline.advance(p['id'])
    decisions = co.db.all("SELECT * FROM routing_decisions WHERE task_kind='builder'")
    assert any(d['success'] == 0 and d['quality'] is not None for d in decisions)
    failures = co.memory.recall(p['id'], 'review', category='failure')
    assert failures and failures[0]['source'].startswith('ticket:')


def test_foundation_api_is_authenticated_and_cli_commands_work(co, capsys):
    p = co.pipeline.create_project('App', 'Build it')
    co.memory.remember('PostgreSQL for transactional data.', 'Sony', p['id'], category='architecture')
    client = TestClient(create_app(co, {'ceo': 'c', 'cto': 't'}))
    for path in ['/api/routing', f"/api/projects/{p['id']}/swarm", '/api/memories?query=PostgreSQL']:
        assert client.get(path).status_code == 401
        assert client.get(path, headers={'X-Token': 'c'}).status_code == 200
    assert client.post(f"/api/projects/{p['id']}/work?cycles=0", headers={'X-Token':'t'}).status_code == 400
    for args in [['swarm', str(p['id'])], ['memory', 'PostgreSQL', '--project', str(p['id'])], ['routing']]:
        assert main(['--home', str(co.s.root), *args]) == 0
        assert json.loads(capsys.readouterr().out) is not None


def test_router_excludes_uninstalled_engines_and_missing_credentials(co, monkeypatch):
    co.s.raw['routing'] = {'enabled':True,'rules':{'builder':['cli:missing','openai:missing','claude-haiku-4-5']}}
    monkeypatch.delenv('OPENAI_API_KEY',raising=False)
    model, _ = co.router.select(co.org.staff(kind='builder')[0],'builder',{})
    assert model == 'claude-haiku-4-5'
    co.s.raw['routing']['rules']['builder'] = ['cli:missing','openai:missing']
    with pytest.raises(RuntimeError,match='No configured routing candidate'):
        co.router.select(co.org.staff(kind='builder')[0],'builder',{})


def test_routing_runtime_failure_is_logged_and_not_scored_as_success(co):
    class Broken:
        def complete(self, **kwargs):
            raise RuntimeError('provider unavailable')
    co.runtime.provider = Broken()
    with pytest.raises(RuntimeError, match='unavailable'):
        co.runtime.run(co.org.staff(kind='planner')[0], 'Design', None)
    route = co.db.one('SELECT * FROM routing_decisions ORDER BY id DESC')
    assert route['status'] == 'failed' and route['success'] is None


def test_capability_rework_keeps_the_substitute_seat(co):
    p = co.pipeline.create_project('App','Build it')
    co.s.raw['swarms'] = {'capabilities':{'frontend_engineer':['backend_engineer']}}
    frontend = co.org.staff(role='frontend_engineer')[0]
    [task] = add_tickets(co,p['id'],1,role='frontend_engineer')
    substitute = co.swarms.pick(task,p['id'],{frontend['id']})
    co.db.run('UPDATE tasks SET assignee_id=? WHERE id=?',substitute['id'],task['id'])
    assigned = co.db.one('SELECT * FROM tasks WHERE id=?',task['id'])
    assert co.swarms.pick(assigned,p['id'],set())['id']==substitute['id']
    assert co.swarms.pick(assigned,p['id'],{substitute['id']}) is None
