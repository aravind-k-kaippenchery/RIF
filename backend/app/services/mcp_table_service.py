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

from sqlalchemy import MetaData, Table, UniqueConstraint, func, select
from sqlalchemy.exc import SQLAlchemyError

from app.core.constants import UserRole
from app.db.session import get_session_factory
from app.models import Base
from app.models.operations import SchemaChangeRequest
from app.services.schema_registry import BUSINESS_TABLES, OPERATIONAL_TABLES, get_allowed_tables, get_table_schema

MAX_TABLE_RECORDS = 500


def _reflected_unique_constraints(table: Table) -> list[dict[str, Any]]:
    """Expose real PostgreSQL uniqueness metadata for duplicate tooling."""

    constraints: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for constraint in table.constraints:
        if not isinstance(constraint, UniqueConstraint):
            continue
        columns = tuple(column.name for column in constraint.columns)
        if columns and columns not in seen:
            constraints.append({"name": constraint.name, "columns": list(columns)})
            seen.add(columns)
    for index in table.indexes:
        if not index.unique:
            continue
        columns = tuple(column.name for column in index.columns)
        if columns and columns not in seen:
            constraints.append({"name": index.name, "columns": list(columns)})
            seen.add(columns)
    for column in table.columns:
        columns = (column.name,)
        if column.unique and columns not in seen:
            constraints.append({"name": None, "columns": [column.name]})
            seen.add(columns)
    return constraints


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
    limit: int = 100,
    offset: int = 0,
    user_role: str = UserRole.NORMAL_USER.value,
) -> dict[str, Any]:
    """Read one bounded page from an allowed table and report the real total count.

    This endpoint is used by the frontend Data Explorer.  It intentionally remains
    bounded and role-aware, but it no longer makes the table appear to contain only
    the current page.  The UI receives rows for this page plus total_row_count,
    next_offset, and has_more so users can browse records beyond the first page.
    """

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
            total_row_count = int(db.scalar(select(func.count()).select_from(table)) or 0)
            order_column = table.c.get("id")
            if order_column is None:
                order_column = next(iter(table.columns))
            statement = (
                select(*table.columns)
                .order_by(order_column.asc())
                .limit(normalized_limit)
                .offset(normalized_offset)
            )
            result = db.execute(statement)
            rows = [{key: _safe_value(value) for key, value in row.items()} for row in result.mappings().all()]
    except SQLAlchemyError as exc:
        raise MCPTableAccessError(code="mcp_table_read_failed", message="The bounded table-read MCP tool could not query PostgreSQL.") from exc

    returned_count = len(rows)
    next_offset = normalized_offset + returned_count
    has_more = next_offset < total_row_count
    return {
        "retrieved": True,
        "table_name": normalized_table,
        "columns": [column.name for column in table.columns],
        "rows": rows,
        "row_count": returned_count,
        "returned_row_count": returned_count,
        "total_row_count": total_row_count,
        "limit": normalized_limit,
        "offset": normalized_offset,
        "next_offset": next_offset if has_more else None,
        "previous_offset": max(0, normalized_offset - normalized_limit) if normalized_offset > 0 else None,
        "has_more": has_more,
        "source": {"source_type": "database", "tables": [normalized_table]},
        "raw_sql_accepted": False,
    }

# BEGIN RIF FULL PGSQL MCP TABLE OVERRIDE
# Runtime PostgreSQL reflection override: /api/tables mirrors public PostgreSQL tables.
from sqlalchemy import func as _rif_func
from app.services.dynamic_pgsql_schema import (
    get_public_table_names as _rif_get_public_table_names,
    get_runtime_table as _rif_get_runtime_table,
    has_public_table as _rif_has_public_table,
    clear_reflection_cache as _rif_clear_reflection_cache,
)

MAX_TABLE_RECORDS = 500


def _table_category(table_name: str) -> str:  # type: ignore[override]
    if table_name in BUSINESS_TABLES:
        return "business"
    if table_name in OPERATIONAL_TABLES:
        return "operational"
    return "business"


def _approved_table_names() -> list[str]:  # type: ignore[override]
    _rif_clear_reflection_cache()
    return _rif_get_public_table_names()


