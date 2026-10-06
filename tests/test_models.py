import json
import sys
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from orgforge.company import Company
from orgforge.costs import price
from orgforge.engines import BUILTIN_ENGINES
from orgforge.llm import MockProvider, OpenAICompatProvider
from orgforge.org import OrgError


@pytest.fixture
def local_model():
    """A stand-in for Ollama's OpenAI-compatible API: first a tool call, then a final answer."""
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(body)
            if any(m["role"] == "tool" for m in body["messages"]):
                reply = {"choices": [{"message": {"content": "Wrote the file."}, "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": 80, "completion_tokens": 5}}
            else:
                reply = {"choices": [{"message": {"content": None, "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "write_file", "arguments": json.dumps({"path": "hello.txt", "content": "hi"})}}]},
                         "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 120, "completion_tokens": 20}}
            data = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/v1", seen
    server.shutdown()


def test_an_agent_on_a_local_model_uses_tools_and_costs_nothing(tmp_path, local_model):
    url, seen = local_model
    co = Company(tmp_path, provider=MockProvider(), create=True)
    co.s.endpoints["ollama"] = {"base_url": url, "free": True}
    co.runtime.provider.endpoints = co.s.endpoints
    hari = co.org.set_model("Hari", "ollama:qwen2.5-coder")
    p = co.pipeline.create_project("Greeter", "A tiny library.")
    res = co.runtime.run(hari, "Write hello.txt", co.pipeline.workspace(p), project_id=p["id"])
    assert res.completed and res.text == "Wrote the file."
    assert (co.pipeline.workspace(p).root / "hello.txt").read_text() == "hi"
    first, second = seen
    assert first["model"] == "qwen2.5-coder" and first["messages"][0]["role"] == "system"
    assert any(t["function"]["name"] == "write_file" for t in first["tools"])
    assert second["messages"][-1] == {"role": "tool", "tool_call_id": "c1", "content": "Wrote hello.txt (2 characters)."}
    used = co.db.all("SELECT * FROM usage WHERE agent_id=?", hari["id"])
    assert [u["input_tokens"] for u in used] == [120, 80] and all(u["cost"] == 0 for u in used)
    assert price(co.s, "ollama:anything") == (0.0, 0.0) and price(co.s, "openai:gpt-x") is None


def test_unreachable_local_model_says_so():
    provider = OpenAICompatProvider("ollama", "http://127.0.0.1:9/v1", timeout=2)
    with pytest.raises(RuntimeError, match="Could not reach ollama"):
        provider.complete(model="x", system="", messages=[{"role": "user", "content": "hi"}], tools=[], max_tokens=10)


def test_keys_are_required_for_hosted_providers(co, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    co.org.set_model("Hari", "openai:gpt-5")
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY is not set"):
        co.runtime.provider.complete(model="openai:gpt-5", system="", messages=[], tools=[], max_tokens=10)


def test_set_model_validates_sources(co):
    assert co.org.set_model("Hari", "lmstudio:qwen")["model"] == "lmstudio:qwen"
    assert co.org.set_model("Hari", "cli:codex")["model"] == "cli:codex"
    with pytest.raises(OrgError, match="Unknown model source"):
        co.org.set_model("Hari", "nowhere:model")
    with pytest.raises(OrgError, match="No engine"):
        co.org.set_model("Hari", "cli:nothing")


def test_message_conversion_round_trip():
    msgs = OpenAICompatProvider.to_openai("rules", [
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"},
                                          {"type": "tool_use", "id": "t1", "name": "list_files", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "a.py"},
                                     {"type": "text", "text": "Message from Niki: hurry"}]}])
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "tool", "user"]
    assert msgs[2]["tool_calls"][0]["function"] == {"name": "list_files", "arguments": "{}"}


@pytest.mark.parametrize("engine", ["codex", "gemini", "copilot", "cursor", "opencode", "qwen"])
def test_cli_presets_put_the_role_in_the_prompt_and_gate_edits(co, tmp_path, monkeypatch, engine):
    fake = tmp_path / "fake.py"
    fake.write_text(textwrap.dedent('''
        import json, os, sys
        args = sys.argv[1:] + ([sys.stdin.read()] if "-" in sys.argv[1:] else [])    # a stdin prompt, logged with the args
        open(os.environ["FAKE_LOG"], "a").write(json.dumps(args) + "\\n")
        print("Checked it.")
    '''))
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("FAKE_LOG", str(log))
    preset = BUILTIN_ENGINES[engine]
    co.s.engines[engine] = {**preset, "command": [sys.executable, str(fake), *preset["command"][1:]]}
    p = co.pipeline.create_project("Greeter", "A tiny library.")
    for name in ("Pavan", "Hari"):                                   # a reviewer, then a builder
        co.org.set_model(name, f"cli:{engine}")
        res = co.runtime.run(co.org.agent(name), "Do the work", co.pipeline.workspace(p), project_id=p["id"])
        assert res.completed and res.text == "Checked it."
    reviewer, builder = [json.loads(line) for line in log.read_text().splitlines()]
    prompt = next(a for a in reviewer if "Do the work" in a)
    assert prompt.startswith("You are Pavan")                       # role and rules lead the prompt
    write_flags = set(preset["level_args"]["write"]) - set(preset["level_args"]["read"])
    assert write_flags and not write_flags & set(reviewer) and write_flags <= set(builder)
