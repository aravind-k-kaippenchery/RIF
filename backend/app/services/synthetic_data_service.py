"""Schema-aware Faker generation for reflected PostgreSQL business tables.

The service is intentionally deterministic at the safety boundary:

* It only recognizes explicit synthetic/demo/random generation requests.
* It discovers eligible targets from the live PostgreSQL ``public`` schema.
* It never writes to PostgreSQL directly.
* Foreign keys are selected from existing parent rows.
* Generated records always enter the existing duplicate-check and confirmation flow.

Current tables keep their dedicated profiles. A conservative reflection-based fallback
makes manually-created business tables usable without adding them to a Python allowlist.
Complex tables can still register a dedicated profile in ``PROFILE_REGISTRY``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
import itertools
import random
import re
from typing import Any, Callable
from uuid import uuid4

from faker import Faker
from sqlalchemy import Boolean, Date, DateTime, Integer, Numeric, String, Text, select
from sqlalchemy.orm import Session

from app.services.duplicate_service import find_batch_duplicates, get_unique_key_sets
from app.services.dynamic_pgsql_schema import get_public_table_names, get_runtime_table
from app.services.schema_registry import OPERATIONAL_TABLES


MAX_SYNTHETIC_RECORDS = 50
DEFAULT_FAKER_LOCALE = "en_IN"
DEFAULT_COUNTRY = "India"
DEFAULT_COMPANY_NAME = "Neolotex"
MANAGED_COLUMNS = {"id", "created_at", "updated_at"}

DEPARTMENTS = (
    "Sales",
    "HR",
    "IT",
    "Finance",
    "Operations",
    "Support",
    "Marketing",
    "Engineering",
    "Admin",
)
CITIES = (
    "Bangalore",
    "Kochi",
    "Chennai",
    "Hyderabad",
    "Mumbai",
    "Pune",
    "Delhi",
    "Thrissur",
    "Coimbatore",
)
VENDOR_CATEGORIES = (
    "Textile Automation",
    "Industrial Equipment",
    "Software Services",
    "Raw Materials",
    "Logistics",
    "Maintenance",
)
CUSTOMER_INDUSTRIES = (
    "Textiles",
    "Manufacturing",
    "Retail",
    "Logistics",
    "Healthcare",
    "Education",
    "Technology",
)
PRODUCT_CATEGORIES = (
    "Textile Automation",
    "Industrial Robotics",
    "Quality Control",
    "Inventory Systems",
    "Analytics Software",
)
DEAL_STAGES: tuple[tuple[str, int], ...] = (
    ("qualification", 20),
    ("discovery", 35),
    ("proposal", 55),
    ("negotiation", 75),
    ("verbal_commit", 90),
)
EMPLOYMENT_TYPES = ("full_time", "part_time", "contract", "internship")
EXPERIENCE_JOB_TITLES = (
    "Software Engineer",
    "Python Developer",
    "Business Analyst",
    "HR Associate",
    "Sales Executive",
    "Operations Coordinator",
    "Finance Analyst",
    "Support Engineer",
)

PERMISSION_CODES = (
    "VIEW_PRODUCTS",
    "VIEW_CUSTOMERS",
    "VIEW_VENDORS",
    "VIEW_DEALS",
    "MANAGE_PRODUCTS",
    "MANAGE_CUSTOMERS",
    "MANAGE_VENDORS",
    "MANAGE_DEALS",
    "EXPORT_REPORTS",
    "APPROVE_DISCOUNTS",
)

_WORD_NUMBER_MAP = {
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
    "eleven": 11,
    "twelve": 12,
    "fifteen": 15,
    "twenty": 20,
    "twenty-five": 25,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
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

# Longer/more-specific aliases must be tested first.
TABLE_ALIASES: dict[str, tuple[str, ...]] = {
    "employee_experiences": (
        "employee experiences",
        "employee experience",
        "work histories",
        "work history",
        "employment histories",
        "employment history",
        "previous jobs",
        "previous job",
        "experiences",
        "experience",
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


class SyntheticDataGenerationError(ValueError):
    """Controlled input/data error for a synthetic batch request."""

    def __init__(self, code: str, message: str, *, details: list[dict[str, Any]] | None = None) -> None:
        self.code = code
        self.message = message
        self.details = details or []
        super().__init__(message)


@dataclass(frozen=True)
class SyntheticDataRequest:
    """Normalized schema-aware generation request."""

    target_table: str
    count: int
    constraints: dict[str, Any] = field(default_factory=dict)
    source_text: str | None = None


@dataclass(frozen=True)
class SyntheticDataBatch:
    """Generated records plus preview/audit metadata."""

    target_table: str
    records: list[dict[str, Any]]
    metadata: dict[str, Any]


GeneratorProfile = Callable[[Session, Faker, SyntheticDataRequest, str], list[dict[str, Any]]]
PROFILE_REGISTRY: dict[str, GeneratorProfile] = {}


def register_synthetic_profile(table_name: str) -> Callable[[GeneratorProfile], GeneratorProfile]:
    """Register a custom profile for an approved current or future business table."""

    normalized = table_name.strip().lower()

    def decorator(function: GeneratorProfile) -> GeneratorProfile:
        PROFILE_REGISTRY[normalized] = function
        return function

    return decorator


def _normalize_text(value: Any, *, max_length: int = 160) -> str | None:
    if value is None:
        return None
    normalized = " ".join(str(value).strip().split())
    return normalized[:max_length] or None


def _phrase_pattern(phrase: str) -> str:
    parts = [re.escape(item) for item in re.split(r"[\s_-]+", phrase.strip()) if item]
    return r"[\s_-]+".join(parts)


def _synthetic_table_names() -> list[str]:
    """Return reflected public tables that Faker may safely target."""

    protected = {str(name).strip().lower() for name in OPERATIONAL_TABLES}
    return [name for name in get_public_table_names() if name not in protected]


def _target_from_prompt(question: str) -> str | None:
    normalized = question.casefold()
    available_tables = _synthetic_table_names()

    # Exact reflected table wording wins over a generic noun such as "people".
    for table_name in sorted(available_tables, key=len, reverse=True):
        readable = table_name.replace("_", " ")
        patterns = (
            rf"\b(?:in|into|to|for|inside)\s+(?:the\s+)?{_phrase_pattern(readable)}\s+table\b",
            rf"\b{_phrase_pattern(readable)}\s+table\b",
            rf"\b{re.escape(table_name)}\b",
        )
        if any(re.search(pattern, normalized, flags=re.IGNORECASE) for pattern in patterns):
            return table_name

    # Preserve friendly aliases only for the original registered profiles.
    aliases: list[tuple[int, str, str]] = []
    for table_name, values in TABLE_ALIASES.items():
        if table_name not in available_tables:
            continue
        for alias in values:
            aliases.append((len(alias), table_name, alias))
    for _, table_name, alias in sorted(aliases, reverse=True):
        if re.search(rf"\b{_phrase_pattern(alias)}\b", normalized, flags=re.IGNORECASE):
            return table_name
    return None


def _extract_count(question: str, target_table: str) -> int | None:
    aliases = TABLE_ALIASES.get(target_table, (target_table.replace("_", " "),))
    alias_pattern = "|".join(_phrase_pattern(alias) for alias in sorted(aliases, key=len, reverse=True))
    generation_words = r"(?:random|randomly|randomized|synthetic|fake|faker|sample|demo|generated)"
    write_words = r"(?:add|create|insert|generate|seed|populate|make)"

    patterns = (
        rf"\b{write_words}\s+(?:me\s+)?([0-9]{{1,3}})\b",
        rf"\b([0-9]{{1,3}})\s+(?:{generation_words}\s+)*(?:{alias_pattern})\b",
        rf"\b([0-9]{{1,3}})\s+(?:records?|rows?)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, question, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))

    for word, number in sorted(_WORD_NUMBER_MAP.items(), key=lambda item: len(item[0]), reverse=True):
        word_pattern = _phrase_pattern(word)
        patterns = (
            rf"\b{write_words}\s+(?:me\s+)?{word_pattern}\b",
            rf"\b{word_pattern}\s+(?:{generation_words}\s+)*(?:{alias_pattern})\b",
        )
        if any(re.search(pattern, question, flags=re.IGNORECASE) for pattern in patterns):
            return number
    return None


def _known_value(question: str, choices: tuple[str, ...]) -> str | None:
    for choice in choices:
        if re.search(rf"\b{re.escape(choice)}\b", question, flags=re.IGNORECASE):
            return choice
    return None


def _extract_named_constraint(question: str, field_name: str) -> str | None:
    readable = field_name.replace("_", " ")
    match = re.search(
        rf"\b{re.escape(readable)}\s+(?:is\s+|of\s+|=\s*)?([A-Za-z][A-Za-z0-9&.' /-]{{1,80}}?)(?:\s+(?:in|at|for|with|and)\b|[.,;]|$)",
        question,
        flags=re.IGNORECASE,
    )
    return _normalize_text(match.group(1)) if match else None


def _extract_business_codes(question: str) -> dict[str, str]:
    patterns = {
        "employee_code": r"\bEMP-[A-Z0-9-]+\b",
        "vendor_code": r"\bVEN-[A-Z0-9-]+\b",
        "customer_code": r"\bCUST-[A-Z0-9-]+\b",
        "product_code": r"\bPROD-[A-Z0-9-]+\b",
    }
    values: dict[str, str] = {}
    for key, pattern in patterns.items():
        match = re.search(pattern, question, flags=re.IGNORECASE)
        if match:
            values[key] = match.group(0).upper()
    return values


def _constraints_from_prompt(question: str, target_table: str) -> dict[str, Any]:
    constraints: dict[str, Any] = {}
    constraints.update(_extract_business_codes(question))

    city = _known_value(question, CITIES)
    if city and target_table in {"employees", "vendors", "customers"}:
        constraints["city"] = city

    department = _known_value(question, DEPARTMENTS)
    if department and target_table == "employees":
        constraints["department"] = department

    if target_table == "employees":
        company_name = _extract_named_constraint(question, "company")
        if company_name:
            constraints["company_name"] = company_name
    elif target_table == "vendors":
        category = _known_value(question, VENDOR_CATEGORIES) or _extract_named_constraint(question, "category")
        if category:
            constraints["category"] = category
    elif target_table == "customers":
        industry = _known_value(question, CUSTOMER_INDUSTRIES) or _extract_named_constraint(question, "industry")
        if industry:
            constraints["industry"] = industry
    elif target_table == "products":
        category = _known_value(question, PRODUCT_CATEGORIES) or _extract_named_constraint(question, "category")
        if category:
            constraints["category"] = category
    elif target_table == "sales_deals":
        stage = next((item[0] for item in DEAL_STAGES if re.search(rf"\b{re.escape(item[0])}\b", question, flags=re.IGNORECASE)), None)
        if stage:
            constraints["stage"] = stage

    return constraints




def _is_explicit_synthetic_generation(normalized: str, tokens: set[str]) -> bool:
    """Recognize only explicit bulk Faker requests.

    Do not treat the token "demo" inside a supplied name, such as "Demo User" or
    "Demo Retail Pvt Ltd", as a request to generate random records.
    """

    if tokens & {"synthetic", "fake", "faker", "random", "randomly", "randomized", "generate", "generated", "seed", "populate"}:
        return True

    if "demo" in tokens or "sample" in tokens:
        table_words = (
            "employees?", "workers?", "vendors?", "suppliers?", "customers?", "clients?",
            "products?", "items?", "sales\\s+deals?", "records?", "rows?", "data",
        )
        table_pattern = r"(?:" + "|".join(table_words) + r")"
        if re.search(rf"\b(?:generate|seed|populate)\b.*\b(?:demo|sample)\b.*\b{table_pattern}\b", normalized.casefold()):
            return True
        if re.search(rf"\b(?:generate|seed|populate)\b.*\b{table_pattern}\b", normalized.casefold()):
            return True
        if re.search(rf"\b(?:create|add|make)\s+\d+\s+(?:demo|sample)\s+{table_pattern}\b", normalized.casefold()):
            return True
        if re.search(rf"\b(?:demo|sample)\s+{table_pattern}\b", normalized.casefold()) and re.search(r"\b(?:records?|data|rows?)\b", normalized.casefold()):
            return True

    return False

def parse_synthetic_data_prompt(question: str) -> SyntheticDataRequest | None:
    """Recognize a safe schema-aware Faker request.

    Ordinary single-record CRUD prompts return ``None`` and remain on the existing LLM
    proposal path. Explicit random/demo/generate/seed/populate prompts are intercepted.
    """

    normalized = " ".join(question.strip().split())
    if not normalized:
        return None

    target_table = _target_from_prompt(normalized)
    if target_table is None:
        return None

    tokens = set(re.findall(r"[a-z0-9_-]+", normalized.casefold()))
    has_generation_signal = _is_explicit_synthetic_generation(normalized, tokens)
    if not has_generation_signal:
        return None

    count = _extract_count(normalized, target_table)
    if count is None:
        raise SyntheticDataGenerationError(
            "synthetic_record_count_required",
            f"State how many synthetic records to generate, from 1 to {MAX_SYNTHETIC_RECORDS}.",
        )
    if not 1 <= count <= MAX_SYNTHETIC_RECORDS:
        raise SyntheticDataGenerationError(
            "synthetic_record_count_out_of_range",
            f"Synthetic generation supports between 1 and {MAX_SYNTHETIC_RECORDS} records per preview.",
            details=[{"requested_count": count, "minimum": 1, "maximum": MAX_SYNTHETIC_RECORDS}],
        )

    return SyntheticDataRequest(
        target_table=target_table,
        count=count,
        constraints=_constraints_from_prompt(normalized, target_table),
        source_text=normalized,
    )


def validate_synthetic_data_request(request: SyntheticDataRequest) -> SyntheticDataRequest:
    target_table = request.target_table.strip().lower()
    if target_table in OPERATIONAL_TABLES:
        raise SyntheticDataGenerationError(
            "synthetic_target_not_allowed",
            "Synthetic generation is not allowed for operational/audit tables.",
            details=[{"target_table": target_table}],
        )
    if target_table not in set(_synthetic_table_names()):
        raise SyntheticDataGenerationError(
            "synthetic_target_unavailable",
            f"Table '{target_table}' is not present in the reflected PostgreSQL public schema.",
        )
    if not 1 <= int(request.count) <= MAX_SYNTHETIC_RECORDS:
        raise SyntheticDataGenerationError(
            "synthetic_record_count_out_of_range",
            f"Synthetic generation supports between 1 and {MAX_SYNTHETIC_RECORDS} records per preview.",
        )
    constraints = {
        str(key).strip().lower(): value
        for key, value in dict(request.constraints or {}).items()
        if str(key).strip()
    }
    return SyntheticDataRequest(
        target_table=target_table,
        count=int(request.count),
        constraints=constraints,
        source_text=request.source_text,
    )


def _batch_code(prefix: str, batch_id: str, index: int) -> str:
    return f"{prefix}-SYN-{batch_id}-{index:02d}"


def _email_local(*parts: str) -> str:
    value = re.sub(r"[^a-z0-9]+", ".", ".".join(parts).casefold()).strip(".")
    return value or "synthetic.record"


def _synthetic_phone(batch_id: str, index: int) -> str:
    base = int(batch_id[:10], 16) % 10_000_000_000
    number = (base + index) % 10_000_000_000
    return f"+91{number:010d}"


def _random_price(faker: Faker, minimum: int, maximum: int, step: int = 1000) -> int:
    return faker.random_int(min=minimum // step, max=maximum // step) * step


def _table_rows(
    db: Session,
    table_name: str,
    columns: tuple[str, ...],
    *,
    filters: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    table = get_runtime_table(table_name, bind=db.bind)
    selected = [table.c[name] for name in columns if name in table.c]
    if not selected:
        return []
    statement = select(*selected)
    for key, value in (filters or {}).items():
        if key in table.c and value not in (None, ""):
            statement = statement.where(table.c[key] == value)
    return [dict(row) for row in db.execute(statement).mappings().all()]


def _required_parent_rows(
    db: Session,
    *,
    table_name: str,
    columns: tuple[str, ...],
    filters: dict[str, Any] | None = None,
    relation_label: str,
) -> list[dict[str, Any]]:
    rows = _table_rows(db, table_name, columns, filters=filters)
    if not rows:
        qualifier = " matching the requested identifier" if filters else ""
        raise SyntheticDataGenerationError(
            "synthetic_parent_records_required",
            f"Create at least one {relation_label}{qualifier} before generating related records.",
            details=[{"parent_table": table_name, "filters": filters or {}}],
        )
    return rows


@register_synthetic_profile("employees")
def _generate_employees(db: Session, faker: Faker, request: SyntheticDataRequest, batch_id: str) -> list[dict[str, Any]]:
    del db
    records: list[dict[str, Any]] = []
    company_name = _normalize_text(request.constraints.get("company_name")) or DEFAULT_COMPANY_NAME
    department = _normalize_text(request.constraints.get("department"))
    city = _normalize_text(request.constraints.get("city"))
    for index in range(1, request.count + 1):
        first_name = faker.first_name()
        last_name = faker.last_name()
        token = f"{batch_id.casefold()}{index:02d}"
        records.append(
            {
                "employee_code": _batch_code("EMP", batch_id, index),
                "first_name": first_name,
                "last_name": last_name,
                "email": f"{_email_local(first_name, last_name)}.{token}@example.test",
                "phone": _synthetic_phone(batch_id, index),
                "department": department or faker.random_element(elements=DEPARTMENTS),
                "city": city or faker.random_element(elements=CITIES),
                "company_name": company_name,
                "salary": _random_price(faker, 30000, 150000, 1000),
                "employment_status": "active",
            }
        )
    return records


@register_synthetic_profile("vendors")
def _generate_vendors(db: Session, faker: Faker, request: SyntheticDataRequest, batch_id: str) -> list[dict[str, Any]]:
    del db
    records: list[dict[str, Any]] = []
    city = _normalize_text(request.constraints.get("city"))
    category = _normalize_text(request.constraints.get("category"))
    for index in range(1, request.count + 1):
        code = _batch_code("VEN", batch_id, index)
        name = f"{faker.company()} Synthetic {batch_id}-{index:02d}"
        records.append(
            {
                "vendor_code": code,
                "vendor_name": name[:200],
                "contact_email": f"vendor.{batch_id.casefold()}.{index:02d}@example.test",
                "phone": _synthetic_phone(batch_id, index),
                "city": city or faker.random_element(elements=CITIES),
                "country": DEFAULT_COUNTRY,
                "category": category or faker.random_element(elements=VENDOR_CATEGORIES),
                "status": "active",
            }
        )
    return records


@register_synthetic_profile("customers")
def _generate_customers(db: Session, faker: Faker, request: SyntheticDataRequest, batch_id: str) -> list[dict[str, Any]]:
    del db
    records: list[dict[str, Any]] = []
    city = _normalize_text(request.constraints.get("city"))
    industry = _normalize_text(request.constraints.get("industry"))
    for index in range(1, request.count + 1):
        records.append(
            {
                "customer_code": _batch_code("CUST", batch_id, index),
                "customer_name": f"{faker.company()} Customer {batch_id}-{index:02d}"[:200],
                "contact_email": f"customer.{batch_id.casefold()}.{index:02d}@example.test",
                "phone": _synthetic_phone(batch_id, index),
                "city": city or faker.random_element(elements=CITIES),
                "country": DEFAULT_COUNTRY,
                "industry": industry or faker.random_element(elements=CUSTOMER_INDUSTRIES),
                "status": "active",
            }
        )
    return records


@register_synthetic_profile("products")
def _generate_products(db: Session, faker: Faker, request: SyntheticDataRequest, batch_id: str) -> list[dict[str, Any]]:
    del db
    category = _normalize_text(request.constraints.get("category"))
    adjectives = ("Smart", "Precision", "Industrial", "Automated", "Advanced", "Connected")
    nouns = ("Loom Controller", "Quality Scanner", "Inventory Hub", "Vision Module", "Production Console", "Analytics Engine")
    records: list[dict[str, Any]] = []
    for index in range(1, request.count + 1):
        name = f"{faker.random_element(elements=adjectives)} {faker.random_element(elements=nouns)} {batch_id}-{index:02d}"
        records.append(
            {
                "product_code": _batch_code("PROD", batch_id, index),
                "product_name": name[:200],
                "category": category or faker.random_element(elements=PRODUCT_CATEGORIES),
                "description": f"Synthetic demonstration product generated by Faker for safe local testing. Batch {batch_id}, record {index}.",
                "list_price": _random_price(faker, 50000, 900000, 5000),
                "is_active": True,
            }
        )
    return records


@register_synthetic_profile("employee_permissions")
def _generate_employee_permissions(db: Session, faker: Faker, request: SyntheticDataRequest, batch_id: str) -> list[dict[str, Any]]:
    filters = {}
    if request.constraints.get("employee_code"):
        filters["employee_code"] = str(request.constraints["employee_code"]).upper()
    employees = _required_parent_rows(
        db,
        table_name="employees",
        columns=("id", "employee_code"),
        filters=filters,
        relation_label="employee",
    )
    records: list[dict[str, Any]] = []
    for index in range(1, request.count + 1):
        employee = employees[(index - 1) % len(employees)]
        base_permission = faker.random_element(elements=PERMISSION_CODES)
        # Batch suffix guarantees the employee_id + permission_code pair is unique.
        permission_code = f"{base_permission}_{batch_id}_{index:02d}"[:120]
        records.append(
            {
                "employee_id": int(employee["id"]),
                "permission_code": permission_code,
                "description": f"Synthetic {base_permission.replace('_', ' ').lower()} permission for {employee['employee_code']}.",
                "is_active": True,
            }
        )
    return records


@register_synthetic_profile("employee_experiences")
def _generate_employee_experiences(db: Session, faker: Faker, request: SyntheticDataRequest, batch_id: str) -> list[dict[str, Any]]:
    filters = {}
    if request.constraints.get("employee_code"):
        filters["employee_code"] = str(request.constraints["employee_code"]).upper()
    employees = _required_parent_rows(
        db,
        table_name="employees",
        columns=("id", "employee_code"),
        filters=filters,
        relation_label="employee",
    )
    today = date.today()
    records: list[dict[str, Any]] = []
    for index in range(1, request.count + 1):
        employee = employees[(index - 1) % len(employees)]
        years_ago = faker.random_int(min=2, max=12)
        start = date(today.year - years_ago, faker.random_int(min=1, max=12), 1)
        duration_days = faker.random_int(min=240, max=1500)
        end = min(start + timedelta(days=duration_days), today - timedelta(days=30))
        records.append(
            {
                "employee_id": int(employee["id"]),
                "company_name": f"{faker.company()} {batch_id}-{index:02d}"[:200],
                "job_title": faker.random_element(elements=EXPERIENCE_JOB_TITLES),
                "employment_type": faker.random_element(elements=EMPLOYMENT_TYPES),
                "location": faker.random_element(elements=CITIES),
                "start_date": start,
                "end_date": end,
                "description": f"Synthetic previous employment for {employee['employee_code']} generated for local demonstration.",
                "is_current": False,
            }
        )
    return records


@register_synthetic_profile("sales_deals")
def _generate_sales_deals(db: Session, faker: Faker, request: SyntheticDataRequest, batch_id: str) -> list[dict[str, Any]]:
    customer_filters = {}
    if request.constraints.get("customer_code"):
        customer_filters["customer_code"] = str(request.constraints["customer_code"]).upper()
    customers = _required_parent_rows(
        db,
        table_name="customers",
        columns=("id", "customer_code", "customer_name"),
        filters=customer_filters,
        relation_label="customer",
    )
    product_filters = {}
    if request.constraints.get("product_code"):
        product_filters["product_code"] = str(request.constraints["product_code"]).upper()
    products = _table_rows(db, "products", ("id", "product_code", "product_name"), filters=product_filters)
    employee_filters = {}
    if request.constraints.get("employee_code"):
        employee_filters["employee_code"] = str(request.constraints["employee_code"]).upper()
    employees = _table_rows(db, "employees", ("id", "employee_code"), filters=employee_filters)

    requested_stage = _normalize_text(request.constraints.get("stage"))
    records: list[dict[str, Any]] = []
    for index in range(1, request.count + 1):
        customer = customers[(index - 1) % len(customers)]
        product = products[(index - 1) % len(products)] if products else None
        employee = employees[(index - 1) % len(employees)] if employees else None
        stage, probability = next(
            ((name, chance) for name, chance in DEAL_STAGES if requested_stage and name == requested_stage.casefold()),
            faker.random_element(elements=DEAL_STAGES),
        )
        product_label = product["product_name"] if product else "Business Solution"
        records.append(
            {
                "deal_code": _batch_code("DEAL", batch_id, index),
                "title": f"{customer['customer_name']} - {product_label} - Synthetic Opportunity"[:250],
                "customer_id": int(customer["id"]),
                "product_id": int(product["id"]) if product else None,
                "owner_employee_id": int(employee["id"]) if employee else None,
                "amount": _random_price(faker, 100000, 2500000, 10000),
                "stage": stage,
                "probability": probability,
                # Keep the nullable date absent from JSONB/bind conversion complexity.
                "expected_close_date": None,
                "status": "open",
            }
        )
    return records


@register_synthetic_profile("product_vendor_mappings")
def _generate_product_vendor_mappings(db: Session, faker: Faker, request: SyntheticDataRequest, batch_id: str) -> list[dict[str, Any]]:
    product_filters = {}
    if request.constraints.get("product_code"):
        product_filters["product_code"] = str(request.constraints["product_code"]).upper()
    vendor_filters = {}
    if request.constraints.get("vendor_code"):
        vendor_filters["vendor_code"] = str(request.constraints["vendor_code"]).upper()

    products = _required_parent_rows(
        db,
        table_name="products",
        columns=("id", "product_code", "list_price"),
        filters=product_filters,
        relation_label="product",
    )
    vendors = _required_parent_rows(
        db,
        table_name="vendors",
        columns=("id", "vendor_code"),
        filters=vendor_filters,
        relation_label="vendor",
    )
    existing = {
        (int(row["product_id"]), int(row["vendor_id"]))
        for row in _table_rows(db, "product_vendor_mappings", ("product_id", "vendor_id"))
    }
    candidates = [
        (product, vendor)
        for product, vendor in itertools.product(products, vendors)
        if (int(product["id"]), int(vendor["id"])) not in existing
    ]
    random.SystemRandom().shuffle(candidates)
    if len(candidates) < request.count:
        raise SyntheticDataGenerationError(
            "insufficient_unique_parent_combinations",
            "There are not enough unused product-vendor combinations for the requested synthetic mapping count.",
            details=[
                {
                    "requested_count": request.count,
                    "available_unique_combinations": len(candidates),
                    "product_count": len(products),
                    "vendor_count": len(vendors),
                }
            ],
        )

    records: list[dict[str, Any]] = []
    for index, (product, vendor) in enumerate(candidates[: request.count], start=1):
        list_price = Decimal(str(product["list_price"]))
        multiplier = Decimal(str(faker.random_int(min=65, max=95))) / Decimal("100")
        quoted_price = (list_price * multiplier).quantize(Decimal("0.01"))
        records.append(
            {
                "product_id": int(product["id"]),
                "vendor_id": int(vendor["id"]),
                "vendor_sku": f"{vendor['vendor_code']}-{product['product_code']}-{batch_id}-{index:02d}"[:100],
                "quoted_price": float(quoted_price),
                "is_preferred": index == 1,
            }
        )
    return records


def _generic_foreign_key_values(db: Session, table: Any) -> dict[str, list[Any]]:
    values: dict[str, list[Any]] = {}
    for column in table.columns:
        for foreign_key in column.foreign_keys:
            parent_table_name = foreign_key.column.table.name
            parent_column_name = foreign_key.column.name
            try:
                parent_table = get_runtime_table(parent_table_name, bind=db.bind)
            except KeyError:
                continue
            rows = db.execute(select(parent_table.c[parent_column_name])).scalars().all()
            if not rows and not column.nullable:
                raise SyntheticDataGenerationError(
                    "synthetic_parent_records_required",
                    f"Create a parent record in '{parent_table_name}' before generating '{table.name}' records.",
                    details=[{"child_table": table.name, "foreign_key_column": column.name, "parent_table": parent_table_name}],
                )
            values[column.name] = list(rows)
    return values


def _database_generates_column(column: Any) -> bool:
    """Return whether INSERT must leave a reflected column to PostgreSQL."""

    if getattr(column, "identity", None) is not None:
        return True
    if getattr(column, "computed", None) is not None:
        return True
    if column.server_default is not None:
        return True
    if column.primary_key and getattr(column, "autoincrement", False) in (True, "auto"):
        return isinstance(column.type, Integer)
    return False


def _bounded_unique_text(
    *,
    table_name: str,
    column_name: str,
    batch_id: str,
    index: int,
    request_count: int,
    length: int | None,
) -> str:
    """Build a compact unique token while preserving its row suffix after truncation."""

    suffix_width = len(str(max(1, request_count)))
    suffix = str(index).zfill(suffix_width)
    if length is not None and length < suffix_width:
        raise SyntheticDataGenerationError(
            "synthetic_unique_column_too_short",
            f"Unique column '{column_name}' in '{table_name}' is too short for {request_count} distinct synthetic values.",
            details=[
                {
                    "table_name": table_name,
                    "column_name": column_name,
                    "maximum_length": length,
                    "requested_record_count": request_count,
                }
            ],
        )

    prefix = re.sub(r"[^a-z0-9]+", "", f"{table_name}-{column_name}-{batch_id}".casefold()) or "record"
    if length is None:
        return f"{prefix}-{suffix}"
    if length == suffix_width:
        return suffix
    head_length = length - suffix_width - 1
    if head_length <= 0:
        return suffix[-length:]
    return f"{prefix[:head_length]}-{suffix}"


def _unique_column_value(
    faker: Faker,
    *,
    column: Any,
    table_name: str,
    batch_id: str,
    index: int,
    request_count: int,
    foreign_key_values: dict[str, list[Any]],
) -> Any:
    """Generate a distinct value for one reflected singleton unique key."""

    if column.name in foreign_key_values:
        choices = foreign_key_values[column.name]
        if len(choices) < request_count:
            raise SyntheticDataGenerationError(
                "synthetic_unique_foreign_keys_unavailable",
                (
                    f"Table '{table_name}' needs {request_count} distinct parent values for unique foreign-key column "
                    f"'{column.name}', but only {len(choices)} are available."
                ),
                details=[
                    {
                        "table_name": table_name,
                        "column_name": column.name,
                        "requested_record_count": request_count,
                        "available_parent_values": len(choices),
                    }
                ],
            )
        return choices[index - 1]

    column_type = column.type
    if isinstance(column_type, Boolean):
        if request_count > 2:
            raise SyntheticDataGenerationError(
                "synthetic_unique_boolean_domain_exhausted",
                f"Unique Boolean column '{column.name}' can provide at most two distinct values.",
                details=[{"table_name": table_name, "column_name": column.name, "requested_record_count": request_count}],
            )
        return index == 1
    if isinstance(column_type, Integer):
        type_name = column_type.__class__.__name__.casefold()
        ceiling = 32_000 if "small" in type_name else 2_000_000_000
        seed = int(batch_id[:8], 16) % max(1, ceiling - request_count - 1)
        return seed + index
    if isinstance(column_type, Numeric):
        seed = int(batch_id[:8], 16) % 10_000_000
        return float(seed + index)
    if isinstance(column_type, DateTime):
        return f"{date.today().isoformat()}T00:00:{index:02d}"
    if isinstance(column_type, Date):
        return (date.today() + timedelta(days=index)).isoformat()
    if isinstance(column_type, (String, Text)):
        return _bounded_unique_text(
            table_name=table_name,
            column_name=column.name,
            batch_id=batch_id,
            index=index,
            request_count=request_count,
            length=getattr(column_type, "length", None),
        )
    return _bounded_unique_text(
        table_name=table_name,
        column_name=column.name,
        batch_id=batch_id,
        index=index,
        request_count=request_count,
        length=None,
    )


def _generic_column_value(
    faker: Faker,
    *,
    column: Any,
    table_name: str,
    batch_id: str,
    index: int,
    request_count: int,
    foreign_key_values: dict[str, list[Any]],
    force_unique: bool = False,
) -> Any:
    if force_unique:
        return _unique_column_value(
            faker,
            column=column,
            table_name=table_name,
            batch_id=batch_id,
            index=index,
            request_count=request_count,
            foreign_key_values=foreign_key_values,
        )
    if column.name in foreign_key_values:
        choices = foreign_key_values[column.name]
        return choices[(index - 1) % len(choices)] if choices else None

    name = column.name.casefold()
    token = f"{batch_id}-{index:02d}"
    column_type = column.type

    if name.endswith("_code") or name == "code":
        prefix = re.sub(r"[^A-Z]", "", table_name.upper())[:8] or "REC"
        return _batch_code(prefix, batch_id, index)[: getattr(column_type, "length", 120) or 120]
    if "email" in name:
        return f"{table_name}.{batch_id.casefold()}.{index:02d}@example.test"
    if "phone" in name:
        return _synthetic_phone(batch_id, index)
    if name in {"city", "location"}:
        return faker.random_element(elements=CITIES)
    if name == "country":
        return DEFAULT_COUNTRY
    if "status" in name:
        return "active"
    if "name" in name or name in {"title", "label"}:
        value = f"Synthetic {table_name.replace('_', ' ').title()} {token}"
        return value[: getattr(column_type, "length", 200) or 200]
    if "description" in name or "notes" in name:
        return f"Synthetic demonstration record for {table_name}, batch {batch_id}, item {index}."
    if isinstance(column_type, Boolean):
        return True
    if isinstance(column_type, Integer):
        return faker.random_int(min=1, max=100)
    if isinstance(column_type, Numeric):
        return float(faker.random_int(min=1000, max=100000))
    if isinstance(column_type, Date):
        return (date.today() + timedelta(days=index)).isoformat()
    if isinstance(column_type, DateTime):
        return faker.date_time_this_year().isoformat()
    if isinstance(column_type, (String, Text)):
        value = f"Synthetic {table_name} {token}"
        length = getattr(column_type, "length", None)
        return value[:length] if length else value
    return f"synthetic-{token}"


def _generate_from_schema(db: Session, faker: Faker, request: SyntheticDataRequest, batch_id: str) -> list[dict[str, Any]]:
    """Conservative fallback for reflected tables with ordinary columns."""

    table = get_runtime_table(request.target_table, bind=db.bind)
    foreign_key_values = _generic_foreign_key_values(db, table)
    unique_key_sets = get_unique_key_sets(db, request.target_table)
    singleton_unique_columns = {keys[0] for keys in unique_key_sets if len(keys) == 1}
    records: list[dict[str, Any]] = []
    for index in range(1, request.count + 1):
        record: dict[str, Any] = {}
        for column in table.columns:
            if column.name in MANAGED_COLUMNS or _database_generates_column(column):
                continue
            if column.name in request.constraints:
                record[column.name] = request.constraints[column.name]
                continue
            required = not column.nullable and column.default is None and column.server_default is None
            common_optional = any(token in column.name.casefold() for token in ("code", "name", "email", "phone", "city", "status"))
            if required or common_optional or column.foreign_keys:
                record[column.name] = _generic_column_value(
                    faker,
                    column=column,
                    table_name=request.target_table,
                    batch_id=batch_id,
                    index=index,
                    request_count=request.count,
                    foreign_key_values=foreign_key_values,
                    force_unique=column.name in singleton_unique_columns,
                )
        records.append(record)

    # Singleton unique columns are generated distinctly above. For composite unique
    # constraints, repair a colliding key through one non-FK generated member while
    # preserving every explicit user constraint and every valid parent reference.
    for _ in range(max(1, len(unique_key_sets))):
        collisions = find_batch_duplicates(request.target_table, records, db)
        if not collisions:
            break
        repaired = False
        for collision in collisions:
            duplicate_index = int(collision["duplicate_record_index"])
            key_fields = [str(item) for item in collision.get("key_fields") or []]
            candidate_name = next(
                (
                    name
                    for name in key_fields
                    if name in table.c
                    and name in records[duplicate_index]
                    and name not in request.constraints
                    and not table.c[name].foreign_keys
                    and not _database_generates_column(table.c[name])
                ),
                None,
            )
            if candidate_name is None:
                continue
            column = table.c[candidate_name]
            records[duplicate_index][candidate_name] = _unique_column_value(
                faker,
                column=column,
                table_name=request.target_table,
                batch_id=batch_id,
                index=duplicate_index + 1,
                request_count=request.count,
                foreign_key_values=foreign_key_values,
            )
            repaired = True
        if not repaired:
            break

    remaining_collisions = find_batch_duplicates(request.target_table, records, db)
    if remaining_collisions:
        raise SyntheticDataGenerationError(
            "synthetic_unique_values_unavailable",
            (
                f"The live unique constraints on '{request.target_table}' cannot support {request.count} distinct "
                "synthetic rows with the currently available parent records or requested fixed values."
            ),
            details=remaining_collisions,
        )
    return records


def generate_synthetic_records(db: Session, request: SyntheticDataRequest) -> SyntheticDataBatch:
    """Generate one validated batch for the normal confirmation-gated bulk path."""

    request = validate_synthetic_data_request(request)
    faker = Faker(DEFAULT_FAKER_LOCALE)
    batch_id = uuid4().hex[:10].upper()
    profile = PROFILE_REGISTRY.get(request.target_table)
    records = (
        profile(db, faker, request, batch_id)
        if profile is not None
        else _generate_from_schema(db, faker, request, batch_id)
    )
    if len(records) != request.count:
        raise SyntheticDataGenerationError(
            "synthetic_generation_count_mismatch",
            "The generator did not return the exact requested record count.",
            details=[{"requested_count": request.count, "generated_count": len(records), "target_table": request.target_table}],
        )

    metadata = {
        "generator": "faker",
        "generator_mode": "registered_profile" if profile is not None else "schema_fallback_reflected_postgresql",
        "synthetic_data_only": True,
        "faker_locale": DEFAULT_FAKER_LOCALE,
        "batch_id": batch_id,
        "requested_record_count": request.count,
        "generated_record_count": len(records),
        "target_table": request.target_table,
        "constraints": request.constraints,
        "supported_tables": _synthetic_table_names(),
        "foreign_keys_use_existing_parent_rows": True,
        "confirmation_required": True,
        "schema_source": "postgresql_reflection",
    }
    return SyntheticDataBatch(target_table=request.target_table, records=records, metadata=metadata)


def supported_synthetic_tables() -> list[dict[str, Any]]:
    """Frontend/status-safe summary of current and fallback generator coverage."""

    return [
        {
            "table_name": table_name,
            "generator_mode": "registered_profile" if table_name in PROFILE_REGISTRY else "schema_fallback_reflected_postgresql",
            "schema_source": "postgresql_reflection",
        }
        for table_name in _synthetic_table_names()
    ]
