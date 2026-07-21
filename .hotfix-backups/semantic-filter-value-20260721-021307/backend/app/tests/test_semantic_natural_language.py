from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from app.core.constants import ResponseStatus, UserRole
from app.schemas.semantic_plan import SemanticFilter, SemanticIntentPlan
from app.services.semantic_query_service import SemanticQueryService, semantic_query_service
from app.services.semantic_understanding_service import resolve_semantic_plan, semantic_understanding_service


def make_plan(**updates):
    values = {
        "intent": "data.list",
        "operation": "read",
        "target_entity": "vendors",
        "target_table": "vendors",
        "requested_table_text": "vendors",
        "requested_columns": [],
        "filters": [],
        "sort": [],
        "limit": 50,
        "aggregate": None,
        "aggregate_field": None,
        "values": {},
        "count": None,
        "parent_table": None,
        "parent_identifier": {},
        "child_table": None,
        "references_previous_result": False,
        "reference_expression": None,
        "confidence": 0.98,
        "requires_clarification": False,
        "clarification_question": None,
        "missing_information": [],
        "ambiguities": [],
        "reasoning_summary": "The user explicitly asked to view vendor rows.",
    }
    values.update(updates)
    return SemanticIntentPlan.model_validate(values)


def test_table_existence_semantics_resolve_singular_vendor():
    plan = make_plan(
        intent="schema.table_exists",
        operation="schema",
        target_entity="vendor",
        target_table="vendor",
        requested_table_text="vendor",
        reasoning_summary="This asks whether the vendor table exists, not whether it has a vendor column.",
    )
    resolved = resolve_semantic_plan(plan, question="do we have vendor table")
    assert resolved.clarification_required is False
    assert resolved.canonical_table == "vendors"


def test_bare_table_phrase_requires_clarification():
    plan = make_plan(
        intent="clarification",
        operation="none",
        confidence=0.44,
        requires_clarification=True,
        clarification_question="Do you want to view vendor rows or inspect the vendors table structure?",
        missing_information=["requested_action"],
        ambiguities=["view records", "inspect schema"],
        reasoning_summary="The phrase names a table but no action.",
    )
    resolved = resolve_semantic_plan(plan, question="vendors table")
    assert resolved.clarification_required is True
    assert "view vendor rows" in resolved.clarification_question.lower()


def test_vendor_email_alias_is_grounded_to_contact_email():
    plan = make_plan(requested_columns=["email"])
    resolved = resolve_semantic_plan(plan, question="show vendor emails")
    assert resolved.clarification_required is False
    assert resolved.canonical_columns == ["contact_email"]


def test_hallucinated_vendor_column_becomes_clarification():
    plan = make_plan(requested_columns=["headquarters_colour"])
    resolved = resolve_semantic_plan(plan, question="show vendor headquarters colour")
    assert resolved.clarification_required is True
    assert "could not match" in resolved.clarification_question.lower()


def test_update_without_record_filter_is_blocked():
    plan = make_plan(
        intent="write.update",
        operation="update",
        target_table="employees",
        target_entity="employee",
        values={"city": "Kochi"},
        reasoning_summary="The requested change lacks a target employee.",
    )
    resolved = resolve_semantic_plan(plan, question="change an employee city to Kochi")
    assert resolved.clarification_required is True
    assert "record_filter" in resolved.missing_information


class FakeMCP:
    def __init__(self):
        self.sql = None

    def call_tool(self, name, arguments):
        assert name == "execute_validated_read"
        self.sql = arguments["sql"]
        return {
            "ok": True,
            "result": {
                "executed": True,
                "validation": {
                    "is_valid": True,
                    "statement_type": "SELECT",
                    "normalized_sql": self.sql,
                    "tables": ["vendors"],
                    "columns": ["vendor_name", "contact_email", "city"],
                    "warnings": [],
                    "error_code": None,
                    "error_message": None,
                    "applied_limit": 50,
                },
                "rows": [
                    {"vendor_name": "Chennai Automation", "contact_email": "contact@example.com", "city": "Chennai"}
                ],
                "row_count": 1,
                "source": {"source_type": "database", "tables": ["vendors"]},
            },
        }


