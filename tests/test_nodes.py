import io
import os
import json
import socket
import sys
import tarfile
import textwrap
import threading
import time

import pytest
import uvicorn
from fastapi.testclient import TestClient

from orgforge import nodes as n
from orgforge import worker_client as wc
from orgforge.engines import BUILTIN_ENGINES, EngineUnavailable
from orgforge.server import create_app

FAKE = textwrap.dedent('''
    import json, os, sys
    prompt = sys.stdin.read()
    if "LIMIT" in prompt:
        print(json.dumps({"type": "result", "is_error": True, "result": "You've hit your usage limit. Try again in 2 hours."}))
        sys.exit(0)
    open("hello.py", "w").write("print('hello from the worker')\\n")
    print(json.dumps({"type": "result", "is_error": False, "result": "Wrote hello.py on " + os.getcwd(),
                      "num_turns": 2, "usage": {"input_tokens": 100, "output_tokens": 20}}))
''')


@pytest.fixture
def server(co):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    srv = uvicorn.Server(uvicorn.Config(create_app(co, {"ceo": "ceo-token", "cto": "cto-token"}),
                                        host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    while not srv.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(5)


@pytest.fixture
def fake_engine(co, tmp_path):
    script = tmp_path / "fake_cli.py"
    script.write_text(FAKE)
    engine = {**BUILTIN_ENGINES["claude-code"], "command": [sys.executable, str(script)]}
    co.s.engines["fake"] = engine
    return engine


@pytest.fixture
def worker(co, server, fake_engine, tmp_path, monkeypatch):
    monkeypatch.setattr(wc, "CONFIG", tmp_path / "worker" / "worker.json")
    code = co.nodes.pair("laptop", "Lucky")["code"]
    config = wc.join(server, code)
    return wc.Worker({**config, "engines": {"fake": fake_engine}})


def work_in_background(worker):
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            try:
                worker.once()
            except Exception:                                    # the server may already be gone
                pass
            time.sleep(0.1)
    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return stop


def test_pairing_proves_the_code_both_ways_and_works_once(co, server, tmp_path, monkeypatch):
    monkeypatch.setattr(wc, "CONFIG", tmp_path / "w.json")
    pairing = co.nodes.pair("laptop", "Lucky")
    assert len(n.normal_code(pairing["code"])) == 16 and pairing["code"].count("-") == 3
    config = wc.join(server, pairing["code"].lower().replace("-", " "))      # forgiving about how it is typed
    node = co.db.one("SELECT * FROM nodes")
    assert config["secret"] == node["secret"] and config["name"] == "laptop"
    assert os.name == "nt" or oct(wc.CONFIG.stat().st_mode & 0o777) == "0o600"
    assert co.db.one("SELECT code FROM node_pairings")["code"] == ""          # forgotten once used
    with pytest.raises(wc.WorkerError, match="wrong, used or expired"):
        wc.join(server, pairing["code"])
    with pytest.raises(wc.WorkerError, match="16 letters"):
        wc.join(server, "ABCD-EFGH")
    stale = co.nodes.pair("old", "Lucky")
    co.db.run("UPDATE node_pairings SET expires_at=0 WHERE name='old'")
    with pytest.raises(wc.WorkerError, match="expired"):
        wc.join(server, stale["code"])


def test_the_code_and_secret_never_cross_the_network_in_the_clear(co):
    code = co.nodes.pair("laptop", "Lucky")["code"]
    reply = co.nodes.join("ab" * 16, n.join_proof(code, "ab" * 16), {})
    secret = co.db.one("SELECT secret FROM nodes")["secret"]
    wire = json.dumps(reply)
    assert n.normal_code(code) not in wire and secret not in wire


def test_requests_must_be_signed_fresh_and_not_replayed(co):
    code = co.nodes.pair("laptop", "Lucky")["code"]
    co.nodes.join("cd" * 16, n.join_proof(code, "cd" * 16), {})
    node = co.db.one("SELECT * FROM nodes")
    lower = lambda h: {k.lower(): v for k, v in h.items()}
    good = lower(n.request_headers(node["id"], node["secret"], "POST", "/api/nodes/next", b"{}"))
    assert co.nodes.verify(good, "POST", "/api/nodes/next", b"{}")["id"] == node["id"]
    with pytest.raises(PermissionError, match="Replayed"):
        co.nodes.verify(good, "POST", "/api/nodes/next", b"{}")
    forged = lower(n.request_headers(node["id"], "not-the-secret", "POST", "/api/nodes/next", b"{}"))
    with pytest.raises(PermissionError, match="Bad signature"):
        co.nodes.verify(forged, "POST", "/api/nodes/next", b"{}")
    moved = lower(n.request_headers(node["id"], node["secret"], "POST", "/api/nodes/next", b"{}"))
    with pytest.raises(PermissionError, match="Bad signature"):                # signed for another path
        co.nodes.verify(moved, "POST", "/api/nodes/heartbeat", b"{}")
    old = lower(n.request_headers(node["id"], node["secret"], "POST", "/p", b""))
    old["x-timestamp"] = str(int(time.time()) - 600)
    old["x-signature"] = n.sign(node["secret"], "POST", "/p", old["x-timestamp"], old["x-nonce"], n.body_hash(b""))
    with pytest.raises(PermissionError, match="too old"):
        co.nodes.verify(old, "POST", "/p", b"")
    co.nodes.revoke("laptop", "Lucky")
    fresh = lower(n.request_headers(node["id"], node["secret"], "POST", "/p", b""))
    with pytest.raises(PermissionError, match="revoked"):
        co.nodes.verify(fresh, "POST", "/p", b"")


def test_a_worker_ignores_replies_its_company_did_not_sign(co, worker, monkeypatch):
    monkeypatch.setattr(co.nodes, "respond", lambda node, nonce, payload: (json.dumps(payload).encode(),
                                                                           {"X-Signature": "forged"}))
    with pytest.raises(wc.WorkerError, match="not signed by this worker's company"):
        worker.once()


def test_controller_addresses_must_be_private_or_https():
    ok = ["https://orgforge.example.com", "http://127.0.0.1:4700", "http://192.168.1.20:4700", "http://10.0.0.5",
          "http://my-mac.local:4700", "http://mac.tail1234.ts.net", "http://100.101.102.103:4700"]
    bad = ["http://8.8.8.8:4700", "http://example.com", "ftp://10.0.0.1", "http://user:pw@10.0.0.1"]
    assert all(n.safe_controller_url(u) for u in ok) and not any(n.safe_controller_url(u) for u in bad)


def test_archives_cannot_escape_their_folder(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"x"
        info = tarfile.TarInfo("../escaped.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    (tmp_path / "in").mkdir()
    with pytest.raises(Exception):
        n.unpack(buf.getvalue(), tmp_path / "in")
    assert not (tmp_path / "escaped.txt").exists()


def test_a_ticket_runs_on_the_worker_and_its_changes_come_back(co, worker):
    hari = co.org.agent("Hari")
    co.org.set_model("Hari", "cli:fake")
    co.nodes.assign("Hari", "laptop", "Lucky")
    p = co.pipeline.create_project("Greeter", "A tiny library.")
    ws = co.pipeline.workspace(p)
    ws.write_file("README.md", "# Greeter\n")
    (ws.root / "node_modules").mkdir()
    (ws.root / "node_modules" / "big.js").write_text("//" * 1000)
    stop = work_in_background(worker)
    try:
        res = co.runtime.run(co.org.agent("Hari"), "Build it.", ws, project_id=p["id"])
    finally:
        stop.set()
    assert res.completed and "Wrote hello.py on" in res.text and str(ws.root) not in res.text   # ran elsewhere
    assert (ws.root / "hello.py").read_text() == "print('hello from the worker')\n"
    job = co.db.one("SELECT * FROM node_jobs")
    assert job["status"] == "done" and "node_modules" not in json.loads(job["result"])["patch"]
    assert not list((co.s.root / ".orgforge" / "node-archives").glob("*"))         # archive cleaned up
    run = co.runs.recent(hari["id"])[0]
    assert co.db.one("SELECT 1 FROM run_events WHERE run_id=? AND body LIKE '%on worker machine ''laptop''%'", run["id"])
    [listed] = co.nodes.list()
    assert listed["online"] and listed["agents"] == ["Hari"] and listed["jobs"]["done"] == 1


def test_a_usage_limit_on_the_worker_is_a_usage_limit_here(co, worker):
    co.org.set_model("Hari", "cli:fake")
    co.nodes.assign("Hari", "laptop", "Lucky")
    stop = work_in_background(worker)
    try:
        with pytest.raises(EngineUnavailable, match="fake on this worker machine.*usage limit"):
            co.runtime.run(co.org.agent("Hari"), "LIMIT", None)
    finally:
        stop.set()


def test_an_offline_worker_hands_the_turn_back_to_this_computer(co, worker):
    co.org.set_model("Hari", "cli:fake")
    co.nodes.assign("Hari", "laptop", "Lucky")
    co.db.run("UPDATE nodes SET last_seen=0")
    res = co.runtime.run(co.org.agent("Hari"), "Build it.", None)                 # nobody polls: runs locally
    assert res.completed and co.db.one("SELECT 1 FROM events WHERE message LIKE '%offline; Hari works on this computer%'")
    co.s.engines["fake"] = {**co.s.engines["fake"], "command": ["no-such-cli-here"]}
    with pytest.raises(EngineUnavailable, match="offline"):
        co.runtime.run(co.org.agent("Hari"), "Build it.", None)


def test_dashboard_pairs_assigns_and_revokes(co):
    client = TestClient(create_app(co, {"ceo": "ceo-token", "cto": "cto-token"}))
    cto = {"X-Token": "cto-token"}
    code = client.post("/api/machines/pair", json={"name": "laptop"}, headers=cto).json()["code"]
    assert client.post("/api/machines/pair", json={"name": "x"}).status_code == 401
    co.nodes.join("ef" * 16, n.join_proof(code, "ef" * 16), {"os": "windows", "engines": {"codex": "ready"}})
    assert client.post("/api/machines/assign", json={"agent": "Hari", "machine": "laptop"}, headers=cto).status_code == 400
    co.org.set_model("Hari", "cli:codex")                                      # only CLI agents can move
    assert client.post("/api/machines/assign", json={"agent": "Hari", "machine": "laptop"}, headers=cto).status_code == 200
    [m] = client.get("/api/machines", headers=cto).json()
    assert m["agents"] == ["Hari"] and m["info"]["engines"] == {"codex": "ready"} and "secret" not in m
    assert client.post("/api/machines/laptop/revoke", headers=cto).status_code == 200
    assert client.get("/api/machines", headers=cto).json() == [] and co.nodes.assigned(co.org.agent("Hari")) is None
