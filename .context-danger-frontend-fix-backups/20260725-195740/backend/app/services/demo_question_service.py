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

from app.core.constants import AgentRoute, ResponseStatus
from app.models.business import Customer, Employee, EmployeeExperience, Product, SalesDeal, Vendor
from app.models.operations import QueryLog
from app.services.schema_registry import BUSINESS_TABLES, OPERATIONAL_TABLES


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
    if not match:
        return default
    return max(1, min(100, int(match.group(1))))


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
    # Synthetic generation and writes are handled elsewhere; do not intercept them here.
    if _contains_any(text, write_terms):
        return DemoQuestionRequest(False)

    document_first_terms = {
        "document", "documents", "uploaded", "upload", "pdf", "file", "files", "faq", "policy",
        "keyword", "ticket", "tickets", "escalate", "escalated", "escalation", "support",
        "onboarding", "compliance", "checklist", "catalog", "submit", "submitted", "required", "requirements",
    }
    if (
        _contains_any(text, document_first_terms)
        and re.search(r"\b(?:what|when|which|how|find|search|tell|summarize|explain)\b", text)
        and not re.search(r"\b(?:show|list|count|how\s+many)\b.*\b(?:employees?|vendors?|customers?|products?|sales\s+deals?)\b", text)
    ):
        return DemoQuestionRequest(False)

    if (
        re.search(r"\bwhat\s+can\s+you\s+do\b", text)
        or re.fullmatch(r"(?:help|capabilities|features)\??", text)
        or re.search(r"\bwhat\s+types?\s+of\s+database\s+operations\b", text)
        or re.search(r"\bcan\s+you\s+(?:read\s+uploaded\s+documents|create\s+and\s+update\s+database\s+records|generate\s+synthetic\s+data)\b", text)
        or re.search(r"\b(?:do\s+you\s+work\s+offline|which\s+local\s+ai\s+model|are\s+you\s+using\s+a\s+cloud\s+llm)\b", text)
    ):
        return DemoQuestionRequest(True, kind="capabilities", route=AgentRoute.SYSTEM)

    if re.search(r"\b(?:fastapi|postgresql|postgres|ollama|chromadb|langgraph|mcp)\b", text) and re.search(r"\b(?:connected|available|ready|status|health)\b", text):
        return DemoQuestionRequest(True, kind="system_health_hint", route=AgentRoute.SYSTEM)

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

    # Sales/orders phrasing. The POC schema uses sales_deals, not an orders table.
    if re.search(r"\b(?:orders?|recent\s+orders?|orders?\s+placed)\b", text):
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

    # Employee experience / work-history questions must be checked before the broader employee detector.
    if re.search(r"\b(?:employee\s+experiences?|experiences?|work\s+history|previous\s+companies|previous\s+jobs)\b", text):
        if re.search(r"\b(?:how\s+many|count)\b", text):
            return DemoQuestionRequest(True, kind="employee_experience_count", table="employee_experiences")
        if re.search(r"\bcount\b.*\bcompany\b", text):
            return DemoQuestionRequest(True, kind="employee_experience_count_by_company", table="employee_experiences")
        return DemoQuestionRequest(True, kind="employee_experience_list", table="employee_experiences", limit=_limit_from_question(text))

    # Employees / workers / staff.
    if re.search(r"\b(?:employees?|workers?|staff|people)\b", text):
        if re.search(r"\b(?:count\s+employees?\s+by\s+department|number\s+of\s+employees\s+in\s+each\s+department|employees\s+in\s+each\s+department|department\s+has\s+the\s+most\s+employees)\b", text):
            return DemoQuestionRequest(True, kind="employee_count_by_department", table="employees")
        if re.search(r"\b(?:count\s+employees?\s+by\s+city|city\s+has\s+the\s+most\s+employees|employees\s+in\s+each\s+city)\b", text):
            return DemoQuestionRequest(True, kind="employee_count_by_city", table="employees")
        if re.search(r"\bcount\s+employees?\s+by\s+status\b", text):
            return DemoQuestionRequest(True, kind="employee_count_by_status", table="employees")
        if re.search(r"\b(?:how\s+many|count\s+all|count)\s+(?:active\s+|inactive\s+)?employees?\b", text):
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
        if "active" in text:
            filters["employment_status"] = "active"
        if "inactive" in text:
            filters["employment_status"] = "inactive"
        if re.search(r"\b(?:show|list|view|display|find|who|which|get)\b", text):
            return DemoQuestionRequest(True, kind="employee_list", table="employees", filters=filters, limit=_limit_from_question(text))

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
        if re.search(r"\bcount\s+vendors?\s+by\s+(?:approval\s+)?status\b", text):
            return DemoQuestionRequest(True, kind="vendor_count_by_status", table="vendors")
        if re.search(r"\b(?:how\s+many|count\s+all|count)\s+(?:active\s+|inactive\s+|approved\s+)?vendors?\b", text):
            return DemoQuestionRequest(True, kind="vendor_count", table="vendors", filters=filters)
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

    # Product aggregate questions may say only "list price" without the word product.
    if re.search(r"\b(?:average|avg|highest|lowest)\s+(?:list\s+)?price\b", text):
        if re.search(r"\b(?:highest|max|maximum)\b", text):
            return DemoQuestionRequest(True, kind="product_highest_price", table="products")
        if re.search(r"\b(?:lowest|min|minimum|cheapest)\b", text):
            return DemoQuestionRequest(True, kind="product_lowest_price", table="products")
        return DemoQuestionRequest(True, kind="product_average_price", table="products")

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
        if re.search(r"\bcount\s+products?\s+by\s+category\b", text):
            return DemoQuestionRequest(True, kind="product_count_by_category", table="products")
        if re.search(r"\b(?:how\s+many|count\s+all|count)\s+(?:active\s+|inactive\s+)?products?\b", text):
            return DemoQuestionRequest(True, kind="product_count", table="products", filters=filters)
        if re.search(r"\b(?:average|avg)\s+(?:list\s+)?price\b", text):
            return DemoQuestionRequest(True, kind="product_average_price", table="products")
        if re.search(r"\b(?:highest|max|maximum)\s+(?:list\s+)?price\b", text):
            return DemoQuestionRequest(True, kind="product_highest_price", table="products")
        if re.search(r"\b(?:lowest|min|minimum|cheapest)\s+(?:list\s+)?price\b", text):
            return DemoQuestionRequest(True, kind="product_lowest_price", table="products")
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


