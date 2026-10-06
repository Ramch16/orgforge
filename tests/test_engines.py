import json
import sys
import textwrap

import pytest

from vittics_builder.company import Company
from vittics_builder.engines import BUILTIN_ENGINES, parse_block, protocol
from vittics_builder.llm import MockProvider

FAKE = textwrap.dedent('''
    import json, os, sys
    prompt = sys.stdin.read()
    with open(os.environ["FAKE_LOG"], "a") as log:
        log.write(json.dumps({"argv": sys.argv[1:], "prompt": prompt, "cwd": os.getcwd(),
                              "api_key": bool(os.environ.get("ANTHROPIC_API_KEY"))}) + "\\n")
    reply = open(os.environ["FAKE_REPLY"]).read()
    print(json.dumps({"type": "result", "is_error": False, "result": reply, "total_cost_usd": 0.42, "num_turns": 3,
                      "usage": {"input_tokens": 1200, "output_tokens": 300, "cache_read_input_tokens": 50}}))
''')


@pytest.fixture
def cli(tmp_path, monkeypatch):
    fake = tmp_path / "fake_cli.py"
    fake.write_text(FAKE)
    log, reply = tmp_path / "log.jsonl", tmp_path / "reply.txt"
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_REPLY", str(reply))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-should-not-reach-the-cli")
    co = Company(tmp_path / "co", provider=MockProvider(), create=True)
    co.s.engines["fake"] = {**BUILTIN_ENGINES["claude-code"], "command": [sys.executable, str(fake), "--system", "{system}"]}

    def say(text):
        reply.write_text(text)

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return co, say, calls


def on_cli(co, name, model="cli:fake/sonnet"):
    co.db.run("UPDATE agents SET model=? WHERE name=?", model, name)
    return co.org.agent(name)


def test_protocol_lists_only_allowed_actions():
    text = protocol(["read_file", "submit_review", "comment_ticket"])
    assert '"review"' in text and '"comments"' in text and '"plan"' not in text


def test_parse_block_takes_the_last_block():
    text, data, problem = parse_block('Done.\n```vittics-builder\n{"review": {"score": 1}}\n```\nmore\n```vittics-builder\n{"review": {"score": 90}}\n```')
    assert data == {"review": {"score": 90}} and problem is None and text.startswith("Done.")
    assert parse_block("```vittics-builder\n{bad json}\n```")[2].startswith("the vittics-builder block is not valid JSON")


def test_review_through_a_cli_on_the_subscription(cli):
    co, say, calls = cli
    reviewer = on_cli(co, "Pavan")
    p = co.pipeline.create_project("Greeter", "A tiny library.")
    say('Looks good.\n```vittics-builder\n{"review": {"score": 91, "verdict": "approve", "notes": "Clear and tested."}}\n```')
    res = co.runtime.run(reviewer, "Check the work.", co.pipeline.workspace(p), project_id=p["id"])
    assert res.completed and res.review == {"score": 91.0, "verdict": "approve", "notes": "Clear and tested."}
    assert res.text == "Looks good." and res.turns == 3
    [call] = calls()
    assert call["api_key"] is False                                # the CLI bills its own login, not the API key
    assert call["cwd"].endswith("001-greeter")
    argv = call["argv"]
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert "--disallowedTools" in argv and "Edit" in argv[argv.index("--disallowedTools"):]   # reviewers only read
    assert "Bash" in argv[argv.index("--allowedTools"):argv.index("--disallowedTools")]       # but may run tests
    used = co.db.one("SELECT * FROM usage WHERE agent_id=?", reviewer["id"])
    assert used["cost"] == 0 and used["model"] == "cli:fake/sonnet" and used["input_tokens"] == 1250
    assert "via fake" in used["purpose"]


def test_builders_may_edit_and_run_and_file_tickets_and_memories(cli):
    co, say, calls = cli
    builder = on_cli(co, "Hari")
    p = co.pipeline.create_project("Greeter", "A tiny library.")
    say('Built it.\n```vittics-builder\n' + json.dumps({
        "create_tickets": [{"title": "Add input validation", "description": "Reject empty names.", "type": "bug",
                            "priority": "high", "role": "backend_engineer"}],
        "remember": [{"text": "All greetings use British spelling.", "scope": "company"}],
        "review": {"score": 99, "verdict": "approve", "notes": "self-approval is not allowed"}}) + '\n```')
    res = co.runtime.run(builder, "Build it.", co.pipeline.workspace(p), project_id=p["id"])
    argv = calls()[0]["argv"]
    allowed = argv[argv.index("--allowedTools"):]
    assert {"Edit", "Write", "Bash"} <= set(allowed) and "--disallowedTools" not in argv
    assert any(t["title"] == "Add input validation" for t in co.tickets.search(p["id"]))
    assert co.memory.list()[0]["text"] == "All greetings use British spelling."
    assert res.review is None and "review is not allowed" in res.text      # builders cannot review their own work
    assert "The project's tickets right now" in calls()[0]["prompt"]


