import sys
import time
from datetime import datetime

import pytest

from vittics_builder.engines import BUILTIN_ENGINES, EngineUnavailable
from vittics_builder.failover import limit_key, reset_after
from test_engines import cli, on_cli  # noqa: F401  (fixture and helper)

LIMITED = ("import json, os, sys\nsys.stdin.read()\nopen(os.environ['LIMITED_LOG'], 'a').write('called\\n')\n"
           "print(json.dumps({'type': 'result', 'is_error': True, "
           "'result': \"You've hit your session limit - resets 10:10pm\"}))\n")


@pytest.fixture
def limited(cli, tmp_path, monkeypatch):
    co, say, calls = cli
    script, log = tmp_path / "limited_cli.py", tmp_path / "limited.log"
    script.write_text(LIMITED)
    monkeypatch.setenv("LIMITED_LOG", str(log))
    co.s.engines["limited"] = {**BUILTIN_ENGINES["claude-code"], "command": [sys.executable, str(script)]}
    co.s.raw["failover"] = {"enabled": True, "models": ["cli:fake/sonnet"], "cooldown_minutes": 30}

    def limited_calls():
        return len(log.read_text().splitlines()) if log.exists() else 0
    return co, say, calls, limited_calls


def test_reset_time_is_read_from_the_engine_message():
    nine = datetime(2026, 10, 6, 21, 0)
    assert reset_after("You've hit your session limit - resets 10:10pm", 60, nine) == 70 * 60
    assert reset_after("limit reached, resets at 9am", 60, nine) == 12 * 3600      # tomorrow morning
    assert reset_after("Rate limited. Try again in 5 minutes.", 60, nine) == 300
    assert reset_after("quota exceeded", 45, nine) == 45 * 60                       # no time given: the cooldown
    assert reset_after("try again in 1 second", 60, nine) == 60                     # never less than a minute


def test_a_cli_login_shares_one_limit_across_its_models():
    assert limit_key("cli:claude-code/haiku") == limit_key("cli:claude-code") == "cli:claude-code"
    assert limit_key("ollama:qwen2.5-coder") == "ollama:qwen2.5-coder"


def test_usage_limit_moves_the_work_to_the_next_engine(limited):
    co, say, calls, limited_calls = limited
    hari = on_cli(co, "Hari", "cli:limited")
    p = co.pipeline.create_project("Greeter", "A tiny library.")
    say("Built it.")
    res = co.runtime.run(hari, "Build the greeter.", co.pipeline.workspace(p), project_id=p["id"])
    assert res.completed and res.text == "Built it."
    assert limited_calls() == 1 and len(calls()) == 1
    assert "earlier attempt at this assignment on another engine stopped part-way" in calls()[0]["prompt"]
    [rest] = co.failover.status()
    assert rest["engine"] == "cli:limited" and "session limit" in rest["reason"]
    routes = co.db.all("SELECT model, status, reason FROM routing_decisions ORDER BY id")
    assert [(r["model"], r["status"]) for r in routes] == [("cli:limited", "unavailable"), ("cli:fake/sonnet", "completed")]
    assert routes[1]["reason"].startswith("Failover from cli:limited")
    assert co.db.one("SELECT 1 FROM events WHERE kind='failover' AND message LIKE 'Hari moved from cli:limited%'")
    assert co.org.agent("Hari")["model"] == "cli:limited"            # the seat keeps its model for later


def test_a_resting_engine_is_skipped_without_a_call(limited):
    co, say, calls, limited_calls = limited
    hari = on_cli(co, "Hari", "cli:limited")
    co.failover.rest("cli:limited", "session limit, resets in 2 hours")
    say("Done.")
    res = co.runtime.run(hari, "Build it.", None)
    assert res.completed and limited_calls() == 0 and len(calls()) == 1
    assert "is resting until" in co.db.one("SELECT reason FROM routing_decisions")["reason"]
    assert co.failover.clear("cli:limited/opus") == 1 and not co.failover.status()   # woken early
    co.runtime.run(hari, "Build it again.", None)
    assert limited_calls() == 1                                     # tried again once awake


