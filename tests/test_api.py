import pytest
from fastapi.testclient import TestClient

from conftest import HAS_NODE


@pytest.fixture()
def client(data_dir):
    from alertwhy.api import app
    with TestClient(app) as c:
        yield c


def test_health_and_alerts(client):
    h = client.get("/api/health").json()
    assert h["status"] == "ok" and h["federation_kind"] == "local"
    assert len(client.get("/api/alerts").json()) == 19


def test_investigate_and_errors(client):
    r = client.post("/api/alerts/A-1003/investigate?memory=false").json()
    assert r["verdict"] == "CLOSE" and r["rounds"] >= 1 and r["trace"]
    assert client.post("/api/alerts/NOPE/investigate").status_code == 404
    assert client.post("/api/alerts/A-1001/feedback", json={"decision": "MAYBE"}).status_code in (400, 404)


def test_evaluate_endpoint(client):
    r = client.post("/api/evaluate").json()
    assert r["correct"] == r["total"] == 19 and r["missed_escalations"] == []
    assert client.get("/api/usage").json()["investigations"] >= 19


def test_status_reports_fallback_honestly(client):
    s = client.get("/api/status").json()
    assert s["zetaris"]["live"] is False and "NOT Zetaris" in s["zetaris"]["backend"]
    assert s["meterless"]["providers"] == [] and s["meterless"]["live"] is False


@pytest.mark.skipif(not HAS_NODE, reason="needs node")
def test_memory_endpoints_roundtrip(client):
    client.post("/api/memory/reset")
    assert client.post("/api/memory/seed").json()["created"]
    assert client.get("/api/memory").json()["enabled"]
    r = client.post("/api/alerts/A-1009/investigate").json()
    assert r["verdict"] == "ESCALATE" and r["memory"]["recalled"]
    fb = client.post("/api/alerts/A-1009/feedback", json={"decision": "escalate", "note": "ok"}).json()
    assert fb["agrees_with_agent"] is True
    client.post("/api/memory/reset")
