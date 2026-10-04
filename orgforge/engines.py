"""Run agents through coding CLIs (Claude Code, and any CLI you configure) instead of the API.

An agent whose model is "cli:<engine>" or "cli:<engine>/<model>" works through that CLI in the
project workspace, on the CLI's own login (for Claude Code, your Claude subscription). The CLI
reads, edits and runs commands with its own tools. OrgForge actions (plans, reviews, tickets,
assessments) come back in a fenced ```orgforge JSON block at the end of the reply.

CLI engines run on this machine under the CLI's own permission rules, not in OrgForge's Docker
sandbox. Read-only roles get read-only tools.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile

# Verified against Claude Code 2.1 (`claude --help`): -p reads the prompt from stdin.
BUILTIN_ENGINES: dict[str, dict] = {
    "claude-code": {
        "command": ["claude", "-p", "--output-format", "json", "--no-session-persistence",
                    "--permission-mode", "acceptEdits", "--append-system-prompt", "{system}"],
        "model_args": ["--model", "{model}"],
        "allow_args": ["--allowedTools"],
        "deny_args": ["--disallowedTools"],
        "tools": {"read": ["Read", "Glob", "Grep"], "write": ["Edit", "Write"], "run": ["Bash"]},
        "prompt": "stdin",
        "output": "claude-json",
        "subscription": True,       # drop ANTHROPIC_API_KEY so the CLI bills its own login, not the API key
    },
}

ACTIONS = {
    # orgforge tool -> (key in the JSON block, how to describe its value)
    "submit_plan": ("plan", '[{"key": "short-id", "title": "...", "description": "what to build and how to tell it '
                            'is done", "role": "role id", "depends_on": ["other-key"]}]'),
    "submit_review": ("review", '{"score": 0-100, "verdict": "approve" or "request_changes", "notes": "concrete findings"}'),
    "submit_assessment": ("assessment", '{"recommendation": "build_internal" | "build_to_sell" | "park" | "drop", '
                                        '"feasibility": "achievable" | "achievable_with_risks" | "not_achievable", '
                                        '"summary": "three to six sentences"}'),
    "submit_department_plan": ("department_plan", '{"summary": "...", "tickets": [{"role": "role id", "title": "...", '
                                                  '"description": "...", "after_build": true or false}]}'),
    "close_as_answered": ("close_as_answered", '"why no work is needed" (only if the ticket asks for information)'),
    "create_ticket": ("create_tickets", '[{"title": "...", "description": "...", "type": "task" | "bug" | "story", '
                                        '"priority": "urgent" | "high" | "medium" | "low", "role": "role id"}]'),
    "comment_ticket": ("comments", '[{"ticket": "T-12 (omit for your current ticket)", "body": "..."}]'),
    "transfer_ticket": ("transfer", '{"ticket": "T-12 (omit for your current ticket)", "role": "role id", "reason": "..."}'),
    "remember": ("remember", '[{"text": "a lasting fact or decision", "scope": "project" | "company"}]'),
}
BLOCK = re.compile(r"```orgforge\s*(\{.*?\})\s*```", re.S)


class EngineError(RuntimeError):
    pass


def protocol(names: list[str]) -> str:
    """How the CLI agent reports OrgForge actions, limited to the ones this run allows."""
    rows = [f'  "{ACTIONS[n][0]}": {ACTIONS[n][1]}' for n in names if n in ACTIONS]
    if not rows:
        return "Finish with a short plain summary of what you did."
    return ("When you are finished, reply with a short plain summary, then end with exactly one fenced block "
            "tagged orgforge holding a JSON object with only the keys you use:\n```orgforge\n{\n"
            + ",\n".join(rows) + "\n}\n```\nUse the block for these company actions; do your file and command work "
            "with your own tools in the working directory.")


def parse_block(text: str) -> tuple[str, dict, str | None]:
    """Split a reply into (summary text, actions, error). Uses the last orgforge block."""
    found = BLOCK.findall(text or "")
    if not found:
        return (text or "").strip(), {}, None
    clean = BLOCK.sub("", text).strip()
    try:
        data = json.loads(found[-1])
        return clean, data if isinstance(data, dict) else {}, None if isinstance(data, dict) else "not an object"
    except ValueError as exc:
        return clean, {}, f"the orgforge block is not valid JSON ({exc})"


def actions_to_calls(data: dict) -> list[tuple[str, dict]]:
    """Turn a parsed block into (tool name, arguments) calls OrgForge already knows how to run."""
    calls: list[tuple[str, dict]] = []
    if "plan" in data:
        calls.append(("submit_plan", {"tasks": data["plan"]}))
    for key, tool in (("review", "submit_review"), ("assessment", "submit_assessment"),
                      ("department_plan", "submit_department_plan"), ("transfer", "transfer_ticket")):
        if isinstance(data.get(key), dict):
            calls.append((tool, data[key]))
    if data.get("close_as_answered"):
        calls.append(("close_as_answered", {"reason": str(data["close_as_answered"])}))
    for key, tool in (("create_tickets", "create_ticket"), ("comments", "comment_ticket"), ("remember", "remember")):
        for item in data.get(key) or []:
            if isinstance(item, dict):
                calls.append((tool, item))
    return calls


def run_cli(engine: dict, *, system: str, prompt: str, cwd: str | None, model: str, write: bool, run: bool,
            timeout: int) -> dict:
    """Run one CLI turn. Reading is always allowed; editing files and running commands only when granted."""
    fill = {"system": system, "prompt": prompt, "model": model}
    argv = [part.format(**fill) for part in engine["command"]]
    if model and engine.get("model_args"):
        argv += [part.format(**fill) for part in engine["model_args"]]
    tools = engine.get("tools") or {}
    allowed = list(tools.get("read", [])) + (list(tools.get("write", [])) if write else []) \
        + (list(tools.get("run", [])) if run else [])
    denied = [t for t in tools.get("write", []) + tools.get("run", []) if t not in allowed]
    if engine.get("allow_args") and allowed:
        argv += engine["allow_args"] + allowed
    if engine.get("deny_args") and denied:
        argv += engine["deny_args"] + denied
    env = dict(os.environ)
    if engine.get("subscription"):
        env.pop("ANTHROPIC_API_KEY", None)
    workdir = cwd or tempfile.mkdtemp(prefix="orgforge-chat-")
    try:
        proc = subprocess.run(argv, input=prompt if engine.get("prompt", "stdin") == "stdin" else None,
                              capture_output=True, text=True, cwd=workdir, env=env, timeout=timeout)
    except FileNotFoundError as exc:
        raise EngineError(f"'{argv[0]}' is not installed or not on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise EngineError(f"The CLI did not finish within {timeout} seconds.") from exc
    out = {"text": proc.stdout.strip(), "is_error": proc.returncode != 0, "cost": None,
           "input_tokens": 0, "output_tokens": 0, "turns": 1}
    if engine.get("output") == "claude-json":
        try:
            data = json.loads(proc.stdout)
        except ValueError:
            raise EngineError(f"Unexpected CLI output: {(proc.stdout or proc.stderr)[:300]}")
        usage = data.get("usage") or {}
        out.update(text=str(data.get("result", "")), is_error=bool(data.get("is_error")) or proc.returncode != 0,
                   cost=data.get("total_cost_usd"), turns=int(data.get("num_turns") or 1),
                   input_tokens=int(usage.get("input_tokens", 0)) + int(usage.get("cache_read_input_tokens", 0))
                   + int(usage.get("cache_creation_input_tokens", 0)),
                   output_tokens=int(usage.get("output_tokens", 0)))
    if out["is_error"] and not out["text"]:
        out["text"] = (proc.stderr or "").strip()[:2000]
    return out
