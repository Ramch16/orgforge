import time
import sqlite3

import pytest
from fastapi.testclient import TestClient

from orgforge.cli import main
from orgforge.db import DB
from orgforge.server import create_app
from orgforge.tickets import TicketError
from test_pipeline import decide_next
from test_server import wait_for


def built(co):
    """A project whose plan has been built and is waiting for the CTO's release approval."""
    p = co.pipeline.create_project("Product", "Build a library")
    co.pipeline.advance(p["id"])
    decide_next(co, "ceo")
    assert decide_next(co, "cto")["stage"] == "release_approval"
    return p


def test_plan_tasks_are_tickets_with_history(co):
    p = built(co)
    tickets = [t for t in co.tickets.search(p["id"]) if t["origin"] == "plan"]
    assert [t["key"] for t in tickets] == ["core", "extras"]
    assert all(t["status"] == "done" and t["ticket"] == f"T-{t['id']}" for t in tickets)
    history = [h["body"] for h in co.tickets.get(tickets[0]["ticket"])["history"]]
    assert history[0].startswith("Created from the build plan")
    assert any(h.startswith("Started work") for h in history) and any(h.startswith("Passed review") for h in history)
    assert tickets[0]["reporter"] == co.org.pick(kind="planner")["name"]


def test_backlog_ticket_waits_and_does_not_block_release(co):
    p = co.pipeline.create_project("Product", "Build a library")
    co.pipeline.advance(p["id"])
    t = co.tickets.create(p["id"], "Nice to have: dark mode", "", "Niki")
    decide_next(co, "ceo")
    assert decide_next(co, "cto")["stage"] == "release_approval"
    assert co.tickets.get(t["id"])["status"] == "backlog"


def test_todo_ticket_reopens_a_releasing_product_and_agent_reads_comments(co, monkeypatch):
    p = built(co)
    [release] = co.pipeline.inbox("cto")
    t = co.tickets.create(p["id"], "Fix the greeting typo", "It says Helo.", "Niki", type="bug", priority="high")
    co.tickets.comment(t["ticket"], "Niki", "Use British spelling.")
    asks = []
    original = co.runtime.run
    def run(agent, ask, *args, **kwargs):
        asks.append(ask)
        return original(agent, ask, *args, **kwargs)
    monkeypatch.setattr(co.runtime, "run", run)

    co.tickets.update(t["ticket"], "Niki", status="todo")
    assert co.pipeline.project(p["id"])["stage"] == "build"
    assert co.db.one("SELECT status FROM approvals WHERE id=?", release["id"])["status"] == "withdrawn"
    assert co.pipeline.advance(p["id"])["stage"] == "release_approval"
    assert co.tickets.get(t["id"])["status"] == "done"
    assert any("Use British spelling." in a and "Fix the greeting typo" in a for a in asks)


def test_urgent_ticket_is_worked_first(co):
    p = built(co)
    low = co.tickets.create(p["id"], "Low", "", "Niki", priority="low", status="todo")
    urgent = co.tickets.create(p["id"], "Urgent", "", "Niki", priority="urgent", status="todo")
    assert co.pipeline._next_task(p["id"])["id"] == urgent["id"]
    co.pipeline.advance(p["id"])
    started = [h["task_id"] for h in co.db.all("SELECT task_id FROM ticket_comments WHERE body LIKE 'Started%' "
                                                "AND task_id IN (?,?) ORDER BY id", low["id"], urgent["id"])]
    assert started == [urgent["id"], low["id"]]


def test_humans_triage_but_the_team_owns_working_statuses(co):
    p = co.pipeline.create_project("Product", "Build a library")
    co.pipeline.advance(p["id"])
    decide_next(co, "ceo")                                   # plan is waiting for the CTO: tickets are to do
    core, extras = [t for t in co.tickets.search(p["id"]) if t["origin"] == "plan"]
    with pytest.raises(TicketError, match="team owns|between backlog"):
        co.tickets.update(core["ticket"], "Niki", status="done")
    with pytest.raises(TicketError, match="depend on"):
        co.tickets.update(core["ticket"], "Niki", status="backlog")
    with pytest.raises(TicketError, match="Unknown priority"):
        co.tickets.update(core["ticket"], "Niki", priority="asap")
    co.tickets.update(extras["ticket"], "Niki", priority="urgent", title="Supporting module v2")
    assert co.tickets.get(extras["id"])["title"] == "Supporting module v2"
    co.tickets.update(core["ticket"], "Niki", status="cancelled")
    assert co.pipeline._next_task(p["id"])["key"] == "extras"   # a cancelled dependency no longer blocks
    with pytest.raises(TicketError, match="closed"):
        co.tickets.update(core["ticket"], "Niki", priority="low")


