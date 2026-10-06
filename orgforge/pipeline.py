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
import subprocess
import threading
import time
from pathlib import Path

from .agent import AgentRuntime
from .costs import spent
from .config import Settings
from .db import DB, now
from .org import Org
from .performance import Performance
from .tickets import PRIORITY_ORDER, WORK_KINDS, note, ticket_key, where
from .tools import Workspace
from .validation import CONTRACT, CONTRACT_GUIDE, validate_plan, verify_product

IDEA_STAGES = ["idea", "idea_review", "idea_decision", "plan", "plan_approval"]
STAGES = [*IDEA_STAGES, "prd", "prd_approval", "architecture", "architecture_approval", "build",
          "escalation", "contract_review", "release_blocked", "release_approval", "signoff", "done", "deploying", "deploy_approval", "deploy_failed", "live",
          "parked", "dropped", "paused"]
STAGE_LABELS = {
    "idea": "Assessing the idea", "idea_review": "Technical sign-off with CTO", "idea_decision": "Decision with CEO",
    "plan": "Planning across departments", "plan_approval": "Plan with CEO and CTO",
    "parked": "Parked", "dropped": "Dropped", "paused": "Paused: budget used up",
    "contract_review": "Acceptance checks changed, with CTO",
    "release_blocked": "Release checks need attention",
    "prd": "Writing requirements", "prd_approval": "Requirements with CEO",
    "architecture": "Designing", "architecture_approval": "Design with CTO",
    "build": "Building", "escalation": "Escalated to CTO",
    "release_approval": "Release with CTO", "signoff": "Sign-off with CEO", "done": "Ready for deployment",
    "deploying": "Deploying", "deploy_approval": "Deployment go-ahead", "deploy_failed": "Deployment failed, with CTO",
    "live": "Live in production",
}
HUMAN_APPROVED, HUMAN_REJECTED = 92, 35
MILESTONES = {"release_approval": "Ready for release review", "release_blocked": "Release blocked",
              "done": "Signed off", "paused": "Paused for budget", "live": "Live in production",
              "deploy_failed": "Deployment failed"}
PURPOSES = {"internal": "for internal use", "commercial": "to sell"}
RECOMMENDATIONS = {"build_internal": "build it for internal use", "build_to_sell": "build it to sell",
                   "park": "park it for now", "drop": "drop it"}
FEASIBILITY = {"achievable": "achievable", "achievable_with_risks": "achievable, with risks",
               "not_achievable": "not achievable as described"}
# Colleagues the product manager consults on an idea: (role id, or kind for the architect), file, question.
CONSULTS = [
    ("planner", "docs/assessment/FEASIBILITY.md", "Feasibility review",
     "Is this technically achievable? Give a rough size (small, medium or large, and about how many build tickets), "
     "the main components, the technical risks, and what it would need (data, integrations, infrastructure)."),
    ("product_marketer", "docs/assessment/MARKET.md", "Market review",
     "Who would use it? Is it better kept for internal use or sold? Name the likely customers, the alternatives "
     "they use today, possible pricing or revenue models, and how strong the demand looks. Be explicit about "
     "what you are unsure of."),
    ("compliance_officer", "docs/assessment/LEGAL.md", "Legal review",
     "What legal, privacy, data-protection or licensing risks does it carry, and would anything stop it being "
     "sold or used?"),
]


class PipelineError(Exception):
    pass


