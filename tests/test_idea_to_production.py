import json
import os
import sys
import urllib.request
from pathlib import Path

import pytest

from vittics_builder.llm import MockProvider
from vittics_builder.validation import verify_product

PY = Path(sys.executable).as_posix()
SERVE = f'{PY} -m http.server $PORT --bind $HOST'          # stands in for "npm start"


def decide_next(co, role, decision="approved", feedback="", choice=""):
    [a] = [a for a in co.pipeline.inbox(role) if a["kind"] not in ("hr", "hire")]
    co.pipeline.decide(a["id"], role, decision, feedback, choice=choice)
    return co.pipeline.advance(a["project_id"])


def gates(co, pid):
    return [(a["kind"], a["status"], a["decided_by"]) for a in co.db.all(
        "SELECT * FROM approvals WHERE project_id=? AND kind NOT IN ('hr','hire') ORDER BY id", pid)]


# ---- autopilot -------------------------------------------------------------------
def test_step_by_step_is_still_the_default(co):
    p = co.pipeline.create_project("Notes", "A small notes app.", idea=True)
    assert co.pipeline.autopilot.level(p["id"]) == "off"
    assert co.pipeline.advance(p["id"])["stage"] == "idea_review"


def test_final_signoff_only_runs_from_idea_to_signoff(co):
    p = co.pipeline.create_project("Notes", "A small notes app.", idea=True)
    co.pipeline.autopilot.set(p["id"], "final", "Niki")
    assert co.pipeline.advance(p["id"])["stage"] == "signoff"           # one call, all the way through
    decided = gates(co, p["id"])
    assert [k for k, s, _ in decided if s == "approved"] == ["feasibility", "idea_decision", "plan", "plan", "prd",
                                                             "architecture", "release"]
    assert all(by.startswith("Autopilot for ") for _, s, by in decided if s == "approved")
    assert co.pipeline.project(p["id"])["purpose"] == "internal"         # as the assessment recommended
    assert decided[-1][:2] == ("signoff", "pending")                     # the release is always yours
    assert not co.db.one("SELECT 1 FROM reviews WHERE source='human'")    # autopilot never scores agents
    assert decide_next(co, "ceo")["stage"] == "done"


def test_key_decisions_leaves_the_idea_and_plan_to_you(co):
    p = co.pipeline.create_project("Notes", "A small notes app.", idea=True)
    co.pipeline.autopilot.set(p["id"], "key", "Niki")
    assert co.pipeline.advance(p["id"])["stage"] == "idea_decision"      # technical sign-off was automatic
    assert decide_next(co, "ceo", choice="commercial")["stage"] == "plan_approval"
    decide_next(co, "ceo")
    assert decide_next(co, "cto")["stage"] == "signoff"                  # requirements, design, release: automatic


def test_failures_always_come_to_a_person(make_company):
    co = make_company(bad_agents={"Hari", "Sandy"})
    p = co.pipeline.create_project("Greeter", "A tiny library.")
    co.pipeline.autopilot.set(p["id"], "final", "Niki")
    assert co.pipeline.advance(p["id"])["stage"] == "escalation"
    assert any(k == "escalation" and s == "pending" for k, s, _ in gates(co, p["id"]))


def test_a_recommendation_not_to_build_is_left_to_the_ceo(co, monkeypatch):
    original = MockProvider._script

    def park(self, meta, step=0):
        if meta.get("purpose") == "assessment":
            return [[self._call("submit_assessment", recommendation="park", feasibility="achievable",
                                summary="Interesting, but not now.")]]
        return original(self, meta, step)
    monkeypatch.setattr(MockProvider, "_script", park)
    p = co.pipeline.create_project("Notes", "A small notes app.", idea=True)
    co.pipeline.autopilot.set(p["id"], "final", "Niki")
    assert co.pipeline.advance(p["id"])["stage"] == "idea_decision"
    assert co.db.one("SELECT 1 FROM events WHERE kind='autopilot' AND message LIKE 'Left for you:%not recommend%'")


def test_unknown_levels_are_refused(co):
    p = co.pipeline.create_project("Notes", "A small notes app.")
    with pytest.raises(ValueError):
        co.pipeline.autopilot.set(p["id"], "yolo", "Niki")


