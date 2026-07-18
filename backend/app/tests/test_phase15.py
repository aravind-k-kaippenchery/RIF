"""Phase 15 deterministic tests for benchmarking and final safety evaluation."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.agents.orchestrator import AgentRunResult, agent_orchestrator
from app.core.constants import AgentRoute, ResponseStatus
from app.main import create_app
from app.schemas.phase15 import BenchmarkScenario
from app.services.benchmark_service import BenchmarkMetric, BenchmarkService, ScenarioOutcome

client = TestClient(create_app())


def test_phase_fifteen_agent_status_exposes_benchmark_capability():
    status = agent_orchestrator.status()
    assert status["phase"] >= 15
    assert status["benchmark_evaluation_available"] is True
    assert status["benchmark_runs_persisted"] is True
    assert status["raw_sql_write_tool_exposed"] is False


def test_benchmark_status_is_available_without_revealing_metrics():
    response = client.get("/api/benchmarks/status")
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["phase"] == 15
    assert body["data"]["business_write_benchmarking_allowed"] is False
    assert body["data"]["raw_sql_accepted"] is False


def test_benchmark_scenarios_do_not_authorize_business_writes():
    response = client.get("/api/benchmarks/scenarios")
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["scenario_count"] >= 10
    assert body["data"]["business_write_benchmarking_allowed"] is False
    assert any(item["scenario"] == "safe_quality_suite" for item in body["data"]["scenarios"])


def test_benchmark_run_requires_admin_role():
    response = client.post("/api/benchmarks/run", json={"scenario": "security_guardrails"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "insufficient_role"


def test_security_guardrail_benchmark_blocks_all_controlled_attempts():
    outcome = BenchmarkService()._security_guardrails(MagicMock())
    assert outcome.status == "success"
    metric = next(item for item in outcome.metrics if item.metric_type == "security_block_rate")
    assert metric.metric_value == 1.0
    assert outcome.details["raw_sql_executed"] is False


def test_supported_scenarios_mark_full_evaluation_as_non_business_write():
    scenarios = BenchmarkService.supported_scenarios()
    full = next(item for item in scenarios if item["scenario"] == "full_evaluation")
    assert full["uses_local_llm"] is True
    assert full["performs_business_writes"] is False


def test_agent_structured_benchmark_measures_route_and_grounding_without_write():
    result = AgentRunResult(
        route=AgentRoute.STRUCTURED_READ,
        status=ResponseStatus.SUCCESS,
        answer="Found 3 matching records in the current database.",
        data={"row_count": 3},
        sources=[SimpleNamespace(source_type="database", reference="employees")],
        generated_sql="SELECT * FROM employees LIMIT 100",
        pending_action_id=None,
    )
    with patch("app.services.benchmark_service.agent_orchestrator.run", return_value=result):
        outcome = BenchmarkService()._agent_route(
            MagicMock(),
            scenario=BenchmarkScenario.AGENT_STRUCTURED_READ,
            question="Show workers from Bangalore",
            expected_route=AgentRoute.STRUCTURED_READ,
            expected_success=ResponseStatus.SUCCESS,
        )
    assert outcome.status == "success"
    assert outcome.details["business_write_executed"] is False
    assert any(metric.metric_type == "route_accuracy_rate" and metric.metric_value == 1.0 for metric in outcome.metrics)


def test_benchmark_run_persists_metric_rows_and_returns_batch_metadata():
    outcome = ScenarioOutcome(
        test_name="security_guardrails",
        route="system",
        status="success",
        summary="ok",
        metrics=[BenchmarkMetric("security_block_rate", 1.0, "ratio", "success", {})],
        details={},
    )
    service = BenchmarkService()
    db = MagicMock()
    with patch.object(service, "_run_one", return_value=outcome), patch.object(service, "_persist_outcome", return_value=1):
        result = service.run(db, scenario=BenchmarkScenario.SECURITY_GUARDRAILS, repeats=1)
    assert result["requested_scenario"] == "security_guardrails"
    assert result["stored_metric_run_count"] == 1
    assert result["business_write_executed"] is False


def test_benchmark_summary_separates_metric_types_and_calculates_percentiles():
    runs = [
        SimpleNamespace(id="1", test_name="a", route="structured_read", metric_type="structured_read_latency_ms", metric_value=10.0, metric_unit="ms", status="success", details={}, started_at=None, completed_at=None),
        SimpleNamespace(id="2", test_name="a", route="structured_read", metric_type="structured_read_latency_ms", metric_value=30.0, metric_unit="ms", status="success", details={}, started_at=None, completed_at=None),
    ]
    scalar_result = MagicMock()
    scalar_result.all.return_value = runs
    db = MagicMock()
    db.scalars.return_value = scalar_result
    result = BenchmarkService().summary(db, limit=10)
    metric = result["metrics"][0]
    assert metric["metric_type"] == "structured_read_latency_ms"
    assert metric["average"] == 20.0
    assert metric["p50"] == 20.0


def test_resource_sample_never_claims_model_only_memory():
    outcome = BenchmarkService()._resource_sample(MagicMock())
    assert outcome.status == "success"
    assert outcome.details["model_only_memory_estimated"] is False
    assert any(metric.metric_type == "backend_process_rss_mib" for metric in outcome.metrics)


def test_mcp_status_marks_benchmarking_as_controlled_boundary_reuse():
    response = client.get("/api/mcp/status")
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["phase"] >= 15
    assert body["data"]["benchmarking_reuses_controlled_mcp_tools"] is True
    assert body["data"]["raw_sql_write_tool_exposed"] is False
