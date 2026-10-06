"""Performance reviews and the HR policy built on them.

Every evaluation (peer review, QA check, a human approval or rejection, a
manual rating) is a score from 0 to 100. An agent's standing is a rolling
average that weights recent work most. The policy in org.yaml turns that
number into probation, replacement, recovery, or reinstating a predecessor.
"""
from __future__ import annotations

import json

from .config import Settings
from .db import DB, now
from .org import Org


class Performance:
    def __init__(self, db: DB, settings: Settings, org: Org) -> None:
        self.db, self.s, self.org = db, settings, org

    def record(self, agent_id: int, score: float, *, source: str, reviewer: str, notes: str = "",
               task_id: int | None = None, project_id: int | None = None) -> float:
        """Store one evaluation and return the agent's new rolling score."""
        score = max(0.0, min(100.0, float(score)))
        agent = self.org.agent(agent_id)
        self.db.run(
            "INSERT INTO reviews (agent_id, task_id, project_id, source, reviewer, score, notes, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            agent_id, task_id, project_id, source, reviewer, score, notes, now())
        alpha = self.s.hr.alpha
        rolling = score if agent["score"] is None else (1 - alpha) * agent["score"] + alpha * score
        self.db.run("UPDATE agents SET score=?, evals=evals+1 WHERE id=?", round(rolling, 1), agent_id)
        return rolling

    def evaluate(self, agent_id: int) -> str | None:
        """Apply HR policy to one agent. Returns what happened, if anything:
        'probation', 'recovered', 'replace', 'rehire', or None."""
        a = self.org.agent(agent_id)
        hr = self.s.hr
        if a["status"] == "fired" or a["score"] is None or a["evals"] < hr.min_tasks:
            return None

        # A replacement who does worse than the agent they replaced: bring the predecessor back.
        if a["predecessor_id"]:
            pred = self.db.one("SELECT * FROM agents WHERE id=?", a["predecessor_id"])
            if pred and pred["status"] == "fired" and pred["score"] is not None \
                    and a["score"] < pred["score"] - hr.rehire_margin:
                return self._act("rehire", a, f"{a['name']} scores {a['score']:.0f}, below predecessor "
                                              f"{pred['name']}'s {pred['score']:.0f}. Reinstate {pred['name']}.",
                                 target=pred)

        if a["score"] < hr.fire_below:
            return self._act("replace", a, f"Rolling score {a['score']:.0f} is below the floor of {hr.fire_below:.0f}.")

        if a["status"] == "active" and a["score"] < hr.probation_below:
            coaching = self._coaching(a)
            self.db.run("UPDATE agents SET status='probation', probation_at=?, lessons=? WHERE id=?",
                        a["evals"], "\n\n".join(x for x in (a["lessons"], coaching) if x), a["id"])
            self.db.log("probation", f"{a['name']} placed on probation (score {a['score']:.0f}).", actor="hr")
            return "probation"

        if a["status"] == "probation":
            if a["score"] >= hr.probation_below + hr.recovery_margin:
                self.db.run("UPDATE agents SET status='active', probation_at=NULL WHERE id=?", a["id"])
                self.db.log("probation", f"{a['name']} recovered (score {a['score']:.0f}) and is off probation.", actor="hr")
                return "recovered"
            if a["evals"] - (a["probation_at"] or 0) >= hr.probation_tasks:
                return self._act("replace", a, f"Still at {a['score']:.0f} after {hr.probation_tasks} "
                                               "evaluation(s) on probation.")
        return None

    def _coaching(self, agent: dict) -> str:
        notes = self.db.all("SELECT notes FROM reviews WHERE agent_id=? AND notes!='' ORDER BY score ASC, id DESC LIMIT 3",
                            agent["id"])
        if not notes:
            return ""
        return ("You are on probation. Recent reviews raised these problems; fix them in your next work:\n"
                + "\n".join(f"- {n['notes'].strip()[:400]}" for n in notes))

    def _act(self, action: str, agent: dict, reason: str, target: dict | None = None) -> str | None:
        """Carry out an HR action, or ask the responsible human first."""
        if self.s.hr.auto_fire:
            self.execute(action, agent["id"], reason, by="hr")
            return action
        pending = self.db.all("SELECT payload FROM approvals WHERE kind='hr' AND status='pending'")
        if any(json.loads(p["payload"]).get("agent_id") == agent["id"] for p in pending):
            return None
        boss = self.org.boss_of(agent)
        title = (f"Reinstate {target['name']} in place of {agent['name']}" if action == "rehire"
                 else f"Replace {agent['name']} ({agent['role'].replace('_', ' ')})")
        recent = self.db.all("SELECT score, source, notes FROM reviews WHERE agent_id=? ORDER BY id DESC LIMIT 5", agent["id"])
        summary = reason + "\n\nRecent evaluations:\n" + "\n".join(
            f"- {r['score']:.0f} ({r['source']}): {r['notes'][:200]}" for r in recent)
        self.db.run(
            "INSERT INTO approvals (kind, required_role, title, summary, payload, created_at) VALUES ('hr',?,?,?,?,?)",
            boss, title, summary, json.dumps({"action": action, "agent_id": agent["id"], "reason": reason}), now())
        self.db.log("hr", f"{title}: waiting for the {boss.upper()}.", actor="hr")
        return action

    def execute(self, action: str, agent_id: int, reason: str, by: str) -> dict:
        agent = self.org.agent(agent_id)
        if action == "rehire":
            return self.org.rehire(agent["predecessor_id"], by=by)
        return self.org.replace(agent_id, reason, by=by)

    def keep(self, agent_id: int) -> None:
        """A human declined to replace the agent: restart their probation clock."""
        agent = self.org.agent(agent_id)
        if agent["status"] != "fired":
            self.db.run("UPDATE agents SET status='probation', probation_at=? WHERE id=?", agent["evals"], agent_id)
