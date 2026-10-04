"""Model providers.

`AnthropicProvider` talks to the Claude API. `MockProvider` is a scripted
stand-in so the whole company can be exercised offline (tests, demos).
To add another vendor, implement `complete()` with the same signature.
"""
from __future__ import annotations

import os
import json
import re
import threading
from dataclasses import dataclass, field


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class LLMResponse:
    content: list[dict]                 # assistant content blocks, ready to append to history
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"
    input_tokens: int = 0
    output_tokens: int = 0


class AnthropicProvider:
    def __init__(self) -> None:
        import anthropic

        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Export it, or set llm.provider to 'mock' in org.yaml."
            )
        self.client = anthropic.Anthropic(max_retries=5)

    def complete(self, *, model, system, messages, tools, max_tokens, meta=None) -> LLMResponse:
        kwargs = dict(model=model, system=system, messages=messages, max_tokens=max_tokens)
        if tools:
            kwargs["tools"] = tools
        resp = self.client.messages.create(**kwargs)
        content, texts, calls = [], [], []
        for block in resp.content:
            if block.type == "text":
                content.append({"type": "text", "text": block.text})
                texts.append(block.text)
            elif block.type == "tool_use":
                content.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})
                calls.append(ToolCall(block.id, block.name, dict(block.input)))
        return LLMResponse(
            content=content,
            text="\n".join(texts).strip(),
            tool_calls=calls,
            stop_reason=resp.stop_reason or "end_turn",
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
        )


MOCK_NAMES = ["Kiran", "Meghana", "Rohit", "Divya", "Arjun", "Keerthi", "Tarun", "Swathi", "Vamsi", "Harika",
              "Praneeth", "Sahithi", "Charan", "Bhavya", "Teja", "Mounika"]


