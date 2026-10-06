import json
import os
import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

from vittics_builder import ai as ai_module
from vittics_builder.company import Company
from vittics_builder.server import create_app

FAKE_KEY = "sk-ant-test-0123456789abcdefghij"


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    """A company as a new desktop install creates it: the Anthropic API, and no key anywhere."""
    monkeypatch.delenv("VITTICS_PROVIDER", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    tools = {"claude-code": {"installed": True, "signed_in": True, "state": "ready", "version": "2.1.0", "detail": ""},
             "codex": {"installed": False, "signed_in": None, "state": "missing", "version": "", "detail": ""}}
    monkeypatch.setattr("vittics_builder.machine.check_tool", lambda tool: tools[tool])
    co = Company(tmp_path / "co", create=True)
    yield co, tools
    os.environ.pop("ANTHROPIC_API_KEY", None)


def test_a_new_install_says_the_agents_cannot_work_yet(fresh):
    co, _ = fresh
    st = co.ai.status(fresh=True)
    assert (st["choice"], st["ready"], st["chosen"]) == ("api", False, False)
    assert "No Anthropic API key" in st["reason"]


def test_choosing_claude_code_moves_everyone_and_new_hires(fresh):
    co, _ = fresh
    st = co.ai.use("claude-code", "Niki")
    assert st["ready"] and st["label"] == "Claude Code"
    assert {a["model"] for a in co.org.staff()} == {"cli:claude-code"}
    assert co.org.hire("backend_engineer", by="Lucky")["model"] == "cli:claude-code"
    before = (co.s.root / "org.yaml").read_text(encoding="utf-8")
    again = Company(co.s.root)                                         # the choice survives a restart
    assert again.ai.current() == "claude-code" and again.s.default_model == "cli:claude-code"
    assert (co.s.root / "org.yaml").read_text(encoding="utf-8") == before   # org.yaml is never rewritten


def test_an_uninstalled_engine_says_what_to_do(fresh):
    co, _ = fresh
    st = co.ai.use("codex", "Niki")
    assert not st["ready"] and "Codex is not installed" in st["reason"] and "codex login" in st["reason"]


def test_an_api_key_is_kept_private(fresh):
    co, _ = fresh
    with pytest.raises(ValueError, match="Enter an Anthropic API key first"):
        co.ai.use("api", "Niki")
    with pytest.raises(ValueError, match="does not look like"):
        co.ai.set_key("hello", "Niki")
    co.ai.set_key(FAKE_KEY, "Niki")
    path = co.s.root / ".vittics" / "credentials.json"
    assert os.name == "nt" or oct(path.stat().st_mode & 0o777) == "0o600"
    assert co.ai.use("api", "Niki")["ready"]
    assert FAKE_KEY not in json.dumps(co.db.all("SELECT * FROM events"))
    hari = co.org.agent("Hari")
    py = pathlib.Path(sys.executable).as_posix()
    out = co.pipeline.workspace(co.pipeline.create_project("P", "p")).run_command(
        f"{py} -c \"import os; print('key is', os.environ.get('ANTHROPIC_API_KEY'))\"")
    assert "key is None" in out                                        # agents' commands never get it
    os.environ.pop("ANTHROPIC_API_KEY")
    Company(co.s.root)                                                 # loaded again on the next start
    assert os.environ.get("ANTHROPIC_API_KEY") == FAKE_KEY and hari


def test_the_offline_demo_needs_nothing(fresh):
    co, _ = fresh
    st = co.ai.use("demo", "Niki")
    assert st["ready"] and co.s.provider == "mock"
    assert co.ai.test()["ok"]


def test_existing_companies_are_read_as_they_are(tmp_path, monkeypatch):
    monkeypatch.delenv("VITTICS_PROVIDER", raising=False)
    co = Company(tmp_path / "co", create=True)
    co.db.run("UPDATE agents SET model='cli:codex'")
    assert co.ai.current() == "codex" and not co.ai.saved()


def test_the_dashboard_never_returns_the_key(fresh):
    co, _ = fresh
    client = TestClient(create_app(co, {"ceo": "ceo-token", "cto": "cto-token"}))
    cto = {"X-Token": "cto-token"}
    assert client.get("/api/state", headers=cto).json()["ai"]["ready"] is False
    assert client.post("/api/ai/use", json={"choice": "api"}, headers=cto).status_code == 400
    r = client.post("/api/ai/key", json={"value": FAKE_KEY}, headers=cto)
    assert r.status_code == 200 and r.json()["has_key"] and FAKE_KEY not in r.text
    assert client.post("/api/ai/use", json={"choice": "api"}, headers=cto).json()["ready"]
    everything = client.get("/api/state", headers=cto).text + client.get("/api/ai", headers=cto).text
    assert FAKE_KEY not in everything
    assert client.post("/api/ai/use", json={"choice": "nope"}, headers=cto).status_code == 400
