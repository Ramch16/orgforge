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

import json
import re
import threading
from pathlib import Path

from .agent import AgentRuntime
from .config import Settings
from .db import DB, now
from .org import Org
from .performance import Performance
from .tools import Workspace

STAGES = ["prd", "prd_approval", "architecture", "architecture_approval", "build",
          "escalation", "release_approval", "signoff", "done"]
STAGE_LABELS = {
    "prd": "Writing requirements", "prd_approval": "Requirements with CEO",
    "architecture": "Designing", "architecture_approval": "Design with CTO",
    "build": "Building", "escalation": "Escalated to CTO",
    "release_approval": "Release with CTO", "signoff": "Sign-off with CEO", "done": "Shipped",
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

    @staticmethod
    def _read(ws: Workspace, path: str) -> str:
        target = ws.root / path
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
               "Write the requirements document to docs/PRD.md.")
        if p["feedback"]:
            ask += f"\n\nThe CEO sent the previous version back. Revise docs/PRD.md to address this:\n{p['feedback']}"
        self.db.log("work", f"{pm['name']} is writing the requirements.", p["id"], actor=pm["name"])
        res = self.runtime.run(pm, ask, ws, project_id=p["id"])
        if not self._read(ws, "docs/PRD.md"):
            ws.write_file("docs/PRD.md", res.text or p["brief"])
        ws.commit(f"Requirements ({pm['name']})")
        authors = [pm["id"]]
        for role in self.org.staffed_roles("designer"):
            designer = self.org.pick(role=role["id"])
            ask = (f"Project \"{p['name']}\". Read docs/PRD.md, then write the design to docs/DESIGN.md.")
            if p["feedback"]:
                ask += f"\n\nThe CEO sent the previous version back. Address this where it concerns the design:\n{p['feedback']}"
            self.db.log("work", f"{designer['name']} is designing the user experience.", p["id"], actor=designer["name"])
            self.runtime.run(designer, ask, ws, project_id=p["id"])
            ws.commit(f"UX design ({designer['name']})")
            authors.append(designer["id"])
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
               "run/deploy setup, automated tests and user documentation.")
        if p["feedback"]:
            ask += f"\n\nThe CTO sent the previous design back. Address this:\n{p['feedback']}"
        meta = {"builder_roles": [r["id"] for r in roles]}
        self.db.log("work", f"{architect['name']} is designing the system.", p["id"], actor=architect["name"])
        res = self.runtime.run(architect, ask, ws, project_id=p["id"], meta=meta)
        if not res.plan:
            res = self.runtime.run(architect, "The design is in docs/ARCHITECTURE.md but no plan was submitted. "
                                   f"Read it and call submit_plan now. Roles:\n{role_list}", ws,
                                   project_id=p["id"], meta=meta)
        if not res.plan:
            raise PipelineError(f"{architect['name']} did not submit a build plan. Run the project again to retry.")

        self.db.run("DELETE FROM tasks WHERE project_id=? AND status!='done'", p["id"])
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
            self.db.run(
                "INSERT INTO tasks (project_id, key, title, description, role, depends_on, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)", p["id"], t["key"], t.get("title", t["key"]), t.get("description", ""),
                role, json.dumps(deps), ts, ts)
        ws.commit(f"Architecture and plan ({architect['name']})")
        plan_text = "\n".join(f"- [{t['key']}] {t.get('title', '')} ({t.get('role', '')})" for t in res.plan)
        self._approval(p["id"], "architecture", "cto", f"Design and plan for {p['name']}",
                       f"Plan ({len(res.plan)} tasks):\n{plan_text}\n\n{self._read(ws, 'docs/ARCHITECTURE.md')}",
                       {"agent_ids": [architect["id"]], "file": "docs/ARCHITECTURE.md"})
        self._stage(p["id"], "architecture_approval", "")

    def _next_task(self, pid: int) -> dict | None:
        tasks = self.db.all("SELECT * FROM tasks WHERE project_id=? ORDER BY id", pid)
        done = {t["key"] for t in tasks if t["status"] == "done"}
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
        stuck = self.db.all("SELECT key FROM tasks WHERE project_id=? AND status!='done'", p["id"])
        if stuck:
            raise PipelineError("Tasks cannot start because their dependencies never finish: "
                                + ", ".join(t["key"] for t in stuck))
        qa = self.org.pick(kind="qa")
        if qa:
            self.db.log("work", f"{qa['name']} is running the release check.", p["id"], actor=qa["name"])
            self.runtime.run(qa, "All build tasks are complete. Check the product as a whole against docs/PRD.md: "
                             "install it, run the full test suite and try the main flows. Write what you ran and "
                             "what you found to docs/QA_REPORT.md, including anything that does not work.",
                             ws, project_id=p["id"], meta={"purpose": "integration"})
            ws.commit(f"Release check ({qa['name']})")
        open_findings = self._audit(p, ws)
        if open_findings is None:
            return                              # audits raised fix tasks; the build loop runs again
        report = self._read(ws, "docs/QA_REPORT.md") or "No QA report was produced."
        if open_findings:
            report = ("UNRESOLVED AUDIT FINDINGS (the team could not clear these):\n"
                      + "\n".join(f"- {f}" for f in open_findings) + "\n\n" + report)
        audits = sorted((ws.root / "docs" / "audits").glob("*.md")) if (ws.root / "docs" / "audits").is_dir() else []
        if audits:
            report += "\n\nAudit reports: " + ", ".join(f"docs/audits/{a.name}" for a in audits)
        self._approval(p["id"], "release", "cto", f"Release {p['name']}", report, {})
        self._stage(p["id"], "release_approval")

    def _audit(self, p: dict, ws: Workspace) -> list[str] | None:
        """Run every staffed auditor role over the finished product. Returns None
        if fix tasks were raised (build continues), else the findings still open."""
        pid, findings = p["id"], []
        for role in self.org.staffed_roles("auditor"):
            auditor = self.org.pick(role=role["id"])
            self.db.log("work", f"{auditor['name']} is running the {role['title'].lower()} audit.", pid, actor=auditor["name"])
            res = self.runtime.run(
                auditor, f"All build tasks for \"{p['name']}\" are complete. Audit the whole product in the workspace "
                f"against docs/PRD.md and docs/ARCHITECTURE.md. Write your report to docs/audits/{role['id']}.md, "
                "then finish by calling submit_review. Use request_changes only for findings that must be fixed "
                "before release, and list exactly what to fix.", ws, project_id=pid)
            ws.commit(f"{role['title']} audit ({auditor['name']})")
            if res.review and res.review["verdict"] != "approve":
                findings.append((role, auditor, res.review["notes"]))
        if not findings:
            return []
        rounds = {t["key"].split("-")[1] for t in self.db.all(
            "SELECT key FROM tasks WHERE project_id=? AND key LIKE 'audit-%'", pid)}
        if len(rounds) >= self.s.max_rework:
            return [f"{role['title']}: {notes}" for role, _, notes in findings]
        for role, auditor, notes in findings:
            self._fix_task(pid, f"Fix {role['title'].lower()} audit findings",
                           f"{auditor['name']} ({role['title']}) audited the product and requires these fixes "
                           f"before release:\n{notes}\n\nFull report: docs/audits/{role['id']}.md",
                           key=f"audit-{len(rounds) + 1}-{role['id']}")
        self.db.log("work", f"Audits raised {len(findings)} fix task(s).", pid)
        return None

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
            ask = (f"Project \"{p['name']}\". Read docs/PRD.md, docs/ARCHITECTURE.md and, if present, "
                   "docs/DESIGN.md first.\n\n"
                   f"Your task [{task['key']}]: {task['title']}\n{task['description']}\n\n"
                   "Done means: the work is in the workspace, tests exist and pass where they apply, "
                   "and nothing that worked before is broken.")
            if task["feedback"]:
                ask += f"\n\nYour previous attempt was sent back. Fix these findings:\n{task['feedback']}"
            meta = {"task_key": task["key"]}
            res = self.runtime.run(agent, ask, ws, project_id=pid, meta=meta)

            changed = ws.changed_files()
            findings, approved = [], True
            for check_role in self.org.staffed_roles("reviewer", "qa"):
                checker = self.org.pick(role=check_role["id"], exclude=agent["id"])
                if not checker:
                    continue
                source = "qa" if check_role["kind"] == "qa" else "peer"
                check = self.runtime.run(
                    checker,
                    f"As {check_role['title']}, check task [{task['key']}] \"{task['title']}\" in project "
                    f"\"{p['name']}\", done by {agent['name']}.\n\nTask:\n{task['description']}\n\n"
                    f"{agent['name']}'s summary:\n{res.text}\n\nFiles changed (git status):\n{changed}\n\n"
                    "Requirements are in docs/PRD.md and the design in docs/ARCHITECTURE.md. "
                    "Finish by calling submit_review.",
                    ws, project_id=pid, meta={**meta, "author": agent["name"]})
                if not check.review:
                    self.db.log("warn", f"{checker['name']} gave no verdict on [{task['key']}].", pid)
                    continue
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
                return True
            attempts = task["attempts"] + 1
            failed = attempts > self.s.max_rework
            self.db.run("UPDATE tasks SET status=?, attempts=?, feedback=?, result=?, updated_at=? WHERE id=?",
                        "failed" if failed else "todo", attempts, "\n".join(findings), res.text, now(), task["id"])
            self.db.log("work", f"[{task['key']}] sent back ({attempts}).", pid, actor=agent["name"])
            if failed:
                return False

    def _fix_task(self, pid: int, title: str, feedback: str, key: str | None = None) -> None:
        staffed = [r["id"] for r in self._builder_roles()]
        if not staffed:
            raise PipelineError("Nobody on staff can build. Hire a builder.")
        # Fixes go to the role that built most of this product (engineers, not e.g. marketing).
        by_work = self.db.all("SELECT role, COUNT(*) AS n FROM tasks WHERE project_id=? AND key NOT LIKE 'fix-%' "
                              "AND key NOT LIKE 'audit-%' GROUP BY role ORDER BY n DESC, MIN(id)", pid)
        role = next((r["role"] for r in by_work if r["role"] in staffed), staffed[0])
        if not key:
            n = self.db.one("SELECT COUNT(*) AS n FROM tasks WHERE project_id=? AND key LIKE 'fix-%'", pid)["n"] + 1
            key = f"fix-{n}"
        ts = now()
        self.db.run("INSERT INTO tasks (project_id, key, title, description, role, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?)", pid, key, title, feedback, role, ts, ts)

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
            if kind == "prd":
                self._stage(pid, "architecture" if ok else "prd", "" if ok else feedback)
            else:
                self._stage(pid, "build" if ok else "architecture", "" if ok else feedback)
        elif kind == "escalation":
            task = self.db.one("SELECT * FROM tasks WHERE id=?", payload["task_id"])
            if ok:
                self.workspace(self.project(pid)).commit(f"[{task['key']}] accepted by the CTO")
                self.db.run("UPDATE tasks SET status='done', updated_at=? WHERE id=?", now(), task["id"])
            else:
                self.db.run("UPDATE tasks SET status='todo', attempts=0, feedback=?, updated_at=? WHERE id=?",
                            f"Guidance from the CTO: {feedback}\n{task['feedback']}", now(), task["id"])
            self._stage(pid, "build")
        elif kind == "release":
            if ok:
                name = self.project(pid)["name"]
                self._approval(pid, "signoff", "ceo", f"Sign off {name}",
                               "The CTO approved the release. Review the product in its workspace and sign off.", {})
                self._stage(pid, "signoff")
            else:
                self._fix_task(pid, "Address the CTO's release feedback", feedback)
                self._stage(pid, "build")
        elif kind == "signoff":
            if ok:
                ws = self.workspace(self.project(pid))
                ws.commit("Release signed off by the CEO")
                ws.git("tag", "-f", "release")
                self._stage(pid, "done")
                self.db.log("project", "Shipped.", pid, actor=who)
            else:
                self._fix_task(pid, "Address the CEO's sign-off feedback", feedback)
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
