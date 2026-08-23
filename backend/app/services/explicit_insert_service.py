"""Deterministic parser for common explicit single-record demo inserts.

This service deliberately handles only narrow, high-confidence INSERT previews used in
RIF demos. It does not execute writes directly; it builds one validated record and then
uses the existing confirmation-gated bulk-insert preview path. Unknown/ambiguous text
returns ``None`` so the normal safe CRUD flow can continue.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any
from uuid import uuid4

from app.services.dynamic_pgsql_schema import get_public_table_names, get_runtime_table, has_public_table
from app.services.schema_registry import OPERATIONAL_TABLES


@dataclass(frozen=True)
class ExplicitInsertRequest:
    target_table: str
    records: list[dict[str, Any]]
    summary: str


def _normalize(value: str) -> str:
    return " ".join(str(value or "").strip().split())


def _slug(value: str, *, fallback: str = "demo") -> str:
    text = re.sub(r"[^a-z0-9]+", ".", str(value or "").casefold()).strip(".")
    return text or fallback


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:8].upper()}"


def _extract_name(text: str, *, entity: str) -> str | None:
    patterns = (
        rf"\b(?:add|create|insert|make)\s+(?:a|an|one|1)?\s*{entity}\s+named\s+(.+?)(?:\s+with\b|\s+from\b|\s+in\b|\s+and\b|,|$)",
        rf"\b{entity}\s+name\s+(?:is\s+)?(.+?)(?:\s+with\b|\s+from\b|\s+in\b|\s+and\b|,|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return " ".join(match.group(1).strip(" .,:;!?'").split())
    return None


def _extract_after(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if not match:
        return None
    return " ".join(match.group(1).strip(" .,:;!?'").split())


def _extract_email(text: str) -> str | None:
    match = re.search(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b", text)
    return match.group(0) if match else None


def _extract_department(text: str) -> str | None:
    value = _extract_after(text, r"\bdepartment\s+(?:is\s+)?([a-zA-Z][a-zA-Z &-]{1,60})(?:,|\s+city\b|\s+and\b|$)")
    if value:
        return value.title().replace("It", "IT").replace("Hr", "HR")
    for item in ("Finance", "IT", "HR", "Sales", "Marketing", "Operations", "Support"):
        if re.search(rf"\b{re.escape(item)}\b", text, flags=re.IGNORECASE):
            return item
    return None


def _extract_city(text: str) -> str | None:
    value = _extract_after(text, r"\bcity\s+(?:is\s+)?([a-zA-Z][a-zA-Z -]{1,60})(?:,|\s+and\b|$)")
    if value:
        return value.title()
    value = _extract_after(text, r"\bfrom\s+([a-zA-Z][a-zA-Z -]{1,60})(?:,|\s+with\b|\s+and\b|$)")
    if value:
        return value.title()
    return None


def _extract_status(text: str) -> str:
    if re.search(r"\binactive\b", text, flags=re.IGNORECASE):
        return "inactive"
    return "active"


def _extract_price(text: str) -> float | None:
    match = re.search(r"\b(?:list\s+price|price)\s+(?:is\s+)?(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)", text, flags=re.IGNORECASE)
    return float(match.group(1)) if match else None


def _extract_product_code(text: str) -> str | None:
    value = _extract_after(text, r"\bproduct\s+code\s+([A-Z0-9_-]{3,40})")
    return value.upper() if value else None


def _extract_category(text: str) -> str | None:
    value = _extract_after(text, r"\bcategory\s+([a-zA-Z][a-zA-Z &-]{1,80})(?:,|\s+list\s+price\b|\s+price\b|\s+and\b|$)")
    return value.title() if value else None


def _split_person_name(value: str) -> tuple[str, str]:
    parts = [part for part in re.split(r"\s+", value.strip()) if part]
    if not parts:
        return "Demo", "User"
    if len(parts) == 1:
        return parts[0].title(), "User"
    return parts[0].title(), " ".join(parts[1:]).title()


def _dynamic_target(text: str) -> str | None:
    """Resolve an explicitly named reflected business table without guessing."""

    lower = text.casefold()
    aliases: list[tuple[str, str]] = []
    for table_name in get_public_table_names():
        if table_name in OPERATIONAL_TABLES:
            continue
        readable = table_name.replace("_", " ")
        candidates = {table_name, readable}
        if readable.endswith("s") and len(readable) > 1:
            candidates.add(readable[:-1])
        for alias in candidates:
            aliases.append((alias, table_name))
    for alias, table_name in sorted(aliases, key=lambda item: len(item[0]), reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", lower):
            return table_name
    return None


def _database_generated(column: Any) -> bool:
    return bool(
        column.identity is not None
        or column.computed is not None
        or column.server_default is not None
        or column.default is not None
        or (column.primary_key and column.autoincrement in (True, "auto"))
    )


def _coerce_literal(value: str, column: Any) -> Any:
    cleaned = value.strip(" \t\r\n,.;!?'\"")
    lowered = cleaned.casefold()
    if lowered in {"null", "none"}:
        return None
    try:
        python_type = column.type.python_type
    except (AttributeError, NotImplementedError):
        python_type = str
    if python_type is bool:
        if lowered in {"true", "yes", "active", "1"}:
            return True
        if lowered in {"false", "no", "inactive", "0"}:
            return False
    if python_type is int:
        try:
            return int(cleaned)
        except ValueError:
            return cleaned
    if python_type is float:
        try:
            return float(cleaned)
        except ValueError:
            return cleaned
    return cleaned


def _extract_dynamic_record(text: str, table_name: str) -> dict[str, Any] | None:
    """Parse only explicit ``column value`` pairs for a reflected table.

    Returning ``None`` on incomplete input deliberately hands the request back to the
    normal validated LLM CRUD path; this parser never invents values for a new schema.
    """

    with_match = re.search(r"\bwith\b(.+)$", text, flags=re.IGNORECASE)
    if not with_match:
        return None
    try:
        table = get_runtime_table(table_name)
    except KeyError:
        return None
    segment = with_match.group(1)
    labels: list[tuple[int, int, Any]] = []
    for column in table.columns:
        aliases = sorted({column.name, column.name.replace("_", " ")}, key=len, reverse=True)
        alias_pattern = "|".join(re.escape(alias) for alias in aliases)
        match = re.search(
            rf"(?<![a-zA-Z0-9_])(?:{alias_pattern})(?![a-zA-Z0-9_])\s*(?:is\s+|=\s*|:\s*)?",
            segment,
            flags=re.IGNORECASE,
        )
        if match:
            labels.append((match.start(), match.end(), column))
    labels.sort(key=lambda item: item[0])
    if not labels:
        return None

    record: dict[str, Any] = {}
    for index, (_, value_start, column) in enumerate(labels):
        value_end = labels[index + 1][0] if index + 1 < len(labels) else len(segment)
        raw = segment[value_start:value_end]
        raw = re.sub(r"^[\s,;]*(?:and\s+)?", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"[\s,;]*(?:and)?\s*$", "", raw, flags=re.IGNORECASE)
        if raw:
            record[column.name] = _coerce_literal(raw, column)

    required = {
        column.name
        for column in table.columns
        if not column.nullable and not _database_generated(column)
    }
    if not required.issubset({key for key, value in record.items() if value not in (None, "")}):
        return None
    return record


def detect_explicit_insert(question: str) -> ExplicitInsertRequest | None:
    text = _normalize(question)
    if not text:
        return None
    lower = text.casefold()
    if not re.search(r"\b(?:add|create|insert|make)\b", lower):
        return None
    # Real generation requests stay in the synthetic-data flow.
    if re.search(r"\b(?:synthetic|random|faker|generate|seed|populate)\b", lower):
        return None

    if has_public_table("employees") and re.search(r"\bemployees?\b", lower):
        name = _extract_name(text, entity="employee")
        email = _extract_email(text)
        if not (name and email):
            return None
        first, last = _split_person_name(name)
        record = {
            "employee_code": _code("EMP-DEMO"),
            "first_name": first,
            "last_name": last,
            "email": email,
            "department": _extract_department(text) or "IT",
            "city": _extract_city(text) or "Bangalore",
            "company_name": "Neolotex",
            "salary": 50000,
            "employment_status": _extract_status(text),
        }
        return ExplicitInsertRequest("employees", [record], f"explicit employee insert for {name}")

    if has_public_table("vendors") and re.search(r"\bvendors?\b", lower):
        name = _extract_name(text, entity="vendor")
        if not name:
            return None
        record = {
            "vendor_code": _code("VEN-DEMO"),
            "vendor_name": name,
            "contact_email": _extract_email(text) or f"{_slug(name)}@example.test",
            "city": _extract_city(text) or "Bangalore",
            "country": "India",
            "category": _extract_category(text) or "Demo Supplier",
            "status": _extract_status(text),
        }
        return ExplicitInsertRequest("vendors", [record], f"explicit vendor insert for {name}")

    if has_public_table("customers") and re.search(r"\bcustomers?\b", lower):
        name = _extract_name(text, entity="customer")
        if not name:
            return None
        record = {
            "customer_code": _code("CUST-DEMO"),
            "customer_name": name,
            "contact_email": _extract_email(text) or f"{_slug(name)}@example.test",
            "city": _extract_city(text) or "Bangalore",
            "country": "India",
            "industry": _extract_category(text) or "Demo",
            "status": _extract_status(text),
        }
        return ExplicitInsertRequest("customers", [record], f"explicit customer insert for {name}")

    if has_public_table("products") and re.search(r"\bproducts?\b", lower):
        name = _extract_after(text, r"\bproduct\s+name\s+(.+?)(?:,|\s+category\b|\s+list\s+price\b|\s+price\b|\s+and\b|$)") or _extract_name(text, entity="product")
        price = _extract_price(text)
        if not (name and price is not None):
            return None
        record = {
            "product_code": _extract_product_code(text) or _code("PROD-DEMO"),
            "product_name": name,
            "category": _extract_category(text) or "Demo Product",
            "description": f"Demo product created from assistant prompt: {name}",
            "list_price": price,
            "is_active": not re.search(r"\binactive\b", lower),
        }
        return ExplicitInsertRequest("products", [record], f"explicit product insert for {name}")

    target_table = _dynamic_target(text)
    if target_table is None:
        return None
    record = _extract_dynamic_record(text, target_table)
    if record is None:
        return None
    return ExplicitInsertRequest(
        target_table,
        [record],
        f"explicit reflected-table insert for {target_table}",
    )
