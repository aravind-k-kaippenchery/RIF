"""Phase 13 deterministic tests for memory, frontend APIs, and admin schema execution."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app.agents.orchestrator import AgentRunResult, agent_orchestrator
from app.core.constants import AgentRoute, ResponseStatus, UserRole
from app.main import create_app
from app.mcp.client import LocalMCPClient
from app.services.session_memory_service import build_memory_context
from app.services.sql_validation import preview_admin_schema_change

client = TestClient(create_app())


def test_phase_thirteen_status_exposes_memory_and_restricted_admin_execution():
    status = agent_orchestrator.status()
    assert status["phase"] >= 13
    assert status["short_term_memory_available"] is True
    assert status["admin_schema_execution_available"] is True
    assert status["raw_sql_write_tool_exposed"] is False


def test_create_table_preview_records_columns_for_future_admin_audit():
    preview = preview_admin_schema_change("CREATE TABLE phase13_demo_items (code VARCHAR(32), label TEXT)")
    assert preview.is_valid is True
    assert preview.statement_type == "CREATE_TABLE_PREVIEW"
    assert preview.columns == ["phase13_demo_items.code", "phase13_demo_items.label"]


def test_mcp_schema_apply_requires_confirmation_without_accepting_raw_sql():
    result = LocalMCPClient().call_tool(
        "apply_admin_schema_change",
        {"schema_change_id": str(uuid4()), "confirmed": False, "user_role": "admin"},
    )["result"]
    assert result["executed"] is False
    assert result["error_code"] == "explicit_confirmation_required"
    assert result["raw_sql_accepted"] is False


def test_frontend_tables_endpoint_reuses_mcp_boundary():
    fake = {"listed": True, "table_count": 1, "tables": [{"table_name": "employees"}], "raw_sql_accepted": False}
    with patch("app.api.routes.frontend.LocalMCPClient.call_tool", return_value={"ok": True, "result": fake}):
        response = client.get("/api/tables")
    assert response.status_code == 200
    assert response.json()["data"]["raw_sql_accepted"] is False


def test_frontend_operational_logs_require_admin_header():
    response = client.get("/api/logs")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "insufficient_role"


def test_session_history_endpoint_returns_controlled_history_contract():
    session_id = uuid4()
    fake = {"session": {"session_id": str(session_id), "status": "active"}, "event_count": 1, "events": [{"event_type": "query"}], "memory_scope": "short_term_session_history"}
    with patch("app.api.routes.sessions.get_session_history", return_value=fake):
        response = client.get(f"/api/sessions/{session_id}/history")
    assert response.status_code == 200
    assert response.json()["data"]["memory_scope"] == "short_term_session_history"


def test_build_memory_context_excludes_hidden_reasoning_and_rows():
    session_id = uuid4()
    fake_history = {
        "events": [
            {
                "event_type": "query",
                "user_prompt": "Show workers from Bangalore",
                "route": "structured_read",
                "status": "success",
                "generated_sql": "SELECT ...",
                "source_references": {"tables": ["employees"]},
                "hidden_reasoning": "not included",
                "rows": [{"email": "not included"}],
            }
        ]
    }
    with patch("app.services.session_memory_service.get_session_history", return_value=fake_history):
        context = build_memory_context(MagicMock(), session_id=session_id)
    assert context["available"] is True
    assert context["events"][0]["prior_question"] == "Show workers from Bangalore"
    assert "hidden_reasoning" not in context["events"][0]
    assert "rows" not in context["events"][0]


def test_frontend_query_creates_session_and_passes_memory_to_agent():
    session_id = uuid4()
    fake_session = SimpleNamespace(id=session_id)
    result = AgentRunResult(
        route=AgentRoute.STRUCTURED_READ,
        status=ResponseStatus.SUCCESS,
        answer="Found 1 matching record in the current database.",
        data={"row_count": 1},
        sources=[],
        generated_sql="SELECT e.id FROM employees AS e LIMIT 1",
        pending_action_id=None,
    )
    with patch("app.api.routes.frontend.create_session", return_value=fake_session), \
         patch("app.api.routes.frontend.build_memory_context", return_value={"available": False, "event_count": 0, "events": [], "policy": "bounded"}), \
         patch("app.api.routes.frontend.agent_orchestrator.run", return_value=result) as run, \
         patch("app.api.routes.frontend.write_agent_memory_event", return_value={"stored": True, "storage": "query_logs"}):
        response = client.post("/api/query", json={"question": "Show employees"})
    assert response.status_code == 200
    assert response.json()["session_id"] == str(session_id)
    assert run.call_args.kwargs["session_id"] == str(session_id)
    assert run.call_args.kwargs["memory_context"]["available"] is False


def test_admin_schema_confirm_endpoint_forwards_persisted_id_not_raw_sql():
    change_id = uuid4()
    tool_result = {
        "executed": True,
        "idempotent": False,
        "schema_change": {"schema_change_id": str(change_id), "status": "executed"},
        "raw_sql_accepted": False,
    }
    with patch("app.api.routes.admin_schema.LocalMCPClient.call_tool", return_value={"ok": True, "result": tool_result}) as call:
        response = client.post(
            f"/api/admin/schema-changes/{change_id}/confirm",
            headers={"X-User-Role": "admin"},
            json={"confirmed": True},
        )
    assert response.status_code == 200
    assert call.call_args.args[0] == "apply_admin_schema_change"
    arguments = call.call_args.args[1]
    assert arguments["schema_change_id"] == str(change_id)
    assert "sql" not in arguments


def test_mcp_status_marks_restricted_admin_execution_available():
    response = client.get("/api/mcp/status")
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["phase"] >= 13
    assert body["data"]["admin_schema_execution_available"] is True
    assert body["data"]["raw_sql_write_tool_exposed"] is False
