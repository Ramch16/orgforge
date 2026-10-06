import importlib.util
import json
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from vittics_builder.browser import permitted
from vittics_builder.integrations import Integrations
from vittics_builder.tools import ToolError, Workspace


@pytest.fixture
def local_app():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            html = b'''<html><title>Customer journey</title><body><input id="name"><button id="login"
                onclick="document.querySelector('#greeting').textContent='Welcome '+document.querySelector('#name').value">Login</button>
                <p id="greeting">Sign in</p></body></html>'''
            if self.path == '/offsite':
                html = b'<html><body><img src="https://example.com/unapproved.png"></body></html>'
            self.send_response(200); self.send_header('Content-Type','text/html'); self.end_headers(); self.wfile.write(html)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown(); server.server_close(); thread.join()


def browser_available():
    if not importlib.util.find_spec('playwright'):
        return False
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        return Path(p.chromium.executable_path).exists() or bool(shutil.which('chromium') or shutil.which('chromium-browser'))


@pytest.mark.skipif(not browser_available(), reason='Optional Playwright/Chromium not installed')
def test_real_browser_login_click_assert_and_screenshot(co, tmp_path, local_app):
    co.s.raw['tools'] = {'browser': {'enabled': True, 'roles': ['qa_engineer']}}
    adapter = Integrations(co.s)
    ws = Workspace(tmp_path)
    result = json.loads(adapter.call('qa_engineer', 'browser_journey', {'url': local_app, 'steps': [
        {'action': 'fill', 'selector': '#name', 'value': 'Customer'},
        {'action': 'click', 'selector': '#login'},
        {'action': 'assert_text', 'selector': '#greeting', 'value': 'Welcome Customer'},
        {'action': 'screenshot', 'path': '.vittics/browser/journey.png'}]}, ws))
    assert result['steps_passed'] == 4 and result['title'] == 'Customer journey'
    assert 'Welcome Customer' in result['text']
    assert (tmp_path / result['screenshots'][0]).read_bytes().startswith(b'\x89PNG')
    with pytest.raises(ToolError, match='outside its allowed origins'):
        adapter.call('qa_engineer', 'browser_journey', {'url': local_app + '/offsite'}, ws)
    with pytest.raises(ToolError, match='outside'):
        adapter.call('qa_engineer','browser_journey', {'url':local_app,'steps':[{'action':'screenshot','path':'../escape.png'}]},ws)
    with pytest.raises(ToolError, match='failed'):
        _bad_journey(adapter, ws, local_app)


def _bad_journey(adapter, ws, url):
    adapter.s.raw['tools']['browser']['timeout_seconds'] = 1
    return adapter.call('qa_engineer', 'browser_journey', {'url':url,'steps':[{'action':'assert_text','value':'missing text'}]},ws)


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'http://example.com', 'http://localhost.evil.test',
                                 'http://localhost@evil.test', 'http://user:pass@localhost', 'http://localhost:invalid'])
def test_browser_origin_policy_denies_unapproved_targets(url):
    assert not permitted(url, {})


def test_browser_exact_origins_and_local_defaults():
    assert permitted('http://127.0.0.1:4700/path', {})
    assert permitted('http://[::1]:4700/path', {})
    config = {'allowed_origins': ['https://app.example.test']}
    assert permitted('https://app.example.test/path', config)
    assert not permitted('http://app.example.test', config)
    assert not permitted('https://app.example.test:444', config)
    assert not permitted('http://localhost:4700', config)


def test_integrations_are_disabled_and_enforce_role_and_tool_allowlists(co, tmp_path):
    adapter = Integrations(co.s); ws = Workspace(tmp_path)
    assert adapter.names('qa_engineer') == []
    with pytest.raises(ToolError, match='not enabled'):
        adapter.call('qa_engineer','browser_journey',{'url':'http://localhost'},ws)
    co.s.raw['tools'] = {'mcp': {'example': {'command':sys.executable, 'roles':['qa_engineer'],'tools':['read']}}}
    assert adapter.names('backend_engineer') == []
    with pytest.raises(ToolError, match='allowlist'):
        adapter.call('qa_engineer','mcp_call',{'server':'example','tool':'write','arguments':{}},ws)
    with pytest.raises(ToolError, match='not permitted'):
        adapter.call('qa_engineer','mcp_list_tools',{'server':'other'},ws)


@pytest.mark.skipif(not importlib.util.find_spec('mcp'), reason='Optional MCP SDK not installed')
def test_real_mcp_stdio_discovery_call_and_environment_isolation(co, tmp_path, monkeypatch):
    server = tmp_path / 'server.py'
    server.write_text('''import os
from mcp.server.fastmcp import FastMCP
mcp = FastMCP("test company tools")
@mcp.tool()
def echo(message: str) -> dict:
    return {"message": message, "secret_present": "TEST_PROVIDER_SECRET" in os.environ,
            "allowed_present": "TEST_ALLOWED_TOKEN" in os.environ}
@mcp.tool()
def forbidden() -> str:
    return "should not be offered"
mcp.run(transport="stdio")
''')
    monkeypatch.setenv('TEST_PROVIDER_SECRET','dummy-test-value')
    monkeypatch.setenv('TEST_ALLOWED_TOKEN','dummy-test-value')
    co.s.raw['tools'] = {'mcp': {'example': {'command':sys.executable,'args':[str(server)],
                      'roles':['qa_engineer'],'tools':['echo'],'env_names':['TEST_ALLOWED_TOKEN']}}}
    adapter = Integrations(co.s); ws = Workspace(tmp_path)
    tools = json.loads(adapter.call('qa_engineer','mcp_list_tools',{'server':'example'},ws))
    assert [t['name'] for t in tools] == ['echo']
    assert tools[0]['input_schema']['properties']['message']['type'] == 'string'
    result = json.loads(adapter.call('qa_engineer','mcp_call',
                                   {'server':'example','tool':'echo','arguments':{'message':'hello'}},ws))
    content = result['structuredContent'] or json.loads(result['content'][0]['text'])
    assert content == {'message':'hello','secret_present':False,'allowed_present':True}


