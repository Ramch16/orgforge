import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from vittics_builder.company import Company
from vittics_builder.federation import canonical
from vittics_builder.hosting import application
from vittics_builder.llm import MockProvider
from vittics_builder.server import create_app
from vittics_builder.workers import Workers
from test_integrations import local_app, browser_available


def project(co):
    return co.pipeline.create_project('Product', 'Build a useful application')


def test_reviewed_learning_explores_then_uses_success(co):
    co.s.raw['learning']={'enabled':True,'min_trials':1,'strategies':{'test_first':{'prompt':'Write tests first.','kinds':['builder']}}}
    p=project(co);agent=co.org.staff(kind='builder')[0];ws=co.pipeline.workspace(p)
    assert co.learning.prepare('builder','Python refactor')[0]=='baseline'
    for strategy,success in [('baseline',False),('test_first',True)]:
        result=co.runtime.run(agent,'Python refactor',ws,project_id=p['id'],meta={'strategy':strategy})
        assert co.learning.summary()==[] if strategy=='baseline' else len(co.learning.summary())==1
        co.router.evaluate(result.route_id,success,100 if success else 0,lessons='Reviewed test result')
        co.router.evaluate(result.route_id,success,100 if success else 0,lessons='Updated review')
    selected,signature,prompt=co.learning.prepare('builder','Python refactor')
    assert selected=='test_first' and signature=='python,refactor' and 'Write tests first' in prompt
    assert sum(r['attempts'] for r in co.learning.summary())==2
    with pytest.raises(ValueError):co.learning.prepare('reviewer','Work','test_first')


def test_observations_deduplicate_redact_and_remain_backlog(co):
    p=project(co)
    args=(p['id'],'production','API crashed','password=secretvalue alice@example.com')
    first=co.observability.ingest(*args,event_key='crash')
    second=co.observability.ingest(*args,event_key='crash')
    assert first['id']==second['id'] and second['occurrences']==2
    assert 'secretvalue' not in second['body'] and 'alice@' not in second['body']
    assert co.tickets.get(first['ticket_id'])['status']=='backlog'
    assert co.observability.ingest(p['id'],'metrics','Healthy','All checks passed',severity='info')['ticket_id'] is None
    assert co.pipeline.project(p['id'])['stage']==p['stage']


def test_worker_failure_retry_lease_recovery_and_disable(co):
    p=project(co)
    job=co.workers.add('Regression',p['id'],'check',10,{'command':'python -c "raise SystemExit(1)"'})
    co.db.run('UPDATE worker_jobs SET lease_until=? WHERE id=?',time.time()+100,job['id'])
    assert Workers(co).tick()==[]
    co.db.run('UPDATE worker_jobs SET lease_until=0 WHERE id=?',job['id'])
    co.db.run("INSERT INTO worker_runs(job_id,status,started_at) VALUES(?,'running','old')",job['id'])
    [run]=co.workers.tick()
    assert run['status']=='failed'
    assert co.db.one("SELECT status FROM worker_runs WHERE started_at='old'")['status']=='abandoned'
    saved=co.workers.list()[0]
    assert saved['failures']==1 and saved['lease_until']==0 and saved['next_run']>time.time()+15
    assert co.observability.list()[0]['ticket_id']
    co.db.run('UPDATE worker_jobs SET next_run=0 WHERE id=?',job['id']);co.workers.enable(job['id'],False)
    assert co.workers.tick()==[]
    reopened=Company(co.s.root,provider=MockProvider())
    assert reopened.workers.list()[0]['enabled']==0 and not reopened.workers.service()['running']


def test_worker_service_persists_and_stops(co):
    assert not co.workers.service()['enabled']
    assert co.workers.service(True)['running']
    assert not co.workers.service(False)['running']
    assert not Company(co.s.root,provider=MockProvider()).workers.service()['enabled']


def candidates(co):
    agent=co.org.staff(kind='builder')[0]['id']
    return [{'name':'A','agent':agent},{'name':'B','agent':agent}]


