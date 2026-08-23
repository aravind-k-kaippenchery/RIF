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
from app.services.dynamic_pgsql_schema import (
    get_primary_key_columns,
    get_public_table_names,
    get_runtime_columns,
    table_relationships,
)
from app.services.schema_registry import OPERATIONAL_TABLES


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
    mode: str = "browse"
    filters: dict[str, Any] | None = None
    relationship_filter: dict[str, Any] | None = None

    def to_state(self) -> dict[str, Any]:
        return {
            "handled": self.handled,
            "canonical_table": self.canonical_table,
            "needs_clarification": self.needs_clarification,
            "clarification_message": self.clarification_message,
            "limit": self.limit,
            "mode": self.mode,
            "filters": dict(self.filters or {}),
            "relationship_filter": dict(self.relationship_filter or {}),
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
    occupied_spans: list[tuple[int, int]] = []
    candidates: list[tuple[int, str, str]] = []
    available_tables = [
        table_name
        for table_name in get_public_table_names()
        if table_name not in OPERATIONAL_TABLES
    ]
    for table_name in available_tables:
        aliases = set(_TABLE_ALIASES.get(table_name, ()))
        aliases.add(table_name)
        aliases.add(table_name.replace("_", " "))
        readable = table_name.replace("_", " ")
        if readable.endswith("s") and len(readable) > 3:
            aliases.add(readable[:-1])
        for alias in aliases:
            candidates.append((len(alias), table_name, alias))

    for _, table_name, alias in sorted(candidates, reverse=True):
        if table_name in found:
            continue
        matches = list(re.finditer(rf"\b{_phrase_pattern(alias)}\b", normalized))
        match = next(
            (
                item
                for item in matches
                if not any(item.start() < end and start < item.end() for start, end in occupied_spans)
            ),
            None,
        )
        if match is not None:
            found.append(table_name)
            occupied_spans.append((match.start(), match.end()))
    return tuple(found)


def _extract_limit(question: str) -> int:
    normalized = _normalize(question)
    match = re.search(r"\b(?:top|first|show|list|view|get)\s+(\d{1,3})\b", normalized)
    if not match:
        return 50
    return max(1, min(100, int(match.group(1))))


def _coerce_filter_value(value: str) -> str | int | float | bool:
    """Convert only unambiguous scalar literals; never evaluate user text."""

    normalized = value.strip()
    if normalized in {"true", "yes"}:
        return True
    if normalized in {"false", "no"}:
        return False
    if re.fullmatch(r"-?\d+", normalized):
        return int(normalized)
    if re.fullmatch(r"-?\d+\.\d+", normalized):
        return float(normalized)
    return normalized


def _equality_filter_request(
    *,
    normalized: str,
    table_name: str,
) -> TableRecordQuestion | None:
    """Resolve one equality predicate against the table's live reflected columns."""

    if not re.match(r"^(?:please\s+)?(?:show|list|view|display|get|give|find)\b", normalized):
        return None
    where_match = re.search(r"\b(?:where|whose)\s+(.+)$", normalized)
    if where_match is None:
        return None
    predicate = where_match.group(1).strip()
    predicate_match = re.fullmatch(
        r"(?:the\s+)?(?P<column>[a-z0-9_\s-]+?)\s+(?:is|equals|equal\s+to)\s+(?P<value>.+)",
        predicate,
    )
    columns = get_runtime_columns(table_name)
    readable_table = table_name.replace("_", " ")
    if predicate_match is None:
        return TableRecordQuestion(
            True,
            canonical_table=table_name,
            needs_clarification=True,
            clarification_message=(
                f"Use one equality filter such as `show {readable_table} where <column> is <value>`."
            ),
            mode="filter",
        )

    requested_column = predicate_match.group("column").strip()
    resolved_column = next(
        (
            column
            for column in columns
            if requested_column in {column.casefold(), column.replace("_", " ").casefold()}
        ),
        None,
    )
    if resolved_column is None:
        available = ", ".join(columns) if columns else "no reflected columns are currently available"
        return TableRecordQuestion(
            True,
            canonical_table=table_name,
            needs_clarification=True,
            clarification_message=(
                f"Column `{requested_column}` does not exist in the live `{readable_table}` table. "
                f"Available columns: {available}."
            ),
            mode="filter",
        )

    raw_value = predicate_match.group("value").strip()
    if re.search(r"\s+(?:and|or|order\s+by|group\s+by|limit)\s+", raw_value):
        return TableRecordQuestion(
            True,
            canonical_table=table_name,
            needs_clarification=True,
            clarification_message="Please use one filter at a time for this deterministic table read.",
            mode="filter",
        )
    return TableRecordQuestion(
        True,
        canonical_table=table_name,
        limit=_extract_limit(normalized),
        mode="filter",
        filters={resolved_column: _coerce_filter_value(raw_value)},
    )


def _live_column_aliases(table_name: str, columns: list[str]) -> list[tuple[str, str]]:
    """Derive unambiguous natural aliases exclusively from reflected column names."""

    candidates: dict[str, set[str]] = {}
    table_parts = set(table_name.casefold().split("_"))
    for column in columns:
        normalized_column = column.casefold()
        readable = normalized_column.replace("_", " ")
        aliases = {normalized_column, readable}
        parts = normalized_column.split("_")
        if len(parts) > 1:
            # ``product_category`` -> ``category`` and ``shipment_status`` ->
            # ``status`` only when that shorter name identifies one live column.
            aliases.add(parts[-1])
            if parts[0] in table_parts:
                aliases.add(" ".join(parts[1:]))
        for alias in aliases:
            candidates.setdefault(alias, set()).add(column)
    resolved = [
        (alias, next(iter(matches)))
        for alias, matches in candidates.items()
        if alias and len(matches) == 1
    ]
    return sorted(resolved, key=lambda item: len(item[0]), reverse=True)


def _table_alias_pattern(table_name: str) -> str:
    aliases = sorted(
        set(_TABLE_ALIASES.get(table_name, ()))
        | {table_name, table_name.replace("_", " ")},
        key=len,
        reverse=True,
    )
    return "(?:" + "|".join(_phrase_pattern(alias) for alias in aliases) + ")"


def _related_table_request(normalized: str, tables: tuple[str, ...]) -> TableRecordQuestion | None:
    """Resolve one direct reflected child→parent relationship and parent identity."""

    if len(tables) != 2 or not re.match(
        r"^(?:please\s+)?(?:show|list|view|display|get|give|find)\b",
        normalized,
    ):
        return None
    relationship_candidates: list[dict[str, Any]] = []
    table_set = set(tables)
    for possible_child in tables:
        for relationship in table_relationships(possible_child):
            if relationship.get("to_table") in table_set - {possible_child}:
                relationship_candidates.append(dict(relationship))
    if len(relationship_candidates) != 1:
        return None

    relationship = relationship_candidates[0]
    child_table = str(relationship["from_table"])
    parent_table = str(relationship["to_table"])
    parent_pattern = _table_alias_pattern(parent_table)
    parent_match = re.search(
        rf"\b(?:for|of|belonging\s+to|linked\s+to|related\s+to)\s+(?:the\s+)?"
        rf"{parent_pattern}\b(?P<tail>.*)$",
        normalized,
    )
    if parent_match is None:
        return None
    tail = parent_match.group("tail").strip()
    readable_child = child_table.replace("_", " ")
    if not tail:
        return TableRecordQuestion(
            True,
            canonical_table=child_table,
            needs_clarification=True,
            clarification_message=(
                f"Which `{parent_table.replace('_', ' ')}` record should be used to filter `{readable_child}`?"
            ),
            mode="related",
        )

    parent_columns = get_runtime_columns(parent_table)
    lookup_column: str | None = None
    lookup_value = tail
    for natural_alias, column_name in _live_column_aliases(parent_table, parent_columns):
        alias_pattern = _phrase_pattern(natural_alias)
        explicit = re.fullmatch(
            rf"(?:with\s+)?{alias_pattern}(?:\s+(?:is|equals|equal\s+to))?\s+(?P<value>.+)",
            tail,
        )
        if explicit is not None:
            lookup_column = column_name
            lookup_value = explicit.group("value").strip()
            break

    if lookup_column is None:
        code_columns = [
            column for column in parent_columns if column == "code" or column.endswith("_code")
        ]
        if len(code_columns) == 1:
            lookup_column = code_columns[0]
        else:
            primary_keys = get_primary_key_columns(parent_table)
            if len(code_columns) == 0 and len(primary_keys) == 1:
                lookup_column = primary_keys[0]
    if lookup_column is None:
        return TableRecordQuestion(
            True,
            canonical_table=child_table,
            needs_clarification=True,
            clarification_message=(
                f"The live `{parent_table.replace('_', ' ')}` table has no single unambiguous code or primary-key column. "
                "Please name the parent lookup column explicitly."
            ),
            mode="related",
        )

    return TableRecordQuestion(
        True,
        canonical_table=child_table,
        limit=_extract_limit(normalized),
        mode="related",
        relationship_filter={
            "parent_table": parent_table,
            "parent_column": lookup_column,
            "parent_value": _coerce_filter_value(lookup_value),
        },
    )


def _natural_reflected_filter_request(
    *,
    normalized: str,
    table_name: str,
    alias_pattern: str,
) -> TableRecordQuestion | None:
    """Parse ``<table> from <value> <column>`` using only live column names."""

    if not re.match(r"^(?:please\s+)?(?:show|list|view|display|get|give|find)\b", normalized):
        return None
    after_table = re.fullmatch(
        rf"^(?:please\s+)?(?:show|list|view|display|get|give|find)(?:\s+me)?(?:\s+the)?\s+"
        rf"(?:(?:first|top)\s+\d{{1,3}}\s+)?{alias_pattern}(?:\s+table)?\s+"
        rf"(?:from|in|with|having)\s+(?P<filter>.+)$",
        normalized,
    )
    if after_table is None:
        return None

    filter_text = after_table.group("filter").strip()
    columns = get_runtime_columns(table_name)
    for natural_alias, column_name in _live_column_aliases(table_name, columns):
        column_pattern = _phrase_pattern(natural_alias)
        value_first = re.fullmatch(rf"(?P<value>.+?)\s+{column_pattern}", filter_text)
        column_first = re.fullmatch(
            rf"{column_pattern}(?:\s+(?:is|equals|equal\s+to))?\s+(?P<value>.+)",
            filter_text,
        )
        filter_match = value_first or column_first
        if filter_match is None:
            continue
        raw_value = filter_match.group("value").strip()
        if not raw_value or re.match(
            r"^(?:greater|less|more|fewer|above|below|before|after|between|contains?|starts?|ends?)\b",
            raw_value,
        ):
            return None
        if re.search(r"\s+(?:and|or|order\s+by|group\s+by|limit)\s+", raw_value):
            return None
        return TableRecordQuestion(
            True,
            canonical_table=table_name,
            limit=_extract_limit(normalized),
            mode="filter",
            filters={column_name: _coerce_filter_value(raw_value)},
        )
    return None


def detect_table_record_question(question: str) -> TableRecordQuestion:
    """Classify simple whole-table and reflected single-filter row requests.

    Equality filters are resolved from live PostgreSQL column metadata and executed
    without generated SQL. Other aggregates, joins, and conditions continue to the
    normal validated natural-language SQL path. Ambiguous requests are clarified.
    """

    normalized = _normalize(question)
    if not normalized:
        return TableRecordQuestion(False)

    tables = _resolve_business_tables(normalized)
    related_request = _related_table_request(normalized, tables)
    if related_request is not None:
        return related_request
    if len(tables) != 1:
        return TableRecordQuestion(False)
    table_name = tables[0]

    aliases = sorted(
        set(_TABLE_ALIASES.get(table_name, ()))
        | {table_name, table_name.replace("_", " ")},
        key=len,
        reverse=True,
    )
    alias_pattern = "(?:" + "|".join(_phrase_pattern(alias) for alias in aliases) + ")"

    # Do not intercept schema questions; the dedicated metadata service owns them.
    if re.search(r"\b(?:column|columns|field|fields|schema|structure|exist|exists)\b", normalized):
        return TableRecordQuestion(False)

    equality_request = _equality_filter_request(normalized=normalized, table_name=table_name)
    if equality_request is not None:
        return equality_request

    natural_filter_request = _natural_reflected_filter_request(
        normalized=normalized,
        table_name=table_name,
        alias_pattern=alias_pattern,
    )
    if natural_filter_request is not None:
        return natural_filter_request

    # Resolve natural location wording only when the live table actually has a
    # city column. The table/column are reflected; no vendor/customer list is used.
    location_match = re.fullmatch(
        rf"^(?:please\s+)?(?:show|list|view|display|get|give|find)(?:\s+me)?(?:\s+the)?\s+"
        rf"{alias_pattern}(?:\s+table)?\s+(?:from|in|at)\s+(?P<value>.+)$",
        normalized,
    )
    if location_match is not None and "city" in get_runtime_columns(table_name):
        return TableRecordQuestion(
            True,
            canonical_table=table_name,
            limit=_extract_limit(normalized),
            mode="filter",
            filters={"city": _coerce_filter_value(location_match.group("value"))},
        )

    exact_count_patterns = (
        rf"^count\s+(?:every|all)\s+(?:row|rows|record|records|entry|entries)\s+in\s+(?:the\s+)?{alias_pattern}(?:\s+table)?$",
        rf"^how\s+many\s+(?:rows|records|entries)\s+(?:are\s+)?in\s+(?:the\s+)?{alias_pattern}(?:\s+table)?$",
    )
    if any(re.fullmatch(pattern, normalized) for pattern in exact_count_patterns):
        return TableRecordQuestion(
            True,
            canonical_table=table_name,
            limit=1,
            mode="count",
        )

    bounded_record_patterns = (
        rf"^(?:please\s+)?(?:show|list|view|display|get)(?:\s+me)?(?:\s+the)?\s+(?:first|top)\s+"
        rf"(?P<limit>\d{{1,3}})\s+(?:records|rows|entries)\s+(?:(?:from|in|of)\s+)?(?:the\s+)?"
        rf"{alias_pattern}(?:\s+table)?$",
        rf"^(?:please\s+)?(?:show|list|view|display|get)(?:\s+me)?(?:\s+the)?\s+(?:first|top)\s+"
        rf"(?P<limit>\d{{1,3}})\s+(?:the\s+)?{alias_pattern}(?:\s+table)?(?:\s+(?:records|rows|entries))?$",
    )
    for pattern in bounded_record_patterns:
        bounded_match = re.fullmatch(pattern, normalized)
        if bounded_match is not None:
            return TableRecordQuestion(
                True,
                canonical_table=table_name,
                limit=max(1, min(100, int(bounded_match.group("limit")))),
                mode="browse",
            )

    # Any explicit filter/aggregation/join language requires normal NL-to-SQL.
    if re.search(
        r"\b(?:from|where|whose|with|without|in|at|based|living|above|below|under|over|"
        r"greater|less|between|highest|lowest|average|avg|sum|count|total|join|related|"
        r"active|inactive|status|salary|price|department|city|category|country)\b",
        normalized,
    ):
        return TableRecordQuestion(False)

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
            "limit": 1 if request.mode == "count" else request.limit,
            "offset": 0,
            "user_role": user_role.value,
            "filters": dict(request.filters or {}),
            "relationship_filter": dict(request.relationship_filter or {}),
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

    returned_row_count = len(rows)
    row_count = int(payload.get("total_row_count") or 0) if request.mode == "count" else returned_row_count
    readable = request.canonical_table.replace("_", " ")
    if request.mode == "count":
        status = ResponseStatus.SUCCESS
        noun = "row" if row_count == 1 else "rows"
        answer = f"There are {row_count} {noun} in the `{readable}` table."
    elif row_count == 0:
        status = ResponseStatus.INFORMATION_NOT_AVAILABLE
        if request.relationship_filter:
            parent_table = str(request.relationship_filter.get("parent_table") or "parent").replace("_", " ")
            parent_column = str(request.relationship_filter.get("parent_column") or "identifier")
            parent_value = request.relationship_filter.get("parent_value")
            answer = (
                f"No rows in the `{readable}` table are related to `{parent_table}` "
                f"where {parent_column} = {parent_value}."
            )
        elif request.filters:
            qualifiers = ", ".join(f"{name} = {value}" for name, value in request.filters.items())
            answer = f"No rows in the `{readable}` table match {qualifiers}."
        else:
            answer = f"The `{readable}` table exists, but it currently contains no rows."
    elif row_count == 1:
        status = ResponseStatus.SUCCESS
        answer = f"Found 1 row in the `{readable}` table."
    else:
        status = ResponseStatus.SUCCESS
        answer = f"Found {row_count} rows in the `{readable}` table."

    if request.filters and row_count > 0:
        qualifiers = ", ".join(f"{name} = {value}" for name, value in request.filters.items())
        answer = answer[:-1] + f" matching {qualifiers}."
    if request.relationship_filter and row_count > 0:
        parent_table = str(request.relationship_filter.get("parent_table") or "parent").replace("_", " ")
        parent_column = str(request.relationship_filter.get("parent_column") or "identifier")
        parent_value = request.relationship_filter.get("parent_value")
        answer = answer[:-1] + f" related to `{parent_table}` where {parent_column} = {parent_value}."

    return TableRecordResult(
        status=status,
        answer=answer,
        data={
            "target_table": request.canonical_table,
            "row_count": row_count,
            "rows": [] if request.mode == "count" else rows,
            "returned_row_count": 0 if request.mode == "count" else returned_row_count,
            "columns": list(payload.get("columns") or []),
            "limit": int(payload.get("limit") or request.limit),
            "offset": int(payload.get("offset") or 0),
            "applied_filters": dict(payload.get("applied_filters") or request.filters or {}),
            "applied_relationship_filter": dict(
                payload.get("applied_relationship_filter") or request.relationship_filter or {}
            ),
            "database_source": payload.get("source")
            or {"source_type": "database", "tables": [request.canonical_table]},
            "execution": {
                "tool": "get_table_records",
                "via": "local_mcp_client_facade",
                "executed": True,
                "raw_sql_accepted": False,
                "ollama_called": False,
                "operation": request.mode,
            },
        },
    )
