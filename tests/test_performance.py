import json


def rate(co, name, *scores):
    agent = co.org.agent(name)
    outcome = None
    for s in scores:
        co.perf.record(agent["id"], s, source="peer", reviewer="Pavan", notes=f"scored {s}")
        outcome = co.perf.evaluate(agent["id"])
    return outcome


def test_no_judgement_before_enough_evidence(co):
    assert rate(co, "Hari", 10, 10) is None
    assert co.org.agent("Hari")["status"] == "active"


def test_probation_then_recovery(co):
    assert rate(co, "Hari", 55, 55, 55) == "probation"
    assert "probation" in co.org.agent("Hari")["lessons"]
    assert rate(co, "Hari", 95) == "recovered"
    assert co.org.agent("Hari")["status"] == "active"


def test_failed_probation_asks_the_cto_then_replaces(co):
    rate(co, "Hari", 55, 55, 55)
    assert rate(co, "Hari", 50, 50) == "replace"
    [approval] = co.pipeline.inbox("cto")                # engineering reports to the CTO
    assert json.loads(approval["payload"])["action"] == "replace"
    assert co.org.agent("Hari")["status"] == "probation"  # nothing happens until a human decides
    co.pipeline.decide(approval["id"], "cto", "approved")
    assert co.org.pick(role="backend_engineer", exclude=co.org.agent("Sandy")["id"])["generation"] == 2


def test_human_can_overrule_hr(co):
    rate(co, "Ram", 30, 30, 30)                        # below the floor: straight to replacement
    [approval] = co.pipeline.inbox("ceo")                # product reports to the CEO
    co.pipeline.decide(approval["id"], "ceo", "rejected")
    assert co.org.agent("Ram")["status"] == "probation"


def test_auto_fire_needs_no_approval(make_company):
    co = make_company(auto_fire=True)
    assert rate(co, "Hari", 20, 20, 20) == "replace"
    assert co.pipeline.inbox() == [] and co.org.agent("be-1")["generation"] == 2


def test_worse_successor_brings_the_predecessor_back(make_company):
    co = make_company(auto_fire=True)
    ife = co.org.agent("Hari")
    rate(co, "Hari", 38, 38, 38)                          # fired at 38
    successor = co.org.agent("be-1")
    assert successor["predecessor_id"] == ife["id"]
    assert rate(co, successor["id"], 25, 25, 25) == "rehire"
    assert co.org.agent("be-1")["id"] == ife["id"]
    assert co.org.agent(successor["id"])["status"] == "fired"
