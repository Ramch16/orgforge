import json

from vittics_builder import legacy
from vittics_builder import worker_client as wc
from vittics_builder.cli import legacy_main
from vittics_builder.company import Company
from vittics_builder.engines import parse_block
from vittics_builder.llm import MockProvider


def test_old_environment_variables_still_work(monkeypatch):
    monkeypatch.setenv("ORGFORGE_HOME", "/old/home")
    monkeypatch.setenv("ORGFORGE_PROVIDER", "anthropic")
    monkeypatch.setenv("VITTICS_PROVIDER", "mock")                   # the new name wins when both are set
    monkeypatch.delenv("VITTICS_HOME", raising=False)
    legacy.adopt_env()
    import os
    assert os.environ["VITTICS_HOME"] == "/old/home" and os.environ["VITTICS_PROVIDER"] == "mock"


def test_an_orgforge_company_moves_to_the_new_folder_with_its_data(tmp_path):
    co = Company(tmp_path, provider=MockProvider(), create=True)
    p = co.pipeline.create_project("Greeter", "A tiny library.")
    workspace = co.pipeline.project(p["id"])["workspace"]
    co.db.conn.close()
    (tmp_path / ".vittics").rename(tmp_path / ".orgforge")             # as an OrgForge 0.18 company looks on disk
    again = Company(tmp_path, provider=MockProvider())
    assert (tmp_path / ".vittics" / "company.db").exists() and not (tmp_path / ".orgforge").exists()
    assert again.pipeline.project(p["id"])["workspace"] == workspace   # project paths are unchanged
    assert again.pipeline.workspace(again.pipeline.project(p["id"])).root.is_dir()


def test_nothing_moves_when_both_folders_exist(tmp_path):
    (tmp_path / ".orgforge").mkdir()
    (tmp_path / ".vittics").mkdir()
    assert legacy.migrate_company(tmp_path) is False and (tmp_path / ".orgforge").exists()


def test_the_desktop_app_keeps_an_existing_orgforge_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(legacy.Path, "home", lambda: tmp_path)
    assert legacy.desktop_home() == tmp_path / "VitticsBuilder"
    (tmp_path / "OrgForge").mkdir()
    assert legacy.desktop_home() == tmp_path / "OrgForge"              # its workspaces' paths point there
    (tmp_path / "VitticsBuilder").mkdir()
    assert legacy.desktop_home() == tmp_path / "VitticsBuilder"


def test_a_paired_worker_keeps_its_pairing(tmp_path, monkeypatch):
    monkeypatch.setattr(legacy.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(wc.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(wc, "CONFIG", tmp_path / ".vittics" / "worker.json")
    monkeypatch.delenv("VITTICS_WORKER_CONFIG", raising=False)
    old = tmp_path / ".orgforge" / "worker.json"
    old.parent.mkdir()
    old.write_text(json.dumps({"url": "http://10.0.0.2:4700", "node_id": 1, "name": "laptop", "secret": "ab"}))
    assert wc.load()["name"] == "laptop" and not old.exists() and wc.CONFIG.exists()


def test_cli_agents_may_still_answer_with_the_old_block_tag():
    assert parse_block('Done.\n```orgforge\n{"review": {"score": 90}}\n```')[1] == {"review": {"score": 90}}
    assert parse_block('Done.\n```vittics-builder\n{"review": {"score": 91}}\n```')[1] == {"review": {"score": 91}}


def test_the_old_command_says_it_was_renamed(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["orgforge", "--help"])
    try:
        legacy_main()
    except SystemExit:
        pass
    out = capsys.readouterr()
    assert "now Vittics Builder" in out.err and "usage: vittics-builder" in out.out


def test_a_moved_company_finds_its_workspaces(tmp_path):
    old_root, new_root = tmp_path / "var-lib-orgforge", tmp_path / "var-lib-vittics-builder"
    co = Company(old_root, provider=MockProvider(), create=True)
    p = co.pipeline.create_project("Greeter", "A tiny library.")
    co.pipeline.workspace(co.pipeline.project(p["id"])).write_file("README.md", "# Greeter\n")
    co.db.conn.close()
    old_root.rename(new_root)                                         # e.g. the hosted data folder's new path
    moved = Company(new_root, provider=MockProvider())
    project = moved.pipeline.project(p["id"])
    assert project["workspace"].startswith(str(new_root))
    assert moved.pipeline.workspace(project).read_file("README.md") == "# Greeter\n"
    assert moved.db.one("SELECT 1 FROM events WHERE message LIKE 'Workspace found at%'")
