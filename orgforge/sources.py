"""Bring existing work in: clone a repository, read a GitHub issue."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path


class SourceError(ValueError):
    pass


ISSUE = re.compile(r"^(?:https://github\.com/)?([\w.-]+/[\w.-]+?)(?:#|/issues/)(\d+)/?$")
SLUG = re.compile(r"^[\w.-]+/[\w.-]+$")


def repo_url(ref: str) -> str:
    """A local path, a GitHub owner/name, or any git URL, as something `git clone` accepts."""
    ref = ref.strip()
    path = Path(ref).expanduser()
    if path.exists():
        if not (path / ".git").exists():
            raise SourceError(f"{path} is not a git repository.")
        return str(path.resolve())
    if SLUG.match(ref):
        return f"https://github.com/{ref}.git"
    if re.match(r"^(https?://|git@|ssh://)", ref):
        return ref
    raise SourceError(f"'{ref}' is not a folder, a GitHub owner/name, or a git URL.")


def clone(ref: str, dest: Path, branch: str) -> str:
    """Clone into dest and start a working branch. Returns the branch the work starts from."""
    url = repo_url(ref)
    proc = subprocess.run(["git", "clone", "-q", url, str(dest)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    if proc.returncode:
        shutil.rmtree(dest, ignore_errors=True)
        raise SourceError(f"Could not clone {ref}: {proc.stderr.strip()[:300]}"
                          + (" For a private GitHub repo, run `gh auth setup-git` once." if "github.com" in url else ""))
    base = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=dest, capture_output=True,
                          text=True, encoding="utf-8", errors="replace").stdout.strip() or "main"
    subprocess.run(["git", "checkout", "-q", "-b", branch], cwd=dest, check=True)
    return base


def github_issue(ref: str) -> dict:
    """Title, body and URL of a GitHub issue, via the gh CLI (so private repos work once you `gh auth login`)."""
    m = ISSUE.match(ref.strip())
    if not m:
        raise SourceError(f"'{ref}' is not an issue. Use owner/repo#123 or the issue's URL.")
    if not shutil.which("gh"):
        raise SourceError("Reading GitHub issues needs the GitHub CLI (gh). Install it and run `gh auth login`.")
    proc = subprocess.run(["gh", "issue", "view", m.group(2), "--repo", m.group(1), "--json",
                           "title,body,url,number,labels"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    if proc.returncode:
        raise SourceError(f"Could not read {ref}: {proc.stderr.strip()[:300]}")
    data = json.loads(proc.stdout)
    return {"repo": m.group(1), "number": data["number"], "title": data["title"], "body": data.get("body") or "",
            "url": data["url"], "labels": [label["name"] for label in data.get("labels", [])]}
