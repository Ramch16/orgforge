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
READ_TOOLS = {"read_file", "list_files", "list_tickets", "view_ticket"}
HISTORY = 30                                    # earlier messages the agent sees when replying
STALE = timedelta(minutes=15)                   # a reply still pending after this is treated as lost


class ChatError(ValueError):
    pass


class Chat:
    def __init__(self, company) -> None:
        self.co = company
        self.db = company.db
        self.on_work = None      # called with a project id when a chat files work (the server starts the team)
        self.background = False  # the server answers ticket comments in the background; the CLI waits

    def snapshot(self, project_id: int | None) -> str:
        """Where things stand, so an agent can answer "any update?" from facts rather than guesses."""
        if project_id:
            return self.co.reports.facts(project_id)
        from .pipeline import STAGE_LABELS
        rows = self.db.all("SELECT id, name, stage, version FROM projects WHERE stage!='dropped' ORDER BY id DESC")
        lines = []
        for p in rows:
            open_ = self.db.one("SELECT COUNT(*) AS n FROM tasks WHERE project_id=? AND status IN "
                                "('todo','in_progress','in_review','failed')", p["id"])["n"]
            waiting = self.db.one("SELECT COUNT(*) AS n FROM approvals WHERE project_id=? AND status='pending'",
                                  p["id"])["n"]
            lines.append(f"- Project {p['id']}, {p['name']}: {STAGE_LABELS.get(p['stage'], p['stage'])}"
                         + (f", version {p['version']}" if p["version"] else "")
                         + f"; {open_} open ticket(s); {waiting} decision(s) waiting")
        return "Company projects:\n" + ("\n".join(lines) or "- none yet")

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
                context = f"This chat is about project {project['id']}, \"{project['name']}\"."
            facts = self.snapshot(project_id)
            context += f"\nCurrent status (from the company's records):\n{facts}"
            mine = self.db.all("SELECT id, title, status FROM tasks WHERE assignee_id=? AND status NOT IN "
                               "('done','cancelled') ORDER BY id DESC LIMIT 10", agent["id"])
            if mine:
                context += "\nYour open tickets: " + "; ".join(f"T-{t['id']} {t['title']} ({t['status']})" for t in mine)
            message = f"{name} ({human.upper()}) writes:\n{body}\n\n[{context}]"
            result = self.co.runtime.run(
                agent, message, ws, project_id=project_id, history=history,
                only_tools=CHAT_TOOLS if project_id else set(), chat_with=f"{name}, the {human.upper()}",
                meta={"purpose": "chat", "human": name, "message": body, "requested_by": f"{name} ({human.upper()})",
                      "facts": facts})
            text = result.text.strip() or "(No reply.)"
            status = "sent" if result.completed else "failed"
            if project_id and "create_ticket" in result.tool_log and self.on_work:
                self.on_work(project_id)
        except Exception as exc:                 # shown in the thread rather than lost
            text, status = f"(Could not reply: {type(exc).__name__}: {exc})", "failed"
        self.db.run("UPDATE messages SET body=?, status=? WHERE id=?", text[:8000], status, reply_id)
        if status == "sent":
            self.db.log("chat", f"{agent['name']} replied to {name}.", project_id, actor=agent["name"])

    # ---- replies on tickets -------------------------------------------------
    def ticket_note(self, ticket_id: int, by: str, text: str, filed: bool = False) -> None:
        """The CEO or CTO filed a ticket or commented on one: its owner replies on the ticket."""
        if self.background:
            threading.Thread(target=self.answer_ticket, args=(ticket_id, by, text, filed), daemon=True).start()
        else:
            self.answer_ticket(ticket_id, by, text, filed)

    def answer_ticket(self, ticket_id: int, by: str, text: str, filed: bool = False) -> None:
        from .tickets import note
        t = self.co.tickets.get(ticket_id)
        holder = self.co.org.agent(t["assignee_id"]) if t["assignee_id"] else None
        agent = (holder if holder and holder["status"] != "fired" else None) or \
            self.co.org.pick(role=t["role"]) or self.co.org.pick(kind="product")
        if not agent:
            return
        p = self.co.pipeline.project(t["project_id"])
        facts = self.snapshot(p["id"])
        waiting = {"backlog": "It is in the Backlog, so nobody works on it until the CEO or CTO moves it to To do.",
                   "todo": "It is To do, so the team will pick it up."}.get(t["status"], "")
        ask = (f"{by} {'filed' if filed else 'commented on'} ticket {t['ticket']}, \"{t['title']}\" "
               f"({t['status_label']}, owned by {t['department'] or t['role']}):\n\n{text or t['description'] or t['title']}"
               f"\n\n{waiting}\n\nCurrent status of project {p['id']}, \"{p['name']}\" (from the company's records):\n"
               f"{facts}\n\nReply to {by} as a comment on the ticket, in a few sentences. Answer any question from the "
               "facts, the tickets and the files. If it asks for work, say plainly what happens next. Do not promise "
               "anything you cannot check.")
        try:
            res = self.co.runtime.run(agent, ask, self.co.pipeline.workspace(p), project_id=p["id"],
                                      only_tools=READ_TOOLS, chat_with=by,
                                      meta={"purpose": "ticket_reply", "human": by, "facts": facts,
                                            "ticket_status": t["status"], "message": text})
            reply = res.text.strip() if res.completed else ""
        except Exception as exc:
            reply = ""
            self.db.log("warn", f"{agent['name']} could not reply on {t['ticket']}: {exc}", p["id"])
        if reply:
            note(self.db, t["id"], agent["name"], reply[:4000], kind="comment")
            self.db.log("ticket", f"{agent['name']} replied to {by} on {t['ticket']}.", p["id"], actor=agent["name"])

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
