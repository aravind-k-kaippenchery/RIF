"""Deterministic handling for simple natural-language table row requests.

Requests such as ``show me vendors table data`` do not need an LLM.  This
service resolves one approved business table, calls the bounded MCP table-read
tool, and returns real rows.  Bare phrases such as ``vendors table`` remain
ambiguous and produce a clarification instead of guessing whether the user
wants rows or schema metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from app.core.constants import ResponseStatus, UserRole
from app.mcp.client import LocalMCPClient
from app.services.schema_registry import BUSINESS_TABLES


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
}

_DISPLAY_VERBS = ("show", "shoe", "list", "view", "display", "get", "give")
_DATA_NOUNS = ("data", "records", "rows", "entries")


@dataclass(frozen=True)
class TableRecordQuestion:
    handled: bool
    canonical_table: str | None = None
    needs_clarification: bool = False
    clarification_message: str | None = None
    limit: int = 50

    def to_state(self) -> dict[str, Any]:
        return {
            "handled": self.handled,
            "canonical_table": self.canonical_table,
            "needs_clarification": self.needs_clarification,
            "clarification_message": self.clarification_message,
            "limit": self.limit,
        }


@dataclass(frozen=True)
class TableRecordResult:
    status: ResponseStatus
    answer: str
    data: dict[str, Any]


class TableRecordQuestionError(RuntimeError):
    def __init__(self, *, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _normalize(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9_\s-]+", " ", str(value or "").casefold())
    return " ".join(normalized.split())


def _phrase_pattern(value: str) -> str:
    parts = [re.escape(part) for part in re.split(r"[\s_-]+", value) if part]
    return r"[\s_-]+".join(parts)


def _resolve_business_tables(question: str) -> tuple[str, ...]:
    normalized = _normalize(question)
    found: list[str] = []
    candidates: list[tuple[int, str, str]] = []
    for table_name in BUSINESS_TABLES:
        aliases = set(_TABLE_ALIASES.get(table_name, ()))
        aliases.add(table_name)
        aliases.add(table_name.replace("_", " "))
        for alias in aliases:
            candidates.append((len(alias), table_name, alias))

    for _, table_name, alias in sorted(candidates, reverse=True):
        if table_name in found:
            continue
        if re.search(rf"\b{_phrase_pattern(alias)}\b", normalized):
            found.append(table_name)
    return tuple(found)


def _extract_limit(question: str) -> int:
    normalized = _normalize(question)
    match = re.search(r"\b(?:top|first|show|list|view|get)\s+(\d{1,3})\b", normalized)
    if not match:
        return 50
    return max(1, min(100, int(match.group(1))))


def detect_table_record_question(question: str) -> TableRecordQuestion:
    """Classify only simple whole-table row requests.

    Filtered, aggregate, joined, or conditional questions deliberately return
    ``handled=False`` and continue to the normal validated natural-language SQL
    path.  Ambiguous bare table phrases are stopped for clarification.
    """

    normalized = _normalize(question)
    if not normalized:
        return TableRecordQuestion(False)

    tables = _resolve_business_tables(normalized)
    if len(tables) != 1:
        return TableRecordQuestion(False)
    table_name = tables[0]

    # Do not intercept schema questions; the dedicated metadata service owns them.
    if re.search(r"\b(?:column|columns|field|fields|schema|structure|exist|exists)\b", normalized):
        return TableRecordQuestion(False)

    # Any explicit filter/aggregation/join language requires normal NL-to-SQL.
    if re.search(
        r"\b(?:from|where|whose|with|without|in|at|based|living|above|below|under|over|"
        r"greater|less|between|highest|lowest|average|avg|sum|count|total|join|related|"
        r"active|inactive|status|salary|price|department|city|category|country)\b",
        normalized,
    ):
        return TableRecordQuestion(False)

    aliases = sorted(
        set(_TABLE_ALIASES.get(table_name, ()))
        | {table_name, table_name.replace("_", " ")},
        key=len,
        reverse=True,
    )
    alias_pattern = "(?:" + "|".join(_phrase_pattern(alias) for alias in aliases) + ")"
    verb_pattern = "(?:" + "|".join(_DISPLAY_VERBS) + ")"
    data_pattern = "(?:" + "|".join(_DATA_NOUNS) + ")"

    explicit_patterns = (
        rf"^(?:please\s+)?{verb_pattern}(?:\s+me)?(?:\s+the)?\s+{alias_pattern}(?:\s+table)?(?:\s+{data_pattern})?$",
        rf"^(?:please\s+)?{verb_pattern}(?:\s+me)?(?:\s+the)?\s+{data_pattern}\s+(?:from|of)\s+(?:the\s+)?{alias_pattern}(?:\s+table)?$",
        rf"^(?:please\s+)?{verb_pattern}\s+\d{{1,3}}\s+{alias_pattern}(?:\s+{data_pattern})?$",
    )
    if any(re.fullmatch(pattern, normalized) for pattern in explicit_patterns):
        return TableRecordQuestion(
            True,
            canonical_table=table_name,
            limit=_extract_limit(normalized),
        )

    ambiguous_patterns = (
        rf"^(?:the\s+)?{alias_pattern}(?:\s+table)?$",
        rf"^what\s+about\s+(?:the\s+)?{alias_pattern}(?:\s+table)?$",
        rf"^and\s+(?:the\s+)?{alias_pattern}(?:\s+table)?$",
    )
    if any(re.fullmatch(pattern, normalized) for pattern in ambiguous_patterns):
        readable = table_name.replace("_", " ")
        return TableRecordQuestion(
            True,
            canonical_table=table_name,
            needs_clarification=True,
            clarification_message=(
                f"Do you want to view rows from the `{readable}` table, or inspect its columns and structure?"
            ),
        )

    return TableRecordQuestion(False)


def read_table_records(
    *,
    request: TableRecordQuestion,
    user_role: UserRole,
    mcp_client: LocalMCPClient | None = None,
) -> TableRecordResult:
    """Read one approved business table through the bounded MCP tool only."""

    if not request.handled or request.needs_clarification or not request.canonical_table:
        raise TableRecordQuestionError(
            code="table_record_request_invalid",
            message="The table-record request is incomplete or ambiguous.",
        )

    client = mcp_client or LocalMCPClient()
    outcome = client.call_tool(
        "get_table_records",
        {
            "table_name": request.canonical_table,
            "limit": request.limit,
            "offset": 0,
            "user_role": user_role.value,
        },
    )
    if not outcome.get("ok"):
        raise TableRecordQuestionError(
            code=str(outcome.get("error_code") or "mcp_table_read_failed"),
            message=str(outcome.get("error_message") or "The bounded table read could not be completed."),
        )

    payload = outcome.get("result")
    if not isinstance(payload, dict) or not payload.get("retrieved"):
        raise TableRecordQuestionError(
            code=str(payload.get("error_code") if isinstance(payload, dict) else "mcp_table_read_failed"),
            message=str(
                payload.get("error_message")
                if isinstance(payload, dict)
                else "The bounded table read returned an invalid result."
            ),
        )

    rows = payload.get("rows") or []
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise TableRecordQuestionError(
            code="mcp_table_rows_invalid",
            message="The bounded table read returned invalid row data.",
        )

    row_count = len(rows)
    readable = request.canonical_table.replace("_", " ")
    if row_count == 0:
        status = ResponseStatus.INFORMATION_NOT_AVAILABLE
        answer = f"The `{readable}` table exists, but it currently contains no rows."
    elif row_count == 1:
        status = ResponseStatus.SUCCESS
        answer = f"Found 1 row in the `{readable}` table."
    else:
        status = ResponseStatus.SUCCESS
        answer = f"Found {row_count} rows in the `{readable}` table."

    return TableRecordResult(
        status=status,
        answer=answer,
        data={
            "target_table": request.canonical_table,
            "row_count": row_count,
            "rows": rows,
            "columns": list(payload.get("columns") or []),
            "limit": int(payload.get("limit") or request.limit),
            "offset": int(payload.get("offset") or 0),
            "database_source": payload.get("source")
            or {"source_type": "database", "tables": [request.canonical_table]},
            "execution": {
                "tool": "get_table_records",
                "via": "local_mcp_client_facade",
                "executed": True,
                "raw_sql_accepted": False,
                "ollama_called": False,
            },
        },
    )