def test_chat_without_a_project_reads_nothing(cli):
    co, say, calls = cli
    on_cli(co, "Ram")
    say("Hello Niki, all good.")
    reply = co.chat.send("Ram", "ceo", "How are things?", wait=True)["messages"][-1]["body"]
    assert reply == "Hello Niki, all good."
    argv = calls()[0]["argv"]
    assert "Edit" in argv[argv.index("--disallowedTools"):] and "Bash" in argv[argv.index("--disallowedTools"):]


def test_engine_problems_are_reported_not_raised(cli, tmp_path):
    co, say, calls = cli
    on_cli(co, "Pavan", "cli:nonexistent")
    res = co.runtime.run(co.org.agent("Pavan"), "Check.", None)
    assert not res.completed and "Unknown engine" in res.text
    co.s.engines["gone"] = {**BUILTIN_ENGINES["claude-code"], "command": ["no-such-cli-here", "-p"]}
    on_cli(co, "Pavan", "cli:gone")
    res = co.runtime.run(co.org.agent("Pavan"), "Check.", None)
    assert not res.completed and "not installed" in res.text
    on_cli(co, "Pavan", "cli:fake")
    say("Done.\n```vittics-builder\n{not json}\n```")
    res = co.runtime.run(co.org.agent("Pavan"), "Check.", None)
    assert "could not use part of the reply" in res.text and res.review is None


def test_shared_memory_reaches_later_agents(co):
    p = co.pipeline.create_project("Greeter", "A tiny library.")
    co.memory.remember("The API uses JWT tokens in cookies, never local storage.", "Sony", p["id"])
    co.memory.remember("Niki wants British spelling in every user-facing text.", "Ram", None, scope="company")
    co.memory.remember("Deploys go to the staging server first.", "Sushank", p["id"])
    top = co.memory.recall(p["id"], "Add a login form that stores the JWT tokens")[0]
    assert "JWT" in top["text"]
    other = co.pipeline.create_project("Wiki", "An internal wiki.")
    assert [m["text"] for m in co.memory.recall(other["id"], "anything")] == \
        ["Niki wants British spelling in every user-facing text."]       # company-wide only
    assert co.memory.remember("The API uses JWT tokens in cookies, never local storage.", "Hari", p["id"]) == "Already remembered."
    seen = []
    original = co.runtime.provider.complete
    def spy(**kw):
        seen.append(kw["system"])
        return original(**kw)
    co.runtime.provider.complete = spy
    co.runtime.run(co.org.agent("Hari"), "Build the JWT login", co.pipeline.workspace(p), project_id=p["id"])
    assert "What the team has learned" in seen[0] and "JWT tokens in cookies" in seen[0]


def test_a_usage_limit_pauses_work_without_blaming_anyone(cli, tmp_path):
    co, say, calls = cli
    limited = tmp_path / "limited_cli.py"                 # answers like Claude Code at its session limit
    limited.write_text("import json, sys\nsys.stdin.read()\nprint(json.dumps({'type': 'result', 'is_error': True, "
                       "'result': \"You've hit your session limit - resets 10:10pm\"}))\n")
    co.s.engines["limited"] = {**BUILTIN_ENGINES["claude-code"], "command": [sys.executable, str(limited)]}
    for name in ("Hari", "Pavan", "Badri"):
        on_cli(co, name, "cli:limited")
    p = co.pipeline.create_project("Greeter", "A tiny library.")
    co.db.run("UPDATE projects SET stage='build' WHERE id=?", p["id"])
    co.tickets.create(p["id"], "Core engine", "Build it.", "Lucky", status="todo", role="backend_engineer")
    with pytest.raises(Exception, match="paused.*session limit"):
        co.pipeline.advance(p["id"])
    t = next(t for t in co.tickets.search(p["id"]) if t["title"] == "Core engine")
    assert t["status"] == "todo" and t["attempts"] == 0                     # no rework counted
    assert not co.pipeline.inbox("cto")                                     # no escalation
    assert not co.db.one("SELECT 1 FROM reviews")                           # no scores
    assert co.db.one("SELECT 1 FROM events WHERE kind='paused' AND message LIKE '%Nothing was counted%'")
    assert not co.db.one("SELECT 1 FROM events WHERE kind='error'")
    assert co.runs.recent(co.org.agent("Hari")["id"])[0]["status"] == "failed"
