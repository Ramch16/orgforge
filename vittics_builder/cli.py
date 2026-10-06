"""vittics-builder command line."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

from .chat import ChatError
from .feedback import FeedbackError
from .company import Company
from .org import KINDS, OrgError
from .pipeline import PipelineError
from .tickets import PRIORITIES, STATUSES, TYPES, TicketError
from .tools import TOOL_SPECS


def _role(args) -> str:
    return getattr(args, "as_role", None) or os.environ.get("VITTICS_ROLE", "ceo")


def _print_inbox(co: Company, role: str | None) -> None:
    items = co.pipeline.inbox(role)
    if not items:
        print("Nothing is waiting for a decision.")
        return
    for a in items:
        print(f"\n#{a['id']}  [{a['required_role'].upper()}]  {a['title']}")
        for line in a["summary"].splitlines()[:25]:
            print(f"    {line}")
    print("\nDecide with: vittics-builder approve <id> --as <ceo|cto>   or   vittics-builder reject <id> --as <ceo|cto> --note \"...\"")
    if any(a["kind"] == "idea_decision" for a in items):
        print("Decide ideas with: vittics-builder idea decide <id> internal|commercial|park|drop --note \"...\"")


def _print_status(co: Company) -> None:
    projects = co.pipeline.overview()
    if not projects:
        print('No projects yet. Start one with: vittics-builder new "Name" --brief "What to build"')
    for p in projects:
        print(f"\nProject {p['id']}: {p['name']}  —  {p['stage_label']}")
        print(f"  workspace: {p['workspace']}")
        for t in p["tasks"]:
            who = f" ({t['assignee']})" if t["assignee"] else ""
            redo = f", sent back {t['attempts']}x" if t["attempts"] else ""
            print(f"  [{t['status']:<11}] {t['key']}: {t['title']}{who}{redo}")
    pending = co.pipeline.inbox()
    if pending:
        print(f"\n{len(pending)} decision(s) waiting: " + ", ".join(f"#{a['id']} ({a['required_role'].upper()})" for a in pending))


def _print_org(co: Company, show_fired: bool) -> None:
    print(f"{co.s.company}   CEO: {co.s.ceo_name}   CTO: {co.s.cto_name}")
    for d in co.org.chart(include_fired=show_fired):
        print(f"\n{d['name']} ({d['id']}) — reports to the {d['reports_to'].upper()}")
        if not d["agents"]:
            print("  (no agents)   roles: " + (", ".join(r["id"] for r in d["roles"]) or "none"))
        for a in d["agents"]:
            score = "  new" if a["score"] is None else f"{a['score']:5.1f}"
            gen = f" gen {a['generation']}" if a["generation"] > 1 else ""
            print(f"  #{a['id']:<3} {a['name']:<10} {a['role']:<18} {a['status']:<9} score {score} "
                  f"({a['evals']} evals)  seat {a['seat']}{gen}  {a['model']}")


def _advance(co: Company, pid: int) -> None:
    print("Working... (this runs until a human decision is needed)")
    project = co.pipeline.advance(pid)
    print(f"Project {pid} is now: {project['stage']}")
    _print_inbox(co, None)


def legacy_main() -> int:
    """`orgforge`, the old name of the command (kept for one release)."""
    print("Note: OrgForge is now Vittics Builder. Use `vittics-builder`; `orgforge` will be removed.", file=sys.stderr)
    return main()


def main(argv: list[str] | None = None) -> int:
    from .legacy import adopt_env
    adopt_env()
    ap = argparse.ArgumentParser(prog="vittics-builder", description="Run an AI-staffed software company as its CEO and CTO.")
    ap.add_argument("--home", help="Company folder (default: current folder or $VITTICS_HOME)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def who(p):
        p.add_argument("--as", dest="as_role", choices=["ceo", "cto"], help="Act as CEO or CTO (default: $VITTICS_ROLE or ceo)")

    sub.add_parser("init", help="Create the company in this folder")
    p = sub.add_parser("new", help="Start a project from a brief")
    p.add_argument("name"); p.add_argument("--brief"); p.add_argument("--brief-file"); p.add_argument("--no-run", action="store_true")
    p = sub.add_parser("run", help="Continue a project until it needs a decision"); p.add_argument("project", type=int)
    p = sub.add_parser('work', help='Run bounded autonomous cycles; preserve human approval gates')
    p.add_argument('project', type=int)
    p.add_argument('--cycles', type=int, default=5)
    p.add_argument('--seconds', type=float, default=300)
    p = sub.add_parser('swarm', help='Show the adaptive project team and workload bottlenecks')
    p.add_argument('project', type=int)
    p = sub.add_parser('memory', help='Search persistent company and project knowledge')
    p.add_argument('query'); p.add_argument('--project', type=int); p.add_argument('--category')
    p.add_argument('--history', action='store_true', help='Include other projects')
    sub.add_parser('routing', help='Model performance evidence and costs')
    p = sub.add_parser("export", help="Package a signed-off product as a zip")
    p.add_argument("project", type=int); p.add_argument("--output", required=True)
    p = sub.add_parser('learn',help='Review learned strategy evidence')
    p.add_argument('--kind');p.add_argument('--signature')
    p = sub.add_parser('workers',help='Schedule bounded company employees')
    p.add_argument('action',choices=['list','add','tick','enable','disable'])
    p.add_argument('--name');p.add_argument('--project',type=int);p.add_argument('--kind')
    p.add_argument('--interval',type=int,default=3600);p.add_argument('--config');p.add_argument('--id',type=int)
    p = sub.add_parser('observe',help='Record an incident for deduplicated triage')
    p.add_argument('project',type=int);p.add_argument('--source',required=True);p.add_argument('--title',required=True)
    p.add_argument('--body',required=True);p.add_argument('--severity',default='error')
    p = sub.add_parser('arena',help='Benchmark strategies in isolated snapshots')
    p.add_argument('project',type=int);p.add_argument('--goal',required=True);p.add_argument('--candidates',required=True)
    p.add_argument('--check',action='append')
    p = sub.add_parser('assess',help='Customer, board, or red-team evaluations')
    p.add_argument('kind',choices=['customers','board','red-team','security','dependencies']);p.add_argument('project',type=int)
    p.add_argument('--journeys');p.add_argument('--check',action='append')
    p = sub.add_parser('marketplace',help='Review and install SHA256-pinned agent packages')
    p.add_argument('action',choices=['list','register','install']);p.add_argument('--bundle');p.add_argument('--name');p.add_argument('--sha256')
    p = sub.add_parser('federation',help='Exchange signed scoped proposals, observations, or results')
    p.add_argument('action',choices=['sign','send','receive']);p.add_argument('--peer');p.add_argument('--kind');p.add_argument('--payload',required=True)
    sub.add_parser("status", help="Projects and tasks")
    p = sub.add_parser("inbox", help="Decisions waiting for you"); who(p)
    for name in ("approve", "reject"):
        p = sub.add_parser(name, help=f"{name.title()} a pending decision")
        p.add_argument("id", type=int); p.add_argument("--note", default=""); p.add_argument("--no-run", action="store_true"); who(p)
    p = sub.add_parser("rate", help="Score an agent yourself (0-100)")
    p.add_argument("agent"); p.add_argument("score", type=float); p.add_argument("--note", default=""); who(p)
    p = sub.add_parser("log", help="Recent activity"); p.add_argument("-n", type=int, default=40)
    p = sub.add_parser("serve", help="Start the dashboard")
    p.add_argument("--host", default="127.0.0.1"); p.add_argument("--port", type=int, default=4700)
    p = sub.add_parser("app", help="Start the dashboard and open it in its own app window")
    p.add_argument("--host", default="127.0.0.1"); p.add_argument("--port", type=int, default=4700)

    p = sub.add_parser("task", help="A quick task: one change, optionally on an existing repo or GitHub issue")
    p.add_argument("title", nargs="?", default=""); p.add_argument("--brief", default="")
    p.add_argument("--repo", help="Folder, GitHub owner/name, or git URL to work on")
    p.add_argument("--issue", help="GitHub issue: owner/repo#123 or its URL (needs gh)")
    p.add_argument("--check", action="append", default=[], help="Command that must pass, e.g. 'pytest -q' (repeatable)")
    p.add_argument("--role"); p.add_argument("--budget", type=float); p.add_argument("--no-run", action="store_true"); who(p)
    p = sub.add_parser("steer", help="Send a message to an agent while it works")
    p.add_argument("agent"); p.add_argument("message"); who(p)
    p = sub.add_parser("watch", help="Show an agent's latest run: what it was told, did and said")
    p.add_argument("agent")
    p = sub.add_parser("diff", help="Code changes for a ticket (T-12) or a task project (P3)"); p.add_argument("ref")
    p = sub.add_parser("skills", help="Guidelines agents work by (no argument: list them)")
    p.add_argument("action", nargs="?", choices=["list", "add"], default="list")
    p.add_argument("source", nargs="?", help="For add: a Markdown file or URL (e.g. a CLAUDE.md)")
    p = sub.add_parser("engines", help="Coding CLIs agents can work through, and whether they are ready")
    p.add_argument("--test", metavar="ENGINE", help="Run a tiny prompt through this engine")
    p.add_argument("--wake", metavar="MODEL", nargs="?", const="all",
                   help="Try a resting engine again now (no value: all of them)")
    sub.add_parser("learning", help="Reviewer accuracy, and approaches the team learned from failed reviews")
    p = sub.add_parser("doctor", help="Is this computer ready? What the company and its projects need, and installing it")
    p.add_argument("--project", type=int, help="Only what this project needs")
    p.add_argument("--install", nargs="+", metavar="TOOL", help="Install these (e.g. node gemini), asking first")
    p.add_argument("--missing", action="store_true", help="Install everything needed that is missing, asking first")
    p.add_argument("--yes", action="store_true", help="Do not ask before each install")
    p = sub.add_parser("ai", help="How the agents think: status, use claude-code|codex|api|demo, key, test")
    p.add_argument("action", nargs="?", choices=["status", "use", "key", "test"], default="status")
    p.add_argument("choice", nargs="?", choices=["claude-code", "codex", "api", "demo"]); who(p)
    p = sub.add_parser("build", help="Idea to production: describe what to build; it is assessed, planned, built and released")
    p.add_argument("brief", help='e.g. "A customer support app with login, a database and Stripe payments"')
    p.add_argument("--name", default="", help="Project name (default: from the brief)")
    p.add_argument("--autopilot", choices=["off", "key", "final"], default="key",
                   help="off: you approve every step; key (default): you decide the idea, the plan and the final "
                        "sign-off; final: only the final sign-off")
    p.add_argument("--budget", type=float); who(p)
    p = sub.add_parser("autopilot", help="How hands-on to be on a project: off, key or final")
    p.add_argument("project", type=int); p.add_argument("level", choices=["off", "key", "final"]); who(p)
    p = sub.add_parser("keys", help="A project's private keys (API keys, database URLs): list, set or remove")
    p.add_argument("project", type=int); p.add_argument("action", nargs="?", choices=["list", "set", "remove"], default="list")
    p.add_argument("name", nargs="?", help="e.g. STRIPE_SECRET_KEY (the value is asked for, hidden)"); who(p)
    p = sub.add_parser("preview", help="The signed-off product running on this computer: status, start or stop")
    p.add_argument("project", type=int); p.add_argument("action", nargs="?", choices=["status", "start", "stop"], default="status"); who(p)
    p = sub.add_parser("desktop", help="Start the backend the Vittics Builder desktop app uses (company in ~/VitticsBuilder)")
    p.add_argument("--port", type=int, help="Default: a free port on 127.0.0.1")
    p = sub.add_parser("machines", help="Worker machines: other computers that run your agents' coding CLIs")
    p.add_argument("action", nargs="?", choices=["list", "pair", "assign", "unassign", "revoke"], default="list")
    p.add_argument("name", nargs="?", help="pair/revoke: machine name; assign/unassign: agent name")
    p.add_argument("machine", nargs="?", help="assign: the machine the agent works on"); who(p)
    p = sub.add_parser("worker", help="On another computer: join a company as a worker machine, then work for it")
    p.add_argument("action", choices=["join", "run", "status"])
    p.add_argument("url", nargs="?", help="join: the company's dashboard address, e.g. http://my-mac.local:4700")
    p.add_argument("code", nargs="?", help="join: the pairing code from Machines in the dashboard")
    p.add_argument("--insecure", action="store_true", help="join: allow plain HTTP to a public address")
    p = sub.add_parser("deployments", help="What each project has deployed, where, and whether it is live")
    p.add_argument("project", type=int, nargs="?")
    sub.add_parser("costs", help="What the company's AI work has cost (estimate)")
    p = sub.add_parser("budget", help="CEO: set a project's budget in USD (0 = no limit)")
    p.add_argument("project", type=int); p.add_argument("amount", type=float)
    p = sub.add_parser("report", help="Get a status report from the product manager")
    p.add_argument("project", type=int); p.add_argument("--latest", action="store_true", help="Show the last report only"); who(p)
    p = sub.add_parser("feedback", help="Pass on customer feedback for Support to triage (no text: list it)")
    p.add_argument("project", type=int); p.add_argument("text", nargs="?"); p.add_argument("--source", default=""); who(p)
    ia = sub.add_parser("idea", help="Ideas: assess, decide, revisit").add_subparsers(dest="idea_cmd")
    p = ia.add_parser("new", help="Submit an idea; Ram assesses it with Engineering, Marketing and Legal")
    p.add_argument("name"); p.add_argument("--brief"); p.add_argument("--brief-file"); p.add_argument("--no-run", action="store_true"); who(p)
    p = ia.add_parser("decide", help="CEO: decide an assessed idea")
    p.add_argument("id", type=int, help="Approval id of the decision"); p.add_argument("choice", choices=["internal", "commercial", "park", "drop"])
    p.add_argument("--note", default=""); p.add_argument("--no-run", action="store_true")
    p = ia.add_parser("revisit", help="CEO: bring a parked idea back for a decision"); p.add_argument("project", type=int)
    p = sub.add_parser("chat", help="Talk to an agent directly (no message: show your conversation)")
    p.add_argument("agent"); p.add_argument("message", nargs="?")
    p.add_argument("--project", type=int, help="Project the conversation is about (lets the agent read it)"); who(p)
    tk = sub.add_parser("ticket", help="Internal ticket tracker").add_subparsers(dest="ticket_cmd")
    p = tk.add_parser("list", help="Tickets, highest priority first")
    p.add_argument("--project", type=int); p.add_argument("--status", choices=STATUSES)
    p = tk.add_parser("show", help="A ticket with its full history"); p.add_argument("ticket")
    p = tk.add_parser("new", help="File a ticket"); p.add_argument("project", type=int); p.add_argument("title")
    p.add_argument("--description", default=""); p.add_argument("--type", choices=TYPES, default="task")
    p.add_argument("--priority", choices=PRIORITIES, default="medium"); p.add_argument("--role")
    p.add_argument("--todo", action="store_true", help="Ready for the team now (default: backlog)")
    p.add_argument("--no-run", action="store_true"); who(p)
    p = tk.add_parser("update", help="Change status, priority, title or description"); p.add_argument("ticket")
    p.add_argument("--status", choices=("backlog", "todo", "cancelled")); p.add_argument("--priority", choices=PRIORITIES)
    p.add_argument("--title"); p.add_argument("--description")
    p.add_argument("--role", help="Transfer to this role (and its department)")
    p.add_argument("--no-run", action="store_true"); who(p)
    p = tk.add_parser("comment", help="Comment on a ticket; the agent who works it next will read it")
    p.add_argument("ticket"); p.add_argument("text"); who(p)

    org = sub.add_parser("org", help="Org chart, hiring and firing").add_subparsers(dest="org_cmd")
    p = org.add_parser("show"); p.add_argument("--all", action="store_true", help="Include former agents")
    p = org.add_parser("hire"); p.add_argument("--role", required=True)
    p.add_argument("--name", help="Leave out to let a teammate in the department pick one"); p.add_argument("--model"); who(p)
    p = org.add_parser("fire"); p.add_argument("agent"); p.add_argument("--reason", default="Decision by management")
    p.add_argument("--no-replace", action="store_true", help="Leave the seat empty"); who(p)
    p = org.add_parser("set-model", help="Move an agent, or everyone with --all, to a model or engine")
    p.add_argument("agent", nargs="?"); p.add_argument("model"); p.add_argument("--all", action="store_true"); who(p)
    p = org.add_parser("rehire"); p.add_argument("agent"); p.add_argument("--note", default=""); who(p)
    p = org.add_parser("add-dept"); p.add_argument("id"); p.add_argument("--name"); p.add_argument("--reports-to", choices=["ceo", "cto"], default="cto")
    p = org.add_parser("remove-dept"); p.add_argument("id")
    p = org.add_parser("sync", help="Add departments, roles and seats this company is missing")
    p.add_argument("--from", dest="source", help="Org file to add from (default: the built-in full company)"); who(p)
    p = org.add_parser("add-role"); p.add_argument("id"); p.add_argument("--dept", required=True)
    p.add_argument("--kind", choices=KINDS, required=True); p.add_argument("--title")
    p.add_argument("--tools", default="read_file,write_file,replace_in_file,list_files,run_command", help=f"Comma-separated: {', '.join(TOOL_SPECS)}")
    p.add_argument("--prompt", required=True, help="What this role does and how")

    args = ap.parse_args(argv)
    try:
        return _dispatch(args)
    except (OrgError, PipelineError, TicketError, ChatError, FeedbackError, PermissionError, RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def _dispatch(args) -> int:
    if args.cmd == "desktop":                  # creates the company itself on first launch
        from .desktop import main as desktop_main
        desktop_main(args.home, args.port)
        return 0
    if args.cmd == "worker":                   # runs on the worker computer, which has no company of its own
        from . import worker_client as wc
        if args.action == "join":
            if not args.url or not args.code:
                raise ValueError("Usage: vittics-builder worker join <url> <pairing code>")
            config = wc.join(args.url, args.code, insecure=args.insecure)
            print(f"Joined as worker machine '{config['name']}'. Start working with: vittics-builder worker run")
        elif args.action == "status":
            config = wc.load()
            print(f"Worker machine '{config['name']}' for {config['url']} (config: {wc.CONFIG})")
            for engine, state in wc.info()["engines"].items():
                print(f"  {engine:<12} {state}")
        else:
            try:
                wc.Worker().run_forever()
            except KeyboardInterrupt:
                print("\nStopped.")
        return 0
    if args.cmd == "init":
        co = Company(args.home, create=True)
        print(f"{co.s.company} is set up in {co.s.root}")
        print("Edit org.yaml to set the company name, your names and the model, then:")
        print('  vittics-builder org show\n  vittics-builder new "Product name" --brief "What to build"')
        return 0

    co = Company(args.home)
    role = _role(args)

    if args.cmd == 'learn':
        print(json.dumps(co.learning.summary(args.kind,args.signature),indent=2))
    elif args.cmd=='workers':
        if args.action=='list':result=co.workers.list()
        elif args.action=='tick':result=co.workers.tick()
        elif args.action in ('enable','disable'):
            co.workers.enable(args.id,args.action=='enable');result={'id':args.id,'enabled':args.action=='enable'}
        else:
            if not args.name or args.project is None or not args.kind:raise ValueError('Worker add needs name, project and kind.')
            result=co.workers.add(args.name,args.project,args.kind,args.interval,json.loads(Path(args.config).read_text(encoding="utf-8")) if args.config else {})
        print(json.dumps(result,indent=2))
    elif args.cmd=='observe':
        print(json.dumps(co.observability.ingest(args.project,args.source,args.title,args.body,args.severity),indent=2))
    elif args.cmd=='arena':
        print(json.dumps(co.arena.compete(args.project,args.goal,json.loads(Path(args.candidates).read_text(encoding="utf-8")),args.check),indent=2))
    elif args.cmd=='assess':
        if args.kind=='board':result=co.assessments.board(args.project)
        elif args.kind=='customers':
            if not args.journeys:raise ValueError('Customer evaluation needs a journey JSON file.')
            result=co.assessments.customers(args.project,json.loads(Path(args.journeys).read_text(encoding="utf-8")))
        elif args.kind=='red-team':result=co.assessments.red_team(args.project,args.check)
        elif args.kind=='dependencies':result=co.security.dependencies(args.project)
        else:result=co.security.scan(args.project)
        print(json.dumps(result,indent=2))
    elif args.cmd=='marketplace':
        if args.action=='list':result=co.marketplace.list()
        elif args.action=='register':
            if not args.bundle:raise ValueError('Registration needs a bundle JSON file.')
            bundle=json.loads(Path(args.bundle).read_text(encoding="utf-8"));result=co.marketplace.register(bundle['manifest'],bundle['assets'])
        else:result=co.marketplace.install(args.name,args.sha256)
        print(json.dumps(result,indent=2))
    elif args.cmd=='federation':
        payload=json.loads(Path(args.payload).read_text(encoding="utf-8"))
        if args.action=='receive':result=co.federation.receive(payload)
        elif args.action=='sign':result=co.federation.envelope(args.peer,args.kind,payload)
        else:result=co.federation.send(args.peer,args.kind,payload)
        print(json.dumps(result,indent=2))
    elif args.cmd == "new":
        brief = Path(args.brief_file).read_text(encoding="utf-8") if args.brief_file else args.brief
        if not brief:
            raise PipelineError("Give the brief with --brief or --brief-file.")
        project = co.pipeline.create_project(args.name, brief, by=co.s.ceo_name)
        print(f"Project {project['id']} created: {project['workspace']}")
        if not args.no_run:
            _advance(co, project["id"])
    elif args.cmd == 'work':
        project = co.pipeline.advance(args.project, max_cycles=args.cycles, max_seconds=args.seconds)
        print(f"Project {args.project}: {project['stage']} (bounded work cycle complete)")
        _print_inbox(co, None)
    elif args.cmd == 'swarm':
        co.pipeline.project(args.project)
        print(json.dumps(co.swarms.snapshot(args.project), indent=2))
    elif args.cmd == 'memory':
        print(json.dumps(co.memory.search(args.project, args.query, category=args.category,
                                        include_history=args.history), indent=2))
    elif args.cmd == 'routing':
        print(json.dumps(co.router.summary(), indent=2))
    elif args.cmd == "run":
        _advance(co, args.project)
    elif args.cmd == "export":
        from .delivery import export_product
        print(export_product(co.pipeline, args.project, Path(args.output)))
    elif args.cmd == "status":
        _print_status(co)
    elif args.cmd == "inbox":
        _print_inbox(co, getattr(args, "as_role", None))
    elif args.cmd in ("approve", "reject"):
        decision = "approved" if args.cmd == "approve" else "rejected"
        approval = co.pipeline.decide(args.id, role, decision, args.note)
        print(f"#{approval['id']} {decision} by the {role.upper()}.")
        if approval["project_id"] and not args.no_run:
            _advance(co, approval["project_id"])
    elif args.cmd == "rate":
        agent = co.org.agent(args.agent)
        if not co.can_manage(role, agent):
            raise PermissionError(f"{agent['name']}'s department reports to the CEO.")
        score = co.perf.record(agent["id"], args.score, source="human", reviewer=co.s.human(role), notes=args.note)
        outcome = co.perf.evaluate(agent["id"])
        print(f"{agent['name']}: rolling score {score:.1f}" + (f" -> {outcome}" if outcome else ""))
    elif args.cmd == "log":
        for e in co.events(args.n):
            print(f"{e['ts'][11:19]}  {e['actor']:<10} {e['kind']:<9} {e['message']}")
    elif args.cmd in ("serve", "app"):
        from .server import serve
        serve(co, args.host, args.port, open_app=args.cmd == "app")
    elif args.cmd == "org":
        _org(co, args, role)
    elif args.cmd == "ticket":
        _ticket(co, args, role)
    elif args.cmd == "idea":
        _idea(co, args, role)
    elif args.cmd == "task":
        project = co.pipeline.create_task(args.title, args.brief, by=co.s.human(role), repo=args.repo, issue=args.issue,
                                          checks=args.check, role=args.role, budget=args.budget)
        print(f"Task {project['id']} created: {project['name']}" + (f" on {project['source']}, branch {project['branch']}"
                                                                     if project["source"] else ""))
        if not args.no_run:
            _advance(co, project["id"])
    elif args.cmd == "steer":
        agent = co.org.agent(args.agent)
        sent = co.runs.steer(agent["id"], co.s.human(role), args.message)
        print(f"{agent['name']} gets it " + ("at their next step." if sent["live"] else "when they next start work."))
    elif args.cmd == "watch":
        agent = co.org.agent(args.agent)
        runs = co.runs.recent(agent["id"], 1)
        if not runs:
            print(f"{agent['name']} has not worked yet.")
        else:
            log = co.runs.transcript(runs[0]["id"])
            r = log["run"]
            print(f"{agent['name']} · {r['purpose']} · {r['status']} · started {r['started_at'][11:19]}")
            for e in log["events"]:
                print(f"\n[{e['kind']}] " + e["body"])
    elif args.cmd == "diff":
        from .review import changes, ticket_diff
        ref = args.ref.upper()
        print(changes(co, int(ref[1:]))["diff"] if ref.startswith("P") and ref[1:].isdigit()
              else ticket_diff(co, co.tickets.get(ref)["id"])["diff"])
    elif args.cmd == "skills":
        if args.action == "add":
            if not args.source:
                raise OrgError("Give a file or URL to add.")
            from .skills import SkillError
            try:
                s = co.skills.add(args.source)
            except (SkillError, OSError) as exc:
                raise OrgError(str(exc)) from exc
            print(f"Added skill '{s['name']}' ({s['file']}). It applies to " + (", ".join(s["kinds"]) or "every role") + ".")
        for s in co.skills.all():
            print(f"  {'on ' if s['enabled'] else 'off'}  {s['name']:<24} {s['title']}  · "
                  + (", ".join(s["kinds"]) or "every role") + ("  · built in" if s["builtin"] else "  · company")
                  + (f"\n       source: {s['source']}" if s["source"] else ""))
        print("Built-in skills are switched on under skills: in org.yaml; company skills live in the skills/ folder.")
    elif args.cmd == "engines":
        import shutil
        from .engines import EngineError, run_cli
        for name, e in sorted(co.s.engines.items()):
            exe = e["command"][0]
            print(f"  {name:<12} {'installed    ' if shutil.which(exe) else 'not installed'}  ({exe})"
                  + ("  · tested" if e.get("verified") else "  · from its docs, untested")
                  + ("  · uses its own login" if e.get("subscription") else ""))
        import urllib.request
        for name, e in sorted(co.s.endpoints.items()):
            if e.get("free"):
                try:
                    with urllib.request.urlopen(e["base_url"].rstrip("/") + "/models", timeout=2) as r:
                        models = [m.get("id") for m in json.loads(r.read()).get("data", [])]
                    state = f"running · models: {', '.join(models[:6]) or 'none pulled yet'}"
                except Exception:
                    state = "not running"
            else:
                state = "key set" if os.environ.get(e.get("key_env", "")) else f"needs {e.get('key_env')}"
            print(f"  {name:<12} {state}  ({e['base_url']})  · use {name}:<model>")
        users = co.db.all("SELECT model, COUNT(*) AS n FROM agents WHERE status!='fired' GROUP BY model")
        print("Agents by model: " + ", ".join(f"{u['model']} ({u['n']})" for u in users))
        if args.wake:
            n = co.failover.clear(None if args.wake == "all" else args.wake)
            print(f"Woke {n} resting engine{'s' if n != 1 else ''}.")
        fo = co.failover
        print("Failover: " + (("on · " + (" → ".join(fo.models()) or "no backup models set")) if fo.enabled
                              else "off (failover: in org.yaml)"))
        for r in fo.status():
            print(f"  resting  {r['engine']:<24} until {fo.when(r['until'])}  · {r['reason'][:100]}")
        if args.test and ":" in args.test:          # a model on an endpoint, e.g. ollama:qwen2.5-coder
            try:
                r = co.runtime.provider.complete(model=args.test, system="You are testing a connection.",
                                                 messages=[{"role": "user", "content": "Reply with just: OK"}],
                                                 tools=[], max_tokens=20)
                print(f"\n{args.test}: works: {r.text[:120]}")
            except RuntimeError as exc:
                print(f"\n{args.test}: {exc}")
        elif args.test:
            if args.test not in co.s.engines:
                raise OrgError(f"No engine '{args.test}'.")
            try:
                out = run_cli(co.s.engines[args.test], system="You are testing a connection.", prompt="Reply with just: OK",
                              cwd=None, model="", write=False, run=False, timeout=120)
                ok = not out["is_error"] and "OK" in out["text"]
                print(f"\n{args.test}: {'works' if ok else 'answered, but not as expected'}: {out['text'][:200]}")
                if "login" in out["text"].lower():
                    print("Sign in first: run `claude` in a terminal and type /login.")
            except EngineError as exc:
                print(f"\n{args.test}: {exc}")
    elif args.cmd == "doctor":
        from .machine import CATALOG
        r = co.machine.report(args.project)
        s = r["system"]
        print(f"This computer: {s['os']} {s['release']} ({s['machine']}), {s['cpus']} CPUs, {s['free_disk_gb']} GB free. "
              "Package managers: " + (", ".join(m for m, ok in s["managers"].items() if ok) or "none found"))
        marks = {"ready": "ok  ", "missing": "MISS", "outdated": "OLD ", "not_ready": "WAIT", "signed_out": "SIGN"}
        for t in r["tools"]:
            print(f"  {marks[t['state']]} {t['name']:<20} {t['version'] or '-':<10} needed by: {'; '.join(t['needed_by'])[:70]}")
            if t["detail"]:
                print(f"       {t['detail']}")
            if not t["installed"]:
                print(f"       install: {t['install']['how']}" + ("" if t["install"]["auto"] else "   (run it yourself)"))
        print("\nReady." if r["ready"] else "\nNot ready yet.")
        wanted = list(args.install or [])
        if args.missing:
            wanted += [t["id"] for t in r["tools"] if not t["installed"] and t["install"]["auto"]]
        for tool in dict.fromkeys(wanted):
            if tool not in CATALOG:
                raise OrgError(f"Unknown tool '{tool}'. Known: {', '.join(sorted(CATALOG))}.")
            recipe = co.machine.recipe(tool)
            if not recipe["auto"]:
                print(f"\n{CATALOG[tool]['name']}: run this yourself: {recipe['how']}"
                      + (f"\n  {recipe['why']}" if recipe.get("why") else ""))
                continue
            try:
                answer = "y" if args.yes else input(f"\nInstall {CATALOG[tool]['name']} with `{recipe['how']}`? [y/N] ")
            except EOFError:                       # nobody to ask: never install without a yes
                answer = ""
            if answer.strip().lower() != "y":
                print("Skipped.")
                continue
            print(f"Installing {CATALOG[tool]['name']}...")
            done = co.machine.install(tool, by=getpass.getuser())
            after = co.machine.check(tool)
            print(f"{'Installed' if done['status'] == 'installed' else 'Failed'}: {CATALOG[tool]['name']} "
                  f"{after['version']}".rstrip() + (f"\n{done['output'][-800:]}" if done["status"] != "installed" else "")
                  + (f"\n  {after['detail']}" if after["detail"] else ""))
    elif args.cmd == "ai":
        by = co.s.human(role)
        if args.action == "use":
            if not args.choice:
                raise ValueError("Usage: vittics-builder ai use claude-code|codex|api|demo")
            co.ai.use(args.choice, by)
        elif args.action == "key":
            co.ai.set_key(getpass.getpass("Anthropic API key (hidden): "), by)
            print("Saved privately. Choose it with: vittics-builder ai use api")
        elif args.action == "test":
            r = co.ai.test()
            print(("Works: " if r["ok"] else "Did not work: ") + r["reply"])
            return 0 if r["ok"] else 1
        st = co.ai.status(fresh=True)
        print(f"Agents think with: {st['label']}" + ("" if st["chosen"] else " (not chosen yet)") +
              (" · ready" if st["ready"] else f" · NOT READY: {st['reason']}"))
        for key, t in st["tools"].items():
            print(f"  {st['choices'][key]['label']:<12} " + ("not installed" if not t["installed"] else
                  f"{t['version']} · {'signed in' if t['signed_in'] else 'signed out' if t['signed_in'] is False else t['state']}"))
        print(f"  {'API key':<12} " + ("set" if st["has_key"] else "not set"))
    elif args.cmd == "build":
        from .autopilot import LEVELS, name_from
        project = co.pipeline.create_project(args.name or name_from(args.brief), args.brief, by=co.s.human(role),
                                             idea=True, budget=args.budget)
        co.pipeline.autopilot.set(project["id"], args.autopilot, co.s.human(role))
        print(f"P{project['id']} {project['name']}: autopilot {LEVELS[args.autopilot]}. Working...")
        _advance(co, project["id"])
    elif args.cmd == "autopilot":
        co.pipeline.autopilot.set(args.project, args.level, co.s.human(role))
        _advance(co, args.project)
    elif args.cmd == "keys":
        if args.action == "set":
            if not args.name:
                raise ValueError("Usage: vittics-builder keys <project> set <NAME>")
            co.vault.set(args.project, args.name, getpass.getpass(f"Value for {args.name} (hidden): "), co.s.human(role))
            print(f"Saved {args.name}. Checks, the preview and deploy commands get it; agents only see its name.")
        elif args.action == "remove":
            co.vault.delete(args.project, args.name or "", co.s.human(role))
            print(f"Removed {args.name}.")
        for name in co.vault.names(args.project):
            print(f"  {name}")
        if not co.vault.names(args.project):
            print("No keys for this project.")
    elif args.cmd == "preview":
        project = co.pipeline.project(args.project)
        if args.action == "start":
            if not project["version"]:
                raise ValueError("Nothing signed off yet: the preview runs the latest signed-off version.")
            ok, out = co.previews.start(project, project["version"])
            print(out)
            if not ok:
                return 1
            print("Keep this terminal open: the preview stops when this command ends (Ctrl+C).")
            try:
                while co.previews.current(args.project)["status"] == "running":
                    __import__("time").sleep(1)
            except KeyboardInterrupt:
                co.previews.stop(args.project, co.s.human(role))
        elif args.action == "stop":
            print("Stopped." if co.previews.stop(args.project, co.s.human(role)) else "No preview is running.")
        else:
            row = co.previews.current(args.project)
            print(f"v{row['version']} {row['status']} at {row['url']} (log: {row['log']})" if row else "No preview yet.")
    elif args.cmd == "learning":
        print("Learning is " + ("on." if co.learning.enabled else "off (learning.enabled in org.yaml)."))
        rows = co.learning.reviewers()
        print("\nReviewer accuracy (verdicts checked against what happened next):")
        for r in rows:
            print(f"  {r['name']:<12} {r['role']:<20} {r['right']}/{r['settled']} right · {r['missed']} missed problems · "
                  f"{r['too_strict']} blocked good work · {r['pending']} waiting")
        if not rows:
            print("  Nothing settled yet: it appears once releases are accepted or escalations decided.")
        print("\nApproaches the team learned:")
        for s in co.learning.proposals():
            print(f"  [{s['status']}] {s['name']} · {s['task_kind']}s on {s['signature']} work · "
                  f"from {s['evidence']} failed reviews\n      {s['prompt'][:300]}")
        if not co.learning.proposals():
            print("  None yet.")
    elif args.cmd == "machines":
        by = co.s.human(role)
        if args.action == "pair":
            if not args.name:
                raise ValueError("Usage: vittics-builder machines pair <name>")
            pairing = co.nodes.pair(args.name, by)
            print(f"On the other computer, within 10 minutes:\n\n  vittics-builder worker join <this dashboard's address> "
                  f"{pairing['code']}\n\nThe code works once.")
        elif args.action in ("assign", "unassign"):
            if not args.name or (args.action == "assign" and not args.machine):
                raise ValueError("Usage: vittics-builder machines assign <agent> <machine>  |  unassign <agent>")
            co.nodes.assign(args.name, args.machine if args.action == "assign" else None, by)
            print("Done.")
        elif args.action == "revoke":
            co.nodes.revoke(args.name, by)
            print(f"Revoked '{args.name}'. It can no longer take work; pair it again to bring it back.")
        else:
            nodes = co.nodes.list()
            for n in nodes:
                i = n["info"]
                print(f"  {'online ' if n['online'] else 'offline'} {n['name']:<16} {i.get('os', '')} {i.get('machine', '')}"
                      f" · engines: {', '.join(f'{e} ({s})' for e, s in i.get('engines', {}).items()) or 'none'}"
                      f" · agents: {', '.join(n['agents']) or 'none'} · jobs done: {n['jobs']['done'] or 0}")
            if not nodes:
                print("No worker machines. Pair one: vittics-builder machines pair <name>")
    elif args.cmd == "deployments":
        rows = co.production.list(args.project)
        if not rows:
            print("No deployments yet. Configure them under production: in org.yaml; they run after the CEO signs off.")
        for d in rows:
            print(f"  P{d['project_id']:<3} v{d['version']:<4} {d['environment']:<14} {d['status']:<16} "
                  f"{d['started_at'][:16].replace('T', ' ')}  {d['commit_sha'][:10]}")
    elif args.cmd == "costs":
        from .costs import summary
        c = summary(co.db)
        print(f"Total so far (estimate): ${c['total']['cost']:.2f}  "
              f"({c['total']['input_tokens']:,} tokens in, {c['total']['output_tokens']:,} out)")
        for p in c["projects"]:
            print(f"  P{p['id']:<3} {p['name']:<30} ${p['spent']:.2f}" + (f" of ${p['budget']:.2f}" if p["budget"] else ""))
        if c["departments"]:
            print("By department: " + ", ".join(f"{d['department']} ${d['spent']:.2f}" for d in c["departments"]))
        if c["unpriced_models"]:
            print("No price for: " + ", ".join(c["unpriced_models"]) + " (set llm.prices in org.yaml)")
    elif args.cmd == "budget":
        project = co.pipeline.set_budget(args.project, args.amount, co.s.ceo_name)
        print(f"{project['name']}: budget " + (f"${project['budget']:.2f}" if project["budget"] else "no limit")
              + f". Stage: {project['stage']}")
    elif args.cmd == "report":
        if not args.latest:
            co.reports.request(args.project, co.s.human(role))
            co.pipeline.write_reports(args.project)
        latest = [r for r in co.reports.latest(args.project) if r["status"] == "sent"]
        print(f"{latest[0]['author']}, {latest[0]['created_at'][:16].replace('T', ' ')}:\n\n{latest[0]['body']}"
              if latest else "No reports yet.")
    elif args.cmd == "feedback":
        if args.text:
            print("Support is triaging...")
            f = co.feedback.submit(args.project, args.text, co.s.human(role), source=args.source, wait=True)
            row = co.feedback.list(args.project)[0]
            print(row["result"])
            if row["tickets"]:
                print(f"\nThe team picks up new tickets on the next run: vittics-builder run {args.project}")
        else:
            for f in co.feedback.list(args.project):
                print(f"\n#{f['id']} {f['created_at'][:16].replace('T', ' ')} {f['source']}\n  {f['body']}\n  -> {f['result']}")
    elif args.cmd == "chat":
        if args.message:
            print(f"Waiting for {co.org.agent(args.agent)['name']}...")
            thread = co.chat.send(args.agent, role, args.message, project_id=args.project, wait=True)
            reply = thread["messages"][-1]
            print(f"\n{thread['agent']['name']}: {reply['body']}")
            if args.project and "T-" in reply["body"]:
                print(f"\nThe team picks up new tickets on the next run: vittics-builder run {args.project}")
        else:
            thread = co.chat.thread(args.agent, role)
            a = thread["agent"]
            print(f"{a['name']}, {a['title']} ({a['department']})")
            for m in thread["messages"]:
                who = co.s.human(role) if m["sender"] == "human" else a["name"]
                print(f"\n{m['created_at'][:16].replace('T', ' ')}  {who}:\n  " + m["body"].replace("\n", "\n  "))
            if not thread["messages"]:
                print(f'\nNo messages yet. Start with: vittics-builder chat {a["name"]} "Hello" --as {role}')
    return 0


def _idea(co: Company, args, role: str) -> None:
    cmd = args.idea_cmd or "new"
    if cmd == "new":
        brief = Path(args.brief_file).read_text(encoding="utf-8") if args.brief_file else args.brief
        if not brief:
            raise PipelineError("Describe the idea with --brief or --brief-file.")
        project = co.pipeline.create_project(args.name, brief, by=co.s.human(role), idea=True)
        print(f"Idea {project['id']} submitted: {project['name']}")
        if not args.no_run:
            _advance(co, project["id"])
    elif cmd == "decide":
        decision = "approved" if args.choice in ("internal", "commercial") else "rejected"
        approval = co.pipeline.decide(args.id, "ceo", decision, args.note, choice=args.choice)
        print(f"#{approval['id']} decided by the CEO: {args.choice}.")
        if approval["project_id"] and not args.no_run:
            _advance(co, approval["project_id"])
    elif cmd == "revisit":
        project = co.pipeline.revisit(args.project, co.s.ceo_name)
        print(f"{project['name']} is back with the CEO for a decision.")
        _print_inbox(co, "ceo")


def _print_ticket_row(t: dict) -> None:
    who = t["assignee"] or "-"
    print(f"  {t['ticket']:<7} {t['status_label']:<12} {t['priority']:<7} {t['type']:<6} {t['department'][:22]:<22} "
          f"{who:<10} P{t['project_id']} {t['title']}" + (f"  ({t['comments']} comments)" if t.get("comments") else ""))


def _ticket(co: Company, args, role: str) -> None:
    cmd = args.ticket_cmd or "list"
    by = co.s.human(role)
    if cmd == "list":
        tickets = co.tickets.search(getattr(args, "project", None), getattr(args, "status", None))
        if not tickets:
            print("No tickets.")
        for t in tickets:
            _print_ticket_row(t)
        return
    if cmd == "show":
        t = co.tickets.get(args.ticket)
    elif cmd == "new":
        t = co.tickets.create(args.project, args.title, args.description, by, type=args.type, priority=args.priority,
                              status="todo" if args.todo else "backlog", role=args.role)
        print(f"Filed {t['ticket']}.")
    elif cmd == "update":
        t = co.tickets.update(args.ticket, by, status=args.status, priority=args.priority,
                              title=args.title, description=args.description, role=args.role)
    else:
        t = co.tickets.comment(args.ticket, by, args.text)
    print(f"\n{t['ticket']}  {t['title']}\n  {t['type']} · {t['priority']} priority · {t['status_label']} · "
          f"project {t['project_id']} ({t['project']}) · {co.tickets.where(t['role'])} · {t['assignee'] or 'unassigned'} · "
          f"reported by {t['reporter']}")
    if t["description"]:
        print("\n  " + t["description"].replace("\n", "\n  "))
    for h in t["history"]:
        label = {"comment": "commented", "transfer": "transferred", "handoff": "handed off"}.get(h["kind"], "")
        print(f"\n  {h['created_at'][:16].replace('T', ' ')}  {h['author']} {label}\n    "
              + h["body"].replace("\n", "\n    "))
    if cmd in ("new", "update") and t["status"] == "todo" and not args.no_run:
        _advance(co, t["project_id"])


def _org(co: Company, args, role: str) -> None:
    cmd = args.org_cmd or "show"
    by = co.s.human(role)
    if cmd == "show":
        _print_org(co, getattr(args, "all", False))
    elif cmd == "hire":
        role_row = co.org.role(args.role)
        if role != "ceo" and co.db.one("SELECT reports_to FROM departments WHERE id=?", role_row["department"])["reports_to"] != "cto":
            raise PermissionError("That department reports to the CEO.")
        a = co.org.hire(args.role, name=args.name, model=args.model, by=by)
        print(f"Hired {a['name']} as {a['role']} (#{a['id']}, seat {a['seat']}).")
    elif cmd == "set-model":
        targets = [a["id"] for a in co.org.staff()] if args.all else [args.agent]
        if not args.all and not args.agent:
            raise OrgError("Name an agent, or use --all.")
        for ref in targets:
            agent = co.org.agent(ref)
            if not co.can_manage(role, agent):
                print(f"  skipped {agent['name']}: their department reports to the CEO")
                continue
            print(f"  {co.org.set_model(ref, args.model, by=by)['name']} -> {args.model}")
    elif cmd in ("fire", "rehire"):
        agent = co.org.agent(args.agent)
        if not co.can_manage(role, agent):
            raise PermissionError(f"{agent['name']}'s department reports to the CEO.")
        if cmd == "rehire":
            a = co.org.rehire(agent["id"], by=by, note=args.note)
            print(f"{a['name']} is back in seat {a['seat']}.")
        elif args.no_replace:
            co.org.fire(agent["id"], args.reason, by=by)
            print(f"{agent['name']} let go. Seat {agent['seat']} is empty.")
        else:
            new = co.org.replace(agent["id"], args.reason, by=by)
            print(f"{agent['name']} let go. {new['name']} takes seat {new['seat']} ({new['model']}).")
    elif cmd == "add-dept":
        co.org.add_department(args.id, args.name or args.id.title(), args.reports_to)
        print(f"Department {args.id} created.")
    elif cmd == "remove-dept":
        co.org.remove_department(args.id)
        print(f"Department {args.id} closed.")
    elif cmd == "sync":
        import yaml
        from .config import DEFAULT_ORG
        raw = yaml.safe_load(Path(args.source or DEFAULT_ORG).read_text(encoding="utf-8")) or {}
        added = co.org.sync(raw, by=by)
        for what, items in added.items():
            print(f"Added {len(items)} {what}" + (f": {', '.join(items)}" if items else ""))
    elif cmd == "add-role":
        co.org.add_role(args.id, args.dept, args.kind, [t.strip() for t in args.tools.split(",") if t.strip()],
                        args.prompt, title=args.title)
        print(f"Role {args.id} added. Staff it with: vittics-builder org hire --role {args.id}")


if __name__ == "__main__":
    sys.exit(main())
