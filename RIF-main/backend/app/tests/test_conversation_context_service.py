"""Deterministic tests for action-aware conversation memory and ambiguity protection."""

from __future__ import annotations

from app.core.constants import AgentRoute, ResponseStatus
from app.services.conversation_context_service import (
    conversation_reference_from_result,
    resolve_followup_from_state,
)


def _records(count: int) -> list[dict[str, str]]:
    return [
        {
            "employee_code": f"EMP-MEM-{index:03d}",
            "first_name": f"Worker{index}",
            "last_name": "Demo",
            "email": f"worker{index}@example.test",
        }
        for index in range(1, count + 1)
    ]


def _action(*, status: str = "pending", count: int = 10, timestamp: str = "2026-07-19T10:00:00+00:00"):
    records = _records(count)
    return {
        "pending_action_id": "08ef1073-b9cf-40ad-a5bb-08ed91e8a961",
        "target_table": "employees",
        "action_type": "bulk_insert",
        "status": status,
        "record_count": count,
        "records": records,
        "record_ids": [{"employee_code": row["employee_code"]} for row in records],
        "event_timestamp": timestamp,
    }


def test_can_i_see_it_returns_exact_pending_ten_record_batch():
    action = _action(status="pending", count=10)
    state = {
        "last_action": action,
        "last_query": {
            "route": "crud_write",
            "status": "pending_confirmation",
            "created_at": "2026-07-19T10:00:01+00:00",
            "conversation_reference": {"pending_action_id": action["pending_action_id"]},
        },
    }

    resolution = resolve_followup_from_state("Can I see it?", state)

    assert resolution.handled is True
    assert resolution.route == AgentRoute.CRUD_WRITE
    assert resolution.status == ResponseStatus.PENDING_CONFIRMATION
    assert resolution.pending_action_id == action["pending_action_id"]
    assert resolution.data is not None
    assert resolution.data["record_count"] == 10
    assert len(resolution.data["rows"]) == 10
    assert "not been added" in resolution.answer


def test_show_them_returns_confirmed_records_not_an_old_location_query():
    action = _action(status="confirmed", count=10, timestamp="2026-07-19T10:05:00+00:00")
    state = {
        "last_action": action,
        "last_query": {
            "route": "crud_write",
            "status": "pending_confirmation",
            "created_at": "2026-07-19T10:00:01+00:00",
            "conversation_reference": {"pending_action_id": action["pending_action_id"]},
        },
    }

    resolution = resolve_followup_from_state("show them", state)

    assert resolution.status == ResponseStatus.SUCCESS
    assert resolution.data is not None
    assert len(resolution.data["rows"]) == 10
    assert all(row["employee_code"].startswith("EMP-MEM-") for row in resolution.data["rows"])
    assert "confirmed action" in resolution.answer


def test_show_me_is_resolved_without_calling_ollama():
    action = _action(status="confirmed", count=1)
    state = {
        "last_action": action,
        "last_query": {
            "route": "crud_write",
            "status": "success",
            "created_at": "2026-07-19T10:00:01+00:00",
            "conversation_reference": {"pending_action_id": action["pending_action_id"]},
        },
    }

    resolution = resolve_followup_from_state("show me", state)

    assert resolution.handled is True
    assert resolution.status == ResponseStatus.SUCCESS
    assert resolution.data is not None
    assert resolution.data["record_count"] == 1
    assert resolution.data["rows"][0]["employee_code"] == "EMP-MEM-001"


def test_failed_older_show_me_attempt_does_not_hide_the_confirmed_batch():
    action = _action(status="confirmed", count=1, timestamp="2026-07-19T10:00:00+00:00")
    state = {
        "last_action": action,
        "last_query": {
            "question": "show me",
            "route": "system",
            "status": "llm_unavailable",
            "answer": "Local Ollama could not complete the generation request.",
            "created_at": "2026-07-19T10:01:00+00:00",
        },
    }

    resolution = resolve_followup_from_state("show me", state)

    assert resolution.handled is True
    assert resolution.status == ResponseStatus.SUCCESS
    assert resolution.data is not None
    assert resolution.data["rows"][0]["employee_code"] == "EMP-MEM-001"


def test_failed_previous_write_does_not_fall_back_to_an_older_batch():
    state = {
        "last_action": _action(status="confirmed", count=5, timestamp="2026-07-19T09:00:00+00:00"),
        "last_query": {
            "question": "Create 10 random employees",
            "route": "crud_write",
            "status": "llm_unavailable",
            "answer": "The previous generation failed safety validation.",
            "created_at": "2026-07-19T10:00:00+00:00",
        },
    }

    resolution = resolve_followup_from_state("Can I see it?", state)

    assert resolution.status == ResponseStatus.INFORMATION_NOT_AVAILABLE
    assert resolution.data is not None
    assert resolution.data["conversation_resolution"] == "previous_request_failed"
    assert resolution.data["rows"] == []
    assert "failed" in resolution.answer


