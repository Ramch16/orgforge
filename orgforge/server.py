"""Dashboard server. The CEO and CTO each sign in with their own token."""
from __future__ import annotations

import os
import secrets
import shutil
import tempfile
import threading
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel

from .chat import ChatError
from .company import Company
from .costs import summary as cost_summary
from .feedback import FeedbackError
from .review import read_file as review_file, review as review_product
from .tools import ToolError
from .org import OrgError
from .pipeline import IDEA_STAGES, STAGE_LABELS, STAGES, PipelineError
from .tickets import PRIORITIES, STATUS_LABELS, TYPES, TicketError

STATIC = Path(__file__).with_name("static")


class NewProject(BaseModel):
    name: str
    brief: str
    idea: bool = False
    budget: float | None = None


class Budget(BaseModel):
    amount: float


class CustomerFeedback(BaseModel):
    body: str
    source: str = ""


class Decision(BaseModel):
    decision: str
    feedback: str = ""
    choice: str = ""


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


class NewTicket(BaseModel):
    project_id: int
    title: str
    description: str = ""
    type: str = "task"
    priority: str = "medium"
    status: str = "backlog"
    role: str | None = None


class TicketChange(BaseModel):
    status: str | None = None
    priority: str | None = None
    title: str | None = None
    description: str | None = None
    role: str | None = None


class Comment(BaseModel):
    body: str


class Message(BaseModel):
    body: str
    project_id: int | None = None


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

    co.chat.on_work = co.feedback.on_work = run_in_background
    co.chat.background = True               # replies on tickets arrive without holding up the page

    def guard(fn):
        try:
            return fn()
        except PermissionError as exc:
            raise HTTPException(403, str(exc))
        except (OrgError, PipelineError, TicketError, ChatError, FeedbackError, ToolError) as exc:
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
            "idea_stages": IDEA_STAGES,
            "departments": co.org.chart(include_fired=True),
            "projects": co.pipeline.overview(),
            "approvals": co.pipeline.inbox(),
            "tickets": co.tickets.search(),
            "ticket_meta": {"types": TYPES, "priorities": PRIORITIES, "statuses": STATUS_LABELS,
                            "roles": co.tickets.work_roles()},
            "events": co.events(60)[::-1],
            "costs": cost_summary(co.db),
            "chats": co.db.all("SELECT agent_id, MAX(id) AS last_id, SUM(status='pending') AS pending FROM messages "
                               "WHERE human=? GROUP BY agent_id", role),
        }

    @app.post("/api/projects")
    def new_project(body: NewProject, role: str = Depends(auth)) -> dict:
        if not body.name.strip() or not body.brief.strip():
            raise HTTPException(400, "A project needs a name and a brief.")
        project = guard(lambda: co.pipeline.create_project(body.name.strip(), body.brief.strip(), by=co.s.human(role),
                                                           idea=body.idea, budget=body.budget))
        run_in_background(project["id"])
        return project

    @app.post("/api/projects/{pid}/run")
    def run_project(pid: int, role: str = Depends(auth)) -> dict:
        project = guard(lambda: co.pipeline.project(pid))
        run_in_background(pid)
        return project

    @app.post("/api/projects/{pid}/budget")
    def set_budget(pid: int, body: Budget, role: str = Depends(auth)) -> dict:
        if role != "ceo":
            raise HTTPException(403, "Budgets are the CEO's call.")
        project = guard(lambda: co.pipeline.set_budget(pid, body.amount, co.s.human(role)))
        run_in_background(pid)
        return project

    @app.get("/api/projects/{pid}/reports")
    def reports(pid: int, role: str = Depends(auth)) -> list:
        return guard(lambda: co.reports.latest(pid))

    @app.post("/api/projects/{pid}/reports")
    def request_report(pid: int, role: str = Depends(auth)) -> list:
        guard(lambda: co.reports.request(pid, co.s.human(role)))
        run_in_background(pid)
        return co.reports.latest(pid)

    @app.get("/api/projects/{pid}/review")
    def product_review(pid: int, role: str = Depends(auth)) -> dict:
        return guard(lambda: review_product(co, pid))

    @app.get("/api/projects/{pid}/file")
    def product_file(pid: int, path: str, role: str = Depends(auth)) -> dict:
        return guard(lambda: review_file(co, pid, path))

    @app.get("/api/projects/{pid}/download")
    def download(pid: int, role: str = Depends(auth)):
        from .delivery import export_product
        folder = Path(tempfile.mkdtemp(prefix="orgforge-export-"))
        project = guard(lambda: co.pipeline.project(pid))
        name = f"{project['name'].lower().replace(' ', '-')}-v{project['version']}.zip"
        zipped = guard(lambda: export_product(co.pipeline, pid, folder / name))
        return FileResponse(zipped, filename=name, media_type="application/zip",
                            background=BackgroundTask(shutil.rmtree, folder, ignore_errors=True))

    @app.get("/api/projects/{pid}/feedback")
    def feedback(pid: int, role: str = Depends(auth)) -> list:
        return guard(lambda: co.feedback.list(pid))

    @app.post("/api/projects/{pid}/feedback")
    def submit_feedback(pid: int, body: CustomerFeedback, role: str = Depends(auth)) -> list:
        guard(lambda: co.feedback.submit(pid, body.body, co.s.human(role), source=body.source))
        return co.feedback.list(pid)

    @app.post("/api/projects/{pid}/revisit")
    def revisit(pid: int, role: str = Depends(auth)) -> dict:
        if role != "ceo":
            raise HTTPException(403, "Parked ideas go back to the CEO; only the CEO can revisit them.")
        return guard(lambda: co.pipeline.revisit(pid, co.s.human(role)))

    @app.post("/api/approvals/{aid}")
    def decide(aid: int, body: Decision, role: str = Depends(auth)) -> dict:
        approval = guard(lambda: co.pipeline.decide(aid, role, body.decision, body.feedback, choice=body.choice))
        if approval["project_id"]:
            run_in_background(approval["project_id"])
        return approval

    @app.get("/api/tickets/{ref}")
    def ticket(ref: str, role: str = Depends(auth)) -> dict:
        return guard(lambda: co.tickets.get(ref))

    @app.post("/api/tickets")
    def new_ticket(body: NewTicket, role: str = Depends(auth)) -> dict:
        t = guard(lambda: co.tickets.create(body.project_id, body.title, body.description, co.s.human(role),
                                            type=body.type, priority=body.priority, status=body.status,
                                            role=body.role or None))
        return t                                # the owner reads it first, then starts the team if it is work

    @app.post("/api/tickets/{ref}")
    def change_ticket(ref: str, body: TicketChange, role: str = Depends(auth)) -> dict:
        t = guard(lambda: co.tickets.update(ref, co.s.human(role), status=body.status, priority=body.priority,
                                            title=body.title, description=body.description, role=body.role or None))
        run_in_background(t["project_id"])
        return t

    @app.post("/api/tickets/{ref}/comments")
    def comment(ref: str, body: Comment, role: str = Depends(auth)) -> dict:
        return guard(lambda: co.tickets.comment(ref, co.s.human(role), body.body))

    @app.get("/api/agents/{agent_id}/messages")
    def messages(agent_id: int, role: str = Depends(auth)) -> dict:
        return guard(lambda: co.chat.thread(agent_id, role))

    @app.post("/api/agents/{agent_id}/messages")
    def send_message(agent_id: int, body: Message, role: str = Depends(auth)) -> dict:
        return guard(lambda: co.chat.send(agent_id, role, body.body, project_id=body.project_id or None))

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
