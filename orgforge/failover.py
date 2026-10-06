"""Engine failover: when an engine hits its usage limit, the work moves to the next one instead of pausing.

Off by default. With `failover.enabled`, an engine that refuses work (a subscription's session limit, an
API rate limit, a local model server that is not running) rests until its stated reset time, or for
`cooldown_minutes`. The same assignment is then retried on the next model in `failover.models` that is
ready and not resting. Only when every option is resting does the project pause, as before.
"""
from __future__ import annotations

import math
import re
import time
from datetime import datetime, timedelta

from .db import now

RESETS_AT = re.compile(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)", re.I)
RESETS_IN = re.compile(r"(?:in|after)\s+(\d+)\s*(second|sec|minute|min|hour|hr)s?\b", re.I)


def limit_key(model: str) -> str:
    """What a usage limit applies to: a CLI's whole login, or one API model."""
    model = str(model)
    if model.startswith("cli:"):
        return "cli:" + model[4:].split("/")[0]
    return model


def reset_after(message: str, default_minutes: float, clock: datetime | None = None) -> float:
    """Seconds until the engine should be tried again, read from its message when it says."""
    clock = clock or datetime.now()
    if m := RESETS_AT.search(message or ""):
        hour = int(m[1]) % 12 + (12 if m[3].lower() == "pm" else 0)
        at = clock.replace(hour=hour, minute=int(m[2] or 0), second=0, microsecond=0)
        if at <= clock:
            at += timedelta(days=1)
        seconds = (at - clock).total_seconds()
    elif m := RESETS_IN.search(message or ""):
        unit = m[2].lower()
        seconds = int(m[1]) * (3600 if unit.startswith("h") else 60 if unit.startswith("m") else 1)
    else:
        seconds = default_minutes * 60
    return min(max(seconds, 60), 24 * 3600)


class Failover:
    def __init__(self, db, settings, router=None) -> None:
        self.db, self.s, self.router = db, settings, router

    @property
    def config(self) -> dict:
        return self.s.raw.get("failover") or {}

    @property
    def enabled(self) -> bool:
        return self.config.get("enabled") is True

    def models(self) -> list[str]:
        models = self.config.get("models") or []
        if not isinstance(models, list) or any(not isinstance(m, str) or not m.strip() for m in models):
            raise ValueError("failover.models must be a list of model names.")
        return models

    def resting(self, model: str) -> dict | None:
        row = self.db.one("SELECT * FROM engine_cooldowns WHERE engine=? AND until>?", limit_key(model), time.time())
        return row

    def rest(self, model: str, message: str) -> float:
        """Mark the engine as resting; returns when it can be tried again (epoch seconds)."""
        until = time.time() + reset_after(message, float(self.config.get("cooldown_minutes", 60)))
        self.db.run("INSERT INTO engine_cooldowns(engine, until, reason, created_at) VALUES (?,?,?,?) "
                    "ON CONFLICT(engine) DO UPDATE SET until=excluded.until, reason=excluded.reason, "
                    "created_at=excluded.created_at", limit_key(model), until, str(message)[:300], now())
        return until

    def clear(self, model: str | None = None) -> int:
        """Try an engine again now (e.g. after upgrading a plan). Returns how many were resting."""
        where, args = (" WHERE engine=?", [limit_key(model)]) if model else ("", [])
        count = self.db.one("SELECT COUNT(*) AS n FROM engine_cooldowns" + where, *args)["n"]
        self.db.run("DELETE FROM engine_cooldowns" + where, *args)
        return count

    def status(self) -> list[dict]:
        return self.db.all("SELECT * FROM engine_cooldowns WHERE until>? ORDER BY until", time.time())

    def pick(self, first: str, *, exclude: set[str] = frozenset(), avoid: str | None = None) -> str | None:
        """The first model, in order, that is ready, not resting and not ruled out."""
        ruled_out = {limit_key(m) for m in exclude}
        for model in dict.fromkeys([first, *self.models()]):
            if limit_key(model) in ruled_out or model == avoid or self.resting(model):
                continue
            if self.router and model != first and not self.router.available(model):
                continue
            return model
        return None

    @staticmethod
    def when(until: float) -> str:
        return datetime.fromtimestamp(math.ceil(until / 60) * 60).strftime("%H:%M")
