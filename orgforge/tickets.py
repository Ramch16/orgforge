"""The internal ticket tracker. Every pipeline task is a ticket; the CEO and CTO can file their own.

Agents own the working statuses (in progress, in review, done, failed). Humans triage:
they file tickets, comment, set priority and move tickets between backlog, to do and
cancelled. Comments from humans are passed to the agent who next works the ticket.
"""
from __future__ import annotations

import json

from .db import DB, now

TYPES = ("task", "bug", "story")
PRIORITIES = ("urgent", "high", "medium", "low")
STATUSES = ("backlog", "todo", "in_progress", "in_review", "done", "failed", "cancelled")
STATUS_LABELS = {"backlog": "Backlog", "todo": "To do", "in_progress": "In progress", "in_review": "In review",
                 "done": "Done", "failed": "Needs CTO", "cancelled": "Cancelled"}
OPEN = ("backlog", "todo")                  # statuses a human may set, along with cancelled
PRIORITY_ORDER = "CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END"
REOPEN_STAGES = ("release_approval", "signoff", "done")


class TicketError(ValueError):
    pass


def ticket_key(task_id: int) -> str:
    return f"T-{task_id}"


def note(db: DB, task_id: int, author: str, body: str, kind: str = "change") -> None:
    db.run("INSERT INTO ticket_comments (task_id, author, kind, body, created_at) VALUES (?,?,?,?,?)",
           task_id, author, kind, body, now())


