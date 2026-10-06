"""New hires are named by their future teammates.

When an agent joins without a name (a workload hire, a replacement, or a hire with the
name left blank), the longest-serving agent in that department picks one. The name must be
a single first name nobody at the company already uses. If the agent cannot come up with a
usable name, or the model is unavailable, the built-in name pool is used instead.
"""
from __future__ import annotations

import re

from .config import Settings
from .costs import record_usage
from .db import DB

NAME = re.compile(r"^[A-Za-z][A-Za-z'-]{1,19}$")


def make_namer(db: DB, settings: Settings, provider):
    def name_new_hire(role_id: str, taken: set[str]) -> str | None:
        role = db.one("SELECT r.title, r.department, d.name AS dept FROM roles r JOIN departments d "
                      "ON d.id=r.department WHERE r.id=?", role_id)
        if not role:
            return None
        namer = (db.one("SELECT a.* FROM agents a JOIN roles r ON r.id=a.role WHERE a.status!='fired' "
                        "AND r.department=? ORDER BY a.status!='active', a.id LIMIT 1", role["department"])
                 or db.one("SELECT * FROM agents WHERE status!='fired' ORDER BY status!='active', id LIMIT 1"))
        if not namer:
            return None
        blocked = {n.lower() for n in taken} | {settings.ceo_name.lower(), settings.cto_name.lower()}
        team = [r["name"] for r in db.all("SELECT a.name FROM agents a JOIN roles r ON r.id=a.role "
                                          "WHERE a.status!='fired' AND r.department=?", role["department"])]
        system = (f"You are {namer['name']} at {settings.company}, a software company staffed by AI agents and led by "
                  f"{settings.ceo_name} (CEO) and {settings.cto_name} (CTO).")
        messages = [{"role": "user", "content":
                     f"{role['dept']} is hiring a new {role['title']} to join "
                     + (f"you and your teammates ({', '.join(team)})" if team else "the company")
                     + ". Choose a first name for your new teammate, in the same spirit as the names already here. "
                     "It must be one first name that nobody at the company already uses: "
                     + ", ".join(sorted(taken | {settings.ceo_name, settings.cto_name}))
                     + ". Reply with the name only."}]
        meta = {"purpose": "name_hire", "kind": "naming", "role": role_id, "agent": namer["name"],
                "taken": sorted(blocked)}
        for _ in range(2):
            resp = provider.complete(model=namer["model"], system=system, messages=messages, tools=[],
                                     max_tokens=50, meta=meta)
            record_usage(db, settings, namer, None, resp.input_tokens, resp.output_tokens, "naming")
            raw = (resp.text or "").strip().splitlines()[0].strip(" .!\"'*`") if (resp.text or "").strip() else ""
            if NAME.match(raw) and raw.lower() not in blocked:
                name = raw[0].upper() + raw[1:]
                db.log("hire", f"{namer['name']} ({role['dept']}) named the new {role['title']} {name}.",
                       actor=namer["name"])
                return name
            why = "is already taken" if raw.lower() in blocked else "is not a single first name"
            messages += [{"role": "assistant", "content": resp.text or "(no reply)"},
                         {"role": "user", "content": f"'{raw}' {why}. Reply with a different single first name only."}]
        return None

    return name_new_hire
