"""Phase 13 bounded table inspection behind the MCP boundary.

The service never accepts raw SQL.  It exposes the approved application schema plus a
small, admin-only set of tables that were created by a persisted Phase 13 restricted
schema-change approval.  Runtime reflection makes approved ``ALTER TABLE ADD COLUMN``
changes visible to the frontend table viewer immediately.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from sqlalchemy import MetaData, Table, select
from sqlalchemy.exc import SQLAlchemyError

from app.core.constants import UserRole
from app.db.session import get_session_factory
from app.models import Base
from app.models.operations import SchemaChangeRequest
from app.services.schema_registry import BUSINESS_TABLES, OPERATIONAL_TABLES, get_allowed_tables, get_table_schema

MAX_TABLE_RECORDS = 100


class MCPTableAccessError(RuntimeError):
    """Safe, structured error raised inside bounded MCP table access."""

    def __init__(self, *, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def parse_mcp_role(user_role: str) -> UserRole:
    """Parse one temporary role value without silently escalating privileges."""

    try:
        return UserRole(str(user_role).strip().lower())
    except ValueError as exc:
        raise MCPTableAccessError(
            code="invalid_user_role",
            message="user_role must be normal_user or admin.",
        ) from exc


def _safe_value(value: Any) -> Any:
    """Convert SQLAlchemy values to a JSON-safe representation."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe_value(item) for item in value]
    return str(value)


def _admin_created_table_names() -> list[str]:
    """Return only tables created by a completed restricted Phase 13 request."""

    try:
        with get_session_factory()() as db:
            changes = list(
                db.scalars(
                    select(SchemaChangeRequest).where(SchemaChangeRequest.status == "executed")
                ).all()
            )
            names: list[str] = []
            for change in changes:
                preview = change.schema_preview if isinstance(change.schema_preview, dict) else {}
                if preview.get("statement_type") != "CREATE_TABLE_PREVIEW":
                    continue
                for table_name in preview.get("tables") or []:
                    normalized = str(table_name).strip().lower()
                    if normalized and normalized not in names:
                        names.append(normalized)
            return names
    except SQLAlchemyError:
        # Table listings must remain available for the original approved schema when the
        # optional executed-schema registry cannot be read.
        return []


def _table_category(table_name: str) -> str:
    if table_name in BUSINESS_TABLES:
        return "business"
    if table_name in OPERATIONAL_TABLES:
        return "operational"
    return "admin_created"


def _approved_table_names() -> list[str]:
    names = list(get_allowed_tables())
    for table_name in _admin_created_table_names():
        if table_name not in names:
            names.append(table_name)
    return names


def _require_read_access(table_name: str, role: UserRole) -> None:
    """Normal users may read approved business tables only.

    Operations/audit tables and admin-created tables use an explicit admin boundary.
    """

    if table_name in OPERATIONAL_TABLES and role != UserRole.ADMIN:
        raise MCPTableAccessError(
            code="admin_role_required_for_operational_table",
            message=f"Table '{table_name}' contains operational or audit data and can be read only by an admin role.",
        )
    if table_name in _admin_created_table_names() and role != UserRole.ADMIN:
        raise MCPTableAccessError(
            code="admin_role_required_for_admin_created_table",
            message=f"Table '{table_name}' was created through a restricted admin schema workflow and can be read only by an admin role.",
        )


def _resolve_table(db, table_name: str):
    """Reflect live approved columns so Phase 13 ADD COLUMN changes appear immediately."""

    metadata = MetaData()
    return Table(table_name, metadata, autoload_with=db.bind, schema="public")


def list_table_capabilities(*, user_role: str = UserRole.NORMAL_USER.value) -> dict[str, Any]:
    """List approved table names and explain whether the caller can read records."""

    role = parse_mcp_role(user_role)
    tables: list[dict[str, Any]] = []
    table_names = _approved_table_names()
    try:
        with get_session_factory()() as db:
            for table_name in table_names:
                category = _table_category(table_name)
                try:
                    table = _resolve_table(db, table_name)
                    column_count = len(table.columns)
                except SQLAlchemyError:
                    # Existing static metadata remains safe fallback if reflection is not available.
                    column_count = get_table_schema(table_name)["column_count"] if table_name in get_allowed_tables() else 0
                tables.append(
                    {
                        "table_name": table_name,
                        "category": category,
                        "record_read_allowed": category == "business" or role == UserRole.ADMIN,
                        "requires_admin_for_records": category in {"operational", "admin_created"},
                        "column_count": column_count,
                    }
                )
    except SQLAlchemyError:
        # Deterministic fallback used by tests and by the status/catalog endpoint when
        # PostgreSQL is temporarily offline.  It does not expose any dynamic table.
        tables = []
        for table_name in get_allowed_tables():
            category = _table_category(table_name)
            tables.append(
                {
                    "table_name": table_name,
                    "category": category,
                    "record_read_allowed": category == "business" or role == UserRole.ADMIN,
                    "requires_admin_for_records": category == "operational",
                    "column_count": get_table_schema(table_name)["column_count"],
                }
            )
    return {
        "listed": True,
        "table_count": len(tables),
        "tables": tables,
        "user_role": role.value,
        "raw_sql_accepted": False,
    }


def get_bounded_table_records(
    *,
    table_name: str,
    limit: int = 50,
    offset: int = 0,
    user_role: str = UserRole.NORMAL_USER.value,
) -> dict[str, Any]:
    """Read a bounded page from one allowed table without accepting raw SQL."""

    normalized_table = str(table_name).strip().lower()
    role = parse_mcp_role(user_role)
    if normalized_table not in _approved_table_names():
        raise MCPTableAccessError(
            code="table_not_allowed",
            message=f"Table '{normalized_table}' is not an approved application or executed admin-created table.",
        )
    _require_read_access(normalized_table, role)

    try:
        normalized_limit = int(limit)
        normalized_offset = int(offset)
    except (TypeError, ValueError) as exc:
        raise MCPTableAccessError(code="invalid_pagination", message="limit and offset must be integers.") from exc
    if normalized_limit < 1 or normalized_limit > MAX_TABLE_RECORDS:
        raise MCPTableAccessError(code="table_record_limit_invalid", message=f"limit must be between 1 and {MAX_TABLE_RECORDS}.")
    if normalized_offset < 0 or normalized_offset > 100000:
        raise MCPTableAccessError(code="table_record_offset_invalid", message="offset must be between 0 and 100000.")

    try:
        with get_session_factory()() as db:
            table = _resolve_table(db, normalized_table)
            statement = select(*table.columns).limit(normalized_limit).offset(normalized_offset)
            result = db.execute(statement)
            rows = [{key: _safe_value(value) for key, value in row.items()} for row in result.mappings().all()]
    except SQLAlchemyError as exc:
        raise MCPTableAccessError(code="mcp_table_read_failed", message="The bounded table-read MCP tool could not query PostgreSQL.") from exc

    return {
        "retrieved": True,
        "table_name": normalized_table,
        "columns": [column.name for column in table.columns],
        "rows": rows,
        "row_count": len(rows),
        "limit": normalized_limit,
        "offset": normalized_offset,
        "source": {"source_type": "database", "tables": [normalized_table]},
        "raw_sql_accepted": False,
    }
