"""The worker side: run on another computer to lend its coding CLIs to an OrgForge company.

    orgforge worker join http://my-mac.local:4700 ABCD-EFGH-JKMN-PQRS
    orgforge worker run

Only jobs signed by the controller this worker paired with are accepted. A job names an engine;
the worker runs its own definition of it (built in, or under `engines` in the worker's config),
in a fresh folder that is removed afterwards, and sends back the agent's changes as a patch.
Use a dedicated user account on this computer: agents run commands here with that account's rights.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import platform
import secrets
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from .engines import BUILTIN_ENGINES, EngineError, EngineUnavailable, run_cli
from .machine import CATALOG, check_tool
from .nodes import (join_confirmation, join_proof, normal_code, request_headers, response_signature,
                    safe_controller_url, secret_key, unpack)
from .tools import SKIP_DIRS

CONFIG = Path(os.environ.get("ORGFORGE_WORKER_CONFIG", Path.home() / ".orgforge" / "worker.json"))


class WorkerError(RuntimeError):
    pass


def info() -> dict:
    engines = {}
    for tool, t in CATALOG.items():
        if t.get("engine"):
            status = check_tool(tool)
            if status["installed"]:
                engines[t["engine"]] = status["state"]
    return {"os": {"Darwin": "macos", "Windows": "windows"}.get(platform.system(), "linux"),
            "release": platform.release(), "machine": platform.machine(), "cpus": os.cpu_count(), "engines": engines}


def save(config: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(config, indent=2))
    os.chmod(tmp, 0o600)                       # holds the secret
    tmp.replace(CONFIG)


def load() -> dict:
    if not CONFIG.exists():
        raise WorkerError("This computer has not joined a company yet: run `orgforge worker join <url> <code>`.")
    return json.loads(CONFIG.read_text())


def join(url: str, code: str, insecure: bool = False) -> dict:
    url = url.rstrip("/")
    if not insecure and not safe_controller_url(url):
        raise WorkerError("Use https://, or a local, private-network or Tailscale address (or --insecure).")
    if len(normal_code(code)) != 16:
        raise WorkerError("A pairing code has 16 letters and digits, like ABCD-EFGH-JKMN-PQRS.")
    nonce = secrets.token_hex(16)
    body = json.dumps({"nonce": nonce, "proof": join_proof(code, nonce), "info": info()}).encode()
    req = urllib.request.Request(url + "/api/nodes/join", data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            reply = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise WorkerError(f"The company refused to pair: {exc.read().decode(errors='replace')[:200]}") from exc
    expected = join_confirmation(code, nonce, reply["nonce"], reply["node_id"], reply["sealed"])
    if not hmac.compare_digest(expected, str(reply.get("confirm", ""))):
        raise WorkerError("That server could not prove it created the code. Not paired.")
    key = secret_key(code, nonce, reply["nonce"])
    secret = bytes(a ^ b for a, b in zip(bytes.fromhex(reply["sealed"]), key)).hex()
    config = {"url": url, "node_id": reply["node_id"], "name": reply["name"], "secret": secret}
    save(config)
    return config


class Worker:
    def __init__(self, config: dict | None = None) -> None:
        self.config = config or load()
        self.engines = {**BUILTIN_ENGINES, **(self.config.get("engines") or {})}

    def call(self, path: str, payload=None, raw: bool = False):
        body = json.dumps(payload if payload is not None else {}).encode()
        headers = request_headers(self.config["node_id"], self.config["secret"], "POST", path, body)
        req = urllib.request.Request(self.config["url"] + path, data=body,
                                     headers={**headers, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data, signature = resp.read(), resp.headers.get("X-Signature", "")
        except urllib.error.HTTPError as exc:
            raise WorkerError(f"{path}: {exc.code} {exc.read().decode(errors='replace')[:200]}") from exc
        expected = response_signature(self.config["secret"], headers["X-Nonce"], data)
        if not hmac.compare_digest(expected, signature):
            raise WorkerError("The reply was not signed by this worker's company. Ignored.")
        return data if raw else json.loads(data)

    def once(self) -> dict | None:
        """Report in, take one job if there is one, and do it."""
        self.call("/api/nodes/heartbeat", info())
        job = self.call("/api/nodes/next")
        if not job:
            return None
        result = self.work(job)
        self.call(f"/api/nodes/jobs/{job['id']}/result", result)
        return {"job": job["id"], **{k: result[k] for k in ("is_error", "unavailable")}}

    def work(self, job: dict) -> dict:
        engine = self.engines.get(job.get("engine"))
        base = {"text": "", "is_error": True, "turns": 1, "input_tokens": 0, "output_tokens": 0,
                "unavailable": None, "patch": ""}
        if not engine:
            return {**base, "unavailable": f"engine '{job.get('engine')}' is not set up on this worker machine"}
        folder = Path(tempfile.mkdtemp(prefix="orgforge-job-"))
        try:
            has_files = bool(job.get("archive_sha256"))
            if has_files:
                data = self.call(f"/api/nodes/jobs/{job['id']}/archive", raw=True)
                if hashlib.sha256(data).hexdigest() != job["archive_sha256"]:
                    return {**base, "text": "The workspace archive did not match its checksum."}
                unpack(data, folder)
                self._snapshot(folder)
            try:
                out = run_cli(engine, system=job["system"], prompt=job["prompt"], cwd=str(folder) if has_files else None,
                              model=job.get("model") or "", write=bool(job.get("write")) and has_files,
                              run=bool(job.get("run")) and has_files, timeout=int(job.get("timeout") or 1800))
            except EngineUnavailable as exc:
                return {**base, "unavailable": f"{job['engine']} on this worker machine: {exc}"}
            except EngineError as exc:
                return {**base, "text": f"(The {job['engine']} engine failed on the worker machine: {exc})"}
            patch = self._patch(folder) if has_files else b""
            return {**base, "text": out["text"], "is_error": out["is_error"], "turns": out["turns"],
                    "input_tokens": out["input_tokens"], "output_tokens": out["output_tokens"],
                    "patch": base64.b64encode(patch).decode()}
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    @staticmethod
    def _git(folder: Path, *args: str, data: bool = False):
        proc = subprocess.run(["git", "-c", "user.name=OrgForge worker", "-c", "user.email=worker@orgforge.local",
                               "-c", "core.autocrlf=false", *args], cwd=folder, capture_output=True)
        if proc.returncode:
            raise WorkerError(f"git {args[0]} failed: {proc.stderr.decode(errors='replace')[:200]}")
        return proc.stdout if data else proc.stdout.decode()

    def _snapshot(self, folder: Path) -> None:
        self._git(folder, "init", "-q")
        (folder / ".git" / "info" / "exclude").write_text("\n".join(f"{d}/" for d in sorted(SKIP_DIRS - {".git"})) + "\n")
        self._git(folder, "add", "-A")
        self._git(folder, "commit", "-q", "--allow-empty", "-m", "snapshot")

    def _patch(self, folder: Path) -> bytes:
        self._git(folder, "add", "-A")
        return self._git(folder, "diff", "--cached", "--binary", "HEAD", data=True)

    def run_forever(self, poll: float = 3.0, log=print) -> None:
        log(f"Worker machine '{self.config['name']}' working for {self.config['url']}. Ctrl+C to stop.")
        while True:
            try:
                done = self.once()
                if done:
                    log(f"Job {done['job']}: " + ("engine unavailable" if done["unavailable"] else
                                                  "finished with an error" if done["is_error"] else "done"))
                    continue
            except (WorkerError, urllib.error.URLError, OSError) as exc:
                log(f"Waiting for the company: {exc}")
            time.sleep(poll)
