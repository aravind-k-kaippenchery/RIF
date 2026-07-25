"""Phase 14 audit hardening, rollback, and error-contract tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app.agents.orchestrator import agent_orchestrator
from app.core.constants import ResponseStatus, UserRole
from app.main import create_app
from app.services.audit_rollback_service import (
    AuditRollbackError,
    RollbackConfirmationResult,
    RollbackPreviewResult,
    _serialize_snapshot,
    action_log_to_dict,
    audit_rollback_service,
)

client = TestClient(create_app())


def test_phase_fourteen_agent_status_exposes_rollback_controls():
    status = agent_orchestrator.status()
    assert status["phase"] >= 14
    assert status["audit_rollback_available"] is True
    assert status["rollback_confirmation_required"] is True
    assert status["rollback_supported_original_actions"] == ["update", "delete"]
    assert status["raw_sql_write_tool_exposed"] is False


def test_audit_status_requires_admin_role():
    response = client.get("/api/audit/status")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "insufficient_role"


def test_admin_audit_status_exposes_no_raw_sql_policy():
    response = client.get("/api/audit/status", headers={"X-User-Role": "admin"})
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["phase"] >= 14
    assert body["data"]["raw_sql_accepted"] is False
    assert body["data"]["rollback_insert_compensation_available"] is False


def test_rollback_rejects_insert_compensation_before_database_access():
    action = SimpleNamespace(action_type="insert", status="success", target_table="employees", id=55)
    try:
        audit_rollback_service._build_plan(MagicMock(), action, [])
    except AuditRollbackError as exc:
        assert exc.status == ResponseStatus.VALIDATION_FAILED
        assert exc.code == "rollback_not_supported_for_action_type"
    else:  # pragma: no cover
        raise AssertionError("INSERT rollback should be intentionally deferred")


def test_action_and_snapshot_serializers_expose_safe_audit_contracts():
    snapshot = SimpleNamespace(
        id=uuid4(),
        action_log_id=12,
        table_name="vendors",
        record_id="7",
        snapshot_type="before",
        snapshot_data={"id": 7, "city": "Bangalore"},
        created_at=None,
    )
    encoded_snapshot = _serialize_snapshot(snapshot)
    assert encoded_snapshot["table_name"] == "vendors"
    assert encoded_snapshot["snapshot_data"]["city"] == "Bangalore"

    action = SimpleNamespace(
        id=12,
        request_id="req-14",
        session_id=None,
        pending_action_id=None,
        actor_role="admin",
        action_type="update",
        target_table="vendors",
        affected_record_ids={"affected_row_count": 1},
        generated_sql="UPDATE vendors ...",
        confirmation_status="confirmed",
        status="success",
        error_message=None,
        created_at=None,
    )
    encoded_action = action_log_to_dict(action, snapshot_count=2)
    assert encoded_action["snapshot_count"] == 2
    assert encoded_action["action_type"] == "update"


def test_rollback_preview_endpoint_creates_admin_session_when_missing():
    change_id = 91
    session_id = uuid4()
    pending_id = uuid4()
    preview_result = RollbackPreviewResult(
        pending_action={"pending_action_id": str(pending_id), "session_id": str(session_id)},
        original_action={"action_log_id": change_id, "action_type": "update"},
        rollback_plan={"original_action_log_id": change_id, "target_table": "vendors", "operation_count": 1},
        created_session_id=None,
    )
    with patch("app.api.routes.audit.create_session", return_value=SimpleNamespace(id=session_id)), \
         patch("app.api.routes.audit.audit_rollback_service.preview_rollback", return_value=preview_result) as preview:
        response = client.post(
            f"/api/audit/actions/{change_id}/rollback/preview",
            headers={"X-User-Role": "admin"},
            json={},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "pending_confirmation"
    assert body["pending_action_id"] == str(pending_id)
    assert body["data"]["session_created_for_rollback"] is True
    assert preview.call_args.kwargs["actor_role"] == UserRole.ADMIN


def test_rollback_confirm_requires_explicit_boolean_confirmation():
    pending_id = uuid4()
    response = client.post(
        f"/api/audit/rollback-actions/{pending_id}/confirm",
        headers={"X-User-Role": "admin"},
        json={"session_id": str(uuid4()), "confirmed": False},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "explicit_confirmation_required"


def test_rollback_confirm_endpoint_returns_idempotent_safe_result():
    pending_id = uuid4()
    session_id = uuid4()
    result = RollbackConfirmationResult(
        pending_action={"pending_action_id": str(pending_id), "status": "confirmed"},
        rollback_action_log_id=401,
        original_action_log_id=99,
        restored_record_count=0,
        before_snapshot_count=0,
        after_snapshot_count=0,
        idempotent=True,
    )
    with patch("app.api.routes.audit.audit_rollback_service.confirm_rollback", return_value=result):
        response = client.post(
            f"/api/audit/rollback-actions/{pending_id}/confirm",
            headers={"X-User-Role": "admin"},
            json={"session_id": str(session_id), "confirmed": True},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["idempotent"] is True
    assert body["data"]["raw_sql_accepted"] is False


def test_mcp_status_announces_phase_fourteen_control_plane_without_new_raw_sql_tool():
    response = client.get("/api/mcp/status")
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["phase"] >= 14
    assert body["data"]["audit_rollback_control_plane_available"] is True
    assert body["data"]["raw_sql_write_tool_exposed"] is False
