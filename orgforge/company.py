"""Wires the pieces together. `Company` is the one object the CLI and server use."""
from __future__ import annotations

import os

from .agent import AgentRuntime
from .chat import Chat
from .feedback import Feedback
from .reports import Reports
from .config import ensure_org_file, load_settings, resolve_root
from .db import DB
from .llm import RoutingProvider, make_provider
from .integrations import Integrations
from .memory import Memory
from .failover import Failover
from .machine import Machine
from .nodes import Nodes
from .production import Production
from .routing import ModelRouter
from .operations_store import initialize
from .learning import Learning
from .observability import Observability
from .workers import Workers
from .arena import Arena
from .assessments import Assessments
from .marketplace import Marketplace
from .federation import Federation
from .security import Security
from .swarms import Swarms
from .runs import Runs
from .skills import Skills
from .naming import make_namer
from .org import Org
from .performance import Performance
from .pipeline import Pipeline
from .tickets import Tickets


class _LazyProvider:
    """Creates the real provider on first use, so org and status commands work without an API key."""

    def __init__(self, name: str) -> None:
        self._name, self._real = name, None

    def complete(self, **kwargs):
        if self._real is None:
            self._real = make_provider(self._name)
        return self._real.complete(**kwargs)


class Company:
    def __init__(self, root: str | os.PathLike | None = None, provider=None, create: bool = False) -> None:
        root = resolve_root(root)
        if create:
            ensure_org_file(root)
        self.s = load_settings(root)
        if not create and not self.s.db_path.exists():
            raise SystemExit(f"No company found in {root}. Run `orgforge init` there first.")
        self.db = DB(self.s.db_path)
        initialize(self.db)
        self.org = Org(self.db, self.s)
        self.perf = Performance(self.db, self.s, self.org)
        self.runtime = AgentRuntime(self.db, self.s, self.org,
                                    RoutingProvider(provider or _LazyProvider(self.s.provider), self.s.endpoints))
        self.pipeline = Pipeline(self.db, self.s, self.org, self.perf, self.runtime)
        self.learning = Learning(self)
        self.runtime.learning = self.learning
        self.router = ModelRouter(self.db, self.s)
        self.router.learning = self.learning
        self.runtime.router = self.router
        self.failover = Failover(self.db, self.s, self.router)
        self.runtime.failover = self.failover
        self.runtime.integrations = Integrations(self.s)
        self.swarms = Swarms(self.db, self.s, self.org)
        self.pipeline.swarms = self.swarms
        self.tickets = Tickets(self.db, self.pipeline)
        self.runtime.tickets = self.tickets
        self.memory = Memory(self.db)
        self.runtime.memory = self.memory
        self.runs = Runs(self.db)
        self.runtime.runs = self.runs
        self.skills = Skills(self.s)
        self.runtime.skills = self.skills
        self.org.namer = make_namer(self.db, self.s, self.runtime.provider)
        self.chat = Chat(self)
        self.tickets.on_human_note = self.chat.ticket_note
        self.reports = Reports(self)
        self.pipeline.reporter = self.reports.write
        self.feedback = Feedback(self)
        self.observability = Observability(self)
        self.workers = Workers(self)
        self.production = Production(self)
        self.machine = Machine(self)
        self.nodes = Nodes(self)
        self.runtime.nodes = self.nodes
        self.pipeline.machine = self.machine
        self.pipeline.production = self.production
        self.arena = Arena(self)
        self.assessments = Assessments(self)
        self.marketplace = Marketplace(self)
        self.federation = Federation(self)
        self.security = Security(self)
        if create:
            self.org.seed()

    def can_manage(self, role: str, agent: dict) -> bool:
        """The CEO manages everyone. The CTO manages departments that report to the CTO."""
        return role == "ceo" or self.org.boss_of(agent) == "cto"

    def events(self, limit: int = 50, project_id: int | None = None) -> list[dict]:
        if project_id:
            rows = self.db.all("SELECT * FROM events WHERE project_id=? ORDER BY id DESC LIMIT ?", project_id, limit)
        else:
            rows = self.db.all("SELECT * FROM events ORDER BY id DESC LIMIT ?", limit)
        return rows[::-1]
