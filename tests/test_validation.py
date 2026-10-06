import json
from pathlib import Path

import pytest

from vittics_builder.agent import RunResult
from vittics_builder.pipeline import PipelineError
from vittics_builder.tools import Workspace
from vittics_builder.validation import validate_plan, verify_product
from test_pipeline import decide_next


def contract(ws, command='python -c "print(123)"'):
    ws.write_file('product.json', json.dumps({'name': 'Example', 'setup': 'Python', 'run': 'python app.py',
        'checks': [{'id': 'flow', 'requirement': 'Core behavior', 'command': command}]}))
    ws.write_file('README.md', '# Usage')
    ws.write_file('docs/OPERATIONS.md', '# Run locally')


def test_verification_executes_and_records_failure(tmp_path):
    ws = Workspace(tmp_path)
    contract(ws, 'python -c "raise SystemExit(7)"')
    result = verify_product(ws)
    assert not result['passed']
    assert 'exit code 7' in result['checks'][0]['output']
    assert json.loads((tmp_path / 'docs/VERIFICATION.json').read_text()) == result


@pytest.mark.parametrize('bad', [None, {}, [], {'checks': []}])
def test_missing_or_malformed_contract_fails(tmp_path, bad):
    ws = Workspace(tmp_path)
    if bad is not None:
        ws.write_file('product.json', json.dumps(bad))
    assert not verify_product(ws)['passed']


def test_verification_checks_documentation_and_command_policy(tmp_path):
    ws = Workspace(tmp_path)
    contract(ws, 'sudo echo test')
    assert not verify_product(ws)['checks'][0]['passed']
    contract(ws)
    (tmp_path / 'README.md').unlink()
    assert not verify_product(ws)['passed']


def task(key, deps=()):
    return dict(key=key, title=key, description='Implement and test', role='builder', depends_on=list(deps))


@pytest.mark.parametrize('tasks', [[task('a', ['b'])], [task('a', ['b']), task('b', ['a'])],
                                  [task('a'), task('a')], [task('a', ['a'])]])
def test_bad_plans_rejected(tasks):
    with pytest.raises(ValueError):
        validate_plan(tasks, {'builder'})


def test_out_of_order_acyclic_plan_allowed():
    validate_plan([task('b', ['a']), task('a')], {'builder'})


def start_build(co):
    p = co.pipeline.create_project('Product', 'Build a library')
    co.pipeline.advance(p['id'])
    decide_next(co, 'ceo')
    return p


def test_missing_review_cannot_pass_task(co, monkeypatch):
    original = co.runtime.run
    def run(agent, *args, **kwargs):
        if agent['role'] == 'code_reviewer':
            return RunResult(text='Looks good', completed=True)
        return original(agent, *args, **kwargs)
    monkeypatch.setattr(co.runtime, 'run', run)
    start_build(co)
    assert decide_next(co, 'cto')['stage'] == 'escalation'


def test_integration_failure_repairs_then_blocks_without_override(co, monkeypatch):
    original = co.runtime.run
    def run(agent, *args, **kwargs):
        if kwargs.get('meta', {}).get('purpose') == 'integration':
            return RunResult(completed=True, review={'score': 20, 'verdict': 'request_changes', 'notes': 'Broken flow'})
        return original(agent, *args, **kwargs)
    monkeypatch.setattr(co.runtime, 'run', run)
    start_build(co)
    assert decide_next(co, 'cto')['stage'] == 'release_blocked'
    fixes = co.db.all("SELECT * FROM tasks WHERE key LIKE 'verify-%'")
    assert len(fixes) == co.s.max_rework
    approval = next(a for a in co.pipeline.inbox('cto') if a['kind'] == 'release_blocked')
    with pytest.raises(PipelineError, match='cannot be approved'):
        co.pipeline.decide(approval['id'], 'cto', 'approved')
    monkeypatch.setattr(co.runtime, 'run', original)
    assert decide_next(co, 'cto', 'rejected', 'Fix the broken flow')['stage'] == 'release_approval'


def test_changed_product_cannot_be_signed_off(co):
    p = start_build(co)
    decide_next(co, 'cto')
    decide_next(co, 'cto')
    Path(p['workspace'], 'src/core.py').write_text('broken')
    approval = co.pipeline.inbox('ceo')[0]
    with pytest.raises(PipelineError, match='changed after verification'):
        co.pipeline.decide(approval['id'], 'ceo', 'approved')


def test_interrupted_task_is_resumed(co):
    p = start_build(co)
    approval = co.pipeline.inbox('cto')[0]
    co.pipeline.decide(approval['id'], 'cto', 'approved')
    co.db.run("UPDATE tasks SET status='in_progress' WHERE project_id=? AND key='core'", p['id'])
    assert co.pipeline.advance(p['id'])['stage'] == 'release_approval'