def test_arena_winner_and_failed_benchmarks(co):
    p=project(co)
    result=co.arena.compete(p['id'],'Compare approaches',candidates(co),['python -c "print(42)"'])
    assert result['status']=='completed' and result['winner'] in ('A','B')
    assert all(r['passed'] for r in result['results'])
    assert len(co.learning.summary())==1 and co.learning.summary()[0]['attempts']==2
    failed=co.arena.compete(p['id'],'Compare approaches',candidates(co),['python -c "raise SystemExit(1)"'])
    assert failed['winner'] is None and not any(r['passed'] for r in failed['results'])


def test_arena_rejects_fixture_tampering(co,monkeypatch):
    p=project(co);ws=co.pipeline.workspace(p)
    ws.write_file('tests/check.py','assert True\n')
    original=co.runtime.run
    def cheating(agent,instructions,workspace,**kwargs):
        result=original(agent,instructions,workspace,**kwargs)
        workspace.write_file('tests/check.py','assert False\n')
        return result
    monkeypatch.setattr(co.runtime,'run',cheating)
    run=co.arena.compete(p['id'],'Compare approaches',candidates(co),['python tests/check.py'])
    assert run['winner'] is None and all('pinned benchmark' in r['error'] for r in run['results'])
    assert ws.read_file('tests/check.py')=='assert True\n'


def test_live_arena_requires_isolation(co):
    p=project(co);co.s.provider='anthropic'
    with pytest.raises(ValueError,match='Docker'):co.arena.compete(p['id'],'Work',candidates(co),['true'])


def test_board_independence_ceo_decision_and_staleness(co):
    p=project(co);board=co.assessments.board(p['id'])
    assert board['status']=='pending_ceo' and len(board['report']['evaluations'])==3
    approval=next(a for a in co.pipeline.inbox() if a['kind']=='board')
    with pytest.raises(Exception):co.pipeline.decide(approval['id'],'cto','approved')
    ws=co.pipeline.workspace(p);ws.write_file('new.txt','changed')
    with pytest.raises(Exception,match='stale'):co.pipeline.decide(approval['id'],'ceo','approved')
    (ws.root/'new.txt').unlink()
    co.pipeline.decide(approval['id'],'ceo','rejected','Improve onboarding')
    assert co.assessments.get(board['id'])['status']=='changes_requested'
    assert co.db.one("SELECT status FROM tasks WHERE origin='board'")['status']=='backlog'
    assert co.pipeline.project(p['id'])['stage']==p['stage']


def test_red_team_requires_executable_checks_and_creates_incident(co):
    p=project(co)
    with pytest.raises(ValueError):co.assessments.red_team(p['id'],[])
    with pytest.raises(ValueError):co.assessments.red_team(p['id'],['true']*31)
    run=co.assessments.red_team(p['id'],['python -c "raise SystemExit(1)"'])
    assert not run['report']['passed'] and co.observability.list()[0]['severity']=='critical'


def package():
    return {'name':'company/testing','version':'1.0.0'}, {'testing.md':'# Testing\nUse independent regression tests.'}


def test_marketplace_pins_immutable_versions_and_content(co):
    manifest,assets=package();pkg=co.marketplace.register(manifest,assets)
    with pytest.raises(ValueError,match='pin'):co.marketplace.install(pkg['name'],'wrong')
    with pytest.raises(ValueError,match='cannot change'):co.marketplace.register(manifest,{'testing.md':'# Different content with enough characters.'})
    result=co.marketplace.install(pkg['name'],pkg['sha256'])
    assert (co.s.root/'skills'/result['skills'][0]).read_text()==assets['testing.md']
    assert co.marketplace.install(pkg['name'],pkg['sha256'])['already_installed']


@pytest.mark.parametrize('assets',[{'../bad.md':'x'*30},{'script.py':'x'*30}])
def test_marketplace_denies_unsafe_assets(co,assets):
    with pytest.raises(ValueError):co.marketplace.register(package()[0],assets)


