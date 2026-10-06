import pytest
from fastapi.testclient import TestClient

from vittics_builder.cli import main
from vittics_builder.pipeline import PipelineError
from vittics_builder.server import create_app
from test_server import wait_for


def submit(co, name="Leave Tracker"):
    p = co.pipeline.create_project(name, "Track staff leave requests and approvals.", by=co.s.ceo_name, idea=True)
    co.pipeline.advance(p["id"])
    return p


def only(co, kind, role=None):
    [a] = [a for a in co.pipeline.inbox(role) if a["kind"] == kind]
    return a


def decided(co, choice, name="Leave Tracker"):
    p = submit(co, name)
    co.pipeline.decide(only(co, "feasibility")["id"], "cto", "approved")
    co.pipeline.decide(only(co, "idea_decision")["id"], "ceo",
                       "approved" if choice in ("internal", "commercial") else "rejected", choice=choice)
    co.pipeline.advance(p["id"])
    return p


def test_ram_assesses_with_engineering_marketing_and_legal(co):
    p = submit(co)
    assert co.pipeline.project(p["id"])["stage"] == "idea_review"
    stage = {t["key"]: t for t in co.tickets.search(p["id"])}
    assert stage["stage-assessment"]["assignee"] == "Ram" and stage["stage-assessment"]["status"] == "in_review"
    consulted = {stage[k]["department"]: stage[k]["assignee"] for k in stage if k.startswith("stage-consult-")}
    assert consulted == {"Engineering": "Sony", "Marketing": "Anshu", "Legal and Compliance": "Srujana"}
    ws = co.pipeline.workspace(co.pipeline.project(p["id"]))
    for f in ("docs/ASSESSMENT.md", "docs/assessment/FEASIBILITY.md", "docs/assessment/MARKET.md",
              "docs/assessment/LEGAL.md"):
        assert ws.resolve(f).read_text()
    review = only(co, "feasibility")
    assert review["required_role"] == "cto" and "Recommendation: build it for internal use" in review["summary"]


def test_lucky_signs_off_before_niki_decides(co):
    p = submit(co)
    review = only(co, "feasibility")
    with pytest.raises(PermissionError):
        co.pipeline.decide(review["id"], "ceo", "approved")
    co.pipeline.decide(review["id"], "cto", "rejected", "Check the payroll integration")
    assert co.pipeline.project(p["id"])["feedback"] == "Check the payroll integration"
    co.pipeline.advance(p["id"])                                   # Ram reassesses with the feedback
    co.pipeline.decide(only(co, "feasibility")["id"], "cto", "approved", "Fine now")
    decision = only(co, "idea_decision")
    assert decision["required_role"] == "ceo" and "Fine now" in decision["summary"]
    with pytest.raises(PipelineError, match="internal"):
        co.pipeline.decide(decision["id"], "ceo", "approved")       # a decision needs one of the four choices
    with pytest.raises(PipelineError):
        co.pipeline.decide(decision["id"], "ceo", "approved", choice="park")


def test_build_to_sell_plans_every_department_and_needs_both_signoffs(co):
    p = decided(co, "commercial")
    project = co.pipeline.project(p["id"])
    assert project["stage"] == "plan_approval" and project["purpose"] == "commercial"
    business, technical = only(co, "plan", "ceo"), only(co, "plan", "cto")
    assert "Marketing (Product Marketer): Pricing and launch plan" in business["summary"]
    co.pipeline.decide(business["id"], "ceo", "approved")
    assert co.pipeline.project(p["id"])["stage"] == "plan_approval"   # still waiting for Lucky
    co.pipeline.decide(technical["id"], "cto", "approved")
    assert co.pipeline.project(p["id"])["stage"] == "prd"
    dept = {t["department"]: t for t in co.tickets.search(p["id"]) if t["origin"] == "dept"}
    assert set(dept) == {"Customer Support", "Legal and Compliance", "Marketing"}
    assert dept["Customer Support"]["depends_on"] == '["@build"]'

    co.pipeline.advance(p["id"])
    co.pipeline.decide(only(co, "prd")["id"], "ceo", "approved")
    co.pipeline.advance(p["id"])
    co.pipeline.decide(only(co, "architecture")["id"], "cto", "approved")
    assert co.pipeline.advance(p["id"])["stage"] == "release_approval"
    assert all(t["status"] == "done" for t in dept.values() for t in [co.tickets.get(t["id"])])
    # the help guide waited for the build: it started after every build ticket had passed
    help_start = co.db.one("SELECT MIN(id) AS i FROM ticket_comments WHERE task_id=? AND body LIKE 'Started%'",
                           dept["Customer Support"]["id"])["i"]
    build_done = co.db.one("SELECT MAX(c.id) AS i FROM ticket_comments c JOIN tasks t ON t.id=c.task_id "
                           "WHERE t.origin='plan' AND c.body LIKE 'Passed review%'")["i"]
    assert help_start > build_done