def test_missing_audit_verdict_blocks_release(co, monkeypatch):
    co.s.max_rework = 0
    original = co.runtime.run
    def run(agent, *args, **kwargs):
        if agent['role'] == 'security_auditor':
            return RunResult(completed=True, text='No verdict')
        return original(agent, *args, **kwargs)
    monkeypatch.setattr(co.runtime, 'run', run)
    start_build(co)
    assert decide_next(co, 'cto')['stage'] == 'release_blocked'


def test_export_only_signed_off_product(co, tmp_path):
    from vittics_builder.delivery import export_product
    import zipfile
    p = start_build(co)
    with pytest.raises(PipelineError):
        export_product(co.pipeline, p['id'], tmp_path / 'release.zip')
    decide_next(co, 'cto')
    decide_next(co, 'cto')
    decide_next(co, 'ceo')
    destination = export_product(co.pipeline, p['id'], tmp_path / 'release.zip')
    with zipfile.ZipFile(destination) as archive:
        assert 'src/core.py' in archive.namelist()
        assert json.loads(archive.read('docs/VERIFICATION.json'))['passed']
        assert not any(name.startswith('.git/') for name in archive.namelist())
    with pytest.raises(PipelineError):
        export_product(co.pipeline, p['id'], destination)


def test_executable_failure_blocks_release(co):
    co.s.max_rework = 0
    p = start_build(co)
    ws = co.pipeline.workspace(p)
    contract(ws, 'python -c "raise SystemExit(9)"')
    assert decide_next(co, 'cto')['stage'] == 'contract_review'
    assert decide_next(co, 'cto')['stage'] == 'release_blocked'
    report = json.loads(ws.resolve('docs/VERIFICATION.json').read_text())
    assert not report['passed'] and 'exit code 9' in report['checks'][0]['output']


def approve_design_then_edit_contract(co, command):
    p = start_build(co)
    ws = co.pipeline.workspace(p)
    approved = ws.resolve('product.json').read_text()
    [approval] = co.pipeline.inbox('cto')
    co.pipeline.decide(approval['id'], 'cto', 'approved')
    contract(ws, command)                   # an agent rewrites the checks mid-build
    return p, ws, approved


def test_weakened_contract_goes_to_cto_and_rejection_restores_it(co):
    p, ws, approved = approve_design_then_edit_contract(co, 'true')
    assert co.pipeline.advance(p['id'])['stage'] == 'contract_review'
    [approval] = co.pipeline.inbox('cto')
    assert approval['kind'] == 'contract' and '"command": "true"' in approval['summary']
    with pytest.raises(PipelineError):
        co.pipeline.decide(approval['id'], 'cto', 'rejected')    # feedback is required
    assert decide_next(co, 'cto', 'rejected', 'Keep the real checks')['stage'] == 'release_approval'
    assert ws.resolve('product.json').read_text() == approved
    assert co.db.one("SELECT COUNT(*) AS n FROM tasks WHERE title LIKE '%acceptance checks%'")['n'] == 1
    report = json.loads(ws.resolve('docs/VERIFICATION.json').read_text())
    assert [c['id'] for c in report['checks']] == ['unit-tests', 'core-flow']


def test_cto_can_accept_changed_contract(co):
    p, ws, _ = approve_design_then_edit_contract(co, 'python -c "print(1)"')
    assert co.pipeline.advance(p['id'])['stage'] == 'contract_review'
    assert decide_next(co, 'cto')['stage'] == 'release_approval'
    report = json.loads(ws.resolve('docs/VERIFICATION.json').read_text())
    assert [c['id'] for c in report['checks']] == ['flow'] and report['passed']
    assert co.pipeline._approved_contract(p['id']) == ws.resolve('product.json').read_text()


def test_design_approved_without_pinned_contract_needs_cto_review(co):
    p = start_build(co)
    [approval] = co.pipeline.inbox('cto')
    co.db.run("UPDATE approvals SET payload='{}' WHERE id=?", approval['id'])   # designed before 0.3.1
    co.pipeline.decide(approval['id'], 'cto', 'approved')
    assert co.pipeline.advance(p['id'])['stage'] == 'contract_review'
    assert decide_next(co, 'cto')['stage'] == 'release_approval'


def test_exhausted_builder_cannot_pass_even_when_reviewers_approve(co, monkeypatch):
    original = co.runtime.run
    def run(agent, *args, **kwargs):
        result = original(agent, *args, **kwargs)
        if co.org.role(agent['role'])['kind'] == 'builder':
            result.completed = False
        return result
    monkeypatch.setattr(co.runtime, 'run', run)
    start_build(co)
    assert decide_next(co, 'cto')['stage'] == 'escalation'