def test_redesign_keeps_human_tickets(co):
    p = co.pipeline.create_project("Product", "Build a library")
    co.pipeline.advance(p["id"])
    decide_next(co, "ceo")
    mine = co.tickets.create(p["id"], "Add CSV export", "", "Niki")
    decide_next(co, "cto", "rejected", "Simplify the design")
    assert co.tickets.get(mine["id"])["status"] == "backlog"
    assert len([t for t in co.tickets.search(p["id"]) if t["origin"] != "stage"]) == 3


def test_ticket_api(co):
    client = TestClient(create_app(co, {"ceo": "ceo-token", "cto": "cto-token"}))
    cto = {"X-Token": "cto-token"}
    pid = client.post("/api/projects", json={"name": "Greeter", "brief": "A tiny library."}, headers=cto).json()["id"]
    wait_for(co, "prd_approval")
    assert client.post("/api/tickets", json={"project_id": pid, "title": "x"}).status_code == 401
    t = client.post("/api/tickets", json={"project_id": pid, "title": "Write docs", "type": "story"}, headers=cto).json()
    assert t["ticket"] == f"T-{t['id']}" and t["reporter"] == co.s.cto_name and t["status"] == "backlog"
    assert client.post(f"/api/tickets/{t['ticket']}/comments", json={"body": "Cover install"}, headers=cto).status_code == 200
    assert client.post(f"/api/tickets/{t['id']}", json={"status": "in_progress"}, headers=cto).status_code == 400
    assert client.post(f"/api/tickets/{t['id']}", json={"priority": "high"}, headers=cto).json()["priority"] == "high"
    detail = client.get(f"/api/tickets/{t['id']}", headers=cto).json()
    mine = [h["kind"] for h in detail["history"] if h["author"] == co.s.cto_name]
    assert mine == ["change", "comment", "change"]
    for _ in range(100):                                         # the owner replies in the background
        replies = [h for h in co.tickets.get(t["id"])["history"] if h["author"] != co.s.cto_name]
        if len(replies) >= 2:
            break
        time.sleep(0.05)
    assert len(replies) == 2 and all("where things stand" in h["body"] for h in replies)
    state = client.get("/api/state", headers=cto).json()
    assert next(x for x in state["tickets"] if x["id"] == t["id"])["comments"] == 3 and "in_review" in state["ticket_meta"]["statuses"]
    assert client.get("/api/tickets/T-999", headers=cto).status_code == 400


def test_ticket_cli(co, capsys):
    p = co.pipeline.create_project("Product", "Build a library")
    home = ["--home", str(co.s.root)]
    assert main([*home, "ticket", "new", str(p["id"]), "Add a logo", "--type", "story", "--priority", "high"]) == 0
    assert main([*home, "ticket", "comment", "T-1", "Make it blue", "--as", "cto"]) == 0
    assert main([*home, "ticket", "list"]) == 0
    out = capsys.readouterr().out
    assert "Filed T-1." in out and "Make it blue" in out and "high" in out and "(3 comments)" in out
    assert "nobody will work on it until you move it to To do" in out      # the owner replied, twice
    assert main([*home, "ticket", "update", "T-1", "--status", "cancelled"]) == 0
    assert main([*home, "ticket", "update", "T-1", "--priority", "low"]) == 1


def test_old_task_tables_gain_ticket_columns(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, project_id INTEGER, key TEXT, title TEXT, "
                 "description TEXT, role TEXT, assignee_id INTEGER, status TEXT, depends_on TEXT, attempts INTEGER, "
                 "feedback TEXT, result TEXT, created_at TEXT, updated_at TEXT)")
    conn.execute("INSERT INTO tasks (id, key, title, status) VALUES (1, 'core', 'Core', 'done')")
    conn.commit(); conn.close()
    row = DB(path).one("SELECT * FROM tasks")
    assert (row["type"], row["priority"], row["origin"], row["reporter"]) == ("task", "medium", "plan", "OrgForge")


