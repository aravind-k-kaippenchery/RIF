from __future__ import annotations

from app.core.constants import ResponseStatus, UserRole
from app.services.table_record_question_service import (
    TableRecordQuestion,
    detect_table_record_question,
    read_table_records,
)


class FakeMCPClient:
    def call_tool(self, name, arguments):
        assert name == "get_table_records"
        assert arguments["table_name"] == "vendors"
        assert arguments["user_role"] == UserRole.NORMAL_USER.value
        return {
            "ok": True,
            "result": {
                "retrieved": True,
                "table_name": "vendors",
                "columns": ["vendor_code", "vendor_name", "contact_email"],
                "rows": [
                    {
                        "vendor_code": "VEN-101",
                        "vendor_name": "Alpha Textiles",
                        "contact_email": "sales@alpha.example",
                    }
                ],
                "row_count": 1,
                "limit": 50,
                "offset": 0,
                "source": {"source_type": "database", "tables": ["vendors"]},
            },
        }


class FakeCountMCPClient:
    def call_tool(self, name, arguments):
        assert name == "get_table_records"
        assert arguments["table_name"] == "orders"
        assert arguments["limit"] == 1
        return {
            "ok": True,
            "result": {
                "retrieved": True,
                "table_name": "orders",
                "columns": ["order_id", "order_number"],
                "rows": [{"order_id": 1, "order_number": "ORD-1"}],
                "row_count": 1,
                "total_row_count": 12,
                "limit": 1,
                "offset": 0,
                "source": {"source_type": "database", "tables": ["orders"]},
            },
        }


class FakeFilteredMCPClient:
    def call_tool(self, name, arguments):
        assert name == "get_table_records"
        assert arguments["table_name"] == "vendors"
        assert arguments["filters"] == {"city": "chennai"}
        return {
            "ok": True,
            "result": {
                "retrieved": True,
                "table_name": "vendors",
                "columns": ["vendor_code", "vendor_name", "city"],
                "rows": [{"vendor_code": "VND-9", "vendor_name": "Chennai Textiles", "city": "Chennai"}],
                "row_count": 1,
                "total_row_count": 1,
                "limit": 50,
                "offset": 0,
                "applied_filters": {"city": "chennai"},
                "source": {"source_type": "database", "tables": ["vendors"]},
            },
        }


class FakeRelatedMCPClient:
    def call_tool(self, name, arguments):
        assert name == "get_table_records"
        assert arguments["table_name"] == "employee_permissions"
        assert arguments["relationship_filter"] == {
            "parent_table": "employees",
            "parent_column": "employee_code",
            "parent_value": "emp-001",
        }
        return {
            "ok": True,
            "result": {
                "retrieved": True,
                "table_name": "employee_permissions",
                "columns": ["permission_id", "employee_id", "permission_code"],
                "rows": [{"permission_id": 7, "employee_id": 1, "permission_code": "VIEW_REPORTS"}],
                "row_count": 1,
                "total_row_count": 1,
                "limit": 50,
                "offset": 0,
                "applied_relationship_filter": {
                    "parent_table": "employees",
                    "parent_column": "employee_code",
                    "parent_value": "emp-001",
                    "child_foreign_key": "employee_id",
                    "referenced_parent_column": "employee_id",
                },
                "source": {"source_type": "database", "tables": ["employee_permissions", "employees"]},
            },
        }


def test_explicit_vendor_table_data_is_deterministic_browse():
    for question in (
        "show me vendors table data",
        "show me vendors table",
        "shoe me vendors table data",
        "list vendors",
        "view vendor records",
    ):
        request = detect_table_record_question(question)
        assert request.handled is True
        assert request.needs_clarification is False
        assert request.canonical_table == "vendors"


def test_bare_vendor_table_requires_clarification():
    for question in ("vendors table", "what about vendors table"):
        request = detect_table_record_question(question)
        assert request.handled is True
        assert request.needs_clarification is True
        assert request.canonical_table == "vendors"
        assert "view rows" in (request.clarification_message or "")


