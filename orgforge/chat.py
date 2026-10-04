"""Direct chat between the two humans (CEO and CTO) and any agent.

Each human has a private thread with each agent. An agent answers in its own role and can
read the chosen project's files and tickets. It cannot change code from a chat: requests for
work become tickets (filed on the human's behalf), so they go through review and the release
checks like everything else.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from .db import now

CHAT_TOOLS = {"read_file", "list_files", "list_tickets", "view_ticket", "create_ticket", "comment_ticket"}
HISTORY = 30                                    # earlier messages the agent sees when replying
STALE = timedelta(minutes=15)                   # a reply still pending after this is treated as lost


class ChatError(ValueError):
    pass


class Chat:
    def __init__(self, company) -> None:
        self.co = company
        self.db = company.db
        self.on_work = None      # called with a project id when a chat files work (the server starts the team)

    def thread(self, agent_ref, human: str, limit: int = 200) -> dict:
        agent = self.co.org.agent(agent_ref)
        rows = self.db.all("SELECT * FROM (SELECT * FROM messages WHERE agent_id=? AND human=? ORDER BY id DESC "
                           "LIMIT ?) ORDER BY id", agent["id"], human, limit)
        cutoff = (datetime.now(timezone.utc) - STALE).isoformat(timespec="seconds")
        for m in rows:
            if m["status"] == "pending" and m["created_at"] < cutoff:
                m["status"], m["body"] = "failed", "(No reply. Send your message again.)"
        role = self.co.org.role(agent["role"])
        dept = self.db.one("SELECT name FROM departments WHERE id=?", role["department"])
        return {"agent": {"id": agent["id"], "name": agent["name"], "role": agent["role"], "title": role["title"],
                          "department": dept["name"] if dept else "", "status": agent["status"]},
                "messages": rows, "pending": any(m["status"] == "pending" for m in rows)}

    def send(self, agent_ref, human: str, body: str, project_id: int | None = None, wait: bool = False) -> dict:
        """Post the human's message and get the agent's reply (in the background unless `wait`)."""
        if human not in ("ceo", "cto"):
            raise ChatError("Only the CEO and CTO can chat with agents.")
        agent = self.co.org.agent(agent_ref)
        body = body.strip()
        if not body:
            raise ChatError("Write a message first.")
        if agent["status"] == "fired":
            raise ChatError(f"{agent['name']} has left the company.")
        if project_id:
            self.co.pipeline.project(project_id)
        if self.thread(agent["id"], human, limit=5)["pending"]:
            raise ChatError(f"{agent['name']} is still replying to your last message.")
        ts = now()
        asked = self.db.run("INSERT INTO messages (agent_id, human, sender, body, project_id, created_at) "
                            "VALUES (?,?,?,?,?,?)", agent["id"], human, "human", body[:8000], project_id, ts)
        reply_id = self.db.run("INSERT INTO messages (agent_id, human, sender, body, project_id, status, created_at) "
                               "VALUES (?,?,?,?,?,?,?)", agent["id"], human, "agent", "", project_id, "pending", ts)
        if wait:
            self._reply(reply_id, asked, agent, human, body, project_id)
        else:
            threading.Thread(target=self._reply, args=(reply_id, asked, agent, human, body, project_id),
                             daemon=True).start()
        return self.thread(agent["id"], human)

    def _reply(self, reply_id: int, asked: int, agent: dict, human: str, body: str, project_id: int | None) -> None:
        name = self.co.s.human(human)
        try:
            earlier = self.db.all("SELECT * FROM (SELECT * FROM messages WHERE agent_id=? AND human=? AND id<? "
                                  "AND status='sent' ORDER BY id DESC LIMIT ?) ORDER BY id",
                                  agent["id"], human, asked, HISTORY)
            history = [{"role": "user" if m["sender"] == "human" else "assistant", "content": m["body"]}
                       for m in earlier if m["body"]]
            history = self._alternate(history)
            ws, context = None, "No project is selected, so you cannot open files or tickets in this chat."
            if project_id:
                project = self.co.pipeline.project(project_id)
                ws = self.co.pipeline.workspace(project)
                context = (f"This chat is about project {project['id']}, \"{project['name']}\" "
                           f"(stage: {project['stage']}).")
            mine = self.db.all("SELECT id, title, status FROM tasks WHERE assignee_id=? AND status NOT IN "
                               "('done','cancelled') ORDER BY id DESC LIMIT 10", agent["id"])
            if mine:
                context += "\nYour open tickets: " + "; ".join(f"T-{t['id']} {t['title']} ({t['status']})" for t in mine)
            message = f"{name} ({human.upper()}) writes:\n{body}\n\n[{context}]"
            result = self.co.runtime.run(
                agent, message, ws, project_id=project_id, history=history,
                only_tools=CHAT_TOOLS if project_id else set(), chat_with=f"{name}, the {human.upper()}",
                meta={"purpose": "chat", "human": name, "message": body, "requested_by": f"{name} ({human.upper()})"})
            text = result.text.strip() or "(No reply.)"
            status = "sent" if result.completed else "failed"
            if project_id and "create_ticket" in result.tool_log and self.on_work:
                self.on_work(project_id)
        except Exception as exc:                 # shown in the thread rather than lost
            text, status = f"(Could not reply: {type(exc).__name__}: {exc})", "failed"
        self.db.run("UPDATE messages SET body=?, status=? WHERE id=?", text[:8000], status, reply_id)
        if status == "sent":
            self.db.log("chat", f"{agent['name']} replied to {name}.", project_id, actor=agent["name"])

    @staticmethod
    def _alternate(history: list[dict]) -> list[dict]:
        """The model needs user/assistant turns to alternate, starting with the user."""
        out: list[dict] = []
        for m in history:
            if out and out[-1]["role"] == m["role"]:
                out[-1] = {"role": m["role"], "content": out[-1]["content"] + "\n\n" + m["content"]}
            else:
                out.append(m)
        while out and out[0]["role"] != "user":
            out.pop(0)
        if out and out[-1]["role"] == "user":       # the new message is a user turn too
            out.pop()
        return out
