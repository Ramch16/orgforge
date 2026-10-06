import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from vittics_builder.cli import main
from vittics_builder.costs import price, spent, summary
from vittics_builder.feedback import FeedbackError
from vittics_builder.review import read_file, review
from vittics_builder.server import create_app
from vittics_builder.tools import ToolError
from test_tickets import built


def signed_off(co):
    p = built(co)
    co.pipeline.decide(co.pipeline.inbox("cto")[0]["id"], "cto", "approved")
    co.pipeline.advance(p["id"])
    co.pipeline.decide(co.pipeline.inbox("ceo")[0]["id"], "ceo", "approved")
    co.pipeline.advance(p["id"])
    return p


# ---- costs and budgets --------------------------------------------------------

def test_every_model_call_is_priced_and_attributed(co):
    p = built(co)
    assert price(co.s, "claude-sonnet-5-5") == (2.0, 10.0) and price(co.s, "claude-opus-5-5") == (4.0, 20.0)
    used = spent(co.db, p["id"])
    calls = co.db.one("SELECT COUNT(*) AS n, SUM(input_tokens) AS i, SUM(output_tokens) AS o FROM usage "
                      "WHERE project_id=?", p["id"])
    assert calls["n"] > 20 and used == pytest.approx((calls["i"] * 2 + calls["o"] * 10) / 1e6)
    s = summary(co.db)
    assert s["projects"][0]["spent"] == pytest.approx(used) and s["projects"][0]["budget"] == 25.0
    assert {d["department"] for d in s["departments"]} >= {"Engineering", "Quality and Testing", "Product"}
    assert not s["unpriced_models"]


def test_custom_prices_and_unpriced_models(co):
    co.s.prices = {"claude-sonnet-5-5": {"input": 3, "output": 15}}
    assert price(co.s, "claude-sonnet-5-5") == (3.0, 15.0)
    co.db.run("UPDATE agents SET model='some-other-model' WHERE name='Ram'")
    co.pipeline.advance(co.pipeline.create_project("Greeter", "A tiny library.")["id"])
    assert summary(co.db)["unpriced_models"] == ["some-other-model"]


def test_budget_pauses_work_and_asks_the_ceo(co):
    p = co.pipeline.create_project("Greeter", "A tiny library.", budget=0.001)
    assert co.pipeline.advance(p["id"])["stage"] == "prd_approval"     # the first step runs, then the gate
    co.pipeline.decide(co.pipeline.inbox("ceo")[0]["id"], "ceo", "approved")
    assert co.pipeline.advance(p["id"])["stage"] == "architecture"       # paused before designing
    [ask] = [a for a in co.pipeline.inbox("ceo") if a["kind"] == "budget"]
    co.pipeline.advance(p["id"])
    assert len([a for a in co.pipeline.inbox("ceo") if a["kind"] == "budget"]) == 1   # asked once
    co.pipeline.decide(ask["id"], "ceo", "approved", "Make it 40")
    assert co.pipeline.project(p["id"])["budget"] == 40.0
    assert co.pipeline.advance(p["id"])["stage"] == "architecture_approval"
    assert co.db.one("SELECT 1 FROM events WHERE kind='budget' AND message LIKE '%has used%'")


def test_stopping_at_the_budget_pauses_until_a_new_budget(co):
    p = co.pipeline.create_project("Greeter", "A tiny library.", budget=0.001)
    co.pipeline.advance(p["id"])
    co.pipeline.decide(co.pipeline.inbox("ceo")[0]["id"], "ceo", "approved")
    co.pipeline.advance(p["id"])
    [ask] = [a for a in co.pipeline.inbox("ceo") if a["kind"] == "budget"]
    with pytest.raises(PermissionError):
        co.pipeline.decide(ask["id"], "cto", "rejected")
    co.pipeline.decide(ask["id"], "ceo", "rejected")
    assert co.pipeline.project(p["id"])["stage"] == "paused"
    assert co.pipeline.advance(p["id"])["stage"] == "paused"
    assert co.pipeline.set_budget(p["id"], 0, "Niki")["stage"] == "architecture"   # no limit: resume
    assert co.pipeline.advance(p["id"])["stage"] == "architecture_approval"


# ---- status reports -----------------------------------------------------------

def test_reports_at_milestones_and_on_request(co):
    p = built(co)
    triggers = [r["trigger"] for r in co.reports.latest(p["id"])]
    assert triggers == ["Ready for release review", "Build started"]
    report = co.reports.latest(p["id"])[0]
    assert report["author"] == "Ram" and "Waiting on a decision" in report["body"] and "Release" in report["body"]
    co.reports.request(p["id"], "Niki")
    co.reports.request(p["id"], "Lucky")                          # not queued twice
    co.pipeline.write_reports(p["id"])
    assert [r["trigger"] for r in co.reports.latest(p["id"])][0] == "Requested"
    assert len(co.reports.latest(p["id"])) == 3


def test_report_falls_back_to_facts_without_a_product_manager(co):
    p = co.pipeline.create_project("Greeter", "A tiny library.")
    for a in co.org.staff(kind="product"):
        co.org.fire(a["id"], "x", by="CEO")
    co.reports.request(p["id"], "Niki")
    co.pipeline.write_reports(p["id"])
    r = co.reports.latest(p["id"])[0]
    assert r["author"] == "Vittics Builder" and r["body"].startswith("Project: Greeter")


