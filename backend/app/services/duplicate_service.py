"""Duplicate detection helpers for confirmation-based Phase 7 writes.

Duplicate checks are advisory safety checks before a write is proposed. PostgreSQL
unique constraints remain the final concurrency-safe protection at confirmation time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.models import Base


UNIQUE_KEY_SETS: dict[str, list[tuple[str, ...]]] = {
    "employees": [("employee_code",), ("email",), ("phone",)],
    "employee_permissions": [("employee_id", "permission_code")],
    "vendors": [("vendor_code",), ("vendor_name",), ("contact_email",), ("phone",)],
    "customers": [("customer_code",), ("customer_name",), ("contact_email",), ("phone",)],
    "products": [("product_code",), ("product_name",)],
    "product_vendor_mappings": [("product_id", "vendor_id")],
    "sales_deals": [("deal_code",)],
}


@dataclass(frozen=True)
class DuplicateMatch:
    key_fields: tuple[str, ...]
    records: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {"key_fields": list(self.key_fields), "records": self.records}


def json_safe(value: Any) -> Any:
    """Convert SQLAlchemy row values into JSONB-safe primitives."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def row_to_dict(row: Any) -> dict[str, Any]:
    mapping = getattr(row, "_mapping", row)
    return {str(key): json_safe(value) for key, value in dict(mapping).items()}


def candidate_key_sets(table_name: str, values: dict[str, Any]) -> list[tuple[str, ...]]:
    """Return only complete and non-empty unique key candidates for an incoming record."""

    normalized = table_name.strip().lower()
    candidates: list[tuple[str, ...]] = []
    for keys in UNIQUE_KEY_SETS.get(normalized, []):
        if all(values.get(key) not in (None, "") for key in keys):
            candidates.append(keys)
    return candidates


def detect_record_duplicates(
    db: Session,
    *,
    table_name: str,
    values: dict[str, Any],
    exclude_record_id: Any | None = None,
) -> list[DuplicateMatch]:
    """Find rows matching a unique business key.

    The function never deletes/updates data. It is intentionally limited to approved
    business tables and known unique constraints.
    """

    normalized = table_name.strip().lower()
    table = Base.metadata.tables.get(normalized)
    if table is None:
        return []

    matches: list[DuplicateMatch] = []
    for key_fields in candidate_key_sets(normalized, values):
        filters = [table.c[field] == values[field] for field in key_fields if field in table.c]
        if len(filters) != len(key_fields):
            continue
        statement = select(table).where(and_(*filters)).limit(5)
        if exclude_record_id is not None and "id" in table.c:
            statement = statement.where(table.c.id != exclude_record_id)
        rows = [row_to_dict(row) for row in db.execute(statement).mappings().all()]
        if rows:
            matches.append(DuplicateMatch(key_fields=key_fields, records=rows))
    return matches


def find_batch_duplicates(table_name: str, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Detect duplicate unique keys inside the requested bulk batch before PostgreSQL sees it."""

    seen: dict[tuple[str, tuple[str, ...], tuple[str, ...]], int] = {}
    duplicates: list[dict[str, Any]] = []
    normalized = table_name.strip().lower()
    for index, record in enumerate(records):
        for keys in candidate_key_sets(normalized, record):
            fingerprint = tuple(str(record[key]).strip().casefold() for key in keys)
            marker = (normalized, keys, fingerprint)
            if marker in seen:
                duplicates.append(
                    {
                        "first_record_index": seen[marker],
                        "duplicate_record_index": index,
                        "key_fields": list(keys),
                        "key_values": {key: record[key] for key in keys},
                    }
                )
            else:
                seen[marker] = index
    return duplicates
