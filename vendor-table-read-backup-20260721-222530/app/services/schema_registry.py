"""Controlled schema intelligence for Phase 3.

The LLM must never guess table names or columns. This module exposes only approved
application tables and relationships from SQLAlchemy metadata and, when requested,
from live PostgreSQL inspection.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.models import Base
from app.services.database_inspection import APPLICATION_TABLES


BUSINESS_TABLES = [
    "employees",
    "employee_permissions",
    "vendors",
    "customers",
    "products",
    "product_vendor_mappings",
    "sales_deals",
]

OPERATIONAL_TABLES = [
    "sessions",
    "pending_actions",
    "query_logs",
    "action_logs",
    "change_snapshots",
    "documents",
    "document_ingestion_jobs",
    "benchmark_runs",
    "schema_change_requests",
]

COLUMN_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "employees": {
        "employee_code": "Stable employee identifier used in demos and API paths.",
        "first_name": "Employee first name.",
        "last_name": "Employee last name.",
        "email": "Unique employee email used for duplicate checks.",
        "department": "Employee department such as HR, Finance, IT, or Sales.",
        "city": "Employee city/location.",
        "salary": "Employee salary used for salary-based queries.",
    },
    "employee_permissions": {
        "employee_id": "Foreign key pointing to employees.id.",
        "permission_code": "Permission assigned to one employee.",
        "description": "Human-readable permission explanation.",
        "is_active": "Whether the permission is active.",
    },
    "vendors": {
        "vendor_code": "Stable vendor identifier.",
        "vendor_name": "Vendor/company name.",
        "contact_email": "Unique vendor contact email.",
        "city": "Vendor city/location.",
        "category": "Vendor business category.",
    },
    "customers": {
        "customer_code": "Stable customer identifier.",
        "customer_name": "Customer/company name.",
        "contact_email": "Unique customer contact email.",
        "city": "Customer city/location.",
        "industry": "Customer industry.",
    },
    "products": {
        "product_code": "Stable product identifier.",
        "product_name": "Product name.",
        "category": "Product category.",
        "price": "Product price used for price filters.",
        "currency": "Currency of the product price.",
    },
    "sales_deals": {
        "deal_code": "Stable sales deal identifier.",
        "amount": "Sales deal amount.",
        "stage": "Sales pipeline stage.",
        "expected_close_date": "Expected deal close date.",
    },
}


def _normalize_type(column_type: Any) -> str:
    """Return a stable string for a SQLAlchemy column type."""

    return str(column_type).lower()


def _table_category(table_name: str) -> str:
    if table_name in BUSINESS_TABLES:
        return "business"
    if table_name in OPERATIONAL_TABLES:
        return "operational"
    return "unknown"


def _column_metadata(table_name: str, column: Any) -> dict[str, Any]:
    return {
        "name": column.name,
        "type": _normalize_type(column.type),
        "nullable": bool(column.nullable),
        "primary_key": bool(column.primary_key),
        "unique": bool(column.unique),
        "indexed": bool(column.index),
        "description": COLUMN_DESCRIPTIONS.get(table_name, {}).get(column.name),
    }


def _relationship_metadata(table_name: str, column: Any) -> list[dict[str, Any]]:
    relationships: list[dict[str, Any]] = []
    for foreign_key in column.foreign_keys:
        target_table = foreign_key.column.table.name
        if target_table not in APPLICATION_TABLES:
            continue
        relationships.append(
            {
                "from_table": table_name,
                "from_column": column.name,
                "to_table": target_table,
                "to_column": foreign_key.column.name,
                "relationship_type": "many_to_one",
                "delete_rule": foreign_key.ondelete,
            }
        )
    return relationships


def get_allowed_tables() -> list[str]:
    """Return approved application tables in stable order."""

    return [table_name for table_name in APPLICATION_TABLES if table_name in Base.metadata.tables]


def get_allowed_columns(table_name: str) -> list[str]:
    """Return approved columns for one application table."""

    normalized = table_name.strip().lower()
    if normalized not in get_allowed_tables():
        return []
    return [column.name for column in Base.metadata.tables[normalized].columns]


def get_table_schema(table_name: str) -> dict[str, Any]:
    """Return controlled schema metadata for one approved table."""

    normalized = table_name.strip().lower()
    if normalized not in get_allowed_tables():
        raise KeyError(f"Table '{table_name}' is not an approved application table.")

    table = Base.metadata.tables[normalized]
    columns = [_column_metadata(normalized, column) for column in table.columns]
    relationships: list[dict[str, Any]] = []
    for column in table.columns:
        relationships.extend(_relationship_metadata(normalized, column))

    return {
        "table_name": normalized,
        "category": _table_category(normalized),
        "columns": columns,
        "column_count": len(columns),
        "primary_key_columns": [column.name for column in table.primary_key.columns],
        "relationships": relationships,
        "allowed_for_llm": True,
    }


def get_schema_contract() -> dict[str, Any]:
    """Return the complete controlled schema summary used by later LLM prompts."""

    tables = [get_table_schema(table_name) for table_name in get_allowed_tables()]
    relationships = get_relationships()
    return {
        "schema_source": "sqlalchemy_metadata",
        "table_count": len(tables),
        "business_tables": [table for table in BUSINESS_TABLES if table in get_allowed_tables()],
        "operational_tables": [table for table in OPERATIONAL_TABLES if table in get_allowed_tables()],
        "tables": tables,
        "relationships": relationships,
        "feature_17": {
            "parent_table": "employees",
            "child_table": "employee_permissions",
            "parent_key": "employees.id",
            "child_key": "employee_permissions.employee_id",
            "cardinality": "one_to_zero_or_many",
            "delete_rule": "RESTRICT",
        },
    }


def get_relationships() -> list[dict[str, Any]]:
    """Return all approved table foreign-key relationships."""

    relationships: list[dict[str, Any]] = []
    for table_name in get_allowed_tables():
        table = Base.metadata.tables[table_name]
        for column in table.columns:
            relationships.extend(_relationship_metadata(table_name, column))
    return relationships


def get_live_table_names(session: Session) -> list[str]:
    """Return approved application tables that actually exist in PostgreSQL."""

    inspector = inspect(session.bind)
    existing = set(inspector.get_table_names(schema="public"))
    return [table for table in get_allowed_tables() if table in existing]


def get_live_schema_status(session: Session) -> dict[str, Any]:
    """Compare SQLAlchemy's controlled schema with live PostgreSQL tables."""

    expected_tables = set(get_allowed_tables())
    live_tables = set(get_live_table_names(session))
    return {
        "expected_table_count": len(expected_tables),
        "live_table_count": len(live_tables),
        "missing_tables": sorted(expected_tables - live_tables),
        "unexpected_application_tables": sorted(live_tables - expected_tables),
        "is_in_sync": expected_tables.issubset(live_tables),
    }
