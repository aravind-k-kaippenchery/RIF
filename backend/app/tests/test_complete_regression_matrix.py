"""Fast, external-service-free smoke matrix for the final assistant workflow."""

from app.agents.orchestrator import agent_orchestrator
from app.agents.router import classify_question
from app.core.constants import AgentRoute
from app.services.parent_child_crud_service import _parse_clear_employee_experience_request
from app.services.sql_validation import validate_dml_sql
from app.services.synthetic_data_service import parse_synthetic_data_prompt


def test_structured_read_and_safe_write_routes_remain_distinct():
    assert classify_question("Show workers from Bangalore").route == AgentRoute.STRUCTURED_READ
    assert classify_question("Update EMP-101 city to Kochi").route == AgentRoute.CRUD_WRITE


def test_writes_still_require_where_and_confirmation_boundary():
    assert validate_dml_sql("UPDATE employees SET city = 'Kochi'").error_code == "where_clause_required"
    assert agent_orchestrator.status()["write_execution_allowed_without_confirmation"] is False


def test_schema_aware_faker_parses_multiple_business_tables_and_exact_counts():
    cases = {
        "Create 10 random employees": ("employees", 10),
        "Generate 5 synthetic vendors": ("vendors", 5),
        "Create 6 demo customers": ("customers", 6),
        "Populate 4 sample products": ("products", 4),
    }
    for prompt, expected in cases.items():
        request = parse_synthetic_data_prompt(prompt)
        assert request is not None
        assert (request.target_table, request.count) == expected


def test_parent_child_employee_experience_prompt_is_deterministic():
    plan = _parse_clear_employee_experience_request(
        "Add experience for EMP-105 at Infosys as Python Developer from 2021-01-01 to 2024-01-01"
    )
    assert plan is not None
    assert plan.operation == "create"
    assert plan.child_table == "employee_experiences"
    assert plan.parent_references[0].role == "employee"
    assert plan.parent_references[0].code == "EMP-105"


def test_rag_hybrid_and_memory_capabilities_remain_enabled():
    status = agent_orchestrator.status()
    assert status["graph_compiled"] is True
    assert status["hybrid_final_answer_available"] is True
    assert status["short_term_memory_available"] is True
