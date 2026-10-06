"""Smoke-test the bundled backend: it starts, announces itself, serves the dashboard and quits with its parent.

    python desktop/scripts/smoke_server.py [path to vittics-builder-server binary]
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

BINARIES = Path(__file__).resolve().parent.parent / "src-tauri" / "binaries"


def main() -> None:
    binary = Path(sys.argv[1]) if len(sys.argv) > 1 else next(BINARIES.glob("vittics-builder-server-*"))
    home = tempfile.mkdtemp(prefix="vittics-smoke-")
    env = {**os.environ, "VITTICS_HOME": home, "VITTICS_EXIT_WITH_PARENT": "1"}
    started = time.time()
    proc = subprocess.Popen([str(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=env, text=True, encoding="utf-8")
    line = proc.stdout.readline()
    if not line:
        sys.exit("The backend did not start:\n" + proc.stderr.read()[-3000:])
    ready = json.loads(line)
    port, token = ready["port"], ready["url"].split("#token=")[1]
    page = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=20).read().decode("utf-8")
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/state", headers={"X-Token": token})
    state = json.loads(urllib.request.urlopen(req, timeout=20).read())
    assert "<title>" in page and state["desktop"] is True and state["you"]["role"] == "ceo", "unexpected dashboard"
    agents = sum(len(d["agents"]) for d in state["departments"])
    print(f"Started in {time.time() - started:.1f}s on port {port}; dashboard {len(page)} bytes; {agents} agents; "
          f"version {state['version']}")
    proc.stdin.close()
    code = proc.wait(timeout=20)
    assert code == 0, f"backend exited with {code} when the app quit"
    print("Quit with its parent: ok")


if __name__ == "__main__":
    main()
