import subprocess
from pathlib import Path

import pytest

from orgforge.pipeline import PipelineError


def decide_next(co, role, decision="approved", feedback=""):
    [approval] = [a for a in co.pipeline.inbox(role) if a["kind"] != "hr"]
    co.pipeline.decide(approval["id"], role, decision, feedback)
    return co.pipeline.advance(approval["project_id"])


def test_brief_to_shipped_product(co):
    p = co.pipeline.create_project("Greeter", "A tiny library.")
    assert co.pipeline.advance(p["id"])["stage"] == "prd_approval"

    with pytest.raises(PermissionError):                 # requirements are the CEO's call
        co.pipeline.decide(co.pipeline.inbox()[0]["id"], "cto", "approved")

    assert decide_next(co, "ceo")["stage"] == "architecture_approval"
    assert decide_next(co, "cto")["stage"] == "release_approval"
    assert decide_next(co, "cto")["stage"] == "signoff"
    assert decide_next(co, "ceo")["stage"] == "done"

    ws = Path(p["workspace"])
    for f in ("docs/PRD.md", "docs/DESIGN.md", "docs/ARCHITECTURE.md", "src/core.py", "tests/test_core.py",
              "docs/QA_REPORT.md", "docs/audits/security_auditor.md", "docs/audits/compliance_officer.md",
              "docs/audits/performance_engineer.md"):
        assert (ws / f).exists(), f
    tests = subprocess.run(["python", "-m", "unittest", "discover", "-s", "tests"], cwd=ws, capture_output=True)
    assert tests.returncode == 0
    log = subprocess.run(["git", "log", "--oneline"], cwd=ws, capture_output=True, text=True).stdout
    assert "[core]" in log and "signed off" in log
    all_tickets = co.pipeline.overview()[0]["tasks"]
    assert {t["status"] for t in all_tickets} == {"done"}            # stage work and build work alike
    tasks = [t for t in all_tickets if t["origin"] == "plan"]
    assert [t["key"] for t in tasks] == ["core", "extras"]
    assert co.org.agent("Ram")["score"] > 90 and co.org.agent("Sony")["score"] > 90   # scored by the humans
    assert co.org.agent("Sravani")["score"] > 90                                            # designer shares the CEO gate
    checkers = {r["reviewer"] for r in co.db.all("SELECT reviewer FROM reviews WHERE task_id=?", tasks[0]["id"])}
    assert checkers == {"Pavan", "Mani", "Badri"}        # code review, security review and QA on every task


def test_rejection_needs_feedback_and_sends_work_back(co):
    p = co.pipeline.create_project("Greeter", "A tiny library.")
    co.pipeline.advance(p["id"])
    approval = co.pipeline.inbox("ceo")[0]
    with pytest.raises(PipelineError):
        co.pipeline.decide(approval["id"], "ceo", "rejected")
    assert decide_next(co, "ceo", "rejected", "Too vague.")["stage"] == "prd_approval"
    assert co.org.agent("Ram")["score"] < 40


def test_failing_work_is_reworked_escalated_and_the_agent_replaced(make_company):
    co = make_company(bad_agents={"Hari", "Sandy"})        # both backend engineers get poor reviews
    p = co.pipeline.create_project("Greeter", "A tiny library.")
    co.pipeline.advance(p["id"])
    decide_next(co, "ceo")
    project = decide_next(co, "cto")
    assert project["stage"] == "escalation"
    task = next(t for t in co.pipeline.overview()[0]["tasks"] if t["key"] == "core")
    assert task["status"] == "failed" and task["attempts"] == 3      # first try + two reworks
    assert "Security Engineer" in task["feedback"] and "Code Reviewer" in task["feedback"]

    owner = co.org.agent(task["assignee_id"])
    assert owner["score"] < 40                            # below the floor: HR asks the CTO to replace them
    hr = [a for a in co.pipeline.inbox("cto") if a["kind"] == "hr"]
    assert len(hr) == 1 and owner["name"] in hr[0]["title"]
    co.pipeline.decide(hr[0]["id"], "cto", "approved")
    successor = co.org.agent(owner["seat"])
    assert successor["generation"] == 2 and "acceptance criteria" in successor["lessons"]

    # CTO sends the task back; the seat's new holder picks it up and passes.
    decide_next(co, "cto", "rejected", "Start over and follow the design.")
    task = next(t for t in co.pipeline.overview()[0]["tasks"] if t["key"] == "core")
    assert task["status"] == "done" and task["assignee_id"] == successor["id"]


def test_audit_findings_become_fix_tasks_before_release(make_company):
    from orgforge.llm import MockProvider
    co = make_company()
    co.runtime.provider = MockProvider(fail_audits_once={"security_auditor"})
    p = co.pipeline.create_project("Greeter", "A tiny library.")
    co.pipeline.advance(p["id"])
    decide_next(co, "ceo")
    assert decide_next(co, "cto")["stage"] == "release_approval"
    tasks = co.pipeline.overview()[0]["tasks"]
    fix = [t for t in tasks if t["key"].startswith("audit-")]
    assert len(fix) == 1 and fix[0]["status"] == "done" and fix[0]["role"] == "backend_engineer"
    assert "high-severity" in fix[0]["description"]
    release = co.pipeline.inbox("cto")[0]
    assert "UNRESOLVED" not in release["summary"] and "docs/audits/security_auditor.md" in release["summary"]


def test_unresolved_audit_findings_reach_the_cto(make_company):
    co = make_company()
    co.s.max_rework = 0                                   # no fix rounds allowed
    from orgforge.llm import MockProvider
    co.runtime.provider = MockProvider(fail_audits_once={"compliance_officer"})
    p = co.pipeline.create_project("Greeter", "A tiny library.")
    co.pipeline.advance(p["id"])
    decide_next(co, "ceo")
    decide_next(co, "cto")
    assert "UNRESOLVED AUDIT FINDINGS" in co.pipeline.inbox("cto")[0]["summary"]
