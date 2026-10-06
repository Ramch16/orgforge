import json
import sys

import pytest

PY = sys.executable


def decide_next(co, role, decision="approved", feedback=""):
    [approval] = [a for a in co.pipeline.inbox(role) if a["kind"] not in ("hr", "hire")]
    co.pipeline.decide(approval["id"], role, decision, feedback)
    return co.pipeline.advance(approval["project_id"])


def to_signoff(co, name="Greeter"):
    p = co.pipeline.create_project(name, "A tiny library.")
    co.pipeline.advance(p["id"])
    decide_next(co, "ceo")
    decide_next(co, "cto")
    assert decide_next(co, "cto")["stage"] == "signoff"
    return p


@pytest.fixture
def prod(co, tmp_path):
    """A deploy script that records what it was asked, and a health check you can switch off."""
    log, healthy = tmp_path / "deploys.jsonl", tmp_path / "healthy"
    healthy.write_text("yes")
    script = tmp_path / "deploy.py"
    script.write_text(
        "import json, os, sys\n"
        f"open({str(log)!r}, 'a').write(json.dumps({{'args': sys.argv[1:], 'cwd': os.getcwd(), "
        "'token': os.environ.get('DEPLOY_TOKEN'), 'other': os.environ.get('OTHER_SECRET')}) + '\\n')\n")
    check = f"{PY} -c \"import sys; sys.exit(0 if open({str(healthy)!r}).read() == 'yes' else 1)\""

    def env(name, **extra):
        return {"name": name, "deploy": f"{PY} {script} deploy {name} {{version}} {{commit}}",
                "health": check, "rollback": f"{PY} {script} rollback {name} {{previous}}",
                "health_retries": 2, "health_interval_seconds": 0, **extra}

    def configure(*envs, **extra):
        co.s.raw["production"] = {"projects": {"Greeter": {"env": ["DEPLOY_TOKEN"], "environments": list(envs), **extra}}}

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return configure, env, calls, healthy


def test_without_production_config_signoff_stops_at_ready(co):
    p = to_signoff(co)
    assert decide_next(co, "ceo")["stage"] == "done"
    assert not co.production.list(p["id"])


def test_signed_off_release_deploys_through_each_environment(co, prod, monkeypatch):
    configure, env, calls, _ = prod
    monkeypatch.setenv("DEPLOY_TOKEN", "tok-123")
    monkeypatch.setenv("OTHER_SECRET", "must-not-leak")
    configure(env("staging"), env("production", approval="ceo"))
    p = to_signoff(co)
    assert decide_next(co, "ceo")["stage"] == "deploy_approval"          # staging went out; production waits
    [staging] = co.production.list(p["id"])
    assert (staging["environment"], staging["version"], staging["status"]) == ("staging", 1, "live")
    [call] = calls()
    head = co.pipeline.workspace(co.pipeline.project(p["id"])).git("rev-parse", "HEAD")
    assert call["args"] == ["deploy", "staging", "1", head] and call["cwd"].endswith("001-greeter")
    assert call["token"] == "tok-123" and call["other"] is None          # only the listed secret is passed
    assert decide_next(co, "ceo")["stage"] == "live"
    assert [d["environment"] for d in co.production.list(p["id"]) if d["status"] == "live"] == ["production", "staging"]
    assert co.db.one("SELECT 1 FROM events WHERE kind='deploy' AND message LIKE 'Version 1 is live%'")


def test_holding_back_a_deployment_needs_no_reason(co, prod):
    configure, env, calls, _ = prod
    configure(env("production", approval="cto"))
    to_signoff(co)
    assert decide_next(co, "ceo")["stage"] == "deploy_approval"
    summary = co.pipeline.inbox("cto")[0]["summary"]
    assert "deploy production 1 " in summary and "{version}" not in summary    # the actual command, filled in
    assert decide_next(co, "cto", "rejected")["stage"] == "done"
    assert not calls()


def test_unhealthy_release_rolls_back_and_goes_to_the_cto(co, prod):
    configure, env, calls, healthy = prod
    configure(env("production"))
    p = to_signoff(co)
    assert decide_next(co, "ceo")["stage"] == "live"                     # v1 is live
    co.tickets.create(p["id"], "Add a farewell", "Say goodbye too.", "Niki", status="todo", role="backend_engineer")
    assert co.pipeline.advance(p["id"])["stage"] == "release_approval"   # reopened, built and verified again
    assert decide_next(co, "cto")["stage"] == "signoff"
    healthy.write_text("no")
    assert decide_next(co, "ceo")["stage"] == "deploy_failed"
    assert [c["args"][:3] for c in calls()][-2:] == [["deploy", "production", "2"], ["rollback", "production", "1"]]
    latest, first = co.production.list(p["id"])[:2]
    assert latest["status"] == "rollback_failed" and first["status"] == "live"   # still unhealthy after rollback
    incident = co.db.one("SELECT * FROM tasks WHERE project_id=? AND origin='observation'", p["id"])
    assert incident["priority"] == "urgent" and "v2" in incident["title"]
    [a] = co.pipeline.inbox("cto")
    assert a["kind"] == "deploy_failed" and "did NOT come back healthy" in a["summary"]

    healthy.write_text("yes")                                            # the CTO fixes the outage and retries
    assert decide_next(co, "cto")["stage"] == "live"
    assert co.production.list(p["id"])[0]["version"] == 2


