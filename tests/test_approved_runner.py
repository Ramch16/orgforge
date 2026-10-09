import json

import pytest

from vittics_builder.agent import RunResult
from vittics_builder.engines import BUILTIN_ENGINES
from vittics_builder.tools import ToolError
from test_validation import contract


def approved(co):
    p = co.pipeline.create_project('Runner', 'Exercise approved checks')
    ws = co.pipeline.workspace(p)
    contract(ws)
    co.pipeline._approval(p['id'], 'contract', 'cto', 'Checks', '', {'contract': ws.resolve('product.json').read_text()})
    co.db.run("UPDATE approvals SET status='approved' WHERE project_id=?", p['id'])
    return p, ws


def execute(co, p, ws, ids):
    return co.runtime._execute(co.org.agent('Hari'), 'run_approved_checks', {'ids': ids}, ws, RunResult(), p['id'], {}, 0)


def test_selected_approved_check_runs(co):
    p, ws = approved(co)
    result = execute(co, p, ws, ['flow'])
    assert 'flow: PASSED' in result and 'exit code 0' in result
    assert not ws.resolve('docs/VERIFICATION.json').exists()


def test_unknown_ids_cannot_run_commands(co, monkeypatch):
    p, ws = approved(co)
    monkeypatch.setattr(ws, 'run_command', lambda *a, **k: pytest.fail('command executed'))
    assert 'Use existing product.json check IDs' in execute(co, p, ws, ['not-a-check'])


def test_changed_contract_blocks_both_runner_paths(co, monkeypatch):
    p, ws = approved(co)
    contract(ws, 'python -c "print(456)"')
    monkeypatch.setattr(ws, 'run_command', lambda *a, **k: pytest.fail('unapproved command executed'))
    with pytest.raises(ToolError, match='CTO-approved'):
        execute(co, p, ws, ['flow'])
    report = co.pipeline._host_checks(p, ws, record=False)
    assert not report['passed'] and not report['checks']


def test_cli_gets_actual_results_before_submission(co, monkeypatch):
    p, ws = approved(co)
    co.s.engines['fake'] = BUILTIN_ENGINES['codex']
    co.db.run("UPDATE agents SET model='cli:fake' WHERE name='Hari'")
    prompts = []
    def cli(engine, **kw):
        prompts.append(kw['prompt'])
        text = ('Request.\n```vittics-builder\n{"run_checks":{"ids":["flow"]},"comments":[{"body":"Premature"}]}\n```'
                if len(prompts) == 1 else 'Verified the actual result.')
        return dict(text=text, is_error=False, turns=1, cost=0, input_tokens=0, output_tokens=0)
    monkeypatch.setattr('vittics_builder.agent.run_cli', cli)
    result = co.runtime.run(co.org.agent('Hari'), 'Repair', ws, project_id=p['id'])
    assert result.completed and len(prompts) == 2 and 'flow: PASSED' in prompts[1]
    assert 'exit code 0' in prompts[1]
    assert not co.db.all("SELECT * FROM ticket_comments WHERE body='Premature'")


def test_repeated_cli_requests_are_bounded(co, monkeypatch):
    p, ws = approved(co)
    co.s.engines['fake'] = BUILTIN_ENGINES['codex']
    co.db.run("UPDATE agents SET model='cli:fake' WHERE name='Hari'")
    calls = []
    def cli(engine, **kw):
        calls.append(kw)
        return dict(text='```vittics-builder\n{"run_checks":{"ids":["flow"]}}\n```', is_error=False,
                    turns=1, cost=0, input_tokens=0, output_tokens=0)
    monkeypatch.setattr('vittics_builder.agent.run_cli', cli)
    result = co.runtime.run(co.org.agent('Hari'), 'Repair', ws, project_id=p['id'])
    assert len(calls) == 3 and not result.completed and 'continuation limit' in result.text


def test_chat_cannot_request_host_execution(co, monkeypatch):
    p, ws = approved(co)
    co.s.engines['fake'] = BUILTIN_ENGINES['codex']
    co.db.run("UPDATE agents SET model='cli:fake' WHERE name='Hari'")
    monkeypatch.setattr('vittics_builder.agent.run_cli', lambda *a, **kw: dict(
        text='```vittics-builder\n{"run_checks":{"ids":["flow"]}}\n```', is_error=False,
        turns=1, cost=0, input_tokens=0, output_tokens=0))
    monkeypatch.setattr(ws, 'run_command', lambda *a, **k: pytest.fail('chat executed command'))
    result = co.runtime.run(co.org.agent('Hari'), 'Status?', ws, project_id=p['id'], chat_with='Lucky', only_tools={'read_file'})
    assert not result.completed and 'not available' in result.text


@pytest.mark.parametrize('args', [None, [], {"ids": "flow"}, {"ids": []}, {"ids": [None]}])
def test_malformed_check_requests_are_rejected(co, args):
    p, ws = approved(co)
    with pytest.raises(ToolError, match='check IDs'):
        co.runtime._execute(co.org.agent('Hari'), 'run_approved_checks', args, ws, RunResult(), p['id'], {}, 0)


def test_long_approval_preserves_failing_assertion(co):
    p, _ = approved(co)
    failure = 'AssertionError: current failure'
    aid = co.pipeline._approval(p['id'], 'release_blocked', 'cto', 'Failure', 'Header\n' + 'x' * 10000 + failure, {})
    summary = co.db.one('SELECT summary FROM approvals WHERE id=?', aid)['summary']
    assert summary.startswith('Header') and summary.endswith(failure) and len(summary) <= 6000


def test_host_result_redacts_project_secrets(co):
    p, ws = approved(co)
    contract(ws, 'python -c "import os; print(os.environ[\'PRIVATE_TOKEN\']); raise SystemExit(3)"')
    co.db.run('UPDATE approvals SET payload=? WHERE project_id=?', json.dumps({'contract': ws.resolve('product.json').read_text()}), p['id'])
    class Vault:
        def env(self, pid): return {'PRIVATE_TOKEN': 'private-test-value'}
        def redact(self, pid, text): return text.replace('private-test-value', '[redacted]')
    co.runtime.vault = Vault()
    result = execute(co, p, ws, ['flow'])
    assert 'exit code 3' in result and '[redacted]' in result and 'private-test-value' not in result


def test_api_check_requests_are_also_bounded(co):
    p, ws = approved(co)
    meta = {}
    for _ in range(2):
        co.runtime._execute(co.org.agent('Hari'), 'run_approved_checks', {'ids': ['flow']}, ws, RunResult(), p['id'], meta, 0)
    with pytest.raises(ToolError, match='continuation limit'):
        co.runtime._execute(co.org.agent('Hari'), 'run_approved_checks', {'ids': ['flow']}, ws, RunResult(), p['id'], meta, 0)
