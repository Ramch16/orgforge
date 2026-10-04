"""Shared long-term memory: what the team has learned, kept across tickets and projects.

Agents save lasting facts and decisions with `remember` ("the API uses JWT in cookies",
"Niki wants British spelling everywhere"). Before each run, the memories most relevant to the
assignment, from its project and company-wide, are given to the agent.
"""
from __future__ import annotations

import re

from .db import DB, now

MEMORY_TOOL_SPECS = {
    "remember": {
        "description": "Save a lasting fact or decision the team should know later: a convention, a choice and why, "
                       "a gotcha, a preference from the CEO or CTO. Not for progress updates (comment on the ticket).",
        "input_schema": {"type": "object", "properties": {
            "text": {"type": "string", "description": "One or two sentences"},
            "scope": {"type": "string", "enum": ["project", "company"],
                      "description": "project (default) or company-wide"}}, "required": ["text"]},
    },
}
WORD = re.compile(r"[a-z0-9]{3,}")
STOP = {"the", "and", "for", "with", "that", "this", "from", "into", "your", "are", "was", "will", "should", "have",
        "has", "not", "but", "you", "all", "any", "can", "use", "its", "when", "what", "how", "why", "who"}


def words(text: str) -> set[str]:
    return {w for w in WORD.findall((text or "").lower()) if w not in STOP}


class Memory:
    def __init__(self, db: DB) -> None:
        self.db = db

    def remember(self, text: str, author: str, project_id: int | None, scope: str = "project") -> str:
        text = " ".join(str(text).split())[:600]
        if len(text) < 8:
            raise ValueError("Write the fact in a sentence or two.")
        pid = None if scope == "company" or not project_id else project_id
        same = self.db.one("SELECT id FROM memories WHERE lower(text)=lower(?) AND project_id IS ?", text, pid)
        if same:
            return "Already remembered."
        self.db.run("INSERT INTO memories (project_id, author, text, created_at) VALUES (?,?,?,?)", pid, author, text, now())
        self.db.log("memory", f"{author} remembered: {text[:120]}", project_id, actor=author)
        return "Remembered" + (" for the whole company." if pid is None else " for this project.")

    def recall(self, project_id: int | None, about: str, limit: int = 8) -> list[dict]:
        """The memories most relevant to `about`: shared words first, then the newest."""
        rows = self.db.all("SELECT * FROM memories WHERE project_id IS NULL OR project_id=? ORDER BY id DESC LIMIT 300",
                           project_id)
        want = words(about)
        ranked = sorted(rows, key=lambda m: (len(want & words(m["text"])), m["project_id"] == project_id, m["id"]),
                        reverse=True)
        return ranked[:limit]

    def list(self, project_id: int | None = None, limit: int = 50) -> list[dict]:
        if project_id:
            return self.db.all("SELECT * FROM memories WHERE project_id=? OR project_id IS NULL ORDER BY id DESC LIMIT ?",
                               project_id, limit)
        return self.db.all("SELECT * FROM memories ORDER BY id DESC LIMIT ?", limit)

    @staticmethod
    def as_text(memories: list[dict]) -> str:
        if not memories:
            return ""
        return ("What the team has learned (shared memory; follow it unless the CEO or CTO says otherwise):\n"
                + "\n".join(f"- {m['text']}" for m in memories))
