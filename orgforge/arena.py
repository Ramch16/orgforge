"""Compete approved strategies on identical snapshots with immutable benchmark fixtures."""
import hashlib
import json
import shutil
from pathlib import Path

from .db import now
from .tools import SKIP_DIRS, Workspace

EXCLUDED=SKIP_DIRS|{'.orgforge','.env*','*.pem','*.key','credentials*','org.yaml','skills'}


def hashes(root):
    result={}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():raise ValueError('Arena snapshots cannot contain symbolic links.')
        if path.is_file():result[str(path.relative_to(root))]=hashlib.sha256(path.read_bytes()).hexdigest()
    return result


class Arena:
    def __init__(self,company):
        self.co,self.db=company,company.db

    def compete(self,project_id,goal,candidates,checks=None):
        if not isinstance(candidates,list) or not 2<=len(candidates)<=8:
            raise ValueError('Arena needs 2–8 candidates.')
        p=self.co.pipeline.project(project_id);original=self.co.pipeline.workspace(p)
        if checks is None:
            approved=self.co.pipeline._approved_contract(project_id)
            if not approved:raise ValueError('Arena needs approved acceptance checks or explicit human benchmark commands.')
            checks=[c['command'] for c in json.loads(approved)['checks']]
        if not isinstance(checks,list) or not 1<=len(checks)<=30 or not all(isinstance(c,str) and c.strip() for c in checks):
            raise ValueError('Arena needs nonempty executable benchmark commands.')
        if self.co.s.provider!='mock' and self.co.s.sandbox_mode!='docker':
            raise ValueError('Live arena competitions require Docker command isolation.')
        names=[]
        for c in candidates:
            if not isinstance(c,dict) or not isinstance(c.get('name'),str) or not c['name'].strip():
                raise ValueError('Each arena candidate needs a unique name and an agent.')
            names.append(c['name'])
            agent=self.co.org.agent(c['agent'])
            if self.co.org.role(agent['role'])['kind']!='builder':raise ValueError('Only builders may enter the arena.')
            self.co.learning.prepare('builder',goal,c.get('strategy','baseline'))
            if self.co.s.provider!='mock' and str(c.get('model') or agent['model']).startswith('cli:'):
                raise ValueError('Host CLI engines cannot enter isolated live arenas; use API models.')
        if len(set(names))!=len(names):raise ValueError('Arena candidate names must be unique.')
        run=self.db.run('INSERT INTO arena_runs(project_id,status,goal,benchmark,created_at) VALUES(?,?,?,?,?)',
                        project_id,'running',goal,json.dumps(checks),now())
        base=self.co.s.root/'.orgforge'/'arena'/str(run);base.mkdir(parents=True)
        snapshot=base/'baseline'
        results=[]
        try:
            shutil.copytree(original.root,snapshot,ignore=shutil.ignore_patterns(*EXCLUDED),symlinks=True)
            baseline=hashes(snapshot)
            fixtures={name:digest for name,digest in baseline.items() if name=='product.json' or name.startswith(('tests/','benchmarks/'))}
            for index,c in enumerate(candidates):
                if hashes(snapshot)!=baseline:raise ValueError('Arena baseline integrity changed.')
                if not self.co.pipeline._budget_ok(project_id):break
                workspace=base/f'candidate-{index}';shutil.copytree(snapshot,workspace)
                ws=Workspace(workspace,mode=self.co.s.sandbox_mode,docker_image=self.co.s.docker_image,
                             docker_network=self.co.s.docker_network,timeout=self.co.s.command_timeout)
                result={'name':c['name'],'strategy':c.get('strategy','baseline'),'passed':False,'checks':[],
                        'workspace':str(workspace),'error':''}
                execution=None
                try:
                    agent=self.co.org.agent(c['agent'])
                    if c.get('model'):agent={**agent,'model':c['model']}
                    execution=self.co.runtime.run(agent,goal+'\nDo not edit existing test/benchmark fixtures or product.json.',ws,
                            project_id=project_id,only_tools={'read_file','list_files','write_file','replace_in_file','run_command'},
                            meta={'purpose':'arena','strategy':result['strategy'],'model_override':True})
                    after=hashes(workspace)
                    if any(after.get(k)!=v for k,v in fixtures.items()):raise ValueError('Candidate changed a pinned benchmark fixture.')
                    for command in checks:
                        output=ws.run_command(command)
                        result['checks'].append({'command':command,'passed':output.splitlines()[0]=='exit code 0','output':output})
                    after=hashes(workspace)
                    if any(after.get(k)!=v for k,v in fixtures.items()):raise ValueError('Benchmark fixtures changed during execution.')
                    result['passed']=execution.completed and all(check['passed'] for check in result['checks'])
                    route=self.db.one('SELECT * FROM routing_decisions WHERE id=?',execution.route_id)
                    result.update(model=route['model'],cost=route['cost'],priced=route['priced'],seconds=route['latency'])
                    self.co.router.evaluate(execution.route_id,result['passed'],100 if result['passed'] else 0,
                                            lessons='Immutable arena benchmarks '+('passed' if result['passed'] else 'failed'))
                except Exception as exc:
                    result['error']=f'{type(exc).__name__}: {exc}'
                    if execution and execution.route_id:
                        self.co.router.evaluate(execution.route_id,False,0,lessons=result['error'])
                if hashes(snapshot)!=baseline:raise ValueError('Candidate corrupted the shared arena baseline.')
                results.append(result)
            eligible=[r for r in results if r['passed']]
            winner=min(eligible,key=lambda r:(r.get('cost',0) if r.get('priced') else float('inf'),r.get('seconds',float('inf')))) if eligible else None
            if len(results)!=len(candidates):winner=None
            self.db.run('UPDATE arena_runs SET status=?,winner=?,results=? WHERE id=?',
                        'completed' if len(results)==len(candidates) else 'budget_blocked',winner['name'] if winner else None,json.dumps(results),run)
            if winner:
                self.co.memory.remember(f"Arena {run}: {winner['name']} passed every pinned benchmark using strategy {winner['strategy']}.",
                                        'Arena',project_id,category='agent',source=f'arena:{run}')
        except Exception:
            self.db.run("UPDATE arena_runs SET status='failed',results=? WHERE id=?",json.dumps(results),run);raise
        return self.get(run)

    def get(self,run_id):
        row=self.db.one('SELECT * FROM arena_runs WHERE id=?',run_id)
        if not row:raise ValueError('Unknown arena run.')
        return {**row,'results':json.loads(row['results']),'benchmark':json.loads(row['benchmark'])}
