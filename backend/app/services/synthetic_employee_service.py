"""Safe Faker-based synthetic employee generation for the CRUD preview workflow.

This module generates demo-only employee data. It does not talk to PostgreSQL and it
never writes records directly. Generated records must go through
``CrudWriteService.propose_bulk_insert`` so duplicate checks, confirmation, action logs,
and transaction handling remain exactly the same as for a manually supplied bulk batch.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any
from uuid import uuid4

from faker import Faker


MAX_SYNTHETIC_EMPLOYEES = 50
DEFAULT_COMPANY_NAME = "Neolotex"
DEFAULT_EMPLOYMENT_STATUS = "active"
DEFAULT_FAKER_LOCALE = "en_IN"

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
    "fifteen": 15,
    "twenty": 20,
    "twenty-five": 25,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
}

_GENERATION_WORDS = {"random", "synthetic", "fake", "sample", "demo", "generated", "generate"}
_EMPLOYEE_WORDS = {"employee", "employees", "worker", "workers", "staff", "person", "persons", "people"}


class SyntheticEmployeeGenerationError(ValueError):
    """Controlled input error for a requested synthetic employee batch."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class SyntheticEmployeeRequest:
    """Normalized generation request extracted from an API payload or user prompt."""

    count: int
    department: str | None = None
    city: str | None = None
    company_name: str | None = None
    employment_status: str = DEFAULT_EMPLOYMENT_STATUS


@dataclass(frozen=True)
class SyntheticEmployeeBatch:
    """Synthetic records plus non-sensitive generation metadata for the preview/audit trail."""

    records: list[dict[str, Any]]
    metadata: dict[str, Any]


def _normalize_optional_text(value: str | None, *, max_length: int = 120) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.strip().split())
    return normalized[:max_length] or None


def _extract_count(question: str) -> int | None:
    """Extract one requested employee count from a short natural-language prompt."""

    generation_qualifiers = r"(?:(?:random|synthetic|fake|sample|demo)\s+){0,3}"
    numeric_match = re.search(
        rf"\b([0-9]{{1,3}})\s+{generation_qualifiers}(?:employee|employees|worker|workers|staff|person|persons|people|records?)\b",
        question,
        flags=re.IGNORECASE,
    )
    if numeric_match:
        return int(numeric_match.group(1))

    for word, number in _WORD_NUMBER_MAP.items():
        if re.search(
            rf"\b{re.escape(word)}\s+{generation_qualifiers}(?:employee|employees|worker|workers|staff|person|persons|people|records?)\b",
            question,
            flags=re.IGNORECASE,
        ):
            return number
    return None


def _extract_from_known_values(question: str, choices: tuple[str, ...]) -> str | None:
    for choice in choices:
        if re.search(rf"\b{re.escape(choice)}\b", question, flags=re.IGNORECASE):
            return choice
    return None


