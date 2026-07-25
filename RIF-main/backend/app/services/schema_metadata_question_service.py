"""Deterministic natural-language answers for database schema metadata questions.

This service handles questions such as ``Do we have an employee table?`` before the
request reaches Ollama or SQL generation. It reads only approved SQLAlchemy/PostgreSQL
metadata, never business rows, and never reuses conversational filters from older turns.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Literal

from sqlalchemy import inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.constants import ResponseStatus, UserRole
from app.services.schema_registry import BUSINESS_TABLES, OPERATIONAL_TABLES, get_allowed_tables


SchemaQuestionKind = Literal["list_tables", "table_exists", "list_columns", "column_exists"]


# Natural-language aliases are deterministic and verified against the live database.
# Aliases never cause a row query and never become guessed SQL targets.
_TABLE_ALIASES: dict[str, tuple[str, ...]] = {
    "employees": ("employees", "employee", "workers", "worker"),
    "employee_experiences": (
        "employee experiences", "employee experience", "work history", "employment history", "experiences", "experience",
    ),
    "employee_permissions": (
        "employee permissions",
        "employee permission",
        "permissions",
        "permission",
    ),
    "vendors": ("vendors", "vendor", "suppliers", "supplier"),
    "customers": ("customers", "customer", "clients", "client"),
    "products": ("products", "product", "items", "item"),
    "product_vendor_mappings": (
        "product vendor mappings",
        "product vendor mapping",
        "vendor mappings",
        "vendor mapping",
    ),
    "sales_deals": ("sales deals", "sales deal", "deals", "deal"),
    "sessions": ("sessions", "session"),
    "pending_actions": ("pending actions", "pending action"),
    "query_logs": ("query logs", "query log"),
    "action_logs": ("action logs", "action log"),
    "change_snapshots": ("change snapshots", "change snapshot"),
    "documents": ("documents", "document"),
    "document_ingestion_jobs": ("document ingestion jobs", "document ingestion job"),
    "benchmark_runs": ("benchmark runs", "benchmark run"),
    "schema_change_requests": ("schema change requests", "schema change request"),
}


@dataclass(frozen=True)
class SchemaQuestionRequest:
    """A deterministic schema-metadata intent extracted from the current turn."""

    handled: bool
    kind: SchemaQuestionKind | None = None
    requested_table: str | None = None
    canonical_table: str | None = None
    requested_column: str | None = None
    ambiguous_tables: tuple[str, ...] = ()

    def to_state(self) -> dict[str, Any]:
        return {
            "handled": self.handled,
            "kind": self.kind,
            "requested_table": self.requested_table,
            "canonical_table": self.canonical_table,
            "requested_column": self.requested_column,
            "ambiguous_tables": list(self.ambiguous_tables),
        }


@dataclass(frozen=True)
class SchemaQuestionResult:
    status: ResponseStatus
    answer: str
    data: dict[str, Any]


class SchemaMetadataQuestionError(RuntimeError):
    def __init__(self, *, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _normalize(value: str) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _phrase_pattern(value: str) -> str:
    parts = [re.escape(item) for item in re.split(r"[\s_-]+", value.strip()) if item]
    return r"[\s_-]+".join(parts)


def _resolve_table_mentions(question: str) -> tuple[str, ...]:
    normalized = _normalize(question)
    matches: list[str] = []
    candidates: list[tuple[int, str, str]] = []
    for table_name in get_allowed_tables():
        aliases = set(_TABLE_ALIASES.get(table_name, ()))
        aliases.add(table_name)
        aliases.add(table_name.replace("_", " "))
        for alias in aliases:
            candidates.append((len(alias), table_name, alias))

    for _, table_name, alias in sorted(candidates, reverse=True):
        if table_name in matches:
            continue
        if re.search(rf"\b{_phrase_pattern(alias)}\b", normalized, flags=re.IGNORECASE):
            matches.append(table_name)
    return tuple(matches)


def _extract_unknown_table_name(question: str) -> str | None:
    normalized = _normalize(question)
    patterns = (
        r"\btable\s+(?:named\s+|called\s+)?([a-z][a-z0-9_-]*)\b",
        r"\b(?:a|an|the)\s+([a-z][a-z0-9_-]*)\s+table\b",
        r"\bhave\s+(?:a|an|the)?\s*(?:table\s+)?([a-z][a-z0-9_-]*)\b",
        r"\bdoes\s+(?:the\s+)?([a-z][a-z0-9_-]*)\s+(?:table\s+)?exist\b",
    )
    ignored = {
        "table",
        "tables",
        "any",
        "available",
        "database",
        "new",
        "random",
        "schema",
    }
    for pattern in patterns:
        match = re.search(pattern, normalized)
        if not match:
            continue
        candidate = match.group(1).strip(" _-")
        if candidate and candidate not in ignored:
            return candidate
    return None


def _extract_column_name(question: str) -> str | None:
    normalized = _normalize(question)
    patterns = (
        r"\b(?:have|has|contain|contains|include|includes)\s+(?:a|an|the)?\s*([a-z][a-z0-9_]*)\s+(?:column|field)\b",
        r"\b(?:have|has|contain|contains|include|includes)\s+(?:a|an|the)?\s*([a-z][a-z0-9_]*)\b",
        r"\bis\s+there\s+(?:a|an|the)?\s*([a-z][a-z0-9_]*)\s+(?:column|field)\b",
        r"\b(?:column|field)\s+(?:named\s+|called\s+)?([a-z][a-z0-9_]*)\b",
    )
    ignored = {"column", "columns", "field", "fields", "table", "schema"}
    for pattern in patterns:
        match = re.search(pattern, normalized)
        if not match:
            continue
        candidate = match.group(1).strip(" _-")
        if candidate and candidate not in ignored:
            return candidate
    return None


def detect_schema_metadata_question(question: str) -> SchemaQuestionRequest:
    """Detect schema intent using only the current user turn.

    The detector intentionally runs before the normal read router. A metadata question
    therefore cannot inherit an old city/department filter and cannot cause row SQL.
    """

    normalized = _normalize(question)
    if not normalized:
        return SchemaQuestionRequest(False)

    table_mentions = _resolve_table_mentions(normalized)

    list_table_patterns = (
        r"^(?:what|which)\s+(?:database\s+)?tables?\s+(?:do\s+we\s+have|are\s+available|exist|are\s+there)",
        r"^(?:list|show|display)\s+(?:all\s+)?(?:available\s+|database\s+)?tables?\??$",
        r"^(?:what|which)\s+tables?\??$",
    )
    if any(re.search(pattern, normalized) for pattern in list_table_patterns):
        return SchemaQuestionRequest(True, kind="list_tables")

    # Table-existence questions must be resolved before the more general
    # ``have/has`` column detector. Without this precedence, a phrase such as
    # ``do we have vendor table`` is incorrectly parsed as asking whether the
    # ``vendors`` table has a ``vendor`` column.
    table_existence_patterns = (
        r"\bdo\s+we\s+have\b.*\btable\b",
        r"\bis\s+there\b.*\btable\b",
        r"\bdoes\b.*\btable\b.*\bexist\b",
        r"\bdoes\b.*\bexist\b.*\btable\b",
        r"\btable\b.*\b(?:exist|available)\b",
    )
    explicit_column_language = bool(
        re.search(
            r"\b(?:column|columns|field|fields|contain|contains|include|includes)\b",
            normalized,
        )
    )
    if (
        not explicit_column_language
        and any(re.search(pattern, normalized) for pattern in table_existence_patterns)
    ):
        if len(table_mentions) > 1:
            return SchemaQuestionRequest(True, kind="table_exists", ambiguous_tables=table_mentions)
        if table_mentions:
            return SchemaQuestionRequest(
                True,
                kind="table_exists",
                requested_table=table_mentions[0].replace("_", " "),
                canonical_table=table_mentions[0],
            )
        return SchemaQuestionRequest(
            True,
            kind="table_exists",
            requested_table=_extract_unknown_table_name(normalized),
        )

    # A column question needs explicit column language, a contain/include verb,
    # or the unambiguous shape ``<table> have <name>``. A bare phrase such as
    # ``do we have employees table`` must never enter this branch.
    table_has_value_shape = bool(
        re.search(
            r"\b(?:does|do)\s+(?!we\b)(?:the\s+)?[a-z][a-z0-9_\s-]*?"
            r"(?:\s+table)?\s+(?:have|has)\s+(?:a|an|the)?\s*"
            r"[a-z][a-z0-9_]*\b",
            normalized,
        )
    )
    column_exists = bool(
        table_mentions
        and re.search(r"\b(?:does|do|has|have|is\s+there)\b", normalized)
        and (explicit_column_language or table_has_value_shape)
    )
    requested_column = _extract_column_name(normalized) if column_exists else None
    if column_exists and requested_column:
        if len(table_mentions) > 1:
            return SchemaQuestionRequest(True, kind="column_exists", requested_column=requested_column, ambiguous_tables=table_mentions)
        return SchemaQuestionRequest(
            True,
            kind="column_exists",
            requested_table=table_mentions[0].replace("_", " "),
            canonical_table=table_mentions[0],
            requested_column=requested_column,
        )

    list_column_patterns = (
        r"\bwhat\s+(?:are\s+the\s+)?(?:columns|fields)\b",
        r"\b(?:list|show|display)\s+(?:the\s+)?(?:columns|fields)\b",
        r"\b(?:describe|show)\s+(?:the\s+)?(?:schema|structure)\b",
        r"\bwhat\s+is\s+(?:the\s+)?(?:schema|structure)\b",
    )
    if any(re.search(pattern, normalized) for pattern in list_column_patterns):
        if len(table_mentions) > 1:
            return SchemaQuestionRequest(True, kind="list_columns", ambiguous_tables=table_mentions)
        if table_mentions:
            return SchemaQuestionRequest(
                True,
                kind="list_columns",
                requested_table=table_mentions[0].replace("_", " "),
                canonical_table=table_mentions[0],
            )
        return SchemaQuestionRequest(True, kind="list_columns", requested_table=_extract_unknown_table_name(normalized))

    return SchemaQuestionRequest(False)


def _visible_tables(role: UserRole) -> list[str]:
    allowed = get_allowed_tables()
    if role == UserRole.ADMIN:
        return allowed
    return [table for table in BUSINESS_TABLES if table in allowed]


def answer_schema_metadata_question(
    db: Session,
    *,
    request: SchemaQuestionRequest,
    role: UserRole,
) -> SchemaQuestionResult:
    """Answer one detected schema question from live PostgreSQL metadata only."""

    if not request.handled or request.kind is None:
        raise ValueError("A handled schema metadata request is required.")

    if request.ambiguous_tables:
        labels = ", ".join(f"`{item}`" for item in request.ambiguous_tables)
        return SchemaQuestionResult(
            status=ResponseStatus.CLARIFICATION_REQUIRED,
            answer=f"I found multiple table references: {labels}. Which one do you mean?",
            data={
                "schema_metadata": {
                    **request.to_state(),
                    "source": "current_turn_and_live_postgresql_metadata",
                    "ollama_called": False,
                    "sql_generated": False,
                    "business_rows_queried": False,
                }
            },
        )

    try:
        inspector = inspect(db.bind)
        live_tables = set(inspector.get_table_names(schema="public"))
    except (SQLAlchemyError, AttributeError, TypeError) as exc:
        raise SchemaMetadataQuestionError(
            code="schema_metadata_unavailable",
            message="The database schema metadata could not be inspected right now.",
        ) from exc

    visible_tables = _visible_tables(role)
    visible_live_tables = [table for table in visible_tables if table in live_tables]

    base = {
        **request.to_state(),
        "source": "live_postgresql_metadata",
        "ollama_called": False,
        "sql_generated": False,
        "business_rows_queried": False,
        "conversation_memory_used": False,
    }

    if request.kind == "list_tables":
        return SchemaQuestionResult(
            status=ResponseStatus.SUCCESS,
            answer=(
                "The available database tables are: " + ", ".join(f"`{item}`" for item in visible_live_tables) + "."
                if visible_live_tables
                else "No approved tables are currently available to your role."
            ),
            data={"schema_metadata": {**base, "tables": visible_live_tables, "table_count": len(visible_live_tables)}},
        )

    canonical = request.canonical_table
    requested = request.requested_table or canonical or "the requested table"

    if canonical is None:
        return SchemaQuestionResult(
            status=ResponseStatus.SUCCESS,
            answer=f"No approved database table named `{requested}` was found.",
            data={"schema_metadata": {**base, "exists": False, "tables": visible_live_tables}},
        )

    if canonical not in visible_tables:
        return SchemaQuestionResult(
            status=ResponseStatus.INFORMATION_NOT_AVAILABLE,
            answer=f"The table `{canonical}` is not available to your current role.",
            data={"schema_metadata": {**base, "exists": False, "access_allowed": False}},
        )

    exists = canonical in live_tables
    if request.kind == "table_exists":
        if exists:
            article = "an" if canonical[:1].lower() in {"a", "e", "i", "o", "u"} else "a"
            return SchemaQuestionResult(
                status=ResponseStatus.SUCCESS,
                answer=f"Yes. The database contains {article} `{canonical}` table.",
                data={"schema_metadata": {**base, "exists": True, "table_name": canonical}},
            )
        return SchemaQuestionResult(
            status=ResponseStatus.SUCCESS,
            answer=f"No. The approved `{canonical}` table is not present in the live database.",
            data={"schema_metadata": {**base, "exists": False, "table_name": canonical}},
        )

    if not exists:
        return SchemaQuestionResult(
            status=ResponseStatus.INFORMATION_NOT_AVAILABLE,
            answer=f"The approved `{canonical}` table is not present in the live database.",
            data={"schema_metadata": {**base, "exists": False, "table_name": canonical}},
        )

    try:
        live_columns = [str(item["name"]) for item in inspector.get_columns(canonical, schema="public")]
    except (SQLAlchemyError, KeyError, TypeError) as exc:
        raise SchemaMetadataQuestionError(
            code="schema_columns_unavailable",
            message=f"The columns for `{canonical}` could not be inspected right now.",
        ) from exc

    if request.kind == "list_columns":
        return SchemaQuestionResult(
            status=ResponseStatus.SUCCESS,
            answer=f"The `{canonical}` table contains these columns: {', '.join(f'`{item}`' for item in live_columns)}.",
            data={
                "schema_metadata": {
                    **base,
                    "exists": True,
                    "table_name": canonical,
                    "columns": live_columns,
                    "column_count": len(live_columns),
                }
            },
        )

    requested_column = str(request.requested_column or "").strip().lower()
    column_exists = requested_column in {item.lower() for item in live_columns}
    return SchemaQuestionResult(
        status=ResponseStatus.SUCCESS,
        answer=(
            f"Yes. The `{canonical}` table contains a `{requested_column}` column."
            if column_exists
            else f"No. The `{canonical}` table does not contain a `{requested_column}` column."
        ),
        data={
            "schema_metadata": {
                **base,
                "exists": True,
                "table_name": canonical,
                "requested_column": requested_column,
                "column_exists": column_exists,
                "columns": live_columns,
            }
        },
    )