# ---- product review -----------------------------------------------------------

def test_product_review_shows_what_was_built_and_checked(co):
    p = built(co)
    r = review(co, p["id"])
    assert r["verification"]["passed"] and [c["id"] for c in r["verification"]["checks"]] == ["unit-tests", "core-flow"]
    assert r["run"]["run"].startswith("PYTHONPATH=src")
    titles = [d["title"] for d in r["docs"]]
    assert titles[:3] == ["Requirements", "UX design", "Architecture"] and "audits: security auditor" in titles
    assert len(r["verdicts"]) == 4 and r["verdicts"][0]["verdict"].startswith("Approved")
    assert "src/core.py" in r["files"] and not r["downloadable"]
    assert read_file(co, p["id"], "src/core.py")["text"].startswith("def core")
    with pytest.raises(ToolError):
        read_file(co, p["id"], "../../etc/passwd")
    with pytest.raises(ToolError):
        read_file(co, p["id"], ".git/config")


# ---- customer feedback and versions -------------------------------------------

def test_customer_bug_reopens_the_product_for_version_two(co):
    p = signed_off(co)
    assert co.pipeline.project(p["id"])["version"] == 1
    co.feedback.submit(p["id"], "It crashes when the name is empty.", "Niki", source="support email", wait=True)
    [f] = co.feedback.list(p["id"])
    assert f["status"] == "triaged" and f["result"].startswith("Venky:") and len(f["tickets"]) == 1
    bug = co.tickets.get(f["tickets"][0])
    assert bug["type"] == "bug" and bug["status"] == "todo" and bug["reporter"] == "Venky"
    assert co.pipeline.project(p["id"])["stage"] == "build"
    assert co.pipeline.advance(p["id"])["stage"] == "release_approval"
    co.pipeline.decide(co.pipeline.inbox("cto")[0]["id"], "cto", "approved")
    co.pipeline.advance(p["id"])
    co.pipeline.decide(co.pipeline.inbox("ceo")[0]["id"], "ceo", "approved")
    assert co.pipeline.project(p["id"])["version"] == 2
    ws = co.pipeline.workspace(co.pipeline.project(p["id"]))
    assert ws.git("tag").split() == ["release", "v1", "v2"]


def test_feature_requests_wait_and_questions_get_replies(co):
    p = signed_off(co)
    co.feedback.submit(p["id"], "Please add a French greeting.", "Lucky", wait=True)
    request = co.tickets.get(co.feedback.list(p["id"])[0]["tickets"][0])
    assert request["type"] == "story" and request["status"] == "backlog"
    assert co.pipeline.project(p["id"])["stage"] == "done"             # a wish does not reopen the release
    co.feedback.submit(p["id"], "How do I install it?", "Lucky", wait=True)
    question = co.feedback.list(p["id"])[0]
    assert question["tickets"] == [] and "Suggested reply" in question["result"]
    with pytest.raises(FeedbackError, match="not been built"):
        co.feedback.submit(co.pipeline.create_project("Idea", "x", idea=True)["id"], "hi", "Niki")


# ---- dashboard and terminal ---------------------------------------------------

def test_operations_api(co):
    client = TestClient(create_app(co, {"ceo": "ceo-token", "cto": "cto-token"}))
    ceo, cto = {"X-Token": "ceo-token"}, {"X-Token": "cto-token"}
    p = signed_off(co)
    state = client.get("/api/state", headers=cto).json()
    assert state["costs"]["total"]["cost"] > 0 and state["costs"]["projects"][0]["id"] == p["id"]
    assert client.post(f"/api/projects/{p['id']}/budget", json={"amount": 60}, headers=cto).status_code == 403
    assert client.post(f"/api/projects/{p['id']}/budget", json={"amount": 60}, headers=ceo).json()["budget"] == 60
    assert client.get(f"/api/projects/{p['id']}/review", headers=cto).json()["downloadable"]
    assert "def core" in client.get(f"/api/projects/{p['id']}/file", params={"path": "src/core.py"}, headers=cto).json()["text"]
    assert client.get(f"/api/projects/{p['id']}/file", params={"path": "../x"}, headers=cto).status_code == 400
    zipped = client.get(f"/api/projects/{p['id']}/download", headers=ceo)
    assert zipped.status_code == 200 and "src/core.py" in zipfile.ZipFile(io.BytesIO(zipped.content)).namelist()
    assert client.post(f"/api/projects/{p['id']}/reports", headers=cto).status_code == 200
    assert client.post(f"/api/projects/{p['id']}/feedback", json={"body": "  "}, headers=ceo).status_code == 400
    assert client.post(f"/api/projects/{p['id']}/feedback", json={"body": "Thanks!", "source": "call"},
                       headers=ceo).json()[0]["source"] == "call"


def test_operations_cli(co, capsys):
    p = built(co)
    home = ["--home", str(co.s.root)]
    assert main([*home, "costs"]) == 0
    assert main([*home, "budget", str(p["id"]), "80"]) == 0
    assert main([*home, "report", str(p["id"]), "--as", "cto"]) == 0
    out = capsys.readouterr().out
    assert "Total so far (estimate): $" in out and "budget $80.00" in out and "Ram," in out
