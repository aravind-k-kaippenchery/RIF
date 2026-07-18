from unittest.mock import patch

from fastapi.testclient import TestClient

from app.db.health import DatabaseHealth
from app.main import create_app
from app.services.business_glossary import normalize_business_text, resolve_business_term
from app.services.schema_registry import get_allowed_columns, get_relationships, get_schema_contract, get_table_schema

client = TestClient(create_app())


def test_schema_contract_contains_business_and_operational_tables():
    contract = get_schema_contract()

    assert contract["table_count"] == 16
    assert "employees" in contract["business_tables"]
    assert "pending_actions" in contract["operational_tables"]
    assert contract["feature_17"]["parent_table"] == "employees"
    assert contract["feature_17"]["child_table"] == "employee_permissions"


def test_employee_schema_exposes_columns_for_future_llm_prompting():
    table = get_table_schema("employees")
    column_names = {column["name"] for column in table["columns"]}

    assert table["allowed_for_llm"] is True
    assert {"employee_code", "first_name", "last_name", "city", "salary"}.issubset(column_names)
    assert "id" in table["primary_key_columns"]


def test_feature_17_relationship_is_exposed_by_schema_registry():
    relationships = get_relationships()

    assert any(
        relationship["from_table"] == "employee_permissions"
        and relationship["from_column"] == "employee_id"
        and relationship["to_table"] == "employees"
        and relationship["to_column"] == "id"
        and relationship["delete_rule"] == "RESTRICT"
        for relationship in relationships
    )


def test_allowed_columns_returns_controlled_columns_only():
    columns = get_allowed_columns("vendors")

    assert "vendor_code" in columns
    assert "vendor_name" in columns
    assert "contact_email" in columns
    assert get_allowed_columns("pg_user") == []


def test_business_glossary_resolves_common_user_words():
    assert resolve_business_term("workers")["target_name"] == "employees"
    assert resolve_business_term("suppliers")["target_name"] == "vendors"
    assert resolve_business_term("clients")["target_name"] == "customers"
    assert resolve_business_term("permissions")["target_name"] == "employee_permissions"
    assert resolve_business_term("unknown-term") is None


def test_business_glossary_normalizes_prompt_terms():
    result = normalize_business_text("Show workers from Bangalore with permissions")

    assert "employees" in result["schema_hints"]
    assert "employee_permissions" in result["schema_hints"]


@patch("app.api.routes.schema.get_database_health")
def test_schema_endpoint_returns_controlled_contract(mock_database_health):
    mock_database_health.return_value = DatabaseHealth(
        connected=True,
        database="b2b_assistant",
        message="Connected.",
    )

    response = client.get("/api/schema")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["data"]["database_connected"] is True
    assert body["data"]["feature_17"]["child_table"] == "employee_permissions"


def test_glossary_resolve_endpoint_maps_workers_to_employees():
    response = client.get("/api/schema/glossary/resolve", params={"term": "workers"})

    assert response.status_code == 200
    body = response.json()
    assert body["data"]["target_name"] == "employees"
    assert body["data"]["target_type"] == "table"