def peers(co,monkeypatch,pid):
    monkeypatch.setenv('TEST_PEER_KEY','test-signing-key-'+'x'*40)
    co.s.raw['federation']={'enabled':True,'identity':'local','peers':{'remote':{'key_env':'TEST_PEER_KEY','projects':[pid],'capabilities':['observation','task_proposal','task_result'],'roles':['backend_engineer']}}}
    def message(kind,payload):
        import os
        signed=co.federation.envelope('remote',kind,payload)
        signed.update(sender='remote',recipient='local');signed.pop('signature')
        signed['signature']=hmac.new(os.environ['TEST_PEER_KEY'].encode(),canonical(signed),hashlib.sha256).hexdigest()
        return signed
    return message


def test_federation_signed_replay_redaction_and_scope(co,monkeypatch):
    p=project(co);message=peers(co,monkeypatch,p['id'])
    envelope=message('task_proposal',{'project_id':p['id'],'title':'Fix login','body':'password=secret alice@example.com','role':'backend_engineer'})
    receipt=co.federation.receive(envelope)
    assert co.federation.receive(envelope)['duplicate']
    task=co.tickets.get(receipt['ticket_id']);assert task['status']=='backlog' and 'secret' not in task['description']
    reopened=Company(co.s.root,provider=MockProvider());reopened.s.raw['federation']=co.s.raw['federation']
    assert reopened.federation.receive(envelope)['duplicate']
    tampered={**envelope,'id':'tampered'}
    with pytest.raises(PermissionError,match='signature'):co.federation.receive(tampered)
    with pytest.raises(PermissionError,match='scope'):co.federation.receive(message('observation',{'project_id':999,'title':'Error','body':'Failed'}))
    with pytest.raises(PermissionError,match='role'):co.federation.receive(message('task_proposal',{'project_id':p['id'],'title':'Task','body':'Work','role':'ceo'}))
    result=co.federation.receive(message('task_result',{'project_id':p['id'],'ticket_id':task['id'],'body':'Completed remotely'}))
    assert result['review_required'] and co.tickets.get(task['id'])['status']=='backlog'


def test_operations_auth_and_telemetry_separate_credentials(co,monkeypatch):
    p=project(co);monkeypatch.setenv('VITTICS_OBSERVABILITY_TOKEN','telemetry-'+'x'*32)
    co.s.raw['observability']={'projects':[p['id']]}
    with TestClient(create_app(co,{'ceo':'test-ceo','cto':'test-cto'})) as client:
        assert client.get('/healthz').status_code==200
        assert client.get('/api/operations').status_code==401
        assert client.get('/api/operations',headers={'X-Token':'test-cto'}).status_code==200
        data={'project_id':p['id'],'source':'production','title':'Error','body':'Request failed'}
        assert client.post('/api/telemetry/events',json=data).status_code==403
        response=client.post('/api/telemetry/events',json=data,headers={'X-Observation-Token':'telemetry-'+'x'*32})
        assert response.status_code==200
        assert client.post(f"/api/projects/{p['id']}/board",json={},headers={'X-Token':'test-cto'}).status_code==403


def test_hosted_application_requires_distinct_tokens_and_persists(tmp_path,monkeypatch):
    monkeypatch.setenv('VITTICS_HOME',str(tmp_path/'hosted'))
    with pytest.raises(ValueError):application()
    monkeypatch.setenv('VITTICS_CEO_TOKEN','a'*32);monkeypatch.setenv('VITTICS_CTO_TOKEN','b'*32)
    with TestClient(application()) as client:
        assert client.get('/healthz').status_code==200
    with TestClient(application()) as client:
        assert client.get('/api/operations',headers={'X-Token':'a'*32}).status_code==200


