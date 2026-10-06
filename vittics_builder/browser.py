"""Native, bounded browser journeys with per-request origin policy and workspace artifacts."""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from urllib.parse import urlsplit

from .tools import MAX_OUTPUT, SECRET_ENV, ToolError


def permitted(url, config):
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password:
            return False
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        origin = f'{parsed.scheme}://{parsed.hostname}:{port}'
        configured = config.get('allowed_origins') or []
        # Defaults are development apps on literal loopback hosts, with any local port.
        if not configured:
            return parsed.hostname in ('localhost', '127.0.0.1', '::1')
        for entry in configured:
            p = urlsplit(entry)
            if p.scheme in ('http', 'https') and not p.username and not p.password and \
                    p.path in ('', '/') and not p.query and not p.fragment:
                other = f'{p.scheme}://{p.hostname}:{p.port or (443 if p.scheme == "https" else 80)}'
                if origin == other:
                    return True
        return False
    except (ValueError, TypeError):
        return False


def journey(ws, config, args):
    if not permitted(args.get('url', ''), config):
        raise ToolError('Browser URL is outside the configured allowed origins.')
    steps = args.get('steps') or []
    if not isinstance(steps, list) or len(steps) > 30:
        raise ToolError('A browser journey supports at most 30 steps.')
    timeout = min(120, max(1, int(config.get('timeout_seconds', 30))))
    for step in steps:
        if not isinstance(step, dict) or step.get('action') not in ('click', 'fill', 'assert_text', 'screenshot', 'goto'):
            raise ToolError('Unsupported browser action.')
        if step['action'] == 'goto' and not permitted(step.get('value', ''), config):
            raise ToolError('Browser navigation is outside the configured allowed origins.')
        if step['action'] == 'screenshot':
            target = ws.resolve(step.get('path') or '.vittics/browser/screenshot.png')
            if target.suffix.lower() != '.png':
                raise ToolError('Browser screenshots must be PNG files inside the workspace.')
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ToolError('Browser support needs pip install -e ".[browser]" and playwright install chromium.') from exc
    blocked, screenshots = [], []
    deadline = time.monotonic() + timeout
    try:
        with sync_playwright() as playwright:
            executable = config.get('executable_path') or os.environ.get('VITTICS_BROWSER_EXECUTABLE')
            if not executable and not Path(playwright.chromium.executable_path).exists():
                executable = shutil.which('chromium') or shutil.which('chromium-browser')
            browser = playwright.chromium.launch(headless=True, executable_path=executable, timeout=timeout * 1000,
                                                  env={k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)})
            try:
                context = browser.new_context(service_workers='block', accept_downloads=False)
                def route(request):
                    if permitted(request.request.url, config):
                        request.continue_()
                    else:
                        blocked.append(request.request.url.split('?')[0])
                        request.abort()
                context.route('**/*', route)
                context.route_web_socket('**/*', lambda socket: socket.close())
                page = context.new_page()
                page.goto(args['url'], timeout=max(1, (deadline - time.monotonic()) * 1000))
                for step in steps:
                    remaining = max(0, deadline - time.monotonic()) * 1000
                    if remaining <= 0:
                        raise ToolError('Browser journey exceeded its time budget.')
                    page.set_default_timeout(remaining)
                    action = step['action']
                    if action == 'goto':
                        page.goto(step['value'], timeout=remaining)
                    elif action == 'click':
                        page.locator(step['selector']).click(timeout=remaining)
                    elif action == 'fill':
                        page.locator(step['selector']).fill(step['value'], timeout=remaining)
                    elif action == 'assert_text':
                        from playwright.sync_api import expect
                        expect(page.locator(step.get('selector', 'body'))).to_contain_text(step['value'], timeout=remaining)
                    else:
                        target = ws.resolve(step.get('path') or '.vittics/browser/screenshot.png')
                        target.parent.mkdir(parents=True, exist_ok=True)
                        page.screenshot(path=str(target), full_page=True, timeout=remaining)
                        screenshots.append(target.relative_to(ws.root).as_posix())
                if blocked:
                    raise ToolError('Browser journey attempted requests outside its allowed origins.')
                return json.dumps({'url': page.url, 'title': page.title(), 'steps_passed': len(steps),
                                   'text': page.locator('body').inner_text(timeout=max(1, (deadline-time.monotonic())*1000))[:8000],
                                   'screenshots': screenshots})[:MAX_OUTPUT]
            finally:
                browser.close()
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(f'Browser journey failed ({type(exc).__name__}); inspect the application and selectors.') from exc
