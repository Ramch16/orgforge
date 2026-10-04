"""Customer feedback after release, triaged by Support.

The CEO or CTO pastes feedback (an email, a call note, a review, a bug report). The support
specialist reads it with the product's files and tickets, checks for an existing ticket,
and files new ones: bugs go straight to To do (a finished product reopens for its next
version), feature requests wait in the backlog for the CEO and CTO to prioritise, and
questions get a suggested reply instead of a ticket.
"""
from __future__ import annotations

import json
import threading

from .db import now

TRIAGE_TOOLS = {"read_file", "list_files", "list_tickets", "view_ticket", "create_ticket", "comment_ticket"}


class FeedbackError(ValueError):
    pass


class Feedback:
    def __init__(self, company) -> None:
        self.co = company
        self.db = company.db
        self.on_work = None      # called with a project id when triage files work (the server starts the team)

    def list(self, pid: int) -> list[dict]:
        rows = self.db.all("SELECT * FROM feedback WHERE project_id=? ORDER BY id DESC", pid)
        return [{**r, "tickets": json.loads(r["tickets"])} for r in rows]

    def submit(self, pid: int, body: str, by: str, source: str = "", wait: bool = False) -> dict:
        project = self.co.pipeline.project(pid)
        if not body.strip():
            raise FeedbackError("Paste the feedback first.")
        if project["stage"] in ("idea", "idea_review", "idea_decision", "plan", "plan_approval", "dropped"):
            raise FeedbackError(f"{project['name']} has not been built yet, so there is no product to give feedback on.")
        fid = self.db.run("INSERT INTO feedback (project_id, source, body, submitted_by, created_at) VALUES (?,?,?,?,?)",
                          pid, source.strip()[:200], body.strip()[:8000], by, now())
        if wait:
            self._triage(fid)
        else:
            threading.Thread(target=self._triage, args=(fid,), daemon=True).start()
        return self.db.one("SELECT * FROM feedback WHERE id=?", fid)

    def _agent(self) -> dict | None:
        return self.co.org.pick(role="support_specialist") or self.co.org.pick(kind="product")

    def _triage(self, fid: int) -> None:
        f = self.db.one("SELECT * FROM feedback WHERE id=?", fid)
        p = self.co.pipeline.project(f["project_id"])
        agent = self._agent()
        if not agent:
            self.db.run("UPDATE feedback SET status='failed', result=? WHERE id=?",
                        "Nobody can triage feedback. Hire a support specialist.", fid)
            return
        ask = (f"Customer feedback on \"{p['name']}\"" + (f" (from: {f['source']})" if f["source"] else "")
               + f", passed on by {f['submitted_by']}:\n\n{f['body']}\n\n"
               "Triage it. Read the product's README and docs and check list_tickets for an existing ticket first. "
               "Then: for a bug, file a bug ticket for the role that should fix it, with steps to reproduce and the "
               "expected behaviour; for a feature request, file a story; for a question, file nothing and write a "
               "short suggested reply to the customer. If an existing ticket already covers it, comment on that "
               "ticket instead. Finish with a two- or three-line summary of what you did.")
        try:
            res = self.co.runtime.run(agent, ask, self.co.pipeline.workspace(p), project_id=p["id"],
                                      only_tools=TRIAGE_TOOLS,
                                      meta={"purpose": "triage", "feedback": f["body"], "feedback_id": fid,
                                            "requested_by": f["submitted_by"]})
            status, text, filed = ("triaged" if res.completed else "failed"), res.text.strip(), res.filed
        except Exception as exc:
            status, text, filed = "failed", f"Could not triage: {type(exc).__name__}: {exc}", []
        self.db.run("UPDATE feedback SET status=?, result=?, tickets=? WHERE id=?", status,
                    f"{agent['name']}: {text}" if status == "triaged" else text, json.dumps(filed), fid)
        self.db.log("feedback", f"{agent['name']} triaged customer feedback"
                    + (f": filed {', '.join(filed)}." if filed else "."), p["id"], actor=agent["name"])
        if filed and self.on_work:
            self.on_work(p["id"])
