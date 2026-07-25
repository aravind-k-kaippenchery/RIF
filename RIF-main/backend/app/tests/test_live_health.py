from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _item(key: str, available: bool = True):
    return {
        "key": key,
        "name": key,
        "detail": "test",
        "available": available,
        "state": "ready" if available else "unavailable",
        "message": "test",
        "latency_ms": 1,
        "kind": "runtime",
        "metadata": {},
    }


def test_live_health_returns_fresh_service_snapshot():
    with (
        patch("app.api.routes.live_health._probe_fastapi", return_value=_item("fastapi")),
        patch("app.api.routes.live_health._probe_postgres", return_value=_item("postgres")),
        patch("app.api.routes.live_health._probe_ollama", return_value=_item("ollama")),
        patch("app.api.routes.live_health._probe_chromadb", return_value=_item("chromadb")),
        patch("app.api.routes.live_health._probe_langgraph", return_value=_item("langgraph")),
        patch("app.api.routes.live_health._probe_mcp", return_value=_item("mcp")),
    ):
        response = client.get("/api/system/live-health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["data"]["live"] is True
    assert body["data"]["overall_ready"] is True
    assert body["data"]["poll_after_seconds"] == 5
    assert len(body["data"]["services"]) == 6


def test_live_health_marks_overall_not_ready_when_one_probe_fails():
    with (
        patch("app.api.routes.live_health._probe_fastapi", return_value=_item("fastapi")),
        patch("app.api.routes.live_health._probe_postgres", return_value=_item("postgres")),
        patch("app.api.routes.live_health._probe_ollama", return_value=_item("ollama", False)),
        patch("app.api.routes.live_health._probe_chromadb", return_value=_item("chromadb")),
        patch("app.api.routes.live_health._probe_langgraph", return_value=_item("langgraph")),
        patch("app.api.routes.live_health._probe_mcp", return_value=_item("mcp")),
    ):
        response = client.get("/api/system/live-health")

    assert response.status_code == 200
    assert response.json()["data"]["overall_ready"] is False