class MockProvider:
    """Deterministic scripted agents. No network, no API key.

    Agents named in `bad_agents` (or the ORGFORGE_MOCK_BAD_AGENTS env var,
    comma-separated) get poor reviews, which lets you watch probation,
    firing and rehiring work.
    """

    def __init__(self, bad_agents: set[str] | None = None, fail_audits_once: set[str] | None = None) -> None:
        self.fail_audits_once, self._failed = set(fail_audits_once or ()), set()
        env = os.environ.get("ORGFORGE_MOCK_BAD_AGENTS", "")
        self.bad_agents = set(bad_agents or ()) | {n.strip() for n in env.split(",") if n.strip()}
        self._n, self._filed, self._lock = 0, False, threading.Lock()

    def _call(self, name: str, **inp) -> dict:
        with self._lock:                        # agents may run in parallel threads
            self._n += 1
            n = self._n
        return {"type": "tool_use", "id": f"mock_{n}", "name": name, "input": inp}

    def _script(self, meta: dict, step: int = 0) -> list[list[dict]]:
        kind, purpose = meta.get("kind"), meta.get("purpose", "")
        if purpose == "consult":                # a colleague's input on an idea
            return [[self._call("write_file", path=meta["file"],
                                content=f"# Input from {meta.get('agent')}\n\nLooks achievable as a small first "
                                        "release. Main risk: unclear demand; start small and measure.\n")]]
        if purpose == "assessment":
            return [[self._call("write_file", path="docs/ASSESSMENT.md", content=(
                        "# Assessment\n\n## Problem and users\nA small tool the team can use right away.\n\n"
                        "## Internal or sell\nStart internal; it could be sold once proven.\n\n"
                        "## Feasibility\nAchievable: small, about three build tickets.\n\n"
                        "## Effort and cost\nA few agents for a short project; low model cost.\n\n"
                        "## Revenue\nSubscription later, if it proves useful.\n\n## Risks\nUnclear demand.\n\n"
                        "## Recommendation\nBuild it for internal use first.\n"))],
                    [self._call("submit_assessment", recommendation="build_internal", feasibility="achievable",
                                summary="Small, achievable, useful internally; could be sold later. Build it for "
                                        "internal use first.")]]
        if purpose == "plan_of_action":
            tickets = [{"role": "support_specialist", "title": "Write the help guide", "after_build": True,
                        "description": "A short guide covering setup and the main use, in docs/HELP.md."},
                       {"role": "compliance_officer", "title": "Terms of use and privacy notice", "after_build": False,
                        "description": "docs/legal/TERMS.md and docs/legal/PRIVACY.md for the first release."}]
            if meta.get("selling"):
                tickets.append({"role": "product_marketer", "title": "Pricing and launch plan", "after_build": False,
                                "description": "docs/marketing/LAUNCH.md: pricing, target customers and launch steps."})
            return [[self._call("write_file", path="docs/PLAN.md", content=(
                        "# Plan of action\n\n1. Product: requirements\n2. Design: user experience\n"
                        "3. Engineering: design and build\n4. Quality and Security: checks\n5. Legal: terms and privacy\n"
                        "6. Support: help guide\n" + ("7. Marketing: pricing and launch\n" if meta.get("selling") else "")))],
                    [self._call("submit_department_plan", summary="One small release; every department has its part.",
                                tickets=tickets)]]
        if purpose == "ticket_reply":           # a question is answered and closed; a request is left for the team
            message = str(meta.get("message", ""))
            request = re.match(r"\s*(please\s+|(can|could|would) you\s+(please\s+)?)?(add|fix|build|change|create|"
                               r"make|update|implement|write|remove)\b", message, re.I)
            asks = "?" in message or re.search(r"\b(status|ready|update|progress|when|how|what|why|where)\b",
                                               message, re.I)
            return [[self._call("list_tickets")] + ([self._call("close_as_answered", reason="It asks for "
                                                    "information only.")] if asks and not request else [])]
        if purpose == "status_report":          # look at the tickets, then answer from the facts
            return [[self._call("list_tickets")]]
        if purpose == "triage":                 # Support triages customer feedback
            text = str(meta.get("feedback", "")).lower()
            if re.search(r"crash|error|bug|broken|fails|wrong", text):
                return [[self._call("list_tickets"),
                         self._call("create_ticket", title="Customer bug: " + str(meta.get("feedback", ""))[:60],
                                    description=str(meta.get("feedback", "")), type="bug", priority="high",
                                    role="backend_engineer")]]
            if re.search(r"please add|would be nice|feature|could you|wish", text):
                return [[self._call("list_tickets"),
                         self._call("create_ticket", title="Customer request: " + str(meta.get("feedback", ""))[:60],
                                    description=str(meta.get("feedback", "")), type="story", priority="low",
                                    role="backend_engineer")]]
            return [[self._call("list_tickets")]]
        if purpose == "chat":                   # asked for work in a chat: file it as a ticket
            message = str(meta.get("message", ""))
            if re.match(r"\s*(please\s+|(can|could|would) you\s+(please\s+)?)?(add|fix|build|change|create|make|update|implement)\b",
                        message, re.I):
                owner = meta.get("role") if kind in ("builder", "designer", "product", "qa") else "backend_engineer"
                return [[self._call("list_tickets"),
                         self._call("create_ticket", title=message.strip().rstrip(".?!")[:80], description=message,
                                    type="task", priority="medium", role=owner)]]
            return [[self._call("list_tickets")]]
        key = re.sub(r"[^a-z0-9_]", "_", str(meta.get("task_key", "work")).lower())
        if kind == "product":
            return [[self._call("write_file", path="docs/PRD.md",
                                content="# Requirements\n\nA small first release that works end to end.\n\n"
                                        "## Acceptance criteria\n- The core module runs.\n- Tests pass.\n")]]
        if kind == "designer":
            return [[self._call("write_file", path="docs/DESIGN.md",
                                content="# Design\n\nOne command, clear errors, no configuration.\n")]]
        if kind == "auditor":
            role = meta.get("role", "audit")
            fail = role in self.fail_audits_once and role not in self._failed
            if step == 1 and fail:
                self._failed.add(role)
            verdict = dict(score=40, verdict="request_changes", notes=f"{role}: one high-severity finding.") if fail \
                else dict(score=90, verdict="approve", notes=f"{role}: no blocking findings.")
            return [[self._call("write_file", path=f"docs/audits/{role}.md", content=f"# {role}\n\n{verdict['notes']}\n")],
                    [self._call("submit_review", **verdict)]]
        if kind == "planner":
            roles = meta.get("builder_roles") or ["backend_engineer"]
            tasks = [
                {"key": "core", "title": "Implement the core module", "role": roles[0],
                 "description": "Create the core module with unit tests.", "depends_on": []},
                {"key": "extras", "title": "Add the supporting module", "role": roles[-1],
                 "description": "Create the supporting module with unit tests.", "depends_on": ["core"]},
            ]
            return [
                [self._call("write_file", path="docs/ARCHITECTURE.md",
                            content="# Architecture\n\nPython package in `src/`, tests in `tests/`.\n"
                                    "Run tests with `python -m unittest discover -s tests`.\n")],
                [self._call("write_file", path="product.json", content=json.dumps({
                    "name": "Demo library", "setup": "Python 3.11+; no dependencies",
                    "run": "PYTHONPATH=src python -c 'from core import core; print(core())'",
                    "checks": [{"id": "unit-tests", "requirement": "Core and supporting modules work",
                                "command": "python -m unittest discover -s tests"},
                               {"id": "core-flow", "requirement": "The core module returns its result",
                                "command": "PYTHONPATH=src python -c 'from core import core; assert core() == \"core ok\"'"}]})),
                 self._call("write_file", path="README.md", content="# Demo library\nPython 3.11+. Run `PYTHONPATH=src python -c 'from core import core; print(core())'`.\n"),
                 self._call("write_file", path="docs/OPERATIONS.md", content="# Operations\nLocal library. No credentials, migrations, or services required. Install Python 3.11+.\n"),
                 self._call("submit_plan", tasks=tasks)],
            ]
        if kind == "builder":
            code = f'def {key}():\n    return "{key} ok"\n'
            test = (
                "import sys, unittest\nsys.path.insert(0, 'src')\n"
                f"from {key} import {key}\n\n\nclass T(unittest.TestCase):\n"
                f"    def test_it(self):\n        self.assertEqual({key}(), '{key} ok')\n"
            )
            return [
                [self._call("comment_ticket", body=f"Starting: implementing {key} with unit tests."),
                 self._call("write_file", path=f"src/{key}.py", content=code),
                 self._call("write_file", path=f"tests/test_{key}.py", content=test)],
                [self._call("run_command", command="python -m unittest discover -s tests"),
                 self._call("comment_ticket", body="Ran the test suite; all tests pass.")],
            ]
        if kind in ("reviewer", "qa"):
            if purpose == "integration":
                return [[self._call("run_command", command="python -m unittest discover -s tests")],
                        [self._call("write_file", path="docs/QA_REPORT.md",
                                    content="# QA report\n\nRan the unit test suite. All tests passed.\n")],
                        [self._call("submit_review", score=90, verdict="approve", notes="Demo integration check passed.")]]
            bad = meta.get("author") in self.bad_agents
            first = (self._call("list_files", path=".") if kind == "reviewer"
                     else self._call("run_command", command="python -m unittest discover -s tests"))
            verdict = dict(score=30, verdict="request_changes", notes="Does not meet the acceptance criteria.") \
                if bad else dict(score=88, verdict="approve", notes="Meets the task. Tests pass.")
            first = [first]
            if kind == "reviewer" and key == "core" and not bad and step == 0 and not self._filed:
                self._filed = True              # one follow-up per company run, filed for Engineering
                first.append(self._call("create_ticket", title="Add a usage example for the core module",
                                        description="README should show calling core() and its output.",
                                        type="task", priority="low", role="backend_engineer"))
            return [first, [self._call("submit_review", **verdict)]]
        return []

    def complete(self, *, model, system, messages, tools, max_tokens, meta=None) -> LLMResponse:
        meta = meta or {}
        if meta.get("purpose") == "name_hire":        # a teammate names a new hire
            taken = set(meta.get("taken") or ())
            text = next((n for n in MOCK_NAMES if n.lower() not in taken), "Agent")
            return LLMResponse(content=[{"type": "text", "text": text}], text=text, input_tokens=60, output_tokens=3)
        # Steps count from the latest instruction, so earlier chat history does not shift the script.
        start = max((i for i, m in enumerate(messages) if m["role"] == "user" and isinstance(m["content"], str)),
                    default=0)
        step = sum(1 for m in messages[start:] if m["role"] == "assistant")
        script = self._script(meta, step)
        allowed = {t["name"] for t in tools or []}
        if step < len(script):
            blocks = [b for b in script[step] if b["name"] in allowed]
            if blocks:
                calls = [ToolCall(b["id"], b["name"], b["input"]) for b in blocks]
                return LLMResponse(content=blocks, tool_calls=calls, stop_reason="tool_use",
                                   input_tokens=100, output_tokens=50)
        text = f"Done ({meta.get('role', 'agent')})."
        if meta.get("purpose") == "status_report":
            text = "Status report (demo)\n\n" + str(meta.get("facts", ""))
        if meta.get("purpose") == "triage":
            ticket = re.search(r"Filed (T-\d+)", json.dumps(messages))
            text = (f"Filed {ticket.group(1)} for Engineering." if ticket else
                    "This is a question, not a bug. Suggested reply: thanks for asking; the README explains how "
                    "to get started, and we are happy to help further.")
        if meta.get("purpose") in ("chat", "ticket_reply") and meta.get("facts"):
            facts = [l for l in str(meta["facts"]).splitlines() if l.strip()][:8]
            text = (f"Hi {meta.get('human', 'there')}, {meta.get('agent', 'I')} here. Here is where things stand:\n"
                    + "\n".join(facts))
            if "close_as_answered" in json.dumps(messages):
                text += "\n\nThis only asks for information, so I've answered it here and closed the ticket."
            elif meta.get("ticket_status") == "backlog":
                text += "\n\nThis ticket is in the Backlog, so nobody will work on it until you move it to To do."
        if meta.get("purpose") == "chat" and re.search(r"Filed (T-\d+)", json.dumps(messages)):
            filed = any("Filed T-" in str(b.get("content", "")) for m in messages if m["role"] == "user"
                        and isinstance(m["content"], list) for b in m["content"])
            ticket = re.search(r"Filed (T-\d+)", json.dumps(messages))
            text = (f"Thanks, {meta.get('human', 'boss')}. I've filed {ticket.group(1)} for that; it goes through "
                    "review like everything else, and you can follow it on the board." if filed and ticket else
                    f"Hi {meta.get('human', 'there')}, {meta.get('agent', 'I')} here. Noted: "
                    f"\"{str(meta.get('message', ''))[:120]}\". Ask me to add, fix or change something and I'll file "
                    "a ticket for it.")
        return LLMResponse(content=[{"type": "text", "text": text}], text=text,
                           input_tokens=100, output_tokens=20)


