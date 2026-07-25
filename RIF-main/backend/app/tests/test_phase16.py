"""Phase 16 deterministic tests for final demo readiness and release evidence."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.main import create_app
from app.services.demo_readiness_service import DemoReadinessService

client = TestClient(create_app())


def test_phase_sixteen_status_is_public_and_never_accepts_raw_sql():
    with patch("app.services.demo_readiness_service.get_database_health", return_value=SimpleNamespace(connected=True, message="ok")), patch(
        "app.services.demo_readiness_service.llm_service.check_llm_health",
        return_value=SimpleNamespace(
            connected=True,
            model_installed=True,
            message="ok",
            base_url="http://127.0.0.1:11434",
            configured_model="llama3:8b",
            installed_model_names=["llama3:8b"],
        ),
    ), patch("app.services.demo_readiness_service.document_rag_service.status", return_value={"chromadb_available": True}):
        response = client.get("/api/demo/status")
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["phase"] == 16
    assert body["data"]["local_only"] is True
    assert body["data"]["raw_sql_accepted"] is False
    assert body["data"]["business_write_executed"] is False


def test_feature_matrix_covers_every_required_feature_once():
    matrix = DemoReadinessService.feature_matrix()
    assert len(matrix) == 17
    assert [item["feature_id"] for item in matrix] == list(range(1, 18))
    assert any("employee_permissions" in item["evidence"] for item in matrix)


def test_public_feature_endpoint_returns_all_seventeen_features():
    response = client.get("/api/demo/features")
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["feature_count"] == 17
    assert body["data"]["raw_sql_accepted"] is False


def test_demo_scenarios_are_descriptive_and_non_destructive():
    response = client.get("/api/demo/scenarios")
    assert response.status_code == 200
    scenarios = response.json()["data"]["scenarios"]
    assert len(scenarios) >= 8
    assert all(item["business_write_executed"] is False for item in scenarios)
    assert all(item["raw_sql_accepted"] is False for item in scenarios)
    assert any(item["scenario_id"] == "hybrid_evidence" for item in scenarios)


def test_admin_readiness_requires_admin_role_before_operational_evidence_is_returned():
    response = client.get("/api/demo/readiness")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "insufficient_role"


def test_admin_report_requires_admin_role():
    response = client.get("/api/demo/report")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "insufficient_role"


def test_readiness_adds_latest_persisted_metric_highlights_without_raw_sql():
    service = DemoReadinessService()
    benchmark_rows = [
        SimpleNamespace(metric_type="security_block_rate", metric_value=1.0),
        SimpleNamespace(metric_type="mcp_tool_success_rate", metric_value=1.0),
        SimpleNamespace(metric_type="retrieval_relevance_rate", metric_value=1.0),
        SimpleNamespace(metric_type="hybrid_evidence_accuracy_rate", metric_value=1.0),
    ]
    db = MagicMock()
    db.scalars.return_value.all.return_value = benchmark_rows
    with patch.object(service, "public_status", return_value={"full_demo_ready": True, "checks": []}):
        result = service.readiness(db)
    assert result["benchmark_evidence"]["benchmark_run_count"] == 4
    assert result["benchmark_evidence"]["latest_security_block_rate"] == 1.0
    assert result["release_gate"]["raw_sql_write_tool_exposed"] is False


def test_smoke_check_never_runs_a_model_or_business_write():
    service = DemoReadinessService()
    with patch.object(service, "readiness", return_value={"checks": [{"component": "db", "ready": True, "status": "ready", "detail": "ok"}], "benchmark_evidence_available": True}):
        result = service.smoke(MagicMock())
    assert result["overall_status"] == "success"
    assert result["business_write_executed"] is False
    assert result["raw_sql_accepted"] is False
    assert result["model_generation_triggered"] is False


def test_final_report_states_production_limitations_and_safety_boundary():
    service = DemoReadinessService()
    with patch.object(service, "readiness", return_value={"full_demo_ready": True, "benchmark_evidence": {}, "checks": [], "benchmark_evidence_available": True}):
        report = service.final_report(MagicMock())
    assert report["implementation_state"] == "final_poc_demo_ready"
    assert any("authentication" in item.lower() for item in report["known_limitations"])
    assert "No Phase 16 report" in report["safety_statement"]
