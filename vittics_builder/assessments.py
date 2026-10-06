"""Artificial customers, independent board reviews, and executable red-team checks."""
import json
import hashlib

from .db import now
from .tools import ToolError

DIMENSIONS=('functionality','ux','security','cost','architecture','scalability','customer_value','maintainability')
PERSONAS=('enterprise_admin','developer','beginner','security_officer','end_user')


class Assessments:
    def __init__(self,company):
        self.co,self.db=company,company.db

    def _start(self,pid,kind):
        p=self.co.pipeline.project(pid);ws=self.co.pipeline.workspace(p)
        revision=ws.git('rev-parse','HEAD')
        run=self.db.run('INSERT INTO company_assessments(project_id,kind,status,revision,created_at) VALUES(?,?,?,?,?)',
                        pid,kind,'running',revision,now())
        return run,p,ws

    def customers(self,project_id,journeys):
        if not isinstance(journeys,dict) or not journeys or set(journeys)-set(PERSONAS):
            raise ValueError('Provide executable journeys for supported customer personas.')
        config=(self.co.s.raw.get('tools') or {}).get('browser') or {}
        if config.get('enabled') is not True:raise ValueError('Customer simulations require enabled company browser configuration.')
        from .browser import journey
        run,p,ws=self._start(project_id,'customers');results=[]
        try:
            for persona,spec in journeys.items():
                if isinstance(spec,dict) and spec.get('goal'):
                    spec=self._plan_customer(project_id,persona,spec,ws)
                if not isinstance(spec,dict) or not isinstance(spec.get('steps'),list) or not spec['steps'] or not all(isinstance(s,dict) for s in spec['steps']) or not any(s.get('action')=='assert_text' for s in spec['steps']):
                    raise ValueError('Each customer journey needs an executable assertion.')
                try:
                    evidence=json.loads(journey(ws,config,spec));passed=True;error=''
                except ToolError as exc:
                    evidence={};passed=False;error=str(exc)
                    self.co.observability.ingest(project_id,'ai-customer:'+persona,persona+' could not complete a user journey',
                                               error,event_key=persona+':'+hashlib.sha256(json.dumps(spec,sort_keys=True).encode()).hexdigest())
                results.append({'persona':persona,'journey':spec,'passed':passed,'evidence':evidence,'error':error})
            report={'passed':all(r['passed'] for r in results),'personas':results}
            self._finish(run,'completed',report)
        except Exception as exc:
            self._finish(run,'failed',{'error':str(exc)});raise
        return self.get(run)

    def _plan_customer(self,project_id,persona,spec,ws):
        from .browser import permitted
        config=(self.co.s.raw.get('tools') or {}).get('browser') or {}
        if not permitted(spec.get('url',''),config):raise ValueError('Customer application URL is outside allowed origins.')
        if not isinstance(spec['goal'],str) or not 1<=len(spec['goal'])<=8000:raise ValueError('Customer goal must be bounded text.')
        if not self.co.pipeline._budget_ok(project_id):raise ValueError('Customer planning paused at the project budget.')
        if not self.db.one("SELECT id FROM departments WHERE id='customers'"):
            self.co.org.add_department('customers','AI Customers','ceo')
        role='customer_'+persona
        if not self.db.one('SELECT id FROM roles WHERE id=?',role):
            self.co.org.add_role(role,'customers','customer',['read_file','list_files','submit_journey'],
                                'Represent '+persona.replace('_',' ')+'. Evaluate the real user goal. '
                                'Read UI source and documentation. Choose realistic browser actions and meaningful assertions.',title=persona.replace('_',' ').title())
        if not self.co.org.staff(role=role):self.co.org.hire(role,by='AI Customers')
        result=self.co.runtime.run(self.co.org.pick(role=role),'Plan a user journey for this goal: '+spec['goal']+'\nApplication URL: '+spec['url']+
                    '\nRead the UI files to choose selectors. Submit steps with submit_journey; include a meaningful assert_text. '
                    'A separate browser executor verifies the plan.',ws,project_id=project_id,
                    only_tools={'read_file','list_files','recall_memory'},extra_tools=['submit_journey'],
                    meta={'purpose':'customer_plan','customer_url':spec['url']})
        if not result.completed or not result.journey:raise ValueError('Customer agent did not submit an executable journey.')
        return {'url':spec['url'],**result.journey}

    def board(self,project_id):
        workspace=self.co.pipeline.workspace(self.co.pipeline.project(project_id))
        if workspace.changed_files()!='(no uncommitted changes)':
            raise ValueError('Commit product changes before requesting a board assessment.')
        run,p,ws=self._start(project_id,'board');evaluations=[];used=set()
        if not self.db.one("SELECT id FROM departments WHERE id='board'"):
            self.co.org.add_department('board','Board of Directors','ceo')
        for role,title in [('board_cto','CTO judge'),('board_security','Security judge'),('board_customer','Customer judge')]:
            if not self.db.one('SELECT id FROM roles WHERE id=?',role):
                self.co.org.add_role(role,'board','advisor',['read_file','list_files','submit_evaluation'],
                                    'Judge the product independently. Base every conclusion on evidence.',title=title)
            if not self.co.org.staff(role=role):
                self.co.org.hire(role,name=title,by='Board')
        authors={r['assignee_id'] for r in self.db.all('SELECT assignee_id FROM tasks WHERE project_id=?',project_id) if r['assignee_id']}
        try:
            for role in ('board_cto','board_security','board_customer'):
                if not self.co.pipeline._budget_ok(project_id):raise ValueError('Board review paused at the project budget.')
                agent=self.co.org.pick(role=role,exclude=authors|used)
                if not agent:raise ValueError(f'No independent {role} is available for the board.')
                used.add(agent['id'])
                result=self.co.runtime.run(agent,'Independently assess functionality, UX, security, cost, architecture, scalability, '
                    'customer value and maintainability. Read product.json, verification evidence and source. '
                    'Submit all eight scores plus findings through submit_evaluation. Treat missing evidence as a finding.',ws,
                    project_id=project_id,only_tools={'read_file','list_files','recall_memory'},extra_tools=['submit_evaluation'],meta={'purpose':'board'})
                if not result.completed or not result.evaluation:raise ValueError('Board evaluator did not submit a complete verdict.')
                evaluations.append({'agent':agent['name'],'role':role,**result.evaluation})
            if ws.changed_files()!='(no uncommitted changes)' or ws.git('rev-parse','HEAD')!=self.db.one('SELECT revision FROM company_assessments WHERE id=?',run)['revision']:
                raise ValueError('Product revision changed during board evaluation.')
            scores={d:sum(e['scores'][d] for e in evaluations)/len(evaluations) for d in DIMENSIONS}
            report={'evaluations':evaluations,'scores':scores,'score':sum(scores.values())/len(scores)}
            self._finish(run,'pending_ceo',report)
            self.co.pipeline._approval(project_id,'board','ceo',f"Board assessment for {p['name']}",
                                      f"Board score {report['score']:.1f}/100. Review the evidence before deciding whether to continue.",
                                      {'assessment_id':run})
        except Exception as exc:
            self._finish(run,'failed',{'error':str(exc),'evaluations':evaluations});raise
        return self.get(run)

    def red_team(self,project_id,commands):
        if not isinstance(commands,list) or not 1<=len(commands)<=30 or not all(isinstance(c,str) and c.strip() for c in commands):
            raise ValueError('Red-team assessment needs explicit negative-test commands.')
        run,p,ws=self._start(project_id,'red_team');checks=[]
        try:
            for command in commands[:30]:
                output=ws.run_command(command)
                checks.append({'command':command,'passed':output.splitlines()[0]=='exit code 0','output':output})
            report={'passed':all(c['passed'] for c in checks),'checks':checks}
            if not report['passed']:
                self.co.observability.ingest(project_id,'red-team','Red-team negative checks failed',json.dumps(checks),severity='critical')
            self._finish(run,'completed',report)
        except Exception as exc:
            self._finish(run,'failed',{'error':str(exc)});raise
        return self.get(run)

    def _finish(self,run,status,report):
        from .observability import sanitize
        self.db.run('UPDATE company_assessments SET status=?,report=? WHERE id=?',status,json.dumps(sanitize(report)),run)

    def get(self,run):
        row=self.db.one('SELECT * FROM company_assessments WHERE id=?',run)
        if not row:raise ValueError('Unknown assessment.')
        return {**row,'report':json.loads(row['report'])}
