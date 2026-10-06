"""Machine readiness: what this computer needs for the company and its projects, what it has, and
installing what is missing, with consent, one tool at a time.

Detect → recommend → ask → install → verify. Only tools in CATALOG can be installed, through the
platform's package manager (Homebrew, winget) or npm, never arbitrary commands. Anything that needs
admin rights or a licence (Docker Desktop, Homebrew itself, Apple's command line tools, apt) is shown
as a command for you to run. Signing in to an AI CLI or GitHub is always yours to do.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import threading
from pathlib import Path

from .db import now

# id: name, binary, version command, minimum version, install recipes, sign-in check and how to sign in.
# Homebrew names checked with `brew info` (Homebrew 7.0, Oct 2026); npm names with `npm view`.
# winget ids are the publishers' documented ids, not yet run by OrgForge.
CATALOG: dict[str, dict] = {
    "git": {"name": "Git", "bin": "git", "version": ["git", "--version"], "min": "2.30",
            "brew": "git", "winget": "Git.Git", "apt": "git"},
    "python": {"name": "Python", "bin": "python3", "win_bin": "python", "version": ["{bin}", "--version"], "min": "3.10",
               "brew": "python@3.12", "winget": "Python.Python.3.12", "apt": "python3"},
    "node": {"name": "Node.js", "bin": "node", "version": ["node", "--version"], "min": "20",
             "brew": "node", "winget": "OpenJS.NodeJS.LTS", "apt": "nodejs"},
    "gh": {"name": "GitHub CLI", "bin": "gh", "version": ["gh", "--version"], "brew": "gh", "winget": "GitHub.cli",
           "apt": "gh", "login": ["gh", "auth", "status"], "sign_in": "gh auth login"},
    "docker": {"name": "Docker", "bin": "docker", "version": ["docker", "--version"], "ready": ["docker", "info"],
               "not_ready": "installed, but Docker is not running: open Docker Desktop",
               "brew_cask": "docker-desktop", "winget": "Docker.DockerDesktop", "admin": True,
               "note": "Docker Desktop needs admin rights, and a paid plan for companies over 250 people."},
    "go": {"name": "Go", "bin": "go", "version": ["go", "version"], "brew": "go", "winget": "GoLang.Go", "apt": "golang"},
    "rust": {"name": "Rust", "bin": "cargo", "version": ["cargo", "--version"], "brew": "rust", "winget": "Rustlang.Rustup"},
    "java": {"name": "Java (JDK)", "bin": "java", "version": ["java", "-version"], "brew": "openjdk",
             "winget": "Microsoft.OpenJDK.21", "apt": "default-jdk"},
    "postgresql": {"name": "PostgreSQL", "bin": "psql", "version": ["psql", "--version"], "brew": "postgresql@16",
                   "apt": "postgresql"},
    "uv": {"name": "uv (Python packages)", "bin": "uv", "version": ["uv", "--version"], "brew": "uv", "winget": "astral-sh.uv"},
    "claude-code": {"name": "Claude Code", "bin": "claude", "version": ["claude", "--version"], "npm": "@anthropic-ai/claude-code",
                    "login": ["claude", "auth", "status"], "login_json": "loggedIn",
                    "sign_in": "claude, then type /login", "engine": "claude-code"},
    "codex": {"name": "Codex", "bin": "codex", "version": ["codex", "--version"], "npm": "@openai/codex",
              "login": ["codex", "login", "status"], "login_text": "Logged in", "sign_in": "codex login", "engine": "codex",
              "bundled": "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"},
    "gemini": {"name": "Gemini CLI", "bin": "gemini", "version": ["gemini", "--version"], "npm": "@google/gemini-cli",
               "sign_in": "gemini, then choose Login with Google", "engine": "gemini"},
}
CORE = ("git", "python")
ENGINE_TOOLS = {t["engine"]: tid for tid, t in CATALOG.items() if t.get("engine")}
FILES = {"package.json": "node", "requirements.txt": "python", "pyproject.toml": "python", "setup.py": "python",
         "Dockerfile": "docker", "docker-compose.yml": "docker", "compose.yaml": "docker", "go.mod": "go",
         "Cargo.toml": "rust", "pom.xml": "java", "build.gradle": "java", "build.gradle.kts": "java"}
COMMANDS = {"npm": "node", "npx": "node", "node": "node", "pnpm": "node", "yarn": "node",
            "python": "python", "python3": "python", "pytest": "python", "pip": "python", "pip3": "python",
            "uv": "uv", "docker": "docker", "go": "go", "cargo": "rust", "java": "java", "mvn": "java",
            "gradle": "java", "psql": "postgresql", "pg_ctl": "postgresql", "gh": "gh", "git": "git"}
NOT_FOUND = re.compile(r"(?:command not found:\s*([\w.+-]+))|(?:([\w.+-]+): (?:command )?not found)|"
                       r"(?:'([\w.+-]+)' is not recognized)", re.I)
APPLE_STUBS = {"/usr/bin/git", "/usr/bin/python3"}     # ask to install Apple's tools instead of answering


def system() -> str:
    return {"Darwin": "macos", "Windows": "windows"}.get(platform.system(), "linux")


def version_tuple(text: str) -> tuple[int, ...]:
    m = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", text or "")
    return tuple(int(x) for x in m.groups() if x is not None) if m else ()


def _run(argv: list[str], timeout: int = 15) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)


def tokens(command: str) -> set[str]:
    """Programs a shell command runs: the first word of each simple command."""
    found = set()
    for part in re.split(r"&&|\|\||[;|]|\$\(|`", command or ""):
        words = [w for w in part.strip().split() if "=" not in w or w.startswith("-")]
        while words and words[0] in ("sudo", "env", "exec", "time", "cd") or (words and words[0].startswith("-")):
            words = words[2:] if words[0] == "cd" else words[1:]
        if words:
            found.add(os.path.basename(words[0].strip("'\"")))
    return found


def check_tool(tool: str) -> dict:
    """Installed? Which version? Running and signed in, where that can be checked?"""
    t = CATALOG[tool]
    binary = t.get("win_bin") if system() == "windows" and t.get("win_bin") else t["bin"]
    path = shutil.which(binary)
    out = {"id": tool, "name": t["name"], "installed": False, "version": "", "path": path or "",
           "ok": False, "state": "missing", "signed_in": None, "detail": ""}
    if not path and t.get("bundled") and Path(t["bundled"]).exists():
        out["detail"] = f"Installed but not on PATH. Run: ln -s '{t['bundled']}' ~/.local/bin/{binary}"
        return out
    if not path:
        return out
    if system() == "macos" and path in APPLE_STUBS and _run(["xcode-select", "-p"])[0] != 0:
        out["detail"] = "Needs Apple's command line tools. Run: xcode-select --install"
        return out
    code, text = _run([p.replace("{bin}", binary) for p in t["version"]])
    out["installed"] = code == 0 or bool(version_tuple(text))
    if not out["installed"]:
        out["detail"] = text[:200]
        return out
    found = version_tuple(text)
    out["version"] = ".".join(map(str, found))
    if t.get("min") and found and found < version_tuple(t["min"]):
        out.update(state="outdated", detail=f"Version {out['version']} is older than {t['min']}.")
        return out
    if t.get("ready") and _run(t["ready"], timeout=20)[0] != 0:
        out.update(state="not_ready", detail=t["not_ready"])
        return out
    if t.get("login"):
        code, text = _run(t["login"], timeout=20)
        if t.get("login_json"):
            try:
                signed = bool(json.loads(text).get(t["login_json"]))
            except ValueError:
                signed = False
        else:
            signed = code == 0 and (not t.get("login_text") or t["login_text"] in text)
        out["signed_in"] = signed
        if not signed:
            out.update(state="signed_out", detail=f"Sign in: run `{t['sign_in']}` in a terminal.")
            return out
    elif t.get("sign_in"):
        out["detail"] = f"Sign-in is not checked; if needed run `{t['sign_in']}`."
    out.update(ok=True, state="ready")
    return out


class Machine:
    def __init__(self, company) -> None:
        self.co, self.db, self.s = company, company.db, company.s
        self.installs_allowed = True              # off when OrgForge is hosted as a server (hosting.py)
        self._running: set[str] = set()
        self._lock = threading.Lock()

    @property
    def config(self) -> dict:
        return self.s.raw.get("machine") or {}

    # ---- what is needed ----------------------------------------------------
    def requirements(self, pid: int | None = None) -> dict[str, list[str]]:
        """tool id -> why it is needed. Company-wide, or for one project."""
        need: dict[str, list[str]] = {}

        def add(tool: str, why: str) -> None:
            if tool in CATALOG and why not in need.setdefault(tool, []):
                need[tool].append(why)
        if pid is None:
            add("git", "OrgForge itself (every project is a Git repository)")
            add("python", "the team's Python checks and scripts")
            if self.s.sandbox_mode == "docker":
                add("docker", "sandbox.mode is docker")
            models = {a["model"] for a in self.db.all("SELECT model FROM agents WHERE status!='fired'")}
            models |= set((self.s.raw.get("failover") or {}).get("models") or [])
            for model in sorted(models):
                if str(model).startswith("cli:"):
                    engine = str(model)[4:].split("/")[0]
                    if engine in ENGINE_TOOLS:
                        add(ENGINE_TOOLS[engine], f"agents work through {model}")
            users: dict[str, list[str]] = {}
            for p in self.db.all("SELECT id, name FROM projects WHERE stage NOT IN ('dropped','parked') ORDER BY id"):
                for tool in self.requirements(p["id"]):
                    users.setdefault(tool, []).append(f"P{p['id']} {p['name']}")
            for tool, names in users.items():                 # pick a project for the detailed reasons
                add(tool, ("project " if len(names) == 1 else "projects ") + ", ".join(names))
            return need
        project = self.co.pipeline.project(pid)
        root = Path(project["workspace"])
        for name, tool in FILES.items():
            if (root / name).is_file():
                add(tool, name)
        contract = root / "product.json"
        if contract.is_file():
            try:
                data = json.loads(contract.read_text())
                commands = [("setup", data.get("setup", ""))] + [(f"check {c.get('id')}", c.get("command", ""))
                                                                 for c in data.get("checks") or [] if isinstance(c, dict)]
            except (ValueError, AttributeError):
                commands = []
            for label, command in commands:
                for word in tokens(str(command)):
                    if word in COMMANDS:
                        add(COMMANDS[word], f"product.json {label} runs {word}")
        try:
            prod = self.co.production.config(project)
        except ValueError:                           # a broken production entry is reported when deploying
            prod = None
        for env in (prod or {}).get("environments", []):
            for key in ("deploy", "health", "rollback"):
                for word in tokens(env.get(key, "")):
                    if word in COMMANDS:
                        add(COMMANDS[word], f"{env['name']} {key} runs {word}")
        return need

    # ---- what is here ------------------------------------------------------
    def check(self, tool: str) -> dict:
        return check_tool(tool)

    def report(self, pid: int | None = None) -> dict:
        need = self.requirements(pid)
        tools = []
        for tool in sorted(need, key=lambda x: (x not in CORE, CATALOG[x]["name"].lower())):
            status = self.check(tool)
            status["needed_by"] = need[tool]
            status["install"] = self.recipe(tool)
            tools.append(status)
        return {"system": self.about(), "tools": tools, "ready": all(t["ok"] for t in tools),
                "installs_allowed": self.allowed(), "running": sorted(self._running),
                "history": self.db.all("SELECT * FROM machine_installs ORDER BY id DESC LIMIT 20")}

    def about(self) -> dict:
        root = self.s.root
        free = shutil.disk_usage(root).free if Path(root).exists() else 0
        managers = {m: bool(shutil.which(m)) for m in ("brew", "winget", "npm", "apt-get")}
        return {"os": system(), "release": platform.release(), "machine": platform.machine(),
                "cpus": os.cpu_count(), "free_disk_gb": round(free / 1e9, 1), "managers": managers}

    # ---- installing --------------------------------------------------------
    def recipe(self, tool: str) -> dict:
        """How this tool would be installed here: automatically, or a command for you to run."""
        t, os_name = CATALOG[tool], system()
        if t.get("npm"):
            if shutil.which("npm"):
                return {"auto": True, "argv": ["npm", "install", "-g", t["npm"]],
                        "how": f"npm install -g {t['npm']}", "then": t.get("sign_in")}
            return {"auto": False, "how": "Install Node.js first (it provides npm).", "needs": "node"}
        if os_name == "macos":
            if not shutil.which("brew"):
                return {"auto": False, "how": '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/'
                                              'install/HEAD/install.sh)"',
                        "why": "Homebrew installs developer tools on macOS; its installer asks for your password."}
            if t.get("brew_cask"):
                return {"auto": False, "how": f"brew install --cask {t['brew_cask']}", "why": t.get("note", "")}
            if t.get("brew"):
                return {"auto": True, "argv": ["brew", "install", t["brew"]], "how": f"brew install {t['brew']}",
                        "then": t.get("sign_in")}
        if os_name == "windows" and t.get("winget") and shutil.which("winget"):
            argv = ["winget", "install", "--id", t["winget"], "-e", "--accept-package-agreements",
                    "--accept-source-agreements"]
            if t.get("admin"):
                return {"auto": False, "how": " ".join(argv), "why": t.get("note", "")}
            return {"auto": True, "argv": argv, "how": " ".join(argv), "then": t.get("sign_in")}
        if os_name == "linux" and t.get("apt"):
            return {"auto": False, "how": f"sudo apt-get install -y {t['apt']}", "why": "apt needs administrator rights."}
        return {"auto": False, "how": f"Install {t['name']} from its official website.", "why": ""}

    def allowed(self) -> bool:
        return self.installs_allowed and self.config.get("installs", True) is not False

    def install(self, tool: str, by: str, wait: bool = True) -> dict:
        if tool not in CATALOG:
            raise ValueError(f"Unknown tool '{tool}'. Known: {', '.join(sorted(CATALOG))}.")
        if not self.allowed():
            raise PermissionError("Installing is switched off here (machine.installs, or OrgForge is hosted).")
        recipe = self.recipe(tool)
        if not recipe["auto"]:
            raise PermissionError(f"{CATALOG[tool]['name']} is not installed automatically. Run this yourself: "
                                  f"{recipe['how']}" + (f" ({recipe['why']})" if recipe.get("why") else ""))
        with self._lock:
            if tool in self._running:
                raise ValueError(f"{CATALOG[tool]['name']} is already being installed.")
            self._running.add(tool)
        iid = self.db.run("INSERT INTO machine_installs(tool, command, status, requested_by, started_at) "
                          "VALUES (?,?,?,?,?)", tool, recipe["how"], "running", by, now())
        self.db.log("machine", f"{by} started installing {CATALOG[tool]['name']} ({recipe['how']}).", actor=by)

        def work() -> None:
            try:
                code, output = _run(recipe["argv"], timeout=int(self.config.get("install_timeout_seconds", 1800)))
                after = self.check(tool)
                status = "installed" if code == 0 and after["installed"] else "failed"
                if status == "installed" and recipe.get("then") and after["state"] != "ready":
                    output += f"\n\nNext: sign in with `{recipe['then']}`."
                self.db.run("UPDATE machine_installs SET status=?, output=?, finished_at=? WHERE id=?",
                            status, output[-6000:], now(), iid)
                self.db.log("machine", f"{CATALOG[tool]['name']} {'installed' if status == 'installed' else 'failed to install'}"
                            + (f" ({after['version']})." if after["version"] else "."), actor=by)
            finally:
                with self._lock:
                    self._running.discard(tool)
        if wait:
            work()
        else:
            threading.Thread(target=work, name=f"install-{tool}", daemon=True).start()
        return self.db.one("SELECT * FROM machine_installs WHERE id=?", iid)

    # ---- explaining failures -----------------------------------------------
    @staticmethod
    def explain(output: str) -> list[str]:
        """Turn "command not found" in a failed check into what to install."""
        hints = []
        for m in NOT_FOUND.finditer(output or ""):
            word = next(g for g in m.groups() if g)
            tool = COMMANDS.get(word)
            if word == "python" and shutil.which("python3") and not shutil.which("python"):
                hint = ("`python` is not on this machine, only `python3`: use python3 in product.json, "
                        "or add a python command (e.g. via a virtual environment).")
            elif tool:
                hint = f"`{word}` is missing on this machine: install {CATALOG[tool]['name']} (orgforge doctor)."
            else:
                continue
            if hint not in hints:
                hints.append(hint)
        return hints

    def missing(self, pid: int) -> list[dict]:
        return [t for t in self.report(pid)["tools"] if not t["installed"]]
