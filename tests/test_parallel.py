import subprocess
import threading
import time

import pytest

from orgforge.company import Company
from orgforge.llm import MockProvider
from test_pipeline import decide_next


class Timed(MockProvider):
    """The mock company, but builders take a moment, so overlapping work is measurable."""

    def __init__(self, shared_file=False):
        super().__init__()
        self.shared_file, self.running, self.peak, self.lock = shared_file, 0, 0, threading.Lock()

    def complete(self, **kwargs):
        meta = kwargs.get("meta") or {}
        if meta.get("kind") != "builder":
            return super().complete(**kwargs)
        with self.lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
        time.sleep(0.15)
        try:
            return super().complete(**kwargs)
        finally:
            with self.lock:
                self.running -= 1

    def _script(self, meta, step=0):
        script = super()._script(meta, step)
        if self.shared_file and meta.get("kind") == "builder" and script:
            key = meta.get("task_key")         # every ticket also edits the same line of one file
            script[0].append(self._call("write_file", path="NOTES.md", content=f"Last change: {key}\n"))
        return script


def company(tmp_path, provider, **settings):
    co = Company(tmp_path, provider=provider, create=True)
    for key, value in settings.items():
        setattr(co.s, key, value)
    return co


def plan_approved(co):
    p = co.pipeline.create_project("Product", "Build a library")
    co.pipeline.advance(p["id"])
    decide_next(co, "ceo")
    approval = next(a for a in co.pipeline.inbox("cto") if a["kind"] == "architecture")
    co.pipeline.decide(approval["id"], "cto", "approved")
    return p


def add_tickets(co, pid, n, role="backend_engineer"):
    return [co.tickets.create(pid, f"Ticket {i}", "Independent work.", co.s.cto_name, status="todo", role=role)
            for i in range(n)]


def test_independent_tickets_run_in_parallel_and_merge(tmp_path):
    provider = Timed()
    co = company(tmp_path, provider)
    p = plan_approved(co)
    add_tickets(co, p["id"], 2, role="frontend_engineer")
    add_tickets(co, p["id"], 2, role="backend_engineer")
    assert co.pipeline.advance(p["id"])["stage"] == "release_approval"
    assert provider.peak >= 2                       # builders really overlapped
    ws = p["workspace"]
    log = subprocess.run(["git", "log", "--oneline"], cwd=ws, capture_output=True, text=True).stdout
    assert log.count("Merge T-") >= 3
    assert subprocess.run(["git", "worktree", "list"], cwd=ws, capture_output=True, text=True).stdout.count("\n") == 1
    assert not subprocess.run(["git", "branch", "--list", "ticket/*"], cwd=ws, capture_output=True, text=True).stdout
    built = [t for t in co.tickets.search(p["id"]) if t["origin"] != "stage"]
    assert {t["status"] for t in built} == {"done"}


def test_one_ticket_at_a_time_per_agent(tmp_path):
    provider = Timed()
    co = company(tmp_path, provider)
    p = plan_approved(co)
    add_tickets(co, p["id"], 3, role="frontend_engineer")      # one frontend engineer: no overlap among these
    co.pipeline.advance(p["id"])
    fe = co.org.staff(role="frontend_engineer")[0]
    starts = co.db.all("SELECT task_id, body FROM ticket_comments c JOIN tasks t ON t.id=c.task_id "
                       "WHERE t.role='frontend_engineer' AND c.body LIKE 'Started%' ORDER BY c.id")
    assert len(starts) == 3
    assert {t["assignee_id"] for t in co.tickets.search(p["id"]) if t["role"] == "frontend_engineer"} == {fe["id"]}


def test_max_parallel_one_works_sequentially(tmp_path):
    provider = Timed()
    co = company(tmp_path, provider, max_parallel=1)
    p = plan_approved(co)
    add_tickets(co, p["id"], 2, role="frontend_engineer")
    add_tickets(co, p["id"], 2, role="backend_engineer")
    assert co.pipeline.advance(p["id"])["stage"] == "release_approval"
    assert provider.peak == 1


def test_merge_conflict_sends_ticket_back_to_redo_on_latest_code(tmp_path):
    co = company(tmp_path, Timed(shared_file=True))
    p = plan_approved(co)
    add_tickets(co, p["id"], 1, role="frontend_engineer")
    add_tickets(co, p["id"], 1, role="backend_engineer")
    assert co.pipeline.advance(p["id"])["stage"] == "release_approval"
    notes = [r["body"] for r in co.db.all("SELECT body FROM ticket_comments")]
    assert any("redo alone on the latest code" in n and "NOTES.md" in n for n in notes)
    built = [t for t in co.tickets.search(p["id"]) if t["origin"] != "stage"]
    assert {t["status"] for t in built} == {"done"}
    assert all(t["attempts"] == 0 for t in built)           # a conflict is not held against the agent
    assert not co.pipeline._solo


def test_workload_raises_a_hiring_request_for_the_right_boss(tmp_path):
    co = company(tmp_path, MockProvider(), max_parallel=1)
    p = plan_approved(co)
    backend = len(co.org.staff(role="backend_engineer"))
    add_tickets(co, p["id"], co.s.hire_when_waiting * backend)
    co.pipeline._check_staffing(p["id"])
    co.pipeline._check_staffing(p["id"])                       # no duplicate while one is pending
    [ask] = [a for a in co.pipeline.inbox() if a["kind"] == "hire"]
    assert ask["required_role"] == "cto" and "Backend Engineer" in ask["title"]
    with pytest.raises(PermissionError):
        co.pipeline.decide(ask["id"], "ceo", "approved")
    co.pipeline.decide(ask["id"], "cto", "approved")
    assert len(co.org.staff(role="backend_engineer")) == backend + 1


def test_declined_hiring_request_is_not_repeated_until_the_queue_grows(tmp_path):
    co = company(tmp_path, MockProvider())
    p = co.pipeline.create_project("Product", "Build a library")
    add_tickets(co, p["id"], 3, role="ux_designer")             # Design reports to the CEO
    co.pipeline._check_staffing(p["id"])
    [ask] = [a for a in co.pipeline.inbox() if a["kind"] == "hire"]
    assert ask["required_role"] == "ceo"
    co.pipeline.decide(ask["id"], "ceo", "rejected")            # no reason needed to decline a hire
    co.pipeline._check_staffing(p["id"])
    assert not [a for a in co.pipeline.inbox() if a["kind"] == "hire"]
    add_tickets(co, p["id"], 1, role="ux_designer")
    co.pipeline._check_staffing(p["id"])
    assert len([a for a in co.pipeline.inbox() if a["kind"] == "hire"]) == 1


def test_no_hiring_request_past_the_role_cap(tmp_path):
    co = company(tmp_path, MockProvider(), max_per_role=2)
    p = co.pipeline.create_project("Product", "Build a library")
    add_tickets(co, p["id"], 20)                                # two backend engineers already
    co.pipeline._check_staffing(p["id"])
    assert not [a for a in co.pipeline.inbox() if a["kind"] == "hire"]
