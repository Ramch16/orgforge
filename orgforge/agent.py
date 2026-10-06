"""Runs one agent on one assignment: prompt, tool loop, result."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

from .config import Settings
from .costs import record_usage
from .engines import ACTIONS, EngineError, EngineUnavailable, actions_to_calls, parse_block, protocol, run_cli
from .failover import limit_key
from .memory import MEMORY_TOOL_SPECS
from .runs import describe_call
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
    route_id: int | None = None
    evaluation: dict | None = None
    journey: dict | None = None


class AgentRuntime:
    def __init__(self, db: DB, settings: Settings, org: Org, provider) -> None:
        self.db, self.s, self.org, self.provider = db, settings, org, provider
        self.tickets = None                 # set by Company; every agent on a project can use the tracker
        self.memory = None                  # set by Company; shared long-term memory
        self.runs = None                    # set by Company; live run log and steering
        self.skills = None                  # set by Company; guidelines by kind of role
        self.learning = None
        self.router = None
        self.failover = None
        self.integrations = None

    def _system(self, agent: dict, role: dict, on_project: bool = False, chat_with: str | None = None,
                extra: str = "") -> str:
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
            if extra:
                parts.append(extra)
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
        if extra:
            parts.append(extra)
        return "\n\n".join(parts)

    def run(self, agent: dict, instructions: str, ws: Workspace | None, **kw) -> RunResult:
        """One assignment, or one chat reply when `chat_with` is set. Logged live, and open to steering."""
        if not self.runs:
            return self._run(agent, instructions, ws, **kw)
        meta = dict(kw.get("meta") or {})
        waiting = self.runs.take_steers(agent["id"])              # sent while the agent was between runs
        if waiting:
            instructions += "\n\n" + self.runs.steer_text(waiting)
        kind = self.org.role(agent["role"])["kind"]
        if self.learning:
            strategy, signature, instructions = self.learning.prepare(kind,instructions,meta.get('strategy'))
            meta = {**meta,'strategy':strategy,'signature':signature}
        pid = kw.get("project_id")
        model, reason = self.router.choose(agent, meta, kind) if self.router else (agent["model"], "")
        failover = self.failover if self.failover and self.failover.enabled else None
        routing = self.s.raw.get("routing") or {}
        avoid = meta.get("author_model") if routing.get("enabled") is True and routing.get("independent_reviews") else None
        rest = failover.resting(model) if failover else None
        if rest:                                 # already resting: go straight to a backup, without spending a call
            ready = failover.pick(model, avoid=avoid)
            if ready is None:
                raise EngineUnavailable(f"{limit_key(model)} is resting until {failover.when(rest['until'])} "
                                        f"({rest['reason']}) and no failover engine is ready")
            reason = f"Failover: {limit_key(model)} is resting until {failover.when(rest['until'])}"
            model = ready
        tried: list[str] = []
        while True:
            agent = {**agent, "model": model}
            route_id, route_started = None, None
            if self.router:
                agent, route_id, route_started = self.router.start(agent, pid, meta, kind, model, reason)
            run_id = self.runs.start(agent, pid, meta.get("ticket_id"),
                                     meta.get("purpose") or ("chat" if kw.get("chat_with") else kind), instructions)
            if not tried:
                for s in waiting:
                    self.runs.event(run_id, "steer", f"{s['author']}: {s['body']}")
            else:
                self.runs.event(run_id, "text", f"(Continuing on {model}: {reason}.)")
            kw["meta"] = {**meta, "run_id": run_id}
            try:
                res = self._run(agent, instructions, ws, **kw)
            except Exception as exc:
                self.runs.finish(run_id, "failed", f"{type(exc).__name__}: {exc}")
                limited = isinstance(exc, EngineUnavailable)
                if route_id:
                    self.router.finish(route_id, route_started, 'unavailable' if limited else 'failed', run_id)
                if not (limited and failover):
                    raise
                until = failover.rest(model, str(exc))
                tried.append(model)
                following = failover.pick(model, exclude=set(tried), avoid=avoid)
                if following is None:
                    raise EngineUnavailable(f"{exc}; no failover engine is ready") from exc
                reason = f"Failover from {limit_key(model)}: usage limit, back around {failover.when(until)}"
                self.db.log("failover", f"{agent['name']} moved from {model} to {following}: {limit_key(model)} hit "
                            f"its usage limit (back around {failover.when(until)}).", pid, actor=agent["name"])
                if len(tried) == 1 and ws is not None:
                    instructions += ("\n\nNote: an earlier attempt at this assignment on another engine stopped "
                                     "part-way (usage limit). Files in the workspace may already be partly changed: "
                                     "check what is there (for example git status and git diff) before continuing.")
                model = following
                continue
            self.runs.finish(run_id, "done" if res.completed else "stopped", res.text)
            if route_id:
                res.route_id = route_id
                self.router.finish(route_id, route_started, 'completed' if res.completed else 'stopped', run_id)
            return res

    def _run(self, agent: dict, instructions: str, ws: Workspace | None, *, project_id: int | None = None,
             meta: dict | None = None, depth: int = 0, history: list[dict] | None = None,
             only_tools: set[str] | None = None, chat_with: str | None = None,
             extra_tools: list[str] | None = None) -> RunResult:
        role = self.org.role(agent["role"])
        names = json.loads(role["tools"])
        if only_tools is not None:
            names = [n for n in names if n in only_tools]
        names += [n for n in (extra_tools or []) if n not in names]      # stage-specific, e.g. submit_assessment
        if self.integrations:
            enabled = self.integrations.names(role['id'])
            names = [n for n in names if n not in ('browser_journey', 'mcp_call', 'mcp_list_tools') or n in enabled]
            names += [n for n in enabled if n not in names and (only_tools is None or n in only_tools)]
        if depth >= self.s.max_delegation_depth:
            names = [n for n in names if n != "delegate"]
        tools = [self.integrations.schema(role['id'], n) if self.integrations and
                 n in ('browser_journey', 'mcp_call', 'mcp_list_tools') else tool_schema(n) for n in names]
        if project_id and self.tickets:
            extra = [n for n in TICKET_TOOL_SPECS if n not in names and (only_tools is None or n in only_tools)]
            names = names + extra
            tools += [{"name": n, **TICKET_TOOL_SPECS[n]} for n in extra]
        if project_id and self.memory:
            for name, spec in MEMORY_TOOL_SPECS.items():
                if name not in names and (only_tools is None or name in only_tools) and (not chat_with or name != 'remember'):
                    names.append(name)
                    tools.append({'name': name, **spec})
        meta = {**(meta or {}), "role": role["id"], "kind": role["kind"], "agent": agent["name"]}
        memories = self.memory.as_text(self.memory.recall(project_id, instructions)) if self.memory and project_id else ""
        if self.skills and not chat_with:
            memories = "\n\n".join(x for x in (self.skills.for_kind(role["kind"], role["department"]), memories) if x)
        if str(agent["model"]).startswith("cli:"):
            return self._run_cli(agent, role, names, instructions, ws, project_id, meta, history, chat_with, memories)
        messages: list[dict] = [*(history or []), {"role": "user", "content": instructions}]
        result = RunResult()

        for turn in range(1, self.s.max_turns + 1):
            result.turns = turn
            resp = self.provider.complete(model=agent["model"], system=self._system(agent, role, bool(project_id), chat_with, memories), messages=messages,
                                          tools=tools, max_tokens=self.s.max_tokens, meta=meta)
            record_usage(self.db, self.s, agent, project_id, resp.input_tokens, resp.output_tokens,
                         meta.get("purpose") or ("chat" if chat_with else role["kind"]), run_id=meta.get('run_id'))

            if resp.stop_reason == "max_tokens":
                # A reply cut off mid-way may hold a half-written tool call. Drop it and ask for smaller steps.
                messages.append({"role": "assistant", "content": resp.text or "(reply cut off)"})
                messages.append({"role": "user", "content": "Your reply hit the length limit and was cut off. "
                                 "Continue, and write large files in several smaller steps."})
                continue

            run_id = meta.get("run_id")
            if run_id and resp.text and resp.tool_calls:
                self.runs.event(run_id, "text", resp.text)
            if not resp.tool_calls:
                result.text = resp.text
                result.completed = True
                return result

            messages.append({"role": "assistant", "content": resp.content})
            outputs = []
            for call in resp.tool_calls:
                if run_id:
                    self.runs.event(run_id, "tool", describe_call(call.name, call.input))
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
                if run_id:
                    self.runs.event(run_id, "error" if failed else "output", str(out))
            steers = self.runs.take_steers(agent["id"]) if run_id else []
            if steers:                              # the CEO or CTO stepped in: the agent reads it on this step
                outputs.append({"type": "text", "text": self.runs.steer_text(steers)})
                for s in steers:
                    self.runs.event(run_id, "steer", f"{s['author']}: {s['body']}")
            messages.append({"role": "user", "content": outputs})

        result.text = "(Stopped: reached the turn limit before finishing.)"
        return result

    def _run_cli(self, agent, role, names, instructions, ws, project_id, meta, history, chat_with, memories) -> RunResult:
        """One run through a coding CLI on its own login (see engines.py)."""
        result = RunResult()
        engine_name, _, model = str(agent["model"])[4:].partition("/")
        engine = self.s.engines.get(engine_name)
        if not engine:
            result.text = f"(Unknown engine '{engine_name}'. Configure it under engines: in org.yaml.)"
            return result
        system = self._system(agent, role, bool(project_id), chat_with, memories) + "\n\n" + protocol(names)
        prompt = instructions
        if history:
            prompt = "Conversation so far:\n" + "\n".join(
                f"{'Them' if m['role'] == 'user' else 'You'}: {m['content']}" for m in history) + "\n\nNow:\n" + prompt
        if project_id and self.tickets and "list_tickets" in names:   # no live tool: give the board up front
            board = self.tickets.agent_tool(agent, "list_tickets", {}, project_id, meta.get("ticket_id"), result)
            prompt += f"\n\nThe project's tickets right now:\n{board}"
        can_write = bool(ws) and bool({"write_file", "replace_in_file"} & set(names))   # same rights as the role's tools
        can_run = bool(ws) and "run_command" in names
        try:
            out = run_cli(engine, system=system, prompt=prompt, cwd=str(ws.root) if ws else None, model=model,
                          write=can_write, run=can_run, timeout=self.s.cli_timeout)
        except EngineUnavailable as exc:      # a usage limit is nobody's failure: stop, don't score or rework
            raise EngineUnavailable(f"{engine_name} is unavailable: {exc}") from exc
        except EngineError as exc:
            result.text = f"(The {engine_name} engine failed: {exc})"
            return result
        cost = 0.0 if engine.get("subscription") else out["cost"]
        record_usage(self.db, self.s, agent, project_id, out["input_tokens"], out["output_tokens"],
                     f"{meta.get('purpose') or ('chat' if chat_with else role['kind'])} via {engine_name}", cost=cost,
                     run_id=meta.get('run_id'))
        text, data, problem = parse_block(out["text"])
        if meta.get("run_id") and self.runs:
            self.runs.event(meta["run_id"], "text", f"(through {engine_name}, {out['turns']} turns)")
        result.turns, result.completed = out["turns"], not out["is_error"]
        notes = [problem] if problem else []
        for name, args in actions_to_calls(data):
            if name not in names:
                notes.append(f"{ACTIONS.get(name, (name,))[0]} is not allowed in this assignment")
                continue
            try:
                self._execute(agent, name, args, ws, result, project_id, meta, 0)
                result.tool_log.append(name)
            except (ToolError, KeyError, TypeError, ValueError) as exc:
                notes.append(f"{name}: {exc}")
                result.tool_log.append(f"{name} (failed)")
        result.text = text + (("\n\n(OrgForge could not use part of the reply: " + "; ".join(notes) + ")") if notes else "")
        return result

    def _execute(self, agent, name, args, ws, result, project_id, meta, depth) -> str:
        if name in ('browser_journey', 'mcp_call', 'mcp_list_tools') and self.integrations:
            return self.integrations.call(agent['role'], name, args, ws)
        if name == "remember" and self.memory:
            return self.memory.remember(args.get("text", ""), agent["name"], project_id, args.get("scope", "project"),
                                        args.get('category', 'decision'),
                                        args.get('source') or (f"run:{meta['run_id']}" if meta.get('run_id') else ''))
        if name == 'recall_memory' and self.memory:
            return json.dumps(self.memory.search(project_id, args['query'], category=args.get('category'),
                                                include_history=args.get('include_history', False)))
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
        if name == 'submit_journey':
            steps=args.get('steps')
            if not isinstance(steps,list) or not 1<=len(steps)<=30 or any(not isinstance(s,dict) or
                s.get('action') not in ('click','fill','assert_text','screenshot','goto') for s in steps):
                raise ToolError('Customer journey needs 1–30 supported browser steps.')
            if not any(s['action']=='assert_text' and isinstance(s.get('value'),str) and s['value'].strip() for s in steps):
                raise ToolError('Customer journey requires an executable text assertion.')
            result.journey={'steps':steps,'rationale':str(args.get('rationale',''))[:4000]}
            return 'Journey plan recorded; the browser executor will independently verify it.'
        if name == 'submit_evaluation':
            from .assessments import DIMENSIONS
            scores=args.get('scores')
            if not isinstance(scores,dict) or set(scores)!=set(DIMENSIONS) or any(
                isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or not 0<=v<=100 for v in scores.values()):
                raise ToolError('Evaluation requires all eight scores from 0–100.')
            findings=args.get('findings')
            if not isinstance(findings,list) or len(findings)>100 or any(not isinstance(f,dict) or
                f.get('severity') not in ('info','warning','error','critical') or not all(isinstance(f.get(k),str) and f[k].strip() for k in ('title','body')) for f in findings):
                raise ToolError('Evaluation findings require title, body and severity.')
            result.evaluation={'scores':scores,'findings':findings}
            return 'Evaluation recorded.'
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
