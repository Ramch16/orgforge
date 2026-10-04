"""The org chart: departments, roles, seats, and who sits in them."""
from __future__ import annotations

import json

from .config import Settings
from .db import DB, now
from .tools import TOOL_SPECS

KINDS = ("product", "designer", "planner", "builder", "reviewer", "qa", "auditor")
NAME_POOL = [
    "Amara", "Bao", "Carmen", "Dmitri", "Elif", "Farid", "Greta", "Hana", "Idris", "Jonas",
    "Kavya", "Leon", "Malia", "Noor", "Oskar", "Paloma", "Quinn", "Rafael", "Sana", "Teo",
    "Uma", "Viktor", "Wren", "Ximena", "Yusuf", "Zofia",
]


class OrgError(Exception):
    pass


class Org:
    def __init__(self, db: DB, settings: Settings) -> None:
        self.db, self.s = db, settings
        self.namer = None        # set by Company: lets a teammate name new hires (see naming.py)

    # ---- setup -----------------------------------------------------------
    def seed(self) -> bool:
        """Populate an empty database from org.yaml. Returns False if already seeded."""
        if self.db.one("SELECT 1 FROM departments LIMIT 1"):
            return False
        raw = self.s.raw
        for d in raw.get("departments", []):
            self.add_department(d["id"], d.get("name", d["id"].title()), d.get("reports_to", "cto"), log=False)
        for role_id, r in (raw.get("roles") or {}).items():
            self.add_role(role_id, r["department"], r["kind"], r.get("tools", []), r.get("prompt", ""),
                          title=r.get("title"), log=False)
        for seat in raw.get("seats", []):
            self.hire(seat["role"], name=seat.get("name"), seat=seat.get("seat"), model=seat.get("model"), by="setup")
        self.db.log("org", f"{self.s.company} opened with {len(raw.get('seats', []))} agents.")
        return True

    def sync(self, raw: dict, by: str = "ceo") -> dict:
        """Add whatever an org file defines that this company lacks: departments,
        roles, and seats that have never been filled. Nothing existing is changed."""
        added = {"departments": [], "roles": [], "agents": []}
        for d in raw.get("departments", []):
            if not self.db.one("SELECT 1 FROM departments WHERE id=?", d["id"]):
                self.add_department(d["id"], d.get("name", d["id"].title()), d.get("reports_to", "cto"))
                added["departments"].append(d["id"])
        for role_id, r in (raw.get("roles") or {}).items():
            if not self.db.one("SELECT 1 FROM roles WHERE id=?", role_id):
                self.add_role(role_id, r["department"], r["kind"], r.get("tools", []), r.get("prompt", ""), title=r.get("title"))
                added["roles"].append(role_id)
        for seat in raw.get("seats", []):
            if seat.get("seat") and not self.db.one("SELECT 1 FROM agents WHERE seat=?", seat["seat"]):
                name = seat.get("name")
                if name and self.db.one("SELECT 1 FROM agents WHERE name=?", name):
                    name = None
                a = self.hire(seat["role"], name=name, seat=seat["seat"], model=seat.get("model"), by=by)
                added["agents"].append(a["name"])
        return added

    # ---- departments and roles ------------------------------------------
    def add_department(self, dept_id: str, name: str, reports_to: str, log: bool = True) -> None:
        if reports_to not in ("ceo", "cto"):
            raise OrgError("A department reports to 'ceo' or 'cto'.")
        if self.db.one("SELECT 1 FROM departments WHERE id=?", dept_id):
            raise OrgError(f"Department '{dept_id}' already exists.")
        self.db.run("INSERT INTO departments (id, name, reports_to) VALUES (?,?,?)", dept_id, name, reports_to)
        if log:
            self.db.log("org", f"Department {name} created, reporting to the {reports_to.upper()}.")

    def remove_department(self, dept_id: str) -> None:
        if not self.db.one("SELECT 1 FROM departments WHERE id=?", dept_id):
            raise OrgError(f"No department '{dept_id}'.")
        staffed = self.db.one(
            "SELECT COUNT(*) AS n FROM agents a JOIN roles r ON r.id=a.role WHERE r.department=? AND a.status!='fired'",
            dept_id)["n"]
        if staffed:
            raise OrgError(f"Department '{dept_id}' still has {staffed} active agent(s). Let them go first.")
        if self.db.one("SELECT 1 FROM agents a JOIN roles r ON r.id=a.role WHERE r.department=? LIMIT 1", dept_id):
            raise OrgError(f"Department '{dept_id}' has former agents on record and cannot be deleted.")
        self.db.run("DELETE FROM roles WHERE department=?", dept_id)
        self.db.run("DELETE FROM departments WHERE id=?", dept_id)
        self.db.log("org", f"Department {dept_id} closed.")

    def add_role(self, role_id: str, department: str, kind: str, tools: list[str], prompt: str,
                 title: str | None = None, log: bool = True) -> None:
        if kind not in KINDS:
            raise OrgError(f"Role kind must be one of {', '.join(KINDS)}.")
        if not self.db.one("SELECT 1 FROM departments WHERE id=?", department):
            raise OrgError(f"No department '{department}'.")
        unknown = [t for t in tools if t not in TOOL_SPECS]
        if unknown:
            raise OrgError(f"Unknown tool(s): {', '.join(unknown)}. Available: {', '.join(TOOL_SPECS)}.")
        if self.db.one("SELECT 1 FROM roles WHERE id=?", role_id):
            raise OrgError(f"Role '{role_id}' already exists.")
        self.db.run("INSERT INTO roles (id, title, department, kind, tools, prompt) VALUES (?,?,?,?,?,?)",
                    role_id, title or role_id.replace("_", " ").title(), department, kind, json.dumps(tools), prompt.strip())
        if log:
            self.db.log("org", f"Role {role_id} added to {department}.")

    # ---- lookups ---------------------------------------------------------
    def agent(self, ref: int | str) -> dict:
        """Find an agent by id, or by name/seat among current staff."""
        row = None
        if str(ref).isdigit():
            row = self.db.one("SELECT * FROM agents WHERE id=?", int(ref))
        if not row:
            row = self.db.one(
                "SELECT * FROM agents WHERE (lower(name)=lower(?) OR seat=?) ORDER BY status='fired', id DESC LIMIT 1",
                str(ref), str(ref))
        if not row:
            raise OrgError(f"No agent '{ref}'.")
        return row

    def role(self, role_id: str) -> dict:
        row = self.db.one("SELECT * FROM roles WHERE id=?", role_id)
        if not row:
            raise OrgError(f"No role '{role_id}'.")
        return row

    def boss_of(self, agent: dict) -> str:
        """'ceo' or 'cto': who the agent's department reports to."""
        return self.db.one(
            "SELECT d.reports_to FROM roles r JOIN departments d ON d.id=r.department WHERE r.id=?",
            agent["role"])["reports_to"]

    def staff(self, *, role: str | None = None, kind: str | None = None) -> list[dict]:
        sql = ("SELECT a.* FROM agents a JOIN roles r ON r.id=a.role WHERE a.status!='fired'"
               + (" AND a.role=?" if role else "") + (" AND r.kind=?" if kind else ""))
        return self.db.all(sql, *[v for v in (role, kind) if v])

    def staffed_roles(self, *kinds: str) -> list[dict]:
        """Roles of the given kinds that currently have at least one agent."""
        marks = ",".join("?" * len(kinds))
        return self.db.all(
            f"SELECT DISTINCT r.* FROM roles r JOIN agents a ON a.role=r.id WHERE a.status!='fired' "
            f"AND r.kind IN ({marks}) ORDER BY r.kind, r.id", *kinds)

    def pick(self, *, role: str | None = None, kind: str | None = None, project_id: int | None = None,
             exclude: int | set[int] | None = None) -> dict | None:
        """Choose who gets the next piece of work: spread the load, then prefer
        agents in good standing with the best record. `exclude` is one agent id or a set (e.g. busy agents)."""
        skip = exclude if isinstance(exclude, (set, frozenset)) else {exclude}
        candidates = [a for a in self.staff(role=role, kind=kind) if a["id"] not in skip]
        if not candidates:
            return None

        def load(a: dict) -> int:
            return self.db.one("SELECT COUNT(*) AS n FROM tasks WHERE assignee_id=? AND (? IS NULL OR project_id=?)",
                               a["id"], project_id, project_id)["n"]

        return min(candidates, key=lambda a: (load(a), a["status"] != "active", -(75 if a["score"] is None else a["score"]), a["id"]))

    # ---- hiring and firing ----------------------------------------------
    def _fresh_name(self, role_id: str | None = None) -> str:
        used = {r["name"] for r in self.db.all("SELECT name FROM agents")}
        if role_id and self.namer:
            try:
                if name := self.namer(role_id, used):
                    return name
            except Exception as exc:             # e.g. no API key: fall back to the name pool
                self.db.log("warn", f"A teammate could not name the new {role_id}: {exc}")
        for name in NAME_POOL:
            if name not in used:
                return name
        return f"Agent-{len(used) + 1}"

    def _next_seat(self, role_id: str) -> str:
        prefix = "".join(w[0] for w in role_id.split("_"))[:4]
        taken = {r["seat"] for r in self.db.all("SELECT seat FROM agents")}
        n = 1
        while f"{prefix}-{n}" in taken:
            n += 1
        return f"{prefix}-{n}"

    def hire(self, role_id: str, *, name: str | None = None, seat: str | None = None, model: str | None = None,
             by: str = "ceo", generation: int = 1, predecessor_id: int | None = None, lessons: str = "") -> dict:
        self.role(role_id)
        seat = seat or self._next_seat(role_id)
        if self.db.one("SELECT 1 FROM agents WHERE seat=? AND status!='fired'", seat):
            raise OrgError(f"Seat '{seat}' is already filled.")
        agent_id = self.db.run(
            "INSERT INTO agents (seat, name, role, model, generation, predecessor_id, lessons, hired_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            seat, name or self._fresh_name(role_id if by != "setup" else None), role_id, model or self.s.default_model, generation,
            predecessor_id, lessons, now())
        agent = self.agent(agent_id)
        if by != "setup":
            self.db.log("hire", f"{agent['name']} hired as {role_id} (seat {seat}, {agent['model']}).", actor=by)
        return agent

    def set_model(self, ref: int | str, model: str, by: str = "ceo") -> dict:
        """Move an agent to another model or engine (e.g. claude-opus-5-5, cli:claude-code, cli:claude-code/haiku)."""
        agent = self.agent(ref)
        model = model.strip()
        if not model:
            raise OrgError("Give a model, e.g. claude-sonnet-5-5 or cli:claude-code.")
        if model.startswith("cli:") and model[4:].partition("/")[0] not in self.s.engines:
            raise OrgError(f"No engine '{model[4:].partition('/')[0]}'. Known: {', '.join(sorted(self.s.engines))}.")
        prefix, sep, rest = model.partition(":")
        if sep and prefix != "cli" and (prefix not in self.s.endpoints or not rest):
            raise OrgError(f"Unknown model source '{prefix}'. Use one of: {', '.join(sorted(self.s.endpoints))}, cli:<engine>,"
                           " or an Anthropic model id.")
        self.db.run("UPDATE agents SET model=? WHERE id=?", model, agent["id"])
        self.db.log("org", f"{agent['name']} now works on {model}.", actor=by)
        return self.agent(agent["id"])

    def fire(self, ref: int | str, reason: str, by: str = "ceo") -> dict:
        agent = self.agent(ref)
        if agent["status"] == "fired":
            raise OrgError(f"{agent['name']} has already left the company.")
        self.db.run("UPDATE agents SET status='fired', fired_at=?, fire_reason=? WHERE id=?", now(), reason, agent["id"])
        self.db.log("fire", f"{agent['name']} ({agent['role']}) let go: {reason}", actor=by)
        return self.agent(agent["id"])

    def _stronger_model(self, model: str) -> str:
        ladder = self.s.model_ladder
        if self.s.hr.escalate_model and model in ladder and ladder.index(model) + 1 < len(ladder):
            return ladder[ladder.index(model) + 1]
        return model

    def _exit_lessons(self, agent: dict) -> str:
        worst = self.db.all(
            "SELECT notes FROM reviews WHERE agent_id=? AND notes!='' ORDER BY score ASC, id DESC LIMIT 4", agent["id"])
        lines = [f"- {w['notes'].strip()[:400]}" for w in worst]
        header = (f"The previous holder of this seat ({agent['name']}) was let go for poor results. "
                  "Reviewers flagged these problems; do not repeat them:")
        own = "\n".join([header, *lines]) if lines else ""
        return "\n\n".join(x for x in (agent["lessons"], own) if x)

    def replace(self, ref: int | str, reason: str, by: str = "ceo") -> dict:
        """Fire an agent and hire a successor into the same seat. The successor
        inherits the lessons from the predecessor's worst reviews and, if
        configured, a stronger model."""
        old = self.agent(ref)
        self.fire(old["id"], reason, by)
        return self.hire(old["role"], seat=old["seat"], model=self._stronger_model(old["model"]), by=by,
                         generation=old["generation"] + 1, predecessor_id=old["id"], lessons=self._exit_lessons(old))

    def rehire(self, ref: int | str, by: str = "ceo", note: str = "") -> dict:
        """Bring a fired agent back into their seat with a clean record.
        Whoever holds the seat now steps down."""
        agent = self.agent(ref)
        if agent["status"] != "fired":
            raise OrgError(f"{agent['name']} is still with the company.")
        holder = self.db.one("SELECT * FROM agents WHERE seat=? AND status!='fired'", agent["seat"])
        if holder:
            self.fire(holder["id"], f"{agent['name']} was reinstated in seat {agent['seat']}", by)
        lesson = "You were let go and then reinstated. Earlier feedback on your work still applies." + (f" {note}" if note else "")
        self.db.run(
            "UPDATE agents SET status='active', fired_at=NULL, fire_reason=NULL, score=NULL, evals=0, "
            "probation_at=NULL, lessons=? WHERE id=?",
            "\n\n".join(x for x in (agent["lessons"], lesson) if x), agent["id"])
        self.db.log("rehire", f"{agent['name']} reinstated as {agent['role']} (seat {agent['seat']}).", actor=by)
        return self.agent(agent["id"])

    # ---- views -----------------------------------------------------------
    def chart(self, include_fired: bool = False) -> list[dict]:
        out = []
        for d in self.db.all("SELECT * FROM departments ORDER BY reports_to, id"):
            agents = self.db.all(
                "SELECT a.*, r.title AS role_title, r.kind FROM agents a JOIN roles r ON r.id=a.role "
                "WHERE r.department=?" + ("" if include_fired else " AND a.status!='fired'") +
                " ORDER BY a.status='fired', a.seat, a.generation", d["id"])
            roles = self.db.all("SELECT id, title, kind FROM roles WHERE department=? ORDER BY id", d["id"])
            out.append({**d, "agents": agents, "roles": roles})
        return out
