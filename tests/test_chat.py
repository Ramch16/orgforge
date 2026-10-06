import time

import pytest
from fastapi.testclient import TestClient

from orgforge.chat import ChatError
from orgforge.cli import main
from orgforge.company import Company
from orgforge.db import now
from orgforge.llm import MockProvider
from orgforge.server import create_app
from orgforge.tickets import TicketError
from test_tickets import built


class Recorder(MockProvider):
    """The mock company, recording what each chat reply was given; can fail on demand."""

    def __init__(self, fail=False):
        super().__init__()
        self.fail, self.calls = fail, []

    def complete(self, **kwargs):
        if (kwargs.get("meta") or {}).get("purpose") == "chat":
            self.calls.append(kwargs)
            if self.fail:
                raise RuntimeError("model unavailable")
        return super().complete(**kwargs)


def test_agent_replies_in_a_private_thread(co):
    p = co.pipeline.create_project("Greeter", "A tiny library.")
    thread = co.chat.send("Ram", "ceo", "What are you working on?", project_id=p["id"], wait=True)
    assert [m["sender"] for m in thread["messages"]] == ["human", "agent"]
    assert thread["messages"][-1]["status"] == "sent" and "Ram here" in thread["messages"][-1]["body"]
    assert thread["agent"]["department"] == "Product"
    assert co.chat.thread("Ram", "cto")["messages"] == []          # Lucky does not see Niki's chat


def test_asking_for_work_files_a_ticket_on_the_humans_behalf(co):
    p = built(co)                                                   # waiting for the CTO's release approval
    [release] = co.pipeline.inbox("cto")
    reply = co.chat.send("Hari", "cto", "Please add a farewell function.", project_id=p["id"], wait=True)["messages"][-1]
    ticket = next(t for t in co.tickets.search(p["id"]) if t["title"] == "Please add a farewell function")
    assert ticket["ticket"] in reply["body"] and ticket["status"] == "todo" and ticket["reporter"] == "Hari"
    history = co.tickets.get(ticket["id"])["history"]
    assert any(h["body"] == "Requested by Lucky (CTO) in a chat with Hari." for h in history)
    assert co.pipeline.project(p["id"])["stage"] == "build"         # the CTO's request reopens the release
    assert co.db.one("SELECT status FROM approvals WHERE id=?", release["id"])["status"] == "withdrawn"


def test_chat_tools_are_read_only_plus_tickets(tmp_path):
    provider = Recorder()
    co = Company(tmp_path, provider=provider, create=True)
    p = co.pipeline.create_project("Greeter", "A tiny library.")
    co.chat.send("Hari", "cto", "How is it going?", project_id=p["id"], wait=True)
    offered = {t["name"] for t in provider.calls[-1]["tools"]}
    assert offered == {"read_file", "list_files", "list_tickets", "view_ticket", "create_ticket", "comment_ticket", "recall_memory"}
    assert "direct chat with Lucky" in provider.calls[-1]["system"]
    co.chat.send("Hari", "cto", "Thanks", wait=True)                 # no project: nothing to open
    assert provider.calls[-1]["tools"] == []


def test_the_agent_remembers_the_conversation(tmp_path):
    provider = Recorder()
    co = Company(tmp_path, provider=provider, create=True)
    co.chat.send("Sony", "cto", "First question", wait=True)
    co.chat.send("Sony", "cto", "Second question", wait=True)
    turns = provider.calls[-1]["messages"]
    assert turns[0] == {"role": "user", "content": "First question"} and turns[1]["role"] == "assistant"
    assert "Second question" in turns[-1]["content"]


def test_chat_rules(co):
    with pytest.raises(ChatError, match="Write a message"):
        co.chat.send("Ram", "ceo", "   ")
    with pytest.raises(ChatError, match="Only the CEO and CTO"):
        co.chat.send("Ram", "agent", "hi")
    co.db.run("INSERT INTO messages (agent_id, human, sender, status, created_at) VALUES (1, 'ceo', 'agent', "
              "'pending', ?)", now())
    with pytest.raises(ChatError, match="still replying"):
        co.chat.send("Ram", "ceo", "hello again")
    co.org.fire("Sanjana", "left", by="CEO")
    with pytest.raises(ChatError, match="left the company"):
        co.chat.send("Sanjana", "ceo", "hi")


def test_a_failed_reply_is_shown_not_lost(tmp_path):
    co = Company(tmp_path, provider=Recorder(fail=True), create=True)
    reply = co.chat.send("Ram", "ceo", "hello", wait=True)["messages"][-1]
    assert reply["status"] == "failed" and "model unavailable" in reply["body"]
    assert co.chat.send("Ram", "ceo", "hello again", wait=True)          # not stuck as pending


