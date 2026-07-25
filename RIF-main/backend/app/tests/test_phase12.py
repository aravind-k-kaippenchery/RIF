"""Phase 12 MCP expansion and hardening tests.

These tests keep the new MCP policy deterministic. They do not require a running
PostgreSQL server, ChromaDB, or Ollama instance.
"""

from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app.agents.orchestrator import agent_orchestrator
from app.main import create_app
from app.mcp.client import LocalMCPClient
from app.mcp.tools import (
    apply_admin_schema_change_tool,
    create_schema_change_preview_tool,
    get_table_records_tool,
    list_tables_tool,
)

client = TestClient(create_app())


def test_phase_twelve_status_and_catalog_expose_hardened_tools():
    status = agent_orchestrator.status()
    assert status["phase"] >= 12
    assert status["mcp_hardening_phase"] == 12
    assert status["raw_sql_write_tool_exposed"] is False

    names = {tool["name"] for tool in LocalMCPClient().list_tools()}
    assert {
        "list_tables",
        "get_table_records",
        "create_schema_change_preview",
        "apply_admin_schema_change",
    }.issubset(names)
    assert "execute_any_sql" not in names
    assert "run_unrestricted_sql" not in names


def test_list_tables_returns_only_controlled_application_tables():
    result = list_tables_tool(user_role="normal_user")
    assert result["listed"] is True
    names = {table["table_name"] for table in result["tables"]}
    assert "employees" in names
    assert "documents" in names
    assert "pg_catalog" not in names
    query_logs = next(table for table in result["tables"] if table["table_name"] == "query_logs")
    assert query_logs["requires_admin_for_records"] is True
    assert query_logs["record_read_allowed"] is False


def test_bounded_table_records_rejects_unknown_table_before_database_access():
    result = get_table_records_tool(table_name="pg_catalog", user_role="admin")
    assert result["retrieved"] is False
    assert result["error_code"] == "table_not_allowed"


def test_bounded_table_records_blocks_operational_data_for_normal_user_before_database_access():
    result = get_table_records_tool(table_name="action_logs", user_role="normal_user")
    assert result["retrieved"] is False
    assert result["error_code"] == "admin_role_required_for_operational_table"


def test_schema_preview_tool_requires_admin_before_any_database_access():
    result = create_schema_change_preview_tool(
        sql="ALTER TABLE vendors ADD COLUMN region VARCHAR(32)",
        user_prompt="Add a region column to vendors",
        user_role="normal_user",
    )
    assert result["created"] is False
    assert result["error_code"] == "admin_role_required"


def test_schema_preview_tool_rejects_forbidden_ddl_without_storing_it():
    result = create_schema_change_preview_tool(
        sql="DROP TABLE vendors",
        user_prompt="Drop vendors",
        user_role="admin",
    )
    assert result["created"] is False
    assert result["error_code"] == "forbidden_sql_operation"


def test_admin_schema_apply_requires_explicit_confirmation_before_any_execution():
    change_id = str(uuid4())
    pending = apply_admin_schema_change_tool(
        schema_change_id=change_id,
        confirmed=False,
        user_role="admin",
    )
    assert pending["executed"] is False
    assert pending["error_code"] == "explicit_confirmation_required"
    assert pending["raw_sql_accepted"] is False


def test_mcp_table_records_rest_endpoint_returns_bounded_database_source():
    fake_result = {
        "retrieved": True,
        "table_name": "employees",
        "columns": ["id", "employee_code"],
        "rows": [{"id": 1, "employee_code": "EMP-101"}],
        "row_count": 1,
        "limit": 10,
        "offset": 0,
        "source": {"source_type": "database", "tables": ["employees"]},
        "raw_sql_accepted": False,
    }
    with patch("app.api.routes.mcp.LocalMCPClient.call_tool", return_value={"ok": True, "result": fake_result}):
        response = client.get("/api/mcp/tables/employees/records?limit=10&offset=0")
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["row_count"] == 1
    assert body["sources"][0]["reference"] == "employees"
    assert body["data"]["raw_sql_accepted"] is False
