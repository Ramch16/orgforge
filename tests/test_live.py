from fastapi.testclient import TestClient

from vittics_builder.server import app_icon_png, create_app
from vittics_builder.telemetry import presence, telemetry
from test_tickets import built


def test_presence_shows_who_is_working(co):
    p = co.pipeline.create_project("Greeter", "A tiny library.")
    co.pipeline.advance(p["id"])
    people = {a["name"]: a for a in presence(co.db)}
    assert len(people) == 22
    assert people["Ram"]["state"] == "working" and "Requirements for Greeter" in people["Ram"]["doing"]
    assert people["Venky"]["state"] == "idle" and people["Venky"]["doing"] == ""


def test_telemetry_counts_recent_work(co):
    built(co)
    t = telemetry(co.db)
    assert t["last_hour"]["calls"] > 20 and t["today"]["cost"] > 0 and t["tickets_done_today"] >= 2
    assert len(t["series"]) == 24 and t["series"][-1]["calls"] > 0


def test_dashboard_is_installable(co):
    client = TestClient(create_app(co, {"ceo": "c", "cto": "t"}))
    m = client.get("/manifest.webmanifest").json()
    assert m["display"] == "standalone" and {i["sizes"] for i in m["icons"]} >= {"192x192", "512x512"}
    png = client.get("/icon-192.png")
    assert png.status_code == 200 and png.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert client.get("/sw.js").status_code == 200 and client.get("/icon-7.png").status_code == 404
    assert '<link rel="manifest"' in client.get("/").text
    state = client.get("/api/state", headers={"X-Token": "c"}).json()
    assert {"presence", "telemetry", "memories"} <= set(state)
    assert app_icon_png(180)[:4] == b"\x89PNG"