def test_when_every_engine_is_resting_the_project_still_pauses(limited):
    co, say, calls, limited_calls = limited
    co.s.raw["failover"]["models"] = ["cli:limited"]               # no real backup
    for name in ("Hari", "Pavan", "Badri"):
        on_cli(co, name, "cli:limited")
    p = co.pipeline.create_project("Greeter", "A tiny library.")
    co.db.run("UPDATE projects SET stage='build' WHERE id=?", p["id"])
    co.tickets.create(p["id"], "Core engine", "Build it.", "Lucky", status="todo", role="backend_engineer")
    with pytest.raises(Exception, match="paused.*session limit.*no failover engine is ready"):
        co.pipeline.advance(p["id"])
    t = next(t for t in co.tickets.search(p["id"]) if t["title"] == "Core engine")
    assert t["status"] == "todo" and t["attempts"] == 0
    assert not calls() and limited_calls() == 1
    with pytest.raises(EngineUnavailable, match="resting until"):  # later runs don't knock on it again
        co.runtime.run(co.org.agent("Hari"), "Build it.", None)
    assert limited_calls() == 1


def test_an_unreachable_local_model_fails_over_too(limited):
    co, say, calls, _ = limited
    co.s.endpoints["deadlocal"] = {"base_url": "http://127.0.0.1:9/v1"}
    co.runtime.provider.endpoints["deadlocal"] = co.s.endpoints["deadlocal"]
    hari = on_cli(co, "Hari", "deadlocal:coder")
    say("Done on the backup.")
    res = co.runtime.run(hari, "Build it.", None)
    assert res.text == "Done on the backup."
    assert co.failover.status()[0]["engine"] == "deadlocal:coder"
    assert co.failover.status()[0]["until"] > time.time() + 29 * 60      # the configured cooldown


def test_failover_is_off_by_default(cli, tmp_path):
    co, say, calls = cli
    assert co.failover.enabled is False
    script = tmp_path / "limited_cli.py"
    script.write_text(LIMITED.replace("open(os.environ['LIMITED_LOG'], 'a').write('called\\n')\n", ""))
    co.s.engines["limited"] = {**BUILTIN_ENGINES["claude-code"], "command": [sys.executable, str(script)]}
    with pytest.raises(EngineUnavailable, match="session limit"):
        co.runtime.run(on_cli(co, "Hari", "cli:limited"), "Build it.", None)
    assert not co.failover.status() and not calls()


def test_a_project_keeps_building_when_the_builder_hits_its_limit(limited):
    co, say, calls, limited_calls = limited
    co.s.raw["failover"]["models"] = ["claude-sonnet-5-5"]          # back up onto the API (the offline mock here)
    on_cli(co, "Hari", "cli:limited")
    p = co.pipeline.create_project("Greeter", "A tiny library.")
    co.pipeline.advance(p["id"])
    for role in ("ceo", "cto"):
        [a] = [a for a in co.pipeline.inbox(role) if a["kind"] not in ("hr", "hire")]
        co.pipeline.decide(a["id"], role, "approved", "")
        stage = co.pipeline.advance(p["id"])["stage"]
    assert stage == "release_approval"
    assert limited_calls() == 1                                     # tried once, then rested
    assert not co.db.one("SELECT 1 FROM events WHERE kind IN ('paused', 'error')")
    assert co.db.one("SELECT 1 FROM routing_decisions WHERE model='claude-sonnet-5-5' AND reason LIKE 'Failover%'")


def test_codex_style_limit_messages():
    nine = datetime(2026, 10, 6, 21, 0)
    assert reset_after("You've hit your usage limit. Try again in 4 days 3 hours.", 60, nine) == 24 * 3600
    assert reset_after("Usage limit reached. Try again at 9:30 PM.", 60, nine) == 30 * 60


def test_a_limit_after_a_long_echoed_prompt_is_still_seen(limited, tmp_path):
    co, say, calls, _ = limited
    codex = tmp_path / "codex_like.py"                 # Codex echoes the whole prompt to stderr, error last
    codex.write_text("import sys\nprompt = sys.stdin.read()\nsys.stderr.write('user\\n' + prompt + '\\n')\n"
                     "sys.stderr.write(\"ERROR: You've hit your usage limit. Try again in 2 hours.\\n\")\nsys.exit(1)\n")
    co.s.engines["codexlike"] = {**BUILTIN_ENGINES["codex"], "command": [sys.executable, str(codex)],
                                 "level_args": {"read": [], "write": []}, "model_args": []}
    say("Done on the backup.")
    res = co.runtime.run(on_cli(co, "Hari", "cli:codexlike"), "Build it. " + "x" * 5000, None)
    assert res.text == "Done on the backup."
    rest = co.failover.status()[0]
    assert rest["engine"] == "cli:codexlike" and 7000 < rest["until"] - time.time() <= 7200