def test_chat_api_replies_in_the_background_and_starts_work(co):
    client = TestClient(create_app(co, {"ceo": "ceo-token", "cto": "cto-token"}))
    cto = {"X-Token": "cto-token"}
    pid = co.pipeline.create_project("Greeter", "A tiny library.")["id"]
    hari = co.org.agent("Hari")["id"]
    started = []
    co.chat.on_work = started.append
    assert client.post(f"/api/agents/{hari}/messages", json={"body": "hi"}).status_code == 401
    sent = client.post(f"/api/agents/{hari}/messages", json={"body": "Please fix the typo", "project_id": pid},
                       headers=cto).json()
    assert sent["messages"][0]["body"] == "Please fix the typo"
    for _ in range(100):
        thread = client.get(f"/api/agents/{hari}/messages", headers=cto).json()
        if not thread["pending"]:
            break
        time.sleep(0.05)
    assert thread["messages"][-1]["status"] == "sent" and "T-" in thread["messages"][-1]["body"]
    assert started == [pid]
    chats = client.get("/api/state", headers=cto).json()["chats"]
    assert chats[0]["agent_id"] == hari and not chats[0]["pending"]
    assert client.get(f"/api/agents/{hari}/messages", headers={"X-Token": "ceo-token"}).json()["messages"] == []


def test_chat_cli(co, capsys):
    home = ["--home", str(co.s.root)]
    assert main([*home, "chat", "Sravani", "How should errors look?", "--as", "ceo"]) == 0
    assert main([*home, "chat", "Sravani", "--as", "ceo"]) == 0
    out = capsys.readouterr().out
    assert "Sravani: Hi Niki, Sravani here." in out and "UX Designer (Design)" in out and "How should errors look?" in out


def test_status_questions_get_answered_from_the_records(tmp_path):
    provider = Recorder()
    co = Company(tmp_path, provider=provider, create=True)
    p = co.pipeline.create_project("APTV clone", "Clone an iOS app.")
    co.pipeline.advance(p["id"])
    reply = co.chat.send("Ram", "cto", "any update on APTV clone? is it ready?", wait=True)["messages"][-1]["body"]
    assert "APTV clone: Requirements with CEO" in reply             # no project picked: every project's status
    assert "Requirements with CEO" in [m["content"] for m in provider.calls[-1]["messages"] if isinstance(m["content"], str)][-1]
    reply = co.chat.send("Ram", "cto", "and now?", project_id=p["id"], wait=True)["messages"][-1]["body"]
    asked = [m["content"] for m in provider.calls[-1]["messages"] if isinstance(m["content"], str)][-1]
    assert "Stage: Requirements with CEO" in reply and "Waiting on a decision" in asked


def test_ticket_owner_replies_to_the_ceo_and_cto(co):
    p = co.pipeline.create_project("APTV clone", "Clone an iOS app.")
    t = co.tickets.create(p["id"], "Check for status", "Is it ready?", "Niki")
    first = next(h for h in co.tickets.get(t["id"])["history"] if h["kind"] == "comment")
    assert first["author"] == "Hari" and "APTV clone" in first["body"]       # the backend ticket's owner
    assert co.tickets.get(t["id"])["status"] == "done"                        # just a question: answered, closed
    co.tickets.comment(t["ticket"], "Lucky", "any update?")
    assert co.tickets.get(t["id"])["history"][-1]["author"] == "Hari"
    co.tickets.comment(t["ticket"], "Hari", "Working on it")                # agents' own comments get no reply
    assert co.tickets.get(t["id"])["history"][-1]["body"] == "Working on it"


def test_a_question_ticket_is_answered_and_closed_never_built(co):
    p = built(co)                                                   # waiting for the CTO's release approval
    [release] = co.pipeline.inbox("cto")
    t = co.tickets.create(p["id"], "Request for status", "Is the application ready to test and deploy?", "Niki",
                          priority="urgent", status="todo")
    t = co.tickets.get(t["id"])
    assert t["status"] == "done" and t["type"] == "question"
    assert any(h["body"].startswith("Closed as answered") for h in t["history"])
    assert co.pipeline.advance(p["id"])["stage"] == "release_approval"      # nothing was built
    assert co.db.one("SELECT status FROM approvals WHERE id=?", release["id"])["status"] == "pending"
    assert not co.db.one("SELECT 1 FROM ticket_comments WHERE task_id=? AND body LIKE 'Started%'", t["id"])


def test_work_tickets_stay_open_and_comments_never_close_them(co):
    p = co.pipeline.create_project("Greeter", "A tiny library.")
    work = co.tickets.create(p["id"], "Add a farewell", "Please add a farewell function.", "Niki")
    assert co.tickets.get(work["id"])["status"] == "backlog"
    co.tickets.comment(work["ticket"], "Lucky", "Any update? Is it ready?")
    assert co.tickets.get(work["id"])["status"] == "backlog"                # still open
    q = co.tickets.create(p["id"], "Status?", "", "Niki", type="question")
    assert co.tickets.get(q["id"])["status"] == "done"
    co.db.run("UPDATE tasks SET status='backlog' WHERE id=?", q["id"])      # even if reopened by hand
    with pytest.raises(TicketError, match="question"):
        co.tickets.update(q["id"], "Niki", status="todo")