def test_semantic_read_is_compiled_without_model_sql():
    fake = FakeMCP()
    service = SemanticQueryService(mcp_client=fake)
    plan = make_plan(
        intent="data.filter",
        requested_columns=["vendor name", "email", "city"],
        filters=[SemanticFilter(field="city", operator="eq", value="Chennai")],
    )
    resolved = resolve_semantic_plan(plan, question="show vendors from Chennai")
    result = service.execute_read(plan=resolved)
    assert result.status == ResponseStatus.SUCCESS
    assert result.row_count == 1
    assert "contact_email" in result.generated_sql
    assert "company_name" not in result.generated_sql
    assert "FROM vendors" in result.generated_sql
    assert fake.sql == result.generated_sql


def test_semantic_count_uses_deterministic_compiler():
    fake = FakeMCP()
    fake.call_tool = lambda name, arguments: {
        "ok": True,
        "result": {
            "executed": True,
            "validation": {
                "is_valid": True,
                "statement_type": "SELECT",
                "normalized_sql": arguments["sql"],
                "tables": ["vendors"],
                "columns": [],
                "warnings": [],
                "error_code": None,
                "error_message": None,
                "applied_limit": 50,
            },
            "rows": [{"count": 4}],
            "row_count": 1,
            "source": {"source_type": "database", "tables": ["vendors"]},
        },
    }
    service = SemanticQueryService(mcp_client=fake)
    plan = make_plan(intent="data.count", aggregate="count")
    resolved = resolve_semantic_plan(plan, question="how many vendors do we have")
    result = service.execute_read(plan=resolved)
    assert "4 matching vendors" in result.answer
    assert "count(" in result.generated_sql.lower()


def test_understanding_service_uses_typed_plan_not_sql(monkeypatch):
    plan = make_plan(
        intent="schema.table_exists",
        operation="schema",
        target_entity="employee",
        target_table="employee",
        requested_table_text="employee",
        reasoning_summary="The sentence asks whether a table exists.",
    )
    metadata = SimpleNamespace(model_dump=lambda: {"model": "llama3:8b", "attempts": 1})

    def fake_generate_json(**kwargs):
        assert kwargs["output_model"] is SemanticIntentPlan
        assert "Never generate SQL" in kwargs["system_prompt"]
        assert "current_user_message" in kwargs["user_prompt"]
        return plan, metadata

    monkeypatch.setattr("app.services.semantic_understanding_service.llm_service.generate_json", fake_generate_json)
    result = semantic_understanding_service.understand(question="do we have employees table", memory_context={})
    assert result.resolved.canonical_table == "employees"
    assert result.resolved.plan.intent == "schema.table_exists"
    assert result.model_metadata["model"] == "llama3:8b"


def test_clear_worker_location_request_repairs_over_cautious_model(monkeypatch):
    cautious = make_plan(
        intent="clarification",
        operation="none",
        target_entity=None,
        target_table=None,
        requested_table_text=None,
        filters=[],
        confidence=0.41,
        requires_clarification=True,
        clarification_question="Which database table do you want to use?",
        missing_information=["target_table", "low_confidence"],
        ambiguities=["target table is unclear"],
        reasoning_summary="The model was overly cautious.",
    )
    metadata = SimpleNamespace(model_dump=lambda: {"model": "llama3:8b", "attempts": 1})
    monkeypatch.setattr(
        "app.services.semantic_understanding_service.llm_service.generate_json",
        lambda **kwargs: (cautious, metadata),
    )

    result = semantic_understanding_service.understand(
        question="Show workers from Bangalore",
        memory_context={"available": False, "events": []},
    )

    assert result.resolved.clarification_required is False
    assert result.resolved.canonical_table == "employees"
    assert result.resolved.plan.intent == "data.filter"
    assert result.resolved.canonical_filters[0].field == "city"
    assert result.resolved.canonical_filters[0].value == "Bangalore"


