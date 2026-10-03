"""Dashboard server. The CEO and CTO each sign in with their own token."""
from __future__ import annotations

import os
import secrets
import threading
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .company import Company
from .org import OrgError
from .pipeline import STAGE_LABELS, STAGES, PipelineError

STATIC = Path(__file__).with_name("static")


class NewProject(BaseModel):
    name: str
    brief: str


class Decision(BaseModel):
    decision: str
    feedback: str = ""


class Hire(BaseModel):
    role: str
    name: str | None = None
    model: str | None = None


class Fire(BaseModel):
    reason: str = "Decision by management"
    replace: bool = True


class Rating(BaseModel):
    score: float
    note: str = ""


def create_app(co: Company, tokens: dict[str, str]) -> FastAPI:
    app = FastAPI(title="OrgForge", docs_url=None, redoc_url=None)

    def auth(x_token: str = Header(default="")) -> str:
        for role, token in tokens.items():
            if x_token and secrets.compare_digest(x_token, token):
                return role
        raise HTTPException(401, "Sign in with your CEO or CTO token.")

    def run_in_background(pid: int) -> None:
        def work():
            try:
                co.pipeline.advance(pid)
            except Exception:
                pass                            # already written to the activity log
        threading.Thread(target=work, daemon=True).start()

    def guard(fn):
        try:
            return fn()
        except PermissionError as exc:
            raise HTTPException(403, str(exc))
        except (OrgError, PipelineError) as exc:
            raise HTTPException(400, str(exc))

    def managed(role: str, ref: int) -> dict:
        agent = guard(lambda: co.org.agent(ref))
        if not co.can_manage(role, agent):
            raise HTTPException(403, f"{agent['name']}'s department reports to the CEO.")
        return agent

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (STATIC / "index.html").read_text()

    @app.get("/api/state")
    def state(role: str = Depends(auth)) -> dict:
        return {
            "company": co.s.company,
            "you": {"role": role, "name": co.s.human(role)},
            "humans": {"ceo": co.s.ceo_name, "cto": co.s.cto_name},
            "stages": [{"id": s, "label": STAGE_LABELS[s]} for s in STAGES],
            "departments": co.org.chart(include_fired=True),
            "projects": co.pipeline.overview(),
            "approvals": co.pipeline.inbox(),
            "events": co.events(60)[::-1],
        }

    @app.post("/api/projects")
    def new_project(body: NewProject, role: str = Depends(auth)) -> dict:
        if not body.name.strip() or not body.brief.strip():
            raise HTTPException(400, "A project needs a name and a brief.")
        project = guard(lambda: co.pipeline.create_project(body.name.strip(), body.brief.strip(), by=co.s.human(role)))
        run_in_background(project["id"])
        return project

    @app.post("/api/projects/{pid}/run")
    def run_project(pid: int, role: str = Depends(auth)) -> dict:
        project = guard(lambda: co.pipeline.project(pid))
        run_in_background(pid)
        return project

    @app.post("/api/approvals/{aid}")
    def decide(aid: int, body: Decision, role: str = Depends(auth)) -> dict:
        approval = guard(lambda: co.pipeline.decide(aid, role, body.decision, body.feedback))
        if approval["project_id"]:
            run_in_background(approval["project_id"])
        return approval

    @app.post("/api/agents")
    def hire(body: Hire, role: str = Depends(auth)) -> dict:
        role_row = guard(lambda: co.org.role(body.role))
        dept = co.db.one("SELECT reports_to FROM departments WHERE id=?", role_row["department"])
        if role != "ceo" and dept["reports_to"] != "cto":
            raise HTTPException(403, "That department reports to the CEO.")
        return guard(lambda: co.org.hire(body.role, name=body.name or None, model=body.model or None, by=co.s.human(role)))

    @app.post("/api/agents/{agent_id}/fire")
    def fire(agent_id: int, body: Fire, role: str = Depends(auth)) -> dict:
        managed(role, agent_id)
        by = co.s.human(role)
        if body.replace:
            return guard(lambda: co.org.replace(agent_id, body.reason, by=by))
        return guard(lambda: co.org.fire(agent_id, body.reason, by=by))

    @app.post("/api/agents/{agent_id}/rehire")
    def rehire(agent_id: int, role: str = Depends(auth)) -> dict:
        managed(role, agent_id)
        return guard(lambda: co.org.rehire(agent_id, by=co.s.human(role)))

    @app.post("/api/agents/{agent_id}/rate")
    def rate(agent_id: int, body: Rating, role: str = Depends(auth)) -> dict:
        managed(role, agent_id)
        co.perf.record(agent_id, body.score, source="human", reviewer=co.s.human(role), notes=body.note)
        return {"outcome": co.perf.evaluate(agent_id), "agent": co.org.agent(agent_id)}

    return app


def serve(co: Company, host: str, port: int) -> None:
    import uvicorn

    tokens = {}
    for role in ("ceo", "cto"):
        env = f"ORGFORGE_{role.upper()}_TOKEN"
        tokens[role] = os.environ.get(env) or secrets.token_urlsafe(18)
        if not os.environ.get(env):
            print(f"{role.upper()} token (set {env} to keep it fixed): {tokens[role]}")
    if tokens["ceo"] == tokens["cto"]:
        raise SystemExit("The CEO and CTO tokens must differ.")
    print(f"{co.s.company} dashboard: http://{host}:{port}")
    uvicorn.run(create_app(co, tokens), host=host, port=port, log_level="warning")
