"""Dashboard server. The CEO and CTO each sign in with their own token."""
from __future__ import annotations

import os
import secrets
import shutil
import tempfile
import threading
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field

from .chat import ChatError
from .company import Company
from .costs import summary as cost_summary
from .feedback import FeedbackError
from .reports import NEXT_STEP
from .telemetry import presence, telemetry
from .review import changes as project_changes, commit_diff, patch_file, read_file as review_file, review as review_product, ticket_diff
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


class NewTask(BaseModel):
    title: str = ""
    description: str = ""
    repo: str = ""
    issue: str = ""
    checks: list[str] = []
    role: str | None = None
    budget: float | None = None


class Steer(BaseModel):
    body: str


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


class WorkerRequest(BaseModel):
    name: str = Field(min_length=1,max_length=100)
    project_id: int
    kind: str
    interval_seconds: int = Field(ge=10)
    config: dict = Field(default_factory=dict)

class ObservationRequest(BaseModel):
    project_id: int
    source: str = Field(min_length=1,max_length=200)
    title: str = Field(min_length=1,max_length=200)
    body: str = Field(min_length=1,max_length=8000)
    severity: str = 'error'
    event_key: str | None = Field(default=None,max_length=200)

class ArenaRequest(BaseModel):
    goal: str = Field(min_length=1,max_length=8000)
    candidates: list[dict] = Field(min_length=2,max_length=8)
    checks: list[str] | None = None

class CustomerRequest(BaseModel):
    journeys: dict

class RedTeamRequest(BaseModel):
    commands: list[str] = Field(min_length=1,max_length=30)

class PackageRequest(BaseModel):
    manifest: dict
    assets: dict[str,str]

class InstallRequest(BaseModel):
    name: str
    sha256: str


