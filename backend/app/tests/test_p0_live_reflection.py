"""P0 regression coverage for live-table behavior.

These tests protect the compatibility rule: original demo shortcuts still work, but
an exact reflected PostgreSQL table always wins and generic write safety uses its real
primary/unique keys.
"""

from sqlalchemy import Column, ForeignKey, Integer, MetaData, String, Table, UniqueConstraint, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.constants import UserRole
from app.services import demo_question_service, explicit_insert_service, llm_service
from app.services import schema_metadata_question_service, table_record_question_service
from app.services.audit_rollback_service import AuditRollbackService
from app.services.crud_write_service import CrudWriteService
from app.services.duplicate_service import detect_record_duplicates, get_unique_key_sets
from app.services import mcp_table_service
from app.services.mcp_table_service import MCPTableAccessError, _reflected_unique_constraints


def test_real_orders_table_wins_over_legacy_sales_deals_alias(monkeypatch):
    monkeypatch.setattr(demo_question_service, "has_public_table", lambda name: name == "orders")
    result = demo_question_service.detect_demo_question("Count every row in the orders table.")
    assert result.handled is False


def test_legacy_orders_alias_remains_when_orders_table_is_absent(monkeypatch):
    monkeypatch.setattr(demo_question_service, "has_public_table", lambda _name: False)
    result = demo_question_service.detect_demo_question("Show recent orders")
    assert result.handled is True
    assert result.table == "sales_deals"


def test_vendor_demo_shortcut_never_discards_an_unhandled_location_filter():
    result = demo_question_service.detect_demo_question("vendors in Kochi")
    assert result.handled is False


def test_product_demo_shortcut_never_discards_an_unhandled_category_filter():
    result = demo_question_service.detect_demo_question(
        "show products from Textile Automation category"
    )
    assert result.handled is False


def test_dynamic_table_record_and_schema_visibility(monkeypatch):
    monkeypatch.setattr(
        table_record_question_service,
        "get_public_table_names",
        lambda: ["employees", "failed_orders", "action_logs"],
    )
    request = table_record_question_service.detect_table_record_question("Show failed orders table data")
    assert request.handled is True
    assert request.canonical_table == "failed_orders"

    monkeypatch.setattr(
        schema_metadata_question_service,
        "get_allowed_tables",
        lambda: ["employees", "failed_orders", "action_logs"],
    )
    assert schema_metadata_question_service._visible_tables(UserRole.NORMAL_USER) == ["employees", "failed_orders"]
    assert "action_logs" in schema_metadata_question_service._visible_tables(UserRole.ADMIN)


def test_compact_llm_contract_contains_reflected_business_table(monkeypatch):
    contract = {
        "tables": [
            {
                "table_name": "orders",
                "category": "business",
                "allowed_for_llm": True,
                "columns": [{"name": "order_id"}, {"name": "order_number"}],
                "primary_key_columns": ["order_id"],
            },
            {
                "table_name": "action_logs",
                "category": "operational",
                "allowed_for_llm": False,
                "columns": [{"name": "id"}],
                "primary_key_columns": ["id"],
            },
        ],
        "relationships": [],
    }
    monkeypatch.setattr(llm_service, "get_schema_contract", lambda: contract)
    compact = llm_service.LLMService._compact_sql_schema_contract()
    assert compact["business_tables"] == ["orders"]
    assert compact["tables"][0]["columns"] == ["order_id", "order_number"]


def test_explicit_insert_parses_required_columns_from_reflected_table(monkeypatch):
    metadata = MetaData()
    table = Table(
        "failed_orders",
        metadata,
        Column("failed_order_id", Integer, primary_key=True, autoincrement=True),
        Column("order_number", String, nullable=False, unique=True),
        Column("failure_reason", String, nullable=False),
    )
    monkeypatch.setattr(explicit_insert_service, "get_public_table_names", lambda: ["failed_orders"])
    monkeypatch.setattr(explicit_insert_service, "get_runtime_table", lambda _name: table)
    result = explicit_insert_service.detect_explicit_insert(
        "Create a failed order with order number ORD-101, failure reason card declined"
    )
    assert result is not None
    assert result.target_table == "failed_orders"
    assert result.records == [{"order_number": "ORD-101", "failure_reason": "card declined"}]