@pytest.mark.skipif(not browser_available(),reason='Chromium not installed')
def test_customer_personas_use_real_browser_and_fail_to_ticket(co,local_app):
    p=project(co);co.s.raw['tools']={'browser':{'enabled':True,'timeout_seconds':5}}
    run=co.assessments.customers(p['id'],{
        'beginner':{'url':local_app,'steps':[{'action':'fill','selector':'#name','value':'Customer'},{'action':'click','selector':'#login'},{'action':'assert_text','selector':'#greeting','value':'Welcome Customer'}]},
        'end_user':{'url':local_app,'steps':[{'action':'assert_text','value':'Missing feature'}]}})
    assert run['status']=='completed' and not run['report']['passed']
    assert run['report']['personas'][0]['passed'] and not run['report']['personas'][1]['passed'], run['report']
    assert co.observability.list()[0]['ticket_id']


@pytest.mark.skipif(not browser_available(),reason='Chromium not installed')
def test_customer_agent_plans_and_executor_verifies(co,local_app):
    p=project(co);co.s.raw['tools']={'browser':{'enabled':True,'timeout_seconds':15}}
    run=co.assessments.customers(p['id'],{'developer':{'url':local_app,'goal':'Sign in and confirm the welcome message.'}})
    assert run['report']['passed']
    assert run['report']['personas'][0]['evidence']['steps_passed']==3
    assert co.org.staff(role='customer_developer')


def test_model_routing_uses_specific_task_evidence(co):
    co.s.raw['routing']={'enabled':True,'rules':{'builder':['claude-haiku-4-5','claude-sonnet-5-5']}}
    agent=co.org.staff(kind='builder')[0]
    for model,signature,success in [('claude-haiku-4-5','python',True),('claude-sonnet-5-5','python',False),
                                    ('claude-haiku-4-5','frontend',False),('claude-sonnet-5-5','frontend',True)]:
        _,route,start=co.router.start({**agent,'model':model},None,{'model_override':True,'signature':signature},'builder')
        co.router.finish(route,start,'completed',None);co.router.evaluate(route,success,100 if success else 0)
    assert co.router.select(agent,'builder',{'signature':'python'})[0]=='claude-haiku-4-5'
    assert co.router.select(agent,'builder',{'signature':'frontend'})[0]=='claude-sonnet-5-5'


def test_security_findings_hide_actual_credential(co):
    p=project(co);ws=co.pipeline.workspace(p)
    credential='sk-ant-'+'x'*40
    ws.write_file('config.py','API_KEY="'+credential+'"')
    result=co.security.scan(p['id'])
    assert not result['passed'] and result['findings'][0]['path']=='config.py'
    assert credential not in json.dumps(result)+json.dumps(co.observability.list())
    with pytest.raises(ValueError,match='opt-in'):co.security.dependencies(p['id'])


@pytest.mark.skipif(not browser_available(),reason='Chromium not installed')
def test_operations_dashboard_schedules_and_triages_in_browser(co,tmp_path):
    import socket
    import threading
    import shutil
    from pathlib import Path
    import uvicorn
    from playwright.sync_api import sync_playwright,expect
    p=project(co)
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen()
    url=f'http://127.0.0.1:{listener.getsockname()[1]}'
    server=uvicorn.Server(uvicorn.Config(create_app(co,{'ceo':'test-ceo','cto':'test-cto'}),log_level='error'))
    thread=threading.Thread(target=lambda:server.run(sockets=[listener]),daemon=True);thread.start()
    try:
        for _ in range(100):
            if server.started:break
            time.sleep(.01)
        assert server.started
        with sync_playwright() as playwright:
            executable=None if Path(playwright.chromium.executable_path).exists() else shutil.which('chromium')
            browser=playwright.chromium.launch(executable_path=executable)
            try:
                page=browser.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(url);page.locator('#token').fill('test-ceo');page.locator('#signin-btn').click()
                page.get_by_role('button',name='Operations',exact=True).click()
                page.locator('#ops-name').fill('Nightly QA');page.locator('#ops-project').select_option(str(p['id']))
                page.locator('#ops-command').fill('python -c "raise SystemExit(1)"')
                page.locator('#ops-worker-form button[type=submit]').click()
                expect(page.locator('#ops-workers')).to_contain_text('Nightly QA')
                page.locator('#ops-tick').click()
                expect(page.locator('#ops-observations')).to_contain_text('Nightly QA detected a failure')
                assert co.tickets.get(co.observability.list()[0]['ticket_id'])['status']=='backlog'
                page.screenshot(path=str(tmp_path/'operations.png'),full_page=True)
                assert not errors
            finally:browser.close()
    finally:
        server.should_exit=True;thread.join(timeout=10);listener.close()


