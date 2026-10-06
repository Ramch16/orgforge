"""Worker machines (nodes): other computers that run agents' coding CLIs for this company.

The controller (this OrgForge) never connects to a worker. A worker joins with a one-time pairing
code, then asks for jobs. Every request and every response is signed with the worker's own secret,
with a timestamp and a single-use nonce, so neither side acts on a forged or replayed message.

A job names an engine (e.g. "codex"), never a command: the worker runs its own definition of that
engine, so the controller cannot make a worker run arbitrary programs. The ticket's workspace goes
to the worker as an archive, and the agent's changes come back as a patch that is applied here,
where the usual reviews and checks run.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import io
import json
import os
import secrets
import subprocess
import tarfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from .db import now
from .engines import EngineUnavailable
from .tools import SKIP_DIRS

class NodeOffline(EngineUnavailable):
    """The worker machine is not there; the engine itself may be fine."""


PAIRING_SECONDS = 600
SKEW_SECONDS = 120
OFFLINE_SECONDS = 60
MAX_ARCHIVE = 200 * 1024 * 1024
MAX_PATCH = 50 * 1024 * 1024


# ---- signing, shared by both sides ------------------------------------------
def body_hash(body: bytes) -> str:
    return hashlib.sha256(body or b"").hexdigest()


def sign(secret: str, *parts: str) -> str:
    return hmac.new(secret.encode(), "\n".join(parts).encode(), hashlib.sha256).hexdigest()


def request_headers(node_id: int, secret: str, method: str, path: str, body: bytes) -> dict:
    ts, nonce = str(int(time.time())), secrets.token_hex(16)
    return {"X-Node": str(node_id), "X-Timestamp": ts, "X-Nonce": nonce,
            "X-Signature": sign(secret, method.upper(), path, ts, nonce, body_hash(body))}


def response_signature(secret: str, nonce: str, body: bytes) -> str:
    return sign(secret, "response", nonce, body_hash(body))


CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"      # no 0/O, 1/I/L


def normal_code(code: str) -> str:
    return "".join(c for c in str(code).upper() if c in CODE_ALPHABET)


def join_proof(code: str, nonce: str) -> str:
    """The worker proves it knows the pairing code without sending it."""
    return sign(normal_code(code), "join", nonce)


def secret_key(code: str, worker_nonce: str, controller_nonce: str) -> bytes:
    return hmac.new(normal_code(code).encode(), f"key\n{worker_nonce}\n{controller_nonce}".encode(), hashlib.sha256).digest()


def seal(secret: bytes, code: str, worker_nonce: str, controller_nonce: str) -> str:
    """The new secret travels encrypted with a key only the two holders of the code can derive."""
    return bytes(a ^ b for a, b in zip(secret, secret_key(code, worker_nonce, controller_nonce))).hex()


def join_confirmation(code: str, worker_nonce: str, controller_nonce: str, node_id: int, sealed: str) -> str:
    """The controller proves it knows the code too, so a worker never trusts an impostor."""
    return sign(normal_code(code), "confirm", worker_nonce, controller_nonce, str(node_id), sealed)


def safe_controller_url(url: str) -> bool:
    """HTTPS anywhere; plain HTTP only to this machine, a private network, or Tailscale (100.64.0.0/10)."""
    parts = urlsplit(url)
    if parts.scheme == "https":
        return True
    if parts.scheme != "http" or not parts.hostname or parts.username or parts.password:
        return False
    host = parts.hostname
    if host == "localhost" or host.endswith((".local", ".ts.net")):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip in ipaddress.ip_network("100.64.0.0/10")


def pack(root: Path) -> bytes:
    """The workspace as a tar.gz, without .git, dependencies or build output."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(root)
            if any(p in SKIP_DIRS for p in rel.parts) or path.is_symlink() or not path.is_file():
                continue
            tar.add(path, arcname=str(rel), recursive=False)
            if buf.tell() > MAX_ARCHIVE:
                raise ValueError("The workspace is too large to send to a worker machine (over 200 MB).")
    return buf.getvalue()