class Pipeline:
    def __init__(self, db: DB, settings: Settings, org: Org, perf: Performance, runtime: AgentRuntime) -> None:
        self.db, self.s, self.org, self.perf, self.runtime = db, settings, org, perf, runtime
        self._locks: dict[int, threading.Lock] = {}
        self._solo: set[int] = set()            # tickets that hit a merge conflict: redo them alone
        self.swarms = None
        self._cycle_limits = {}
        self.reporter = None                    # writes a pending status report (set by Company)
        self.production = None                  # deploys signed-off releases (set by Company)

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
        old = self.db.one("SELECT stage FROM projects WHERE id=?", pid)
        self.db.run("UPDATE projects SET stage=?, updated_at=? WHERE id=?", stage, now(), pid)
        if self.s.auto_reports and old and old["stage"] != stage and (
                stage in MILESTONES or (stage == "build" and old["stage"] == "architecture_approval")):
            self.queue_report(pid, MILESTONES.get(stage, "Build started"))
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
    def create_project(self, name: str, brief: str, by: str = "ceo", idea: bool = False,
                       budget: float | None = None) -> dict:
        """A project that starts building, or with `idea` an idea that is assessed and decided first."""
        ts = now()
        pid = self.db.run("INSERT INTO projects (name, brief, stage, author, budget, created_at, updated_at) "
                          "VALUES (?,?,?,?,?,?,?)", name, brief, "idea" if idea else "prd", by,
                          self.s.default_budget if budget is None else max(0.0, budget), ts, ts)
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "project"
        path = self.s.workspaces / f"{pid:03d}-{slug}"
        self.db.run("UPDATE projects SET workspace=? WHERE id=?", str(path), pid)
        ws = Workspace(path)
        ws.init_repo()
        ws.write_file("BRIEF.md", f"# {name}\n\n{brief}\n")
        ws.commit(f"{'Idea' if idea else 'Brief'} from {by}")
        self.db.log("project", f"New {'idea' if idea else 'project'}: {name}", pid, actor=by)
        return self.project(pid)

    def create_task(self, title: str, brief: str, by: str = "ceo", repo: str | None = None, issue: str | None = None,
                    checks: list[str] | None = None, role: str | None = None, budget: float | None = None) -> dict:
        """A quick task: one ticket straight to build, optionally on an existing repository or GitHub issue.
        No idea, plan, requirements or design stages; the CTO reviews the changes at the end."""
        from .sources import SourceError, clone, github_issue
        found = None
        if issue:
            try:
                found = github_issue(issue)
            except SourceError as exc:
                raise PipelineError(str(exc)) from exc
            repo = repo or found["repo"]
            title = title or f"#{found['number']} {found['title']}"
            brief = (brief + "\n\n" if brief else "") + f"GitHub issue {found['url']}:\n{found['title']}\n\n{found['body']}"
        if not (title or "").strip() or not (brief or "").strip():
            raise PipelineError("A task needs a title and a description (or a GitHub issue).")
        builders = {r["id"] for r in self._builder_roles()}
        if role and role not in builders:
            raise PipelineError(f"'{role}' is not a staffed builder role: {', '.join(sorted(builders))}.")
        ts = now()
        pid = self.db.run("INSERT INTO projects (name, brief, stage, author, budget, kind, source, checks, created_at, "
                          "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", title.strip()[:120], brief.strip(), "build", by,
                          self.s.default_budget if budget is None else max(0.0, budget), "task", repo or "",
                          json.dumps([c for c in (checks or []) if c.strip()]), ts, ts)
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "task"
        path = self.s.workspaces / f"{pid:03d}-{slug}"
        branch = f"orgforge/task-{pid}"
        try:
            if repo:
                base = clone(repo, path, branch)
            else:
                Workspace(path).init_repo()
                base = ""
        except (SourceError, subprocess.SubprocessError) as exc:
            self.db.run("DELETE FROM projects WHERE id=?", pid)
            raise PipelineError(str(exc)) from exc
        self.db.run("UPDATE projects SET workspace=?, base_branch=?, branch=? WHERE id=?", str(path), base,
                    branch if repo else "main", pid)
        ws = Workspace(path)
        ws.ensure_ignores(shared=not repo)
        if not repo:
            ws.write_file("TASK.md", f"# {title}\n\n{brief}\n")
            ws.commit(f"Task from {by}")
        role = role or (self._fix_role(pid) if builders else None)
        if not role:
            self.db.run("DELETE FROM projects WHERE id=?", pid)
            raise PipelineError("Nobody on staff can build. Hire a builder.")
        guide = (f"\n\nWork on the branch {branch} of the existing repository{f' ({repo})' if repo else ''}. Read the code "
                 "and its README first, follow its conventions, keep the change focused, add or update tests, and run "
                 "the existing tests. Never push." if repo else "")
        tid = self.db.run("INSERT INTO tasks (project_id, key, title, description, role, status, type, priority, origin, "
                          "reporter, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", pid, "task", title.strip(),
                          brief.strip() + guide, role, "todo", "bug" if found and any("bug" in l for l in found["labels"])
                          else "task", "high", "human", by, ts, ts)
        note(self.db, tid, by, f"Quick task for {where(self.db, role)}" + (f", on {repo}" if repo else "")
             + (f" (from {found['url']})" if found else "") + ".")
        self.db.log("project", f"New task: {title}" + (f" on {repo}" if repo else ""), pid, actor=by)
        return self.project(pid)

    def _task_finish(self, p: dict, ws: Workspace) -> None:
        """A quick task's tickets are done: run its checks, then the CTO reviews the changes."""
        pid, findings, results = p["id"], [], []
        for command in json.loads(p["checks"] or "[]"):
            output = ws.run_command(command)
            passed = output.splitlines()[0] == "exit code 0"
            results.append(f"{'PASS' if passed else 'FAIL'}  {command}")
            if not passed:
                findings.append(f"Check failed: {command}\n{output[:3000]}")
        if findings:
            self._release_failure(p, findings)
            return
        base = p["base_branch"]
        try:
            stat = ws.git("diff", "--stat", f"{base}...HEAD") if base else ws.git("show", "--stat", "--format=", "HEAD")
            log = ws.git("log", "--oneline", f"{base}..HEAD") if base else ws.git("log", "--oneline", "-15")
        except Exception:
            stat, log = "", ""
        done = self.db.all("SELECT id, title FROM tasks WHERE project_id=? AND status='done' ORDER BY id", pid)
        summary = ("Tickets done:\n" + "\n".join(f"- T-{t['id']} {t['title']}" for t in done)
                   + ("\n\nChecks:\n" + "\n".join(results) if results else "")
                   + f"\n\nChanges on {p['branch']}" + (f" (from {base})" if base else "") + f":\n{stat or '(none)'}"
                   + f"\n\nCommits:\n{log}\n\nApprove to accept the changes. Open the review to read the diff.")
        self._approval(pid, "task_review", "cto", f"Review the changes: {p['name']}", summary,
                       {"verified_head": ws.git("rev-parse", "HEAD")})
        self._stage(pid, "release_approval")

    def advance(self, pid: int, *, max_cycles: int | None = None, max_seconds: float | None = None) -> dict:
        """Run the project forward until it needs a human or is finished."""
        from .engines import EngineUnavailable
        if max_cycles is not None and (isinstance(max_cycles, bool) or not isinstance(max_cycles, int) or max_cycles < 1):
            raise ValueError('max_cycles must be a positive integer.')
        if max_seconds is not None and (not isinstance(max_seconds, (int, float)) or
                                       not 0 < max_seconds < float('inf')):
            raise ValueError('max_seconds must be positive and finite.')
        lock = self._locks.setdefault(pid, threading.Lock())
        if not lock.acquire(blocking=False):
            return self.project(pid)          # already running in another thread
        self._cycle_limits[pid] = {'remaining': max_cycles,
                                   'deadline': time.monotonic() + max_seconds if max_seconds else None}
        try:
            return self._advance(pid)
        except EngineUnavailable as exc:
            self.db.run("UPDATE tasks SET status='todo' WHERE project_id=? AND status IN ('in_progress','in_review') "
                        "AND origin!='stage'", pid)
            self.db.log("paused", f"Work paused: {exc}. Nothing was counted against the team. "
                        f"Resume once it is back (Resume work, or `orgforge run {pid}`).", pid)
            raise PipelineError(f"Work paused: {exc}. Nothing was counted against the team; resume once it is back "
                                f"with Resume work or `orgforge run {pid}`.") from exc
        finally:
            self._cycle_limits.pop(pid, None)
            lock.release()

    def _cycle_available(self, pid: int, consume: bool = False) -> bool:
        limit = self._cycle_limits.get(pid)
        if not limit:
            return True
        available = ((limit['remaining'] is None or limit['remaining'] > 0) and
                     (limit['deadline'] is None or time.monotonic() < limit['deadline']))
        if available and consume and limit['remaining'] is not None:
            limit['remaining'] -= 1
        return available

    def _advance(self, pid: int) -> dict:
        try:
            self.db.run("UPDATE tasks SET status='todo' WHERE project_id=? AND status IN ('in_progress', 'in_review') AND origin!='stage'", pid)
            steps = {"idea": self._idea, "plan": self._plan, "prd": self._prd, "architecture": self._architecture,
                     "build": self._build}
            if self.production:
                steps["deploying"] = self.production.run
            self.write_reports(pid)
            while (stage := self.project(pid)["stage"]) in steps:
                if not self._budget_ok(pid) or not self._cycle_available(pid, consume=stage != 'build'):
                    break
                steps[stage](self.project(pid))
            self.write_reports(pid)
        except Exception as exc:
            from .engines import EngineUnavailable
            if not isinstance(exc, EngineUnavailable):   # a pause is logged by advance(), not as an error
                self.db.log("error", f"{type(exc).__name__}: {exc}", pid)
            raise
        return self.project(pid)

    # ---- budgets and reports ---------------------------------------------
    def _budget_ok(self, pid: int) -> bool:
        """False when the project has used its budget; the CEO is asked to raise it or stop."""
        p, used = self.project(pid), spent(self.db, pid)
        budget = p["budget"]
        if budget <= 0:
            return True
        if used >= budget * self.s.budget_warn_at and p["budget_warned"] != budget:
            self.db.run("UPDATE projects SET budget_warned=? WHERE id=?", budget, pid)
            self.db.log("budget", f"{p['name']} has used ${used:.2f} of its ${budget:.2f} budget.", pid)
        if used < budget:
            return True
        if not self.db.one("SELECT 1 FROM approvals WHERE project_id=? AND kind='budget' AND status='pending'", pid):
            suggested = round(max(budget * 1.5, used + 5), 2)
            self._approval(pid, "budget", "ceo", f"Budget used up: {p['name']}",
                           f"{p['name']} has used ${used:.2f} of its ${budget:.2f} budget, so work is paused "
                           f"({STAGE_LABELS.get(p['stage'], p['stage'])}).\n\nApprove to raise the budget to "
                           f"${suggested:.2f}, or write a different amount in the note (e.g. 60). "
                           "Send back to stop work; you can set a new budget later to resume.",
                           {"suggested": suggested, "stage": p["stage"]})
        return False

    def set_budget(self, pid: int, amount: float, by: str) -> dict:
        """Set a project's budget (0 = no limit). Resumes a project paused for budget if there is room."""
        p = self.project(pid)
        amount = max(0.0, float(amount))
        self.db.run("UPDATE projects SET budget=? WHERE id=?", amount, pid)
        self.db.log("budget", f"Budget for {p['name']} set to " + (f"${amount:.2f}." if amount else "no limit."),
                    pid, actor=by)
        if amount == 0 or amount > spent(self.db, pid):
            self.db.run("UPDATE approvals SET status='withdrawn', decided_by=?, decided_at=? WHERE project_id=? "
                        "AND kind='budget' AND status='pending'", by, now(), pid)
            if p["stage"] == "paused":
                self._stage(pid, p["paused_stage"] or "build")
        return self.project(pid)

    def queue_report(self, pid: int, trigger: str, by: str = "") -> None:
        if not self.db.one("SELECT 1 FROM reports WHERE project_id=? AND status='pending'", pid):
            self.db.run("INSERT INTO reports (project_id, trigger, requested_by, created_at) VALUES (?,?,?,?)",
                        pid, trigger, by, now())

    def write_reports(self, pid: int) -> None:
        """Write any status reports waiting for this project (set by Company: see reports.py)."""
        if self.reporter:
            for r in self.db.all("SELECT * FROM reports WHERE project_id=? AND status='pending' ORDER BY id", pid):
                self.reporter(r)

    # ---- stages ----------------------------------------------------------
    def _idea(self, p: dict) -> None:
        """Product leads an assessment of the idea, with feasibility, market and legal input from colleagues."""
        ws, pid = self.workspace(p), p["id"]
        pm = self.org.pick(kind="product")
        if not pm:
            raise PipelineError("Nobody can assess ideas. Hire a role of kind 'product' (e.g. product manager).")
        author = p["author"] or self.s.ceo_name
        lead = self._stage_ticket(pid, "stage-assessment", f"Assess the idea: {p['name']}", pm, p["brief"],
                                  reporter=author)
        for ref, path, label, question in CONSULTS:
            colleague = self.org.pick(kind=ref) if ref == "planner" else self.org.pick(role=ref)
            if not colleague or colleague["id"] == pm["id"]:
                continue
            note(self.db, lead, pm["name"], f"Asked {colleague['name']}, {where(self.db, colleague['role'])}, "
                 f"for a {label.lower()}.", kind="handoff")
            ticket = self._stage_ticket(pid, f"stage-consult-{colleague['role']}", f"{label}: {p['name']}", colleague,
                                        question, reporter=pm["name"], handoff=where(self.db, pm["role"]))
            ask = (f"{pm['name']} ({where(self.db, pm['role'])}) is assessing an idea from {author} and asks for your "
                   f"{label.lower()}.\n\nThe idea, \"{p['name']}\":\n{p['brief']}\n\n{question}\n\n"
                   f"Write your answer to {path}. Keep it short and concrete.")
            if p["feedback"]:
                ask += f"\n\nThe previous assessment was sent back with this feedback; take it into account:\n{p['feedback']}"
            res = self.runtime.run(colleague, ask, ws, project_id=pid,
                                   meta={"purpose": "consult", "file": path, "ticket_id": ticket})
            if not self._read(ws, path):
                ws.write_file(path, f"# {label}\n\n{res.text or '(no answer)'}\n")
            ws.commit(f"{label} ({colleague['name']})")
            self._stage_status(pid, f"stage-consult-{colleague['role']}", "done", colleague["name"],
                               f"Written to {path}.", kind="handoff")
        ask = (f"{author} has an idea, \"{p['name']}\":\n\n{p['brief']}\n\nAssess whether the company should build it. "
               "Read your colleagues' input in docs/assessment/. Write docs/ASSESSMENT.md with: the problem and who it "
               "is for; whether it is better for internal use or to sell; feasibility; rough effort and cost (agents, "
               "tickets, model usage); revenue options if sold; risks; and your recommendation. Be honest when the "
               "answer is not to build it. Then call submit_assessment.")
        if p["feedback"]:
            ask += f"\n\nYour previous assessment was sent back. Address this:\n{p['feedback']}"
        self.db.log("work", f"{pm['name']} is assessing the idea.", pid, actor=pm["name"])
        res = self.runtime.run(pm, ask, ws, project_id=pid, meta={"purpose": "assessment", "ticket_id": lead},
                               extra_tools=["submit_assessment"])
        if not res.assessment:
            res = self.runtime.run(pm, "Your assessment is in docs/ASSESSMENT.md. Call submit_assessment now.", ws,
                                   project_id=pid, meta={"purpose": "assessment", "ticket_id": lead},
                                   extra_tools=["submit_assessment"])
        if not res.assessment:
            raise PipelineError(f"{pm['name']} did not submit an assessment. Run the project again to retry.")
        ws.commit(f"Idea assessment ({pm['name']})")
        a = res.assessment
        self._stage_status(pid, "stage-assessment", "in_review", pm["name"],
                           f"Recommends: {RECOMMENDATIONS[a['recommendation']]}. Feasibility: "
                           f"{FEASIBILITY[a['feasibility']]}. Sent to {self._boss('cto')} for technical sign-off.",
                           kind="handoff")
        self._approval(pid, "feasibility", "cto", f"Technical sign-off: {p['name']}",
                       self._assessment_summary(ws, a) + "\n\nApprove if the technical assessment is sound. Send back "
                       f"with your concerns otherwise. {self._boss('ceo')} decides after you.", {"assessment": a})
        self._stage(pid, "idea_review", "")

    def _assessment_summary(self, ws: Workspace, a: dict) -> str:
        return (f"Recommendation: {RECOMMENDATIONS[a['recommendation']]}\nFeasibility: {FEASIBILITY[a['feasibility']]}\n\n"
                f"{a['summary']}\n\n----- docs/ASSESSMENT.md -----\n{self._read(ws, 'docs/ASSESSMENT.md')}")

    def _plan(self, p: dict) -> None:
        """After a go decision, product writes one plan of action and assigns department tickets."""
        ws, pid = self.workspace(p), p["id"]
        pm = self.org.pick(kind="product")
        if not pm:
            raise PipelineError("Nobody can plan the work. Hire a role of kind 'product' (e.g. product manager).")
        purpose = PURPOSES.get(p["purpose"], "")
        selling = p["purpose"] == "commercial"
        ticket = self._stage_ticket(pid, "stage-plan", f"Plan of action: {p['name']}", pm,
                                    f"Plan the work across departments to build {p['name']} {purpose}.",
                                    reporter=self.s.ceo_name)
        roles = "\n".join(f"- {r['id']}: {r['title']} ({r['department']})" for r in self._work_roles())
        ask = (f"{self._boss('ceo')} decided to build \"{p['name']}\" {purpose}. Read docs/ASSESSMENT.md.\n\n"
               "Write docs/PLAN.md, the plan of action across departments: goals; scope of the first release; what "
               "each department does and in what order (Product requirements, Design, Engineering design and build, "
               "Quality and Security checks, Legal, Support" + (", Marketing pricing and launch" if selling else "")
               + "); milestones; risks.\n\nRequirements, UX design, architecture and build, QA and audits happen "
               "automatically. File department tickets only for other work, such as help docs for Support and terms "
               "and privacy for Legal" + (", and pricing and a launch plan for Marketing" if selling else
                                          ". It is for internal use, so do not plan marketing or pricing work")
               + ". Set after_build for work that needs the finished product. Then call submit_department_plan.\n\n"
               f"Roles that can own tickets:\n{roles}")
        if p["feedback"]:
            ask += f"\n\nYour previous plan was sent back. Address this:\n{p['feedback']}"
        self.db.log("work", f"{pm['name']} is planning the work across departments.", pid, actor=pm["name"])
        meta = {"purpose": "plan_of_action", "ticket_id": ticket, "selling": selling}
        res = self.runtime.run(pm, ask, ws, project_id=pid, meta=meta, extra_tools=["submit_department_plan"])
        if not res.dept_plan:
            res = self.runtime.run(pm, "The plan is in docs/PLAN.md. Call submit_department_plan now.", ws,
                                   project_id=pid, meta=meta, extra_tools=["submit_department_plan"])
        if not res.dept_plan:
            raise PipelineError(f"{pm['name']} did not submit a plan. Run the project again to retry.")
        ws.commit(f"Plan of action ({pm['name']})")
        plan = res.dept_plan
        listing = "\n".join(f"- {where(self.db, t['role'])}: {t['title']}" + (" (after the build)" if t["after_build"] else "")
                             for t in plan["tickets"]) or "- (no extra department tickets)"
        body = (f"{plan['summary']}\n\nDepartment tickets:\n{listing}\n\n----- docs/PLAN.md -----\n"
                f"{self._read(ws, 'docs/PLAN.md')}")
        self._stage_status(pid, "stage-plan", "in_review", pm["name"],
                           f"Plan shared with {self._boss('ceo')} and {self._boss('cto')} for approval.", kind="handoff")
        payload = {"tickets": plan["tickets"], "planner": pm["name"]}
        self._approval(pid, "plan", "ceo", f"Plan of action for {p['name']}: business side",
                       body + "\n\nApprove the business side: scope, launch, pricing, support and legal work.", payload)
        self._approval(pid, "plan", "cto", f"Plan of action for {p['name']}: technical side",
                       body + "\n\nApprove the technical side: scope, order of work, and what Engineering, Quality "
                       "and Security take on.", payload)
        self._stage(pid, "plan_approval", "")

    def _work_roles(self) -> list[dict]:
        marks = ",".join("?" * len(WORK_KINDS))
        return self.db.all(f"SELECT DISTINCT r.id, r.title, d.name AS department FROM roles r JOIN agents a ON "
                           f"a.role=r.id AND a.status!='fired' JOIN departments d ON d.id=r.department WHERE r.kind "
                           f"IN ({marks}) ORDER BY d.name, r.title", *WORK_KINDS)

    def revisit(self, pid: int, by: str) -> dict:
        """Bring a parked idea back to the CEO for a fresh decision."""
        p = self.project(pid)
        if p["stage"] != "parked":
            raise PipelineError(f"{p['name']} is not parked.")
        a = self.db.one("SELECT payload FROM approvals WHERE project_id=? AND kind='idea_decision' ORDER BY id DESC "
                        "LIMIT 1", pid)
        assessment = json.loads(a["payload"]).get("assessment") if a else None
        summary = (self._assessment_summary(self.workspace(p), assessment) if assessment
                   else self._read(self.workspace(p), "docs/ASSESSMENT.md"))
        self._approval(pid, "idea_decision", "ceo", f"Decide again: {p['name']}",
                       "This idea was parked and is back for a decision.\n\n" + summary,
                       {"assessment": assessment})
        self._stage(pid, "idea_decision")
        self.db.log("project", f"{p['name']} is back for a decision.", pid, actor=by)
        return self.project(pid)

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
        if p["purpose"]:
            ask += (f"\n\nThis product was assessed and planned, and is being built {PURPOSES[p['purpose']]}. Read "
                    "docs/ASSESSMENT.md and docs/PLAN.md first, and keep the requirements within the planned scope.")
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

    def _ready_tasks(self, pid: int) -> list[dict]:
        """To-do tickets whose dependencies are finished, most urgent first."""
        tasks = self.db.all(f"SELECT * FROM tasks WHERE project_id=? AND origin!='stage' ORDER BY {PRIORITY_ORDER}, id", pid)
        done = {t["key"] for t in tasks if t["status"] in ("done", "cancelled")}
        plan = [t for t in tasks if t["origin"] == "plan"]
        if plan and all(t["status"] in ("done", "cancelled") for t in plan):
            done.add("@build")                  # department work that needs the finished product can start
        return [t for t in tasks if t["status"] == "todo" and t["type"] != "question"
                and all(d in done for d in json.loads(t["depends_on"]))]

    def _next_task(self, pid: int) -> dict | None:
        ready = self._ready_tasks(pid)
        return ready[0] if ready else None

    def _build(self, p: dict) -> None:
        ws, pid = self.workspace(p), p["id"]
        if ws.ensure_ignores(shared=not p["source"]):   # projects from before 0.10.1 committed caches; stop that
            ws.commit("Ignore caches and build output")
        if self.db.one("SELECT 1 FROM approvals WHERE project_id=? AND kind='escalation' AND status='pending'", pid):
            self._stage(pid, "escalation")      # another failed ticket is still waiting for the CTO
            return
        while True:
            if not self._budget_ok(pid) or not self._cycle_available(pid, consume=True):
                return
            self._check_staffing(pid)
            batch = self._claim_batch(pid)
            if not batch:
                break
            if len(batch) == 1:
                self._solo.discard(batch[0]["id"])
                failed = [] if self._run_task(p, batch[0], ws) else [batch[0]]
            else:
                failed = self._run_parallel(p, batch, ws)
            if failed:
                for task in failed:
                    task = self.db.one("SELECT * FROM tasks WHERE id=?", task["id"])
                    self._approval(pid, "escalation", "cto",
                                   f"Task \"{task['title']}\" keeps failing review",
                                   f"Sent back {task['attempts']} time(s). Latest findings:\n{task['feedback']}\n\n"
                                   "Approve to accept the work as it stands. Reject with guidance to have it redone.",
                                   {"task_id": task["id"]})
                self._stage(pid, "escalation")
                return
        stuck = self.db.all("SELECT key FROM tasks WHERE project_id=? AND origin!='stage' AND type!='question' "
                            "AND status NOT IN ('done', 'cancelled', 'backlog')", p["id"])
        if stuck:
            raise PipelineError("Tasks cannot start because their dependencies never finish: "
                                + ", ".join(t["key"] for t in stuck))
        if p["kind"] == "task":
            self._task_finish(p, ws)
            return
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
        if self.runtime.learning:
            self.runtime.learning.settle_project(pid, False, "OrgForge", "the release checks then failed")
        rounds = self.db.one("SELECT COUNT(*) AS n FROM tasks WHERE project_id=? AND key LIKE 'verify-%'", pid)["n"]
        feedback = "\n".join(findings)
        if rounds < self.s.max_rework:
            task = p["kind"] == "task"
            self._fix_task(pid, "Make the failing checks pass" if task else "Repair product acceptance failures",
                           feedback + ("" if task else "\n" + CONTRACT_GUIDE), key=f"verify-{rounds + 1}")
            self.db.log("work", f"{'Checks' if task else 'Product verification'} failed; repair task created.", pid)
        else:
            self._approval(pid, "release_blocked", "cto", f"Release checks failed for {p['name']}", feedback,
                           {})
            self._stage(pid, "release_blocked")

    # ---- parallel work and staffing ----------------------------------------
    def _claim_batch(self, pid: int) -> list[dict]:
        """Give up to max_parallel ready tickets to distinct free agents. Returns the claimed tickets."""
        ready, busy, batch = self._ready_tasks(pid), set(), []
        solo = [t for t in ready if t["id"] in self._solo]
        limit = 1 if solo else self.s.max_parallel
        for task in solo[:1] or ready:
            if len(batch) >= limit:
                break
            agent = self._free_agent(task, pid, busy)
            if not agent:
                continue                        # everyone who can do it is busy: it waits for the next round
            busy.add(agent["id"])
            if self.swarms:
                self.swarms.assign(pid, task['id'], agent)
            self.db.run("UPDATE tasks SET assignee_id=?, updated_at=? WHERE id=?", agent["id"], now(), task["id"])
            batch.append(self.db.one("SELECT * FROM tasks WHERE id=?", task["id"]))
        if ready and not batch:
            raise PipelineError(f"Nobody on staff can take task '{ready[0]['key']}'. Hire a builder.")
        return batch

    def _free_agent(self, task: dict, pid: int, busy: set[int]) -> dict | None:
        """Rework stays with the same seat; new work goes to the least-loaded free agent in the role."""
        if self.swarms:
            return self.swarms.pick(task, pid, busy)
        if task["assignee_id"]:
            prev = self.db.one("SELECT * FROM agents WHERE id=?", task["assignee_id"])
            holder = prev and self.db.one("SELECT * FROM agents WHERE seat=? AND status!='fired'", prev["seat"])
            if holder and holder["role"] == task["role"]:
                return None if holder["id"] in busy else holder
        agent = self.org.pick(role=task["role"], project_id=pid, exclude=busy)
        if agent or self.org.staff(role=task["role"]):
            return agent
        return self.org.pick(kind="builder", project_id=pid, exclude=busy)

    def _run_parallel(self, p: dict, batch: list[dict], ws: Workspace) -> list[dict]:
        """Work several tickets at once, each in its own git worktree, merging each into main as it passes."""
        pid, results, errors = p["id"], {}, []
        if ws.changed_files() != "(no uncommitted changes)":
            ws.commit("Work in progress before parallel tickets")
        root = self.s.workspaces / ".worktrees"
        self.db.log("work", f"Working {len(batch)} tickets in parallel: "
                    + ", ".join(ticket_key(t["id"]) for t in batch), pid)

        def work(task: dict) -> None:
            branch, path = f"ticket/t-{task['id']}", root / f"{pid:03d}-t{task['id']}"
            try:
                ws.add_worktree(path, branch)
                wt = Workspace(path, mode=self.s.sandbox_mode, docker_image=self.s.docker_image,
                               docker_network=self.s.docker_network, timeout=self.s.command_timeout)
                ok = self._run_task(p, task, wt)
                if wt.changed_files() != "(no uncommitted changes)":
                    wt.commit(f"[{task['key']}] work in progress")
                results[task["id"]] = self._merge_ticket(p, task, ws, branch, ok)
            except Exception as exc:            # surfaced after every thread has finished
                errors.append(exc)
            finally:
                try:
                    ws.remove_worktree(path, branch)
                except Exception as exc:
                    errors.append(exc)

        threads = [threading.Thread(target=work, args=(t,), name=f"ticket-{t['id']}") for t in batch]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if errors:
            raise errors[0]
        return [t for t in batch if results.get(t["id"]) is False]

    def _merge_ticket(self, p: dict, task: dict, ws: Workspace, branch: str, ok: bool) -> bool:
        """Merge a ticket's branch into main. A conflict sends passed work back to be redone on the latest code."""
        current = self.db.one("SELECT * FROM tasks WHERE id=?", task["id"])
        agent = self.org.agent(current["assignee_id"]) if current["assignee_id"] else None
        conflicts = ws.merge(branch, f"Merge {ticket_key(task['id'])} [{task['key']}] {task['title']}")
        if not conflicts:
            if current["status"] == "done":
                note(self.db, task["id"], "OrgForge", "Merged into the main code.")
            return ok
        files = ", ".join(conflicts[:10])
        if current["status"] != "done":
            note(self.db, task["id"], "OrgForge", f"This attempt could not be merged (conflicts in {files}); "
                 "it was set aside.", kind="handoff")
            return ok
        # Not the agent's fault, so it does not count as a failed attempt. Redone alone, it cannot conflict again.
        feedback = (f"Your work passed review but conflicts with changes merged meanwhile in: {files}. "
                    "Redo it on top of the latest code; keep the other changes working.")
        self.db.run("UPDATE tasks SET status='todo', feedback=?, updated_at=? WHERE id=?", feedback, now(), task["id"])
        self._solo.add(task["id"])
        note(self.db, task["id"], "OrgForge", f"Sent back to {agent['name'] if agent else 'the team'} to redo alone "
             f"on the latest code: {feedback}", kind="handoff")
        self.db.log("work", f"[{task['key']}] conflicted with merged work; it will be redone on its own.", p["id"])
        return True

    def _check_staffing(self, pid: int) -> None:
        """Ask the department's boss to hire when a role's waiting work outgrows its people."""
        if self.swarms and self.swarms.scale(pid):
            return
        waiting = self.db.all("SELECT role, COUNT(*) AS n FROM tasks WHERE status='todo' AND origin!='stage' "
                              "GROUP BY role")
        for row in waiting:
            staff = self.org.staff(role=row["role"])
            if not staff or len(staff) >= self.s.max_per_role or row["n"] < self.s.hire_when_waiting * len(staff):
                continue
            asks = [json.loads(a["payload"]) | {"status": a["status"]} for a in self.db.all(
                "SELECT payload, status FROM approvals WHERE kind='hire' AND status IN ('pending', 'rejected') "
                "ORDER BY id DESC")]
            asks = [a for a in asks if a.get("role") == row["role"]]
            if any(a["status"] == "pending" for a in asks):
                continue
            declined = next((a for a in asks if a["status"] == "rejected"), None)
            if declined and declined.get("staff") == len(staff) and row["n"] <= declined.get("waiting", 0):
                continue                         # declined before, and the queue has not grown since
            role = self.org.role(row["role"])
            boss = self.db.one("SELECT reports_to FROM departments WHERE id=?", role["department"])["reports_to"]
            summary = (f"{row['n']} tickets are waiting for {where(self.db, role['id'])}, which has {len(staff)} "
                       f"agent(s): {', '.join(a['name'] for a in staff)}. Each agent works one ticket at a time, and "
                       f"the team works up to {self.s.max_parallel} tickets in parallel per project, so another "
                       f"{role['title']} would clear this queue sooner. Each extra agent adds model cost.\n\n"
                       "Approve to hire one more (seat and name are assigned automatically). "
                       "Send back to keep the team as it is.")
            self._approval(pid, "hire", boss, f"Hire another {role['title']}?", summary,
                           {"role": role["id"], "waiting": row["n"], "staff": len(staff)})

    def _checkers(self) -> list[dict]:
        """Roles that check each ticket, by review_mode. Fewer checks cost less; release QA and audits always run."""
        staffed = self.org.staffed_roles("reviewer", "qa")
        if self.s.review_mode == "thorough":
            return staffed
        reviewers = sorted((r for r in staffed if r["kind"] == "reviewer"), key=lambda r: r["id"] != "code_reviewer")
        qa = [r for r in staffed if r["kind"] == "qa"]
        if self.s.review_mode == "light":
            return (reviewers or qa)[:1]
        return reviewers[:1] + qa[:1]

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
            findings, approved, scores = [], res.completed, []
            if not res.completed:
                findings.append("Builder did not finish within its turn budget.")
            for check_role in self._checkers():
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
                    ws, project_id=pid, meta={**meta, "author": agent["name"],
                                               "author_model": self.db.one('SELECT model FROM routing_decisions WHERE id=?',
                                                                           res.route_id)['model'] if res.route_id else agent['model']})
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
                scores.append(rv["score"])
                if self.runtime.learning:
                    self.runtime.learning.verdict(pid, task["id"], checker["id"], check.route_id, rv["verdict"], rv["score"])
                self.perf.record(agent["id"], rv["score"], source=source, reviewer=checker["name"], notes=rv["notes"],
                                 task_id=task["id"], project_id=pid)
                if rv["verdict"] != "approve":
                    approved = False
                    findings.append(f"{checker['name']} ({check_role['title']}, {rv['score']:.0f}/100): {rv['notes']}")
            self.perf.evaluate(agent["id"])
            if res.route_id and self.runtime.router:
                self.runtime.router.evaluate(res.route_id, approved, sum(scores) / len(scores) if scores else None, lessons='\n'.join(findings))
            if not approved and self.runtime.memory:
                self.runtime.memory.remember(
                    f"Task {task['key']} failed review: " + '\n'.join(findings), 'OrgForge', pid,
                    category='failure', source=f"ticket:{task['id']};attempt:{task['attempts'] + 1}")
            if not approved and self.runtime.learning:
                self.runtime.learning.after_failure(res.route_id, pid)

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
    def decide(self, approval_id: int, role: str, decision: str, feedback: str = "", choice: str = "") -> dict:
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
        if a["kind"] == "idea_decision":
            allowed = ("internal", "commercial") if decision == "approved" else ("park", "drop")
            if choice not in allowed:
                raise PipelineError("Decide the idea with one of: build it for internal use (internal), build it to "
                                    "sell (commercial), park it (park), or drop it (drop).")
        if decision == "rejected" and not feedback and a["kind"] not in ("hr", "hire", "idea_decision", "budget", "deploy", "strategy"):
            raise PipelineError("Say what needs to change when you reject, so the team can act on it.")
        who, ok, pid = self.s.human(role), decision == "approved", a["project_id"]
        payload = json.loads(a["payload"])
        if a['kind']=='board':
            assessment=self.db.one('SELECT * FROM company_assessments WHERE id=?',payload.get('assessment_id'))
            board_ws=self.workspace(self.project(pid))
            if not assessment or board_ws.changed_files()!='(no uncommitted changes)' or board_ws.git('rev-parse','HEAD')!=assessment['revision']:
                raise PipelineError('Board assessment is stale; rerun it on the current product revision.')
        if ok and a["kind"] == "release_blocked":
            raise PipelineError("Failed release checks cannot be approved. Reject with repair guidance to retry.")
        if ok and a["kind"] in ("release", "signoff", "task_review"):
            ws = self.workspace(self.project(pid))
            if ws.changed_files() != "(no uncommitted changes)" or ws.git("rev-parse", "HEAD") != payload.get("verified_head"):
                raise PipelineError("Product changed after verification. Reject with guidance to rebuild and recheck.")
        self.db.run("UPDATE approvals SET status=?, feedback=?, decided_by=?, decided_at=? WHERE id=?",
                    decision, feedback, who, now(), approval_id)
        self.db.log("decision", f"{a['title']}: {decision}" + (f" — {feedback}" if feedback else ""), pid, actor=who)

        kind = a["kind"]
        if kind == "feasibility":
            if ok:
                p = self.project(pid)
                self._stage_status(pid, "stage-assessment", "in_review", who,
                                   f"Technical sign-off by {who}." + (f" {feedback}" if feedback else ""), kind="comment")
                self._approval(pid, "idea_decision", "ceo", f"Decide: {p['name']}",
                               f"{who} (CTO) signed off the technical assessment."
                               + (f"\nTheir note: {feedback}" if feedback else "") + "\n\n"
                               + self._assessment_summary(self.workspace(p), payload["assessment"])
                               + "\n\nChoose: build it for internal use, build it to sell, park it, or drop it.",
                               payload)
                self._stage(pid, "idea_decision")
            else:
                self._stage_status(pid, "stage-assessment", "todo", who, f"Sent back by {who}: {feedback}", kind="comment")
                self._stage(pid, "idea", feedback)
        elif kind == "idea_decision":
            label = {"internal": "build it for internal use", "commercial": "build it to sell", "park": "park it",
                     "drop": "drop it"}[choice]
            body = f"{who} decided to {label}." + (f" {feedback}" if feedback else "")
            if choice in PURPOSES:
                self.db.run("UPDATE projects SET purpose=? WHERE id=?", choice, pid)
                self._stage_status(pid, "stage-assessment", "done", who, body)
                self._stage(pid, "plan", "")
            else:
                self._stage_status(pid, "stage-assessment", "done" if choice == "park" else "cancelled", who, body)
                self._stage(pid, "parked" if choice == "park" else "dropped")
            self.db.log("project", body, pid, actor=who)
        elif kind == "plan":
            if not ok:
                self.db.run("UPDATE approvals SET status='withdrawn', decided_at=? WHERE project_id=? AND kind='plan' "
                            "AND status='pending'", now(), pid)
                self._stage_status(pid, "stage-plan", "todo", who, f"Sent back by {who}: {feedback}", kind="comment")
                self._stage(pid, "plan", feedback)
            elif not self.db.one("SELECT 1 FROM approvals WHERE project_id=? AND kind='plan' AND status='pending'", pid):
                created = []
                for t in payload["tickets"]:
                    ts = now()
                    tid = self.db.run(
                        "INSERT INTO tasks (project_id, key, title, description, role, depends_on, status, type, "
                        "priority, origin, reporter, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        pid, "pending", t["title"], t["description"], t["role"],
                        json.dumps(["@build"] if t["after_build"] else []), "todo", "task", "medium", "dept",
                        payload.get("planner", "OrgForge"), ts, ts)
                    self.db.run("UPDATE tasks SET key=? WHERE id=?", f"dept-{tid}", tid)
                    note(self.db, tid, payload.get("planner", "OrgForge"), f"From the plan of action, for "
                         f"{where(self.db, t['role'])}." + (" Starts once the build is finished." if t["after_build"] else ""))
                    created.append(ticket_key(tid))
                self._stage_status(pid, "stage-plan", "done", who,
                                   f"Plan approved by {self._boss('ceo')} and {self._boss('cto')}. Department tickets: "
                                   + (", ".join(created) or "none") + ".", kind="handoff")
                self._stage(pid, "prd", "")
            else:
                self.db.log("decision", f"Plan approved by {who}; waiting for the other sign-off.", pid, actor=who)
        elif kind in ("prd", "architecture"):
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
            if self.runtime.learning:
                self.runtime.learning.settle_task(task["id"], ok, who)
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
        elif kind == "task_review":
            if self.runtime.learning:
                self.runtime.learning.settle_project(pid, ok, who, f"{who} {'accepted' if ok else 'sent back'} the changes")
            if ok:
                self._stage(pid, "done")
                p = self.project(pid)
                self.db.log("project", f"Task accepted by {who}. The changes are on {p['branch']} in {p['workspace']}.",
                            pid, actor=who)
            else:
                self._fix_task(pid, "Address the CTO's review of the changes", feedback, reporter=who)
                self._stage(pid, "build")
        elif kind == "release":
            if not ok and self.runtime.learning:
                self.runtime.learning.settle_project(pid, False, who, f"{who} sent the release back")
            if ok:
                name = self.project(pid)["name"]
                self._approval(pid, "signoff", "ceo", f"Sign off {name}",
                               "The CTO approved the release. Review the product in its workspace and sign off.", payload)
                self._stage(pid, "signoff")
            else:
                self._fix_task(pid, "Address the CTO's release feedback", feedback, reporter=who)
                self._stage(pid, "build")
        elif kind == "signoff":
            if ok and self.runtime.learning:
                self.runtime.learning.settle_project(pid, True, who, "the release was accepted")
            if ok:
                ws = self.workspace(self.project(pid))
                ws.commit("Release signed off by the CEO")
                self.db.run("UPDATE projects SET version=version+1 WHERE id=?", pid)
                version = self.project(pid)["version"]
                ws.git("tag", "-f", "release")
                ws.git("tag", "-f", f"v{version}")
                deploys = bool(self.production and self.production.configured(self.project(pid)))
                self._stage(pid, "deploying" if deploys else "done")
                self.db.log("project", f"Version {version} verified and " + ("is being deployed." if deploys else
                            "ready for deployment."), pid, actor=who)
            else:
                self._fix_task(pid, "Address the CEO's sign-off feedback", feedback, reporter=who)
                self._stage(pid, "build")
        elif kind == "strategy":
            if self.runtime.learning:
                self.runtime.learning.decide(payload, ok, who)
        elif kind == "deploy":
            if ok:
                self._stage(pid, "deploying")
            else:
                self._stage(pid, "done")
                self.db.log("deploy", f"{who} held v{payload['version']} back from {payload['environment']}"
                            + (f": {feedback}" if feedback else "."), pid, actor=who)
        elif kind == "deploy_failed":
            if ok:
                self._stage(pid, "deploying")
            else:
                self._fix_task(pid, "Fix the failed deployment", feedback, reporter=who)
                self._stage(pid, "build")
        elif kind == "budget":
            if ok:
                amount = payload["suggested"]
                if m := re.search(r"\d+(?:\.\d+)?", feedback or ""):
                    amount = float(m.group(0))
                self.set_budget(pid, amount, who)
            else:
                self.db.run("UPDATE projects SET paused_stage=? WHERE id=?", payload["stage"], pid)
                self._stage(pid, "paused")
                self.db.log("budget", f"{who} stopped work when the budget ran out. Set a new budget to resume.",
                            pid, actor=who)
        elif kind == "hire":
            if ok:
                hired = self.org.hire(payload["role"], by=who)
                self.db.log("hire", f"{who} approved hiring {hired['name']} as {payload['role']} for the workload.",
                            pid, actor=who)
        elif kind == 'board':
            assessment = self.db.one('SELECT * FROM company_assessments WHERE id=?',payload['assessment_id'])
            self.db.run('UPDATE company_assessments SET status=? WHERE id=?','accepted' if ok else 'changes_requested',assessment['id'])
            if not ok:
                from .tickets import Tickets
                Tickets(self.db,self).create(pid,'Address board feedback',feedback,who,status='backlog',origin='board')
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
            label = "Changes with CTO" if p["kind"] == "task" and p["stage"] == "release_approval" else \
                STAGE_LABELS.get(p["stage"], p["stage"])
            out.append({**p, "stage_label": label, "tasks": tasks,
                        "workspace": str(Path(p["workspace"])),
                        "production": bool(self.production and self.production.configured(p)),
                        "deployments": self.production.list(p["id"], 10) if self.production else []})
        return out
