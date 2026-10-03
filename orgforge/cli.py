"""orgforge command line."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .company import Company
from .org import KINDS, OrgError
from .pipeline import PipelineError
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

    org = sub.add_parser("org", help="Org chart, hiring and firing").add_subparsers(dest="org_cmd")
    p = org.add_parser("show"); p.add_argument("--all", action="store_true", help="Include former agents")
    p = org.add_parser("hire"); p.add_argument("--role", required=True); p.add_argument("--name"); p.add_argument("--model"); who(p)
    p = org.add_parser("fire"); p.add_argument("agent"); p.add_argument("--reason", default="Decision by management")
    p.add_argument("--no-replace", action="store_true", help="Leave the seat empty"); who(p)
    p = org.add_parser("rehire"); p.add_argument("agent"); p.add_argument("--note", default=""); who(p)
    p = org.add_parser("add-dept"); p.add_argument("id"); p.add_argument("--name"); p.add_argument("--reports-to", choices=["ceo", "cto"], default="cto")
    p = org.add_parser("remove-dept"); p.add_argument("id")
    p = org.add_parser("add-role"); p.add_argument("id"); p.add_argument("--dept", required=True)
    p.add_argument("--kind", choices=KINDS, required=True); p.add_argument("--title")
    p.add_argument("--tools", default="read_file,write_file,replace_in_file,list_files,run_command", help=f"Comma-separated: {', '.join(TOOL_SPECS)}")
    p.add_argument("--prompt", required=True, help="What this role does and how")

    args = ap.parse_args(argv)
    try:
        return _dispatch(args)
    except (OrgError, PipelineError, PermissionError, RuntimeError) as exc:
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
    elif args.cmd == "serve":
        from .server import serve
        serve(co, args.host, args.port)
    elif args.cmd == "org":
        _org(co, args, role)
    return 0


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
    elif cmd == "add-role":
        co.org.add_role(args.id, args.dept, args.kind, [t.strip() for t in args.tools.split(",") if t.strip()],
                        args.prompt, title=args.title)
        print(f"Role {args.id} added. Staff it with: orgforge org hire --role {args.id}")


if __name__ == "__main__":
    sys.exit(main())
