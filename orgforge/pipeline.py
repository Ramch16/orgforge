"""How a brief becomes a product.

    brief -> requirements + UX design -> CEO approves -> architecture + plan -> CTO approves
          -> build (each task: implement, then every reviewer and QA role checks it;
             rework or escalate)
          -> release check + audits (security, performance, compliance; findings
             become fix tasks) -> CTO approves release -> CEO signs off -> done

The pipeline is a resumable state machine. `advance()` runs until it needs a
human decision; `decide()` records the decision and moves the stage on.
"""
from __future__ import annotations

import difflib
import json
import re
import threading
from pathlib import Path

from .agent import AgentRuntime
from .config import Settings
from .db import DB, now
from .org import Org
from .performance import Performance
from .tickets import PRIORITY_ORDER, note, ticket_key, where
from .tools import Workspace
from .validation import CONTRACT, CONTRACT_GUIDE, validate_plan, verify_product

STAGES = ["prd", "prd_approval", "architecture", "architecture_approval", "build",
          "escalation", "contract_review", "release_blocked", "release_approval", "signoff", "done"]
STAGE_LABELS = {
    "contract_review": "Acceptance checks changed, with CTO",
    "release_blocked": "Release checks need attention",
    "prd": "Writing requirements", "prd_approval": "Requirements with CEO",
    "architecture": "Designing", "architecture_approval": "Design with CTO",
    "build": "Building", "escalation": "Escalated to CTO",
    "release_approval": "Release with CTO", "signoff": "Sign-off with CEO", "done": "Ready for deployment",
}
HUMAN_APPROVED, HUMAN_REJECTED = 92, 35


class PipelineError(Exception):
    pass


