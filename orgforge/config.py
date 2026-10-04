"""Settings loaded from org.yaml."""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_ORG = Path(__file__).with_name("default_org.yaml")


@dataclass
class HRPolicy:
    auto_fire: bool = False
    min_tasks: int = 3
    probation_below: float = 60
    fire_below: float = 40
    probation_tasks: int = 2
    recovery_margin: float = 5
    rehire_margin: float = 5
    escalate_model: bool = True
    alpha: float = 0.4


@dataclass
class Settings:
    root: Path
    raw: dict = field(default_factory=dict)
    company: str = "Your Software Company"
    ceo_name: str = "CEO"
    cto_name: str = "CTO"
    provider: str = "anthropic"
    default_model: str = "claude-sonnet-5-5"
    model_ladder: list[str] = field(default_factory=list)
    max_turns: int = 40
    max_tokens: int = 8000
    sandbox_mode: str = "local"
    docker_image: str = "python:3.12-slim"
    docker_network: bool = False
    command_timeout: int = 300
    max_rework: int = 2
    max_delegation_depth: int = 2
    max_parallel: int = 3
    hire_when_waiting: int = 3
    max_per_role: int = 4
    prices: dict = field(default_factory=dict)
    default_budget: float = 25.0
    budget_warn_at: float = 0.8
    auto_reports: bool = True
    hr: HRPolicy = field(default_factory=HRPolicy)

    @property
    def db_path(self) -> Path:
        return self.root / ".orgforge" / "company.db"

    @property
    def workspaces(self) -> Path:
        return self.root / "workspaces"

    def human(self, role: str) -> str:
        return self.ceo_name if role == "ceo" else self.cto_name


def resolve_root(root: str | os.PathLike | None = None) -> Path:
    return Path(root or os.environ.get("ORGFORGE_HOME") or Path.cwd()).resolve()


def ensure_org_file(root: Path) -> Path:
    """Copy the default org.yaml into the company folder if it has none."""
    target = root / "org.yaml"
    if not target.exists():
        root.mkdir(parents=True, exist_ok=True)
        shutil.copy(DEFAULT_ORG, target)
    return target


def load_settings(root: str | os.PathLike | None = None) -> Settings:
    root = resolve_root(root)
    org_file = root / "org.yaml"
    raw = yaml.safe_load((org_file if org_file.exists() else DEFAULT_ORG).read_text()) or {}
    llm, sandbox = raw.get("llm", {}), raw.get("sandbox", {})
    pipeline, humans = raw.get("pipeline", {}), raw.get("humans", {})
    hr_known = {k: v for k, v in raw.get("hr", {}).items() if k in HRPolicy.__dataclass_fields__}
    s = Settings(root=root, raw=raw, hr=HRPolicy(**hr_known))
    s.company = raw.get("company", s.company)
    s.ceo_name = (humans.get("ceo") or {}).get("name", s.ceo_name)
    s.cto_name = (humans.get("cto") or {}).get("name", s.cto_name)
    s.provider = os.environ.get("ORGFORGE_PROVIDER") or llm.get("provider", s.provider)
    s.default_model = llm.get("default_model", s.default_model)
    s.model_ladder = list(llm.get("model_ladder") or [s.default_model])
    s.max_turns = int(llm.get("max_turns", s.max_turns))
    s.max_tokens = int(llm.get("max_tokens", s.max_tokens))
    s.sandbox_mode = sandbox.get("mode", s.sandbox_mode)
    s.docker_image = sandbox.get("docker_image", s.docker_image)
    s.docker_network = bool(sandbox.get("docker_network", s.docker_network))
    s.command_timeout = int(sandbox.get("command_timeout", s.command_timeout))
    s.max_rework = int(pipeline.get("max_rework", s.max_rework))
    s.max_delegation_depth = int(pipeline.get("max_delegation_depth", s.max_delegation_depth))
    s.max_parallel = max(1, int(pipeline.get("max_parallel", s.max_parallel)))
    staffing = raw.get("staffing", {})
    s.hire_when_waiting = max(1, int(staffing.get("hire_when_waiting", s.hire_when_waiting)))
    s.max_per_role = max(1, int(staffing.get("max_per_role", s.max_per_role)))
    s.prices = dict(llm.get("prices") or {})
    budgets = raw.get("budgets", {})
    s.default_budget = max(0.0, float(budgets.get("default_project_usd", s.default_budget)))
    s.budget_warn_at = min(1.0, max(0.1, float(budgets.get("warn_at", s.budget_warn_at))))
    s.auto_reports = bool((raw.get("reports") or {}).get("automatic", s.auto_reports))
    return s
