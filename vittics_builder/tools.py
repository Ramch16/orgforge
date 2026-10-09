"""The tools agents work with. Everything is confined to a project workspace."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

MAX_OUTPUT = 12_000
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".pytest_cache", "dist", "build"}
SECRET_ENV = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", re.I)
# Agents work in parallel git worktrees that share one object store. Git bookkeeping is
# quick next to agent work, so it is serialised rather than risking ref-lock races.
_GIT_LOCK = threading.RLock()

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
    # Windows (cmd.exe, PowerShell)
    (r"(?i)\b(format|diskpart|bcdedit|vssadmin|cipher\s+/w)\b", "system command"),
    (r"(?i)\b(rd|rmdir|del|erase)\s+(/\w\s+)*[a-z]:\\?\s*($|/|\*)", "deleting outside the workspace"),
    (r"(?i)remove-item\b.*\b[a-z]:\\(\*|\s|$)", "deleting outside the workspace"),
    (r"(?i)\b(reg\s+(add|delete)|set-executionpolicy|runas)\b", "system or privilege change"),
    (r"(?i)(%systemroot%|%windir%|[a-z]:\\windows\\|%userprofile%\\\.ssh|\$env:userprofile\\\.ssh)",
     "touching system or credential paths"),
    (r"(?i)(iwr|irm|invoke-webrequest|invoke-restmethod)\b[^|;&]*\|\s*iex\b", "piping a download into a shell"),
]


GITIGNORE = [".vittics/browser/", "__pycache__/", "*.py[cod]", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/", ".coverage", "htmlcov/",
             ".venv/", "venv/", "node_modules/", "dist/", "*.egg-info/", ".next/", ".cache/", "*.log", ".DS_Store"]


class ToolError(RuntimeError):
    pass


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


_STR = {"type": "string"}

TOOL_SPECS: dict[str, dict] = {
    "run_approved_checks": {"description": "Run selected CTO-approved product.json checks through Vittics on this computer, outside the coding CLI sandbox. Use when local execution is blocked; results are runtime evidence.",
        "input_schema": {"type": "object", "properties": {"ids": {"type": "array", "items": {"type": "string"}, "minItems": 1}}, "required": ["ids"], "additionalProperties": False}},
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
    "submit_strategy": {
        "description": "Propose a strategy (an approach the team can add to its assignments) for the CTO to approve. "
                       "Call exactly once.",
        "input_schema": _obj(
            {"name": {"type": "string", "description": "Short lowercase name with dashes, e.g. failing-test-first"},
             "prompt": {"type": "string", "description": "The approach, as instructions to the engineer (40-1500 characters)"},
             "why": {"type": "string", "description": "One or two sentences: which failures it prevents"}},
            ["name", "prompt", "why"],
        ),
    },
    "submit_assessment": {
        "description": "Submit your assessment of an idea, after writing docs/ASSESSMENT.md. Call exactly once.",
        "input_schema": _obj(
            {"recommendation": {"type": "string", "enum": ["build_internal", "build_to_sell", "park", "drop"]},
             "feasibility": {"type": "string", "enum": ["achievable", "achievable_with_risks", "not_achievable"]},
             "summary": {"type": "string", "description": "Three to six sentences the CEO and CTO can decide on"}},
            ["recommendation", "feasibility", "summary"],
        ),
    },
    "submit_department_plan": {
        "description": "Submit the plan of action, after writing docs/PLAN.md, with a ticket for each piece of "
                       "department work beyond the standard requirements, design, build, QA and audits. Call once.",
        "input_schema": _obj(
            {"summary": {"type": "string", "description": "The plan in a few sentences"},
             "tickets": {"type": "array", "items": _obj(
                 {"role": {"type": "string", "description": "Role id that owns it, e.g. support_specialist"},
                  "title": _STR,
                  "description": {"type": "string", "description": "What to deliver and how to tell it is done"},
                  "after_build": {"type": "boolean", "description": "True if it needs the finished product"}},
                 ["role", "title", "description", "after_build"])}},
            ["summary", "tickets"],
        ),
    },
    "close_as_answered": {
        "description": "Close a ticket that only asks a question or for information and needs no work, after "
                       "answering it in your reply. Never use it for a ticket that asks for work.",
        "input_schema": _obj({"reason": {"type": "string", "description": "Why no work is needed"}}, ["reason"]),
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


INTEGRATION_SPECS = {
    'mcp_list_tools': {
        'description': 'List the tools permitted on a configured MCP server.',
        'input_schema': {'type': 'object', 'properties': {'server': {'type': 'string'}}, 'required': ['server']},
    },
    'mcp_call': {
        'description': 'Call a company-configured MCP tool. Inspect its schema with mcp_list_tools first.',
        'input_schema': {'type': 'object', 'properties': {
            'server': {'type': 'string'}, 'tool': {'type': 'string'}, 'arguments': {'type': 'object'}},
            'required': ['server', 'tool', 'arguments']},
    },
    'browser_journey': {
        'description': 'Use the application in a fresh browser: navigate, fill, click, assert text and save screenshots. '
                       'A journey retains login state between steps. Returns page text and screenshot paths.',
        'input_schema': {'type': 'object', 'properties': {
            'url': {'type': 'string'}, 'steps': {'type': 'array', 'maxItems': 30, 'items': {
                'type': 'object', 'properties': {
                    'action': {'type': 'string', 'enum': ['click', 'fill', 'assert_text', 'screenshot', 'goto']},
                    'selector': {'type': 'string'}, 'value': {'type': 'string'}, 'path': {'type': 'string'}},
                'required': ['action']}}}, 'required': ['url']},
    },
}

TOOL_SPECS.update(INTEGRATION_SPECS)
TOOL_SPECS['submit_evaluation'] = {'description':'Submit an independent board assessment with eight evidence-based scores and findings.',
 'input_schema':{'type':'object','properties':{
 'scores':{'type':'object','properties':{d:{'type':'number','minimum':0,'maximum':100} for d in
 ('functionality','ux','security','cost','architecture','scalability','customer_value','maintainability')},
 'required':['functionality','ux','security','cost','architecture','scalability','customer_value','maintainability'],'additionalProperties':False},
 'findings':{'type':'array','items':{'type':'object','properties':{'title':{'type':'string'},'body':{'type':'string'},
 'severity':{'type':'string','enum':['info','warning','error','critical']}},'required':['title','body','severity']}}},
 'required':['scores','findings']}}


TOOL_SPECS['submit_journey'] = {'description':'Propose a browser journey for your customer persona. It will be independently executed; include an assert_text step.',
 'input_schema':{'type':'object','properties':{'rationale':{'type':'string'},'steps':{'type':'array','minItems':1,'maxItems':30,
 'items':{'type':'object','properties':{'action':{'type':'string','enum':['click','fill','assert_text','screenshot','goto']},
 'selector':{'type':'string'},'value':{'type':'string'},'path':{'type':'string'}},'required':['action']}}},'required':['steps','rationale']}}


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
        return _clip(target.read_text(encoding="utf-8", errors="replace"))

    def write_file(self, path: str, content: str) -> str:
        target = self.resolve(path)
        if target == self.root or ".git" in target.relative_to(self.root).parts:
            raise ToolError(f"'{path}' is not a writable file path.")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="")   # exactly what the agent wrote
        return f"Wrote {path} ({len(content)} characters)."

    def replace_in_file(self, path: str, old: str, new: str) -> str:
        target = self.resolve(path)
        if not target.is_file():
            raise ToolError(f"No file at '{path}'.")
        text = target.read_text(encoding="utf-8")
        count = text.count(old)
        if count != 1:
            raise ToolError(f"`old` occurs {count} times in '{path}'; it must occur exactly once.")
        target.write_text(text.replace(old, new, 1), encoding="utf-8", newline="")
        return f"Edited {path}."

    def list_files(self, path: str = ".") -> str:
        base = self.resolve(path)
        if not base.is_dir():
            raise ToolError(f"No folder at '{path}'.")
        found = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for name in sorted(filenames):
                found.append((Path(dirpath) / name).relative_to(self.root).as_posix())
                if len(found) >= 400:
                    return "\n".join(found) + "\n... (more files not shown)"
        return "\n".join(found) or "(empty)"

    def run_command(self, command: str, timeout_seconds: int | None = None, extra_env: dict | None = None,
                    max_seconds: int | None = None) -> str:
        """Run a command in the workspace. `extra_env` (the product's own keys) and `max_seconds` (a limit above
        the sandbox's) are for acceptance checks only."""
        for pattern, why in DENIED:
            if re.search(pattern, command):
                raise ToolError(f"Command refused by company policy ({why}).")
        cap = int(max_seconds or self.timeout)
        timeout = min(int(timeout_seconds or cap), cap)
        env = {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}
        env.update(extra_env or {})
        if self.mode == "docker":
            argv = ["docker", "run", "--rm", "-v", f"{self.root}:/work", "-w", "/work"]
            for name in extra_env or {}:            # -e NAME passes the value from our environment, not the command line
                argv += ["-e", name]
            if not self.docker_network:
                argv += ["--network", "none"]
            argv += [self.docker_image, "sh", "-lc", command]
            kwargs = dict(args=argv)
        else:
            from .platforms import shell_command
            args, shell = shell_command(command)
            kwargs = dict(args=args, shell=shell, cwd=self.root)
        try:
            proc = subprocess.run(capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env, **kwargs)
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
        with _GIT_LOCK:
            proc = subprocess.run(
                ["git", "-c", "user.name=Vittics Builder", "-c", "user.email=vittics@localhost", *args],
                cwd=self.root, capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
        if proc.returncode:
            raise ToolError(f"Git {args[0]} failed: {_clip(proc.stderr)}")
        return (proc.stdout + proc.stderr).strip()

    def init_repo(self) -> None:
        if not (self.root / ".git").exists():
            self.git("init", "-q", "-b", "main")
            if os.name == "nt":                # workspaces nest deeply; Windows' default limit is 260 characters
                self.git("config", "core.longpaths", "true")
        self.ensure_ignores()

    def ensure_ignores(self, shared: bool = True) -> bool:
        """Keep caches and build output out of the product's history: committed, they differ between parallel
        tickets and make merges conflict. Returns True if the .gitignore changed. For someone else's repository
        (shared=False) the patterns go in .git/info/exclude, so their files and history are left alone."""
        if not shared:
            exclude = self.root / ".git" / "info" / "exclude"
            have = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
            missing = [p for p in GITIGNORE if p not in have]
            if missing:
                exclude.parent.mkdir(parents=True, exist_ok=True)
                exclude.write_text("\n".join(have + missing) + "\n", encoding="utf-8")
            return False
        target = self.root / ".gitignore"
        have = target.read_text(encoding="utf-8").splitlines() if target.exists() else []
        missing = [p for p in GITIGNORE if p not in have]
        if not missing:
            return False
        target.write_text("\n".join(have + (["# Added by Vittics Builder"] if have else []) + missing) + "\n", encoding="utf-8")
        with _GIT_LOCK:
            tracked = self.git("ls-files", "-ci", "--exclude-standard").splitlines()
            if tracked:
                self.git("rm", "-r", "-q", "--cached", "--", *tracked)
        return True

    def changed_files(self) -> str:
        return self.git("status", "--porcelain") or "(no uncommitted changes)"

    def commit(self, message: str) -> None:
        with _GIT_LOCK:
            self.git("add", "-A")
            self.git("commit", "-q", "-m", message, "--allow-empty")

    # ---- parallel work: one git worktree per ticket ------------------------
    def add_worktree(self, path: Path, branch: str) -> None:
        """A separate working copy of the current main code on its own branch."""
        with _GIT_LOCK:
            self.remove_worktree(path, branch)
            path.parent.mkdir(parents=True, exist_ok=True)
            self.git("worktree", "add", "-q", "-b", branch, str(path), "HEAD")

    def remove_worktree(self, path: Path, branch: str) -> None:
        with _GIT_LOCK:
            if path.exists():
                try:
                    self.git("worktree", "remove", "--force", str(path))
                except ToolError:
                    shutil.rmtree(path, ignore_errors=True)
            self.git("worktree", "prune")
            if self.git("branch", "--list", branch):
                self.git("branch", "-D", branch)

    def merge(self, branch: str, message: str) -> list[str]:
        """Merge a ticket branch into the current branch. Returns conflicting files (merge undone) or []."""
        with _GIT_LOCK:
            try:
                self.git("merge", "--no-ff", "-m", message, branch)
                return []
            except ToolError:
                conflicts = self.git("diff", "--name-only", "--diff-filter=U").splitlines()
                try:
                    self.git("merge", "--abort")
                except ToolError:
                    pass                        # the merge never started, so there is nothing to undo
                return conflicts or ["(merge failed)"]
