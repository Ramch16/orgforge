"""How the agents think: one company-wide choice, made in the app or with `vittics-builder ai`.

  claude-code  Claude Code on this computer, on your Claude subscription (cli:claude-code)
  codex        Codex on this computer, on your ChatGPT plan (cli:codex)
  api          The Anthropic API, with a key you enter once (billed per token)
  demo         Offline demo agents: no AI, nothing is really built

Choosing moves every current agent to it and makes it the default for new hires. The choice is kept in
the company's database, so org.yaml (and its comments) is never rewritten. An API key entered here is
stored privately in .vittics/credentials.json and loaded only into this server's environment; agents'
commands and the product's checks never receive it.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .db import now

CHOICES = {
    "claude-code": {"label": "Claude Code", "model": "cli:claude-code", "tool": "claude-code",
                    "help": "Uses your Claude subscription. Install Claude Code, run `claude` and type /login."},
    "codex": {"label": "Codex", "model": "cli:codex", "tool": "codex",
              "help": "Uses your ChatGPT plan. Install Codex (it comes with the ChatGPT app) and run `codex login`."},
    "api": {"label": "Anthropic API key", "model": None, "tool": None,
            "help": "Pay per use with an Anthropic API key (console.anthropic.com)."},
    "demo": {"label": "Offline demo", "model": None, "tool": None,
             "help": "Scripted agents for trying the app: no AI is used and nothing real is built."},
}
SECRETS = ("ANTHROPIC_API_KEY",)


class AI:
    def __init__(self, company) -> None:
        self.co, self.db, self.s = company, company.db, company.s
        self.path = self.s.root / ".vittics" / "credentials.json"
        self._status: tuple[float, dict] | None = None
        self.load_credentials()
        self.apply_saved()

    # ---- credentials -------------------------------------------------------
    def _read(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def load_credentials(self) -> None:
        for name, value in self._read().items():
            if name in SECRETS and value:
                os.environ.setdefault(name, value)

    def set_key(self, value: str, by: str) -> None:
        value = (value or "").strip()
        if not value.startswith("sk-") or len(value) < 20 or any(c.isspace() for c in value):
            raise ValueError("That does not look like an Anthropic API key (they start with sk-).")
        data = self._read()
        data["ANTHROPIC_API_KEY"] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)
        os.environ["ANTHROPIC_API_KEY"] = value
        self._status = None
        self.db.log("ai", f"{by} saved an Anthropic API key.", actor=by)        # never the key itself

    def has_key(self) -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY") or self._read().get("ANTHROPIC_API_KEY"))

    # ---- the choice ----------------------------------------------------------
    def saved(self) -> str | None:
        row = self.db.one("SELECT value FROM company_settings WHERE key='ai'")
        return row["value"] if row and row["value"] in CHOICES else None

    def current(self) -> str:
        """The saved choice, or what an existing company is already set up as."""
        if self.saved():
            return self.saved()
        if self.s.provider == "mock":
            return "demo"
        models = [a["model"] for a in self.co.org.staff()]
        for choice in ("claude-code", "codex"):
            if models and sum(m.startswith(CHOICES[choice]["model"]) for m in models) * 2 > len(models):
                return choice
        return "api"

    def apply_saved(self) -> None:
        choice = self.saved()
        if not choice:
            return
        if choice == "demo":
            self.s.provider = "mock"
        elif choice == "api":
            self.s.provider = "anthropic"
        model = CHOICES[choice]["model"]
        if model:
            self.s.default_model = model
        elif self.s.default_model.startswith("cli:"):
            self.s.default_model = "claude-sonnet-5-5"
        self._rebuild_provider()

    def _rebuild_provider(self) -> None:
        from .company import _LazyProvider
        from .llm import RoutingProvider
        routing = self.co.runtime.provider
        if isinstance(routing, RoutingProvider) and isinstance(routing.default, _LazyProvider):
            routing.default = _LazyProvider(self.s.provider)

    def use(self, choice: str, by: str) -> dict:
        if choice not in CHOICES:
            raise ValueError(f"Choose one of: {', '.join(CHOICES)}.")
        if choice == "api" and not self.has_key():
            raise ValueError("Enter an Anthropic API key first.")
        self.db.run("INSERT INTO company_settings(key, value, updated_at) VALUES ('ai', ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at", choice, now())
        self.apply_saved()
        model = CHOICES[choice]["model"] or (self.s.default_model if not self.s.default_model.startswith("cli:")
                                             else "claude-sonnet-5-5")
        for agent in self.co.org.staff():
            if agent["model"] != model:
                self.co.org.set_model(agent["id"], model, by=by)
        self._status = None
        self.db.log("ai", f"{by}: agents now think with {CHOICES[choice]['label']}.", actor=by)
        return self.status(fresh=True)

    # ---- can the agents work? -------------------------------------------------
    def status(self, fresh: bool = False) -> dict:
        if not fresh and self._status and time.time() - self._status[0] < 30:
            return self._status[1]
        from .machine import check_tool
        choice = self.current()
        ready, reason = True, ""
        tools = {}
        for key in ("claude-code", "codex"):
            t = check_tool(CHOICES[key]["tool"])
            tools[key] = {"installed": t["installed"], "signed_in": t["signed_in"], "state": t["state"],
                          "version": t["version"], "detail": t["detail"]}
        if choice in ("claude-code", "codex"):
            t = tools[choice]
            if not t["installed"]:
                ready, reason = False, f"{CHOICES[choice]['label']} is not installed. {CHOICES[choice]['help']}"
            elif t["state"] != "ready":
                ready, reason = False, f"{CHOICES[choice]['label']}: {t['detail'] or 'not ready'}"
        elif choice == "api" and not self.has_key():
            ready, reason = False, "No Anthropic API key yet."
        result = {"choice": choice, "label": CHOICES[choice]["label"], "ready": ready, "reason": reason,
                  "chosen": bool(self.saved()), "has_key": self.has_key(), "tools": tools,
                  "choices": {k: {"label": v["label"], "help": v["help"]} for k, v in CHOICES.items()}}
        self._status = (time.time(), result)
        return result

    def test(self) -> dict:
        """One tiny prompt through the current choice, to prove it really answers."""
        from .engines import EngineError, run_cli
        choice = self.current()
        try:
            if choice in ("claude-code", "codex"):
                out = run_cli(self.s.engines[CHOICES[choice]["model"][4:]], system="You are testing a connection.",
                              prompt="Reply with just: OK", cwd=None, model="", write=False, run=False, timeout=180)
                ok, text = (not out["is_error"] and "OK" in out["text"]), out["text"]
            else:
                r = self.co.runtime.provider.complete(model=self.s.default_model, system="You are testing a connection.",
                                                      messages=[{"role": "user", "content": "Reply with just: OK"}],
                                                      tools=[], max_tokens=20)
                ok, text = ("OK" in r.text or choice == "demo"), r.text
        except (EngineError, RuntimeError, ValueError, KeyError) as exc:
            ok, text = False, str(exc)
        except Exception as exc:                                  # e.g. the API rejected the key
            ok, text = False, f"{type(exc).__name__}: {exc}"
        return {"ok": ok, "reply": str(text)[:300], "choice": choice}
