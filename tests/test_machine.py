import json
import os
import shutil
import stat
import sys

import pytest
from fastapi.testclient import TestClient

from orgforge import machine as m
from orgforge.cli import main
from orgforge.server import create_app


@pytest.fixture
def bin_dir(tmp_path, monkeypatch):
    """A PATH holding only git (the pipeline needs it) plus whatever fake tools a test adds."""
    d = tmp_path / "bin"
    d.mkdir()
    os.symlink(shutil.which("git"), d / "git")
    monkeypatch.setenv("PATH", str(d))

    def tool(name, body="print('1.0.0')", code=0):
        f = d / name
        f.write_text(f"#!{sys.executable}\nimport sys, os, json\n{body}\nsys.exit({code})\n")
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
        return f
    return d, tool


def test_commands_are_read_word_by_word():
    assert m.tokens("cd app && npm ci && python3 -m pytest -q | tee log; FOO=1 docker compose up") == \
        {"npm", "python3", "tee", "docker"}
    assert m.tokens("sudo env go test ./...") == {"go"}


def test_company_needs_core_tools_and_the_engines_its_agents_use(co):
    need = co.machine.requirements()
    assert set(need) == {"git", "python"} and need["git"] == ["OrgForge itself (every project is a Git repository)"]
    co.db.run("UPDATE agents SET model='cli:codex/gpt-6' WHERE name='Hari'")
    co.s.raw["failover"] = {"models": ["cli:gemini"]}
    co.s.sandbox_mode = "docker"
    need = co.machine.requirements()
    assert need["codex"] == ["agents work through cli:codex/gpt-6"] and "gemini" in need
    assert need["docker"] == ["sandbox.mode is docker"]


def test_projects_need_what_their_files_and_commands_use(co):
    p = co.pipeline.create_project("Shop", "A web shop.")
    root = co.pipeline.workspace(p).root
    (root / "package.json").write_text("{}")
    (root / "product.json").write_text(json.dumps({"setup": "uv sync", "checks": [
        {"id": "api", "requirement": "r", "command": "cd api && go test ./..."},
        {"id": "db", "requirement": "r", "command": "psql -c 'select 1'"}]}))
    co.s.raw["production"] = {"projects": {"Shop": {"environments": [
        {"name": "prod", "deploy": "docker compose up -d", "health": "curl -f localhost"}]}}}
    need = co.machine.requirements(p["id"])
    assert need["node"] == ["package.json"] and need["uv"] == ["product.json setup runs uv"]
    assert need["go"] == ["product.json check api runs go"] and "postgresql" in need
    assert need["docker"] == ["prod deploy runs docker"]
    assert co.machine.requirements()["node"] == ["project P1 Shop"]     # the company view names the project


def test_versions_daemons_and_sign_in_are_checked(co, bin_dir):
    d, tool = bin_dir
    assert co.machine.check("node")["state"] == "missing"
    tool("node", "print('v18.19.0')")
    assert co.machine.check("node")["state"] == "outdated"
    tool("node", "print('v24.3.0')")
    assert co.machine.check("node").items() >= {"state": "ready", "version": "24.3.0", "ok": True}.items()
    tool("docker", "print('Docker version 28.1.0') if sys.argv[1:] == ['--version'] else sys.exit(1)")
    assert co.machine.check("docker")["state"] == "not_ready"
    tool("codex", "print('codex-cli 0.160.1' if sys.argv[1] == '--version' else 'Not logged in')")
    assert co.machine.check("codex")["signed_in"] is False and co.machine.check("codex")["state"] == "signed_out"
    tool("codex", "print('codex-cli 0.160.1' if sys.argv[1] == '--version' else 'Logged in using ChatGPT')")
    assert co.machine.check("codex")["state"] == "ready"
    tool("claude", "print('2.1.0' if sys.argv[1] == '--version' else json.dumps({'loggedIn': True}))")
    assert co.machine.check("claude-code")["signed_in"] is True


def test_apple_stubs_are_not_mistaken_for_real_tools(co, monkeypatch):
    monkeypatch.setattr(m, "system", lambda: "macos")
    monkeypatch.setattr(m.shutil, "which", lambda b: "/usr/bin/git" if b == "git" else None)
    calls = []
    monkeypatch.setattr(m, "_run", lambda argv, timeout=15: (calls.append(argv), (2, "no developer tools"))[1])
    status = co.machine.check("git")
    assert status["state"] == "missing" and "xcode-select --install" in status["detail"]
    assert calls == [["xcode-select", "-p"]]                  # never ran the stub, which would pop up a dialog


def test_install_recipes_per_platform(co, monkeypatch):
    have = set()
    monkeypatch.setattr(m.shutil, "which", lambda b: f"/x/{b}" if b in have else None)
    monkeypatch.setattr(m, "system", lambda: "macos")
    assert "Homebrew" in m.Machine.recipe(co.machine, "node")["why"]      # no brew yet: run its installer yourself
    have.add("brew")
    assert m.Machine.recipe(co.machine, "node").items() >= {"auto": True, "argv": ["brew", "install", "node"]}.items()
    docker = co.machine.recipe("docker")
    assert not docker["auto"] and docker["how"] == "brew install --cask docker-desktop" and "250" in docker["why"]
    assert co.machine.recipe("gemini") == {"auto": False, "how": "Install Node.js first (it provides npm).", "needs": "node"}
    have.add("npm")
    assert co.machine.recipe("gemini")["argv"] == ["npm", "install", "-g", "@google/gemini-cli"]
    monkeypatch.setattr(m, "system", lambda: "windows")
    have.add("winget")
    assert co.machine.recipe("git")["argv"][:4] == ["winget", "install", "--id", "Git.Git"]
    assert not co.machine.recipe("docker")["auto"]                       # admin rights: yours to run
    monkeypatch.setattr(m, "system", lambda: "linux")
    assert co.machine.recipe("git") == {"auto": False, "how": "sudo apt-get install -y git",
                                        "why": "apt needs administrator rights."}


