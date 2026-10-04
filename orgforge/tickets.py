"""The internal ticket tracker, used by the whole company.

Every piece of work is a ticket: each department's stage work (requirements, design,
architecture, release QA, audits), every build task, every fix. Tickets record each
handoff between departments. Agents work their tickets and log as they go: they comment,
file tickets for bugs and follow-up work, and transfer tickets that another department
should own. The CEO and CTO triage: they file, comment, set priority, assign a role and
move tickets between backlog, to do and cancelled. The pipeline owns the working statuses.
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
WORK_KINDS = ("builder", "designer", "product", "qa", "auditor")   # roles that can own and work a ticket
AGENT_OPEN_LIMIT = 10                       # past these, agent-filed tickets wait in backlog for triage,
AGENT_TOTAL_LIMIT = 25                      # so agents cannot keep a project busy indefinitely
MAX_TRANSFERS = 4                           # agent transfers per ticket, so tickets cannot ping-pong forever

_STR = {"type": "string"}
_TICKET = {"type": "string", "description": "Ticket id like T-12. Defaults to the ticket you are working on."}
TICKET_TOOL_SPECS: dict[str, dict] = {
    "list_tickets": {
        "description": "List this project's tickets with status, priority, department and owner.",
        "input_schema": {"type": "object", "properties": {
            "status": {"type": "string", "enum": list(STATUSES), "description": "Optional filter"}}, "required": []},
    },
    "view_ticket": {
        "description": "Read a ticket: description and its full history of work, comments and transfers.",
        "input_schema": {"type": "object", "properties": {"ticket": _TICKET}, "required": []},
    },
    "create_ticket": {
        "description": "File a ticket for work outside your assignment: a bug you found, missing work, a follow-up "
                       "or something another department must do. Do not file tickets for your own current task.",
        "input_schema": {"type": "object", "properties": {
            "title": _STR,
            "description": {"type": "string", "description": "What is wrong or needed, and how to tell it is done"},
            "type": {"type": "string", "enum": list(TYPES)},
            "priority": {"type": "string", "enum": list(PRIORITIES)},
            "role": {"type": "string", "description": "Role id that should own it, e.g. frontend_engineer"}},
            "required": ["title", "description", "type", "priority", "role"]},
    },
    "comment_ticket": {
        "description": "Log progress, a decision, a blocker or a question on a ticket. Keep it short and factual.",
        "input_schema": {"type": "object", "properties": {"ticket": _TICKET, "body": _STR}, "required": ["body"]},
    },
    "transfer_ticket": {
        "description": "Hand a ticket to another role or department that should own it. Transferring the ticket "
                       "you are working on ends your work on it: stop, and summarise what you did and why.",
        "input_schema": {"type": "object", "properties": {
            "ticket": _TICKET, "role": {"type": "string", "description": "Role id to own it next"},
            "reason": {"type": "string", "description": "Why they should own it and what they need to know"}},
            "required": ["role", "reason"]},
    },
}
TICKET_RULES = ("Use the company ticket tracker as you work: comment progress, decisions and blockers on your ticket "
                "(comment_ticket), file tickets for bugs or follow-up work you find outside your assignment "
                "(create_ticket), and transfer a ticket when another department should own it (transfer_ticket). "
                "Check list_tickets before filing so you do not duplicate an existing ticket.")


class TicketError(ValueError):
    pass


def ticket_key(task_id: int) -> str:
    return f"T-{task_id}"


def where(db: DB, role_id: str) -> str:
    """A role as people say it: its department and title."""
    r = db.one("SELECT r.title, d.name AS department FROM roles r LEFT JOIN departments d "
               "ON d.id=r.department WHERE r.id=?", role_id)
    return f"{r['department']} ({r['title']})" if r else role_id


def note(db: DB, task_id: int, author: str, body: str, kind: str = "change") -> None:
    db.run("INSERT INTO ticket_comments (task_id, author, kind, body, created_at) VALUES (?,?,?,?,?)",
           task_id, author, kind, body, now())


class Tickets:
    def __init__(self, db: DB, pipeline) -> None:
        self.db, self.pipeline = db, pipeline

    # ---- reading ---------------------------------------------------------
    _SELECT = ("SELECT t.*, a.name AS assignee, p.name AS project, d.name AS department, "
               "(SELECT COUNT(*) FROM ticket_comments c WHERE c.task_id=t.id AND c.kind='comment') AS comments "
               "FROM tasks t JOIN projects p ON p.id=t.project_id LEFT JOIN agents a ON a.id=t.assignee_id "
               "LEFT JOIN roles r ON r.id=t.role LEFT JOIN departments d ON d.id=r.department")

    def search(self, project_id: int | None = None, status: str | None = None) -> list[dict]:
        sql, args = self._SELECT + " WHERE 1=1", []
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
        t = self.db.one(self._SELECT + " WHERE t.id=?", tid)
        if not t:
            raise TicketError(f"No ticket {ref}.")
        t = self._present(t)
        t["history"] = self.db.all("SELECT * FROM ticket_comments WHERE task_id=? ORDER BY id", tid)
        return t

    @staticmethod
    def _present(t: dict) -> dict:
        return {**t, "ticket": ticket_key(t["id"]), "status_label": STATUS_LABELS.get(t["status"], t["status"]),
                "department": t.get("department") or ""}

    def work_roles(self) -> list[dict]:
        """Staffed roles that can own a ticket, with their department."""
        marks = ",".join("?" * len(WORK_KINDS))
        return self.db.all(f"SELECT DISTINCT r.id, r.title, r.kind, d.name AS department FROM roles r "
                           f"JOIN agents a ON a.role=r.id AND a.status!='fired' JOIN departments d ON d.id=r.department "
                           f"WHERE r.kind IN ({marks}) ORDER BY d.name, r.title", *WORK_KINDS)

    def where(self, role_id: str) -> str:
        return where(self.db, role_id)

    # ---- shared actions (humans and agents) ------------------------------
    def create(self, project_id: int, title: str, description: str, by: str, type: str = "task",
               priority: str = "medium", status: str = "backlog", role: str | None = None,
               origin: str = "human") -> dict:
        project = self.pipeline.project(project_id)
        title, description = title.strip(), description.strip()
        if not title:
            raise TicketError("A ticket needs a title.")
        self._check(type=type, priority=priority, status=status)
        if status not in OPEN:
            raise TicketError("File a ticket as backlog or to do.")
        role = self._work_role(role) if role else self.pipeline._fix_role(project_id)
        ts = now()
        tid = self.db.run(
            "INSERT INTO tasks (project_id, key, title, description, role, status, type, priority, origin, reporter, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            project_id, "pending", title, description, role, status, type, priority, origin, by, ts, ts)
        self.db.run("UPDATE tasks SET key=? WHERE id=?", f"t-{tid}", tid)
        note(self.db, tid, by, f"Filed as {type}, {priority} priority, {STATUS_LABELS[status]}, "
                               f"for {self.where(role)}.")
        self.db.log("ticket", f"{by} filed {ticket_key(tid)} for {self.where(role)}: {title}", project_id, actor=by)
        if status == "todo" and origin == "human":
            self._reopen(project, by)
        return self.get(tid)

    def comment(self, ref: int | str, by: str, body: str) -> dict:
        t = self.get(ref)
        if not body.strip():
            raise TicketError("Write a comment first.")
        note(self.db, t["id"], by, body.strip(), kind="comment")
        self.db.log("ticket", f"{by} commented on {t['ticket']}.", t["project_id"], actor=by)
        return self.get(t["id"])

    def transfer(self, ref: int | str, by: str, role: str, reason: str = "", *, limit: int | None = None) -> dict:
        """Move an open ticket to another role or department."""
        t = self.get(ref)
        role = self._work_role(role)
        if t["origin"] == "stage":
            raise TicketError(f"{t['ticket']} is department stage work and moves with the project's approvals.")
        if t["status"] not in OPEN:
            raise TicketError(f"{t['ticket']} is {t['status_label'].lower()}; only backlog or to-do tickets can be "
                              "transferred.")
        if role == t["role"]:
            raise TicketError(f"{t['ticket']} is already with {self.where(role)}.")
        self._record_transfer(t, by, role, reason, limit=limit)
        self.db.run("UPDATE tasks SET role=?, assignee_id=NULL, updated_at=? WHERE id=?", role, now(), t["id"])
        return self.get(t["id"])

    def _record_transfer(self, t: dict, by: str, role: str, reason: str, *, limit: int | None) -> None:
        if limit is not None:
            done = self.db.one("SELECT COUNT(*) AS n FROM ticket_comments WHERE task_id=? AND kind='transfer'",
                               t["id"])["n"]
            if done >= limit:
                raise TicketError(f"{t['ticket']} has already been transferred {done} times. Finish it, or comment "
                                  "and let the CTO decide who owns it.")
        msg = f"Transferred from {self.where(t['role'])} to {self.where(role)}." + (f" {reason.strip()}" if reason.strip() else "")
        note(self.db, t["id"], by, msg, kind="transfer")
        self.db.log("ticket", f"{by}: {t['ticket']} {msg[0].lower()}{msg[1:]}", t["project_id"], actor=by)

    # ---- human actions ---------------------------------------------------
    def update(self, ref: int | str, by: str, *, status: str | None = None, priority: str | None = None,
               title: str | None = None, description: str | None = None, role: str | None = None) -> dict:
        t = self.get(ref)
        self._check(priority=priority, status=status)
        if t["origin"] == "stage" and (status or role or title is not None or description is not None):
            raise TicketError(f"{t['ticket']} is department stage work and moves with the project's approvals. "
                              "You can comment or change its priority.")
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
        if role and role != t["role"]:
            self.transfer(t["id"], by, role)       # validates and records the transfer itself
        if changes:
            for field, value, _ in changes:
                self.db.run(f"UPDATE tasks SET {field}=?, updated_at=? WHERE id=?", value, now(), t["id"])
            note(self.db, t["id"], by, " ".join(msg for *_, msg in changes))
            self.db.log("ticket", f"{by} updated {t['ticket']}: " + " ".join(msg for *_, msg in changes),
                        t["project_id"], actor=by)
        if status == "todo":
            self._reopen(self.pipeline.project(t["project_id"]), by)
        return self.get(t["id"])

    # ---- agent tools -----------------------------------------------------
    def agent_tool(self, agent: dict, name: str, args: dict, project_id: int | None,
                   current: int | None, result, requested_by: str | None = None) -> str:
        """Run a ticket tool for an agent. `requested_by` names the CEO or CTO when the agent acts on their chat request."""
        if not project_id:
            raise TicketError("Tickets belong to a project; this assignment has none.")

        def mine(ref) -> dict:
            if not ref and not current:
                raise TicketError("Say which ticket, e.g. T-12. You are not working on a ticket right now.")
            t = self.get(ref or current)
            if t["project_id"] != project_id:
                raise TicketError(f"{t['ticket']} belongs to another project.")
            return t

        if name == "list_tickets":
            rows = self.search(project_id, args.get("status") or None)
            return "\n".join(f"{t['ticket']} [{t['status_label']}] {t['priority']} {t['type']} · "
                             f"{t['department'] or t['role']} · {t['assignee'] or 'unassigned'} · {t['title']}"
                             for t in rows) or "No tickets."
        if name == "view_ticket":
            t = self.get(mine(args.get("ticket"))["id"])
            lines = [f"{t['ticket']} {t['title']}",
                     f"{t['type']} · {t['priority']} · {t['status_label']} · {self.where(t['role'])} · "
                     f"{t['assignee'] or 'unassigned'} · reported by {t['reporter']}", "", t["description"] or "(no description)", ""]
            lines += [f"[{h['created_at'][11:16]}] {h['author']}: {h['body']}" for h in t["history"][-20:]]
            return "\n".join(lines)
        if name == "comment_ticket":
            t = self.comment(mine(args.get("ticket"))["id"], agent["name"], str(args["body"])[:4000])
            return f"Comment added to {t['ticket']}."
        if name == "create_ticket":
            filed = self.db.one("SELECT COUNT(*) AS total, SUM(status IN ('backlog','todo','in_progress','in_review')) "
                                "AS open FROM tasks WHERE project_id=? AND origin='agent'", project_id)
            stage = self.pipeline.project(project_id)["stage"]
            busy = (filed["open"] or 0) >= AGENT_OPEN_LIMIT or filed["total"] >= AGENT_TOTAL_LIMIT
            status = "todo" if requested_by else "backlog" if busy or stage in REOPEN_STAGES else "todo"
            t = self.create(project_id, str(args["title"])[:200], str(args.get("description", ""))[:4000],
                            agent["name"], type=args.get("type", "task"), priority=args.get("priority", "medium"),
                            status=status, role=args.get("role"), origin="agent")
            if current:
                note(self.db, current, agent["name"], f"Filed {t['ticket']} for {self.where(t['role'])}: {t['title']}")
            if requested_by:                     # the CEO or CTO asked for this in a chat: it is their request
                note(self.db, t["id"], agent["name"], f"Requested by {requested_by} in a chat with {agent['name']}.",
                     kind="comment")
                self._reopen(self.pipeline.project(project_id), requested_by)
            return (f"Filed {t['ticket']} for {self.where(t['role'])} ({t['status_label']})."
                    + (" It waits in the backlog for the CEO or CTO to triage." if status == "backlog" else ""))
        if name == "transfer_ticket":
            t = mine(args.get("ticket"))
            role = self._work_role(args["role"])
            if t["id"] == current:
                if role == t["role"]:
                    raise TicketError(f"{t['ticket']} is already with {self.where(role)}.")
                self._record_transfer(t, agent["name"], role, str(args.get("reason", "")), limit=MAX_TRANSFERS)
                result.transfer = {"role": role, "reason": str(args.get("reason", ""))}
                return (f"{t['ticket']} goes to {self.where(role)} when you finish. Stop working on it now and "
                        "reply with a short summary of what you did.")
            t = self.transfer(t["id"], agent["name"], role, str(args.get("reason", "")), limit=MAX_TRANSFERS)
            return f"{t['ticket']} transferred to {self.where(role)}."
        raise TicketError(f"Unknown ticket tool '{name}'.")

    # ---- helpers ---------------------------------------------------------
    def _work_role(self, role: str) -> str:
        roles = self.work_roles()
        if role not in {r["id"] for r in roles}:
            raise TicketError(f"Role '{role}' cannot own tickets right now. Staffed roles: "
                              + ", ".join(f"{r['id']} ({r['department']})" for r in roles) + ".")
        return role

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
