"""Previews: the signed-off product running on this computer, so you can try it.

A product that can be served says how in product.json:

    "serve": {"setup": "npm ci", "command": "npm start", "health": "/healthz"}

`command` must listen on $PORT (and HOST, 127.0.0.1). The preview runs from a clean copy of the release
(a git worktree at its tag), with the project's keys from the vault, and its output goes to a log.
It counts as running once GET http://127.0.0.1:$PORT<health> answers. When a new version starts,
the old one is stopped only after the new one is healthy; a version that fails leaves the old one up.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from .db import now
from .platforms import WINDOWS, shell_command
from .tools import SECRET_ENV


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


_STARTED: dict[int, subprocess.Popen] = {}       # processes this server started: their real status, no zombies


def alive(pid: int | None) -> bool:
    if not pid:
        return False
    if pid in _STARTED:
        return _STARTED[pid].poll() is None
    if WINDOWS:                                   # os.kill(pid, 0) would send Ctrl-C on Windows
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)        # SYNCHRONIZE
        if not handle:
            return False
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT: still running
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


class Previews:
    def __init__(self, company) -> None:
        self.co, self.db, self.s = company, company.db, company.s
        self.folder = self.s.workspaces / ".previews"
        self.logs = self.s.root / ".vittics" / "previews"

    def spec(self, project: dict) -> dict | None:
        path = Path(project["workspace"]) / "product.json"
        try:
            serve = json.loads(path.read_text(encoding="utf-8")).get("serve")
        except (OSError, ValueError, AttributeError):
            return None
        if not isinstance(serve, dict) or not isinstance(serve.get("command"), str) or not serve["command"].strip():
            return None
        health = serve.get("health") if isinstance(serve.get("health"), str) and serve["health"].startswith("/") else "/"
        setup = serve.get("setup") if isinstance(serve.get("setup"), str) else ""
        return {"command": serve["command"], "setup": setup, "health": health}

    def available(self, project: dict) -> bool:
        return (self.s.raw.get("preview") or {}).get("enabled", True) is not False and self.spec(project) is not None

    def current(self, pid: int) -> dict | None:
        row = self.db.one("SELECT * FROM previews WHERE project_id=?", pid)
        if row and row["status"] == "running" and not alive(row["pid"]):
            self.db.run("UPDATE previews SET status='stopped' WHERE project_id=?", pid)
            row = self.db.one("SELECT * FROM previews WHERE project_id=?", pid)
        return row

    # ---- starting and stopping -------------------------------------------------
    def start(self, project: dict, version: int, timeout: float | None = None) -> tuple[bool, str]:
        """Start `version` and wait until it is healthy. Returns (ok, what happened)."""
        spec = self.spec(project)
        if not spec:
            return False, "product.json has no serve command."
        pid = project["id"]
        ws = self.co.pipeline.workspace(project)
        tag = f"v{version}"
        copy = self.folder / f"{pid}-{tag}"
        if copy.exists():
            shutil.rmtree(copy, ignore_errors=True)
            ws.git("worktree", "prune")
        self.folder.mkdir(parents=True, exist_ok=True)
        ws.git("worktree", "add", "--detach", "-q", str(copy), tag)
        self.logs.mkdir(parents=True, exist_ok=True)
        log_path = self.logs / f"{pid}-{tag}.log"
        port = free_port()
        env = {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}
        env.update(self.co.vault.env(pid))
        env.update(PORT=str(port), HOST="127.0.0.1")
        timeout = float(timeout or (self.s.raw.get("preview") or {}).get("start_timeout_seconds", 120))
        with open(log_path, "w", encoding="utf-8") as log:
            if spec["setup"]:
                args, shell = shell_command(spec["setup"])
                try:
                    done = subprocess.run(args, shell=shell, cwd=copy, env=env, stdout=log, stderr=subprocess.STDOUT,
                                          timeout=max(60, int(self.s.command_timeout)))
                except subprocess.TimeoutExpired:
                    return False, self._fail(pid, copy, log_path, "setup timed out")
                if done.returncode:
                    return False, self._fail(pid, copy, log_path, f"setup exited with {done.returncode}")
            args, shell = shell_command(spec["command"])
            extra = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS else {"start_new_session": True})
            proc = subprocess.Popen(args, shell=shell, cwd=copy, env=env, stdout=log, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, **extra)
            _STARTED[proc.pid] = proc
        url = f"http://127.0.0.1:{port}{spec['health']}"
        deadline = time.time() + timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                self._kill(proc.pid)
                return False, self._fail(pid, copy, log_path, f"it exited with {proc.returncode} before answering")
            try:
                with urllib.request.urlopen(url, timeout=3) as resp:
                    if 200 <= resp.status < 400:
                        break
            except (urllib.error.URLError, OSError, ValueError):
                pass
            time.sleep(0.5)
        else:
            self._kill(proc.pid)
            return False, self._fail(pid, copy, log_path, f"{url} did not answer within {timeout:.0f}s")
        old = self.current(pid)
        if old and old["status"] == "running":
            self._kill(old["pid"])
            self._remove(project, old["path"])
        self.db.run("INSERT INTO previews(project_id, version, port, pid, url, path, log, status, started_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET version=excluded.version, "
                    "port=excluded.port, pid=excluded.pid, url=excluded.url, path=excluded.path, log=excluded.log, "
                    "status=excluded.status, started_at=excluded.started_at",
                    pid, version, port, proc.pid, f"http://127.0.0.1:{port}/", str(copy), str(log_path), "running", now())
        self.db.log("preview", f"Version {version} is running at http://127.0.0.1:{port}/", pid, actor="Preview")
        return True, f"healthy at {url}"

    def stop(self, pid: int, by: str = "Preview") -> bool:
        row = self.current(pid)
        if not row or row["status"] != "running":
            return False
        self._kill(row["pid"])
        self.db.run("UPDATE previews SET status='stopped' WHERE project_id=?", pid)
        self.db.log("preview", f"{by} stopped the preview.", pid, actor=by)
        return True

    def stop_all(self) -> None:
        for row in self.db.all("SELECT project_id FROM previews WHERE status='running'"):
            self.stop(row["project_id"], "Vittics Builder (shutting down)")

    def _fail(self, pid: int, copy: Path, log_path: Path, why: str) -> str:
        tail = self.co.vault.redact(pid, log_path.read_text(encoding="utf-8", errors="replace")[-3000:]) \
            if log_path.exists() else ""
        project = self.co.pipeline.project(pid)
        self._remove(project, str(copy))
        return f"The preview did not start: {why}.\n\n{tail}".strip()

    def _remove(self, project: dict, path: str) -> None:
        try:
            self.co.pipeline.workspace(project).git("worktree", "remove", "--force", path)
        except Exception:
            shutil.rmtree(path, ignore_errors=True)

    @staticmethod
    def _kill(pid: int | None) -> None:
        """Stop the preview and everything it started; always forget the process afterwards."""
        try:
            if alive(pid):
                if WINDOWS:
                    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
                else:
                    os.killpg(pid, signal.SIGTERM)
                    for _ in range(20):
                        if not alive(pid):
                            break
                        time.sleep(0.1)
                    else:
                        os.killpg(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        proc = _STARTED.pop(pid, None) if pid else None
        if proc:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