def _require_read_access(table_name: str, role: UserRole) -> None:  # type: ignore[override]
    if table_name in OPERATIONAL_TABLES and role != UserRole.ADMIN:
        raise MCPTableAccessError(
            code="admin_role_required_for_operational_table",
            message=f"Table '{table_name}' contains operational or audit data and can be read only by an admin role.",
        )


def list_table_capabilities(*, user_role: str = UserRole.NORMAL_USER.value) -> dict[str, Any]:  # type: ignore[override]
    role = parse_mcp_role(user_role)
    tables: list[dict[str, Any]] = []
    with get_session_factory()() as db:
        for table_name in _approved_table_names():
            category = _table_category(table_name)
            try:
                table = _rif_get_runtime_table(table_name, bind=db.bind)
                column_count = len(table.columns)
                primary_key_columns = [column.name for column in table.primary_key.columns]
                unique_constraints = _reflected_unique_constraints(table)
            except Exception:
                column_count = 0
                primary_key_columns = []
                unique_constraints = []
            admin_required = category == "operational"
            tables.append(
                {
                    "table_name": table_name,
                    "category": category,
                    "record_read_allowed": (not admin_required) or role == UserRole.ADMIN,
                    "requires_admin_for_records": admin_required,
                    "column_count": column_count,
                    "primary_key_columns": primary_key_columns,
                    "unique_constraints": unique_constraints,
                    "schema_source": "postgresql_reflection",
                    "dynamic_postgresql_table": table_name not in BUSINESS_TABLES and table_name not in OPERATIONAL_TABLES,
                }
            )
    return {
        "listed": True,
        "table_count": len(tables),
        "tables": tables,
        "user_role": role.value,
        "raw_sql_accepted": False,
        "schema_source": "postgresql_reflection",
    }


