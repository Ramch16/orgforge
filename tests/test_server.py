import time

from fastapi.testclient import TestClient

from orgforge.server import create_app


def wait_for(co, stage, pid=1):
    for _ in range(100):
        if co.pipeline.project(pid)["stage"] == stage:
            return
        time.sleep(0.05)
    raise AssertionError(f"project never reached {stage}")


def test_two_humans_each_decide_their_own_gates(co):
    client = TestClient(create_app(co, {"ceo": "ceo-token", "cto": "cto-token"}))
    ceo, cto = {"X-Token": "ceo-token"}, {"X-Token": "cto-token"}

    assert client.get("/api/state").status_code == 401
    assert client.get("/").status_code == 200
    assert client.get("/api/state", headers=cto).json()["you"]["role"] == "cto"

    assert client.post("/api/projects", json={"name": "Greeter", "brief": "A tiny library."}, headers=ceo).status_code == 200
    wait_for(co, "prd_approval")
    approval = client.get("/api/state", headers=ceo).json()["approvals"][0]
    assert client.post(f"/api/approvals/{approval['id']}", json={"decision": "approved"}, headers=cto).status_code == 403
    assert client.post(f"/api/approvals/{approval['id']}", json={"decision": "approved"}, headers=ceo).status_code == 200
    wait_for(co, "architecture_approval")


def test_cto_manages_only_their_departments(co):
    client = TestClient(create_app(co, {"ceo": "ceo-token", "cto": "cto-token"}))
    cto = {"X-Token": "cto-token"}
    pm, engineer = co.org.agent("Ram"), co.org.agent("Hari")
    assert client.post(f"/api/agents/{pm['id']}/fire", json={"reason": "x"}, headers=cto).status_code == 403
    assert client.post("/api/agents", json={"role": "product_manager"}, headers=cto).status_code == 403
    new = client.post(f"/api/agents/{engineer['id']}/fire", json={"reason": "x"}, headers=cto).json()
    assert new["generation"] == 2
    assert client.post(f"/api/agents/{engineer['id']}/rehire", json={}, headers=cto).json()["status"] == "active"
