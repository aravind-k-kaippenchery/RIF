"""Deterministic ambiguity gate for natural-language database requests.

This module runs before Ollama.  Its job is not to understand every possible sentence;
its job is to reject requests whose target or requested change is not explicit enough to
be safe.  A rejected request becomes a normal ``clarification_required`` assistant
response with no SQL proposal, no pending action, and no database access.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from app.services.schema_registry import BUSINESS_TABLES


_WRITE_VERBS = {
    "add",
    "create",
    "insert",
    "generate",
    "seed",
    "populate",
    "make",
    "update",
    "delete",
    "remove",
    "change",
    "modify",
    "set",
    "rename",
}
_READ_VERBS = {
    "show",
    "list",
    "display",
    "view",
    "see",
    "get",
    "find",
    "fetch",
    "count",
    "search",
    "which",
    "what",
    "who",
}
_GENERATION_SIGNALS = {
    "random",
    "randomly",
    "randomized",
    "synthetic",
    "fake",
    "faker",
    "sample",
    "demo",
    "generated",
    "generate",
    "seed",
    "populate",
}
_DOCUMENT_TERMS = {
    "brochure",
    "document",
    "documents",
    "manual",
    "specification",
    "specifications",
    "warranty",
    "policy",
    "pdf",
    "docx",
    "upload",
    "scanned",
    "image",
    "contract",
}
_REFERENCE_TERMS = {"it", "them", "those", "that", "one", "ones", "previous", "last", "new"}
_GENERIC_DATA_TERMS = {"data", "record", "records", "row", "rows", "table", "tables", "entry", "entries"}

# Deliberately explicit.  A future table must be approved in BUSINESS_TABLES before it
# can be resolved here or sent to Faker/SQL generation.
_TABLE_ALIASES: dict[str, tuple[str, ...]] = {
    "employee_experiences": (
        "employee experiences", "employee experience", "work history", "employment history",
        "previous jobs", "previous job", "experiences", "experience",
    ),
    "product_vendor_mappings": (
        "product vendor mappings",
        "product vendor mapping",
        "product-vendor mappings",
        "product-vendor mapping",
        "vendor mappings",
        "vendor mapping",
        "supplier mappings",
        "supplier mapping",
    ),
    "employee_permissions": (
        "employee permissions",
        "employee permission",
        "permission records",
        "permission record",
        "permissions",
        "permission",
    ),
    "sales_deals": (
        "sales deals",
        "sales deal",
        "sales opportunities",
        "sales opportunity",
        "opportunities",
        "opportunity",
        "deals",
        "deal",
    ),
    "employees": (
        "employees",
        "employee",
        "workers",
        "worker",
        "staff members",
        "staff member",
        "staff",
        "people",
        "persons",
        "person",
    ),
    "vendors": ("vendors", "vendor", "suppliers", "supplier"),
    "customers": ("customers", "customer", "clients", "client"),
    "products": ("products", "product", "items", "item"),
}


@dataclass(frozen=True)
class ClarificationDecision:
    """Result of the deterministic pre-LLM clarity check."""

    needs_clarification: bool
    code: str | None = None
    message: str | None = None
    missing_fields: tuple[str, ...] = ()
    resolved_tables: tuple[str, ...] = ()
    detected_intent: str = "unknown"
    details: list[dict[str, Any]] = field(default_factory=list)

    def to_data(self) -> dict[str, Any]:
        return {
            "clarification_code": self.code,
            "missing_fields": list(self.missing_fields),
            "resolved_tables": list(self.resolved_tables),
            "detected_intent": self.detected_intent,
            "ollama_called": False,
            "sql_generated": False,
            "database_touched": False,
            "details": list(self.details),
        }


def _tokens(question: str) -> set[str]:
    return set(re.findall(r"[a-z0-9_-]+", question.casefold()))


def _phrase_pattern(value: str) -> str:
    parts = [re.escape(item) for item in re.split(r"[\s_-]+", value.strip()) if item]
    return r"[\s_-]+".join(parts)


def resolve_business_tables(question: str) -> tuple[str, ...]:
    """Resolve only explicitly mentioned approved business tables.

    More-specific aliases are considered first.  Multiple tables are preserved for
    relationship queries; no default table is ever injected.
    """

    normalized = " ".join(question.casefold().split())
    matched: list[str] = []

    candidates: list[tuple[int, str, str]] = []
    for table_name in BUSINESS_TABLES:
        aliases = set(_TABLE_ALIASES.get(table_name, ()))
        aliases.add(table_name)
        aliases.add(table_name.replace("_", " "))
        for alias in aliases:
            candidates.append((len(alias), table_name, alias))

    for _, table_name, alias in sorted(candidates, reverse=True):
        if table_name in matched:
            continue
        if re.search(rf"\b{_phrase_pattern(alias)}\b", normalized, flags=re.IGNORECASE):
            matched.append(table_name)

    return tuple(matched)


def _schema_creation_requested(question: str) -> bool:
    normalized = " ".join(question.casefold().split())
    patterns = (
        r"\b(?:create|make|add)\s+(?:(?:one|1|a|an)\s+)?(?:new\s+|random\s+|synthetic\s+)?tables?\b",
        r"\bcreate\s+tables?\s+(?:named|called)\b",
        r"\bnew\s+table\b",
    )
    return any(re.search(pattern, normalized) for pattern in patterns)


def _explicit_unknown_table(question: str) -> str | None:
    normalized = " ".join(question.casefold().split())
    patterns = (
        r"\b(?:in|into|from|inside|on|for)\s+(?:the\s+)?([a-z][a-z0-9_-]*)\s+table\b",
        r"\btable\s+(?:named\s+|called\s+)?([a-z][a-z0-9_-]*)\b",
    )
    ignored = {"a", "an", "the", "new", "random", "synthetic", "one", "existing"}
    approved_words = {table.casefold() for table in BUSINESS_TABLES}
    approved_words |= {table.replace("_", " ").casefold() for table in BUSINESS_TABLES}
    for pattern in patterns:
        match = re.search(pattern, normalized)
        if not match:
            continue
        candidate = match.group(1).strip(" _-")
        if candidate and candidate not in ignored and candidate not in approved_words:
            return candidate
    return None


def _obviously_empty_insert(question: str, resolved_tables: tuple[str, ...]) -> bool:
    """Detect only clearly underspecified inserts; do not pretend to parse all values."""

    if not resolved_tables:
        return True
    normalized = " ".join(question.casefold().split())
    # Explicit field/value syntax is clear enough to continue to the existing validator.
    if any(marker in normalized for marker in (" with ", " named ", " name ", " email ", " code ", " = ", "'", '"')):
        return False
    if re.search(r"\b[A-Z0-9]+-[A-Z0-9-]+\b", question):
        return False

    # Short commands such as "add an employee" or "insert record into vendors" contain
    # no supplied business values and would force the model to invent them.
    token_count = len(re.findall(r"[a-z0-9_-]+", normalized))
    return token_count <= 5 or bool(
        re.fullmatch(
            r"(?:please\s+)?(?:add|create|insert|make)\s+(?:(?:a|an|one|1|some|new)\s+)?(?:record\s+(?:in|into)\s+)?[a-z0-9 _-]+(?:\s+table)?",
            normalized,
        )
    )


def _has_record_selector(question: str) -> bool:
    normalized = question.casefold()
    patterns = (
        r"\bwhere\b",
        r"\ball\b",
        r"\b(?:id|code|email)\s*(?:=|is|:)?\s*[a-z0-9@._-]+",
        r"\b(?:EMP|VEN|CUST|PROD)-[A-Z0-9-]+\b",
        r"\bfrom\s+[a-z][a-z ._-]+",
        r"\bin\s+[a-z][a-z ._-]+",
    )
    return any(re.search(pattern, question, flags=re.IGNORECASE) for pattern in patterns)


def _has_update_value(question: str) -> bool:
    normalized = question.casefold()
    return bool(
        re.search(r"\b(?:to|as)\s+[^\s]+", normalized)
        or re.search(r"\bset\s+[a-z_ ]+\s*=", normalized)
        or "=" in normalized
    )


def analyze_request_clarity(question: str) -> ClarificationDecision:
    """Return a clarification decision before any LLM call or SQL generation."""

    normalized = " ".join(str(question or "").strip().split())
    if not normalized:
        return ClarificationDecision(
            True,
            code="empty_request",
            message="Please enter a database or document request.",
            missing_fields=("request",),
        )

    tokens = _tokens(normalized)
    resolved_tables = resolve_business_tables(normalized)
    has_write = bool(tokens & _WRITE_VERBS)
    has_read = bool(tokens & _READ_VERBS)
    has_generation = bool(tokens & _GENERATION_SIGNALS)
    has_document = bool(tokens & _DOCUMENT_TERMS)

    if _schema_creation_requested(normalized):
        return ClarificationDecision(
            True,
            code="schema_definition_required",
            message=(
                "Creating a new table is a schema change, so I will not guess its structure. "
                "Please provide the table name and each column with its data type. After the table is safely created and approved, "
                "ask me to generate the required number of synthetic rows for that table."
            ),
            missing_fields=("table_name", "columns", "column_data_types"),
            detected_intent="schema_change",
            details=[{"requested_follow_up": "Provide an explicit table schema; no employee table or SQL fallback was used."}],
        )

    if has_write and has_generation:
        if not resolved_tables:
            unknown = _explicit_unknown_table(normalized)
            if unknown:
                message = (
                    f"I could not find an approved business table named '{unknown}'. "
                    "Please choose an existing approved table or create and approve that table schema first."
                )
            else:
                message = (
                    "Which existing business table should receive the synthetic records? "
                    f"Available targets are: {', '.join(BUSINESS_TABLES)}."
                )
            return ClarificationDecision(
                True,
                code="synthetic_target_table_required",
                message=message,
                missing_fields=("target_table",),
                detected_intent="synthetic_generation",
                details=[{"approved_business_tables": list(BUSINESS_TABLES)}],
            )
        # Synthetic generation has its own deterministic count/relationship validation.
        return ClarificationDecision(False, resolved_tables=(resolved_tables[0],), detected_intent="synthetic_generation")

    if has_write:
        if not resolved_tables:
            unknown = _explicit_unknown_table(normalized)
            message = (
                f"The table '{unknown}' is not an approved business table. Please specify an approved existing table."
                if unknown
                else "Which business table do you want to change?"
            )
            return ClarificationDecision(
                True,
                code="write_target_table_required",
                message=message,
                missing_fields=("target_table",),
                detected_intent="crud_write",
                details=[{"approved_business_tables": list(BUSINESS_TABLES)}],
            )

        if tokens & {"delete", "remove"} and not _has_record_selector(normalized):
            return ClarificationDecision(
                True,
                code="delete_record_selector_required",
                message="Which exact record or records should be deleted? Provide an ID, business code, or an explicit filter.",
                missing_fields=("record_selector",),
                resolved_tables=resolved_tables,
                detected_intent="delete",
            )

        if tokens & {"update", "change", "modify", "set", "rename"}:
            missing: list[str] = []
            if not _has_record_selector(normalized):
                missing.append("record_selector")
            if not _has_update_value(normalized):
                missing.extend(("field", "new_value"))
            if missing:
                return ClarificationDecision(
                    True,
                    code="update_details_required",
                    message=(
                        "Please identify the exact record, the field to change, and the new value. "
                        "For example: Change the city of EMP-101 to Kochi."
                    ),
                    missing_fields=tuple(dict.fromkeys(missing)),
                    resolved_tables=resolved_tables,
                    detected_intent="update",
                )

        if tokens & {"add", "create", "insert", "make"} and _obviously_empty_insert(normalized, resolved_tables):
            return ClarificationDecision(
                True,
                code="insert_values_required",
                message=(
                    f"What values should be inserted into '{resolved_tables[0]}'? "
                    "Provide the record fields explicitly, or say that you want a specific number of random/synthetic records."
                ),
                missing_fields=("record_values",),
                resolved_tables=resolved_tables,
                detected_intent="insert",
            )

        return ClarificationDecision(False, resolved_tables=resolved_tables, detected_intent="crud_write")

    if has_document:
        return ClarificationDecision(False, resolved_tables=resolved_tables, detected_intent="document_or_hybrid")

    if resolved_tables:
        return ClarificationDecision(False, resolved_tables=resolved_tables, detected_intent="structured_read")

    if tokens & _REFERENCE_TERMS or has_read or tokens & _GENERIC_DATA_TERMS:
        return ClarificationDecision(
            True,
            code="read_target_required",
            message=(
                "I do not have a clear table or record set to display. "
                "Please specify the table, records, or filter you mean."
            ),
            missing_fields=("target_table_or_context",),
            detected_intent="structured_read",
        )

    return ClarificationDecision(
        True,
        code="request_intent_unclear",
        message=(
            "Please state whether you want to read, create, update, delete, generate synthetic data, or ask about an uploaded document, "
            "and include the target table or document."
        ),
        missing_fields=("operation", "target"),
        detected_intent="unknown",
    )


def assert_generated_tables_match(*, expected_tables: tuple[str, ...] | list[str], generated_tables: list[str]) -> None:
    """Reject model SQL that omits the table explicitly requested by the user."""

    expected = {str(item).strip().lower() for item in expected_tables if str(item).strip()}
    generated = {str(item).strip().lower() for item in generated_tables if str(item).strip()}
    if expected and not expected.issubset(generated):
        raise ValueError(
            "The generated SQL did not target the table explicitly requested by the user."
        )
