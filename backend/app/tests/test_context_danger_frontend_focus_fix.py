from app.agents.router import classify_question
from app.core.constants import AgentRoute
from app.services.demo_question_service import detect_demo_question
from app.services.request_clarification_service import analyze_request_clarity
from app.services.conversation_context_service import resolve_followup_from_state


def test_second_followup_active_ones_is_context_demo_question():
    request = detect_demo_question("Show only the active ones.")
    assert request.handled is True
    assert request.kind == "context_followup_list"
    assert request.table == "__memory__"
    assert request.filters["status_value"] == "active"


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