# ---- keys --------------------------------------------------------------------------
def test_keys_are_private_and_never_shown(co):
    p = co.pipeline.create_project("Shop", "A web shop.")
    co.vault.set(p["id"], "STRIPE_SECRET_KEY", "sk_test_abc123", "Lucky")
    path = co.s.root / ".vittics" / "vault" / f"{p['id']}.json"
    assert os.name == "nt" or oct(path.stat().st_mode & 0o777) == "0o600"
    assert co.vault.names(p["id"]) == ["STRIPE_SECRET_KEY"]
    assert "sk_test_abc123" not in json.dumps(co.db.all("SELECT * FROM events"))
    assert co.vault.redact(p["id"], "key=sk_test_abc123") == "key=[STRIPE_SECRET_KEY]"
    for bad in ["lower", "PATH", "PORT", "A"]:
        with pytest.raises(ValueError):
            co.vault.set(p["id"], bad, "x", "Lucky")


def test_checks_get_the_keys_but_saved_output_does_not(co):
    p = co.pipeline.create_project("Shop", "A web shop.")
    co.vault.set(p["id"], "STRIPE_SECRET_KEY", "sk_test_abc123", "Lucky")
    ws = co.pipeline.workspace(p)
    ws.write_file("README.md", "# Shop\n")
    ws.write_file("docs/OPERATIONS.md", "Needs STRIPE_SECRET_KEY.\n")
    check = f"{PY} -c \"import os; print('using', os.environ['STRIPE_SECRET_KEY'])\""
    ws.write_file("product.json", json.dumps({"name": "Shop", "setup": "-", "run": "-",
                                              "checks": [{"id": "pay", "requirement": "r", "command": check}]}))
    report = verify_product(ws, co.vault.env(p["id"]), lambda t: co.vault.redact(p["id"], t))
    assert report["passed"]
    saved = ws.read_file("docs/VERIFICATION.json")
    assert "[STRIPE_SECRET_KEY]" in saved and "sk_test_abc123" not in saved
    assert "sk_test_abc123" not in ws.run_command(f"{PY} -c \"import os; print(os.environ.get('STRIPE_SECRET_KEY'))\"")


def test_agents_see_key_names_never_values(co, monkeypatch):
    p = co.pipeline.create_project("Shop", "A web shop.")
    co.vault.set(p["id"], "STRIPE_SECRET_KEY", "sk_test_abc123", "Lucky")
    seen = []
    original = co.runtime.provider.complete
    monkeypatch.setattr(co.runtime.provider, "complete", lambda **kw: (seen.append(kw["system"]), original(**kw))[1])
    co.runtime.run(co.org.agent("Hari"), "Build the payment page.", co.pipeline.workspace(p), project_id=p["id"])
    assert "STRIPE_SECRET_KEY" in seen[0] and "sk_test_abc123" not in "".join(seen)


# ---- previews ----------------------------------------------------------------------
def released(co, serve, version=1):
    p = co.pipeline.create_project("Site", "A tiny website.")
    ws = co.pipeline.workspace(p)
    ws.write_file("index.html", f"<h1>Site v{version}</h1>")
    ws.write_file("product.json", json.dumps({"name": "Site", "setup": "-", "run": "-", "serve": serve, "checks": []}))
    ws.commit(f"v{version}")
    ws.git("tag", "-f", f"v{version}")
    co.db.run("UPDATE projects SET version=?, stage='deploying' WHERE id=?", version, p["id"])
    return co.pipeline.project(p["id"])


@pytest.fixture
def previews(co):
    yield co
    co.previews.stop_all()


def test_a_signed_off_product_runs_as_a_preview(previews):
    co = previews
    p = released(co, {"command": SERVE, "health": "/index.html"})
    assert co.production.configured(p)
    co.production.run(p)
    assert co.pipeline.project(p["id"])["stage"] == "live"
    preview = co.previews.current(p["id"])
    assert preview["status"] == "running" and preview["version"] == 1
    page = urllib.request.urlopen(preview["url"] + "index.html", timeout=5).read().decode()
    assert page == "<h1>Site v1</h1>"
    assert co.production.list(p["id"])[0]["environment"] == "preview"


def test_a_broken_new_version_leaves_the_old_preview_running(previews):
    co = previews
    p = released(co, {"command": SERVE, "health": "/index.html"})
    co.production.run(p)
    first = co.previews.current(p["id"])
    ws = co.pipeline.workspace(p)
    ws.write_file("product.json", json.dumps({"name": "Site", "setup": "-", "run": "-", "checks": [],
                                              "serve": {"command": f"{PY} -c \"raise SystemExit(3)\""}}))
    ws.commit("v2")
    ws.git("tag", "-f", "v2")
    co.db.run("UPDATE projects SET version=2, stage='deploying' WHERE id=?", p["id"])
    co.production.run(co.pipeline.project(p["id"]))
    assert co.pipeline.project(p["id"])["stage"] == "deploy_failed"
    [a] = [a for a in co.pipeline.inbox("cto") if a["kind"] == "deploy_failed"]
    assert "exited with 3" in a["summary"] and "previous preview (v1) keeps running" in a["summary"]
    assert co.previews.current(p["id"])["pid"] == first["pid"]
    assert urllib.request.urlopen(first["url"] + "index.html", timeout=5).status == 200