def make_provider(name: str):
    if name == "mock":
        return MockProvider()
    if name == "anthropic":
        return AnthropicProvider()
    raise ValueError(f"Unknown llm.provider '{name}'. Use 'anthropic' or 'mock'.")


# ---- any OpenAI-compatible endpoint: local models (Ollama, LM Studio) and other providers' keys ----------
ENDPOINTS: dict[str, dict] = {
    "ollama": {"base_url": "http://localhost:11434/v1", "free": True},
    "lmstudio": {"base_url": "http://localhost:1234/v1", "free": True},
    "openai": {"base_url": "https://api.openai.com/v1", "key_env": "OPENAI_API_KEY"},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "key_env": "OPENROUTER_API_KEY"},
    "groq": {"base_url": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY"},
}


class OpenAICompatProvider:
    """Chat completions with tool calling, the interface Ollama, LM Studio, OpenAI and many others share."""

    def __init__(self, name: str, base_url: str, api_key: str | None = None, timeout: int = 900) -> None:
        self.name, self.base_url, self.api_key, self.timeout = name, base_url.rstrip("/"), api_key, timeout

    @staticmethod
    def to_openai(system: str, messages: list[dict]) -> list[dict]:
        out = [{"role": "system", "content": system}] if system else []
        for m in messages:
            content = m["content"]
            if m["role"] == "assistant":
                if isinstance(content, str):
                    out.append({"role": "assistant", "content": content})
                    continue
                text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
                calls = [{"id": b["id"], "type": "function",
                          "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {})}}
                         for b in content if b.get("type") == "tool_use"]
                out.append({"role": "assistant", "content": text or None, **({"tool_calls": calls} if calls else {})})
            elif isinstance(content, str):
                out.append({"role": "user", "content": content})
            else:
                for b in content:
                    if b.get("type") == "tool_result":
                        out.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": str(b.get("content", ""))})
                texts = [b["text"] for b in content if b.get("type") == "text"]
                if texts:
                    out.append({"role": "user", "content": "\n\n".join(texts)})
        return out

    def complete(self, *, model, system, messages, tools, max_tokens, meta=None) -> LLMResponse:
        import urllib.error
        import urllib.request
        body = {"model": model, "messages": self.to_openai(system, messages), "max_tokens": max_tokens}
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                                               "parameters": t["input_schema"]}} for t in tools]
        req = urllib.request.Request(f"{self.base_url}/chat/completions", data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json",
                                              **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"{self.name} refused the request ({exc.code}): {exc.read().decode(errors='replace')[:300]}")
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Could not reach {self.name} at {self.base_url}: is it running? ({exc.reason})")
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        text = msg.get("content") or ""
        calls, content = [], ([{"type": "text", "text": text}] if text else [])
        for c in msg.get("tool_calls") or []:
            fn = c.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {"_unparsed": fn.get("arguments")}
            cid = c.get("id") or f"call_{len(calls)}"
            calls.append(ToolCall(cid, fn.get("name", ""), args if isinstance(args, dict) else {"value": args}))
            content.append({"type": "tool_use", "id": cid, "name": fn.get("name", ""), "input": args})
        finish = choice.get("finish_reason")
        usage = data.get("usage") or {}
        return LLMResponse(content=content, text=text.strip(), tool_calls=calls,
                           stop_reason="max_tokens" if finish == "length" else "tool_use" if calls else "end_turn",
                           input_tokens=int(usage.get("prompt_tokens") or 0),
                           output_tokens=int(usage.get("completion_tokens") or 0))


class RoutingProvider:
    """Sends each agent's calls where its model lives: "ollama:qwen2.5-coder" to Ollama, "openai:..." to OpenAI,
    and plain model ids to the company's provider (Anthropic, or the offline mock)."""

    def __init__(self, default, endpoints: dict[str, dict]) -> None:
        self.default, self.endpoints, self._made = default, endpoints, {}

    def endpoint(self, prefix: str) -> OpenAICompatProvider:
        if prefix not in self._made:
            e = self.endpoints[prefix]
            key = os.environ.get(e["key_env"]) if e.get("key_env") else None
            if e.get("key_env") and not key:
                raise RuntimeError(f"{e['key_env']} is not set. Export it to use {prefix} models.")
            self._made[prefix] = OpenAICompatProvider(prefix, e["base_url"], key)
        return self._made[prefix]

    def complete(self, *, model, **kw) -> LLMResponse:
        prefix, sep, name = str(model).partition(":")
        if sep and prefix in self.endpoints:
            return self.endpoint(prefix).complete(model=name, **kw)
        return self.default.complete(model=model, **kw)