def test_install_runs_the_recipe_and_verifies(co, bin_dir, monkeypatch):
    d, tool = bin_dir
    monkeypatch.setattr(m, "system", lambda: "macos")
    log = d.parent / "brew.log"
    tool("brew", f"open({str(log)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
                 f"p = os.path.join({str(d)!r}, 'go'); open(p, 'w').write('#!/bin/sh\\necho go version go1.27.1')\n"
                 "os.chmod(p, 0o755)")
    row = co.machine.install("go", by="Lucky")
    assert row["status"] == "installed" and row["command"] == "brew install go"
    assert log.read_text() == "install go\n" and co.machine.check("go")["version"] == "1.27.1"
    assert co.db.one("SELECT 1 FROM events WHERE kind='machine' AND message LIKE 'Go installed (1.27.1)%'")


def test_install_refuses_what_it_should_not_do(co, bin_dir, monkeypatch):
    d, tool = bin_dir
    monkeypatch.setattr(m, "system", lambda: "macos")
    tool("brew")
    with pytest.raises(ValueError, match="Unknown tool"):
        co.machine.install("rm -rf /", by="Lucky")
    with pytest.raises(PermissionError, match="Run this yourself: brew install --cask docker-desktop"):
        co.machine.install("docker", by="Lucky")
    co.s.raw["machine"] = {"installs": False}
    with pytest.raises(PermissionError, match="switched off"):
        co.machine.install("go", by="Lucky")
    co.s.raw["machine"] = {}
    co.machine.installs_allowed = False                      # hosted
    with pytest.raises(PermissionError):
        co.machine.install("go", by="Lucky")
    assert not co.db.one("SELECT 1 FROM machine_installs")


def test_doctor_asks_before_installing(co, bin_dir, monkeypatch, capsys):
    d, tool = bin_dir
    monkeypatch.setattr(m, "system", lambda: "macos")
    tool("brew", f"open({str(d.parent / 'brew.log')!r}, 'a').write('called')")
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    main(["--home", str(co.s.root), "doctor", "--install", "go"])
    assert "Skipped." in capsys.readouterr().out and not (d.parent / "brew.log").exists()
    main(["--home", str(co.s.root), "doctor", "--install", "go", "--yes"])
    assert (d.parent / "brew.log").read_text() == "called"


def test_missing_commands_in_failed_checks_are_explained(co, monkeypatch):
    monkeypatch.setattr(m.shutil, "which", lambda b: "/usr/bin/python3" if b == "python3" else None)
    hints = m.Machine.explain("exit code 127\n/bin/sh: python: command not found\nsh: npm: not found\nfoo: not found")
    assert "only `python3`" in hints[0] and "install Node.js" in hints[1] and len(hints) == 2
    p = co.pipeline.create_project("Greeter", "A tiny library.")
    co.s.max_rework = 0
    co.pipeline._release_failure(co.pipeline.project(p["id"]), ["Check unit-tests: exit code 127\n/bin/sh: python: command not found"])
    [a] = [a for a in co.pipeline.inbox("cto") if a["kind"] == "release_blocked"]
    assert "This computer may be the problem, not the code" in a["summary"]


def test_approving_a_design_warns_about_missing_tools(co, bin_dir):
    p = co.pipeline.create_project("Greeter", "A tiny library.")
    co.pipeline.advance(p["id"])
    [prd] = [a for a in co.pipeline.inbox("ceo") if a["kind"] == "prd"]
    co.pipeline.decide(prd["id"], "ceo", "approved")
    co.pipeline.advance(p["id"])
    (co.pipeline.workspace(co.pipeline.project(p["id"])).root / "package.json").write_text("{}")
    [arch] = [a for a in co.pipeline.inbox("cto") if a["kind"] == "architecture"]
    co.pipeline.decide(arch["id"], "cto", "approved")
    assert co.db.one("SELECT 1 FROM events WHERE kind='machine' AND message LIKE '%missing Node.js%package.json%'")


def test_dashboard_reports_and_installs(co, bin_dir, monkeypatch):
    d, tool = bin_dir
    monkeypatch.setattr(m, "system", lambda: "macos")
    tool("brew")
    client = TestClient(create_app(co, {"ceo": "ceo-token", "cto": "cto-token"}))
    cto = {"X-Token": "cto-token"}
    report = client.get("/api/machine", headers=cto).json()
    assert {t["id"] for t in report["tools"]} == {"git", "python"} and report["installs_allowed"]
    assert client.get("/api/machine").status_code == 401
    assert client.post("/api/machine/install/docker", headers=cto).status_code == 403
    co.machine.installs_allowed = False
    assert client.post("/api/machine/install/go", headers=cto).status_code == 403