def test_a_healthy_rollback_is_reported_and_rejecting_sends_it_back(co, prod, tmp_path):
    configure, env, calls, healthy = prod
    flaky = tmp_path / "flaky.py"                                        # unhealthy only for version 2
    flaky.write_text("import sys\nsys.exit(1 if sys.argv[1] == '2' else 0)\n")
    configure(env("production", health=f"{PY} {flaky} {{version}}"))
    p = to_signoff(co)
    assert decide_next(co, "ceo")["stage"] == "live"
    co.tickets.create(p["id"], "Add a farewell", "Say goodbye too.", "Niki", status="todo", role="backend_engineer")
    co.pipeline.advance(p["id"])
    decide_next(co, "cto")
    assert decide_next(co, "ceo")["stage"] == "deploy_failed"
    assert co.production.list(p["id"])[0]["status"] == "rolled_back"
    [a] = co.pipeline.inbox("cto")
    assert "Rolled back to v1, which is healthy" in a["summary"]
    with pytest.raises(Exception, match="Say what needs to change"):
        co.pipeline.decide(a["id"], "cto", "rejected")
    co.pipeline.decide(a["id"], "cto", "rejected", "The farewell crashes on startup; fix it.")
    fix = co.db.one("SELECT * FROM tasks WHERE project_id=? AND title='Fix the failed deployment'", p["id"])
    assert fix and "crashes on startup" in fix["description"]
    assert co.pipeline.project(p["id"])["stage"] == "build"


def test_first_deployment_failure_has_nothing_to_roll_back(co, prod):
    configure, env, calls, healthy = prod
    configure(env("production"))
    healthy.write_text("no")
    p = to_signoff(co)
    assert decide_next(co, "ceo")["stage"] == "deploy_failed"
    assert not any(c["args"][0] == "rollback" for c in calls())
    assert co.production.list(p["id"])[0]["status"] == "failed"
    assert "nothing to roll back to" in co.pipeline.inbox("cto")[0]["summary"]


def test_a_broken_config_does_not_break_signoff(co):
    co.s.raw["production"] = {"projects": {"Greeter": {"environments": [{"name": "production"}]}}}
    p = to_signoff(co)
    assert decide_next(co, "ceo")["stage"] == "deploy_failed"
    assert "needs a deploy command" in co.pipeline.inbox("cto")[0]["summary"]
    assert not co.production.list(p["id"])


def test_nothing_deploys_if_the_release_changed_after_signoff(co, prod):
    configure, env, calls, _ = prod
    configure(env("production", approval="cto"))
    p = to_signoff(co)
    decide_next(co, "ceo")
    co.pipeline.workspace(co.pipeline.project(p["id"])).write_file("sneaky.txt", "changed after sign-off")
    assert decide_next(co, "cto")["stage"] == "deploy_failed"
    assert not calls() and "no longer matches" in co.pipeline.inbox("cto")[0]["summary"]


def test_live_products_are_monitored_and_outages_become_incidents(co, prod):
    configure, env, calls, healthy = prod
    configure(env("production", monitor_minutes=5))
    p = to_signoff(co)
    decide_next(co, "ceo")
    [job] = co.workers.list()
    assert (job["name"], job["kind"], job["interval_seconds"]) == (f"health:{p['id']}:production", "health", 300)
    assert co.production.check(p["id"], "production")["healthy"]
    healthy.write_text("no")
    assert not co.production.check(p["id"], "production")["healthy"]
    assert not co.production.check(p["id"], "production")["healthy"]    # the same outage, not a second ticket
    [incident] = co.db.all("SELECT * FROM tasks WHERE project_id=? AND origin='observation'", p["id"])
    assert incident["priority"] == "urgent" and "unhealthy in production" in incident["title"]


def test_new_work_reopens_a_live_product(co, prod):
    configure, env, calls, _ = prod
    configure(env("production"))
    p = to_signoff(co)
    decide_next(co, "ceo")
    hari = co.org.agent("Hari")
    co.tickets.agent_tool(hari, "create_ticket", {"title": "Crash on empty name", "description": "From a customer.",
                                                  "type": "bug", "role": "backend_engineer"}, p["id"], None,
                          type("R", (), {"filed": []})(), requested_by="Niki")
    assert co.pipeline.project(p["id"])["stage"] == "build"
