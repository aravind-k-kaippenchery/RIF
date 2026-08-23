"""Deterministic demo-question handling for the RIF local POC.

This module is intentionally narrow and safe. It handles common read-only demo
questions without asking Ollama to invent table names or columns. Database writes
still go through the confirmation-gated CRUD path; this service only performs
bounded SELECT-style reads and schema/capability answers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import re
from typing import Any
from uuid import UUID

from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.constants import AgentRoute, ResponseStatus, UserRole
from app.models.business import Customer, Employee, Product, SalesDeal, Vendor
from app.models.operations import QueryLog
from app.services.dynamic_pgsql_schema import get_runtime_columns, get_runtime_table, has_public_table
from app.services.schema_registry import OPERATIONAL_TABLES, get_schema_contract
from app.services.table_record_question_service import (
    TableRecordQuestion,
    TableRecordQuestionError,
    read_table_records,
)


@dataclass(frozen=True)
class DemoQuestionRequest:
    handled: bool
    kind: str | None = None
    route: AgentRoute = AgentRoute.STRUCTURED_READ
    table: str | None = None
    filters: dict[str, Any] | None = None
    limit: int = 50

    def to_state(self) -> dict[str, Any]:
        return {
            "handled": self.handled,
            "kind": self.kind,
            "route": self.route.value,
            "table": self.table,
            "filters": dict(self.filters or {}),
            "limit": self.limit,
        }

    @classmethod
    def from_state(cls, value: dict[str, Any] | None) -> "DemoQuestionRequest":
        payload = dict(value or {})
        try:
            route = AgentRoute(str(payload.get("route") or AgentRoute.STRUCTURED_READ.value))
        except ValueError:
            route = AgentRoute.STRUCTURED_READ
        return cls(
            handled=bool(payload.get("handled")),
            kind=payload.get("kind"),
            route=route,
            table=payload.get("table"),
            filters=dict(payload.get("filters") or {}),
            limit=int(payload.get("limit") or 50),
        )


@dataclass(frozen=True)
class DemoQuestionResult:
    route: AgentRoute
    status: ResponseStatus
    answer: str
    data: dict[str, Any]
    sources: list[dict[str, Any]]
    generated_sql: str | None = None


class DemoQuestionError(RuntimeError):
    def __init__(self, *, code: str, message: str, status: ResponseStatus = ResponseStatus.TOOL_FAILED) -> None:
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)


_DEPARTMENTS = {
    "finance": "Finance",
    "hr": "HR",
    "human resources": "HR",
    "it": "IT",
    "sales": "Sales",
    "marketing": "Marketing",
    "operations": "Operations",
    "design": "Design",
}

_KNOWN_CITIES = {
    "bangalore": "Bangalore",
    "bengaluru": "Bangalore",
    "kochi": "Kochi",
    "cochin": "Kochi",
    "chennai": "Chennai",
    "mumbai": "Mumbai",
    "delhi": "Delhi",
    "pune": "Pune",
    "hyderabad": "Hyderabad",
    "thrissur": "Thrissur",
}


# ----------------------------- detection helpers -----------------------------


def _normalize(value: str) -> str:
    text = re.sub(r"[^a-z0-9_@.\s-]+", " ", str(value or "").casefold())
    return " ".join(text.split())


def _contains_any(text: str, values: set[str] | tuple[str, ...]) -> bool:
    return any(re.search(rf"\b{re.escape(value)}\b", text) for value in values)


def _limit_from_question(text: str, default: int = 50) -> int:
    match = re.search(r"\b(?:top|first|show|list|view|get)\s+(\d{1,3})\b", text)
    if match:
        return max(1, min(100, int(match.group(1))))

    # Demo follow-ups often use natural wording such as "the first five".
    word_numbers = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
    }
    word_match = re.search(r"\b(?:(?:top|first|show|list|view|get)\s+(?:the\s+)?|the\s+first\s+)(one|two|three|four|five|six|seven|eight|nine|ten)\b", text)
    if word_match:
        return word_numbers[word_match.group(1)]
    return default


def _department_from_question(text: str) -> str | None:
    # Check longer aliases first so "human resources" wins before "hr".
    for key, label in sorted(_DEPARTMENTS.items(), key=lambda item: len(item[0]), reverse=True):
        if re.search(rf"\b{re.escape(key)}\b", text):
            return label
    return None


def _city_from_question(text: str) -> str | None:
    for key, label in sorted(_KNOWN_CITIES.items(), key=lambda item: len(item[0]), reverse=True):
        if re.search(rf"\b{re.escape(key)}\b", text):
            return label
    match = re.search(r"\b(?:from|in|at|city)\s+([a-z][a-z\s-]{2,40})(?:\s+who|\s+with|\s+whose|\s+and|$)", text)
    if not match:
        return None
    candidate = " ".join(match.group(1).split()).strip().title()
    if candidate.casefold() in {"finance", "hr", "it", "sales", "active", "inactive"}:
        return None
    return candidate


def _name_contains(text: str) -> str | None:
    match = re.search(r"\bname\s+(?:contains|like|matching|includes)\s+([a-z][a-z0-9 ._-]{1,60})", text)
    if not match:
        return None
    return " ".join(match.group(1).split()).strip(" .?!,:;-'\"").title()


def _vendor_name_contains(text: str) -> str | None:
    match = re.search(r"\b(?:company\s+name|vendor\s+name|name)\s+(?:contains|like|matching|includes)\s+([a-z][a-z0-9 ._-]{1,60})", text)
    if not match:
        return None
    return " ".join(match.group(1).split()).strip(" .?!,:;-'\"").title()


def _price_threshold(text: str) -> float | None:
    match = re.search(r"\b(?:price|cost|amount)\s+(?:greater\s+than|above|over|more\s+than|>)\s+(\d+(?:\.\d+)?)", text)
    if not match:
        return None
    return float(match.group(1))


def _salary_threshold(text: str) -> float | None:
    match = re.search(
        r"\b(?:salary|pay)\s+(?:is\s+)?(?:greater\s+than|above|over|more\s+than|>)\s+(\d+(?:\.\d+)?)",
        text,
    )
    return float(match.group(1)) if match else None


def detect_demo_question(question: str) -> DemoQuestionRequest:
    """Detect high-confidence read-only demo questions.

    Returns handled=False for writes, unsafe requests, or vague prompts that need the
    normal router. This detector deliberately runs before parent-child and LLM SQL
    generation so common demo reads cannot be misrouted to unsupported tables/columns.
    """

    text = _normalize(question)
    if not text:
        return DemoQuestionRequest(False)

    write_terms = {"add", "create", "insert", "update", "delete", "remove", "change", "modify", "set", "confirm", "approve", "generate", "seed", "populate"}
    # Synthetic generation is handled elsewhere; do not intercept it here.
    if _contains_any(text, write_terms):
        return DemoQuestionRequest(False)

    if re.search(r"\bwhat\s+can\s+you\s+do\b", text) or re.fullmatch(r"(?:help|capabilities|features)\??", text):
        return DemoQuestionRequest(True, kind="capabilities", route=AgentRoute.SYSTEM)

    if (
        re.search(r"\b(?:which|what|show|list)\s+(?:are\s+)?(?:the\s+)?business\s+tables\b", text)
        or re.search(r"\btables\s+(?:are\s+)?business\b", text)
    ):
        return DemoQuestionRequest(True, kind="business_tables", route=AgentRoute.SYSTEM)

    if (
        re.search(r"\b(?:which|what|show|list)\s+(?:are\s+)?(?:the\s+)?operational\s+tables\b", text)
        or re.search(r"\btables\s+(?:are\s+)?operational\b", text)
    ):
        return DemoQuestionRequest(True, kind="operational_tables", route=AgentRoute.SYSTEM)

    # Context follow-ups must be resolved before the generic ambiguity guard.
    # These prompts intentionally contain no table name because they refer to the
    # previous successful read in the same session, e.g. "Show only the active ones"
    # after "Show employees from Bangalore".
    explicit_entity = re.search(
        r"\b(?:employees?|workers?|staff|people|vendors?|suppliers?|customers?|clients?|products?|items?|orders?|sales\s+deals?|deals?|opportunities)\b",
        text,
    )
    referential_followup = re.search(r"\b(?:them|those|these|ones|they)\b", text)
    existential_followup = re.search(r"\bthere\b", text) and not explicit_entity
    if referential_followup or existential_followup:
        if re.search(r"\bhow\s+many\s+(?:are\s+there|of\s+them|of\s+those|of\s+these)?\b", text) and not _department_from_question(text):
            return DemoQuestionRequest(True, kind="context_followup_count", table="__memory__", filters={"followup": True})
        if re.search(r"\b(?:show|list|display|view|get)\b", text) and re.search(r"\b(?:active|inactive)\b", text):
            status_value = "inactive" if re.search(r"\binactive\b", text) else "active"
            return DemoQuestionRequest(True, kind="context_followup_list", table="__memory__", filters={"followup": True, "status_value": status_value}, limit=_limit_from_question(text))
        if re.search(r"\b(?:first|top)\b", text):
            return DemoQuestionRequest(True, kind="context_followup_list", table="__memory__", filters={"followup": True}, limit=_limit_from_question(text, 5))
        if re.search(r"\bwhich\s+cities\b", text):
            return DemoQuestionRequest(True, kind="context_followup_distinct_city", table="__memory__", filters={"followup": True})

    if re.fullmatch(r"(?:who\s+are\s+)?(?:the\s+)?first\s+(?:\d{1,3}|one|two|three|four|five|six|seven|eight|nine|ten)[?.!]*", text):
        return DemoQuestionRequest(True, kind="context_followup_list", table="__memory__", filters={"followup": True}, limit=_limit_from_question(text, 5))

    if re.fullmatch(r"(?:show|list|display|view|get)\s+(?:the\s+)?first\s+(?:\d{1,3}|one|two|three|four|five|six|seven|eight|nine|ten)[?.!]*", text):
        return DemoQuestionRequest(True, kind="context_followup_list", table="__memory__", filters={"followup": True}, limit=_limit_from_question(text, 5))

    # Follow-up after a previous employee read, e.g. "How many of them are in Finance?"
    if re.search(r"\bhow\s+many\s+(?:of\s+)?(?:them|those|these)\b", text):
        department = _department_from_question(text)
        if department:
            return DemoQuestionRequest(
                True,
                kind="employee_count_followup_department",
                route=AgentRoute.STRUCTURED_READ,
                table="employees",
                filters={"department": department, "followup": True},
            )

    # Preserve the legacy order-style demo only when PostgreSQL truly has no orders
    # table. An exact reflected table name always wins over a compatibility alias.
    if re.search(r"\b(?:orders?|recent\s+orders?|orders?\s+placed)\b", text):
        if has_public_table("orders"):
            return DemoQuestionRequest(False)
        return DemoQuestionRequest(
            True,
            kind="recent_sales_deals_alias_orders",
            route=AgentRoute.STRUCTURED_READ,
            table="sales_deals",
            limit=_limit_from_question(text, 10),
        )

    if re.search(r"\b(?:sales\s+deals?|deals?|opportunities)\b", text) and re.search(r"\b(?:recent|latest|newest|show|list|view)\b", text):
        return DemoQuestionRequest(
            True,
            kind="recent_sales_deals",
            route=AgentRoute.STRUCTURED_READ,
            table="sales_deals",
            limit=_limit_from_question(text, 10),
        )

    # Employees / workers / staff.
    if re.search(r"\b(?:employees?|workers?|staff|people)\b", text):
        employee_grouping = re.search(
            r"\b(?:group|count|break(?:down)?|distribution)\b.*\b(?:by|per|each|department[- ]?wise|city[- ]?wise|location[- ]?wise|status[- ]?wise)\b",
            text,
        ) or re.search(
            r"\b(?:(?:each|every)\s+(?:department|city|location|status)|(?:department|city|location|status)[- ]?wise)\b.*\bcount\b",
            text,
        )
        if employee_grouping:
            if re.search(r"\b(?:city|location)(?:[- ]?wise)?\b", text):
                return DemoQuestionRequest(True, kind="employee_count_by_city", table="employees")
            if re.search(r"\b(?:employment\s+)?status(?:[- ]?wise)?\b", text):
                return DemoQuestionRequest(True, kind="employee_count_by_status", table="employees")
            if re.search(r"\bdepartment(?:[- ]?wise)?\b", text):
                return DemoQuestionRequest(True, kind="employee_count_by_department", table="employees")
        if re.search(r"\bhow\s+many\s+employees?\b", text):
            filters: dict[str, Any] = {}
            department = _department_from_question(text)
            city = _city_from_question(text)
            if department:
                filters["department"] = department
            if city:
                filters["city"] = city
            if "active" in text:
                filters["employment_status"] = "active"
            if "inactive" in text:
                filters["employment_status"] = "inactive"
            return DemoQuestionRequest(True, kind="employee_count", table="employees", filters=filters)

        if re.search(r"\b(?:average|avg|mean)\b.*\b(?:salary|pay)\b|\b(?:salary|pay)\b.*\b(?:average|avg|mean)\b", text):
            return DemoQuestionRequest(True, kind="employee_average_salary", table="employees")

        filters = {}
        department = _department_from_question(text)
        city = _city_from_question(text)
        name_like = _name_contains(text)
        if department:
            filters["department"] = department
        if city:
            filters["city"] = city
        if name_like:
            filters["name_contains"] = name_like
        salary_threshold = _salary_threshold(text)
        if salary_threshold is not None:
            filters["salary_gt"] = salary_threshold
        if "active" in text:
            filters["employment_status"] = "active"
        if "inactive" in text:
            filters["employment_status"] = "inactive"
        if re.search(r"\b(?:show|list|view|display|find|who|which|get)\b", text):
            highest_paid = bool(
                re.search(r"\b(?:highest|top|most)\s*[- ]?(?:paid|salary|pay)\b", text)
                or re.search(r"\b(?:salary|pay)\b.*\b(?:highest|descending|highest\s+to\s+lowest)\b", text)
            )
            kind = "employee_list_by_salary_desc" if highest_paid else "employee_list"
            return DemoQuestionRequest(True, kind=kind, table="employees", filters=filters, limit=_limit_from_question(text))

    # Natural wording: "Who works under Finance?" / "Who works in Finance?"
    if re.search(r"\bwho\s+works\b", text):
        department = _department_from_question(text)
        city = _city_from_question(text)
        filters = {}
        if department:
            filters["department"] = department
        if city:
            filters["city"] = city
        return DemoQuestionRequest(True, kind="employee_list", table="employees", filters=filters, limit=_limit_from_question(text))

    # Vendors / suppliers.
    if re.search(r"\b(?:vendors?|suppliers?)\b", text):
        filters = {}
        if re.search(r"\b(?:approved|active)\b", text):
            filters["status"] = "active"
            filters["approved_alias"] = True
        if re.search(r"\binactive\b", text):
            filters["status"] = "inactive"
        name_like = _vendor_name_contains(text)
        if name_like:
            filters["vendor_name_contains"] = name_like
        if name_like is None and re.search(r"\b(?:where|whose|from|in|at|city)\b", text):
            # Do not let the legacy vendor demo shortcut discard a condition it
            # cannot represent. The reflected filter path or validated NL-to-SQL
            # path must handle it instead of returning every vendor.
            return DemoQuestionRequest(False)
        return DemoQuestionRequest(True, kind="vendor_list", table="vendors", filters=filters, limit=_limit_from_question(text))

    # Customers / clients.
    if re.search(r"\b(?:customers?|clients?)\b", text):
        filters = {}
        city = _city_from_question(text)
        if city:
            filters["city"] = city
        if "active" in text:
            filters["status"] = "active"
        if "inactive" in text:
            filters["status"] = "inactive"
        return DemoQuestionRequest(True, kind="customer_list", table="customers", filters=filters, limit=_limit_from_question(text))

    # Products / items.
    if re.search(r"\b(?:products?|items?)\b", text):
        filters = {}
        threshold = _price_threshold(text)
        if threshold is not None:
            filters["list_price_gt"] = threshold
        if "active" in text:
            filters["is_active"] = True
        if "inactive" in text:
            filters["is_active"] = False
        if threshold is None and re.search(r"\b(?:where|whose|from|in|with|having|category)\b", text):
            # Never return every product after recognizing but discarding an
            # unsupported condition. Reflected filtering or validated NL-to-SQL
            # owns these prompts.
            return DemoQuestionRequest(False)
        return DemoQuestionRequest(True, kind="product_list", table="products", filters=filters, limit=_limit_from_question(text))

    return DemoQuestionRequest(False)


# ------------------------------ execution helpers -----------------------------


def _safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe(item) for item in value]
    return str(value)


def _rows_from_models(items: list[Any], fields: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in items:
        rows.append({field: _safe(getattr(item, field, None)) for field in fields})
    return rows


def _join(items: list[str]) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return f"{', '.join(items[:-1])}, and {items[-1]}"


def _source(table: str, note: str = "Read through deterministic demo-safe SQLAlchemy query; no LLM SQL was used.") -> list[dict[str, Any]]:
    return [{"source_type": "database", "reference": table, "detail": note}]


def _sql_where(filters: dict[str, Any]) -> str:
    parts = []
    for key, value in filters.items():
        if key == "approved_alias" or key == "followup":
            continue
        if key.endswith("_contains"):
            column = key.replace("_contains", "")
            parts.append(f"lower({column}) LIKE '%{str(value).lower()}%'")
        elif key == "list_price_gt":
            parts.append(f"list_price > {value}")
        elif key == "salary_gt":
            parts.append(f"salary > {value}")
        elif key == "is_active":
            parts.append(f"is_active = {str(bool(value)).lower()}")
        else:
            parts.append(f"lower({key}) = '{str(value).lower()}'")
    return " WHERE " + " AND ".join(parts) if parts else ""


def _latest_employee_city_from_memory(memory_context: dict[str, Any] | None) -> str | None:
    if not isinstance(memory_context, dict):
        return None
    events = memory_context.get("events")
    if not isinstance(events, list):
        return None
    for event in reversed(events):
        if not isinstance(event, dict):
            continue
        sql = str(event.get("generated_sql") or "")
        question = _normalize(str(event.get("prior_question") or event.get("user_prompt") or ""))
        candidate = sql + " " + question
        if "employees" not in candidate.lower() and not re.search(r"\b(?:workers?|employees?|staff)\b", question):
            continue
        for key, label in _KNOWN_CITIES.items():
            if re.search(rf"\b{re.escape(key)}\b", candidate.lower()):
                return label
    return None


def _latest_table_context_from_memory(memory_context: dict[str, Any] | None) -> dict[str, Any] | None:
    """Recover the latest explicit table/filter context for short follow-ups.

    The stored memory contains only a reflected table, replayable equality filters,
    and a row count. Returned business rows and hidden reasoning are never retained.
    """

    if not isinstance(memory_context, dict):
        return None
    events = memory_context.get("events")
    if not isinstance(events, list):
        return None

    for event in reversed(events):
        if not isinstance(event, dict):
            continue
        if str(event.get("status") or "").lower() not in {"success", "pending_confirmation"}:
            continue
        reference = event.get("conversation_reference")
        if isinstance(reference, dict) and reference.get("reference_type") == "table_result":
            referenced_table = str(reference.get("target_table") or "").strip().lower()
            referenced_filters = reference.get("filters")
            if referenced_table:
                return {
                    "table": referenced_table,
                    "filters": dict(referenced_filters) if isinstance(referenced_filters, dict) else {},
                    "relationship_filter": (
                        dict(reference.get("relationship_filter"))
                        if isinstance(reference.get("relationship_filter"), dict)
                        else {}
                    ),
                    "source_question": _normalize(
                        str(event.get("prior_question") or event.get("user_prompt") or "")
                    ),
                    "source_sql": str(event.get("generated_sql") or ""),
                    "reference_type": "table_result",
                    "record_count": reference.get("record_count"),
                    "filters_complete": bool(reference.get("filters_complete", True)),
                }
    return None


def _apply_context_status_filter(table: str, filters: dict[str, Any], status_value: str | None) -> dict[str, Any] | None:
    updated = dict(filters)
    if not status_value:
        return updated
    columns = get_runtime_columns(table)
    status_columns = [column for column in columns if column == "status" or column.endswith("_status")]
    boolean_active_columns = [column for column in columns if column == "is_active" or column.endswith("_active")]
    candidates = status_columns + boolean_active_columns
    if len(candidates) == 1:
        selected = candidates[0]
        updated[selected] = status_value == "active" if selected in boolean_active_columns else status_value
        return updated

    return None


def _live_equality_conditions(table: Any, filters: dict[str, Any]) -> list[Any] | None:
    conditions: list[Any] = []
    for column_name, value in filters.items():
        column = table.c.get(str(column_name).strip().lower())
        if column is None:
            return None
        try:
            python_type = column.type.python_type
        except (AttributeError, NotImplementedError):
            python_type = None
        if value is None:
            conditions.append(column.is_(None))
        elif python_type is str:
            conditions.append(func.lower(column) == str(value).casefold())
        else:
            conditions.append(column == value)
    return conditions


def _execute_context_followup(db: Session, request: DemoQuestionRequest, memory_context: dict[str, Any] | None) -> DemoQuestionResult:
    context = _latest_table_context_from_memory(memory_context)
    if not context:
        return DemoQuestionResult(
            route=AgentRoute.SYSTEM,
            status=ResponseStatus.CLARIFICATION_REQUIRED,
            answer="I do not have a previous table result in this session to apply that follow-up to. Please name the table or filter explicitly.",
            data={"target_table": None, "rows": [], "filters": {}, "execution": {"ollama_called": False, "deterministic_followup": True}},
            sources=[],
            generated_sql=None,
        )

    table = str(context["table"])
    if not bool(context.get("filters_complete", True)):
        return DemoQuestionResult(
            AgentRoute.SYSTEM,
            ResponseStatus.CLARIFICATION_REQUIRED,
            "The previous query used joins, ranges, or another condition that cannot be replayed as safe equality filters. Please restate the table and filters explicitly.",
            {"target_table": table, "rows": [], "filters": {}, "source_context": context},
            [],
            None,
        )
    status_value = dict(request.filters or {}).get("status_value")
    filters = _apply_context_status_filter(table, dict(context.get("filters") or {}), status_value)
    if filters is None:
        return DemoQuestionResult(
            AgentRoute.SYSTEM,
            ResponseStatus.CLARIFICATION_REQUIRED,
            "The previous table has no single unambiguous status column. Please name the status column explicitly.",
            {"target_table": table, "rows": [], "filters": {}, "source_context": context},
            [],
            None,
        )
    limit = max(1, min(100, request.limit))

    if request.kind != "context_followup_distinct_city":
        mode = "count" if request.kind == "context_followup_count" else "filter"
        try:
            reflected = read_table_records(
                request=TableRecordQuestion(
                    True,
                    canonical_table=table,
                    limit=1 if mode == "count" else limit,
                    mode=mode,
                    filters=filters,
                    relationship_filter=dict(context.get("relationship_filter") or {}),
                ),
                user_role=UserRole.NORMAL_USER,
            )
        except TableRecordQuestionError as exc:
            return DemoQuestionResult(
                AgentRoute.SYSTEM,
                ResponseStatus.INFORMATION_NOT_AVAILABLE,
                exc.message,
                {"target_table": table, "rows": [], "filters": filters, "source_context": context},
                [],
                None,
            )
        reflected_data = dict(reflected.data)
        reflected_data["filters"] = filters
        reflected_data["source_context"] = context
        reflected_data.setdefault("execution", {})["deterministic_followup"] = True
        return DemoQuestionResult(
            AgentRoute.STRUCTURED_READ,
            reflected.status,
            reflected.answer,
            reflected_data,
            _source(table, "Follow-up resolved from bounded session memory and live PostgreSQL reflection; no LLM SQL was used."),
            None,
        )

    try:
        reflected_table = get_runtime_table(table, bind=db.bind)
        city_column = reflected_table.c.get("city")
        conditions = _live_equality_conditions(reflected_table, filters)
        if city_column is None or conditions is None:
            raise KeyError("city")
        city_query = select(city_column, func.count()).select_from(reflected_table)
        if conditions:
            city_query = city_query.where(*conditions)
        city_query = city_query.group_by(city_column).order_by(city_column)
        data_rows = [
            {"city": _safe(city), "record_count": int(count)}
            for city, count in db.execute(city_query).all()
        ]
    except (KeyError, SQLAlchemyError):
        return DemoQuestionResult(
            AgentRoute.SYSTEM,
            ResponseStatus.CLARIFICATION_REQUIRED,
            "The previous reflected table has no safely reusable `city` column. Please name a valid column explicitly.",
            {"target_table": table, "rows": [], "filters": filters, "source_context": context},
            [],
            None,
        )
    text = ", ".join(f"{item['city']}: {item['record_count']}" for item in data_rows)
    return DemoQuestionResult(
        AgentRoute.STRUCTURED_READ,
        ResponseStatus.SUCCESS if data_rows else ResponseStatus.INFORMATION_NOT_AVAILABLE,
        f"The previous `{table.replace('_', ' ')}` result is distributed by city as: {text}." if data_rows else "Information not available in the current database.",
        {"target_table": table, "row_count": len(data_rows), "rows": data_rows, "filters": filters, "source_context": context, "execution": {"ollama_called": False, "deterministic_followup": True}},
        _source(table, "Follow-up city breakdown resolved from bounded session memory and live PostgreSQL reflection; no LLM SQL was used."),
        None,
    )


def _employee_display(row: dict[str, Any]) -> str:
    name = " ".join(part for part in [row.get("first_name"), row.get("last_name")] if part).strip()
    name = name or str(row.get("employee_code") or "Employee")
    department = row.get("department")
    return f"{name} ({department})" if department else name


def _vendor_display(row: dict[str, Any]) -> str:
    name = row.get("vendor_name") or row.get("vendor_code") or "Vendor"
    status = row.get("status")
    return f"{name} ({status})" if status else str(name)


def _customer_display(row: dict[str, Any]) -> str:
    name = row.get("customer_name") or row.get("customer_code") or "Customer"
    city = row.get("city")
    return f"{name} ({city})" if city else str(name)


def _product_display(row: dict[str, Any]) -> str:
    name = row.get("product_name") or row.get("product_code") or "Product"
    price = row.get("list_price")
    return f"{name} (₹{price})" if price is not None else str(name)


def _deal_display(row: dict[str, Any]) -> str:
    title = row.get("title") or row.get("deal_code") or "Sales deal"
    stage = row.get("stage")
    return f"{title} ({stage})" if stage else str(title)


def _list_answer(*, table: str, rows: list[dict[str, Any]], filters: dict[str, Any], alias_note: str | None = None, total_count: int | None = None) -> tuple[ResponseStatus, str]:
    if not rows:
        return ResponseStatus.INFORMATION_NOT_AVAILABLE, "Information not available in the current database."

    display_map = {
        "employees": _employee_display,
        "vendors": _vendor_display,
        "customers": _customer_display,
        "products": _product_display,
        "sales_deals": _deal_display,
    }
    singular_plural = {
        "employees": ("employee", "employees"),
        "vendors": ("vendor", "vendors"),
        "customers": ("customer", "customers"),
        "products": ("product", "products"),
        "sales_deals": ("sales deal", "sales deals"),
    }
    singular, plural = singular_plural.get(table, ("record", "records"))
    display_count = total_count if total_count is not None else len(rows)
    label = singular if display_count == 1 else plural
    parts: list[str] = []
    if filters.get("city"):
        parts.append(f"in {filters['city']}")
    if filters.get("department"):
        parts.append(f"in {filters['department']}")
    if filters.get("employment_status"):
        parts.append(f"with status {filters['employment_status']}")
    if filters.get("status"):
        parts.append(f"with status {filters['status']}")
    if filters.get("list_price_gt") is not None:
        parts.append(f"with list price greater than {filters['list_price_gt']:g}")
    if filters.get("salary_gt") is not None:
        parts.append(f"with salary greater than {filters['salary_gt']:g}")
    qualifier = " " + " and ".join(parts) if parts else ""
    summaries = [display_map.get(table, lambda item: str(item))(row) for row in rows[:5]]
    prefix = (alias_note + " ") if alias_note else ""
    if display_count <= 5 and len(rows) <= 5:
        return ResponseStatus.SUCCESS, f"{prefix}Found {display_count} {label}{qualifier}: {_join(summaries)}."
    return ResponseStatus.SUCCESS, f"{prefix}Found {display_count} {plural}{qualifier}. First {len(summaries)}: {_join(summaries)}."


def _execute_employee_query(db: Session, request: DemoQuestionRequest, memory_context: dict[str, Any] | None) -> DemoQuestionResult:
    filters = dict(request.filters or {})
    query = select(Employee)
    conditions = []

    if request.kind == "employee_count_followup_department" and filters.get("followup"):
        memory_city = _latest_employee_city_from_memory(memory_context)
        if memory_city:
            filters["city"] = memory_city

    if filters.get("department"):
        conditions.append(func.lower(Employee.department) == str(filters["department"]).lower())
    if filters.get("city"):
        conditions.append(func.lower(Employee.city) == str(filters["city"]).lower())
    if filters.get("employment_status"):
        conditions.append(func.lower(Employee.employment_status) == str(filters["employment_status"]).lower())
    if filters.get("name_contains"):
        pattern = f"%{filters['name_contains']}%"
        conditions.append(or_(Employee.first_name.ilike(pattern), Employee.last_name.ilike(pattern), (Employee.first_name + " " + Employee.last_name).ilike(pattern)))
    if filters.get("salary_gt") is not None:
        conditions.append(Employee.salary > Decimal(str(filters["salary_gt"])))
    if conditions:
        query = query.where(and_(*conditions))

    if request.kind in {"employee_count", "employee_count_followup_department"}:
        count_query = select(func.count()).select_from(Employee)
        if conditions:
            count_query = count_query.where(and_(*conditions))
        count = int(db.scalar(count_query) or 0)
        if request.kind == "employee_count_followup_department" and filters.get("city"):
            answer = f"Of the previous employee set from {filters['city']}, {count} employee{'s are' if count != 1 else ' is'} in {filters.get('department', 'the requested department')}."
        elif filters:
            qualifiers = []
            if filters.get("city"):
                qualifiers.append(f"from {filters['city']}")
            if filters.get("department"):
                qualifiers.append(f"in {filters['department']}")
            if filters.get("employment_status"):
                qualifiers.append(f"with status {filters['employment_status']}")
            answer = f"There {'are' if count != 1 else 'is'} {count} employee{'s' if count != 1 else ''} {' '.join(qualifiers)}."
        else:
            answer = f"There are {count} employees."
        sql = "SELECT count(*) FROM employees" + _sql_where(filters)
        return DemoQuestionResult(
            route=AgentRoute.STRUCTURED_READ,
            status=ResponseStatus.SUCCESS,
            answer=answer,
            generated_sql=sql,
            sources=_source("employees"),
            data={"target_table": "employees", "row_count": count, "rows": [], "filters": filters, "execution": {"ollama_called": False, "deterministic_demo_query": True}},
        )

    if request.kind == "employee_average_salary":
        average = db.scalar(select(func.avg(Employee.salary)))
        if average is None:
            return DemoQuestionResult(
                route=AgentRoute.STRUCTURED_READ,
                status=ResponseStatus.INFORMATION_NOT_AVAILABLE,
                answer="Information not available in the current database.",
                generated_sql="SELECT avg(salary) AS average_salary FROM employees",
                sources=_source("employees"),
                data={"target_table": "employees", "row_count": 0, "rows": [], "execution": {"ollama_called": False, "deterministic_demo_query": True}},
            )
        average_value = float(average)
        return DemoQuestionResult(
            route=AgentRoute.STRUCTURED_READ,
            status=ResponseStatus.SUCCESS,
            answer=f"The average employee salary is {average_value:,.2f}.",
            generated_sql="SELECT avg(salary) AS average_salary FROM employees",
            sources=_source("employees"),
            data={"target_table": "employees", "row_count": 1, "rows": [{"average_salary": average_value}], "execution": {"ollama_called": False, "deterministic_demo_query": True}},
        )

    grouping_columns = {
        "employee_count_by_department": ("department", Employee.department),
        "employee_count_by_city": ("city", Employee.city),
        "employee_count_by_status": ("employment_status", Employee.employment_status),
    }
    if request.kind in grouping_columns:
        field_name, column = grouping_columns[request.kind]
        rows = list(db.execute(select(column, func.count()).group_by(column).order_by(column)).all())
        data_rows = [{field_name: str(value), "employee_count": int(count)} for value, count in rows]
        text = ", ".join(f"{item[field_name]}: {item['employee_count']}" for item in data_rows)
        total = sum(item["employee_count"] for item in data_rows)
        label = "status" if field_name == "employment_status" else field_name
        return DemoQuestionResult(
            route=AgentRoute.STRUCTURED_READ,
            status=ResponseStatus.SUCCESS if data_rows else ResponseStatus.INFORMATION_NOT_AVAILABLE,
            answer=f"Employee count by {label}: {text}. Total: {total}." if data_rows else "Information not available in the current database.",
            generated_sql=f"SELECT {field_name}, count(*) AS employee_count FROM employees GROUP BY {field_name} ORDER BY {field_name}",
            sources=_source("employees"),
            data={"target_table": "employees", "row_count": len(data_rows), "employee_total": total, "rows": data_rows, "execution": {"ollama_called": False, "deterministic_demo_query": True}},
        )

    total_query = select(func.count()).select_from(Employee)
    if conditions:
        total_query = total_query.where(and_(*conditions))
    total_count = int(db.scalar(total_query) or 0)
    limit = max(1, min(100, request.limit))
    if request.kind == "employee_list_by_salary_desc":
        query = query.order_by(desc(Employee.salary), Employee.id).limit(limit)
        order_sql = " ORDER BY salary DESC, id"
    else:
        query = query.order_by(Employee.id).limit(limit)
        order_sql = " ORDER BY id"
    items = list(db.scalars(query).all())
    rows = _rows_from_models(items, ["id", "employee_code", "first_name", "last_name", "email", "department", "city", "company_name", "salary", "employment_status"])
    status, answer = _list_answer(table="employees", rows=rows, filters=filters, total_count=total_count)
    sql = "SELECT * FROM employees" + _sql_where(filters) + order_sql + f" LIMIT {limit}"
    return DemoQuestionResult(
        route=AgentRoute.STRUCTURED_READ,
        status=status,
        answer=answer,
        generated_sql=sql,
        sources=_source("employees"),
        data={"target_table": "employees", "row_count": total_count, "returned_row_count": len(rows), "rows": rows, "filters": filters, "database_source": {"source_type": "database", "tables": ["employees"]}, "execution": {"ollama_called": False, "deterministic_demo_query": True}},
    )


def _execute_vendor_query(db: Session, request: DemoQuestionRequest) -> DemoQuestionResult:
    filters = dict(request.filters or {})
    query = select(Vendor)
    conditions = []
    if filters.get("status"):
        conditions.append(func.lower(Vendor.status) == str(filters["status"]).lower())
    if filters.get("vendor_name_contains"):
        conditions.append(Vendor.vendor_name.ilike(f"%{filters['vendor_name_contains']}%"))
    if conditions:
        query = query.where(and_(*conditions))
    items = list(db.scalars(query.order_by(Vendor.id).limit(max(1, min(100, request.limit)))).all())
    rows = _rows_from_models(items, ["id", "vendor_code", "vendor_name", "contact_email", "phone", "city", "country", "category", "status"])
    alias_note = "This schema uses `status = active` as the approved/available vendor signal." if filters.get("approved_alias") else None
    status, answer = _list_answer(table="vendors", rows=rows, filters=filters, alias_note=alias_note)
    return DemoQuestionResult(AgentRoute.STRUCTURED_READ, status, answer, {"target_table": "vendors", "row_count": len(rows), "rows": rows, "filters": filters, "database_source": {"source_type": "database", "tables": ["vendors"]}, "execution": {"ollama_called": False, "deterministic_demo_query": True}}, _source("vendors"), "SELECT * FROM vendors" + _sql_where(filters) + f" ORDER BY id LIMIT {max(1, min(100, request.limit))}")


def _execute_customer_query(db: Session, request: DemoQuestionRequest) -> DemoQuestionResult:
    filters = dict(request.filters or {})
    query = select(Customer)
    conditions = []
    if filters.get("city"):
        conditions.append(func.lower(Customer.city) == str(filters["city"]).lower())
    if filters.get("status"):
        conditions.append(func.lower(Customer.status) == str(filters["status"]).lower())
    if conditions:
        query = query.where(and_(*conditions))
    items = list(db.scalars(query.order_by(Customer.id).limit(max(1, min(100, request.limit)))).all())
    rows = _rows_from_models(items, ["id", "customer_code", "customer_name", "contact_email", "phone", "city", "country", "industry", "status"])
    status, answer = _list_answer(table="customers", rows=rows, filters=filters)
    return DemoQuestionResult(AgentRoute.STRUCTURED_READ, status, answer, {"target_table": "customers", "row_count": len(rows), "rows": rows, "filters": filters, "database_source": {"source_type": "database", "tables": ["customers"]}, "execution": {"ollama_called": False, "deterministic_demo_query": True}}, _source("customers"), "SELECT * FROM customers" + _sql_where(filters) + f" ORDER BY id LIMIT {max(1, min(100, request.limit))}")


def _execute_product_query(db: Session, request: DemoQuestionRequest) -> DemoQuestionResult:
    filters = dict(request.filters or {})
    query = select(Product)
    conditions = []
    if filters.get("list_price_gt") is not None:
        conditions.append(Product.list_price > Decimal(str(filters["list_price_gt"])))
    if "is_active" in filters:
        conditions.append(Product.is_active.is_(bool(filters["is_active"])))
    if conditions:
        query = query.where(and_(*conditions))
    items = list(db.scalars(query.order_by(Product.id).limit(max(1, min(100, request.limit)))).all())
    rows = _rows_from_models(items, ["id", "product_code", "product_name", "category", "description", "list_price", "is_active"])
    status, answer = _list_answer(table="products", rows=rows, filters=filters)
    return DemoQuestionResult(AgentRoute.STRUCTURED_READ, status, answer, {"target_table": "products", "row_count": len(rows), "rows": rows, "filters": filters, "database_source": {"source_type": "database", "tables": ["products"]}, "execution": {"ollama_called": False, "deterministic_demo_query": True, "column_aliases": {"price": "list_price"}}}, _source("products"), "SELECT * FROM products" + _sql_where(filters) + f" ORDER BY id LIMIT {max(1, min(100, request.limit))}")


def _execute_sales_deals_query(db: Session, request: DemoQuestionRequest) -> DemoQuestionResult:
    limit = max(1, min(100, request.limit))
    items = list(db.scalars(select(SalesDeal).order_by(desc(SalesDeal.created_at), desc(SalesDeal.expected_close_date)).limit(limit)).all())
    rows = _rows_from_models(items, ["id", "deal_code", "title", "amount", "stage", "probability", "expected_close_date", "status"])
    alias_note = "This schema does not have an `orders` table; it uses `sales_deals` for recent sales/order-style demo records." if request.kind == "recent_sales_deals_alias_orders" else None
    status, answer = _list_answer(table="sales_deals", rows=rows, filters={}, alias_note=alias_note)
    return DemoQuestionResult(AgentRoute.STRUCTURED_READ, status, answer, {"target_table": "sales_deals", "row_count": len(rows), "rows": rows, "database_source": {"source_type": "database", "tables": ["sales_deals"]}, "execution": {"ollama_called": False, "deterministic_demo_query": True, "orders_alias_to_sales_deals": request.kind == "recent_sales_deals_alias_orders"}}, _source("sales_deals"), f"SELECT * FROM sales_deals ORDER BY created_at DESC, expected_close_date DESC LIMIT {limit}")


def execute_demo_question(
    db: Session,
    *,
    request: DemoQuestionRequest,
    question: str,
    memory_context: dict[str, Any] | None = None,
) -> DemoQuestionResult:
    if not request.handled or not request.kind:
        raise DemoQuestionError(code="demo_question_not_handled", message="This request is not a deterministic demo question.")

    try:
        if request.kind == "capabilities":
            return DemoQuestionResult(
                route=AgentRoute.SYSTEM,
                status=ResponseStatus.SUCCESS,
                answer=(
                    "I can answer questions from uploaded documents, run safe read-only database queries, inspect approved table structures, "
                    "prepare confirmation-gated inserts/updates/deletes, generate synthetic demo records for approved business tables, and show evidence for each response."
                ),
                generated_sql=None,
                sources=[],
                data={
                    "capabilities": [
                        "document_rag",
                        "structured_read",
                        "schema_metadata",
                        "confirmation_gated_crud",
                        "synthetic_data_generation",
                        "evidence_traceability",
                    ],
                    "execution": {"ollama_called": False, "deterministic_demo_query": True},
                },
            )

        if request.kind == "business_tables":
            business_tables = list(get_schema_contract().get("business_tables") or [])
            return DemoQuestionResult(
                route=AgentRoute.SYSTEM,
                status=ResponseStatus.SUCCESS,
                answer="The available business tables are: " + ", ".join(f"`{item}`" for item in business_tables) + ".",
                generated_sql=None,
                sources=[],
                data={"business_tables": business_tables, "table_count": len(business_tables), "schema_source": "postgresql_reflection", "execution": {"ollama_called": False, "deterministic_demo_query": True}},
            )

        if request.kind == "operational_tables":
            return DemoQuestionResult(
                route=AgentRoute.SYSTEM,
                status=ResponseStatus.SUCCESS,
                answer="The operational/audit tables are: " + ", ".join(f"`{item}`" for item in OPERATIONAL_TABLES) + ".",
                generated_sql=None,
                sources=[],
                data={"operational_tables": list(OPERATIONAL_TABLES), "table_count": len(OPERATIONAL_TABLES), "execution": {"ollama_called": False, "deterministic_demo_query": True}},
            )

        if request.table == "__memory__":
            return _execute_context_followup(db, request, memory_context)
        if request.table == "employees":
            return _execute_employee_query(db, request, memory_context)
        if request.table == "vendors":
            return _execute_vendor_query(db, request)
        if request.table == "customers":
            return _execute_customer_query(db, request)
        if request.table == "products":
            return _execute_product_query(db, request)
        if request.table == "sales_deals":
            return _execute_sales_deals_query(db, request)

    except SQLAlchemyError as exc:
        raise DemoQuestionError(
            code="deterministic_demo_query_failed",
            message="The deterministic demo query could not be completed against PostgreSQL.",
            status=ResponseStatus.DATABASE_UNAVAILABLE,
        ) from exc

    raise DemoQuestionError(code="demo_question_kind_unsupported", message="This deterministic demo question kind is not supported yet.")
