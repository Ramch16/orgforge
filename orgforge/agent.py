"""Runs one agent on one assignment: prompt, tool loop, result."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

from .config import Settings
from .costs import record_usage
from .db import DB
from .org import Org
from .tickets import TICKET_RULES, TICKET_TOOL_SPECS, TicketError
from .tools import ToolError, Workspace, tool_schema


@dataclass
class RunResult:
    text: str = ""
    plan: list[dict] | None = None
    review: dict | None = None
    transfer: dict | None = None
    assessment: dict | None = None
    dept_plan: dict | None = None
    filed: list[str] = field(default_factory=list)      # tickets this run filed
    answered: str | None = None                          # a question ticket closed as answered (the reason)
    completed: bool = False
    turns: int = 0
    tool_log: list[str] = field(default_factory=list)


class AgentRuntime:
    def __init__(self, db: DB, settings: Settings, org: Org, provider) -> None:
        self.db, self.s, self.org, self.provider = db, settings, org, provider
        self.tickets = None                 # set by Company; every agent on a project can use the tracker

    def _system(self, agent: dict, role: dict, on_project: bool = False, chat_with: str | None = None) -> str:
        parts = [
            f"You are {agent['name']}, {role['title']} at {self.s.company}, a software company staffed by AI "
            f"agents and led by two people: {self.s.ceo_name} (CEO) and {self.s.cto_name} (CTO).",
            role["prompt"],
        ]
        if chat_with:
            parts.append(
                f"You are in a direct chat with {chat_with}. Answer their questions about your work, the projects "
                "and the tickets honestly and briefly, in your own voice. Use your tools to check facts in the "
                "project's files and tickets rather than guessing, and say plainly when you do not know.\n"
                "You cannot change code from a chat. When they ask for a change or new work, file a ticket "
                "(create_ticket) for the role that should do it, so the team builds it through review, and tell "
                "them the ticket id. Comment on an existing ticket instead when the request belongs to it.")
            if agent["lessons"]:
                parts.append("Feedback on record for this seat:\n" + agent["lessons"])
            return "\n\n".join(parts)
        parts.append(
            "Working rules:\n"
            "- All work happens in the project workspace through your tools. Paths are relative to its root.\n"
            "- Read what exists before changing it, and keep changes within your assignment.\n"
            "- If you can run commands, run what you build. Never report something as working unless you saw it work.\n"
            "- Your work is reviewed and scored, and the score decides whether you keep this seat.\n"
            "- When you are finished, reply with a short plain summary: what you did, and anything left open.")
        if on_project and self.tickets:
            parts.append(TICKET_RULES)
        if agent["lessons"]:
            parts.append("Feedback on record for this seat:\n" + agent["lessons"])
        return "\n\n".join(parts)

    def run(self, agent: dict, instructions: str, ws: Workspace | None, *, project_id: int | None = None,
            meta: dict | None = None, depth: int = 0, history: list[dict] | None = None,
            only_tools: set[str] | None = None, chat_with: str | None = None,
            extra_tools: list[str] | None = None) -> RunResult:
        """One assignment, or one chat reply when `chat_with` is set (with `history` and a narrower tool set)."""
        role = self.org.role(agent["role"])
        names = json.loads(role["tools"])
        if only_tools is not None:
            names = [n for n in names if n in only_tools]
        names += [n for n in (extra_tools or []) if n not in names]      # stage-specific, e.g. submit_assessment
        if depth >= self.s.max_delegation_depth:
            names = [n for n in names if n != "delegate"]
        tools = [tool_schema(n) for n in names]
        if project_id and self.tickets:
            extra = [n for n in TICKET_TOOL_SPECS if n not in names and (only_tools is None or n in only_tools)]
            names = names + extra
            tools += [{"name": n, **TICKET_TOOL_SPECS[n]} for n in extra]
        meta = {**(meta or {}), "role": role["id"], "kind": role["kind"], "agent": agent["name"]}
        messages: list[dict] = [*(history or []), {"role": "user", "content": instructions}]
        result = RunResult()

        for turn in range(1, self.s.max_turns + 1):
            result.turns = turn
            resp = self.provider.complete(model=agent["model"], system=self._system(agent, role, bool(project_id), chat_with), messages=messages,
                                          tools=tools, max_tokens=self.s.max_tokens, meta=meta)
            record_usage(self.db, self.s, agent, project_id, resp.input_tokens, resp.output_tokens,
                         meta.get("purpose") or ("chat" if chat_with else role["kind"]))

            if resp.stop_reason == "max_tokens":
                # A reply cut off mid-way may hold a half-written tool call. Drop it and ask for smaller steps.
                messages.append({"role": "assistant", "content": resp.text or "(reply cut off)"})
                messages.append({"role": "user", "content": "Your reply hit the length limit and was cut off. "
                                 "Continue, and write large files in several smaller steps."})
                continue

            if not resp.tool_calls:
                result.text = resp.text
                result.completed = True
                return result

            messages.append({"role": "assistant", "content": resp.content})
            outputs = []
            for call in resp.tool_calls:
                try:
                    if call.name not in names:
                        raise ToolError(f"Your role does not have the '{call.name}' tool.")
                    out = self._execute(agent, call.name, call.input, ws, result, project_id, meta, depth)
                    failed = False
                except ToolError as exc:
                    out, failed = str(exc), True
                except (KeyError, TypeError, ValueError) as exc:
                    out, failed = f"Bad arguments for {call.name}: {exc}", True
                result.tool_log.append(f"{call.name}{' (failed)' if failed else ''}")
                outputs.append({"type": "tool_result", "tool_use_id": call.id, "content": out, "is_error": failed})
            messages.append({"role": "user", "content": outputs})

        result.text = "(Stopped: reached the turn limit before finishing.)"
        return result

    def _execute(self, agent, name, args, ws, result, project_id, meta, depth) -> str:
        if name in TICKET_TOOL_SPECS and self.tickets:
            try:
                return self.tickets.agent_tool(agent, name, args, project_id, meta.get("ticket_id"), result,
                                               requested_by=meta.get("requested_by"),
                                               feedback_id=meta.get("feedback_id"))
            except TicketError as exc:
                raise ToolError(str(exc)) from exc
        if name == "submit_plan":
            tasks = args.get("tasks") or []
            if not tasks:
                raise ToolError("The plan has no tasks.")
            from .validation import validate_plan
            validate_plan(tasks, {a["role"] for a in self.org.staff(kind="builder")})
            result.plan = tasks
            return f"Plan received: {len(tasks)} task(s)."
        if name == "submit_assessment":
            if args.get("recommendation") not in ("build_internal", "build_to_sell", "park", "drop") or \
                    args.get("feasibility") not in ("achievable", "achievable_with_risks", "not_achievable"):
                raise ToolError("Use a listed recommendation and feasibility.")
            result.assessment = {k: str(args.get(k, "")) for k in ("recommendation", "feasibility", "summary")}
            return "Assessment received."
        if name == "submit_department_plan":
            roles = {r["id"] for r in self.tickets.work_roles()} if self.tickets else set()
            tickets = args.get("tickets") or []
            for t in tickets:
                if not isinstance(t, dict) or t.get("role") not in roles or not str(t.get("title", "")).strip():
                    raise ToolError("Each ticket needs a title and a staffed role: " + ", ".join(sorted(roles)))
            result.dept_plan = {"summary": str(args.get("summary", "")), "tickets": [
                {"role": t["role"], "title": str(t["title"])[:200], "description": str(t.get("description", ""))[:4000],
                 "after_build": bool(t.get("after_build"))} for t in tickets][:20]}
            return f"Plan received: {len(result.dept_plan['tickets'])} department ticket(s)."
        if name == "close_as_answered":
            result.answered = str(args.get("reason", "")) or "Answered; no work needed."
            return "The ticket will be closed as answered once you reply."
        if name == "submit_review":
            score = float(args["score"])
            if not math.isfinite(score) or not 0 <= score <= 100 or args["verdict"] not in ("approve", "request_changes"):
                raise ToolError("Review needs a score from 0 to 100 and approve or request_changes.")
            result.review = {"score": score, "verdict": args["verdict"], "notes": args.get("notes", "")}
            return "Review received."
        if name == "delegate":
            colleague = self.org.pick(role=args["role"], project_id=project_id, exclude=agent["id"])
            if not colleague:
                roles = sorted({a["role"] for a in self.org.staff()})
                raise ToolError(f"Nobody available in role '{args['role']}'. Staffed roles: {', '.join(roles)}.")
            self.db.log("delegate", f"{agent['name']} delegated to {colleague['name']}: {args['instructions'][:120]}",
                        project_id, actor=agent["name"])
            if meta.get("ticket_id"):
                from .tickets import note
                note(self.db, meta["ticket_id"], agent["name"], f"Asked {colleague['name']} "
                     f"({self.tickets.where(colleague['role']) if self.tickets else colleague['role']}) for help: "
                     f"{args['instructions'][:300]}", kind="handoff")
            sub = self.run(colleague, f"{agent['name']} asks for your help.\n\n{args['instructions']}", ws,
                           project_id=project_id, meta={k: v for k, v in meta.items() if k == "task_key"},
                           depth=depth + 1)
            return f"{colleague['name']} reports:\n{sub.text}"
        return ws.call(name, args)