def unpack(data: bytes, dest: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(dest, filter="data")        # refuses absolute paths, .. and links out of dest
            return
        root = dest.resolve()                          # Python 3.11.0-3.11.3: check each member ourselves
        for member in tar.getmembers():
            target = (dest / member.name).resolve()
            if not member.isfile() and not member.isdir() or (target != root and root not in target.parents):
                raise ValueError(f"Unsafe path in workspace archive: {member.name}")
        tar.extractall(dest)


class Nodes:
    def __init__(self, company) -> None:
        self.co, self.db, self.s = company, company.db, company.s
        self._nonces: dict[str, float] = {}
        self._lock = threading.Lock()

    # ---- pairing -----------------------------------------------------------
    def pair(self, name: str, by: str) -> dict:
        name = str(name).strip()
        if not name or len(name) > 60 or self.db.one("SELECT 1 FROM nodes WHERE name=? AND status!='revoked'", name):
            raise ValueError("Give the worker machine a new name of up to 60 characters.")
        raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(16))      # about 80 bits
        code = "-".join(raw[i:i + 4] for i in range(0, 16, 4))
        self.db.run("INSERT INTO node_pairings(name, code, expires_at, created_by, created_at) VALUES (?,?,?,?,?)",
                    name, raw, time.time() + PAIRING_SECONDS, by, now())
        self.db.log("machine", f"{by} created a pairing code for worker machine '{name}' (valid 10 minutes).", actor=by)
        return {"name": name, "code": code, "expires_in": PAIRING_SECONDS}

    def join(self, nonce: str, proof: str, info: dict) -> dict:
        if not isinstance(nonce, str) or not 16 <= len(nonce) <= 64 or not isinstance(proof, str):
            raise PermissionError("Bad join request.")
        with self._lock:
            row = next((r for r in self.db.all("SELECT * FROM node_pairings WHERE used_at IS NULL AND expires_at>?",
                                               time.time())
                        if hmac.compare_digest(join_proof(r["code"], nonce), proof)), None)
            if not row:
                raise PermissionError("That pairing code is wrong, used or expired. Create a new one.")
            self.db.run("UPDATE node_pairings SET used_at=?, code='' WHERE id=?", now(), row["id"])
        secret = secrets.token_bytes(32)
        nid = self.db.run("INSERT INTO nodes(name, secret, status, info, last_seen, created_at) VALUES (?,?,?,?,?,?)",
                          row["name"], secret.hex(), "active", json.dumps(self._info(info)), time.time(), now())
        controller_nonce = secrets.token_hex(16)
        sealed = seal(secret, row["code"], nonce, controller_nonce)
        self.db.log("machine", f"Worker machine '{row['name']}' joined.", actor=row["name"])
        return {"node_id": nid, "name": row["name"], "nonce": controller_nonce, "sealed": sealed,
                "confirm": join_confirmation(row["code"], nonce, controller_nonce, nid, sealed)}

    @staticmethod
    def _info(info) -> dict:
        info = info if isinstance(info, dict) else {}
        engines = info.get("engines") if isinstance(info.get("engines"), dict) else {}
        return {"os": str(info.get("os", ""))[:40], "release": str(info.get("release", ""))[:40],
                "machine": str(info.get("machine", ""))[:40], "cpus": info.get("cpus") if isinstance(info.get("cpus"), int) else None,
                "engines": {str(k)[:40]: str(v)[:20] for k, v in list(engines.items())[:20]}}

    def revoke(self, name: str, by: str) -> None:
        node = self.get(name)
        self.db.run("UPDATE nodes SET status='revoked' WHERE id=?", node["id"])
        self.db.run("DELETE FROM node_assignments WHERE node_id=?", node["id"])
        self.db.run("UPDATE node_jobs SET status='failed', error='worker machine revoked', finished_at=? "
                    "WHERE node_id=? AND status IN ('queued','claimed')", now(), node["id"])
        self.db.log("machine", f"{by} revoked worker machine '{node['name']}'.", actor=by)

    # ---- authenticating a worker's request ---------------------------------
    def verify(self, headers: dict, method: str, path: str, body: bytes) -> dict:
        try:
            nid, ts = int(headers.get("x-node", "")), int(headers.get("x-timestamp", ""))
        except ValueError:
            raise PermissionError("Unsigned request.")
        nonce, signature = headers.get("x-nonce", ""), headers.get("x-signature", "")
        node = self.db.one("SELECT * FROM nodes WHERE id=? AND status='active'", nid)
        if not node or not nonce or len(nonce) > 64:
            raise PermissionError("Unknown or revoked worker machine.")
        expected = sign(node["secret"], method.upper(), path, str(ts), nonce, body_hash(body))
        if not hmac.compare_digest(expected, signature):
            raise PermissionError("Bad signature.")
        if abs(time.time() - ts) > SKEW_SECONDS:
            raise PermissionError("Request too old or clock out of sync (more than 2 minutes).")
        with self._lock:
            cutoff = time.time() - 2 * SKEW_SECONDS
            self._nonces = {k: v for k, v in self._nonces.items() if v > cutoff}
            key = f"{nid}:{nonce}"
            if key in self._nonces:
                raise PermissionError("Replayed request.")
            self._nonces[key] = time.time()
        self.db.run("UPDATE nodes SET last_seen=? WHERE id=?", time.time(), nid)
        return node

    def respond(self, node: dict, nonce: str, payload) -> tuple[bytes, dict]:
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return body, {"X-Signature": response_signature(node["secret"], nonce, body)}

    # ---- the worker's side of the queue ------------------------------------
    def heartbeat(self, node: dict, info: dict) -> None:
        self.db.run("UPDATE nodes SET info=? WHERE id=?", json.dumps(self._info(info)), node["id"])

    def next_job(self, node: dict) -> dict | None:
        self._expire()
        with self._lock:
            job = self.db.one("SELECT * FROM node_jobs WHERE node_id=? AND status='queued' ORDER BY id", node["id"])
            if not job:
                return None
            self.db.run("UPDATE node_jobs SET status='claimed', claimed_at=? WHERE id=?", time.time(), job["id"])
        return {"id": job["id"], **json.loads(job["payload"])}

    def archive(self, node: dict, job_id: int) -> bytes:
        job = self._job(node, job_id, ("claimed",))
        return Path(job["archive"]).read_bytes() if job["archive"] else b""

    def finish(self, node: dict, job_id: int, result: dict) -> None:
        job = self._job(node, job_id, ("claimed",))
        patch = base64.b64decode(result.get("patch") or "")
        if len(patch) > MAX_PATCH:
            raise ValueError("Patch too large.")
        clean = {k: result.get(k) for k in ("text", "is_error", "turns", "input_tokens", "output_tokens", "unavailable")}
        clean["patch"] = base64.b64encode(patch).decode()
        self.db.run("UPDATE node_jobs SET status='done', result=?, finished_at=? WHERE id=?", json.dumps(clean), now(), job["id"])

    def _job(self, node: dict, job_id: int, statuses: tuple) -> dict:
        job = self.db.one("SELECT * FROM node_jobs WHERE id=? AND node_id=?", job_id, node["id"])
        if not job or job["status"] not in statuses:
            raise PermissionError("No such job for this worker machine.")
        return job

    def _expire(self) -> None:
        self.db.run("UPDATE node_jobs SET status='failed', error='timed out', finished_at=? "
                    "WHERE status IN ('queued','claimed') AND deadline<?", now(), time.time())

    # ---- the controller's side ---------------------------------------------
    def assign(self, agent_name: str, machine: str | None, by: str) -> None:
        agent = self.co.org.agent(agent_name)
        if machine is None:
            self.db.run("DELETE FROM node_assignments WHERE agent_id=?", agent["id"])
            self.db.log("machine", f"{by}: {agent['name']} works on this computer again.", actor=by)
            return
        if not str(agent["model"]).startswith("cli:"):
            raise ValueError(f"{agent['name']} works through {agent['model']}, not a coding CLI; only CLI agents "
                             "run on worker machines (API agents need no machine).")
        node = self.get(machine)
        self.db.run("INSERT INTO node_assignments(agent_id, node_id) VALUES (?,?) ON CONFLICT(agent_id) DO UPDATE "
                    "SET node_id=excluded.node_id", agent["id"], node["id"])
        self.db.log("machine", f"{by}: {agent['name']} now works on worker machine '{node['name']}'.", actor=by)

    def assigned(self, agent: dict) -> dict | None:
        return self.db.one("SELECT n.* FROM node_assignments a JOIN nodes n ON n.id=a.node_id "
                           "WHERE a.agent_id=? AND n.status='active'", agent["id"])

    def run(self, node: dict, *, engine: str, model: str, system: str, prompt: str, ws, write: bool, run: bool,
            timeout: int, agent: str) -> dict:
        """Run one CLI turn on a worker machine; same result shape as engines.run_cli."""
        if time.time() - (node["last_seen"] or 0) > OFFLINE_SECONDS:
            raise NodeOffline(f"worker machine '{node['name']}' is offline")
        archive_path = None
        if ws is not None:
            folder = self.s.root / ".orgforge" / "node-archives"
            folder.mkdir(parents=True, exist_ok=True)
            archive_path = folder / f"{secrets.token_hex(8)}.tar.gz"
            archive_path.write_bytes(pack(ws.root))
        payload = {"engine": engine, "model": model, "system": system, "prompt": prompt, "write": write, "run": run,
                   "timeout": timeout, "agent": agent,
                   "archive_sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest() if archive_path else None}
        jid = self.db.run("INSERT INTO node_jobs(node_id, status, payload, archive, deadline, created_at) "
                          "VALUES (?,?,?,?,?,?)", node["id"], "queued", json.dumps(payload),
                          str(archive_path) if archive_path else None, time.time() + timeout + 300, now())
        try:
            while True:
                job = self.db.one("SELECT * FROM node_jobs WHERE id=?", jid)
                if job["status"] in ("done", "failed"):
                    break
                if job["status"] == "queued" and time.time() - (self.db.one("SELECT last_seen FROM nodes WHERE id=?",
                                                                            node["id"])["last_seen"] or 0) > OFFLINE_SECONDS:
                    self.db.run("UPDATE node_jobs SET status='failed', error='worker went offline' WHERE id=?", jid)
                    raise NodeOffline(f"worker machine '{node['name']}' went offline")
                time.sleep(0.5)
                self._expire()
        finally:
            if archive_path:
                archive_path.unlink(missing_ok=True)
        if job["status"] == "failed":
            raise EngineUnavailable(f"worker machine '{node['name']}': {job['error']}")
        result = json.loads(job["result"])
        if result.get("unavailable"):
            raise EngineUnavailable(str(result["unavailable"])[:300])
        patch = base64.b64decode(result.get("patch") or "")
        if patch and ws is not None:
            applied = subprocess.run(["git", "apply", "--binary", "--whitespace=nowarn", "-"], input=patch,
                                     cwd=ws.root, capture_output=True)
            if applied.returncode:
                result["text"] = (result.get("text") or "") + ("\n\n(OrgForge could not apply the changes from "
                                  f"worker machine '{node['name']}': {applied.stderr.decode(errors='replace')[:300]})")
                result["is_error"] = True
        return {"text": str(result.get("text") or ""), "is_error": bool(result.get("is_error")), "cost": None,
                "turns": int(result.get("turns") or 1), "input_tokens": int(result.get("input_tokens") or 0),
                "output_tokens": int(result.get("output_tokens") or 0), "node": node["name"]}

    # ---- views ---------------------------------------------------------------
    def get(self, name_or_id) -> dict:
        node = self.db.one("SELECT * FROM nodes WHERE (name=? OR id=?) AND status='active'", str(name_or_id),
                           int(name_or_id) if str(name_or_id).isdigit() else -1)
        if not node:
            raise ValueError(f"No active worker machine '{name_or_id}'.")
        return node

    def list(self) -> list[dict]:
        out = []
        for n in self.db.all("SELECT * FROM nodes WHERE status='active' ORDER BY name"):
            agents = [a["name"] for a in self.db.all("SELECT ag.name FROM node_assignments a JOIN agents ag ON ag.id=a.agent_id "
                                                     "WHERE a.node_id=? ORDER BY ag.name", n["id"])]
            jobs = self.db.one("SELECT COUNT(*) AS total, SUM(status='done') AS done, SUM(status IN ('queued','claimed')) "
                               "AS active FROM node_jobs WHERE node_id=?", n["id"])
            out.append({"id": n["id"], "name": n["name"], "info": json.loads(n["info"] or "{}"),
                        "online": time.time() - (n["last_seen"] or 0) <= OFFLINE_SECONDS,
                        "last_seen": n["last_seen"], "agents": agents, "jobs": jobs, "created_at": n["created_at"]})
        return out
