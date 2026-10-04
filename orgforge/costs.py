"""What the company's AI work costs: a ledger of every model call, and per-project budgets.

Prices are USD per million tokens (input, output): Anthropic API list prices as of
September 2026. Set `llm.prices` in org.yaml to override them or to price other models.
Costs are estimates; your Anthropic bill is the source of truth.
"""
from __future__ import annotations

from .config import Settings
from .db import DB, now

DEFAULT_PRICES = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}


def price(settings: Settings, model: str) -> tuple[float, float] | None:
    custom = (settings.prices or {}).get(model)
    if custom:
        return float(custom.get("input", 0)), float(custom.get("output", 0))
    return DEFAULT_PRICES.get(model)


def record_usage(db: DB, settings: Settings, agent: dict, project_id: int | None, input_tokens: int,
                 output_tokens: int, purpose: str = "") -> float:
    """Add one model call to the ledger (and the agent's running totals). Returns its estimated cost."""
    rate = price(settings, agent["model"])
    cost = (input_tokens * rate[0] + output_tokens * rate[1]) / 1_000_000 if rate else 0.0
    db.run("INSERT INTO usage (agent_id, project_id, model, input_tokens, output_tokens, cost, priced, purpose, "
           "created_at) VALUES (?,?,?,?,?,?,?,?,?)", agent["id"], project_id, agent["model"], input_tokens,
           output_tokens, cost, 1 if rate else 0, purpose, now())
    db.run("UPDATE agents SET input_tokens=input_tokens+?, output_tokens=output_tokens+? WHERE id=?",
           input_tokens, output_tokens, agent["id"])
    return cost


def spent(db: DB, project_id: int) -> float:
    return db.one("SELECT COALESCE(SUM(cost), 0) AS c FROM usage WHERE project_id=?", project_id)["c"]


def summary(db: DB) -> dict:
    """Company-wide cost picture for the dashboard."""
    total = db.one("SELECT COALESCE(SUM(cost), 0) AS cost, COALESCE(SUM(input_tokens), 0) AS input_tokens, "
                   "COALESCE(SUM(output_tokens), 0) AS output_tokens FROM usage")
    projects = db.all("SELECT p.id, p.name, p.budget, p.stage, COALESCE(SUM(u.cost), 0) AS spent FROM projects p "
                      "LEFT JOIN usage u ON u.project_id=p.id GROUP BY p.id ORDER BY p.id DESC")
    departments = db.all("SELECT d.name AS department, COALESCE(SUM(u.cost), 0) AS spent FROM usage u "
                         "JOIN agents a ON a.id=u.agent_id JOIN roles r ON r.id=a.role "
                         "JOIN departments d ON d.id=r.department GROUP BY d.id ORDER BY spent DESC")
    agents = db.all("SELECT a.name, a.role, COALESCE(SUM(u.cost), 0) AS spent FROM usage u JOIN agents a "
                    "ON a.id=u.agent_id GROUP BY a.id ORDER BY spent DESC LIMIT 10")
    unpriced = [r["model"] for r in db.all("SELECT DISTINCT model FROM usage WHERE priced=0")]
    other = db.one("SELECT COALESCE(SUM(cost), 0) AS c FROM usage WHERE project_id IS NULL")["c"]
    return {"total": total, "projects": projects, "departments": departments, "agents": agents,
            "unpriced_models": unpriced, "outside_projects": other}