def test_internal_use_plans_no_marketing(co):
    p = decided(co, "internal")
    assert "Marketing" not in only(co, "plan", "ceo")["summary"]
    assert co.pipeline.project(p["id"])["purpose"] == "internal"


def test_plan_sent_back_by_either_withdraws_the_other(co):
    p = decided(co, "internal")
    business, technical = only(co, "plan", "ceo"), only(co, "plan", "cto")
    co.pipeline.decide(technical["id"], "cto", "rejected", "Split the release in two")
    assert co.db.one("SELECT status FROM approvals WHERE id=?", business["id"])["status"] == "withdrawn"
    assert co.pipeline.project(p["id"])["stage"] == "plan"
    co.pipeline.advance(p["id"])
    assert len([a for a in co.pipeline.inbox() if a["kind"] == "plan"]) == 2


def test_park_revisit_and_drop(co):
    p = decided(co, "park")
    assert co.pipeline.project(p["id"])["stage"] == "parked"
    assert co.pipeline.revisit(p["id"], co.s.ceo_name)["stage"] == "idea_decision"
    co.pipeline.decide(only(co, "idea_decision")["id"], "ceo", "rejected", "Not now", choice="drop")
    assert co.pipeline.project(p["id"])["stage"] == "dropped"
    assessment = next(t for t in co.tickets.search(p["id"]) if t["key"] == "stage-assessment")
    assert assessment["status"] == "cancelled"
    with pytest.raises(PipelineError, match="not parked"):
        co.pipeline.revisit(p["id"], co.s.ceo_name)


def test_direct_projects_skip_the_idea_stage(co):
    p = co.pipeline.create_project("Greeter", "A tiny library.")
    assert co.pipeline.advance(p["id"])["stage"] == "prd_approval"
    assert co.pipeline.project(p["id"])["purpose"] == ""


def test_idea_api_and_cli(co, capsys):
    client = TestClient(create_app(co, {"ceo": "ceo-token", "cto": "cto-token"}))
    ceo, cto = {"X-Token": "ceo-token"}, {"X-Token": "cto-token"}
    p = client.post("/api/projects", json={"name": "Wiki", "brief": "An internal wiki.", "idea": True}, headers=ceo).json()
    assert p["stage"] == "idea"
    wait_for(co, "idea_review", p["id"])                      # the assessment runs in the background
    review = only(co, "feasibility")
    assert client.post(f"/api/approvals/{review['id']}", json={"decision": "approved"}, headers=cto).status_code == 200
    wait_for(co, "idea_decision", p["id"])
    decision = only(co, "idea_decision")
    assert client.post(f"/api/approvals/{decision['id']}", json={"decision": "approved"}, headers=ceo).status_code == 400
    assert client.post(f"/api/approvals/{decision['id']}", json={"decision": "rejected", "choice": "park"},
                       headers=ceo).status_code == 200
    assert client.post(f"/api/projects/{p['id']}/revisit", headers=cto).status_code == 403
    assert client.post(f"/api/projects/{p['id']}/revisit", headers=ceo).json()["stage"] == "idea_decision"
    assert client.get("/api/state", headers=ceo).json()["idea_stages"][0] == "idea"

    decision = only(co, "idea_decision")
    home = ["--home", str(co.s.root)]
    assert main([*home, "idea", "decide", str(decision["id"]), "internal", "--no-run"]) == 0
    assert co.pipeline.project(p["id"])["stage"] in ("plan", "plan_approval")
    assert main([*home, "idea", "new", "Chatbot", "--brief", "Answer staff questions.", "--as", "cto"]) == 0
    out = capsys.readouterr().out
    assert "Idea" in out and "submitted" in out
    chatbot = co.db.one("SELECT * FROM projects WHERE name='Chatbot'")
    assert chatbot["author"] == co.s.cto_name and chatbot["stage"] == "idea_review"