def _list_answer(*, table: str, rows: list[dict[str, Any]], filters: dict[str, Any], alias_note: str | None = None) -> tuple[ResponseStatus, str]:
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
    label = singular if len(rows) == 1 else plural
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
    qualifier = " " + " and ".join(parts) if parts else ""
    summaries = [display_map.get(table, lambda item: str(item))(row) for row in rows[:5]]
    prefix = (alias_note + " ") if alias_note else ""
    if len(rows) <= 5:
        return ResponseStatus.SUCCESS, f"{prefix}Found {len(rows)} {label}{qualifier}: {_join(summaries)}."
    return ResponseStatus.SUCCESS, f"{prefix}Found {len(rows)} {plural}{qualifier}. First {len(summaries)}: {_join(summaries)}."


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

    if request.kind == "employee_count_by_department":
        rows = list(db.execute(select(Employee.department, func.count()).group_by(Employee.department).order_by(Employee.department)).all())
        data_rows = [{"department": str(dept), "employee_count": int(count)} for dept, count in rows]
        text = ", ".join(f"{item['department']}: {item['employee_count']}" for item in data_rows)
        return DemoQuestionResult(
            route=AgentRoute.STRUCTURED_READ,
            status=ResponseStatus.SUCCESS if data_rows else ResponseStatus.INFORMATION_NOT_AVAILABLE,
            answer=f"Employee count by department: {text}." if data_rows else "Information not available in the current database.",
            generated_sql="SELECT department, count(*) AS employee_count FROM employees GROUP BY department ORDER BY department",
            sources=_source("employees"),
            data={"target_table": "employees", "row_count": len(data_rows), "rows": data_rows, "execution": {"ollama_called": False, "deterministic_demo_query": True}},
        )

    if request.kind == "employee_count_by_city":
        rows = list(db.execute(select(Employee.city, func.count()).group_by(Employee.city).order_by(Employee.city)).all())
        data_rows = [{"city": str(city), "employee_count": int(count)} for city, count in rows]
        text = ", ".join(f"{item['city']}: {item['employee_count']}" for item in data_rows)
        return DemoQuestionResult(
            route=AgentRoute.STRUCTURED_READ,
            status=ResponseStatus.SUCCESS if data_rows else ResponseStatus.INFORMATION_NOT_AVAILABLE,
            answer=f"Employee count by city: {text}." if data_rows else "Information not available in the current database.",
            generated_sql="SELECT city, count(*) AS employee_count FROM employees GROUP BY city ORDER BY city",
            sources=_source("employees"),
            data={"target_table": "employees", "row_count": len(data_rows), "rows": data_rows, "execution": {"ollama_called": False, "deterministic_demo_query": True}},
        )

    if request.kind == "employee_count_by_status":
        rows = list(db.execute(select(Employee.employment_status, func.count()).group_by(Employee.employment_status).order_by(Employee.employment_status)).all())
        data_rows = [{"employment_status": str(status), "employee_count": int(count)} for status, count in rows]
        text = ", ".join(f"{item['employment_status']}: {item['employee_count']}" for item in data_rows)
        return DemoQuestionResult(
            route=AgentRoute.STRUCTURED_READ,
            status=ResponseStatus.SUCCESS if data_rows else ResponseStatus.INFORMATION_NOT_AVAILABLE,
            answer=f"Employee count by status: {text}." if data_rows else "Information not available in the current database.",
            generated_sql="SELECT employment_status, count(*) AS employee_count FROM employees GROUP BY employment_status ORDER BY employment_status",
            sources=_source("employees"),
            data={"target_table": "employees", "row_count": len(data_rows), "rows": data_rows, "execution": {"ollama_called": False, "deterministic_demo_query": True}},
        )

    query = query.order_by(Employee.id).limit(max(1, min(100, request.limit)))
    items = list(db.scalars(query).all())
    rows = _rows_from_models(items, ["id", "employee_code", "first_name", "last_name", "email", "department", "city", "company_name", "salary", "employment_status"])
    status, answer = _list_answer(table="employees", rows=rows, filters=filters)
    sql = "SELECT * FROM employees" + _sql_where(filters) + f" ORDER BY id LIMIT {max(1, min(100, request.limit))}"
    return DemoQuestionResult(
        route=AgentRoute.STRUCTURED_READ,
        status=status,
        answer=answer,
        generated_sql=sql,
        sources=_source("employees"),
        data={"target_table": "employees", "row_count": len(rows), "rows": rows, "filters": filters, "database_source": {"source_type": "database", "tables": ["employees"]}, "execution": {"ollama_called": False, "deterministic_demo_query": True}},
    )


