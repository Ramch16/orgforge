"""Executable product acceptance contracts and durable release evidence."""
from __future__ import annotations

import hashlib
import json
import re

from .db import now
from .tools import ToolError, Workspace

CONTRACT = 'product.json'
CONTRACT_GUIDE = '''Write product.json with this schema:
{"name":"Product", "setup":"installation instructions", "run":"how to start/use it",
 "checks":[{"id":"core-flow", "requirement":"A concrete PRD acceptance criterion",
 "command":"a finite shell command that exits nonzero on failure"}]}
Include automated checks for installation/build, tests, and the main end-to-end user flows
as applicable. Checks must exercise the real implementation, not stubs or unconditional success.
For servers, checks must start an isolated test instance and clean it up. Document required
external services, credentials, migrations, backups and deployment in docs/OPERATIONS.md.
Never include secrets. Include README.md with reproducible installation and usage instructions.
'''


def validate_plan(tasks: list[dict], roles: set[str]) -> None:
    if not isinstance(tasks, list) or not tasks:
        raise ValueError('The plan must contain tasks.')
    keys = set()
    for task in tasks:
        if not isinstance(task, dict):
            raise ValueError('Each task must be an object.')
        key = task.get('key', '')
        if not isinstance(key, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', key) or key in keys:
            raise ValueError('Task keys must be unique lowercase identifiers.')
        keys.add(key)
        if task.get('role') not in roles:
            raise ValueError(f'Task {key} has an unstaffed builder role.')
        if not all(isinstance(task.get(k), str) and task[k].strip() for k in ('title', 'description')):
            raise ValueError(f'Task {key} needs a title and acceptance criteria in its description.')
    pending = {}
    for task in tasks:
        deps = task.get('depends_on', [])
        if not isinstance(deps, list) or any(not isinstance(d, str) or d not in keys for d in deps):
            raise ValueError(f"Task {task['key']} has unknown dependencies.")
        pending[task['key']] = set(deps)
    while pending:
        ready = {key for key, deps in pending.items() if not deps}
        if not ready:
            raise ValueError('The plan contains a dependency cycle.')
        pending = {key: deps - ready for key, deps in pending.items() if key not in ready}


def read_contract(ws: Workspace) -> dict:
    data = json.loads(ws.resolve(CONTRACT).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError('product.json must be an object.')
    for key in ('name', 'setup', 'run'):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError(f'product.json needs a nonempty {key}.')
    checks = data.get('checks')
    if not isinstance(checks, list) or not 1 <= len(checks) <= 30:
        raise ValueError('product.json needs 1–30 executable acceptance checks.')
    ids = set()
    for check in checks:
        if not isinstance(check, dict) or not all(isinstance(check.get(k), str) and check[k].strip()
                                                 for k in ('id', 'requirement', 'command')):
            raise ValueError('Every check needs id, requirement and command strings.')
        if check['id'] in ids:
            raise ValueError('Acceptance check ids must be unique.')
        ids.add(check['id'])
    return data


def verify_product(ws: Workspace) -> dict:
    report = {'checked_at': now(), 'passed': False, 'checks': [], 'errors': []}
    try:
        contract = read_contract(ws)
        report['contract_sha256'] = hashlib.sha256(ws.resolve(CONTRACT).read_bytes()).hexdigest()
        for path in ('README.md', 'docs/OPERATIONS.md'):
            if not ws.resolve(path).is_file() or not ws.resolve(path).read_text(encoding="utf-8").strip():
                report['errors'].append(f'Missing delivery documentation: {path}')
        for check in contract['checks']:
            try:
                output = ws.run_command(check['command'])
                passed = output.splitlines()[0] == 'exit code 0'
            except ToolError as exc:
                output, passed = str(exc), False
            report['checks'].append({**check, 'passed': passed, 'output': output})
    except (OSError, ValueError, ToolError) as exc:
        report['errors'].append(f'Invalid acceptance contract: {exc}')
    report['passed'] = bool(report['checks']) and not report['errors'] and all(c['passed'] for c in report['checks'])
    ws.write_file('docs/VERIFICATION.json', json.dumps(report, indent=2))
    return report
