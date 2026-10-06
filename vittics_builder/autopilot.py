"""Autopilot: how hands-on the CEO and CTO want to be on a project.

  off    Step by step. Every gate waits for you (as before).
  key    Key decisions. You decide the idea, approve the plan and sign off the release; the technical
         sign-off, requirements, design and the CTO's release review are approved automatically.
  final  Final sign-off only. The idea is decided as the assessment recommends (when it recommends
         building) and the plan is approved too; you sign off the release.

Only these gates are ever automatic, and only when their conditions hold. Everything else always waits
for a person: the final sign-off, failures (release blocked, escalations, failed deployments), changed
acceptance checks, budgets, hiring, deployments, strategies and board reviews. A release review is only
ever created after the audits, acceptance checks and executable verification have all passed.
"""
from __future__ import annotations

import json

LEVELS = {"off": "Step by step", "key": "Key decisions", "final": "Final sign-off only"}
AUTO = {"key": {"feasibility", "prd", "architecture", "release"},
        "final": {"feasibility", "idea_decision", "plan", "prd", "architecture", "release"}}
CHOICE = {"build_internal": "internal", "build_to_sell": "commercial"}


def name_from(brief: str) -> str:
    """A project name from a brief: 'Build me a customer support SaaS with login...' -> 'Customer Support SaaS'."""
    import re
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'-]*", brief)
    skip = {"build", "me", "a", "an", "the", "make", "create", "i", "want", "need", "please", "us", "my", "our"}
    stop = {"with", "and", "that", "which", "where", "using", "to", "so", "who", "in", "on", "from", "by", "via", "for"}
    picked: list[str] = []
    for w in words:
        if w.lower() in stop and picked:
            break
        if w.lower() not in skip and w.lower() not in stop:
            picked.append(w)
        if len(picked) == 4:
            break
    picked = picked or ["New", "product"]
    keep = lambda w: w.isupper() or any(c.isupper() for c in w[1:])         # SaaS, API, iOS stay as written
    return " ".join(w if keep(w) else w.capitalize() for w in picked)[:40]


class Autopilot:
    def __init__(self, pipeline) -> None:
        self.pipeline, self.db = pipeline, pipeline.db

    def level(self, pid: int) -> str:
        row = self.db.one("SELECT autopilot FROM projects WHERE id=?", pid)
        return row["autopilot"] if row and row["autopilot"] in LEVELS else "off"

    def set(self, pid: int, level: str, by: str) -> None:
        if level not in LEVELS:
            raise ValueError(f"Autopilot is one of: {', '.join(LEVELS)}.")
        self.pipeline.project(pid)
        self.db.run("UPDATE projects SET autopilot=? WHERE id=?", level, pid)
        self.db.log("autopilot", f"{by} set autopilot to {LEVELS[level]}.", pid, actor=by)

    def _verdict(self, approval: dict) -> tuple[bool, str, str]:
        """(may decide, choice, reason) for one pending approval."""
        payload = json.loads(approval["payload"] or "{}")
        kind = approval["kind"]
        if kind in ("feasibility", "idea_decision"):
            a = payload.get("assessment") or {}
            if a.get("feasibility") == "not_achievable":
                return False, "", "the assessment says it is not achievable"
            if kind == "idea_decision":
                choice = CHOICE.get(a.get("recommendation"))
                if not choice:
                    return False, "", "the assessment does not recommend building it"
                return True, choice, f"the assessment recommends building it ({choice})"
            return True, "", "the technical assessment finds it achievable"
        if kind == "release":
            return True, "", "audits, acceptance checks and executable verification all passed"
        return True, "", "within the approved plan"

    def step(self, pid: int) -> bool:
        """Decide one pending gate this project's level allows. True if it decided one."""
        allowed = AUTO.get(self.level(pid), set())
        if not allowed:
            return False
        for approval in self.db.all("SELECT * FROM approvals WHERE project_id=? AND status='pending' ORDER BY id", pid):
            if approval["kind"] not in allowed:
                continue
            ok, choice, reason = self._verdict(approval)
            if not ok:
                note = f"Left for you: {approval['title']} ({reason})."
                if not self.db.one("SELECT 1 FROM events WHERE project_id=? AND kind='autopilot' AND message=?", pid, note):
                    self.db.log("autopilot", note, pid, actor="Autopilot")
                continue
            self.pipeline.decide(approval["id"], approval["required_role"], "approved",
                                 f"Approved by autopilot: {reason}.", choice=choice, autopilot=True)
            return True
        return False
