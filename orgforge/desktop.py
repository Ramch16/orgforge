"""Desktop mode: the backend the OrgForge app starts.

The company lives in ~/OrgForge (or ORGFORGE_HOME) and is created on first launch. The server
listens on 127.0.0.1 only, on a free port, with CEO/CTO tokens kept in a private file in the company
so they survive restarts. Once it is up, one JSON line on stdout tells the app shell where to go:

    {"url": "http://127.0.0.1:53121/#token=...", "port": 53121, "home": "/Users/me/OrgForge"}

Apps opened from Finder or the Start menu do not get the PATH a terminal has, so Homebrew tools and
AI CLIs would look missing; on macOS and Linux the login shell's PATH is loaded first.
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

EXTRA_PATHS = ["/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local" / "bin"),
               str(Path.home() / ".npm-global" / "bin"), str(Path.home() / ".cargo" / "bin")]


def login_path() -> str:
    """PATH as a terminal would have it: the login shell's, plus the usual tool folders."""
    current = os.environ.get("PATH", "")
    if os.name == "nt":
        return current
    shell = os.environ.get("SHELL") or "/bin/zsh"
    found = ""
    try:
        out = subprocess.run([shell, "-ilc", "printf '__PATH__%s' \"$PATH\""], capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=10, stdin=subprocess.DEVNULL)
        if "__PATH__" in out.stdout:
            found = out.stdout.rsplit("__PATH__", 1)[1].strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    parts = [p for p in (found.split(":") + current.split(":") + EXTRA_PATHS) if p]
    return ":".join(dict.fromkeys(parts))


def desktop_tokens(root: Path) -> dict[str, str]:
    path = root / ".orgforge" / "desktop.json"
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("ceo") and data.get("cto") and data["ceo"] != data["cto"]:
            return {"ceo": data["ceo"], "cto": data["cto"]}
    path.parent.mkdir(parents=True, exist_ok=True)
    tokens = {"ceo": secrets.token_urlsafe(24), "cto": secrets.token_urlsafe(24)}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(tokens), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return tokens


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def exit_with_parent() -> None:
    """The app holds our stdin open; when it quits (even if killed) stdin closes, and so do we."""
    def watch() -> None:
        try:
            while sys.stdin.buffer.read(1024):
                pass
        except (OSError, ValueError):
            pass
        os._exit(0)
    threading.Thread(target=watch, name="orgforge-parent-watch", daemon=True).start()


def main(home: str | None = None, port: int | None = None) -> None:
    import uvicorn

    from .company import Company
    from .server import create_app

    os.environ["PATH"] = login_path()
    root = Path(home or os.environ.get("ORGFORGE_HOME") or Path.home() / "OrgForge").expanduser()
    co = Company(root, create=not (root / ".orgforge" / "company.db").exists())
    tokens = desktop_tokens(root)
    port = port or free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(co, tokens, desktop=True), host="127.0.0.1", port=port,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, name="orgforge-server")
    thread.start()
    while not server.started and thread.is_alive():
        time.sleep(0.05)
    if not server.started:
        raise SystemExit("The OrgForge server did not start.")
    print(json.dumps({"url": f"http://127.0.0.1:{port}/#token={tokens['ceo']}", "port": port, "home": str(root)}),
          flush=True)
    if os.environ.get("ORGFORGE_EXIT_WITH_PARENT") == "1":      # set by the desktop app
        exit_with_parent()
    try:
        thread.join()
    except KeyboardInterrupt:
        server.should_exit = True
        thread.join(5)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
