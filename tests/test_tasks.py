import subprocess

import pytest
from fastapi.testclient import TestClient

from orgforge.pipeline import PipelineError
from orgforge.review import changes, patch_file, ticket_diff
from orgforge.server import create_app
from orgforge.sources import ISSUE, SourceError, repo_url


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "their-app"
    r.mkdir()
    git = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=r, check=True,
                                    capture_output=True)
    git("init", "-q", "-b", "main")
    (r / "app.py").write_text("def greet(name):\n    return 'Hi ' + name\n")
    (r / "dist").mkdir()
    (r / "dist" / "bundle.js").write_text("// their committed build\n")
    git("add", "-A")
    git("commit", "-qm", "Their first commit")
    return r


def test_task_on_an_existing_repo(co, repo):
    p = co.pipeline.create_task("Fix empty names", "greet('') should say Hi there.", "Lucky", repo=str(repo),
                                checks=["python -c 'import app'"])
    assert (p["kind"], p["stage"], p["branch"], p["base_branch"]) == ("task", "build", "orgforge/task-1", "main")
    assert co.pipeline.advance(p["id"])["stage"] == "release_approval"
    [review] = co.pipeline.inbox("cto")
    assert review["kind"] == "task_review" and "PASS  python -c 'import app'" in review["summary"]
    assert "orgforge/task-1 (from main)" in review["summary"]
    co.pipeline.decide(review["id"], "cto", "approved")
    assert co.pipeline.project(p["id"])["stage"] == "done"
    ws = p["workspace"]
    files = subprocess.run(["git", "ls-files"], cwd=ws, capture_output=True, text=True).stdout
    assert "dist/bundle.js" in files and ".gitignore" not in files            # their repo's files are untouched
    assert subprocess.run(["git", "log", "--oneline", "main"], cwd=ws, capture_output=True, text=True).stdout.count("\n") == 1
    name, text = patch_file(co, p["id"])
    assert name == "orgforge-task-1.patch" and text.startswith("From ") and "Subject: [PATCH" in text
    assert "+def task():" in changes(co, p["id"])["diff"]
    assert "+def task():" in ticket_diff(co, 1)["diff"]


def test_failing_checks_send_the_task_back(co, repo):
    co.s.max_rework = 1
    p = co.pipeline.create_task("Break it", "x", "Lucky", repo=str(repo), checks=["python -c 'raise SystemExit(3)'"])
    assert co.pipeline.advance(p["id"])["stage"] == "release_blocked"
    repair = next(t for t in co.tickets.search(p["id"]) if t["key"] == "verify-1")
    assert repair["title"] == "Make the failing checks pass" and "product.json" not in repair["description"]


def test_task_without_a_repo_and_bad_inputs(co):
    p = co.pipeline.create_task("Write a script", "A script that prints hello.", "Niki")
    assert p["source"] == "" and co.pipeline.advance(p["id"])["stage"] == "release_approval"
    with pytest.raises(PipelineError, match="title and a description"):
        co.pipeline.create_task("", "", "Niki")
    with pytest.raises(PipelineError, match="not a folder"):
        co.pipeline.create_task("x", "y", "Niki", repo="not a repo at all")
    with pytest.raises(PipelineError, match="staffed builder"):
        co.pipeline.create_task("x", "y", "Niki", role="code_reviewer")
    assert not co.db.one("SELECT 1 FROM projects WHERE name='x'")         # a failed clone leaves nothing behind


def test_source_parsing(tmp_path):
    assert repo_url("Ramch16/orgforge") == "https://github.com/Ramch16/orgforge.git"
    assert repo_url("https://gitlab.com/a/b.git") == "https://gitlab.com/a/b.git"
    (tmp_path / "plain").mkdir()
    with pytest.raises(SourceError, match="not a git repository"):
        repo_url(str(tmp_path / "plain"))
    assert ISSUE.match("owner/repo#12").groups() == ("owner/repo", "12")
    assert ISSUE.match("https://github.com/owner/repo/issues/7").groups() == ("owner/repo", "7")


def test_every_run_is_logged_and_steering_reaches_the_agent(co):
    p = co.pipeline.create_project("Greeter", "A tiny library.")
    hari = co.org.agent("Hari")
    co.runs.steer(hari["id"], "Lucky", "Use type hints everywhere.")               # sent between runs
    seen = []
    original = co.runtime.provider.complete
    def spy(**kw):
        seen.append(kw["messages"])
        if len(seen) == 1:                                                        # mid-run: Niki steps in
            co.runs.steer(hari["id"], "Niki", "Also add a docstring.")
        return original(**kw)
    co.runtime.provider.complete = spy
    co.runtime.run(hari, "Build the greeter", co.pipeline.workspace(p), project_id=p["id"], meta={"task_key": "core"})
    assert "Message from Lucky while you work" in seen[0][0]["content"]
    blocks = [b for m in seen[-1] if isinstance(m["content"], list) for b in m["content"]]   # the whole conversation
    assert any(b.get("type") == "text" and "Message from Niki" in b.get("text", "") for b in blocks)
    [run] = co.runs.recent(hari["id"])
    kinds = [e["kind"] for e in co.runs.transcript(run["id"])["events"]]
    assert run["status"] == "done" and kinds[0] == "instruction" and "tool" in kinds and "output" in kinds
    assert kinds.count("steer") == 2 and kinds[-1] == "result"
    assert not co.db.one("SELECT 1 FROM steers WHERE delivered_at IS NULL")


def test_tasks_runs_and_diffs_over_the_api(co, repo):
    client = TestClient(create_app(co, {"ceo": "c", "cto": "t"}))
    cto = {"X-Token": "t"}
    p = client.post("/api/tasks", json={"title": "Tidy", "description": "Tidy app.py", "repo": str(repo)}, headers=cto).json()
    assert p["kind"] == "task"
    for _ in range(100):
        if co.pipeline.project(p["id"])["stage"] == "release_approval":
            break
        import time; time.sleep(0.05)
    assert "+def task():" in client.get(f"/api/projects/{p['id']}/changes", headers=cto).json()["diff"]
    patch = client.get(f"/api/projects/{p['id']}/patch", headers=cto)
    assert patch.status_code == 200 and "attachment" in patch.headers["content-disposition"]
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=p["workspace"], capture_output=True, text=True).stdout.strip()
    assert client.get(f"/api/projects/{p['id']}/commits/{sha}", headers=cto).status_code == 200
    assert client.get(f"/api/projects/{p['id']}/commits/HEAD;rm", headers=cto).status_code == 400
    hari = co.org.agent("Hari")["id"]
    runs = client.get(f"/api/agents/{hari}/runs", headers=cto).json()["runs"]
    assert runs and client.get(f"/api/runs/{runs[0]['id']}", headers=cto).json()["events"]
    assert client.post(f"/api/agents/{hari}/steer", json={"body": "  "}, headers=cto).status_code == 400
    assert client.post(f"/api/agents/{hari}/steer", json={"body": "Keep it small"}, headers=cto).json()["live"] is False
    assert client.post("/api/tasks", json={"title": "x"}, headers=cto).status_code == 400
