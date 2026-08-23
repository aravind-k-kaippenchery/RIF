from unittest.mock import patch

import pytest

from app.agents.orchestrator import agent_orchestrator
from app.core.constants import AgentRoute, ResponseStatus, UserRole
from app.services import request_clarification_service as clarification_service
from app.services.request_clarification_service import (
    analyze_request_clarity,
    assert_generated_tables_match,
    resolve_business_tables,
)


def test_new_random_table_request_requires_schema_and_never_defaults_to_employees():
    decision = analyze_request_clarity("create 1 random table and add 5 synthetic data")
    assert decision.needs_clarification is True
    assert decision.code == "schema_definition_required"
    assert decision.resolved_tables == ()
    assert "table name" in decision.message.lower()
    assert "column" in decision.message.lower()


def test_synthetic_data_without_target_table_requires_clarification():
    decision = analyze_request_clarity("add 5 synthetic data")
    assert decision.needs_clarification is True
    assert decision.code == "synthetic_target_table_required"
    assert "which existing business table" in decision.message.lower()


def test_clear_synthetic_target_is_allowed():
    decision = analyze_request_clarity("Create 10 random vendors")
    assert decision.needs_clarification is False
    assert decision.resolved_tables == ("vendors",)


def test_clear_structured_read_is_allowed():
    decision = analyze_request_clarity("show workers from Chennai")
    assert decision.needs_clarification is False
    assert decision.resolved_tables == ("employees",)


def test_bare_show_requires_context_instead_of_guessing():
    decision = analyze_request_clarity("show")
    assert decision.needs_clarification is True
    assert decision.code == "read_target_required"


def test_underspecified_insert_update_delete_are_blocked():
    assert analyze_request_clarity("add an employee").code == "insert_values_required"
    assert analyze_request_clarity("update employee EMP-101").code == "update_details_required"
    assert analyze_request_clarity("delete employee").code == "delete_record_selector_required"


def test_explicit_update_and_delete_are_allowed():
    assert analyze_request_clarity("change city of employee EMP-101 to Kochi").needs_clarification is False
    assert analyze_request_clarity("delete employee EMP-101").needs_clarification is False


def test_table_resolution_never_injects_employees():
    assert resolve_business_tables("add 5 synthetic data") == ()
    assert resolve_business_tables("show customers in Kochi") == ("customers",)


def test_newly_reflected_table_is_accepted_for_synthetic_generation(monkeypatch):
    monkeypatch.setattr(
        clarification_service,
        "get_public_table_names",
        lambda: ["employees", "demo_orders", "query_logs"],
    )
    decision = analyze_request_clarity("Generate 5 synthetic rows for demo_orders table")
    assert decision.needs_clarification is False
    assert decision.detected_intent == "synthetic_generation"
    assert decision.resolved_tables == ("demo_orders",)

    short_alias_decision = analyze_request_clarity("Add 5 synthetic records to orders table")
    assert short_alias_decision.needs_clarification is False
    assert short_alias_decision.resolved_tables == ("demo_orders",)


def test_dynamic_table_is_shown_in_available_synthetic_targets(monkeypatch):
    monkeypatch.setattr(
        clarification_service,
        "get_public_table_names",
        lambda: ["employees", "demo_orders", "query_logs"],
    )
    decision = analyze_request_clarity("Generate 5 synthetic rows")
    assert decision.needs_clarification is True
    assert "demo_orders" in decision.message
    assert "query_logs" not in decision.message


def test_generated_table_alignment_rejects_wrong_model_target():
    with pytest.raises(ValueError):
        assert_generated_tables_match(expected_tables=["vendors"], generated_tables=["employees"])
    assert_generated_tables_match(expected_tables=["vendors"], generated_tables=["vendors"])


def test_orchestrator_returns_normal_clarification_without_calling_ollama():
    with patch("app.agents.orchestrator.llm_service.generate_sql") as generate_sql:
        result = agent_orchestrator.run(
            question="create 1 random table and add 5 synthetic data",
            db=None,  # clarification route does not access the database
            request_id=None,
            session_id=None,
            user_role=UserRole.NORMAL_USER,
            top_k=None,
            memory_context=None,
        )

    generate_sql.assert_not_called()
    assert result.route == AgentRoute.SYSTEM
    assert result.status == ResponseStatus.CLARIFICATION_REQUIRED
    assert result.generated_sql is None
    assert result.pending_action_id is None
    assert result.data["clarification"]["ollama_called"] is False
    assert result.data["clarification"]["database_touched"] is False
