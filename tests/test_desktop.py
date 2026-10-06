import json
import os
import subprocess
import sys
import urllib.request

from fastapi.testclient import TestClient

from orgforge import desktop
from orgforge.server import create_app


def test_tokens_are_created_once_privately_and_kept(tmp_path):
    first = desktop.desktop_tokens(tmp_path)
    path = tmp_path / ".orgforge" / "desktop.json"
    assert first["ceo"] != first["cto"] and oct(path.stat().st_mode & 0o777) == "0o600"
    assert desktop.desktop_tokens(tmp_path) == first                 # survives restarts


def test_apps_get_a_terminal_like_path(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    path = desktop.login_path().split(":")
    assert "/usr/bin" in path and "/opt/homebrew/bin" in path
    assert str(desktop.Path.home() / ".local" / "bin") in path and len(path) == len(set(path))


def test_switching_roles_only_in_the_desktop_app(co):
    tokens = {"ceo": "ceo-token", "cto": "cto-token"}
    web = TestClient(create_app(co, tokens))
    assert web.post("/api/desktop/switch", headers={"X-Token": "ceo-token"}).status_code == 404
    assert web.get("/api/state", headers={"X-Token": "ceo-token"}).json()["desktop"] is False
    app = TestClient(create_app(co, tokens, desktop=True))
    assert app.post("/api/desktop/switch").status_code == 401
    r = app.post("/api/desktop/switch", headers={"X-Token": "ceo-token"}).json()
    assert r["role"] == "cto" and r["token"] == "cto-token"
    assert app.post("/api/desktop/switch", headers={"X-Token": "cto-token"}).json()["role"] == "ceo"


def test_the_backend_announces_itself_and_quits_with_the_app(tmp_path):
    env = {**os.environ, "ORGFORGE_PROVIDER": "mock", "ORGFORGE_EXIT_WITH_PARENT": "1", "ORGFORGE_HOME": str(tmp_path)}
    proc = subprocess.Popen([sys.executable, "-m", "orgforge.desktop"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env, text=True)
    try:
        ready = json.loads(proc.stdout.readline())
        assert ready["url"].startswith(f"http://127.0.0.1:{ready['port']}/#token=") and ready["home"] == str(tmp_path)
        token = ready["url"].split("#token=")[1]
        req = urllib.request.Request(f"http://127.0.0.1:{ready['port']}/api/state", headers={"X-Token": token})
        with urllib.request.urlopen(req, timeout=10) as resp:
            state = json.loads(resp.read())
        assert state["desktop"] is True and state["you"]["role"] == "ceo"
        assert (tmp_path / ".orgforge" / "company.db").exists()          # created on first launch
        proc.stdin.close()                                              # the app quit
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
