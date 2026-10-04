"""A live record of every agent run, and messages the CEO or CTO send an agent while it works.

Each run logs its instructions, what the agent says, every tool call and result, and how it ended,
so the dashboard can show an agent's work as it happens. A steering message reaches the agent at its
next step (for CLI engines, which run in one go, at the start of its next run).
"""
from __future__ import annotations

import json

from .db import DB, now

CLIP = 1500


def _clip(text: str, n: int = CLIP) -> str:
    text = str(text)
    return text if len(text) <= n else text[:n] + f"… [{len(text) - n} more characters]"


def describe_call(name: str, args: dict) -> str:
    """A tool call, readable: long values (file contents) are shortened."""
    shown = {k: (_clip(v, 160) if isinstance(v, str) else v) for k, v in (args or {}).items()}
    return f"{name}({json.dumps(shown, ensure_ascii=False)[:600]})"


class Runs:
    def __init__(self, db: DB) -> None:
        self.db = db

    def start(self, agent: dict, project_id: int | None, ticket_id: int | None, purpose: str, instructions: str) -> int:
        rid = self.db.run("INSERT INTO runs (agent_id, project_id, ticket_id, purpose, status, started_at) "
                          "VALUES (?,?,?,?,?,?)", agent["id"], project_id, ticket_id, purpose, "running", now())
        self.event(rid, "instruction", _clip(instructions, 4000))
        return rid

    def event(self, run_id: int, kind: str, body: str) -> None:
        self.db.run("INSERT INTO run_events (run_id, kind, body, created_at) VALUES (?,?,?,?)", run_id, kind,
                    _clip(body, 4000), now())

    def finish(self, run_id: int, status: str, text: str = "") -> None:
        if text:
            self.event(run_id, "result", text)
        self.db.run("UPDATE runs SET status=?, ended_at=? WHERE id=?", status, now(), run_id)

    # ---- steering ----------------------------------------------------------
    def steer(self, agent_id: int, by: str, body: str) -> dict:
        body = body.strip()
        if not body:
            raise ValueError("Write a message first.")
        running = self.db.one("SELECT id FROM runs WHERE agent_id=? AND status='running' ORDER BY id DESC LIMIT 1", agent_id)
        sid = self.db.run("INSERT INTO steers (agent_id, run_id, author, body, created_at) VALUES (?,?,?,?,?)",
                          agent_id, running["id"] if running else None, by, body[:2000], now())
        return {"id": sid, "live": bool(running)}

    def take_steers(self, agent_id: int) -> list[dict]:
        """Undelivered messages for this agent, marked delivered."""
        rows = self.db.all("SELECT * FROM steers WHERE agent_id=? AND delivered_at IS NULL ORDER BY id", agent_id)
        for r in rows:
            self.db.run("UPDATE steers SET delivered_at=? WHERE id=?", now(), r["id"])
        return rows

    @staticmethod
    def steer_text(steers: list[dict]) -> str:
        return "\n".join(f"Message from {s['author']} while you work (it overrides earlier instructions where they "
                         f"conflict): {s['body']}" for s in steers)

    # ---- reading -----------------------------------------------------------
    def recent(self, agent_id: int, limit: int = 10) -> list[dict]:
        return self.db.all("SELECT r.*, (SELECT COUNT(*) FROM run_events e WHERE e.run_id=r.id) AS steps FROM runs r "
                           "WHERE agent_id=? ORDER BY id DESC LIMIT ?", agent_id, limit)

    def transcript(self, run_id: int, after: int = 0) -> dict:
        run = self.db.one("SELECT r.*, a.name AS agent FROM runs r JOIN agents a ON a.id=r.agent_id WHERE r.id=?", run_id)
        if not run:
            raise ValueError(f"No run {run_id}.")
        events = self.db.all("SELECT * FROM run_events WHERE run_id=? AND id>? ORDER BY id", run_id, after)
        return {"run": run, "events": events}
