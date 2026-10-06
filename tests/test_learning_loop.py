import pytest

from orgforge.db import now
from orgforge.engines import actions_to_calls
from orgforge.learning import parse_strategy


def decide_next(co, role, decision="approved", feedback=""):
    [approval] = [a for a in co.pipeline.inbox(role) if a["kind"] not in ("hr", "hire")]
    co.pipeline.decide(approval["id"], role, decision, feedback)
    return co.pipeline.advance(approval["project_id"])


def failed_runs(co, pid, tickets, signature="python", lesson="No test for empty input."):
    """Builder runs that failed review, as the pipeline records them."""
    hari = co.org.agent("Hari")
    last = None
    for task_id in tickets:
        last = co.db.run("INSERT INTO routing_decisions(agent_id, project_id, task_id, task_kind, model, reason, "
                         "strategy, signature, created_at) VALUES (?,?,?,?,?,?,?,?,?)", hari["id"], pid, task_id,
                         "builder", hari["model"], "test", "baseline", signature, now())
        co.learning.record(last, False, 40, lesson)
    return last


def test_on_by_default_and_changes_nothing_without_evidence_or_rules(co):
    assert co.learning.enabled
    hari = co.org.agent("Hari")
    assert co.router.select(hari, "builder", {}) == (hari["model"], "Assigned agent model")
    choice, signature, text = co.learning.prepare("builder", "Add pytest tests")
    assert (choice, signature, text) == ("baseline", "python,testing", "Add pytest tests")


def test_repeated_failures_lead_to_a_proposal_the_cto_decides(co):
    p = co.pipeline.create_project("Greeter", "A tiny library.")
    route = failed_runs(co, p["id"], [1, 2, 3])
    sid = co.learning.after_failure(route, p["id"])
    [a] = [a for a in co.pipeline.inbox("cto") if a["kind"] == "strategy"]
    assert "after 3 failed reviews of python work" in a["summary"] and "write a test" in a["summary"]
    assert co.learning.after_failure(route, p["id"]) is None             # one open proposal at a time
    assert "failing-test-first" not in " ".join(co.learning.strategies("builder", "python"))

    co.pipeline.decide(a["id"], "cto", "approved")
    [s] = co.learning.proposals()
    assert s["id"] == sid and s["status"] == "approved" and s["decided_by"]
    assert s["name"] in co.learning.strategies("builder", "python")
    assert s["name"] not in co.learning.strategies("builder", "frontend")   # only for the work it came from
    assert s["name"] in co.learning.strategies("builder", "python,testing")   # python work that is also testing
    choice, _, text = co.learning.prepare("builder", "Fix the django view")
    assert choice == s["name"] and "write a test that reproduces" in text  # untried, so tried before baseline again


def test_rejected_proposals_are_not_used_and_need_new_failures(co):
    p = co.pipeline.create_project("Greeter", "A tiny library.")
    route = failed_runs(co, p["id"], [1, 2, 3])
    co.learning.after_failure(route, p["id"])
    [a] = [a for a in co.pipeline.inbox("cto") if a["kind"] == "strategy"]
    co.pipeline.decide(a["id"], "cto", "rejected")                        # no reason needed
    assert list(co.learning.strategies("builder", "python")) == ["baseline"]
    assert co.learning.after_failure(route, p["id"]) is None             # the old failures were already used


def test_one_ticket_failing_repeatedly_is_not_a_pattern(co):
    p = co.pipeline.create_project("Greeter", "A tiny library.")
    route = failed_runs(co, p["id"], [7, 7, 7, 7])
    assert co.learning.after_failure(route, p["id"]) is None


def test_proposals_can_be_switched_off(co):
    co.s.raw["learning"] = {"propose_after": 0}
    p = co.pipeline.create_project("Greeter", "A tiny library.")
    assert co.learning.after_failure(failed_runs(co, p["id"], [1, 2, 3]), p["id"]) is None


