"""Everything the CEO and CTO need to review a product without a terminal.

What was built and why, how to run it, the acceptance checks and their real output, the QA
and audit verdicts, the documents and files, the history, and what it cost.
"""
from __future__ import annotations

import json
import re

from .costs import spent
from .pipeline import STAGE_LABELS
from .tools import ToolError

DOCS = [("Assessment", "docs/ASSESSMENT.md"), ("Plan of action", "docs/PLAN.md"), ("Requirements", "docs/PRD.md"),
        ("UX design", "docs/DESIGN.md"), ("Architecture", "docs/ARCHITECTURE.md"), ("README", "README.md"),
        ("Operations", "docs/OPERATIONS.md"), ("QA report", "docs/QA_REPORT.md"), ("Help guide", "docs/HELP.md")]
MAX_FILE = 300_000


def review(co, pid: int) -> dict:
    p = co.pipeline.project(pid)
    ws = co.pipeline.workspace(p)
    try:
        files = ws.git("ls-files").splitlines()
        history = ws.git("log", "--pretty=format:%h %ad %s", "--date=short", "-20").splitlines()
    except ToolError:
        files, history = [], []
    known = {path for _, path in DOCS}
    docs = [{"title": t, "path": path} for t, path in DOCS if path in files]
    docs += [{"title": f.removeprefix("docs/").removesuffix(".md").replace("/", ": ").replace("_", " "), "path": f}
             for f in files if f.startswith("docs/") and f.endswith(".md") and f not in known]

    def load(path: str):
        try:
            return json.loads(ws.resolve(path).read_text(encoding="utf-8")) if path in files else None
        except (OSError, ValueError, ToolError):
            return None

    contract, verification = load("product.json") or {}, load("docs/VERIFICATION.json")
    checks = []
    for c in (verification or {}).get("checks", []):
        checks.append({"id": c.get("id"), "requirement": c.get("requirement"), "passed": c.get("passed"),
                       "command": c.get("command"), "output": str(c.get("output", ""))[:3000]})
    verdicts = []
    for t in co.db.all("SELECT t.id, t.title, t.status, a.name AS who FROM tasks t LEFT JOIN agents a "
                       "ON a.id=t.assignee_id WHERE t.project_id=? AND origin='stage' AND (key='stage-release-qa' "
                       "OR key LIKE 'stage-audit-%') ORDER BY t.id", pid):
        last = co.db.one("SELECT body FROM ticket_comments WHERE task_id=? AND kind='comment' ORDER BY id DESC "
                         "LIMIT 1", t["id"])
        verdicts.append({**t, "verdict": last["body"] if last else "Not run yet."})
    counts = {r["status"]: r["n"] for r in co.db.all(
        "SELECT status, COUNT(*) AS n FROM tasks WHERE project_id=? GROUP BY status", pid)}
    return {"project": {"id": p["id"], "name": p["name"], "stage": p["stage"], "kind": p["kind"], "source": p["source"],
                        "branch": p["branch"], "base_branch": p["base_branch"],
                        "stage_label": STAGE_LABELS.get(p["stage"], p["stage"]), "purpose": p["purpose"],
                        "version": p["version"], "brief": p["brief"], "budget": p["budget"],
                        "spent": round(spent(co.db, pid), 4)},
            "run": {"setup": contract.get("setup", ""), "run": contract.get("run", "")},
            "verification": {"ran": verification is not None, "passed": bool((verification or {}).get("passed")),
                             "checked_at": (verification or {}).get("checked_at"),
                             "errors": (verification or {}).get("errors", []), "checks": checks},
            "verdicts": verdicts, "docs": docs, "files": files[:800], "history": history, "tickets": counts,
            "downloadable": p["stage"] in ("done", "live")}


def read_file(co, pid: int, path: str) -> dict:
    p = co.pipeline.project(pid)
    ws = co.pipeline.workspace(p)
    target = ws.resolve(path)
    if ".git" in target.relative_to(ws.root).parts or not target.is_file():
        raise ToolError(f"No file at '{path}'.")
    data = target.read_bytes()
    if len(data) > MAX_FILE:
        return {"path": path, "text": None, "note": f"Too large to show here ({len(data) // 1000} KB)."}
    try:
        return {"path": path, "text": data.decode("utf-8"), "note": ""}
    except UnicodeDecodeError:
        return {"path": path, "text": None, "note": "Binary file; download the release to open it."}


# ---- code changes ----------------------------------------------------------
MAX_DIFF = 200_000


def _clip_diff(text: str) -> str:
    return text if len(text) <= MAX_DIFF else text[:MAX_DIFF] + f"\n… diff shortened ({len(text) // 1000} KB in total)"


def ticket_diff(co, ticket_id: int) -> dict:
    """The commits made for a ticket (their messages carry its [key]) and their combined changes."""
    t = co.tickets.get(ticket_id)
    ws = co.pipeline.workspace(co.pipeline.project(t["project_id"]))
    try:
        shas = ws.git("log", "--all", "--format=%H", "--fixed-strings", f"--grep=[{t['key']}]").split()
        diff = ws.git("show", "--stat", "--patch", "--format=commit %h  %ad  %s", "--date=short", *shas) if shas else ""
    except ToolError:
        shas, diff = [], ""
    return {"ticket": t["ticket"], "commits": len(shas), "diff": _clip_diff(diff) or "No code changes recorded for this ticket."}


def commit_diff(co, pid: int, sha: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{4,40}", sha):
        raise ToolError("Not a commit id.")
    ws = co.pipeline.workspace(co.pipeline.project(pid))
    return {"sha": sha, "diff": _clip_diff(ws.git("show", "--stat", "--patch", "--format=commit %H%nAuthor: %an%nDate:   %ad%n%n    %s", sha))}


def changes(co, pid: int) -> dict:
    """Everything a task changed against the branch it started from (or the last commit, for a new product)."""
    p = co.pipeline.project(pid)
    ws = co.pipeline.workspace(p)
    base = p["base_branch"]
    diff = ws.git("diff", "--stat", "--patch", f"{base}...HEAD") if base else ws.git("show", "--stat", "--patch", "HEAD")
    return {"base": base, "branch": p["branch"], "diff": _clip_diff(diff)}


def patch_file(co, pid: int) -> tuple[str, str]:
    """A task's commits as a patch you can `git am` into your own repository."""
    p = co.pipeline.project(pid)
    if p["kind"] != "task" or not p["base_branch"]:
        raise ToolError("Patches are for tasks on an existing repository; download the release for a product.")
    text = co.pipeline.workspace(p).git("format-patch", "--stdout", f"{p['base_branch']}..HEAD")
    return f"orgforge-task-{pid}.patch", text + ("\n" if text else "")