def test_filtered_vendor_question_stays_in_normal_nl_sql_path():
    for question in (
        "show active vendors",
        "vendors in Kochi",
        "count vendors",
    ):
        assert detect_table_record_question(question).handled is False


def test_live_reflected_equality_filter_bypasses_hardcoded_vendor_shortcut(monkeypatch):
    monkeypatch.setattr(
        "app.services.table_record_question_service.get_public_table_names",
        lambda: ["vendors"],
    )
    monkeypatch.setattr(
        "app.services.table_record_question_service.get_runtime_columns",
        lambda _table: ["vendor_code", "vendor_name", "city", "status"],
    )
    request = detect_table_record_question("show vendors where city is Chennai")
    assert request.handled is True
    assert request.needs_clarification is False
    assert request.mode == "filter"
    assert request.filters == {"city": "chennai"}

    result = read_table_records(
        request=request,
        user_role=UserRole.NORMAL_USER,
        mcp_client=FakeFilteredMCPClient(),
    )
    assert result.status == ResponseStatus.SUCCESS
    assert result.data["applied_filters"] == {"city": "chennai"}
    assert result.data["rows"][0]["city"] == "Chennai"
    assert "matching city = chennai" in result.answer

    natural_request = detect_table_record_question("show vendors from Chennai")
    assert natural_request.handled is True
    assert natural_request.mode == "filter"
    assert natural_request.filters == {"city": "chennai"}


def test_unknown_live_filter_column_requests_clarification_instead_of_reading_all_rows(monkeypatch):
    monkeypatch.setattr(
        "app.services.table_record_question_service.get_public_table_names",
        lambda: ["vendors"],
    )
    monkeypatch.setattr(
        "app.services.table_record_question_service.get_runtime_columns",
        lambda _table: ["vendor_code", "vendor_name", "city"],
    )
    request = detect_table_record_question("show vendors where warehouse is Chennai")
    assert request.handled is True
    assert request.needs_clarification is True
    assert request.mode == "filter"
    assert "does not exist" in (request.clarification_message or "")


def test_value_then_column_wording_uses_live_schema_for_any_table(monkeypatch):
    cases = (
        (
            ["products"],
            ["product_id", "product_name", "category"],
            "show products from Textile Automation category",
            "products",
            {"category": "textile automation"},
        ),
        (
            ["employees"],
            ["employee_id", "employee_name", "department"],
            "show employees from Sales department",
            "employees",
            {"department": "sales"},
        ),
        (
            ["failed_orders"],
            ["failed_order_id", "order_number", "order_status"],
            "show failed orders with status failed",
            "failed_orders",
            {"order_status": "failed"},
        ),
    )
    for tables, columns, question, expected_table, expected_filters in cases:
        monkeypatch.setattr(
            "app.services.table_record_question_service.get_public_table_names",
            lambda tables=tables: tables,
        )
        monkeypatch.setattr(
            "app.services.table_record_question_service.get_runtime_columns",
            lambda _table, columns=columns: columns,
        )
        request = detect_table_record_question(question)
        assert request.handled is True, question
        assert request.mode == "filter"
        assert request.canonical_table == expected_table
        assert request.filters == expected_filters


def test_write_language_is_never_intercepted_as_a_reflected_read(monkeypatch):
    monkeypatch.setattr(
        "app.services.table_record_question_service.get_public_table_names",
        lambda: ["vendors"],
    )
    assert detect_table_record_question("delete vendors where city is Chennai").handled is False


