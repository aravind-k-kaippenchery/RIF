"""Runtime PostgreSQL reflection for first-class dynamic application tables.

This module makes the local PostgreSQL database the source of truth for the
Database Explorer and for safe AI tooling.  Tables that exist in the public schema
are reflected at request time, so tables created in pgAdmin/psql or through the
admin schema workflow can appear without editing SQLAlchemy ORM models.

Safety boundary:
* Only the configured public schema is reflected.
* PostgreSQL catalog/internal schemas are never exposed.
* Operational/audit tables remain distinguishable from business/data tables.
* DML validation, write previews, confirmations, and synthetic generation still use
  the existing app safety gates; this module only supplies the live schema.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from sqlalchemy import MetaData, Table, inspect, select
from sqlalchemy.exc import SQLAlchemyError

from app.db.session import get_session_factory
from app.models import Base

PUBLIC_SCHEMA = "public"

# Migration/system tables that are not useful as app data in the explorer or LLM schema.
EXCLUDED_PUBLIC_TABLES = {
    "alembic_version",
    "spatial_ref_sys",
}


def _normalize_table_name(table_name: str) -> str:
    return str(table_name or "").strip().lower()


def _is_exposed_public_table(table_name: str) -> bool:
    normalized = _normalize_table_name(table_name)
    if not normalized:
        return False
    if normalized in EXCLUDED_PUBLIC_TABLES:
        return False
    if normalized.startswith(("pg_", "sql_")):
        return False
    return True


def _sort_key(name: str) -> tuple[int, str]:
    # Keep the original ORM/application tables near the top, then list reflected additions.
    try:
        order = list(Base.metadata.tables).index(name)
    except ValueError:
        order = 10_000
    return (order, name)


@lru_cache(maxsize=1)
def _cached_public_table_names() -> tuple[str, ...]:
    """Return public table names from PostgreSQL, with a safe ORM fallback.

    The cache prevents repeating schema inspection several times in one request burst.
    Use ``clear_reflection_cache`` after executing DDL.  Uvicorn reload/restart also
    clears the cache naturally.
    """

    try:
        with get_session_factory()() as db:
            inspector = inspect(db.bind)
            names = [name.lower() for name in inspector.get_table_names(schema=PUBLIC_SCHEMA)]
            filtered = [name for name in names if _is_exposed_public_table(name)]
            return tuple(sorted(dict.fromkeys(filtered), key=_sort_key))
    except Exception:
        fallback = [name for name in Base.metadata.tables if _is_exposed_public_table(name)]
        return tuple(sorted(dict.fromkeys(fallback), key=_sort_key))


def clear_reflection_cache() -> None:
    _cached_public_table_names.cache_clear()


def get_public_table_names() -> list[str]:
    return list(_cached_public_table_names())


def has_public_table(table_name: str) -> bool:
    return _normalize_table_name(table_name) in set(get_public_table_names())


def get_runtime_table(table_name: str, bind: Any | None = None) -> Table:
    """Reflect and return one public table.

    If ``bind`` is omitted the function opens a short-lived DB session.  The returned
    object is a SQLAlchemy Core ``Table`` and works with select/insert/update/delete.
    """

    normalized = _normalize_table_name(table_name)
    if not _is_exposed_public_table(normalized):
        raise KeyError(f"Table '{table_name}' is not an exposed public PostgreSQL table.")

    if bind is not None:
        metadata = MetaData()
        # SQLite and other test databases do not have PostgreSQL's ``public`` schema.
        schema = PUBLIC_SCHEMA if bind.dialect.name == "postgresql" else None
        return Table(normalized, metadata, autoload_with=bind, schema=schema)

    try:
        with get_session_factory()() as db:
            metadata = MetaData()
            return Table(normalized, metadata, autoload_with=db.bind, schema=PUBLIC_SCHEMA)
    except SQLAlchemyError as exc:
        # Offline/test fallback for original ORM tables.
        if normalized in Base.metadata.tables:
            return Base.metadata.tables[normalized]
        raise KeyError(f"Table '{table_name}' does not exist in PostgreSQL public schema.") from exc


def get_runtime_columns(table_name: str) -> list[str]:
    try:
        table = get_runtime_table(table_name)
    except KeyError:
        return []
    return [column.name for column in table.columns]


def get_primary_key_columns(table_name: str) -> list[str]:
    try:
        table = get_runtime_table(table_name)
    except KeyError:
        return []
    return [column.name for column in table.primary_key.columns]


def column_to_metadata(table_name: str, column: Any, descriptions: dict[str, dict[str, str]] | None = None) -> dict[str, Any]:
    descriptions = descriptions or {}
    return {
        "name": column.name,
        "type": str(column.type).lower(),
        "nullable": bool(column.nullable),
        "primary_key": bool(column.primary_key),
        "unique": bool(column.unique),
        "indexed": bool(column.index),
        "description": descriptions.get(table_name, {}).get(column.name),
    }


def table_relationships(table_name: str) -> list[dict[str, Any]]:
    try:
        table = get_runtime_table(table_name)
    except KeyError:
        return []
    relationships: list[dict[str, Any]] = []
    public_tables = set(get_public_table_names())
    for column in table.columns:
        for foreign_key in column.foreign_keys:
            target_table = foreign_key.column.table.name
            if target_table not in public_tables:
                continue
            relationships.append(
                {
                    "from_table": table.name,
                    "from_column": column.name,
                    "to_table": target_table,
                    "to_column": foreign_key.column.name,
                    "relationship_type": "many_to_one",
                    "delete_rule": foreign_key.ondelete,
                }
            )
    return relationships


def table_category(table_name: str, *, business_tables: list[str], operational_tables: list[str]) -> str:
    normalized = _normalize_table_name(table_name)
    if normalized in operational_tables:
        return "operational"
    if normalized in business_tables:
        return "business"
    return "business"  # A manually created public table is user business data by default.


def runtime_table_schema(
    table_name: str,
    *,
    business_tables: list[str],
    operational_tables: list[str],
    descriptions: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    normalized = _normalize_table_name(table_name)
    table = get_runtime_table(normalized)
    columns = [column_to_metadata(normalized, column, descriptions) for column in table.columns]
    relationships = table_relationships(normalized)
    category = table_category(normalized, business_tables=business_tables, operational_tables=operational_tables)
    return {
        "table_name": normalized,
        "category": category,
        "columns": columns,
        "column_count": len(columns),
        "primary_key_columns": [column.name for column in table.primary_key.columns],
        "relationships": relationships,
        "allowed_for_llm": category != "operational",
        "schema_source": "postgresql_reflection",
        "is_dynamic_reflection": normalized not in set(business_tables) | set(operational_tables),
    }


def get_public_relationships() -> list[dict[str, Any]]:
    relationships: list[dict[str, Any]] = []
    for table_name in get_public_table_names():
        relationships.extend(table_relationships(table_name))
    return relationships


def safe_preview_select(table: Table, limit: int, offset: int):
    return select(*table.columns).limit(limit).offset(offset)
