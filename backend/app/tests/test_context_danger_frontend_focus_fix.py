from app.agents.router import classify_question
from app.core.constants import AgentRoute
from app.services import demo_question_service
from app.services.demo_question_service import (
    _apply_context_status_filter,
    _latest_table_context_from_memory,
    detect_demo_question,
)
from app.services.request_clarification_service import analyze_request_clarity
from app.services.conversation_context_service import resolve_followup_from_state


def test_second_followup_active_ones_is_context_demo_question():
    request = detect_demo_question("Show only the active ones.")
    assert request.handled is True
    assert request.kind == "context_followup_list"
    assert request.table == "__memory__"
    assert request.filters["status_value"] == "active"


def test_reflected_read_context_is_recovered_without_generated_sql():
    context = {
        "available": True,
        "events": [
            {
                "prior_question": "Show employees from Bangalore.",
                "route": "structured_read",
                "status": "success",
                "generated_sql": None,
                "conversation_reference": {
                    "reference_type": "table_result",
                    "target_table": "employees",
                    "filters": {"city": "bangalore"},
                    "record_count": 19,
                },
            }
        ],
    }

    recovered = _latest_table_context_from_memory(context)
    assert recovered is not None
    assert recovered["table"] == "employees"
    assert recovered["filters"] == {"city": "bangalore"}
    assert recovered["reference_type"] == "table_result"


def test_active_followup_preserves_old_filter_and_uses_live_status_column(monkeypatch):
    monkeypatch.setattr(
        demo_question_service,
        "get_runtime_columns",
        lambda _table: ["employee_id", "city", "employment_status"],
    )
    filters = _apply_context_status_filter(
        "employees",
        {"city": "bangalore"},
        "active",
    )
    assert filters == {"city": "bangalore", "employment_status": "active"}


def test_ambiguous_live_status_columns_are_not_guessed(monkeypatch):
    monkeypatch.setattr(
        demo_question_service,
        "get_runtime_columns",
        lambda _table: ["order_status", "payment_status"],
    )
    assert _apply_context_status_filter("orders", {}, "active") is None


def test_status_followup_uses_arbitrary_reflected_table_column(monkeypatch):
    monkeypatch.setattr(
        demo_question_service,
        "get_runtime_columns",
        lambda _table: ["failed_order_id", "order_status"],
    )
    assert _apply_context_status_filter("failed_orders", {"city": "kochi"}, "active") == {
        "city": "kochi",
        "order_status": "active",
    }


def test_legacy_question_text_is_not_used_as_a_hardcoded_table_fallback():
    context = {
        "available": True,
        "events": [
            {
                "prior_question": "Show employees from Bangalore.",
                "route": "structured_read",
                "status": "success",
                "generated_sql": None,
                "conversation_reference": None,
            }
        ],
    }
    assert _latest_table_context_from_memory(context) is None


def test_first_three_followup_is_context_demo_question():
    request = detect_demo_question("Show the first three.")
    assert request.handled is True
    assert request.kind == "context_followup_list"
    assert request.table == "__memory__"
    assert request.limit == 3


def test_dangerous_delete_all_is_blocked_before_llm():
    decision = analyze_request_clarity("Delete all employees.")
    assert decision.needs_clarification is True
    assert decision.code == "dangerous_delete_blocked"
    assert "Dangerous action blocked" in decision.message


def test_drop_table_is_blocked_before_routing_to_read():
    decision = analyze_request_clarity("Run DROP TABLE employees.")
    assert decision.needs_clarification is True
    assert decision.code == "dangerous_sql_blocked"
    assert decision.detected_intent == "dangerous_action"


def test_explicit_document_question_prefers_document_rag_over_hybrid():
    decision = classify_question("What does the vendor compliance document say?")
    assert decision.route == AgentRoute.DOCUMENT_RAG


def test_pending_action_followup_is_resolved_from_state():
    state = {
        "last_action": {
            "pending_action_id": "00000000-0000-0000-0000-000000000001",
            "target_table": "employees",
            "action_type": "bulk_insert",
            "status": "pending",
            "record_count": 1,
            "records": [{"id": 1, "first_name": "Demo"}],
            "record_ids": [{"id": 1}],
            "event_timestamp": "2026-01-01T10:00:00+00:00",
        },
        "last_query": None,
    }
    resolution = resolve_followup_from_state("Show me the pending action.", state)
    assert resolution.handled is True
    assert resolution.status.value == "pending_confirmation"
    assert resolution.pending_action_id == "00000000-0000-0000-0000-000000000001"