def _execute_vendor_query(db: Session, request: DemoQuestionRequest) -> DemoQuestionResult:
    filters = dict(request.filters or {})
    conditions = []
    if filters.get("status"):
        conditions.append(func.lower(Vendor.status) == str(filters["status"]).lower())
    if filters.get("vendor_name_contains"):
        conditions.append(Vendor.vendor_name.ilike(f"%{filters['vendor_name_contains']}%"))

    if request.kind == "vendor_count_by_status":
        rows = list(db.execute(select(Vendor.status, func.count()).group_by(Vendor.status).order_by(Vendor.status)).all())
        data_rows = [{"status": str(status), "vendor_count": int(count)} for status, count in rows]
        text = ", ".join(f"{item['status']}: {item['vendor_count']}" for item in data_rows)
        return DemoQuestionResult(AgentRoute.STRUCTURED_READ, ResponseStatus.SUCCESS if data_rows else ResponseStatus.INFORMATION_NOT_AVAILABLE, f"Vendor count by status: {text}." if data_rows else "Information not available in the current database.", {"target_table": "vendors", "row_count": len(data_rows), "rows": data_rows, "execution": {"ollama_called": False, "deterministic_demo_query": True}}, _source("vendors"), "SELECT status, count(*) AS vendor_count FROM vendors GROUP BY status ORDER BY status")

    if request.kind == "vendor_count":
        query = select(func.count()).select_from(Vendor)
        if conditions:
            query = query.where(and_(*conditions))
        count = int(db.scalar(query) or 0)
        return _count_result(table="vendors", count=count, filters=filters, sql="SELECT count(*) FROM vendors" + _sql_where(filters))

    query = select(Vendor)
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
    conditions = []
    if filters.get("list_price_gt") is not None:
        conditions.append(Product.list_price > Decimal(str(filters["list_price_gt"])))
    if "is_active" in filters:
        conditions.append(Product.is_active.is_(bool(filters["is_active"])))

    if request.kind == "product_count_by_category":
        rows = list(db.execute(select(Product.category, func.count()).group_by(Product.category).order_by(Product.category)).all())
        data_rows = [{"category": str(category), "product_count": int(count)} for category, count in rows]
        text = ", ".join(f"{item['category']}: {item['product_count']}" for item in data_rows)
        return DemoQuestionResult(AgentRoute.STRUCTURED_READ, ResponseStatus.SUCCESS if data_rows else ResponseStatus.INFORMATION_NOT_AVAILABLE, f"Product count by category: {text}." if data_rows else "Information not available in the current database.", {"target_table": "products", "row_count": len(data_rows), "rows": data_rows, "execution": {"ollama_called": False, "deterministic_demo_query": True}}, _source("products"), "SELECT category, count(*) AS product_count FROM products GROUP BY category ORDER BY category")

    if request.kind == "product_count":
        query = select(func.count()).select_from(Product)
        if conditions:
            query = query.where(and_(*conditions))
        count = int(db.scalar(query) or 0)
        return _count_result(table="products", count=count, filters=filters, sql="SELECT count(*) FROM products" + _sql_where(filters))

    if request.kind == "product_average_price":
        value = db.scalar(select(func.avg(Product.list_price)))
        if value is None:
            return DemoQuestionResult(AgentRoute.STRUCTURED_READ, ResponseStatus.INFORMATION_NOT_AVAILABLE, "Information not available in the current database.", {"target_table": "products", "row_count": 0, "rows": []}, _source("products"), "SELECT avg(list_price) FROM products")
        avg = float(value)
        return DemoQuestionResult(AgentRoute.STRUCTURED_READ, ResponseStatus.SUCCESS, f"The average product list price is ₹{avg:.2f}.", {"target_table": "products", "row_count": 1, "rows": [{"average_list_price": avg}], "execution": {"ollama_called": False, "deterministic_demo_query": True}}, _source("products"), "SELECT avg(list_price) AS average_list_price FROM products")

    if request.kind in {"product_highest_price", "product_lowest_price"}:
        order = desc(Product.list_price) if request.kind == "product_highest_price" else Product.list_price.asc()
        item = db.scalar(select(Product).order_by(order).limit(1))
        if item is None:
            return DemoQuestionResult(AgentRoute.STRUCTURED_READ, ResponseStatus.INFORMATION_NOT_AVAILABLE, "Information not available in the current database.", {"target_table": "products", "row_count": 0, "rows": []}, _source("products"), "SELECT * FROM products ORDER BY list_price LIMIT 1")
        row = _rows_from_models([item], ["id", "product_code", "product_name", "category", "list_price", "is_active"])[0]
        adjective = "highest" if request.kind == "product_highest_price" else "lowest"
        return DemoQuestionResult(AgentRoute.STRUCTURED_READ, ResponseStatus.SUCCESS, f"The {adjective} product list price is ₹{row.get('list_price')} for {row.get('product_name')}.", {"target_table": "products", "row_count": 1, "rows": [row], "execution": {"ollama_called": False, "deterministic_demo_query": True}}, _source("products"), f"SELECT * FROM products ORDER BY list_price {'DESC' if request.kind == 'product_highest_price' else 'ASC'} LIMIT 1")

    query = select(Product)
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