def test_direct_parent_child_read_is_resolved_from_live_foreign_key(monkeypatch):
    monkeypatch.setattr(
        "app.services.table_record_question_service.get_public_table_names",
        lambda: ["employees", "employee_permissions"],
    )
    monkeypatch.setattr(
        "app.services.table_record_question_service.table_relationships",
        lambda table: (
            [
                {
                    "from_table": "employee_permissions",
                    "from_column": "employee_id",
                    "to_table": "employees",
                    "to_column": "employee_id",
                    "relationship_type": "many_to_one",
                }
            ]
            if table == "employee_permissions"
            else []
        ),
    )
    monkeypatch.setattr(
        "app.services.table_record_question_service.get_runtime_columns",
        lambda table: ["employee_id", "employee_code", "first_name"] if table == "employees" else [],
    )
    monkeypatch.setattr(
        "app.services.table_record_question_service.get_primary_key_columns",
        lambda table: ["employee_id"] if table == "employees" else [],
    )

    request = detect_table_record_question("Show permissions for employee EMP-001")
    assert request.handled is True
    assert request.mode == "related"
    assert request.canonical_table == "employee_permissions"
    assert request.relationship_filter == {
        "parent_table": "employees",
        "parent_column": "employee_code",
        "parent_value": "emp-001",
    }

    result = read_table_records(
        request=request,
        user_role=UserRole.NORMAL_USER,
        mcp_client=FakeRelatedMCPClient(),
    )
    assert result.status == ResponseStatus.SUCCESS
    assert result.data["rows"][0]["permission_code"] == "VIEW_REPORTS"
    assert "employee_code = emp-001" in result.answer


def test_overlapping_child_alias_does_not_create_a_fake_third_table(monkeypatch):
    monkeypatch.setattr(
        "app.services.table_record_question_service.get_public_table_names",
        lambda: ["vendors", "products", "product_vendor_mappings"],
    )
    monkeypatch.setattr(
        "app.services.table_record_question_service.table_relationships",
        lambda table: (
            [
                {"from_table": table, "from_column": "product_id", "to_table": "products", "to_column": "id"},
                {"from_table": table, "from_column": "vendor_id", "to_table": "vendors", "to_column": "id"},
            ]
            if table == "product_vendor_mappings"
            else []
        ),
    )
    monkeypatch.setattr(
        "app.services.table_record_question_service.get_runtime_columns",
        lambda table: ["id", "product_code"] if table == "products" else [],
    )
    monkeypatch.setattr(
        "app.services.table_record_question_service.get_primary_key_columns",
        lambda _table: ["id"],
    )
    request = detect_table_record_question("Show vendor mappings for product PRD-001")
    assert request.handled is True
    assert request.canonical_table == "product_vendor_mappings"
    assert request.relationship_filter == {
        "parent_table": "products",
        "parent_column": "product_code",
        "parent_value": "prd-001",
    }


def test_exact_reflected_table_count_bypasses_ollama(monkeypatch):
    monkeypatch.setattr(
        "app.services.table_record_question_service.get_public_table_names",
        lambda: ["orders"],
    )
    request = detect_table_record_question("Count every row in the orders table.")
    assert request.handled is True
    assert request.canonical_table == "orders"
    assert request.mode == "count"

    result = read_table_records(
        request=request,
        user_role=UserRole.NORMAL_USER,
        mcp_client=FakeCountMCPClient(),
    )
    assert result.answer == "There are 12 rows in the `orders` table."
    assert result.data["row_count"] == 12
    assert result.data["rows"] == []
    assert result.data["execution"]["ollama_called"] is False


def test_first_n_reflected_table_records_bypass_ollama(monkeypatch):
    monkeypatch.setattr(
        "app.services.table_record_question_service.get_public_table_names",
        lambda: ["orders"],
    )
    for question in (
        "Show the first 5 records from the orders table.",
        "show first 5 records of orders table",
        "List the top 5 rows in orders",
        "Show first 5 orders",
    ):
        request = detect_table_record_question(question)
        assert request.handled is True, question
        assert request.canonical_table == "orders"
        assert request.mode == "browse"
        assert request.limit == 5


def test_bounded_mcp_result_is_returned_without_generated_sql():
    result = read_table_records(
        request=TableRecordQuestion(True, canonical_table="vendors"),
        user_role=UserRole.NORMAL_USER,
        mcp_client=FakeMCPClient(),
    )
    assert result.status == ResponseStatus.SUCCESS
    assert result.data["row_count"] == 1
    assert result.data["rows"][0]["contact_email"] == "sales@alpha.example"
    assert result.data["execution"]["ollama_called"] is False
    assert result.data["execution"]["raw_sql_accepted"] is False