def test_agent_runtime_offers_only_permitted_integrations(co):
    from vittics_builder.llm import LLMResponse
    class Recorder:
        def __init__(self):
            self.names = []
        def complete(self, **kwargs):
            self.names = [t['name'] for t in kwargs['tools']]
            return LLMResponse(content=[{'type':'text','text':'done'}],text='done')
    recorder = Recorder(); co.runtime.provider = recorder
    co.s.raw['tools'] = {'browser': {'enabled':True,'roles':['qa_engineer']}}
    qa = co.org.staff(role='qa_engineer')[0]
    co.runtime.run(qa,'Inspect the application',Workspace(co.s.root / 'test-app'))
    assert 'browser_journey' in recorder.names
    co.runtime.run(qa,'Read only',Workspace(co.s.root / 'test-app'),only_tools={'read_file'})
    assert recorder.names == ['read_file']
    builder = co.org.staff(kind='builder')[0]
    co.runtime.run(builder,'Build',Workspace(co.s.root / 'test-app'))
    assert 'browser_journey' not in recorder.names


@pytest.mark.skipif(not browser_available(), reason='Optional Playwright/Chromium not installed')
def test_dashboard_company_knowledge_in_real_browser(co, tmp_path):
    import socket
    import time
    import uvicorn
    from playwright.sync_api import sync_playwright, expect
    from vittics_builder.server import create_app
    from test_parallel import plan_approved
    p = plan_approved(co)
    co.pipeline._claim_batch(p['id'])
    co.memory.remember('PostgreSQL chosen for transactional consistency.', 'Sony', p['id'],
                       category='architecture', source='docs/ARCHITECTURE.md')
    listener = socket.socket(); listener.bind(('127.0.0.1',0)); listener.listen()
    url = f'http://127.0.0.1:{listener.getsockname()[1]}'
    server = uvicorn.Server(uvicorn.Config(create_app(co, {'ceo':'test-ceo','cto':'test-cto'}), log_level='error'))
    thread = threading.Thread(target=lambda:server.run(sockets=[listener]),daemon=True);thread.start()
    try:
        for _ in range(100):
            if server.started: break
            time.sleep(.01)
        assert server.started
        with sync_playwright() as playwright:
            executable = None if Path(playwright.chromium.executable_path).exists() else shutil.which('chromium') or shutil.which('chromium-browser')
            browser = playwright.chromium.launch(executable_path=executable,headless=True)
            try:
                page = browser.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(url)
                page.locator('#token').fill('test-ceo');page.locator('#signin-btn').click()
                page.get_by_role('button',name='Company knowledge',exact=True).click()
                page.locator('#brain-project').select_option(str(p['id']))
                page.locator('#memory-query').fill('PostgreSQL')
                page.get_by_role('button',name='Search',exact=True).click()
                expect(page.locator('#memory-results')).to_contain_text('transactional consistency')
                expect(page.locator('#memory-results')).to_contain_text('docs/ARCHITECTURE.md')
                page.get_by_role('button',name='Show selected team',exact=True).click()
                expect(page.locator('#brain-team')).to_contain_text('backend_engineer')
                expect(page.locator('#brain-routing')).to_contain_text('Not evaluated')
                page.screenshot(path=str(tmp_path / 'company-knowledge.png'),full_page=True)
                page.goto(url + f"/#/project/{p['id']}")
                page.get_by_role('button',name='Work 5 cycles',exact=True).click()
                page.wait_for_function("document.querySelector('#project-view .page-head').textContent.includes('Release with CTO')",timeout=15000)
                assert co.pipeline.project(p['id'])['stage']=='release_approval'
                assert not errors
            finally:
                browser.close()
    finally:
        server.should_exit=True;thread.join(timeout=10);listener.close()


@pytest.mark.skipif(not browser_available(), reason='Optional Playwright/Chromium not installed')
def test_api_agent_executes_browser_tool_and_reads_functional_result(co, local_app):
    from vittics_builder.llm import LLMResponse, ToolCall
    class BrowserAgent:
        def __init__(self):
            self.calls = 0
        def complete(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                assert 'browser_journey' in {t['name'] for t in kwargs['tools']}
                arguments = {'url':local_app,'steps':[
                    {'action':'fill','selector':'#name','value':'Agent'},
                    {'action':'click','selector':'#login'},
                    {'action':'assert_text','selector':'#greeting','value':'Welcome Agent'}]}
                return LLMResponse(content=[{'type':'tool_use','id':'browser-1','name':'browser_journey','input':arguments}],
                                   tool_calls=[ToolCall('browser-1','browser_journey',arguments)],stop_reason='tool_use')
            output = kwargs['messages'][-1]['content'][0]
            assert not output['is_error']
            evidence = json.loads(output['content'])
            assert evidence['steps_passed']==3 and 'Welcome Agent' in evidence['text']
            return LLMResponse(content=[{'type':'text','text':'Login journey verified.'}],text='Login journey verified.')
    co.s.raw['tools'] = {'browser':{'enabled':True,'roles':['qa_engineer']}}
    provider = BrowserAgent();co.runtime.provider=provider
    result = co.runtime.run(co.org.staff(role='qa_engineer')[0],'Validate login',Workspace(co.s.root/'app'))
    assert result.completed and result.text=='Login journey verified.'
    assert provider.calls==2 and result.tool_log==['browser_journey']
