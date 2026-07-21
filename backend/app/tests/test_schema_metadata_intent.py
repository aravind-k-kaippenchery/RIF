"""Tests for current-turn schema intent and stale-memory isolation."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.agents.orchestrator import agent_orchestrator
from app.core.constants import AgentRoute, ResponseStatus, UserRole
from app.services.schema_metadata_question_service import detect_schema_metadata_question
from app.services.session_memory_service import memory_context_for_question


class _Inspector:
    def get_table_names(self, schema: str = "public"):
        assert schema == "public"
        return [
            "employees",
            "employee_permissions",
            "vendors",
            "customers",
            "products",
            "product_vendor_mappings",
            "sales_deals",
        ]

    def get_columns(self, table_name: str, schema: str = "public"):
        assert schema == "public"
        columns = {
            "employees": ["id", "employee_code", "first_name", "last_name", "department", "city", "salary"],
            "products": ["id", "product_code", "product_name", "category", "price"],
        }
        return [{"name": item} for item in columns.get(table_name, ["id"])]


def test_detects_employee_table_existence_question_from_natural_language():
    request = detect_schema_metadata_question("do we have a table employee?")

    assert request.handled is True
    assert request.kind == "table_exists"
    assert request.canonical_table == "employees"



def test_bare_have_table_phrases_are_table_existence_not_column_questions():
    vendor = detect_schema_metadata_question("do we have vendor table")
    employees = detect_schema_metadata_question("do we have employees table")

    assert vendor.handled is True
    assert vendor.kind == "table_exists"
    assert vendor.canonical_table == "vendors"
    assert vendor.requested_column is None

    assert employees.handled is True
    assert employees.kind == "table_exists"
    assert employees.canonical_table == "employees"
    assert employees.requested_column is None


def test_table_existence_phrases_answer_from_metadata_without_ollama():
    with patch("app.services.schema_metadata_question_service.inspect", return_value=_Inspector()), \
         patch("app.agents.orchestrator.llm_service.generate_sql") as generate_sql, \
         patch("app.agents.orchestrator.structured_read_service.execute") as structured_read:
        vendor_result = agent_orchestrator.run(
            question="do we have vendor table",
            db=SimpleNamespace(bind=object()),
            request_id=None,
            session_id=None,
            user_role=UserRole.NORMAL_USER,
            top_k=None,
            memory_context=None,
        )
        employee_result = agent_orchestrator.run(
            question="do we have employees table",
            db=SimpleNamespace(bind=object()),
            request_id=None,
            session_id=None,
            user_role=UserRole.NORMAL_USER,
            top_k=None,
            memory_context=None,
        )

    generate_sql.assert_not_called()
    structured_read.assert_not_called()
    assert vendor_result.answer == "Yes. The database contains a `vendors` table."
    assert employee_result.answer == "Yes. The database contains an `employees` table."
    assert vendor_result.data["schema_metadata"]["requested_column"] is None
    assert employee_result.data["schema_metadata"]["requested_column"] is None


def test_explicit_column_question_still_uses_column_intent():
    request = detect_schema_metadata_question("do we have a city column in employee table?")

    assert request.kind == "column_exists"
    assert request.canonical_table == "employees"
    assert request.requested_column == "city"

def test_detects_table_and_column_schema_questions():
    assert detect_schema_metadata_question("what tables do we have?").kind == "list_tables"
    assert detect_schema_metadata_question("what columns are in the employee table?").kind == "list_columns"
    column = detect_schema_metadata_question("does the employee table have a city column?")
    assert column.kind == "column_exists"
    assert column.canonical_table == "employees"
    assert column.requested_column == "city"


def test_schema_question_bypasses_ollama_and_never_returns_old_bangalore_rows():
    with patch("app.services.schema_metadata_question_service.inspect", return_value=_Inspector()), \
         patch("app.agents.orchestrator.llm_service.generate_sql") as generate_sql, \
         patch("app.agents.orchestrator.structured_read_service.execute") as structured_read:
        result = agent_orchestrator.run(
            question="do we have a table employee?",
            db=SimpleNamespace(bind=object()),
            request_id=None,
            session_id=None,
            user_role=UserRole.NORMAL_USER,
            top_k=None,
            memory_context={
                "available": True,
                "event_count": 1,
                "events": [
                    {
                        "prior_question": "Show employees from Bangalore",
                        "route": "structured_read",
                        "status": "success",
                        "generated_sql": "SELECT employee_code FROM employees WHERE city = 'Bangalore' LIMIT 100",
                    }
                ],
            },
        )

    generate_sql.assert_not_called()
    structured_read.assert_not_called()
    assert result.route == AgentRoute.SYSTEM
    assert result.status == ResponseStatus.SUCCESS
    assert result.answer == "Yes. The database contains an `employees` table."
    assert result.generated_sql is None
    assert result.data["schema_metadata"]["conversation_memory_used"] is False
    assert result.data["schema_metadata"]["business_rows_queried"] is False


def test_column_question_uses_metadata_not_row_sql():
    with patch("app.services.schema_metadata_question_service.inspect", return_value=_Inspector()), \
         patch("app.agents.orchestrator.llm_service.generate_sql") as generate_sql:
        result = agent_orchestrator.run(
            question="does employee table have city column?",
            db=SimpleNamespace(bind=object()),
            request_id=None,
            session_id=None,
            user_role=UserRole.NORMAL_USER,
            top_k=None,
            memory_context=None,
        )

    generate_sql.assert_not_called()
    assert result.status == ResponseStatus.SUCCESS
    assert "contains a `city` column" in result.answer
    assert result.generated_sql is None


def test_standalone_question_excludes_old_memory_filters():
    context = {
        "available": True,
        "event_count": 1,
        "events": [{"prior_question": "Show employees from Bangalore", "generated_sql": "WHERE city = 'Bangalore'"}],
        "policy": "bounded",
    }

    isolated = memory_context_for_question("show workers from Chennai", context)
    referenced = memory_context_for_question("show the same workers again", context)

    assert isolated["available"] is False
    assert isolated["events"] == []
    assert "standalone current-turn" in isolated["policy"]
    assert referenced is context
