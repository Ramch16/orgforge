import pytest

from orgforge.org import OrgError


def test_seeded_from_org_file(co):
    assert len(co.org.staff()) == 22
    assert {d["id"] for d in co.org.chart()} == {
        "product", "design", "marketing", "support", "compliance",
        "engineering", "data", "quality", "security", "platform"}
    assert all(d["agents"] for d in co.org.chart())      # every department is staffed


def test_structure_is_editable(co):
    co.org.add_department("research", "Research", "cto")
    co.org.add_role("research_scientist", "research", "builder", ["read_file", "write_file", "run_command"], "Run experiments.")
    hired = co.org.hire("research_scientist", by="CTO")
    assert hired["seat"] == "rs-1" and co.org.boss_of(hired) == "cto"
    with pytest.raises(OrgError):
        co.org.remove_department("research")        # still staffed
    with pytest.raises(OrgError):
        co.org.add_role("x", "research", "builder", ["launch_rocket"], "nope")


def test_replace_passes_on_lessons_and_a_stronger_model(co):
    kenji = co.org.agent("Kenji")
    co.perf.record(kenji["id"], 20, source="peer", reviewer="Soren", notes="No tests were written.")
    new = co.org.replace("Kenji", "poor results")
    assert co.org.agent(kenji["id"])["status"] == "fired"
    assert new["seat"] == kenji["seat"] and new["generation"] == 2 and new["predecessor_id"] == kenji["id"]
    assert "No tests were written." in new["lessons"]
    assert new["model"] == "claude-opus-5-5"            # one step up from the default


def test_rehire_reinstates_and_benches_the_successor(co):
    kenji = co.org.agent("Kenji")
    successor = co.org.replace("Kenji", "poor results")
    back = co.org.rehire(kenji["id"])
    assert back["status"] == "active" and back["score"] is None and back["evals"] == 0
    assert co.org.agent(successor["id"])["status"] == "fired"
    with pytest.raises(OrgError):
        co.org.rehire(kenji["id"])                      # already back


def test_work_goes_to_staff_in_good_standing(co):
    ife, kenji = co.org.agent("Ife"), co.org.agent("Kenji")
    co.db.run("UPDATE agents SET status='probation' WHERE id=?", ife["id"])
    assert co.org.pick(role="backend_engineer")["id"] == kenji["id"]
    co.org.fire("Kenji", "x")
    assert co.org.pick(role="backend_engineer")["id"] == ife["id"]


def test_sync_adds_what_is_missing_and_leaves_the_rest(co):
    co.org.fire("Kenji", "left", by="CTO")                      # an emptied seat stays empty
    extra = {
        "departments": [{"id": "research", "name": "Research", "reports_to": "cto"}, {"id": "security"}],
        "roles": {"research_scientist": {"department": "research", "kind": "builder", "tools": ["read_file"], "prompt": "x"}},
        "seats": [{"seat": "rs-1", "role": "research_scientist", "name": "Nadia"}, {"seat": "be-2", "role": "backend_engineer"}],
    }
    added = co.org.sync(extra)
    assert added["departments"] == ["research"] and added["roles"] == ["research_scientist"]
    assert len(added["agents"]) == 1 and added["agents"][0] != "Nadia"      # name already taken
    assert co.org.sync(extra) == {"departments": [], "roles": [], "agents": []}


def test_old_databases_accept_new_role_kinds(tmp_path):
    import sqlite3
    from orgforge.db import DB
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE departments (id TEXT PRIMARY KEY, name TEXT NOT NULL, reports_to TEXT NOT NULL);
        CREATE TABLE roles (id TEXT PRIMARY KEY, title TEXT NOT NULL, department TEXT NOT NULL REFERENCES departments(id),
          kind TEXT NOT NULL CHECK (kind IN ('product','planner','builder','reviewer','qa')), tools TEXT NOT NULL, prompt TEXT NOT NULL);
        INSERT INTO departments VALUES ('eng', 'Eng', 'cto');
        INSERT INTO roles VALUES ('dev', 'Dev', 'eng', 'builder', '[]', 'x');
    """)
    old.commit(); old.close()
    db = DB(path)
    db.run("INSERT INTO roles VALUES ('sec', 'Sec', 'eng', 'auditor', '[]', 'x')")
    assert {r["id"] for r in db.all("SELECT id FROM roles")} == {"dev", "sec"}
    db.run("INSERT INTO agents (seat, name, role, model, hired_at) VALUES ('d-1', 'A', 'dev', 'm', 'now')")
