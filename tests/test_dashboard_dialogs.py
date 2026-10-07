"""Desktop webviews do not implement native JS prompt/confirm dialogs."""
import importlib.util
from pathlib import Path
import shutil
import socket
import threading
import time

import pytest
import uvicorn
from vittics_builder.server import create_app


def browser_available():
    if not importlib.util.find_spec("playwright"):
        return False
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        return Path(p.chromium.executable_path).exists() or bool(shutil.which("chromium") or shutil.which("chromium-browser"))


@pytest.mark.skipif(not browser_available(), reason="Optional Playwright/Chromium not installed")
def test_buttons_work_without_native_dialogs(co, monkeypatch):
    from playwright.sync_api import sync_playwright, expect
    monkeypatch.setattr(co.ai, 'has_key', lambda: False)
    project = co.pipeline.create_project('Dialog tests', 'UI regression checks', idea=True)
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0)); listener.listen()
    url = f'http://127.0.0.1:{listener.getsockname()[1]}'
    server = uvicorn.Server(uvicorn.Config(create_app(co, {'ceo': 'test-ceo', 'cto': 'test-cto'}), log_level='error'))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started: break
            time.sleep(.01)
        assert server.started
        with sync_playwright() as p:
            executable = None if Path(p.chromium.executable_path).exists() else shutil.which("chromium") or shutil.which("chromium-browser")
            browser = p.chromium.launch(executable_path=executable, headless=True)
            try:
                page = browser.new_page()
                errors = []
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.add_init_script("window.confirm = window.prompt = window.alert = () => { throw new Error('native dialog used'); };")
                page.goto(url)
                page.locator('#signin-btn').click()
                expect(page.locator('#signin-error')).to_contain_text('Enter')
                page.locator('#token').fill('test-ceo'); page.locator('#signin-btn').click()
                page.get_by_role('button', name='Machine', exact=True).click()
                page.locator('[data-ai-use="api"]').click()
                expect(page.locator('#action-dlg')).to_be_visible()
                page.locator('#action-cancel').click()
                expect(page.locator('#action-dlg')).not_to_be_visible()
                assert co.ai.saved() is None
                page.locator('[data-ai-use="api"]').click()
                page.locator('#action-ok').click()
                expect(page.locator('#error')).to_contain_text('key')
                page.locator('#machine-pair').click()
                page.locator('#action-input').fill('test-worker')
                page.locator('#action-ok').click()
                expect(page.locator('#machine-pairing')).to_contain_text('test-worker')
                page.goto(url + f'/#/project/{project["id"]}')
                page.locator('[data-budget]').click()
                page.locator('#action-input').fill('-1')
                page.locator('#action-ok').click()
                expect(page.locator('#action-dlg')).to_be_visible()
                page.locator('#action-input').fill('12.5')
                page.locator('#action-ok').click()
                expect(page.locator('#action-dlg')).not_to_be_visible()
                expect(page.locator('#project-view')).to_contain_text('$12.50')
                assert co.pipeline.project(project['id'])['budget'] == 12.5
                page.locator('[data-key-add]').click()
                page.locator('#key-save').click()
                expect(page.locator('#key-dlg')).to_be_visible()
                page.locator('#key-form button[value="cancel"]').click()
                page.get_by_role('button', name='Team', exact=True).click()
                page.locator('[data-fire]').first.click()
                expect(page.locator('#action-title')).to_have_text('Replace agent')
                page.locator('#action-input').fill('   ')
                page.locator('#action-ok').click()
                expect(page.locator('#action-error')).to_contain_text('Enter')
                page.keyboard.press('Escape')
                expect(page.locator('#action-dlg')).not_to_be_visible()
                page.get_by_role('button', name='Machine', exact=True).click()
                page.locator('#machine-pair').click()
                expect(page.locator('#action-dlg')).to_be_visible()
                page.locator('#action-cancel').click()
                assert not errors
            finally:
                browser.close()
    finally:
        server.should_exit = True; thread.join(timeout=10); listener.close()
