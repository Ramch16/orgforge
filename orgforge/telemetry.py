"""Live picture of the company: who is busy, and how much work and spend is happening."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .db import DB


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def presence(db: DB) -> list[dict]:
    """Each current agent's status: working (has a ticket in progress), active (did something in the last few
    minutes), or idle, with what they are doing."""
    now = datetime.now(timezone.utc)
    recent = _iso(now - timedelta(minutes=4))
    out = []
    for a in db.all("SELECT a.id, a.name, a.role, a.status, a.model, r.title, d.name AS department FROM agents a "
                    "JOIN roles r ON r.id=a.role JOIN departments d ON d.id=r.department WHERE a.status!='fired' "
                    "ORDER BY d.name, a.id"):
        ticket = db.one("SELECT id, title, status FROM tasks WHERE assignee_id=? AND status IN ('in_progress','in_review') "
                        "ORDER BY updated_at DESC LIMIT 1", a["id"])
        event = db.one("SELECT ts, message FROM events WHERE actor=? AND ts>=? ORDER BY id DESC LIMIT 1", a["name"], recent)
        call = db.one("SELECT created_at FROM usage WHERE agent_id=? AND created_at>=? ORDER BY id DESC LIMIT 1",
                      a["id"], recent)
        if ticket:
            state = "working"
            doing = f"{'Reviewing' if ticket['status'] == 'in_review' else 'Working on'} T-{ticket['id']} {ticket['title']}"
        elif event or call:
            state, doing = "active", (event["message"] if event else "Thinking…")
        else:
            state, doing = "idle", ""
        out.append({**a, "state": state, "doing": doing, "ticket_id": ticket["id"] if ticket else None})
    return out


def telemetry(db: DB, hours: int = 12, bucket_minutes: int = 30) -> dict:
    """Calls, tokens and cost in the last hour and today, plus a series of buckets for a sparkline."""
    now = datetime.now(timezone.utc)
    since = _iso(now - timedelta(hours=hours))
    rows = db.all("SELECT created_at, input_tokens, output_tokens, cost FROM usage WHERE created_at>=?", since)
    n = hours * 60 // bucket_minutes
    series = [{"calls": 0, "tokens": 0, "cost": 0.0} for _ in range(n)]
    for r in rows:
        age = (now - datetime.fromisoformat(r["created_at"])).total_seconds() / 60
        i = n - 1 - int(age // bucket_minutes)
        if 0 <= i < n:
            series[i]["calls"] += 1
            series[i]["tokens"] += r["input_tokens"] + r["output_tokens"]
            series[i]["cost"] += r["cost"]
    hour = _iso(now - timedelta(hours=1))
    day = _iso(now.replace(hour=0, minute=0, second=0, microsecond=0))
    last_hour = db.one("SELECT COUNT(*) AS calls, COALESCE(SUM(input_tokens+output_tokens),0) AS tokens, "
                       "COALESCE(SUM(cost),0) AS cost FROM usage WHERE created_at>=?", hour)
    today = db.one("SELECT COUNT(*) AS calls, COALESCE(SUM(input_tokens+output_tokens),0) AS tokens, "
                   "COALESCE(SUM(cost),0) AS cost FROM usage WHERE created_at>=?", day)
    done_today = db.one("SELECT COUNT(*) AS n FROM tasks WHERE status='done' AND updated_at>=?", day)["n"]
    return {"series": series, "bucket_minutes": bucket_minutes, "last_hour": last_hour, "today": today,
            "tickets_done_today": done_today}