def _execute_employee_experience_query(db: Session, request: DemoQuestionRequest) -> DemoQuestionResult:
    if request.kind == "employee_experience_count":
        count = int(db.scalar(select(func.count()).select_from(EmployeeExperience)) or 0)
        return DemoQuestionResult(
            AgentRoute.STRUCTURED_READ,
            ResponseStatus.SUCCESS,
            f"There are {count} employee experience records.",
            {"target_table": "employee_experiences", "row_count": count, "rows": [], "execution": {"ollama_called": False, "deterministic_demo_query": True}},
            _source("employee_experiences"),
            "SELECT count(*) FROM employee_experiences",
        )
    if request.kind == "employee_experience_count_by_company":
        rows = list(db.execute(select(EmployeeExperience.company_name, func.count()).group_by(EmployeeExperience.company_name).order_by(EmployeeExperience.company_name)).all())
        data_rows = [{"company_name": str(company), "experience_count": int(count)} for company, count in rows]
        text = ", ".join(f"{item['company_name']}: {item['experience_count']}" for item in data_rows[:10])
        return DemoQuestionResult(
            AgentRoute.STRUCTURED_READ,
            ResponseStatus.SUCCESS if data_rows else ResponseStatus.INFORMATION_NOT_AVAILABLE,
            f"Employee experience count by company: {text}." if data_rows else "Information not available in the current database.",
            {"target_table": "employee_experiences", "row_count": len(data_rows), "rows": data_rows, "execution": {"ollama_called": False, "deterministic_demo_query": True}},
            _source("employee_experiences"),
            "SELECT company_name, count(*) AS experience_count FROM employee_experiences GROUP BY company_name ORDER BY company_name",
        )

    limit = max(1, min(100, request.limit))
    items = list(db.scalars(select(EmployeeExperience).order_by(EmployeeExperience.id).limit(limit)).all())
    rows = _rows_from_models(items, ["id", "employee_id", "company_name", "job_title", "employment_type", "location", "start_date", "end_date", "is_current"])
    if not rows:
        return DemoQuestionResult(AgentRoute.STRUCTURED_READ, ResponseStatus.INFORMATION_NOT_AVAILABLE, "Information not available in the current database.", {"target_table": "employee_experiences", "row_count": 0, "rows": []}, _source("employee_experiences"), f"SELECT * FROM employee_experiences ORDER BY id LIMIT {limit}")
    summaries = [f"{row.get('company_name')} - {row.get('job_title')}" for row in rows[:5]]
    answer = f"Found {len(rows)} employee experience records. First {len(summaries)}: {_join(summaries)}."
    return DemoQuestionResult(AgentRoute.STRUCTURED_READ, ResponseStatus.SUCCESS, answer, {"target_table": "employee_experiences", "row_count": len(rows), "rows": rows, "database_source": {"source_type": "database", "tables": ["employee_experiences"]}, "execution": {"ollama_called": False, "deterministic_demo_query": True}}, _source("employee_experiences"), f"SELECT * FROM employee_experiences ORDER BY id LIMIT {limit}")