def test_duplicate_detection_reflects_unique_constraint_and_non_id_primary_key():
    engine = create_engine("sqlite://")
    metadata = MetaData()
    table = Table(
        "orders",
        metadata,
        Column("order_id", Integer, primary_key=True),
        Column("order_number", String, nullable=False),
        UniqueConstraint("order_number", name="uq_orders_order_number"),
    )
    metadata.create_all(engine)
    with Session(engine) as db:
        db.execute(table.insert().values(order_id=1, order_number="ORD-1"))
        db.commit()
        assert ("order_number",) in get_unique_key_sets(db, "orders")
        matches = detect_record_duplicates(db, table_name="orders", values={"order_number": "ORD-1"})
    assert matches and matches[0].key_fields == ("order_number",)


def test_primary_key_helpers_support_composite_keys():
    metadata = MetaData()
    table = Table(
        "order_lines",
        metadata,
        Column("order_id", Integer, primary_key=True),
        Column("line_number", Integer, primary_key=True),
        Column("sku", String),
        UniqueConstraint("order_id", "sku", name="uq_order_line_sku"),
    )
    identities = CrudWriteService._record_identities(
        [{"order_id": 7, "line_number": 2, "sku": "A-1"}],
        table,
    )
    assert identities == [{"order_id": 7, "line_number": 2}]
    clause = AuditRollbackService._identity_filter(table, identities[0])
    assert "order_id" in str(clause) and "line_number" in str(clause)
    assert _reflected_unique_constraints(table) == [
        {"name": "uq_order_line_sku", "columns": ["order_id", "sku"]}
    ]


def test_bounded_mcp_filters_use_live_columns_and_case_insensitive_string_values(monkeypatch):
    engine = create_engine("sqlite://")
    metadata = MetaData()
    vendors = Table(
        "vendors",
        metadata,
        Column("vendor_id", Integer, primary_key=True),
        Column("vendor_name", String, nullable=False),
        Column("city", String, nullable=False),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            vendors.insert(),
            [
                {"vendor_id": 1, "vendor_name": "A", "city": "Chennai"},
                {"vendor_id": 2, "vendor_name": "B", "city": "Bangalore"},
            ],
        )

    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(mcp_table_service, "get_session_factory", lambda: factory)
    monkeypatch.setattr(mcp_table_service, "_rif_has_public_table", lambda name: name == "vendors")
    monkeypatch.setattr(mcp_table_service, "_rif_get_runtime_table", lambda _name, bind=None: vendors)

    result = mcp_table_service.get_bounded_table_records(
        table_name="vendors",
        filters={"city": "chennai"},
    )
    assert result["total_row_count"] == 1
    assert result["row_count"] == 1
    assert result["rows"][0]["city"] == "Chennai"
    assert result["applied_filters"] == {"city": "chennai"}

    try:
        mcp_table_service.get_bounded_table_records(
            table_name="vendors",
            filters={"missing_column": "x"},
        )
    except MCPTableAccessError as exc:
        assert exc.code == "table_filter_column_not_found"
    else:  # pragma: no cover - explicit fail is clearer than silently returning all rows.
        raise AssertionError("An unknown filter column must not return unfiltered rows.")


def test_bounded_mcp_relationship_filter_uses_reflected_foreign_key(monkeypatch):
    engine = create_engine("sqlite://")
    metadata = MetaData()
    employees = Table(
        "employees",
        metadata,
        Column("employee_id", Integer, primary_key=True),
        Column("employee_code", String, nullable=False, unique=True),
    )
    permissions = Table(
        "employee_permissions",
        metadata,
        Column("permission_id", Integer, primary_key=True),
        Column("employee_id", ForeignKey("employees.employee_id"), nullable=False),
        Column("permission_code", String, nullable=False),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            employees.insert(),
            [
                {"employee_id": 1, "employee_code": "EMP-001"},
                {"employee_id": 2, "employee_code": "EMP-002"},
            ],
        )
        connection.execute(
            permissions.insert(),
            [
                {"permission_id": 10, "employee_id": 1, "permission_code": "VIEW_REPORTS"},
                {"permission_id": 20, "employee_id": 2, "permission_code": "EDIT_USERS"},
            ],
        )

    tables = {"employees": employees, "employee_permissions": permissions}
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(mcp_table_service, "get_session_factory", lambda: factory)
    monkeypatch.setattr(mcp_table_service, "_rif_has_public_table", lambda name: name in tables)
    monkeypatch.setattr(
        mcp_table_service,
        "_rif_get_runtime_table",
        lambda name, bind=None: tables[name],
    )

    result = mcp_table_service.get_bounded_table_records(
        table_name="employee_permissions",
        relationship_filter={
            "parent_table": "employees",
            "parent_column": "employee_code",
            "parent_value": "emp-001",
        },
    )
    assert result["row_count"] == 1
    assert result["rows"][0]["permission_code"] == "VIEW_REPORTS"
    assert result["applied_relationship_filter"]["child_foreign_key"] == "employee_id"