# ---- agents use the tracker ------------------------------------------------

from types import SimpleNamespace

from orgforge.company import Company
from orgforge.llm import MockProvider
from orgforge.tickets import AGENT_TOTAL_LIMIT, MAX_TRANSFERS


class Scripted(MockProvider):
    """The mock company, plus one-off scripted moves keyed by (agent kind, task key or purpose)."""

    def __init__(self, moves):
        super().__init__()
        self.moves, self.done = moves, set()

    def _script(self, meta, step=0):
        trigger = (meta.get("kind"), meta.get("task_key") or meta.get("purpose"))
        if trigger in self.moves and step == 0 and trigger not in self.done:
            self.done.add(trigger)
            return [[self._call(name, **args) for name, args in self.moves[trigger]]]
        return super()._script(meta, step)


def scripted_company(tmp_path, moves):
    return Company(tmp_path, provider=Scripted(moves), create=True)


def test_every_department_logs_its_stage_work_as_tickets(co):
    p = co.pipeline.create_project("Product", "Build a library")
    co.pipeline.advance(p["id"])
    stage = {t["key"]: t for t in co.tickets.search(p["id"]) if t["origin"] == "stage"}
    assert stage["stage-requirements"]["department"] == "Product"
    assert stage["stage-requirements"]["status"] == "in_review"
    design = co.tickets.get(stage["stage-design-ux_designer"]["id"])
    assert design["department"] == "Design" and design["history"][0]["kind"] == "handoff"
    assert "Handed over from Product" in design["history"][0]["body"]

    with pytest.raises(TicketError, match="stage work"):
        co.tickets.update(stage["stage-requirements"]["id"], "Niki", status="cancelled")
    decide_next(co, "ceo", "rejected", "Add an offline mode")     # sent back, and the team revises at once
    reqs = co.tickets.get(stage["stage-requirements"]["id"])
    bodies = [h["body"] for h in reqs["history"]]
    sent_back = next(i for i, b in enumerate(bodies) if b.startswith("Sent back by") and "offline" in b)
    assert "Started again." in bodies[sent_back:] and reqs["status"] == "in_review"
    decide_next(co, "ceo")
    assert co.tickets.get(stage["stage-requirements"]["id"])["status"] == "done"

    decide_next(co, "cto")
    stage = {t["key"]: t for t in co.tickets.search(p["id"]) if t["origin"] == "stage"}
    assert stage["stage-architecture"]["department"] == "Engineering" and stage["stage-architecture"]["status"] == "done"
    assert stage["stage-release-qa"]["department"] == "Quality and Testing"
    assert {k for k in stage if k.startswith("stage-audit-")} == {
        "stage-audit-security_auditor", "stage-audit-compliance_officer", "stage-audit-performance_engineer"}
    assert all(t["status"] == "done" for t in stage.values())


def test_build_ticket_records_review_handoffs_between_departments(co):
    p = built(co)
    core = next(t for t in co.tickets.search(p["id"]) if t["key"] == "core")
    history = co.tickets.get(core["id"])["history"]
    handoffs = [h["body"] for h in history if h["kind"] == "handoff"]
    assert any("Quality and Testing (Code Reviewer)" in h for h in handoffs)
    assert any("Quality and Testing (QA Engineer)" in h for h in handoffs)
    assert any(h["kind"] == "comment" and h["body"].startswith("Approved (") for h in history)
    assert any(h["body"] == "Starting: implementing core with unit tests." for h in history)   # agent's own log


def test_agents_file_tickets_that_the_team_then_builds(co):
    p = built(co)
    filed = [t for t in co.tickets.search(p["id"]) if t["origin"] == "agent"]
    assert len(filed) == 1 and filed[0]["status"] == "done"
    assert filed[0]["reporter"] == co.org.pick(role="code_reviewer")["name"]
    core = next(t for t in co.tickets.search(p["id"]) if t["key"] == "core")
    assert any(h["body"].startswith(f"Filed {filed[0]['ticket']}") for h in co.tickets.get(core["id"])["history"])


