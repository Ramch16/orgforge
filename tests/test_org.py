import pytest

from orgforge.org import OrgError


def test_seeded_from_org_file(co):
    assert len(co.org.staff()) == 9
    assert {d["id"] for d in co.org.chart()} == {"product", "engineering", "quality", "platform"}


def test_structure_is_editable(co):
    co.org.add_department("data", "Data", "cto")
    co.org.add_role("data_engineer", "data", "builder", ["read_file", "write_file", "run_command"], "Build pipelines.")
    hired = co.org.hire("data_engineer", by="CTO")
    assert hired["seat"] == "de-1" and co.org.boss_of(hired) == "cto"
    with pytest.raises(OrgError):
        co.org.remove_department("data")            # still staffed
    with pytest.raises(OrgError):
        co.org.add_role("x", "data", "builder", ["launch_rocket"], "nope")


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
