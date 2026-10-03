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
    tickets = co.tickets.search(p["id"])
    assert [t["key"] for t in tickets] == ["core", "extras"]
    assert all(t["status"] == "done" and t["ticket"] == f"T-{t['id']}" for t in tickets)
    history = [h["body"] for h in co.tickets.get(tickets[0]["ticket"])["history"]]
    assert history[0].startswith("Created from the build plan")
    assert any(h.startswith("Started work") for h in history) and any(h.startswith("Passed review") for h in history)
    assert tickets[0]["reporter"] == co.org.pick(kind="planner")["name"]


def test_backlog_ticket_waits_and_does_not_block_release(co):
    p = co.pipeline.create_project("Product", "Build a library")
    co.pipeline.advance(p["id"])
    t = co.tickets.create(p["id"], "Nice to have: dark mode", "", "Ram")
    decide_next(co, "ceo")
    assert decide_next(co, "cto")["stage"] == "release_approval"
    assert co.tickets.get(t["id"])["status"] == "backlog"


def test_todo_ticket_reopens_a_releasing_product_and_agent_reads_comments(co, monkeypatch):
    p = built(co)
    [release] = co.pipeline.inbox("cto")
    t = co.tickets.create(p["id"], "Fix the greeting typo", "It says Helo.", "Ram", type="bug", priority="high")
    co.tickets.comment(t["ticket"], "Ram", "Use British spelling.")
    asks = []
    original = co.runtime.run
    def run(agent, ask, *args, **kwargs):
        asks.append(ask)
        return original(agent, ask, *args, **kwargs)
    monkeypatch.setattr(co.runtime, "run", run)

    co.tickets.update(t["ticket"], "Ram", status="todo")
    assert co.pipeline.project(p["id"])["stage"] == "build"
    assert co.db.one("SELECT status FROM approvals WHERE id=?", release["id"])["status"] == "withdrawn"
    assert co.pipeline.advance(p["id"])["stage"] == "release_approval"
    assert co.tickets.get(t["id"])["status"] == "done"
    assert any("Ram: Use British spelling." in a and "Fix the greeting typo" in a for a in asks)


def test_urgent_ticket_is_worked_first(co):
    p = built(co)
    low = co.tickets.create(p["id"], "Low", "", "Ram", priority="low", status="todo")
    urgent = co.tickets.create(p["id"], "Urgent", "", "Ram", priority="urgent", status="todo")
    assert co.pipeline._next_task(p["id"])["id"] == urgent["id"]
    co.pipeline.advance(p["id"])
    started = [h["task_id"] for h in co.db.all("SELECT task_id FROM ticket_comments WHERE body LIKE 'Started%' "
                                                "AND task_id IN (?,?) ORDER BY id", low["id"], urgent["id"])]
    assert started == [urgent["id"], low["id"]]


def test_humans_triage_but_the_team_owns_working_statuses(co):
    p = co.pipeline.create_project("Product", "Build a library")
    co.pipeline.advance(p["id"])
    decide_next(co, "ceo")                                   # plan is waiting for the CTO: tickets are to do
    core, extras = co.tickets.search(p["id"])
    with pytest.raises(TicketError, match="team owns|between backlog"):
        co.tickets.update(core["ticket"], "Ram", status="done")
    with pytest.raises(TicketError, match="depend on"):
        co.tickets.update(core["ticket"], "Ram", status="backlog")
    with pytest.raises(TicketError, match="Unknown priority"):
        co.tickets.update(core["ticket"], "Ram", priority="asap")
    co.tickets.update(extras["ticket"], "Ram", priority="urgent", title="Supporting module v2")
    assert co.tickets.get(extras["id"])["title"] == "Supporting module v2"
    co.tickets.update(core["ticket"], "Ram", status="cancelled")
    assert co.pipeline._next_task(p["id"])["key"] == "extras"   # a cancelled dependency no longer blocks
    with pytest.raises(TicketError, match="closed"):
        co.tickets.update(core["ticket"], "Ram", priority="low")


def test_redesign_keeps_human_tickets(co):
    p = co.pipeline.create_project("Product", "Build a library")
    co.pipeline.advance(p["id"])
    decide_next(co, "ceo")
    mine = co.tickets.create(p["id"], "Add CSV export", "", "Ram")
    decide_next(co, "cto", "rejected", "Simplify the design")
    assert co.tickets.get(mine["id"])["status"] == "backlog"
    assert len(co.tickets.search(p["id"])) == 3


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
    assert [h["kind"] for h in detail["history"]] == ["change", "comment", "change"]
    state = client.get("/api/state", headers=cto).json()
    assert state["tickets"][0]["comments"] == 1 and "in_review" in state["ticket_meta"]["statuses"]
    assert client.get("/api/tickets/T-999", headers=cto).status_code == 400


def test_ticket_cli(co, capsys):
    p = co.pipeline.create_project("Product", "Build a library")
    home = ["--home", str(co.s.root)]
    assert main([*home, "ticket", "new", str(p["id"]), "Add a logo", "--type", "story", "--priority", "high"]) == 0
    assert main([*home, "ticket", "comment", "T-1", "Make it blue", "--as", "cto"]) == 0
    assert main([*home, "ticket", "list"]) == 0
    out = capsys.readouterr().out
    assert "Filed T-1." in out and "Make it blue" in out and "high" in out and "(1 comments)" in out
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
