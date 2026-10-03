"""The tools agents work with. Everything is confined to a project workspace."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

MAX_OUTPUT = 12_000
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".pytest_cache", "dist", "build"}
SECRET_ENV = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", re.I)

# Commands refused even inside the workspace. This is a seatbelt, not a
# sandbox: use sandbox.mode: docker for real isolation.
DENIED = [
    (r"\bsudo\b|\bsu\s+-", "privilege escalation"),
    (r"\brm\s+(-\w+\s+)*(/|~|\$HOME|/\*)(\s|$)", "deleting outside the workspace"),
    (r"\b(shutdown|reboot|halt|poweroff|mkfs\S*)\b", "system command"),
    (r"\bdd\s+.*of=/dev/", "raw device write"),
    (r":\(\)\s*\{", "fork bomb"),
    (r"(curl|wget)\b[^|;&]*\|\s*(sudo\s+)?(ba|z)?sh\b", "piping a download into a shell"),
    (r"(^|\s)(/etc/|/var/|/usr/|/root/|~/\.ssh|\$HOME/\.ssh)", "touching system or credential paths"),
    (r"\.\./\.\.", "leaving the workspace"),
    (r"\bgit\s+push\b", "pushing (a human publishes releases)"),
]


class ToolError(RuntimeError):
    pass


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


_STR = {"type": "string"}

TOOL_SPECS: dict[str, dict] = {
    "read_file": {
        "description": "Read a text file from the project workspace.",
        "input_schema": _obj({"path": _STR}, ["path"]),
    },
    "write_file": {
        "description": "Create or overwrite a file in the project workspace. Parent folders are created.",
        "input_schema": _obj({"path": _STR, "content": _STR}, ["path", "content"]),
    },
    "replace_in_file": {
        "description": "Replace one exact occurrence of `old` with `new` in a file. Fails if `old` is missing or not unique.",
        "input_schema": _obj({"path": _STR, "old": _STR, "new": _STR}, ["path", "old", "new"]),
    },
    "list_files": {
        "description": "List files under a workspace folder, recursively.",
        "input_schema": _obj({"path": {"type": "string", "description": "Folder, default '.'"}}, []),
    },
    "run_command": {
        "description": "Run a shell command from the workspace root (build, test, install, git). Returns exit code and output.",
        "input_schema": _obj(
            {"command": _STR, "timeout_seconds": {"type": "integer", "description": "Optional, capped by company policy"}},
            ["command"],
        ),
    },
    "delegate": {
        "description": "Hand a self-contained sub-task to a colleague in another role and get their summary back.",
        "input_schema": _obj({"role": {"type": "string", "description": "Role id, e.g. frontend_engineer"}, "instructions": _STR},
                             ["role", "instructions"]),
    },
    "submit_plan": {
        "description": "Submit the build plan as an ordered list of tasks. Call once, after writing the architecture document.",
        "input_schema": _obj(
            {"tasks": {"type": "array", "items": _obj(
                {"key": {"type": "string", "description": "Short unique id, e.g. api-auth"},
                 "title": _STR,
                 "description": {"type": "string", "description": "What to build and the acceptance criteria"},
                 "role": {"type": "string", "description": "Role id that should do it"},
                 "depends_on": {"type": "array", "items": _STR, "description": "Keys of tasks that must finish first"}},
                ["key", "title", "description", "role"])}},
            ["tasks"],
        ),
    },
    "submit_review": {
        "description": "Submit your verdict on the work you were asked to check. Call exactly once.",
        "input_schema": _obj(
            {"score": {"type": "integer", "minimum": 0, "maximum": 100},
             "verdict": {"type": "string", "enum": ["approve", "request_changes"]},
             "notes": {"type": "string", "description": "Concrete findings the author can act on"}},
            ["score", "verdict", "notes"],
        ),
    },
}


def tool_schema(name: str) -> dict:
    return {"name": name, **TOOL_SPECS[name]}


def _clip(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    half = MAX_OUTPUT // 2
    return f"{text[:half]}\n... [{len(text) - MAX_OUTPUT} characters omitted] ...\n{text[-half:]}"


class Workspace:
    """A project's folder. All agent file and command access goes through here."""

    def __init__(self, root: Path | str, *, mode: str = "local", docker_image: str = "python:3.12-slim",
                 docker_network: bool = False, timeout: int = 300) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.mode, self.docker_image, self.docker_network, self.timeout = mode, docker_image, docker_network, timeout

    def resolve(self, path: str) -> Path:
        target = (self.root / (path or ".")).resolve()
        if target != self.root and self.root not in target.parents:
            raise ToolError(f"'{path}' is outside the project workspace.")
        return target

    def read_file(self, path: str) -> str:
        target = self.resolve(path)
        if not target.is_file():
            raise ToolError(f"No file at '{path}'.")
        return _clip(target.read_text(errors="replace"))

    def write_file(self, path: str, content: str) -> str:
        target = self.resolve(path)
        if target == self.root or ".git" in target.relative_to(self.root).parts:
            raise ToolError(f"'{path}' is not a writable file path.")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return f"Wrote {path} ({len(content)} characters)."

    def replace_in_file(self, path: str, old: str, new: str) -> str:
        target = self.resolve(path)
        if not target.is_file():
            raise ToolError(f"No file at '{path}'.")
        text = target.read_text()
        count = text.count(old)
        if count != 1:
            raise ToolError(f"`old` occurs {count} times in '{path}'; it must occur exactly once.")
        target.write_text(text.replace(old, new, 1))
        return f"Edited {path}."

    def list_files(self, path: str = ".") -> str:
        base = self.resolve(path)
        if not base.is_dir():
            raise ToolError(f"No folder at '{path}'.")
        found = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for name in sorted(filenames):
                found.append(str((Path(dirpath) / name).relative_to(self.root)))
                if len(found) >= 400:
                    return "\n".join(found) + "\n... (more files not shown)"
        return "\n".join(found) or "(empty)"

    def run_command(self, command: str, timeout_seconds: int | None = None) -> str:
        for pattern, why in DENIED:
            if re.search(pattern, command):
                raise ToolError(f"Command refused by company policy ({why}).")
        timeout = min(int(timeout_seconds or self.timeout), self.timeout)
        env = {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}
        if self.mode == "docker":
            argv = ["docker", "run", "--rm", "-v", f"{self.root}:/work", "-w", "/work"]
            if not self.docker_network:
                argv += ["--network", "none"]
            argv += [self.docker_image, "sh", "-lc", command]
            kwargs = dict(args=argv)
        else:
            kwargs = dict(args=command, shell=True, cwd=self.root)
        try:
            proc = subprocess.run(capture_output=True, text=True, timeout=timeout, env=env, **kwargs)
        except subprocess.TimeoutExpired:
            raise ToolError(f"Command timed out after {timeout}s.")
        except FileNotFoundError as exc:
            raise ToolError(f"Could not start the command: {exc}")
        output = (proc.stdout + ("\n" + proc.stderr if proc.stderr else "")).strip()
        return _clip(f"exit code {proc.returncode}\n{output}")

    def call(self, name: str, args: dict) -> str:
        if name == "read_file":
            return self.read_file(args["path"])
        if name == "write_file":
            return self.write_file(args["path"], args["content"])
        if name == "replace_in_file":
            return self.replace_in_file(args["path"], args["old"], args["new"])
        if name == "list_files":
            return self.list_files(args.get("path") or ".")
        if name == "run_command":
            return self.run_command(args["command"], args.get("timeout_seconds"))
        raise ToolError(f"Unknown tool '{name}'.")

    # Git bookkeeping is done by the company, not by agents.
    def git(self, *args: str) -> str:
        proc = subprocess.run(
            ["git", "-c", "user.name=OrgForge", "-c", "user.email=orgforge@localhost", *args],
            cwd=self.root, capture_output=True, text=True,
        )
        if proc.returncode:
            raise ToolError(f"Git {args[0]} failed: {_clip(proc.stderr)}")
        return (proc.stdout + proc.stderr).strip()

    def init_repo(self) -> None:
        if not (self.root / ".git").exists():
            self.git("init", "-q", "-b", "main")

    def changed_files(self) -> str:
        return self.git("status", "--porcelain") or "(no uncommitted changes)"

    def commit(self, message: str) -> None:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message, "--allow-empty")
