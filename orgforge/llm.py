"""Model providers.

`AnthropicProvider` talks to the Claude API. `MockProvider` is a scripted
stand-in so the whole company can be exercised offline (tests, demos).
To add another vendor, implement `complete()` with the same signature.
"""
from __future__ import annotations

import os
import json
import re
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
        self._n, self._filed = 0, False

    def _call(self, name: str, **inp) -> dict:
        self._n += 1
        return {"type": "tool_use", "id": f"mock_{self._n}", "name": name, "input": inp}

    def _script(self, meta: dict, step: int = 0) -> list[list[dict]]:
        kind, purpose = meta.get("kind"), meta.get("purpose", "")
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
        step = sum(1 for m in messages if m["role"] == "assistant")
        script = self._script(meta, step)
        allowed = {t["name"] for t in tools or []}
        if step < len(script):
            blocks = [b for b in script[step] if b["name"] in allowed]
            if blocks:
                calls = [ToolCall(b["id"], b["name"], b["input"]) for b in blocks]
                return LLMResponse(content=blocks, tool_calls=calls, stop_reason="tool_use",
                                   input_tokens=100, output_tokens=50)
        text = f"Done ({meta.get('role', 'agent')})."
        return LLMResponse(content=[{"type": "text", "text": text}], text=text,
                           input_tokens=100, output_tokens=20)


def make_provider(name: str):
    if name == "mock":
        return MockProvider()
    if name == "anthropic":
        return AnthropicProvider()
    raise ValueError(f"Unknown llm.provider '{name}'. Use 'anthropic' or 'mock'.")