def test_agent_transfers_its_ticket_to_another_department(tmp_path):
    co = scripted_company(tmp_path, {("builder", "core"): [
        ("comment_ticket", {"body": "This needs a UX decision before it can be built."}),
        ("transfer_ticket", {"role": "ux_designer", "reason": "Decide the greeting wording first."})]})
    p = co.pipeline.create_project("Product", "Build a library")
    co.pipeline.advance(p["id"])
    decide_next(co, "ceo")
    decide_next(co, "cto")       # a designer writes no code, so the release checks then fail: that is expected
    core = co.tickets.get(next(t for t in co.tickets.search(p["id"]) if t["key"] == "core")["id"])
    transfers = [h for h in core["history"] if h["kind"] == "transfer"]
    assert len(transfers) == 1
    assert "from Engineering (Backend Engineer) to Design (UX Designer)" in transfers[0]["body"]
    assert core["role"] == "ux_designer" and core["status"] == "done"
    assert co.org.agent(core["assignee_id"])["role"] == "ux_designer"


def test_tickets_filed_during_release_checks_are_built_before_release(tmp_path):
    co = scripted_company(tmp_path, {("qa", "integration"): [
        ("create_ticket", {"title": "Error message is unclear", "description": "Say what went wrong.",
                           "type": "bug", "priority": "high", "role": "backend_engineer"})]})
    p = built(co)
    bug = next(t for t in co.tickets.search(p["id"]) if t["title"] == "Error message is unclear")
    assert bug["status"] == "done" and bug["reporter"] == co.org.pick(kind="qa")["name"]


def test_agent_ticket_limits(co):
    p = built(co)
    agent, result = co.org.pick(kind="qa"), SimpleNamespace(transfer=None, filed=[])
    ask = {"title": "Late finding", "description": "x", "type": "bug", "priority": "low", "role": "backend_engineer"}
    out = co.tickets.agent_tool(agent, "create_ticket", ask, p["id"], None, result)
    assert "backlog" in out.lower()                       # a releasing product is not reopened by an agent
    assert co.pipeline.project(p["id"])["stage"] == "release_approval"

    other = co.pipeline.create_project("Other", "Another product")
    with pytest.raises(TicketError, match="another project"):
        co.tickets.agent_tool(agent, "comment_ticket", {"ticket": "T-1", "body": "hi"}, other["id"], None, result)
    for _ in range(AGENT_TOTAL_LIMIT):
        co.tickets.agent_tool(agent, "create_ticket", ask, other["id"], None, result)
    statuses = [t["status"] for t in co.tickets.search(other["id"]) if t["origin"] == "agent"]
    assert set(statuses) == {"todo", "backlog"} and statuses.count("todo") < AGENT_TOTAL_LIMIT

    t = co.tickets.search(other["id"], "todo")[0]
    roles = ["frontend_engineer", "backend_engineer"]
    for i in range(MAX_TRANSFERS):
        co.tickets.agent_tool(agent, "transfer_ticket", {"ticket": t["ticket"], "role": roles[i % 2], "reason": "x"},
                              other["id"], None, result)
    with pytest.raises(TicketError, match="already been transferred"):
        co.tickets.agent_tool(agent, "transfer_ticket", {"ticket": t["ticket"], "role": roles[MAX_TRANSFERS % 2]},
                              other["id"], None, result)
    with pytest.raises(TicketError, match="cannot own tickets"):
        co.tickets.agent_tool(agent, "create_ticket", {**ask, "role": "code_reviewer"}, other["id"], None, result)


def test_humans_transfer_tickets_between_departments(co, capsys):
    p = co.pipeline.create_project("Product", "Build a library")
    t = co.tickets.create(p["id"], "Write launch copy", "", "Niki")
    t = co.tickets.update(t["ticket"], "Niki", role="ux_designer")
    assert t["department"] == "Design" and t["history"][-1]["kind"] == "transfer"
    assert main(["--home", str(co.s.root), "ticket", "update", t["ticket"], "--role", "frontend_engineer"]) == 0
    assert co.tickets.get(t["id"])["department"] == "Engineering"
    assert "transferred" in capsys.readouterr().out