def test_unrelated_newer_read_requires_clarification_instead_of_guessing():
    state = {
        "last_action": _action(status="confirmed", count=10, timestamp="2026-07-19T09:00:00+00:00"),
        "last_query": {
            "question": "Show employees from Bangalore",
            "route": "structured_read",
            "status": "success",
            "created_at": "2026-07-19T10:00:00+00:00",
            "conversation_reference": None,
        },
    }

    resolution = resolve_followup_from_state("Can I see it?", state)

    assert resolution.status == ResponseStatus.CLARIFICATION_REQUIRED
    assert resolution.data is not None
    assert resolution.data["conversation_resolution"] == "multiple_possible_references"
    assert resolution.data["rows"] == []


def test_update_that_one_never_guesses_across_a_batch():
    state = {"last_action": _action(status="confirmed", count=10), "last_query": None}

    resolution = resolve_followup_from_state("update that one", state)

    assert resolution.handled is True
    assert resolution.route == AgentRoute.CRUD_WRITE
    assert resolution.status == ResponseStatus.CLARIFICATION_REQUIRED
    assert "10" in resolution.answer
    assert "Specify the exact record identifier" in resolution.answer


def test_update_that_one_with_one_record_still_requests_field_and_value():
    state = {"last_action": _action(status="confirmed", count=1), "last_query": None}

    resolution = resolve_followup_from_state("update that one", state)

    assert resolution.status == ResponseStatus.CLARIFICATION_REQUIRED
    assert "field and value" in resolution.answer
    assert "EMP-MEM-001" in resolution.answer


def test_vague_show_without_history_asks_for_table_or_record():
    resolution = resolve_followup_from_state("show it", {"last_action": None, "last_query": None})

    assert resolution.handled is True
    assert resolution.status == ResponseStatus.CLARIFICATION_REQUIRED
    assert "name the table or record" in resolution.answer


def test_explicit_prompt_is_not_intercepted():
    resolution = resolve_followup_from_state("Show employees from Bangalore", {})

    assert resolution.handled is False


def test_conversation_reference_links_query_log_to_pending_action():
    reference = conversation_reference_from_result(
        pending_action_id="08ef1073-b9cf-40ad-a5bb-08ed91e8a961",
        route="crud_write",
        status="pending_confirmation",
        data={
            "generated_record_count": 10,
            "preview": {"target_table": "employees", "record_count": 10},
        },
    )

    assert reference == {
        "pending_action_id": "08ef1073-b9cf-40ad-a5bb-08ed91e8a961",
        "target_table": "employees",
        "record_count": 10,
        "route": "crud_write",
        "status": "pending_confirmation",
    }


def test_frontend_query_short_circuits_ollama_for_can_i_see_it():
    from types import SimpleNamespace
    from unittest.mock import patch
    from uuid import uuid4

    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.services.conversation_context_service import ConversationResolution

    session_id = uuid4()
    rows = _records(10)
    resolution = ConversationResolution(
        handled=True,
        route=AgentRoute.CRUD_WRITE,
        status=ResponseStatus.PENDING_CONFIRMATION,
        answer="Here are the 10 employees from your latest pending preview.",
        data={
            "conversation_resolution": "resolved_last_action_records",
            "rows": rows,
            "record_count": 10,
            "resolved_reference": {
                "pending_action_id": "08ef1073-b9cf-40ad-a5bb-08ed91e8a961",
                "target_table": "employees",
                "record_count": 10,
            },
        },
        pending_action_id="08ef1073-b9cf-40ad-a5bb-08ed91e8a961",
    )
    client = TestClient(create_app())

    with patch("app.api.routes.frontend.create_session", return_value=SimpleNamespace(id=session_id)), \
         patch("app.api.routes.frontend.build_memory_context", return_value={"available": True, "event_count": 1, "events": [], "policy": "bounded"}), \
         patch("app.api.routes.frontend.resolve_conversation_followup", return_value=resolution), \
         patch("app.api.routes.frontend.write_agent_memory_event", return_value={"stored": True}), \
         patch("app.api.routes.frontend.agent_orchestrator.run") as run:
        response = client.post("/api/query", json={"question": "Can I see it?"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "pending_confirmation"
    assert body["pending_action_id"] == resolution.pending_action_id
    assert len(body["data"]["rows"]) == 10
    run.assert_not_called()