def _extract_company_name(question: str) -> str | None:
    """Read a simple 'for/at <company>' qualifier without treating departments as companies."""

    match = re.search(
        r"\b(?:for|at)\s+(?:the\s+)?([A-Za-z][A-Za-z0-9&.' -]{1,70}?)(?:\s+(?:company|employees?|workers?|staff|department|in|with)\b|[.,;]|$)",
        question,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    candidate = _normalize_optional_text(match.group(1))
    if not candidate:
        return None
    disallowed = {item.casefold() for item in DEPARTMENTS} | {item.casefold() for item in CITIES} | {"the", "a", "an"}
    return None if candidate.casefold() in disallowed else candidate


def parse_synthetic_employee_prompt(question: str) -> SyntheticEmployeeRequest | None:
    """Recognize requests such as 'Create 10 random synthetic employees'.

    ``None`` means this is an ordinary CRUD request and should remain on the existing
    LLM SQL-proposal route. A malformed synthetic request raises a controlled error so
    the caller can request a smaller/clearer batch instead of silently generating data.
    """

    normalized = " ".join(question.strip().split())
    if not normalized:
        return None

    tokens = set(re.findall(r"[a-z0-9-]+", normalized.casefold()))
    has_employee_target = bool(tokens & _EMPLOYEE_WORDS)
    has_generation_signal = bool(tokens & _GENERATION_WORDS)
    if not (has_employee_target and has_generation_signal):
        return None

    count = _extract_count(normalized)
    if count is None:
        raise SyntheticEmployeeGenerationError(
            "synthetic_employee_count_required",
            f"State how many synthetic employees to generate, from 1 to {MAX_SYNTHETIC_EMPLOYEES}.",
        )

    return validate_synthetic_employee_request(
        SyntheticEmployeeRequest(
            count=count,
            department=_extract_from_known_values(normalized, DEPARTMENTS),
            city=_extract_from_known_values(normalized, CITIES),
            company_name=_extract_company_name(normalized),
        )
    )


def validate_synthetic_employee_request(request: SyntheticEmployeeRequest) -> SyntheticEmployeeRequest:
    """Validate programmatic/direct-endpoint generation options before Faker is called."""

    if not 1 <= request.count <= MAX_SYNTHETIC_EMPLOYEES:
        raise SyntheticEmployeeGenerationError(
            "synthetic_employee_count_out_of_range",
            f"Synthetic employee generation supports between 1 and {MAX_SYNTHETIC_EMPLOYEES} records per preview.",
        )

    department = _normalize_optional_text(request.department)
    city = _normalize_optional_text(request.city)
    company_name = _normalize_optional_text(request.company_name)
    employment_status = _normalize_optional_text(request.employment_status) or DEFAULT_EMPLOYMENT_STATUS

    if department and department.casefold() not in {item.casefold() for item in DEPARTMENTS}:
        raise SyntheticEmployeeGenerationError(
            "unsupported_synthetic_department",
            f"Department must be one of: {', '.join(DEPARTMENTS)}.",
        )
    if city and city.casefold() not in {item.casefold() for item in CITIES}:
        raise SyntheticEmployeeGenerationError(
            "unsupported_synthetic_city",
            f"City must be one of: {', '.join(CITIES)}.",
        )
    if employment_status.casefold() != DEFAULT_EMPLOYMENT_STATUS:
        raise SyntheticEmployeeGenerationError(
            "unsupported_synthetic_employment_status",
            "Synthetic employee generation currently creates active demo employees only.",
        )

    return SyntheticEmployeeRequest(
        count=request.count,
        department=next((item for item in DEPARTMENTS if item.casefold() == department.casefold()), None) if department else None,
        city=next((item for item in CITIES if item.casefold() == city.casefold()), None) if city else None,
        company_name=company_name or DEFAULT_COMPANY_NAME,
        employment_status=DEFAULT_EMPLOYMENT_STATUS,
    )


def _safe_email_local_part(first_name: str, last_name: str) -> str:
    value = re.sub(r"[^a-z0-9]+", ".", f"{first_name}.{last_name}".casefold()).strip(".")
    return value or "synthetic.employee"


def generate_synthetic_employees(request: SyntheticEmployeeRequest) -> SyntheticEmployeeBatch:
    """Generate unique, non-real employee records for the existing bulk-preview flow."""

    request = validate_synthetic_employee_request(request)
    faker = Faker(DEFAULT_FAKER_LOCALE)
    batch_id = uuid4().hex[:10].upper()
    records: list[dict[str, Any]] = []

    for index in range(1, request.count + 1):
        first_name = faker.first_name()
        last_name = faker.last_name()
        email_local = _safe_email_local_part(first_name, last_name)
        token = f"{batch_id.lower()}{index:02d}"
        records.append(
            {
                "employee_code": f"EMP-SYN-{batch_id}-{index:02d}",
                "first_name": first_name,
                "last_name": last_name,
                # Reserved example.test domain makes the generated email explicitly non-real.
                "email": f"{email_local}.{token}@example.test",
                "department": request.department or faker.random_element(elements=DEPARTMENTS),
                "city": request.city or faker.random_element(elements=CITIES),
                "company_name": request.company_name or DEFAULT_COMPANY_NAME,
                "salary": faker.random_int(min=30000, max=90000, step=1000),
                "employment_status": request.employment_status,
            }
        )

    return SyntheticEmployeeBatch(
        records=records,
        metadata={
            "generator": "faker",
            "synthetic_data_only": True,
            "faker_locale": DEFAULT_FAKER_LOCALE,
            "batch_id": batch_id,
            "generated_record_count": len(records),
            "target_table": "employees",
            "email_domain": "example.test",
            "department_constraint": request.department,
            "city_constraint": request.city,
            "company_name": request.company_name or DEFAULT_COMPANY_NAME,
            "employment_status": request.employment_status,
        },
    )
