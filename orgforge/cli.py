"""orgforge command line."""
from __future__ import annotations

import argparse
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
    return getattr(args, "as_role", None) or os.environ.get("ORGFORGE_ROLE", "ceo")


def _print_inbox(co: Company, role: str | None) -> None:
    items = co.pipeline.inbox(role)
    if not items:
        print("Nothing is waiting for a decision.")
        return
    for a in items:
        print(f"\n#{a['id']}  [{a['required_role'].upper()}]  {a['title']}")
        for line in a["summary"].splitlines()[:25]:
            print(f"    {line}")
    print("\nDecide with: orgforge approve <id> --as <ceo|cto>   or   orgforge reject <id> --as <ceo|cto> --note \"...\"")
    if any(a["kind"] == "idea_decision" for a in items):
        print("Decide ideas with: orgforge idea decide <id> internal|commercial|park|drop --note \"...\"")


def _print_status(co: Company) -> None:
    projects = co.pipeline.overview()
    if not projects:
        print('No projects yet. Start one with: orgforge new "Name" --brief "What to build"')
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="orgforge", description="Run an AI-staffed software company as its CEO and CTO.")
    ap.add_argument("--home", help="Company folder (default: current folder or $ORGFORGE_HOME)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def who(p):
        p.add_argument("--as", dest="as_role", choices=["ceo", "cto"], help="Act as CEO or CTO (default: $ORGFORGE_ROLE or ceo)")

    sub.add_parser("init", help="Create the company in this folder")
    p = sub.add_parser("new", help="Start a project from a brief")
    p.add_argument("name"); p.add_argument("--brief"); p.add_argument("--brief-file"); p.add_argument("--no-run", action="store_true")
    p = sub.add_parser("run", help="Continue a project until it needs a decision"); p.add_argument("project", type=int)
    p = sub.add_parser("export", help="Package a signed-off product as a zip")
    p.add_argument("project", type=int); p.add_argument("--output", required=True)
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

    p = sub.add_parser("engines", help="Coding CLIs agents can work through, and whether they are ready")
    p.add_argument("--test", metavar="ENGINE", help="Run a tiny prompt through this engine")
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
    except (OrgError, PipelineError, TicketError, ChatError, FeedbackError, PermissionError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def _dispatch(args) -> int:
    if args.cmd == "init":
        co = Company(args.home, create=True)
        print(f"{co.s.company} is set up in {co.s.root}")
        print("Edit org.yaml to set the company name, your names and the model, then:")
        print('  orgforge org show\n  orgforge new "Product name" --brief "What to build"')
        return 0

    co = Company(args.home)
    role = _role(args)

    if args.cmd == "new":
        brief = Path(args.brief_file).read_text() if args.brief_file else args.brief
        if not brief:
            raise PipelineError("Give the brief with --brief or --brief-file.")
        project = co.pipeline.create_project(args.name, brief, by=co.s.ceo_name)
        print(f"Project {project['id']} created: {project['workspace']}")
        if not args.no_run:
            _advance(co, project["id"])
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
    elif args.cmd == "engines":
        import shutil
        from .engines import EngineError, run_cli
        for name, e in sorted(co.s.engines.items()):
            exe = e["command"][0]
            print(f"  {name:<14} {'installed' if shutil.which(exe) else 'not installed'}  ({exe})"
                  + ("  · uses its own login, not the API key" if e.get("subscription") else ""))
        users = co.db.all("SELECT model, COUNT(*) AS n FROM agents WHERE status!='fired' GROUP BY model")
        print("Agents by model: " + ", ".join(f"{u['model']} ({u['n']})" for u in users))
        if args.test:
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
                print(f"\nThe team picks up new tickets on the next run: orgforge run {args.project}")
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
                print(f"\nThe team picks up new tickets on the next run: orgforge run {args.project}")
        else:
            thread = co.chat.thread(args.agent, role)
            a = thread["agent"]
            print(f"{a['name']}, {a['title']} ({a['department']})")
            for m in thread["messages"]:
                who = co.s.human(role) if m["sender"] == "human" else a["name"]
                print(f"\n{m['created_at'][:16].replace('T', ' ')}  {who}:\n  " + m["body"].replace("\n", "\n  "))
            if not thread["messages"]:
                print(f'\nNo messages yet. Start with: orgforge chat {a["name"]} "Hello" --as {role}')
    return 0


def _idea(co: Company, args, role: str) -> None:
    cmd = args.idea_cmd or "new"
    if cmd == "new":
        brief = Path(args.brief_file).read_text() if args.brief_file else args.brief
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
        raw = yaml.safe_load(Path(args.source or DEFAULT_ORG).read_text()) or {}
        added = co.org.sync(raw, by=by)
        for what, items in added.items():
            print(f"Added {len(items)} {what}" + (f": {', '.join(items)}" if items else ""))
    elif cmd == "add-role":
        co.org.add_role(args.id, args.dept, args.kind, [t.strip() for t in args.tools.split(",") if t.strip()],
                        args.prompt, title=args.title)
        print(f"Role {args.id} added. Staff it with: orgforge org hire --role {args.id}")


if __name__ == "__main__":
    sys.exit(main())