class Pipeline:
    def __init__(self, db: DB, settings: Settings, org: Org, perf: Performance, runtime: AgentRuntime) -> None:
        self.db, self.s, self.org, self.perf, self.runtime = db, settings, org, perf, runtime
        self._locks: dict[int, threading.Lock] = {}

    # ---- helpers ---------------------------------------------------------
    def project(self, pid: int) -> dict:
        p = self.db.one("SELECT * FROM projects WHERE id=?", pid)
        if not p:
            raise PipelineError(f"No project {pid}.")
        return p

    def workspace(self, project: dict) -> Workspace:
        return Workspace(project["workspace"], mode=self.s.sandbox_mode, docker_image=self.s.docker_image,
                         docker_network=self.s.docker_network, timeout=self.s.command_timeout)

    def _stage(self, pid: int, stage: str, feedback: str | None = None) -> None:
        self.db.run("UPDATE projects SET stage=?, updated_at=? WHERE id=?", stage, now(), pid)
        if feedback is not None:
            self.db.run("UPDATE projects SET feedback=? WHERE id=?", feedback, pid)

    def _approval(self, pid: int | None, kind: str, role: str, title: str, summary: str, payload: dict) -> int:
        self.db.log("approval", f"Waiting for the {role.upper()}: {title}", pid)
        return self.db.run(
            "INSERT INTO approvals (project_id, kind, required_role, title, summary, payload, created_at) "
            "VALUES (?,?,?,?,?,?,?)", pid, kind, role, title, summary[:6000], json.dumps(payload), now())

    def _stage_ticket(self, pid: int, key: str, title: str, agent: dict, description: str,
                      reporter: str = "OrgForge", handoff: str = "") -> int:
        """Open (or reopen) the ticket for one department's stage work and assign it."""
        t, ts = self.db.one("SELECT * FROM tasks WHERE project_id=? AND key=?", pid, key), now()
        if t:
            self.db.run("UPDATE tasks SET status='in_progress', role=?, assignee_id=?, updated_at=? WHERE id=?",
                        agent["role"], agent["id"], ts, t["id"])
            note(self.db, t["id"], agent["name"], "Started again.")
            return t["id"]
        tid = self.db.run(
            "INSERT INTO tasks (project_id, key, title, description, role, assignee_id, status, type, priority, origin, "
            "reporter, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", pid, key, title, description,
            agent["role"], agent["id"], "in_progress", "story", "high", "stage", reporter, ts, ts)
        note(self.db, tid, reporter, (f"Handed over from {handoff} to " if handoff else "Assigned to ")
             + f"{agent['name']}, {where(self.db, agent['role'])}.", kind="handoff" if handoff else "change")
        return tid

    def _stage_status(self, pid: int, keys: str, status: str, author: str, body: str, kind: str = "change",
                      only: tuple[str, ...] = ()) -> None:
        """Move this project's stage tickets matching a key pattern and record why."""
        sql = "SELECT id FROM tasks WHERE project_id=? AND origin='stage' AND key LIKE ?"
        if only:
            sql += f" AND status IN ({','.join('?' * len(only))})"
        for t in self.db.all(sql, pid, keys, *only):
            self.db.run("UPDATE tasks SET status=?, updated_at=? WHERE id=?", status, now(), t["id"])
            note(self.db, t["id"], author, body, kind=kind)

    @staticmethod
    def _read(ws: Workspace, path: str) -> str:
        target = ws.resolve(path)
        return target.read_text(errors="replace") if target.is_file() else ""

    # ---- projects --------------------------------------------------------
    def create_project(self, name: str, brief: str, by: str = "ceo") -> dict:
        ts = now()
        pid = self.db.run("INSERT INTO projects (name, brief, created_at, updated_at) VALUES (?,?,?,?)",
                          name, brief, ts, ts)
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "project"
        path = self.s.workspaces / f"{pid:03d}-{slug}"
        self.db.run("UPDATE projects SET workspace=? WHERE id=?", str(path), pid)
        ws = Workspace(path)
        ws.init_repo()
        ws.write_file("BRIEF.md", f"# {name}\n\n{brief}\n")
        ws.commit("Brief from the CEO")
        self.db.log("project", f"New project: {name}", pid, actor=by)
        return self.project(pid)

    def advance(self, pid: int) -> dict:
        """Run the project forward until it needs a human or is finished."""
        lock = self._locks.setdefault(pid, threading.Lock())
        if not lock.acquire(blocking=False):
            return self.project(pid)          # already running in another thread
        try:
            self.db.run("UPDATE tasks SET status='todo' WHERE project_id=? AND status IN ('in_progress', 'in_review') AND origin!='stage'", pid)
            steps = {"prd": self._prd, "architecture": self._architecture, "build": self._build}
            while (stage := self.project(pid)["stage"]) in steps:
                steps[stage](self.project(pid))
        except Exception as exc:
            self.db.log("error", f"{type(exc).__name__}: {exc}", pid)
            raise
        finally:
            lock.release()
        return self.project(pid)

    # ---- stages ----------------------------------------------------------
    def _prd(self, p: dict) -> None:
        ws = self.workspace(p)
        pm = self.org.pick(kind="product")
        if not pm:                             # no product staff: the brief is the requirements
            ws.write_file("docs/PRD.md", f"# {p['name']}\n\n{p['brief']}\n")
            ws.commit("Requirements (CEO brief)")
            self._stage(p["id"], "architecture", "")
            return
        ask = (f"The CEO's brief for a new product, \"{p['name']}\":\n\n{p['brief']}\n\n"
               "Write the requirements document to docs/PRD.md. Include numbered, testable acceptance criteria, "
               "main user journeys, error cases, data persistence, required integrations, and explicit exclusions.")
        if p["feedback"]:
            ask += f"\n\nThe CEO sent the previous version back. Revise docs/PRD.md to address this:\n{p['feedback']}"
        self.db.log("work", f"{pm['name']} is writing the requirements.", p["id"], actor=pm["name"])
        prd_ticket = self._stage_ticket(p["id"], "stage-requirements", f"Requirements for {p['name']}", pm,
                                        p["brief"], reporter=self.s.ceo_name)
        res = self.runtime.run(pm, ask, ws, project_id=p["id"], meta={"ticket_id": prd_ticket})
        if not self._read(ws, "docs/PRD.md"):
            ws.write_file("docs/PRD.md", res.text or p["brief"])
        ws.commit(f"Requirements ({pm['name']})")
        note(self.db, prd_ticket, pm["name"], "Requirements written to docs/PRD.md.\n" + (res.text or "")[:1500])
        authors = [pm["id"]]
        for role in self.org.staffed_roles("designer"):
            designer = self.org.pick(role=role["id"])
            ask = (f"Project \"{p['name']}\". Read docs/PRD.md, then write the design to docs/DESIGN.md.")
            if p["feedback"]:
                ask += f"\n\nThe CEO sent the previous version back. Address this where it concerns the design:\n{p['feedback']}"
            self.db.log("work", f"{designer['name']} is designing the user experience.", p["id"], actor=designer["name"])
            note(self.db, prd_ticket, pm["name"], f"Handed to {designer['name']}, {where(self.db, role['id'])}, "
                 "for the user experience design.", kind="handoff")
            design_ticket = self._stage_ticket(p["id"], f"stage-design-{role['id']}", f"UX design for {p['name']}",
                                               designer, "Design the user experience from docs/PRD.md into docs/DESIGN.md.",
                                               reporter=pm["name"], handoff=where(self.db, pm["role"]))
            design = self.runtime.run(designer, ask, ws, project_id=p["id"], meta={"ticket_id": design_ticket})
            ws.commit(f"UX design ({designer['name']})")
            note(self.db, design_ticket, designer["name"], "Design written to docs/DESIGN.md.\n" + (design.text or "")[:1500])
            authors.append(designer["id"])
        self._stage_status(p["id"], "stage-requirements", "in_review", pm["name"], f"Submitted to {self._boss('ceo')} for approval.", kind="handoff")
        self._stage_status(p["id"], "stage-design-%", "in_review", "OrgForge", f"Submitted to {self._boss('ceo')} with the requirements.", kind="handoff", only=("in_progress",))
        summary = self._read(ws, "docs/PRD.md")
        if design := self._read(ws, "docs/DESIGN.md"):
            summary += f"\n\n----- docs/DESIGN.md -----\n{design}"
        self._approval(p["id"], "prd", "ceo", f"Requirements for {p['name']}", summary,
                       {"agent_ids": authors, "file": "docs/PRD.md"})
        self._stage(p["id"], "prd_approval", "")

    def _builder_roles(self) -> list[dict]:
        ids = sorted({a["role"] for a in self.org.staff(kind="builder")})
        return [self.org.role(r) for r in ids]

    def _architecture(self, p: dict) -> None:
        ws = self.workspace(p)
        architect = self.org.pick(kind="planner")
        roles = self._builder_roles()
        if not architect:
            raise PipelineError("Nobody on staff can plan the work. Hire a role of kind 'planner' (e.g. architect).")
        if not roles:
            raise PipelineError("Nobody on staff can build. Hire at least one role of kind 'builder'.")
        role_list = "\n".join(f"- {r['id']}: {r['title']}" for r in roles)
        ask = (f"Project \"{p['name']}\". Read docs/PRD.md (and docs/DESIGN.md if it exists), then write the "
               "technical design to docs/ARCHITECTURE.md.\n"
               "Finish by calling submit_plan with the build tasks. Assign each task to one of these roles:\n"
               f"{role_list}\nUse only the roles this product needs. Where the team has them, include tasks for "
               "run/deploy setup, automated tests and user documentation.\n" + CONTRACT_GUIDE)
        if p["feedback"]:
            ask += f"\n\nThe CTO sent the previous design back. Address this:\n{p['feedback']}"
        arch_ticket = self._stage_ticket(p["id"], "stage-architecture", f"Architecture and build plan for {p['name']}",
                                         architect, "Design the system in docs/ARCHITECTURE.md, write product.json and "
                                         "submit the build plan.", reporter=self.s.cto_name,
                                         handoff="Product and Design (approved requirements)")
        meta = {"builder_roles": [r["id"] for r in roles], "ticket_id": arch_ticket}
        self.db.log("work", f"{architect['name']} is designing the system.", p["id"], actor=architect["name"])
        res = self.runtime.run(architect, ask, ws, project_id=p["id"], meta=meta)
        if not res.plan:
            res = self.runtime.run(architect, "The design is in docs/ARCHITECTURE.md but no plan was submitted. "
                                   f"Read it and call submit_plan now. Roles:\n{role_list}", ws,
                                   project_id=p["id"], meta=meta)
        if not res.plan:
            raise PipelineError(f"{architect['name']} did not submit a build plan. Run the project again to retry.")

        try:
            validate_plan(res.plan, {r["id"] for r in roles})
        except ValueError as exc:
            raise PipelineError(str(exc)) from exc
        self.db.run("DELETE FROM ticket_comments WHERE task_id IN (SELECT id FROM tasks WHERE project_id=? "
                    "AND status!='done' AND origin IN ('plan', 'pipeline'))", p["id"])
        self.db.run("DELETE FROM tasks WHERE project_id=? AND status!='done' AND origin IN ('plan', 'pipeline')", p["id"])
        valid, keys = {r["id"] for r in roles}, set()
        for i, t in enumerate(res.plan, 1):
            key = re.sub(r"[^a-z0-9_-]+", "-", str(t.get("key") or f"task-{i}").lower()).strip("-") or f"task-{i}"
            while key in keys:
                key += "-x"
            keys.add(key)
            t["key"] = key
        for t in res.plan:
            deps = [d for d in (t.get("depends_on") or []) if d in keys and d != t["key"]]
            role = t.get("role") if t.get("role") in valid else roles[0]["id"]
            ts = now()
            tid = self.db.run(
                "INSERT INTO tasks (project_id, key, title, description, role, depends_on, status, reporter, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", p["id"], t["key"], t.get("title", t["key"]),
                t.get("description", ""), role, json.dumps(deps), "todo", architect["name"], ts, ts)
            note(self.db, tid, architect["name"], f"Created from the build plan for {where(self.db, role)}."
                 + (f" Depends on: {', '.join(deps)}." if deps else ""))
        ws.commit(f"Architecture and plan ({architect['name']})")
        built = self.db.all("SELECT id FROM tasks WHERE project_id=? AND origin='plan' AND status='todo' ORDER BY id", p["id"])
        self._stage_status(p["id"], "stage-architecture", "in_review", architect["name"],
                           f"Plan submitted: {len(built)} build ticket(s) for Engineering "
                           f"({', '.join(ticket_key(t['id']) for t in built)}). Sent to {self._boss('cto')} for approval.",
                           kind="handoff")
        plan_text = "\n".join(f"- [{t['key']}] {t.get('title', '')} ({t.get('role', '')})" for t in res.plan)
        self._approval(p["id"], "architecture", "cto", f"Design and plan for {p['name']}",
                       f"Plan ({len(res.plan)} tasks):\n{plan_text}\n\n"
                       f"Acceptance checks ({CONTRACT}):\n{self._read(ws, CONTRACT) or '(none written)'}\n\n"
                       f"{self._read(ws, 'docs/ARCHITECTURE.md')}",
                       {"agent_ids": [architect["id"]], "file": "docs/ARCHITECTURE.md",
                        "contract": self._read(ws, CONTRACT) or None})
        self._stage(p["id"], "architecture_approval", "")

    def _next_task(self, pid: int) -> dict | None:
        tasks = self.db.all(f"SELECT * FROM tasks WHERE project_id=? AND origin!='stage' ORDER BY {PRIORITY_ORDER}, id", pid)
        done = {t["key"] for t in tasks if t["status"] in ("done", "cancelled")}
        for t in tasks:
            if t["status"] == "todo" and all(d in done for d in json.loads(t["depends_on"])):
                return t
        return None

    def _build(self, p: dict) -> None:
        ws = self.workspace(p)
        while task := self._next_task(p["id"]):
            if not self._run_task(p, task, ws):
                task = self.db.one("SELECT * FROM tasks WHERE id=?", task["id"])
                self._approval(p["id"], "escalation", "cto",
                               f"Task \"{task['title']}\" keeps failing review",
                               f"Sent back {task['attempts']} time(s). Latest findings:\n{task['feedback']}\n\n"
                               "Approve to accept the work as it stands. Reject with guidance to have it redone.",
                               {"task_id": task["id"]})
                self._stage(p["id"], "escalation")
                return
        stuck = self.db.all("SELECT key FROM tasks WHERE project_id=? AND origin!='stage' "
                            "AND status NOT IN ('done', 'cancelled', 'backlog')", p["id"])
        if stuck:
            raise PipelineError("Tasks cannot start because their dependencies never finish: "
                                + ", ".join(t["key"] for t in stuck))
        release_findings = []
        qa = self.org.pick(kind="qa")
        if qa:
            self.db.log("work", f"{qa['name']} is running the release check.", p["id"], actor=qa["name"])
            qa_ticket = self._stage_ticket(p["id"], "stage-release-qa", f"Release QA for {p['name']}", qa,
                                           "Check the whole product against docs/PRD.md and report in docs/QA_REPORT.md.",
                                           handoff="Engineering (all build tickets done)")
            integration = self.runtime.run(qa, "All build tasks are complete. Check the product as a whole against docs/PRD.md: "
                             "install it, run the full test suite and try the main flows. Write what you ran and "
                             "what you found to docs/QA_REPORT.md, including anything that does not work. "
                             "Finish by calling submit_review; request_changes if any required flow fails.",
                             ws, project_id=p["id"], meta={"purpose": "integration", "ticket_id": qa_ticket})
            ws.commit(f"Release check ({qa['name']})")
            if not integration.completed or not integration.review or integration.review["verdict"] != "approve" or not self._read(ws, "docs/QA_REPORT.md"):
                release_findings.append("Integration QA: " + ((integration.review or {}).get("notes") or "No completed approval verdict."))
            self._stage_status(p["id"], "stage-release-qa", "done", qa["name"], self._verdict(integration.review)
                               + ("" if not release_findings else " Repair work goes back to Engineering."), kind="comment")
        else:
            release_findings.append("No QA agent is staffed to validate the full product.")
        if release_findings:
            self._release_failure(p, release_findings)
            return
        open_findings = self._audit(p, ws)
        if open_findings is None:
            return
        if open_findings:
            self._release_failure(p, ["UNRESOLVED AUDIT FINDINGS", *open_findings])
            return
        if self._next_task(p["id"]):
            self.db.log("work", "The team filed new tickets during the release checks; building those first.", p["id"])
            return
        if not self._contract_unchanged(p, ws):
            return
        verification = verify_product(ws)
        ws.commit("Executable product verification")
        if not verification["passed"]:
            release_findings.extend(verification["errors"])
            release_findings.extend(f"Check {c['id']}: {c['output']}" for c in verification["checks"] if not c["passed"])
            self._release_failure(p, release_findings)
            return
        report = self._read(ws, "docs/QA_REPORT.md") or "No QA report was produced."
        audits = sorted((ws.root / "docs" / "audits").glob("*.md")) if (ws.root / "docs" / "audits").is_dir() else []
        report += "\n\nExecutable acceptance checks passed. Evidence: docs/VERIFICATION.json"
        if audits:
            report += "\n\nAudit reports: " + ", ".join(f"docs/audits/{a.name}" for a in audits)
        self._approval(p["id"], "release", "cto", f"Release {p['name']}", report, {"verified_head": ws.git("rev-parse", "HEAD")})
        self._stage(p["id"], "release_approval")

    def _audit(self, p: dict, ws: Workspace) -> list[str] | None:
        """Run every staffed auditor role over the finished product. Returns None
        if fix tasks were raised (build continues), else the findings still open."""
        pid, findings = p["id"], []
        for role in self.org.staffed_roles("auditor"):
            auditor = self.org.pick(role=role["id"])
            self.db.log("work", f"{auditor['name']} is running the {role['title'].lower()} audit.", pid, actor=auditor["name"])
            audit_ticket = self._stage_ticket(pid, f"stage-audit-{role['id']}", f"{role['title']} audit for {p['name']}",
                                              auditor, f"Audit the product and report in docs/audits/{role['id']}.md.",
                                              handoff="Quality (release QA passed)")
            res = self.runtime.run(
                auditor, f"All build tasks for \"{p['name']}\" are complete. Audit the whole product in the workspace "
                f"against docs/PRD.md and docs/ARCHITECTURE.md. Write your report to docs/audits/{role['id']}.md, "
                "then finish by calling submit_review. Use request_changes only for findings that must be fixed "
                "before release, and list exactly what to fix.", ws, project_id=pid, meta={"ticket_id": audit_ticket})
            ws.commit(f"{role['title']} audit ({auditor['name']})")
            self._stage_status(pid, f"stage-audit-{role['id']}", "done", auditor["name"], self._verdict(res.review),
                               kind="comment")
            if not res.completed or not res.review or res.review["verdict"] != "approve" or not self._read(ws, f"docs/audits/{role['id']}.md"):
                findings.append((role, auditor, (res.review or {}).get("notes") or "Audit did not produce a completed approval verdict."))
        if not findings:
            return []
        rounds = {t["key"].split("-")[1] for t in self.db.all(
            "SELECT key FROM tasks WHERE project_id=? AND key LIKE 'audit-%'", pid)}
        if len(rounds) >= self.s.max_rework:
            return [f"{role['title']}: {notes}" for role, _, notes in findings]
        for role, auditor, notes in findings:
            fix = self._fix_task(pid, f"Fix {role['title'].lower()} audit findings",
                           f"{auditor['name']} ({role['title']}) audited the product and requires these fixes "
                           f"before release:\n{notes}\n\nFull report: docs/audits/{role['id']}.md",
                           key=f"audit-{len(rounds) + 1}-{role['id']}", reporter=auditor["name"])
            self._stage_status(pid, f"stage-audit-{role['id']}", "done", auditor["name"],
                               f"Filed {ticket_key(fix)} for {where(self.db, self._fix_role(pid))} to fix the findings.",
                               kind="handoff")
        self.db.log("work", f"Audits raised {len(findings)} fix task(s).", pid)
        return None

    def _approved_contract(self, pid: int) -> str | None:
        """The product.json text the CTO last approved, with the design or as a later change."""
        row = self.db.one("SELECT payload FROM approvals WHERE project_id=? AND status='approved' "
                          "AND kind IN ('architecture', 'contract') ORDER BY id DESC LIMIT 1", pid)
        return json.loads(row["payload"]).get("contract") if row else None

    def _contract_unchanged(self, p: dict, ws: Workspace) -> bool:
        """Agents may not rewrite the acceptance checks the CTO approved; any change goes back to the CTO."""
        approved, current = self._approved_contract(p["id"]), self._read(ws, CONTRACT) or None
        if current == approved:
            return True
        diff = "\n".join(difflib.unified_diff((approved or "").splitlines(), (current or "").splitlines(),
                                              "approved/product.json", "current/product.json", lineterm=""))
        self._approval(p["id"], "contract", "cto", f"Acceptance checks changed for {p['name']}",
                       "The team changed product.json after you approved it. Approve to accept the new checks. "
                       "Send back to restore the approved checks and pass your feedback to the team.\n\n" + diff,
                       {"contract": current, "approved": approved})
        self._stage(p["id"], "contract_review")
        return False

    def _release_failure(self, p: dict, findings: list[str]) -> None:
        pid = p["id"]
        rounds = self.db.one("SELECT COUNT(*) AS n FROM tasks WHERE project_id=? AND key LIKE 'verify-%'", pid)["n"]
        feedback = "\n".join(findings)
        if rounds < self.s.max_rework:
            self._fix_task(pid, "Repair product acceptance failures", feedback + "\n" + CONTRACT_GUIDE,
                           key=f"verify-{rounds + 1}")
            self.db.log("work", "Product verification failed; repair task created.", pid)
        else:
            self._approval(pid, "release_blocked", "cto", f"Release checks failed for {p['name']}", feedback,
                           {})
            self._stage(pid, "release_blocked")

    def _assignee(self, task: dict, pid: int) -> dict | None:
        """Rework stays with the same seat (or its new holder); new work goes to whoever is free."""
        if task["assignee_id"]:
            prev = self.db.one("SELECT * FROM agents WHERE id=?", task["assignee_id"])
            holder = prev and self.db.one("SELECT * FROM agents WHERE seat=? AND status!='fired'", prev["seat"])
            if holder:
                return holder
        return self.org.pick(role=task["role"], project_id=pid) or self.org.pick(kind="builder", project_id=pid)

    def _run_task(self, p: dict, task: dict, ws: Workspace) -> bool:
        pid = p["id"]
        while True:
            task = self.db.one("SELECT * FROM tasks WHERE id=?", task["id"])
            agent = self._assignee(task, pid)
            if not agent:
                raise PipelineError(f"Nobody on staff can take task '{task['key']}'. Hire a builder.")
            self.db.run("UPDATE tasks SET status='in_progress', assignee_id=?, updated_at=? WHERE id=?",
                        agent["id"], now(), task["id"])
            self.db.log("work", f"{agent['name']} started [{task['key']}] {task['title']}"
                        + (f" (attempt {task['attempts'] + 1})" if task["attempts"] else ""), pid, actor=agent["name"])
            note(self.db, task["id"], agent["name"], "Started work" + (f" (attempt {task['attempts'] + 1})."
                                                                      if task["attempts"] else "."))
            ask = (f"Project \"{p['name']}\". Read docs/PRD.md, docs/ARCHITECTURE.md and, if present, "
                   "docs/DESIGN.md first.\n\n"
                   f"Your task [{task['key']}]: {task['title']}\n{task['description']}\n\n"
                   "Done means: the work is in the workspace, tests exist and pass where they apply, "
                   "and nothing that worked before is broken.")
            if task["feedback"]:
                ask += f"\n\nYour previous attempt was sent back. Fix these findings:\n{task['feedback']}"
            comments = self.db.all("SELECT author, body FROM (SELECT * FROM ticket_comments WHERE task_id=? "
                                   "AND kind IN ('comment', 'transfer') ORDER BY id DESC LIMIT 12) ORDER BY id", task["id"])
            bosses = {self.s.ceo_name: "CEO", self.s.cto_name: "CTO"}
            if comments:
                ask += ("\n\nRecent comments and transfers on this ticket. Instructions from the CEO or CTO "
                        "override everything else:\n"
                        + "\n".join(f"- {c['author']}{' (' + bosses[c['author']] + ')' if c['author'] in bosses else ''}: "
                                     f"{c['body']}" for c in comments))
            ask += (f"\n\nThis is ticket {ticket_key(task['id'])}, owned by {where(self.db, task['role'])}. Log your "
                    "progress on it, file tickets for anything outside it, and transfer it if another department "
                    "should own it.")
            meta = {"task_key": task["key"], "ticket_id": task["id"]}
            res = self.runtime.run(agent, ask, ws, project_id=pid, meta=meta)

            if res.transfer:                    # the agent handed the ticket to another department
                if ws.changed_files() != "(no uncommitted changes)":
                    ws.commit(f"[{task['key']}] work so far, before transfer ({agent['name']})")
                self.db.run("UPDATE tasks SET status='todo', role=?, assignee_id=NULL, attempts=0, result=?, "
                            "updated_at=? WHERE id=?", res.transfer["role"], res.text, now(), task["id"])
                if res.text:
                    note(self.db, task["id"], agent["name"], res.text[:2000], kind="comment")
                return True

            changed = ws.changed_files()
            self.db.run("UPDATE tasks SET status='in_review', updated_at=? WHERE id=?", now(), task["id"])
            note(self.db, task["id"], agent["name"], "Submitted for review.\n" + (res.text or "")[:1500])
            findings, approved = [], res.completed
            if not res.completed:
                findings.append("Builder did not finish within its turn budget.")
            for check_role in self.org.staffed_roles("reviewer", "qa"):
                checker = self.org.pick(role=check_role["id"], exclude=agent["id"])
                if not checker:
                    continue
                source = "qa" if check_role["kind"] == "qa" else "peer"
                note(self.db, task["id"], agent["name"], f"Handed to {checker['name']}, "
                     f"{where(self.db, check_role['id'])}, for review.", kind="handoff")
                check = self.runtime.run(
                    checker,
                    f"As {check_role['title']}, check task [{task['key']}] \"{task['title']}\" in project "
                    f"\"{p['name']}\", done by {agent['name']}.\n\nTask:\n{task['description']}\n\n"
                    f"{agent['name']}'s summary:\n{res.text}\n\nFiles changed (git status):\n{changed}\n\n"
                    "Requirements are in docs/PRD.md and the design in docs/ARCHITECTURE.md. "
                    "Finish by calling submit_review.",
                    ws, project_id=pid, meta={**meta, "author": agent["name"]})
                note(self.db, task["id"], checker["name"], self._verdict(check.review), kind="comment")
                if not check.review:
                    self.db.log("warn", f"{checker['name']} gave no verdict on [{task['key']}].", pid)
                    approved = False
                    findings.append(f"{checker['name']} gave no verdict.")
                    continue
                if not check.completed:
                    approved = False
                    findings.append(f"{checker['name']} did not complete the review.")
                rv = check.review
                self.perf.record(agent["id"], rv["score"], source=source, reviewer=checker["name"], notes=rv["notes"],
                                 task_id=task["id"], project_id=pid)
                if rv["verdict"] != "approve":
                    approved = False
                    findings.append(f"{checker['name']} ({check_role['title']}, {rv['score']:.0f}/100): {rv['notes']}")
            self.perf.evaluate(agent["id"])

            if approved:
                ws.commit(f"[{task['key']}] {task['title']} ({agent['name']})")
                self.db.run("UPDATE tasks SET status='done', result=?, updated_at=? WHERE id=?",
                            res.text, now(), task["id"])
                self.db.log("work", f"[{task['key']}] passed review.", pid, actor=agent["name"])
                note(self.db, task["id"], agent["name"], "Passed review. Done.")
                return True
            attempts = task["attempts"] + 1
            failed = attempts > self.s.max_rework
            self.db.run("UPDATE tasks SET status=?, attempts=?, feedback=?, result=?, updated_at=? WHERE id=?",
                        "failed" if failed else "todo", attempts, "\n".join(findings), res.text, now(), task["id"])
            self.db.log("work", f"[{task['key']}] sent back ({attempts}).", pid, actor=agent["name"])
            note(self.db, task["id"], "OrgForge", (f"Escalated to {self._boss('cto')} after {attempts} attempts:\n"
                 if failed else f"Sent back to {agent['name']}, {where(self.db, task['role'])} (attempt {attempts}):\n")
                 + "\n".join(findings), kind="handoff")
            if failed:
                return False

    def _boss(self, role: str) -> str:
        name = self.s.human(role)
        return name if name.upper() == role.upper() else f"{name} ({role.upper()})"

    @staticmethod
    def _verdict(review: dict | None) -> str:
        if not review:
            return "No verdict submitted."
        word = "Approved" if review["verdict"] == "approve" else "Changes requested"
        return f"{word} ({review['score']:.0f}/100). {review['notes']}".strip()

    def _fix_role(self, pid: int) -> str:
        staffed = [r["id"] for r in self._builder_roles()]
        if not staffed:
            raise PipelineError("Nobody on staff can build. Hire a builder.")
        # Fixes go to the role that built most of this product (engineers, not e.g. marketing).
        by_work = self.db.all("SELECT role, COUNT(*) AS n FROM tasks WHERE project_id=? AND key NOT LIKE 'fix-%' "
                              "AND key NOT LIKE 'audit-%' GROUP BY role ORDER BY n DESC, MIN(id)", pid)
        return next((r["role"] for r in by_work if r["role"] in staffed), staffed[0])

    def _fix_task(self, pid: int, title: str, feedback: str, key: str | None = None, reporter: str = "OrgForge") -> int:
        role = self._fix_role(pid)
        if not key:
            n = self.db.one("SELECT COUNT(*) AS n FROM tasks WHERE project_id=? AND key LIKE 'fix-%'", pid)["n"] + 1
            key = f"fix-{n}"
        ts = now()
        tid = self.db.run("INSERT INTO tasks (project_id, key, title, description, role, status, type, priority, origin, "
                          "reporter, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          pid, key, title, feedback, role, "todo", "bug", "high", "pipeline", reporter, ts, ts)
        note(self.db, tid, reporter, f"Filed for {where(self.db, role)} from a failed check or a sent-back decision.")
        return tid

    # ---- human decisions -------------------------------------------------
    def decide(self, approval_id: int, role: str, decision: str, feedback: str = "") -> dict:
        """Record a CEO/CTO decision. Returns the approval. Call advance() on its project afterwards."""
        a = self.db.one("SELECT * FROM approvals WHERE id=?", approval_id)
        if not a:
            raise PipelineError(f"No approval {approval_id}.")
        if a["status"] != "pending":
            raise PipelineError(f"Approval {approval_id} was already {a['status']}.")
        if role != a["required_role"]:
            raise PermissionError(f"This decision belongs to the {a['required_role'].upper()}.")
        if decision not in ("approved", "rejected"):
            raise PipelineError("Decision must be 'approved' or 'rejected'.")
        feedback = feedback.strip()
        if decision == "rejected" and not feedback and a["kind"] != "hr":
            raise PipelineError("Say what needs to change when you reject, so the team can act on it.")
        who, ok, pid = self.s.human(role), decision == "approved", a["project_id"]
        payload = json.loads(a["payload"])
        if ok and a["kind"] == "release_blocked":
            raise PipelineError("Failed release checks cannot be approved. Reject with repair guidance to retry.")
        if ok and a["kind"] in ("release", "signoff"):
            ws = self.workspace(self.project(pid))
            if ws.changed_files() != "(no uncommitted changes)" or ws.git("rev-parse", "HEAD") != payload.get("verified_head"):
                raise PipelineError("Product changed after verification. Reject with guidance to rebuild and recheck.")
        self.db.run("UPDATE approvals SET status=?, feedback=?, decided_by=?, decided_at=? WHERE id=?",
                    decision, feedback, who, now(), approval_id)
        self.db.log("decision", f"{a['title']}: {decision}" + (f" — {feedback}" if feedback else ""), pid, actor=who)

        kind = a["kind"]
        if kind in ("prd", "architecture"):
            for author in payload.get("agent_ids") or [payload.get("agent_id")]:
                if author and self.org.agent(author)["status"] != "fired":
                    self.perf.record(author, HUMAN_APPROVED if ok else HUMAN_REJECTED, source="human", reviewer=who,
                                     notes=feedback or f"{a['title']} approved.", project_id=pid)
                    self.perf.evaluate(author)
            keys = ("stage-requirements", "stage-design-%") if kind == "prd" else ("stage-architecture",)
            for key in keys:
                self._stage_status(pid, key, "done" if ok else "todo", who,
                                   f"Approved by {who}." if ok else f"Sent back by {who}: {feedback}",
                                   kind="change" if ok else "comment", only=("in_review",))
            if kind == "prd":
                self._stage(pid, "architecture" if ok else "prd", "" if ok else feedback)
            else:
                self._stage(pid, "build" if ok else "architecture", "" if ok else feedback)
        elif kind == "escalation":
            task = self.db.one("SELECT * FROM tasks WHERE id=?", payload["task_id"])
            if ok:
                self.workspace(self.project(pid)).commit(f"[{task['key']}] accepted by the CTO")
                self.db.run("UPDATE tasks SET status='done', updated_at=? WHERE id=?", now(), task["id"])
                note(self.db, task["id"], who, "Accepted as it stands." + (f" {feedback}" if feedback else ""))
            else:
                note(self.db, task["id"], who, f"Sent back to the team with guidance: {feedback}")
                self.db.run("UPDATE tasks SET status='todo', attempts=0, feedback=?, updated_at=? WHERE id=?",
                            f"Guidance from the CTO: {feedback}\n{task['feedback']}", now(), task["id"])
            self._stage(pid, "build")
        elif kind == "contract":
            if not ok:
                ws = self.workspace(self.project(pid))
                if payload.get("approved") is None:
                    ws.resolve(CONTRACT).unlink(missing_ok=True)
                else:
                    ws.write_file(CONTRACT, payload["approved"])
                ws.commit(f"Restore the CTO-approved {CONTRACT}")
                self._fix_task(pid, "Address the CTO's feedback on the acceptance checks",
                               f"The CTO rejected your changes to {CONTRACT}, which has been restored to the approved "
                               f"version. Do not edit {CONTRACT} again unless the CTO asks.\n{feedback}", reporter=who)
            self._stage(pid, "build")
        elif kind == "release_blocked":
            self.db.run("UPDATE tasks SET key='retried-' || key || '-' || id WHERE project_id=? AND key LIKE 'verify-%'", pid)
            self._fix_task(pid, "Repair blocked release", feedback, reporter=who)
            self._stage(pid, "build")
        elif kind == "release":
            if ok:
                name = self.project(pid)["name"]
                self._approval(pid, "signoff", "ceo", f"Sign off {name}",
                               "The CTO approved the release. Review the product in its workspace and sign off.", payload)
                self._stage(pid, "signoff")
            else:
                self._fix_task(pid, "Address the CTO's release feedback", feedback, reporter=who)
                self._stage(pid, "build")
        elif kind == "signoff":
            if ok:
                ws = self.workspace(self.project(pid))
                ws.commit("Release signed off by the CEO")
                ws.git("tag", "-f", "release")
                self._stage(pid, "done")
                self.db.log("project", "Verified release ready for deployment.", pid, actor=who)
            else:
                self._fix_task(pid, "Address the CEO's sign-off feedback", feedback, reporter=who)
                self._stage(pid, "build")
        elif kind == "hr":
            if ok:
                self.perf.execute(payload["action"], payload["agent_id"], payload.get("reason", ""), by=who)
            else:
                self.perf.keep(payload["agent_id"])
        return self.db.one("SELECT * FROM approvals WHERE id=?", approval_id)

    # ---- views -----------------------------------------------------------
    def inbox(self, role: str | None = None) -> list[dict]:
        sql = "SELECT * FROM approvals WHERE status='pending'" + (" AND required_role=?" if role else "") + " ORDER BY id"
        return self.db.all(sql, *([role] if role else []))

    def overview(self) -> list[dict]:
        out = []
        for p in self.db.all("SELECT * FROM projects ORDER BY id DESC"):
            tasks = self.db.all(
                "SELECT t.*, a.name AS assignee FROM tasks t LEFT JOIN agents a ON a.id=t.assignee_id "
                "WHERE t.project_id=? ORDER BY t.id", p["id"])
            out.append({**p, "stage_label": STAGE_LABELS.get(p["stage"], p["stage"]), "tasks": tasks,
                        "workspace": str(Path(p["workspace"]))})
        return out