def test_observation_escalation_creates_one_ticket_and_increases_priority(co):
    p=project(co)
    first=co.observability.ingest(p['id'],'production','Slow requests','Threshold approached','info','latency')
    assert not first['ticket_id']
    critical=co.observability.ingest(p['id'],'production','Slow requests','Threshold exceeded','critical','latency')
    assert critical['id']==first['id'] and critical['occurrences']==2
    assert co.tickets.get(critical['ticket_id'])['priority']=='urgent'
    repeat=co.observability.ingest(p['id'],'production','Slow requests','Recovered partially','warning','latency')
    assert repeat['severity']=='critical' and repeat['ticket_id']==critical['ticket_id']


def test_marketplace_role_install_and_corruption_fail_closed(co):
    manifest,assets=package()
    department=co.org.role('backend_engineer')['department']
    manifest['roles']=[{'id':'test_advisor','department':department,'kind':'advisor','tools':['read_file'],'prompt':'Independently inspect test quality.'}]
    pkg=co.marketplace.register(manifest,assets)
    co.db.run('UPDATE marketplace SET assets=? WHERE name=?',json.dumps({'testing.md':'# Tampered content with enough characters.'}),pkg['name'])
    with pytest.raises(ValueError,match='integrity'):co.marketplace.install(pkg['name'],pkg['sha256'])
    assert not co.db.one("SELECT id FROM roles WHERE id='test_advisor'")
    co.db.run('UPDATE marketplace SET assets=? WHERE name=?',json.dumps(assets),pkg['name'])
    co.marketplace.install(pkg['name'],pkg['sha256'])
    assert co.org.role('test_advisor')['kind']=='advisor'
    assert not co.org.staff(role='test_advisor')


def test_concurrent_workers_claim_once(co,monkeypatch):
    import threading
    p=project(co);job=co.workers.add('Exclusive check',p['id'],'check',10,{'command':'true'})
    entered=threading.Event();release=threading.Event();calls=[]
    first,second=Workers(co),Workers(co)
    def execute(job):
        calls.append(job['id']);entered.set();assert release.wait(5);return {'passed':True}
    monkeypatch.setattr(first,'_execute',execute)
    thread=threading.Thread(target=first.tick);thread.start()
    try:
        assert entered.wait(5)
        assert second.tick()==[]
    finally:release.set();thread.join(5)
    assert calls==[job['id']]


def test_federation_expiry_and_http_transport_limits(co,monkeypatch):
    import os
    p=project(co);message=peers(co,monkeypatch,p['id'])
    envelope=message('observation',{'project_id':p['id'],'title':'Error','body':'Failed'})
    envelope['issued_at']=int(time.time())-400;envelope['expires_at']=int(time.time())-100
    signed={k:v for k,v in envelope.items() if k!='signature'}
    envelope['signature']=hmac.new(os.environ['TEST_PEER_KEY'].encode(),canonical(signed),hashlib.sha256).hexdigest()
    with pytest.raises(PermissionError,match='expired'):co.federation.receive(envelope)
    co.s.raw['federation']['peers']['remote']['url']='http://untrusted.example.test'
    with pytest.raises(ValueError,match='loopback'):co.federation.send('remote','observation',{})


def test_board_missing_structured_verdict_never_creates_approval(co,monkeypatch):
    from vittics_builder.agent import RunResult
    p=project(co)
    monkeypatch.setattr(co.runtime,'run',lambda *a,**kw:RunResult(completed=True,text='Everything looks good.'))
    with pytest.raises(ValueError,match='complete verdict'):co.assessments.board(p['id'])
    assert not any(a['kind']=='board' for a in co.pipeline.inbox())
    assert co.db.one("SELECT status FROM company_assessments WHERE kind='board'")['status']=='failed'