def create_app(co: Company, tokens: dict[str, str]) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        if co.workers.service()['enabled']:co.workers.start()
        try:yield
        finally:co.workers.stop()
    app = FastAPI(title="OrgForge", docs_url=None, redoc_url=None,lifespan=lifespan)

    def auth(x_token: str = Header(default="")) -> str:
        for role, token in tokens.items():
            if x_token and secrets.compare_digest(x_token, token):
                return role
        raise HTTPException(401, "Sign in with your CEO or CTO token.")

    def run_in_background(pid: int, **limits) -> None:
        def work():
            try:
                co.pipeline.advance(pid, **limits)
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
        except (OrgError, PipelineError, TicketError, ChatError, FeedbackError, ToolError, ValueError, KeyError, TypeError) as exc:
            raise HTTPException(400, str(exc))

    def managed(role: str, ref: int) -> dict:
        agent = guard(lambda: co.org.agent(ref))
        if not co.can_manage(role, agent):
            raise HTTPException(403, f"{agent['name']}'s department reports to the CEO.")
        return agent

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (STATIC / "index.html").read_text()

    # Installable app: manifest, icons and a pass-through service worker.
    @app.get("/manifest.webmanifest")
    def manifest():
        from fastapi.responses import JSONResponse
        return JSONResponse({
            "name": f"{co.s.company} · OrgForge", "short_name": co.s.company[:12] or "OrgForge",
            "description": "Run your AI-staffed software company.", "start_url": "/#/home", "scope": "/",
            "display": "standalone", "background_color": "#edf0f3", "theme_color": "#2348c9",
            "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"},
                      {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                      {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"}]},
            media_type="application/manifest+json")

    @app.get("/icon.svg")
    def icon_svg():
        return FileResponse(STATIC / "icon.svg", media_type="image/svg+xml")

    @app.get("/icon-{size}.png")
    def icon_png(size: int):
        from fastapi.responses import Response
        if size not in (180, 192, 512):
            raise HTTPException(404, "No such icon.")
        return Response(app_icon_png(size), media_type="image/png")

    @app.get("/sw.js")
    def service_worker():
        return FileResponse(STATIC / "sw.js", media_type="text/javascript")

    @app.get('/healthz')
    def health():
        co.db.one('SELECT 1 AS ready')
        return {'status':'ok'}

    @app.get('/api/operations')
    def operations(role: str = Depends(auth)):
        from .observability import sanitize
        return sanitize({'service':co.workers.service(),'workers':co.workers.list(),'worker_runs':co.db.all('SELECT * FROM worker_runs ORDER BY id DESC LIMIT 100'),
                         'observations':co.observability.list(),'learning':co.learning.summary(),
                         'arena':co.db.all('SELECT id,project_id,status,winner,created_at FROM arena_runs ORDER BY id DESC LIMIT 50'),
                         'assessments':co.db.all('SELECT id,project_id,kind,status,revision,created_at FROM company_assessments ORDER BY id DESC LIMIT 50'),
                         'marketplace':co.marketplace.list()})

    @app.post('/api/workers')
    def add_worker(data: WorkerRequest,role: str = Depends(auth)):
        return guard(lambda:co.workers.add(**data.model_dump()))

    @app.post('/api/workers/service')
    def worker_service(enabled: bool,role: str = Depends(auth)):
        return guard(lambda:co.workers.service(enabled))

    @app.post('/api/workers/tick')
    def worker_tick(role: str = Depends(auth)):
        return guard(lambda:co.workers.tick())

    @app.post('/api/workers/{job_id}/enabled')
    def toggle_worker(job_id: int,enabled: bool,role: str = Depends(auth)):
        guard(lambda:co.workers.enable(job_id,enabled));return {'id':job_id,'enabled':enabled}

    @app.post('/api/observations')
    def observation(data: ObservationRequest,role: str = Depends(auth)):
        return guard(lambda:co.observability.ingest(**data.model_dump()))

    @app.post('/api/telemetry/events')
    def ingest_event(data: ObservationRequest,x_observation_token: str = Header(default='')):
        config=co.s.raw.get('observability') or {}
        expected=os.environ.get(config.get('key_env','ORGFORGE_OBSERVABILITY_TOKEN'),'')
        if len(expected)<32 or not x_observation_token or not secrets.compare_digest(expected,x_observation_token):
            raise HTTPException(403,'Observation credential is missing or invalid.')
        if data.project_id not in (config.get('projects') or []):raise HTTPException(403,'Observation project is outside the producer scope.')
        return guard(lambda:co.observability.ingest(**data.model_dump()))

    @app.post('/api/projects/{pid}/arena')
    def arena(pid: int,data: ArenaRequest,role: str = Depends(auth)):
        return guard(lambda:co.arena.compete(pid,**data.model_dump()))

    @app.get('/api/arena/{run_id}')
    def arena_result(run_id: int,role: str = Depends(auth)):
        return guard(lambda:co.arena.get(run_id))

    @app.post('/api/projects/{pid}/customers')
    def customers(pid: int,data: CustomerRequest,role: str = Depends(auth)):
        return guard(lambda:co.assessments.customers(pid,data.journeys))

    @app.post('/api/projects/{pid}/board')
    def board(pid: int,role: str = Depends(auth)):
        if role!='ceo':raise HTTPException(403,'Only the CEO commissions board reviews.')
        return guard(lambda:co.assessments.board(pid))

    @app.post('/api/projects/{pid}/red-team')
    def red_team(pid: int,data: RedTeamRequest,role: str = Depends(auth)):
        return guard(lambda:co.assessments.red_team(pid,data.commands))

    @app.post('/api/projects/{pid}/security-scan')
    def security_scan(pid: int,role: str = Depends(auth)):
        return guard(lambda:co.security.scan(pid))

    @app.get('/api/assessments/{run_id}')
    def assessment(run_id: int,role: str = Depends(auth)):
        return guard(lambda:co.assessments.get(run_id))

    @app.post('/api/marketplace/register')
    def register_package(data: PackageRequest,role: str = Depends(auth)):
        if role!='ceo':raise HTTPException(403,'Only the CEO manages company packages.')
        return guard(lambda:co.marketplace.register(data.manifest,data.assets))

    @app.post('/api/marketplace/install')
    def install_package(data: InstallRequest,role: str = Depends(auth)):
        if role!='ceo':raise HTTPException(403,'Only the CEO manages company packages.')
        return guard(lambda:co.marketplace.install(data.name,data.sha256))

    @app.post('/api/federation/inbox')
    async def federation_inbox(request: Request):
        body=bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body)>64000:raise HTTPException(413,'Federation envelope exceeds 64KB.')
        try:envelope=json.loads(body)
        except ValueError:raise HTTPException(400,'Malformed federation envelope.')
        return guard(lambda:co.federation.receive(envelope))

    @app.get("/api/state")
    def state(role: str = Depends(auth)) -> dict:
        return {
            "company": co.s.company,
            "you": {"role": role, "name": co.s.human(role)},
            "humans": {"ceo": co.s.ceo_name, "cto": co.s.cto_name},
            "stages": [{"id": s, "label": STAGE_LABELS[s]} for s in STAGES],
            "idea_stages": IDEA_STAGES,
            "next_steps": NEXT_STEP,
            "departments": co.org.chart(include_fired=True),
            "projects": co.pipeline.overview(),
            "approvals": co.pipeline.inbox(),
            "tickets": co.tickets.search(),
            "ticket_meta": {"types": TYPES, "priorities": PRIORITIES, "statuses": STATUS_LABELS,
                            "roles": co.tickets.work_roles()},
            "events": co.events(60)[::-1],
            "costs": cost_summary(co.db),
            "presence": presence(co.db),
            "memories": co.memory.list(limit=60),
            "routing": co.router.summary(),
            "learning": {"reviewers": co.learning.reviewers(), "strategies": co.learning.proposals(),
                         "enabled": co.learning.enabled},
            "failover": {"enabled": co.failover.enabled, "models": [str(m) for m in co.failover.config.get("models") or []],
                         "resting": [{**r, "until_local": co.failover.when(r["until"])} for r in co.failover.status()]},
            "skills": [{k: s[k] for k in ("name", "title", "kinds", "departments", "enabled", "builtin", "source")} for s in co.skills.all()],
            "telemetry": telemetry(co.db),
            "chats": co.db.all("SELECT agent_id, MAX(id) AS last_id, SUM(status='pending') AS pending FROM messages "
                               "WHERE human=? GROUP BY agent_id", role),
        }

    @app.get('/api/projects/{pid}/swarm')
    def swarm(pid: int, role: str = Depends(auth)):
        guard(lambda: co.pipeline.project(pid))
        return co.swarms.snapshot(pid)

    @app.get('/api/routing')
    def routing(role: str = Depends(auth)):
        return co.router.summary()

    @app.get('/api/memories')
    def memories(query: str, project_id: int | None = None, category: str | None = None,
                 include_history: bool = False, role: str = Depends(auth)):
        return guard(lambda: co.memory.search(project_id, query, category=category, include_history=include_history))

    @app.post('/api/projects/{pid}/work')
    def work(pid: int, cycles: int = 5, seconds: float = 300, role: str = Depends(auth)):
        guard(lambda: co.pipeline.project(pid))
        if not 1 <= cycles <= 100 or not 0 < seconds <= 3600:
            raise HTTPException(400, 'Use 1–100 cycles and a time budget of at most 3600 seconds.')
        run_in_background(pid, max_cycles=cycles, max_seconds=seconds)
        return {'project_id': pid, 'cycles': cycles, 'seconds': seconds}

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

    @app.post("/api/tasks")
    def new_task(body: NewTask, role: str = Depends(auth)) -> dict:
        project = guard(lambda: co.pipeline.create_task(body.title.strip(), body.description.strip(), by=co.s.human(role),
                                                        repo=body.repo.strip() or None, issue=body.issue.strip() or None,
                                                        checks=body.checks, role=body.role or None, budget=body.budget))
        run_in_background(project["id"])
        return project

    @app.get("/api/projects/{pid}/changes")
    def changes(pid: int, role: str = Depends(auth)) -> dict:
        return guard(lambda: project_changes(co, pid))

    @app.get("/api/projects/{pid}/commits/{sha}")
    def commit(pid: int, sha: str, role: str = Depends(auth)) -> dict:
        return guard(lambda: commit_diff(co, pid, sha))

    @app.get("/api/projects/{pid}/patch")
    def patch(pid: int, role: str = Depends(auth)):
        from fastapi.responses import Response
        name, text = guard(lambda: patch_file(co, pid))
        return Response(text, media_type="text/x-patch", headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/api/tickets/{ref}/diff")
    def diff(ref: str, role: str = Depends(auth)) -> dict:
        return guard(lambda: ticket_diff(co, co.tickets.get(ref)["id"]))

    @app.get("/api/agents/{agent_id}/runs")
    def agent_runs(agent_id: int, role: str = Depends(auth)) -> dict:
        agent = guard(lambda: co.org.agent(agent_id))
        return {"agent": {"id": agent["id"], "name": agent["name"], "role": agent["role"], "model": agent["model"]},
                "runs": co.runs.recent(agent_id),
                "queued": co.db.all("SELECT author, body, created_at FROM steers WHERE agent_id=? AND delivered_at IS NULL",
                                    agent_id)}

    @app.get("/api/runs/{run_id}")
    def run_log(run_id: int, after: int = 0, role: str = Depends(auth)) -> dict:
        return guard(lambda: co.runs.transcript(run_id, after))

    @app.post("/api/agents/{agent_id}/steer")
    def steer(agent_id: int, body: Steer, role: str = Depends(auth)) -> dict:
        guard(lambda: co.org.agent(agent_id))
        return guard(lambda: co.runs.steer(agent_id, co.s.human(role), body.body))

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


def app_icon_png(size: int) -> bytes:
    """The app icon as a PNG, drawn without image libraries: a blue tile with four rounded squares."""
    import struct
    import zlib
    blue, white = (35, 72, 201), (255, 255, 255)

    def inside_round(x, y, x0, y0, w, r):
        cx, cy = min(max(x, x0 + r), x0 + w - r), min(max(y, y0 + r), y0 + w - r)
        return x0 <= x < x0 + w and y0 <= y < y0 + w and (x - cx) ** 2 + (y - cy) ** 2 <= r * r

    k = size / 512
    boxes = [(120, 120, False), (280, 120, False), (120, 280, False), (280, 280, True)]
    rows = bytearray()
    for y in range(size):
        rows.append(0)
        for x in range(size):
            px = (0, 0, 0, 0)
            if inside_round(x, y, 0, 0, size, 112 * k):
                px = (*blue, 255)
                for bx, by, solid in boxes:
                    outer = inside_round(x, y, bx * k - 20 * k, by * k - 20 * k, 152 * k, 30 * k)
                    inner = inside_round(x, y, bx * k + 20 * k, by * k + 20 * k, 72 * k, 8 * k)
                    if outer and (solid or not inner):
                        px = (*white, 255)
            rows += bytes(px)

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b""))


def serve(co: Company, host: str, port: int, open_app: bool = False) -> None:
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
    if open_app:
        open_window(f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}/#/home")
    uvicorn.run(create_app(co, tokens), host=host, port=port, log_level="warning")


def open_window(url: str) -> None:
    """Open the dashboard in its own app window once the server is up (Chrome or Edge app mode, else the browser)."""
    import shutil
    import subprocess
    import sys
    import time
    import webbrowser

    def go() -> None:
        time.sleep(1.5)
        if sys.platform == "darwin":
            for app in ("Google Chrome", "Microsoft Edge", "Brave Browser"):
                if Path(f"/Applications/{app}.app").exists():
                    subprocess.Popen(["open", "-na", app, "--args", f"--app={url}"])
                    return
        for exe in ("google-chrome", "chromium", "microsoft-edge"):
            if shutil.which(exe):
                subprocess.Popen([exe, f"--app={url}"])
                return
        webbrowser.open(url)
    threading.Thread(target=go, daemon=True).start()