def _count_result(*, table: str, count: int, filters: dict[str, Any], sql: str) -> DemoQuestionResult:
    label = table.replace("_", " ")
    return DemoQuestionResult(
        AgentRoute.STRUCTURED_READ,
        ResponseStatus.SUCCESS,
        f"There {'are' if count != 1 else 'is'} {count} {label} record{'s' if count != 1 else ''}.",
        {"target_table": table, "row_count": count, "rows": [], "filters": filters, "execution": {"ollama_called": False, "deterministic_demo_query": True}},
        _source(table),
        sql,
    )

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
            return DemoQuestionResult(
                route=AgentRoute.SYSTEM,
                status=ResponseStatus.SUCCESS,
                answer="The approved business tables are: " + ", ".join(f"`{item}`" for item in BUSINESS_TABLES) + ".",
                generated_sql=None,
                sources=[],
                data={"business_tables": list(BUSINESS_TABLES), "table_count": len(BUSINESS_TABLES), "execution": {"ollama_called": False, "deterministic_demo_query": True}},
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

        if request.kind == "system_health_hint":
            return DemoQuestionResult(
                route=AgentRoute.SYSTEM,
                status=ResponseStatus.SUCCESS,
                answer=(
                    "System health is available from the live-health endpoint and dashboard. "
                    "It checks FastAPI, PostgreSQL, Ollama, ChromaDB, LangGraph, and MCP with bounded local probes."
                ),
                generated_sql=None,
                sources=[],
                data={"health_components": ["fastapi", "postgres", "ollama", "chromadb", "langgraph", "mcp"], "endpoint": "/api/system/live-health", "execution": {"ollama_called": False, "deterministic_demo_query": True}},
            )

        if request.table == "employee_experiences":
            return _execute_employee_experience_query(db, request)
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
