"""Per-project keys (API keys, database URLs, Stripe secrets) that the product needs to run.

Values live in a private file per project under the company's .vittics/vault/, never in the
database. They are given, as environment variables, only to that product's acceptance checks, its
preview and its production commands, never to agents. Agents see the names, so the code can read
them from the environment. Any value that shows up in output is replaced with [NAME] before it is
stored or shown.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
RESERVED = {"PATH", "HOME", "PORT", "HOST", "SHELL", "USER", "PYTHONPATH", "NODE_OPTIONS", "LD_PRELOAD",
            "DYLD_INSERT_LIBRARIES"}


class Vault:
    def __init__(self, company) -> None:
        self.co, self.db = company, company.db
        self.folder = company.s.root / ".vittics" / "vault"

    def _path(self, pid: int) -> Path:
        return self.folder / f"{int(pid)}.json"

    def _read(self, pid: int) -> dict[str, str]:
        path = self._path(pid)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def _write(self, pid: int, data: dict[str, str]) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = self._path(pid).with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self._path(pid))

    def set(self, pid: int, name: str, value: str, by: str) -> None:
        self.co.pipeline.project(pid)
        name = str(name).strip()
        if not NAME.fullmatch(name) or name in RESERVED:
            raise ValueError("Key names are UPPER_CASE letters, digits and _ (e.g. STRIPE_SECRET_KEY), "
                             "and not a system variable like PATH or PORT.")
        if not isinstance(value, str) or not value or len(value) > 8192 or "\n" in value.strip("\n"):
            raise ValueError("A key's value is one line of up to 8,192 characters.")
        data = self._read(pid)
        data[name] = value.strip("\n")
        self._write(pid, data)
        self.db.log("vault", f"{by} set the key {name}.", pid, actor=by)     # the name only, never the value

    def delete(self, pid: int, name: str, by: str) -> None:
        data = self._read(pid)
        if data.pop(name, None) is None:
            raise ValueError(f"No key named {name}.")
        self._write(pid, data)
        self.db.log("vault", f"{by} removed the key {name}.", pid, actor=by)

    def names(self, pid: int) -> list[str]:
        return sorted(self._read(pid))

    def env(self, pid: int) -> dict[str, str]:
        return dict(self._read(pid))

    def redact(self, pid: int, text: str) -> str:
        for name, value in sorted(self._read(pid).items(), key=lambda kv: -len(kv[1])):
            if len(value) >= 4:
                text = text.replace(value, f"[{name}]")
        return text

    def for_agents(self, pid: int) -> str:
        names = self.names(pid)
        if not names:
            return ""
        return ("Keys the product can use, as environment variables (values are private; read them from the "
                "environment, never hard-code or print them): " + ", ".join(names) + ".")