def test_short_table_answer_fills_latest_clarification(monkeypatch):
    captured = {}
    plan = make_plan(
        intent="data.list",
        operation="read",
        target_entity="vendors",
        target_table="vendors",
        requested_table_text="vendors",
        reasoning_summary="The user supplied the missing target table.",
    )
    metadata = SimpleNamespace(model_dump=lambda: {"model": "llama3:8b", "attempts": 1})

    def fake_generate_json(**kwargs):
        captured["prompt"] = kwargs["user_prompt"]
        return plan, metadata

    monkeypatch.setattr("app.services.semantic_understanding_service.llm_service.generate_json", fake_generate_json)
    memory = {
        "available": True,
        "events": [
            {
                "prior_question": "show records",
                "status": "clarification_required",
                "conversation_reference": {
                    "reference_type": "clarification",
                    "original_question": "show records",
                    "missing_fields": ["target_table"],
                    "resolved_tables": [],
                },
            }
        ],
    }
    result = semantic_understanding_service.understand(question="vendors", memory_context=memory)
    assert result.resolved.clarification_required is False
    assert result.resolved.canonical_table == "vendors"
    assert "Use the `vendors` table" in captured["prompt"]


def test_short_action_answer_fills_table_view_clarification(monkeypatch):
    captured = {}
    plan = make_plan(
        intent="data.list",
        operation="read",
        target_entity="employees",
        target_table="employees",
        requested_table_text="employees",
        reasoning_summary="The user chose to view rows.",
    )
    metadata = SimpleNamespace(model_dump=lambda: {"model": "llama3:8b", "attempts": 1})

    def fake_generate_json(**kwargs):
        captured["prompt"] = kwargs["user_prompt"]
        return plan, metadata

    monkeypatch.setattr("app.services.semantic_understanding_service.llm_service.generate_json", fake_generate_json)
    memory = {
        "available": True,
        "events": [
            {
                "prior_question": "employee",
                "status": "clarification_required",
                "conversation_reference": {
                    "reference_type": "clarification",
                    "original_question": "employee",
                    "missing_fields": ["requested_action"],
                    "resolved_tables": ["employees"],
                },
            }
        ],
    }
    result = semantic_understanding_service.understand(question="review rows", memory_context=memory)
    assert result.resolved.clarification_required is False
    assert result.resolved.canonical_table == "employees"
    assert "Show rows from the `employees` table" in captured["prompt"]


def test_text_equality_filters_are_case_insensitive():
    fake = FakeMCP()
    service = SemanticQueryService(mcp_client=fake)
    plan = make_plan(
        intent="data.filter",
        filters=[SemanticFilter(field="city", operator="eq", value="bangalore")],
        target_entity="employee",
        target_table="employees",
        requested_table_text="employees",
    )
    resolved = resolve_semantic_plan(plan, question="show employees in bangalore")
    result = service.execute_read(plan=resolved)
    assert "ILIKE 'bangalore'" in result.generated_sql
    assert "employees.city = 'bangalore'" not in result.generated_sql


def test_text_in_filters_are_case_insensitive():
    fake = FakeMCP()
    service = SemanticQueryService(mcp_client=fake)
    plan = make_plan(
        intent="data.filter",
        filters=[SemanticFilter(field="city", operator="in", value=["bangalore", "chennai"])],
        target_entity="employee",
        target_table="employees",
        requested_table_text="employees",
    )
    resolved = resolve_semantic_plan(plan, question="show employees in Bangalore or Chennai")
    result = service.execute_read(plan=resolved)
    assert result.generated_sql.count("ILIKE") == 2