def get_bounded_table_records(  # type: ignore[override]
    *,
    table_name: str,
    limit: int = 100,
    offset: int = 0,
    user_role: str = UserRole.NORMAL_USER.value,
    filters: dict[str, Any] | None = None,
    relationship_filter: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_table = str(table_name).strip().lower()
    role = parse_mcp_role(user_role)
    if not _rif_has_public_table(normalized_table):
        raise MCPTableAccessError(
            code="table_not_allowed",
            message=f"Table '{normalized_table}' does not exist in the reflected PostgreSQL public schema.",
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
            table = _rif_get_runtime_table(normalized_table, bind=db.bind)
            normalized_filters = dict(filters or {})
            if len(normalized_filters) > 5:
                raise MCPTableAccessError(
                    code="too_many_table_filters",
                    message="At most five reflected-column equality filters are allowed.",
                )
            conditions = []
            applied_filters: dict[str, Any] = {}
            for raw_column, raw_value in normalized_filters.items():
                column_name = str(raw_column or "").strip().lower()
                column = table.c.get(column_name)
                if column is None:
                    raise MCPTableAccessError(
                        code="table_filter_column_not_found",
                        message=f"Column '{column_name}' does not exist in table '{normalized_table}'.",
                    )
                if isinstance(raw_value, (dict, list, tuple, set)):
                    raise MCPTableAccessError(
                        code="table_filter_value_invalid",
                        message="Table filter values must be scalar literals.",
                    )
                try:
                    python_type = column.type.python_type
                except (AttributeError, NotImplementedError):
                    python_type = None
                if python_type is str and raw_value is not None:
                    conditions.append(_rif_func.lower(column) == str(raw_value).casefold())
                elif raw_value is None:
                    conditions.append(column.is_(None))
                else:
                    conditions.append(column == raw_value)
                applied_filters[column_name] = _safe_value(raw_value)

            from_clause = table
            applied_relationship_filter: dict[str, Any] = {}
            relationship_payload = dict(relationship_filter or {})
            if relationship_payload:
                if set(relationship_payload) != {"parent_table", "parent_column", "parent_value"}:
                    raise MCPTableAccessError(
                        code="relationship_filter_invalid",
                        message="A relationship filter requires parent_table, parent_column, and parent_value only.",
                    )
                parent_table_name = str(relationship_payload.get("parent_table") or "").strip().lower()
                parent_column_name = str(relationship_payload.get("parent_column") or "").strip().lower()
                parent_value = relationship_payload.get("parent_value")
                if not _rif_has_public_table(parent_table_name):
                    raise MCPTableAccessError(
                        code="relationship_parent_table_not_found",
                        message=f"Parent table '{parent_table_name}' does not exist in the reflected PostgreSQL public schema.",
                    )
                _require_read_access(parent_table_name, role)
                if isinstance(parent_value, (dict, list, tuple, set)):
                    raise MCPTableAccessError(
                        code="relationship_parent_value_invalid",
                        message="The parent lookup value must be a scalar literal.",
                    )
                parent_table = _rif_get_runtime_table(parent_table_name, bind=db.bind)
                parent_column = parent_table.c.get(parent_column_name)
                if parent_column is None:
                    raise MCPTableAccessError(
                        code="relationship_parent_column_not_found",
                        message=f"Column '{parent_column_name}' does not exist in parent table '{parent_table_name}'.",
                    )
                foreign_key_matches = []
                for child_column in table.columns:
                    for foreign_key in child_column.foreign_keys:
                        if foreign_key.column.table.name == parent_table_name:
                            foreign_key_matches.append((child_column, foreign_key.column.name))
                if len(foreign_key_matches) != 1:
                    raise MCPTableAccessError(
                        code="relationship_path_ambiguous",
                        message=(
                            f"Table '{normalized_table}' does not have exactly one direct reflected foreign key "
                            f"to '{parent_table_name}'."
                        ),
                    )
                child_foreign_key, referenced_parent_column_name = foreign_key_matches[0]
                referenced_parent_column = parent_table.c.get(referenced_parent_column_name)
                if referenced_parent_column is None:
                    raise MCPTableAccessError(
                        code="relationship_reference_invalid",
                        message="The reflected foreign-key target column is unavailable.",
                    )
                from_clause = table.join(parent_table, child_foreign_key == referenced_parent_column)
                try:
                    parent_python_type = parent_column.type.python_type
                except (AttributeError, NotImplementedError):
                    parent_python_type = None
                if parent_value is None:
                    conditions.append(parent_column.is_(None))
                elif parent_python_type is str:
                    conditions.append(_rif_func.lower(parent_column) == str(parent_value).casefold())
                else:
                    conditions.append(parent_column == parent_value)
                applied_relationship_filter = {
                    "parent_table": parent_table_name,
                    "parent_column": parent_column_name,
                    "parent_value": _safe_value(parent_value),
                    "child_foreign_key": child_foreign_key.name,
                    "referenced_parent_column": referenced_parent_column_name,
                }

            count_statement = select(_rif_func.count()).select_from(from_clause)
            if conditions:
                count_statement = count_statement.where(*conditions)
            total_row_count = int(db.scalar(count_statement) or 0)
            order_columns = list(table.primary_key.columns)
            if not order_columns and list(table.columns):
                order_columns = [next(iter(table.columns))]
            statement = select(*table.columns).select_from(from_clause)
            if conditions:
                statement = statement.where(*conditions)
            if order_columns:
                statement = statement.order_by(*(column.asc() for column in order_columns))
            statement = statement.limit(normalized_limit).offset(normalized_offset)
            rows = [{key: _safe_value(value) for key, value in row.items()} for row in db.execute(statement).mappings().all()]
    except SQLAlchemyError as exc:
        raise MCPTableAccessError(code="mcp_table_read_failed", message="The bounded table-read MCP tool could not query PostgreSQL.") from exc

    return {
        "retrieved": True,
        "table_name": normalized_table,
        "columns": [column.name for column in table.columns],
        "rows": rows,
        "row_count": len(rows),
        "total_row_count": total_row_count,
        "has_more": normalized_offset + len(rows) < total_row_count,
        "limit": normalized_limit,
        "offset": normalized_offset,
        "source": {
            "source_type": "database",
            "tables": [normalized_table]
            + ([applied_relationship_filter["parent_table"]] if applied_relationship_filter else []),
        },
        "raw_sql_accepted": False,
        "schema_source": "postgresql_reflection",
        "applied_filters": applied_filters,
        "applied_relationship_filter": applied_relationship_filter,
    }
# END RIF FULL PGSQL MCP TABLE OVERRIDE