def test_previews_get_the_project_keys_and_stop_cleanly(previews):
    co = previews
    p = released(co, {"command": f"{PY} -c \"import os, http.server, socketserver; "
                                 f"open('key.txt','w').write(os.environ['DB_URL']); "
                                 f"socketserver.TCPServer(('127.0.0.1', int(os.environ['PORT'])), "
                                 f"http.server.SimpleHTTPRequestHandler).serve_forever()\"", "health": "/key.txt"})
    co.vault.set(p["id"], "DB_URL", "postgres://local/site", "Lucky")
    co.production.run(co.pipeline.project(p["id"]))
    preview = co.previews.current(p["id"])
    assert urllib.request.urlopen(preview["url"] + "key.txt", timeout=5).read() == b"postgres://local/site"
    assert co.previews.stop(p["id"], "Lucky") and co.previews.current(p["id"])["status"] == "stopped"


def test_products_without_serve_stop_at_ready_as_before(co):
    p = co.pipeline.create_project("Lib", "A library.")
    assert not co.production.configured(co.pipeline.project(p["id"]))


# ---- dashboard and names -------------------------------------------------------------
def test_names_come_from_the_brief():
    from vittics_builder.autopilot import name_from
    assert name_from("Build me a customer support SaaS with authentication and Stripe") == "Customer Support SaaS"
    assert name_from("A todo list app for teams") == "Todo List App"
    assert name_from("I want an iOS habit tracker that syncs") == "iOS Habit Tracker"


def test_one_sentence_from_the_dashboard(co):
    import time
    from fastapi.testclient import TestClient
    from vittics_builder.server import create_app
    client = TestClient(create_app(co, {"ceo": "ceo-token", "cto": "cto-token"}))
    ceo = {"X-Token": "ceo-token"}
    assert client.post("/api/build", json={"brief": "short"}, headers=ceo).status_code == 400
    assert client.post("/api/build", json={"brief": "A todo list app for teams", "autopilot": "yolo"},
                       headers=ceo).status_code == 400
    p = client.post("/api/build", json={"brief": "A todo list app for teams", "autopilot": "final"}, headers=ceo).json()
    assert p["name"] == "Todo List App" and p["autopilot"] == "final"
    for _ in range(100):                                                   # it works in the background
        if co.pipeline.project(p["id"])["stage"] == "signoff":
            break
        time.sleep(0.1)
    assert co.pipeline.project(p["id"])["stage"] == "signoff"
    state = client.get("/api/state", headers=ceo).json()
    assert state["autopilot_levels"]["key"] == "Key decisions"
    assert client.post(f"/api/projects/{p['id']}/autopilot", json={"level": "off"}, headers=ceo).json()["autopilot"] == "off"


def test_key_values_never_come_back_from_the_dashboard(co):
    from fastapi.testclient import TestClient
    from vittics_builder.server import create_app
    client = TestClient(create_app(co, {"ceo": "ceo-token", "cto": "cto-token"}))
    cto = {"X-Token": "cto-token"}
    p = co.pipeline.create_project("Shop", "A web shop.")
    assert client.post(f"/api/projects/{p['id']}/keys", json={"name": "STRIPE_SECRET_KEY", "value": "sk_test_abc123"},
                       headers=cto).json() == ["STRIPE_SECRET_KEY"]
    assert client.post(f"/api/projects/{p['id']}/keys", json={"name": "PATH", "value": "x"}, headers=cto).status_code == 400
    everything = client.get("/api/state", headers=cto).text + client.get(f"/api/projects/{p['id']}/keys", headers=cto).text
    assert "STRIPE_SECRET_KEY" in everything and "sk_test_abc123" not in everything
    assert client.post(f"/api/projects/{p['id']}/keys/STRIPE_SECRET_KEY/delete", headers=cto).json() == []
    assert client.post(f"/api/projects/{p['id']}/preview", json={"action": "start"}, headers=cto).status_code == 400
