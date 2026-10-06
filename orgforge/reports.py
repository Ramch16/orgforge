"""Status reports for the CEO and CTO.

The product manager writes a short report per project: done, in progress, blocked or waiting
on a decision, cost against budget, and next steps. Reports are written on request and at
milestones (build starts, release ready, release blocked, signed off, paused for budget). The
facts are gathered here, so a report is accurate even when no product manager is available.
"""
from __future__ import annotations

from .costs import spent
from .pipeline import STAGE_LABELS

NEXT_STEP = {
    "idea": "Ram finishes the assessment.", "idea_review": "Lucky signs off the technical assessment.",
    "idea_decision": "Niki decides whether to build it.", "plan": "Ram writes the plan of action.",
    "plan_approval": "Niki and Lucky approve the plan.", "prd": "Product writes the requirements.",
    "prd_approval": "Niki approves the requirements.", "architecture": "Engineering designs the system.",
    "architecture_approval": "Lucky approves the design and plan.", "build": "The team builds and reviews tickets.",
    "escalation": "Lucky decides on the escalated ticket.", "contract_review": "Lucky reviews the changed checks.",
    "release_blocked": "Lucky sends repair guidance.", "release_approval": "Lucky approves the release.",
    "signoff": "Niki signs off.", "done": "Ready for deployment; feedback can start the next version.",
    "parked": "Waiting until Niki revisits it.", "dropped": "Nothing; it was dropped.",
    "paused": "Niki sets a new budget to resume.",
    "deploying": "The release is deployed and health-checked.",
    "deploy_approval": "The go-ahead to deploy to the next environment.",
    "deploy_failed": "Lucky retries the deployment or sends it back with guidance.",
    "live": "Live; production checks and feedback can start the next version.",
}
READ_ONLY = {"read_file", "list_files", "list_tickets", "view_ticket"}


class Reports:
    def __init__(self, company) -> None:
        self.co = company
        self.db = company.db

    def request(self, pid: int, by: str) -> None:
        """Ask for a report now; it is written the next time the project is worked on (straight away)."""
        self.co.pipeline.project(pid)
        self.co.pipeline.queue_report(pid, "Requested", by)

    def latest(self, pid: int, limit: int = 10) -> list[dict]:
        return self.db.all("SELECT * FROM reports WHERE project_id=? ORDER BY id DESC LIMIT ?", pid, limit)

    def facts(self, pid: int) -> str:
        p = self.co.pipeline.project(pid)
        tickets = self.db.all("SELECT t.*, a.name AS who FROM tasks t LEFT JOIN agents a ON a.id=t.assignee_id "
                              "WHERE t.project_id=? ORDER BY t.id", pid)
        count = {s: sum(t["status"] == s for t in tickets) for s in
                 ("done", "in_progress", "in_review", "todo", "backlog", "failed", "cancelled")}
        open_ = [t for t in tickets if t["status"] in ("todo", "in_progress", "in_review", "failed")][:12]
        waiting = self.db.all("SELECT title, required_role FROM approvals WHERE project_id=? AND status='pending'", pid)
        events = self.db.all("SELECT actor, message FROM events WHERE project_id=? ORDER BY id DESC LIMIT 8", pid)[::-1]
        used, budget = spent(self.db, pid), p["budget"]
        lines = [f"Project: {p['name']}" + (f" (version {p['version']})" if p["version"] else ""),
                 f"Stage: {STAGE_LABELS.get(p['stage'], p['stage'])}",
                 "Purpose: " + {"internal": "internal use", "commercial": "to sell"}.get(p["purpose"], "not set"),
                 "Tickets: " + ", ".join(f"{n} {s.replace('_', ' ')}" for s, n in count.items() if n),
                 f"Cost so far: ${used:.2f}" + (f" of a ${budget:.2f} budget" if budget else " (no budget limit)")]
        if open_:
            lines.append("Open tickets:\n" + "\n".join(f"- T-{t['id']} {t['title']} [{t['status']}]"
                                                      + (f" with {t['who']}" if t["who"] else "") for t in open_))
        lines.append("Waiting on a decision:\n" + ("\n".join(f"- {a['title']} ({a['required_role'].upper()})"
                                                            for a in waiting) or "- nothing"))
        if events:
            lines.append("Recent activity:\n" + "\n".join(f"- {e['actor']}: {e['message']}" for e in events))
        lines.append(f"Next: {NEXT_STEP.get(p['stage'], '')}")
        return "\n".join(lines)

    def write(self, report: dict) -> None:
        """Write one pending report (called by the pipeline)."""
        pid = report["project_id"]
        facts = self.facts(pid)
        pm, body = self.co.org.pick(kind="product"), ""
        if pm:
            try:
                p = self.co.pipeline.project(pid)
                ask = (f"Write a short status report on \"{p['name']}\" for {self.co.s.ceo_name} (CEO) and "
                       f"{self.co.s.cto_name} (CTO). Reason: {report['trigger']}.\n\nFacts:\n{facts}\n\n"
                       "Use these headings: Done, In progress, Blocked or needs you, Cost, Next steps. Under 200 "
                       "words, plain language, no jargon. Only state what the facts or the tickets show.")
                res = self.co.runtime.run(pm, ask, self.co.pipeline.workspace(p), project_id=pid, only_tools=READ_ONLY,
                                          meta={"purpose": "status_report", "facts": facts})
                body = res.text.strip() if res.completed else ""
            except Exception as exc:              # fall back to the facts rather than lose the report
                self.db.log("warn", f"Status report fell back to plain facts: {exc}", pid)
        author = pm["name"] if pm and body else "OrgForge"
        self.db.run("UPDATE reports SET status='sent', author=?, body=? WHERE id=?", author,
                    body or facts, report["id"])
        self.db.log("report", f"Status report ({report['trigger'].lower()}) by {author}.", pid, actor=author)
