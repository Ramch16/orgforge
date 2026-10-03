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
    for f in ("docs/PRD.md", "docs/ARCHITECTURE.md", "src/core.py", "tests/test_core.py", "docs/QA_REPORT.md"):
        assert (ws / f).exists(), f
    tests = subprocess.run(["python", "-m", "unittest", "discover", "-s", "tests"], cwd=ws, capture_output=True)
    assert tests.returncode == 0
    log = subprocess.run(["git", "log", "--oneline"], cwd=ws, capture_output=True, text=True).stdout
    assert "[core]" in log and "signed off" in log
    tasks = co.pipeline.overview()[0]["tasks"]
    assert [t["status"] for t in tasks] == ["done", "done"]
    assert co.org.agent("Nadia")["score"] > 90 and co.org.agent("Tomas")["score"] > 90   # scored by the humans


def test_rejection_needs_feedback_and_sends_work_back(co):
    p = co.pipeline.create_project("Greeter", "A tiny library.")
    co.pipeline.advance(p["id"])
    approval = co.pipeline.inbox("ceo")[0]
    with pytest.raises(PipelineError):
        co.pipeline.decide(approval["id"], "ceo", "rejected")
    assert decide_next(co, "ceo", "rejected", "Too vague.")["stage"] == "prd_approval"
    assert co.org.agent("Nadia")["score"] < 40


def test_failing_work_is_reworked_escalated_and_the_agent_replaced(make_company):
    co = make_company(bad_agents={"Ife", "Kenji"})        # both backend engineers get poor reviews
    p = co.pipeline.create_project("Greeter", "A tiny library.")
    co.pipeline.advance(p["id"])
    decide_next(co, "ceo")
    project = decide_next(co, "cto")
    assert project["stage"] == "escalation"
    task = co.pipeline.overview()[0]["tasks"][0]
    assert task["status"] == "failed" and task["attempts"] == 3      # first try + two reworks

    owner = co.org.agent(task["assignee_id"])
    assert owner["score"] < 40                            # below the floor: HR asks the CTO to replace them
    hr = [a for a in co.pipeline.inbox("cto") if a["kind"] == "hr"]
    assert len(hr) == 1 and owner["name"] in hr[0]["title"]
    co.pipeline.decide(hr[0]["id"], "cto", "approved")
    successor = co.org.agent(owner["seat"])
    assert successor["generation"] == 2 and "acceptance criteria" in successor["lessons"]

    # CTO sends the task back; the seat's new holder picks it up and passes.
    decide_next(co, "cto", "rejected", "Start over and follow the design.")
    task = co.pipeline.overview()[0]["tasks"][0]
    assert task["status"] == "done" and task["assignee_id"] == successor["id"]
