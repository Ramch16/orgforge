"""Everything the CEO and CTO need to review a product without a terminal.

What was built and why, how to run it, the acceptance checks and their real output, the QA
and audit verdicts, the documents and files, the history, and what it cost.
"""
from __future__ import annotations

import json

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
            return json.loads(ws.resolve(path).read_text()) if path in files else None
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
    return {"project": {"id": p["id"], "name": p["name"], "stage": p["stage"],
                        "stage_label": STAGE_LABELS.get(p["stage"], p["stage"]), "purpose": p["purpose"],
                        "version": p["version"], "brief": p["brief"], "budget": p["budget"],
                        "spent": round(spent(co.db, pid), 4)},
            "run": {"setup": contract.get("setup", ""), "run": contract.get("run", "")},
            "verification": {"ran": verification is not None, "passed": bool((verification or {}).get("passed")),
                             "checked_at": (verification or {}).get("checked_at"),
                             "errors": (verification or {}).get("errors", []), "checks": checks},
            "verdicts": verdicts, "docs": docs, "files": files[:800], "history": history, "tickets": counts,
            "downloadable": p["stage"] == "done"}


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
