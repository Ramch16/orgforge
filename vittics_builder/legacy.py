"""Carrying things over from when Vittics Builder was called OrgForge (kept for one release).

- ORGFORGE_* environment variables still work: each is copied to its VITTICS_* name unless that is set.
- A company's `.orgforge/` data folder is renamed to `.vittics/` the first time it is opened. Project
  workspaces live outside it, so the paths stored in the database stay valid.
- A worker machine's ~/.orgforge/worker.json moves to ~/.vittics/worker.json.
- The desktop app keeps using ~/OrgForge if it exists (its workspaces' paths point there).
- The `orgforge` command still runs, with a note to use `vittics-builder`.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

OLD_DIR, NEW_DIR = ".orgforge", ".vittics"


def adopt_env() -> None:
    for key, value in list(os.environ.items()):
        if key.startswith("ORGFORGE_"):
            os.environ.setdefault("VITTICS_" + key[len("ORGFORGE_"):], value)


def migrate_company(root: Path) -> bool:
    """Rename root/.orgforge to root/.vittics once. Returns True if it moved."""
    old, new = Path(root) / OLD_DIR, Path(root) / NEW_DIR
    if old.is_dir() and not new.exists():
        old.rename(new)
        print(f"Vittics Builder: moved this company's data from {old} to {new}.", file=sys.stderr)
        return True
    return False


def migrate_file(new: Path, old: Path) -> None:
    if old.is_file() and not new.exists():
        new.parent.mkdir(parents=True, exist_ok=True)
        old.replace(new)


def desktop_home() -> Path:
    new, old = Path.home() / "VitticsBuilder", Path.home() / "OrgForge"
    return old if old.is_dir() and not new.exists() else new