class Tickets:
    def __init__(self, db: DB, pipeline) -> None:
        self.db, self.pipeline = db, pipeline

    # ---- reading ---------------------------------------------------------
    def search(self, project_id: int | None = None, status: str | None = None) -> list[dict]:
        sql = ("SELECT t.*, a.name AS assignee, p.name AS project, "
               "(SELECT COUNT(*) FROM ticket_comments c WHERE c.task_id=t.id AND c.kind='comment') AS comments "
               "FROM tasks t JOIN projects p ON p.id=t.project_id LEFT JOIN agents a ON a.id=t.assignee_id WHERE 1=1")
        args: list = []
        if project_id:
            sql += " AND t.project_id=?"; args.append(project_id)
        if status:
            sql += " AND t.status=?"; args.append(status)
        sql += f" ORDER BY {PRIORITY_ORDER.replace('priority', 't.priority')}, t.id"
        return [self._present(t) for t in self.db.all(sql, *args)]

    def get(self, ref: int | str) -> dict:
        raw = str(ref).strip().upper()
        raw = raw[2:] if raw.startswith("T-") else raw
        tid = int(raw) if raw.isdigit() else 0
        t = self.db.one("SELECT t.*, a.name AS assignee, p.name AS project FROM tasks t JOIN projects p "
                        "ON p.id=t.project_id LEFT JOIN agents a ON a.id=t.assignee_id WHERE t.id=?", tid)
        if not t:
            raise TicketError(f"No ticket {ref}.")
        t = self._present(t)
        t["history"] = self.db.all("SELECT * FROM ticket_comments WHERE task_id=? ORDER BY id", tid)
        return t

    @staticmethod
    def _present(t: dict) -> dict:
        return {**t, "ticket": ticket_key(t["id"]), "status_label": STATUS_LABELS.get(t["status"], t["status"])}

    # ---- human actions ---------------------------------------------------
    def create(self, project_id: int, title: str, description: str, by: str, type: str = "task",
               priority: str = "medium", status: str = "backlog", role: str | None = None) -> dict:
        project = self.pipeline.project(project_id)
        title, description = title.strip(), description.strip()
        if not title:
            raise TicketError("A ticket needs a title.")
        self._check(type=type, priority=priority, status=status)
        if status == "cancelled":
            raise TicketError("File a ticket as backlog or to do.")
        builders = {r["id"] for r in self.pipeline._builder_roles()}
        if role and role not in builders:
            raise TicketError(f"Role '{role}' is not a staffed builder role: {', '.join(sorted(builders)) or 'none'}.")
        role = role or self.pipeline._fix_role(project_id)
        ts = now()
        tid = self.db.run(
            "INSERT INTO tasks (project_id, key, title, description, role, status, type, priority, origin, reporter, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            project_id, "pending", title, description, role, status, type, priority, "human", by, ts, ts)
        self.db.run("UPDATE tasks SET key=? WHERE id=?", f"t-{tid}", tid)
        note(self.db, tid, by, f"Filed as {type}, {priority} priority, {STATUS_LABELS[status]}.")
        self.db.log("ticket", f"{by} filed {ticket_key(tid)}: {title}", project_id, actor=by)
        if status == "todo":
            self._reopen(project, by)
        return self.get(tid)

    def update(self, ref: int | str, by: str, *, status: str | None = None, priority: str | None = None,
               title: str | None = None, description: str | None = None) -> dict:
        t = self.get(ref)
        self._check(priority=priority, status=status)
        changes = []
        if priority and priority != t["priority"]:
            if t["status"] in ("done", "cancelled"):
                raise TicketError(f"{t['ticket']} is closed.")
            changes.append(("priority", priority, f"Priority {t['priority']} → {priority}."))
        for field, value in (("title", title), ("description", description)):
            if value is not None and value.strip() != t[field]:
                if t["status"] not in OPEN:
                    raise TicketError(f"{t['ticket']} can only be edited while it is in backlog or to do.")
                if field == "title" and not value.strip():
                    raise TicketError("A ticket needs a title.")
                changes.append((field, value.strip(), f"{field.title()} edited."))
        if status and status != t["status"]:
            if t["status"] not in OPEN or status not in (*OPEN, "cancelled"):
                raise TicketError(f"The team owns {STATUS_LABELS[t['status']].lower()} tickets. "
                                  "You can move tickets between backlog, to do and cancelled.")
            if status == "backlog":
                blocked = self._open_dependents(t)
                if blocked:
                    raise TicketError(f"Open tickets depend on {t['ticket']}: {', '.join(blocked)}. "
                                      "Cancel it instead, or move those first.")
            changes.append(("status", status, f"{STATUS_LABELS[t['status']]} → {STATUS_LABELS[status]}."))
        if not changes:
            return t
        for field, value, _ in changes:
            self.db.run(f"UPDATE tasks SET {field}=?, updated_at=? WHERE id=?", value, now(), t["id"])
        note(self.db, t["id"], by, " ".join(msg for *_, msg in changes))
        self.db.log("ticket", f"{by} updated {t['ticket']}: " + " ".join(msg for *_, msg in changes),
                    t["project_id"], actor=by)
        if status == "todo":
            self._reopen(self.pipeline.project(t["project_id"]), by)
        return self.get(t["id"])

    def comment(self, ref: int | str, by: str, body: str) -> dict:
        t = self.get(ref)
        if not body.strip():
            raise TicketError("Write a comment first.")
        note(self.db, t["id"], by, body.strip(), kind="comment")
        self.db.log("ticket", f"{by} commented on {t['ticket']}.", t["project_id"], actor=by)
        return self.get(t["id"])

    # ---- helpers ---------------------------------------------------------
    @staticmethod
    def _check(type: str | None = None, priority: str | None = None, status: str | None = None) -> None:
        for value, allowed, what in ((type, TYPES, "type"), (priority, PRIORITIES, "priority"),
                                     (status, STATUSES, "status")):
            if value is not None and value not in allowed:
                raise TicketError(f"Unknown {what} '{value}'. Use one of: {', '.join(allowed)}.")

    def _open_dependents(self, t: dict) -> list[str]:
        rows = self.db.all("SELECT id, depends_on FROM tasks WHERE project_id=? AND status NOT IN "
                           "('done','cancelled')", t["project_id"])
        return [ticket_key(r["id"]) for r in rows if t["key"] in json.loads(r["depends_on"])]

    def _reopen(self, project: dict, by: str) -> None:
        """New work on a finished or releasing product sends it back to build; pending release votes lapse."""
        if project["stage"] not in REOPEN_STAGES:
            return
        self.db.run("UPDATE approvals SET status='withdrawn', decided_by=?, decided_at=? WHERE project_id=? "
                    "AND status='pending' AND kind IN ('release','signoff')", by, now(), project["id"])
        self.pipeline._stage(project["id"], "build")
        self.db.log("project", "Reopened for new tickets; the release will be verified again.", project["id"], actor=by)