def test_strategy_tool_validation_and_cli_block():
    assert parse_strategy({"name": "Small-Steps", "prompt": "x" * 50, "why": "y"})["name"] == "small-steps"
    with pytest.raises(ValueError):
        parse_strategy({"name": "no spaces allowed", "prompt": "x" * 50})
    with pytest.raises(ValueError):
        parse_strategy({"name": "short", "prompt": "too short"})
    assert actions_to_calls({"strategy": {"name": "a-b", "prompt": "p", "why": "w"}}) == \
        [("submit_strategy", {"name": "a-b", "prompt": "p", "why": "w"})]


def test_escalation_settles_the_latest_verdicts(make_company):
    co = make_company(bad_agents={"Hari", "Sandy"})
    p = co.pipeline.create_project("Greeter", "A tiny library.")
    co.pipeline.advance(p["id"])
    decide_next(co, "ceo")
    assert decide_next(co, "cto")["stage"] == "escalation"
    task = next(t for t in co.pipeline.overview()[0]["tasks"] if t["key"] == "core")
    rows = co.db.all("SELECT * FROM review_verdicts WHERE task_id=?", task["id"])
    open_rows = [v for v in rows if v["settled_at"] is None]
    assert len(rows) == 6 and len(open_rows) == 2                          # 3 rounds x 2 reviewers; latest round open
    assert all(v["verdict"] == "request_changes" for v in open_rows)
    [esc] = [a for a in co.pipeline.inbox("cto") if a["kind"] == "escalation"]
    co.pipeline.decide(esc["id"], "cto", "approved", "Good enough for now.")   # the CTO overrules the blockers
    stats = {r["name"]: r for r in co.learning.reviewers()}
    assert {r["too_strict"] for r in stats.values()} == {1} and {r["right"] for r in stats.values()} == {0}
    marks = co.db.all("SELECT score, notes FROM reviews WHERE source='calibration'")
    assert len(marks) == 2 and {m["score"] for m in marks} == {35} and "was wrong" in marks[0]["notes"]


def test_accepted_release_confirms_approvals_once_per_reviewer(co):
    p = co.pipeline.create_project("Greeter", "A tiny library.")
    co.pipeline.advance(p["id"])
    for role in ("ceo", "cto", "cto", "ceo"):
        decide_next(co, role)
    assert co.pipeline.project(p["id"])["stage"] == "done"
    assert not co.db.one("SELECT 1 FROM review_verdicts WHERE settled_at IS NULL")
    assert co.db.one("SELECT MIN(correct) AS c FROM review_verdicts")["c"] == 1
    marks = co.db.all("SELECT r.score, a.name FROM reviews r JOIN agents a ON a.id=r.agent_id WHERE source='calibration'")
    reviewers = {v["reviewer_id"] for v in co.db.all("SELECT reviewer_id FROM review_verdicts")}
    assert len(marks) == len(reviewers) and {m["score"] for m in marks} == {92}   # one mark each, not one per ticket


def test_failed_release_checks_are_a_mild_shared_miss(make_company):
    from orgforge.llm import MockProvider
    co = make_company()
    co.s.max_rework = 0                                   # the failure goes straight to release checks
    co.runtime.provider = MockProvider(fail_audits_once={"compliance_officer"})
    p = co.pipeline.create_project("Greeter", "A tiny library.")
    co.pipeline.advance(p["id"])
    decide_next(co, "ceo")
    decide_next(co, "cto")
    misses = co.db.all("SELECT * FROM reviews WHERE source='calibration' AND score=60")
    assert misses and len(misses) == len({m["agent_id"] for m in misses})
    assert not co.db.one("SELECT 1 FROM agents WHERE status='fired'")       # nobody is fired over a shared miss
    assert co.db.one("SELECT 1 FROM review_verdicts WHERE correct=0 AND verdict='approve'")


def test_reviewer_scoring_can_be_switched_off(co):
    co.s.raw["learning"] = {"score_reviewers": False}
    p = co.pipeline.create_project("Greeter", "A tiny library.")
    co.pipeline.advance(p["id"])
    for role in ("ceo", "cto", "cto", "ceo"):
        decide_next(co, role)
    assert co.db.one("SELECT 1 FROM review_verdicts WHERE correct=1")      # still measured
    assert not co.db.one("SELECT 1 FROM reviews WHERE source='calibration'")
