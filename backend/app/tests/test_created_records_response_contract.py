"""Regression contract for complete preview/confirmation evidence returned to the frontend."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi import Request

from app.api.routes import crud as crud_routes
from app.core.constants import UserRole
from app.schemas.phase7 import PendingActionMutationRequest
from app.services.crud_write_service import WriteConfirmationResult


def _request() -> Request:
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    request.state.request_id = "created-record-contract"
    request.state.session_id = None
    return request


def test_confirmation_response_returns_exact_created_records_and_counts():
    pending_id = uuid4()
    session_id = uuid4()
    records = [
        {"id": 101, "vendor_code": "VEN-101", "vendor_name": "Alpha Supplies"},
        {"id": 102, "vendor_code": "VEN-102", "vendor_name": "Beta Supplies"},
    ]
    result = WriteConfirmationResult(
        pending_action={"pending_action_id": str(pending_id), "target_table": "vendors", "action_type": "bulk_insert"},
        action_log_id=77,
        affected_row_count=2,
        before_snapshot_count=0,
        after_snapshot_count=2,
        affected_record_ids=[{"id": 101, "vendor_code": "VEN-101"}, {"id": 102, "vendor_code": "VEN-102"}],
        affected_records=records,
        expected_row_count=2,
        count_verified=True,
        idempotent=False,
    )

    with patch.object(crud_routes.crud_write_service, "confirm_action", return_value=result):
        response = crud_routes.confirm_write(
            str(pending_id),
            PendingActionMutationRequest(session_id=session_id),
            _request(),
            MagicMock(),
            UserRole.NORMAL_USER,
        )

    payload = json.loads(response.body)
    assert payload["status"] == "success"
    assert payload["data"]["affected_row_count"] == 2
    assert payload["data"]["confirmed_record_count"] == 2
    assert payload["data"]["expected_row_count"] == 2
    assert payload["data"]["count_verified"] is True
    assert payload["data"]["rows"] == records
    assert payload["data"]["affected_records"] == records
    assert "Exactly 2 records" in payload["answer"]
