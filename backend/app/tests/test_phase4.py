from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import create_app
from app.services.sql_validation import preview_admin_schema_change, validate_dml_sql

client = TestClient(create_app())


def test_select_validation_adds_server_controlled_limit():
    result = validate_dml_sql("SELECT employee_code, first_name, city FROM employees WHERE city = 'Bangalore'")

    assert result.is_valid is True
    assert result.statement_type == "SELECT"
    assert result.applied_limit == 100
    assert result.normalized_sql.endswith("LIMIT 100")
    assert result.tables == ["employees"]
    assert {"employees.employee_code", "employees.first_name", "employees.city"}.issubset(result.columns)


def test_safe_join_must_follow_known_foreign_key_relationship():
    result = validate_dml_sql(
        "SELECT e.employee_code, p.permission_code "
        "FROM employees e JOIN employee_permissions p ON p.employee_id = e.id "
        "WHERE e.city = 'Bangalore'"
    )

    assert result.is_valid is True
    assert result.statement_type == "SELECT"
    assert result.tables == ["employees", "employee_permissions"]


def test_dangerous_or_multi_statement_sql_is_blocked():
    drop_result = validate_dml_sql("DROP TABLE employees")
    multi_result = validate_dml_sql("SELECT employee_code FROM employees; DELETE FROM employees WHERE employee_code = 'EMP-101'")
    comment_result = validate_dml_sql("SELECT employee_code FROM employees -- bypass")

    assert drop_result.is_valid is False
    assert drop_result.error_code == "forbidden_sql_operation"
    assert multi_result.is_valid is False
    assert multi_result.error_code == "multiple_statements_blocked"
    assert comment_result.is_valid is False
    assert comment_result.error_code == "sql_comments_blocked"


def test_update_and_delete_need_where_clause_and_unknown_columns_are_blocked():
    update_result = validate_dml_sql("UPDATE employees SET city = 'Kochi'")
    delete_result = validate_dml_sql("DELETE FROM employees")
    column_result = validate_dml_sql("SELECT secret_password FROM employees")

    assert update_result.is_valid is False
    assert update_result.error_code == "where_clause_required"
    assert delete_result.is_valid is False
    assert delete_result.error_code == "where_clause_required"
    assert column_result.is_valid is False
    assert column_result.error_code == "unknown_column"


def test_insert_is_validated_but_marked_proposal_only():
    result = validate_dml_sql(
        "INSERT INTO employees "
        "(employee_code, first_name, last_name, email, department, city, company_name, salary, employment_status) "
        "VALUES ('EMP-NEW', 'Maya', 'Nair', 'maya@example.com', 'Sales', 'Bangalore', 'Neolotex', 50000, 'active')"
    )

    assert result.is_valid is True
    assert result.statement_type == "INSERT"
    assert "proposal-only" in result.warnings[0]


def test_admin_schema_previews_allow_only_create_table_and_add_column():
    create_result = preview_admin_schema_change("CREATE TABLE project_notes (id INTEGER, title VARCHAR(100), active BOOLEAN)")
    add_column_result = preview_admin_schema_change("ALTER TABLE vendors ADD COLUMN gst_number VARCHAR(32)")
    drop_column_result = preview_admin_schema_change("ALTER TABLE vendors DROP COLUMN category")

    assert create_result.is_valid is True
    assert create_result.statement_type == "CREATE_TABLE_PREVIEW"
    assert add_column_result.is_valid is True
    assert add_column_result.statement_type == "ALTER_TABLE_ADD_COLUMN_PREVIEW"
    assert drop_column_result.is_valid is False
    assert drop_column_result.error_code == "forbidden_sql_operation"


def test_validation_rest_endpoint_returns_controlled_error_for_drop():
    response = client.post("/api/validation/sql", json={"sql": "DROP TABLE employees"})

    assert response.status_code == 400
    body = response.json()
    assert body["status"] == "validation_failed"
    assert body["error"]["code"] == "forbidden_sql_operation"


def test_admin_schema_preview_requires_admin_header_and_returns_preview():
    blocked = client.post("/api/validation/schema-changes/preview", json={"sql": "ALTER TABLE vendors ADD COLUMN gst_number VARCHAR(32)"})
    allowed = client.post(
        "/api/validation/schema-changes/preview",
        json={"sql": "ALTER TABLE vendors ADD COLUMN gst_number VARCHAR(32)"},
        headers={"X-User-Role": "admin"},
    )

    assert blocked.status_code == 403
    assert allowed.status_code == 200
    assert allowed.json()["data"]["statement_type"] == "ALTER_TABLE_ADD_COLUMN_PREVIEW"


def test_mcp_tool_catalog_and_local_validation_facade_are_available():
    catalog = client.get("/api/mcp/tools")
    validated = client.post("/api/mcp/validate-sql", json={"sql": "SELECT employee_code FROM employees LIMIT 5"})

    assert catalog.status_code == 200
    names = {tool["name"] for tool in catalog.json()["data"]["tools"]}
    assert {"get_schema", "validate_sql", "execute_validated_read", "create_pending_action", "get_pending_action"}.issubset(names)
    assert validated.status_code == 200
    assert validated.json()["data"]["is_valid"] is True
    assert validated.json()["data"]["statement_type"] == "SELECT"
