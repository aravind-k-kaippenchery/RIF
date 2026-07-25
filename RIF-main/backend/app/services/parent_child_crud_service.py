"""Semantic, schema-grounded parent-child CRUD service.

The local model is used only to convert natural language into a typed intent plan.  It
never chooses database IDs, never generates executable SQL, and never writes data.  This
service resolves stable business codes against PostgreSQL, validates every field against
SQLAlchemy metadata, builds bounded SQLAlchemy queries, and sends writes through the
existing confirmation-gated CRUD service.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
import re
from typing import Any, Iterable
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import Boolean, Date, DateTime, Integer, Numeric, String, Text, and_, func, select
from sqlalchemy.orm import Session

from app.core.constants import AgentRoute, ResponseStatus, UserRole
from app.models import Base
from app.schemas.phase17 import ChildFilter, ParentChildIntentPlan, ParentReference
from app.services.crud_write_service import CrudWriteError, crud_write_service
from app.services.duplicate_service import json_safe, row_to_dict
from app.services.llm_service import LLMOutputValidationError, LLMServiceError, llm_service


MAX_RELATIONSHIP_ROWS = 25
MIN_PLAN_CONFIDENCE = 0.68
MANAGED_COLUMNS = {"id", "created_at", "updated_at"}


@dataclass(frozen=True)
class ParentLink:
    role: str
    table: str
    foreign_key: str
    code_column: str
    required_on_create: bool = True


@dataclass(frozen=True)
class RelationshipDefinition:
    child_table: str
    readable_name: str
    parents: tuple[ParentLink, ...]
    mutable_fields: tuple[str, ...]
    required_create_fields: tuple[str, ...]
    ordering_field: str = "created_at"


RELATIONSHIPS: dict[str, RelationshipDefinition] = {
    "employee_experiences": RelationshipDefinition(
        child_table="employee_experiences",
        readable_name="employee experience",
        parents=(ParentLink("employee", "employees", "employee_id", "employee_code"),),
        mutable_fields=(
            "company_name",
            "job_title",
            "employment_type",
            "location",
            "start_date",
            "end_date",
            "description",
            "is_current",
        ),
        required_create_fields=("company_name", "job_title", "start_date"),
        ordering_field="start_date",
    ),
    "employee_permissions": RelationshipDefinition(
        child_table="employee_permissions",
        readable_name="employee permission",
        parents=(ParentLink("employee", "employees", "employee_id", "employee_code"),),
        mutable_fields=("permission_code", "description", "is_active"),
        required_create_fields=("permission_code", "description"),
    ),
    "product_vendor_mappings": RelationshipDefinition(
        child_table="product_vendor_mappings",
        readable_name="product-vendor mapping",
        parents=(
            ParentLink("product", "products", "product_id", "product_code"),
            ParentLink("vendor", "vendors", "vendor_id", "vendor_code"),
        ),
        mutable_fields=("vendor_sku", "quoted_price", "is_preferred"),
        required_create_fields=("quoted_price",),
    ),
    "sales_deals": RelationshipDefinition(
        child_table="sales_deals",
        readable_name="sales deal",
        parents=(
            ParentLink("customer", "customers", "customer_id", "customer_code"),
            ParentLink("product", "products", "product_id", "product_code", required_on_create=False),
            ParentLink("owner_employee", "employees", "owner_employee_id", "employee_code", required_on_create=False),
        ),
        mutable_fields=(
            "deal_code",
            "title",
            "amount",
            "stage",
            "probability",
            "expected_close_date",
            "status",
        ),
        required_create_fields=("deal_code", "title", "amount", "stage", "probability"),
        ordering_field="expected_close_date",
    ),
}


ROLE_ALIASES = {
    "employee": "employee",
    "worker": "employee",
    "staff": "employee",
    "owner": "owner_employee",
    "owner_employee": "owner_employee",
    "sales_owner": "owner_employee",
    "product": "product",
    "item": "product",
    "vendor": "vendor",
    "supplier": "vendor",
    "customer": "customer",
    "client": "customer",
}

FIELD_ALIASES: dict[str, dict[str, str]] = {
    "employee_experiences": {
        "company": "company_name",
        "employer": "company_name",
        "previous_company": "company_name",
        "role": "job_title",
        "title": "job_title",
        "position": "job_title",
        "type": "employment_type",
        "employment": "employment_type",
        "city": "location",
        "start": "start_date",
        "started": "start_date",
        "end": "end_date",
        "ended": "end_date",
        "current": "is_current",
        "summary": "description",
    },
    "employee_permissions": {
        "permission": "permission_code",
        "code": "permission_code",
        "active": "is_active",
    },
    "product_vendor_mappings": {
        "price": "quoted_price",
        "quote": "quoted_price",
        "quoted": "quoted_price",
        "sku": "vendor_sku",
        "preferred": "is_preferred",
    },
    "sales_deals": {
        "code": "deal_code",
        "name": "title",
        "deal_title": "title",
        "value": "amount",
        "close_date": "expected_close_date",
        "close": "expected_close_date",
    },
}


class ParentChildError(RuntimeError):
    def __init__(
        self,
        *,
        status: ResponseStatus,
        code: str,
        message: str,
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.details = details or []
        super().__init__(message)


@dataclass(frozen=True)
class ParentChildResult:
    route: AgentRoute
    status: ResponseStatus
    answer: str
    data: dict[str, Any]
    generated_sql: str | None = None
    pending_action_id: str | None = None


def supported_parent_child_tables() -> list[str]:
    return list(RELATIONSHIPS)


def looks_like_parent_child_request(question: str) -> bool:
    """Detect relationship-oriented language without deciding the operation itself."""

    normalized = " ".join(str(question or "").casefold().split())
    if not normalized:
        return False
    strong_phrases = (
        "experience",
        "experiences",
        "work history",
        "employment history",
        "previous company",
        "previous employer",
        "worked before",
        "worked at",
        "previously worked",
        "permission",
        "permissions",
        "product vendor mapping",
        "vendor mapping",
        "supplier mapping",
        "link product",
        "link vendor",
        "map product",
        "map vendor",
    )
    if any(phrase in normalized for phrase in strong_phrases):
        return True
    code_count = len(re.findall(r"\b(?:EMP|PRD|PROD|VND|VEN|CUST|CUS)-[A-Z0-9-]+\b", question, flags=re.IGNORECASE))
    if code_count >= 2 and re.search(r"\b(?:deal|opportunity|link|mapping|map|assign|relate)\b", normalized):
        return True
    if re.search(r"\b(?:deal|opportunity)\b", normalized) and re.search(r"\b(?:CUST|CUS)-[A-Z0-9-]+\b", question, flags=re.IGNORECASE):
        return True
    if (
        re.search(r"\bEMP-[A-Z0-9-]+\b", question, flags=re.IGNORECASE)
        and re.search(r"\b(?:change|update|modify|set|rename)\b", normalized)
        and re.search(r"\b(?:role|title|position)\b", normalized)
    ):
        return True
    return False




_EMPLOYEE_CODE_PATTERN = re.compile(r"\bEMP-[A-Z0-9-]+\b", flags=re.IGNORECASE)
_ISO_DATE_PATTERN = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def _clean_extracted_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.strip(" ,.;:-").split())
    return cleaned or None


def _employment_type_from_text(question: str) -> str | None:
    normalized = " ".join(question.casefold().replace("-", " ").split())
    mappings = (
        ("full time", "full_time"),
        ("part time", "part_time"),
        ("internship", "internship"),
        ("intern", "internship"),
        ("contract", "contract"),
        ("freelance", "freelance"),
    )
    for phrase, canonical in mappings:
        if re.search(rf"\b{re.escape(phrase)}\b", normalized):
            return canonical
    return None


def _parse_clear_employee_experience_request(question: str) -> ParentChildIntentPlan | None:
    """Parse clear employee-experience requests before invoking Ollama.

    This parser handles the common, fully specified work-history forms deterministically.
    It exists so a clear request such as ``Add experience for EMP-105 at Infosys as
    Python Developer from 2021-01-01 to 2024-01-01`` cannot fail merely because an 8B
    local model emitted a malformed JSON plan.  It never invents missing business data:
    incomplete create/update/delete requests become a concise clarification plan.
    """

    original = " ".join(str(question or "").strip().split())
    normalized = original.casefold()
    experience_signal = bool(
        re.search(
            r"\b(?:experience|experiences|work history|employment history|previous job|previous employer|worked at)\b",
            normalized,
        )
    )
    employee_role_change_signal = bool(
        _EMPLOYEE_CODE_PATTERN.search(original)
        and re.search(r"\b(?:change|update|modify|set|rename)\b", normalized)
        and re.search(r"\b(?:role|title|position)\b", normalized)
    )
    if not original or not (experience_signal or employee_role_change_signal):
        return None

    employee_match = _EMPLOYEE_CODE_PATTERN.search(original)
    employee_code = employee_match.group(0).upper() if employee_match else None
    write_create = bool(re.search(r"\b(?:add|create|insert|record)\b", normalized))
    write_update = bool(re.search(r"\b(?:change|update|modify|set|rename)\b", normalized))
    write_delete = bool(re.search(r"\b(?:delete|remove)\b", normalized))
    read_request = bool(re.search(r"\b(?:show|list|display|view|see|get|find|what)\b", normalized))

    if not employee_code:
        return ParentChildIntentPlan(
            operation="clarify",
            confidence=1.0,
            clarification_question="Which employee should I use? Please provide the employee code, such as EMP-105.",
            interpretation="Employee experience request is missing the parent employee code.",
        )

    parent = [ParentReference(role="employee", code=employee_code)]

    if write_create:
        # Preferred order: at COMPANY as TITLE from START to END.
        company: str | None = None
        job_title: str | None = None
        start_date: str | None = None
        end_date: str | None = None

        full_match = re.search(
            r"\bat\s+(?P<company>.+?)\s+as\s+(?P<title>.+?)\s+"
            r"(?:from\s+|between\s+)(?P<start>\d{4}-\d{2}-\d{2})\s+"
            r"(?:to|and|until|through)\s+(?P<end>\d{4}-\d{2}-\d{2})\b",
            original,
            flags=re.IGNORECASE,
        )
        reverse_match = re.search(
            r"\bas\s+(?P<title>.+?)\s+at\s+(?P<company>.+?)\s+"
            r"(?:from\s+|between\s+)(?P<start>\d{4}-\d{2}-\d{2})\s+"
            r"(?:to|and|until|through)\s+(?P<end>\d{4}-\d{2}-\d{2})\b",
            original,
            flags=re.IGNORECASE,
        )
        matched = full_match or reverse_match
        if matched:
            company = _clean_extracted_text(matched.group("company"))
            job_title = _clean_extracted_text(matched.group("title"))
            start_date = matched.group("start")
            end_date = matched.group("end")
        else:
            company_match = re.search(
                r"\bat\s+(?P<company>.+?)(?=\s+as\s+|\s+(?:from|between|since|starting)\s+|$)",
                original,
                flags=re.IGNORECASE,
            )
            title_match = re.search(
                r"\bas\s+(?P<title>.+?)(?=\s+at\s+|\s+(?:from|between|since|starting)\s+|$)",
                original,
                flags=re.IGNORECASE,
            )
            company = _clean_extracted_text(company_match.group("company")) if company_match else None
            job_title = _clean_extracted_text(title_match.group("title")) if title_match else None
            dates = _ISO_DATE_PATTERN.findall(original)
            start_date = dates[0] if dates else None
            end_date = dates[1] if len(dates) > 1 else None

        missing: list[str] = []
        if not company:
            missing.append("previous company")
        if not job_title:
            missing.append("job title")
        if not start_date:
            missing.append("start date")
        if missing:
            return ParentChildIntentPlan(
                operation="clarify",
                child_table="employee_experiences",
                parent_references=parent,
                confidence=1.0,
                clarification_question=(
                    f"Please provide the {', '.join(missing)} for {employee_code}. "
                    "For example: at Infosys as Python Developer from 2021-01-01 to 2024-01-01."
                ),
                interpretation="Employee experience create request is missing required values.",
            )

        values: dict[str, Any] = {
            "company_name": company,
            "job_title": job_title,
            "start_date": start_date,
            "is_current": end_date is None,
        }
        if end_date is not None:
            values["end_date"] = end_date
        employment_type = _employment_type_from_text(original)
        if employment_type:
            values["employment_type"] = employment_type

        return ParentChildIntentPlan(
            operation="create",
            child_table="employee_experiences",
            parent_references=parent,
            values=values,
            confidence=1.0,
            interpretation="Deterministically parsed a complete employee experience creation request.",
        )

    if write_update:
        update_match = re.search(
            r"\b(?P<code>EMP-[A-Z0-9-]+)(?:'s)?\s+(?P<company>.+?)\s+"
            r"(?:role|title|position)\s+(?:to|as)\s+(?P<title>.+)$",
            original,
            flags=re.IGNORECASE,
        )
        if not update_match:
            update_match = re.search(
                r"\b(?:change|update|modify|set)\s+(?:the\s+)?(?:role|title|position)\s+"
                r"(?:for|of)\s+(?P<code>EMP-[A-Z0-9-]+)\s+(?:at\s+)?(?P<company>.+?)\s+"
                r"(?:to|as)\s+(?P<title>.+)$",
                original,
                flags=re.IGNORECASE,
            )
        if update_match:
            company = _clean_extracted_text(update_match.group("company"))
            title = _clean_extracted_text(update_match.group("title"))
            if company and title:
                return ParentChildIntentPlan(
                    operation="update",
                    child_table="employee_experiences",
                    parent_references=parent,
                    filters=[ChildFilter(field="company_name", operator="eq", value=company)],
                    values={"job_title": title},
                    confidence=1.0,
                    interpretation="Deterministically parsed an employee experience job-title update.",
                )
        return ParentChildIntentPlan(
            operation="clarify",
            child_table="employee_experiences",
            parent_references=parent,
            confidence=1.0,
            clarification_question=(
                f"Which {employee_code} experience should I update, which field should change, and what is the new value?"
            ),
            interpretation="Employee experience update request is missing an exact child selector or new value.",
        )

    if write_delete:
        selection = "all"
        if re.search(r"\boldest\b", normalized):
            selection = "oldest"
        elif re.search(r"\b(?:latest|newest|most recent)\b", normalized):
            selection = "latest"

        filters: list[ChildFilter] = []
        company_match = re.search(
            r"\b(?:at|from)\s+(?P<company>.+?)(?=\s+(?:for|of)\s+EMP-|\s*$)",
            original,
            flags=re.IGNORECASE,
        )
        company = _clean_extracted_text(company_match.group("company")) if company_match else None
        if company:
            filters.append(ChildFilter(field="company_name", operator="eq", value=company))

        if selection == "all" and not filters:
            return ParentChildIntentPlan(
                operation="clarify",
                child_table="employee_experiences",
                parent_references=parent,
                confidence=1.0,
                clarification_question=(
                    f"Which {employee_code} experience should I delete? State the company, or say oldest/latest."
                ),
                interpretation="Employee experience delete request is missing an exact child selector.",
            )
        return ParentChildIntentPlan(
            operation="delete",
            child_table="employee_experiences",
            parent_references=parent,
            filters=filters,
            selection=selection,
            confidence=1.0,
            interpretation="Deterministically parsed an employee experience deletion request.",
        )

    if read_request or re.search(r"\b(?:experience|work history|employment history)\b", normalized):
        return ParentChildIntentPlan(
            operation="read",
            child_table="employee_experiences",
            parent_references=parent,
            confidence=1.0,
            interpretation="Deterministically parsed an employee work-history read request.",
        )

    return None


def _contract_for_prompt() -> dict[str, Any]:
    contract: dict[str, Any] = {}
    for name, definition in RELATIONSHIPS.items():
        contract[name] = {
            "readable_name": definition.readable_name,
            "parents": [
                {
                    "role": item.role,
                    "table": item.table,
                    "business_code_field": item.code_column,
                    "required_on_create": item.required_on_create,
                }
                for item in definition.parents
            ],
            "allowed_child_fields": list(definition.mutable_fields),
            "required_create_fields": list(definition.required_create_fields),
        }
    return contract


def _semantic_plan_validator(output: BaseModel) -> tuple[bool, str]:
    plan = ParentChildIntentPlan.model_validate(output)
    if plan.operation == "clarify":
        return True, ""
    if plan.child_table not in RELATIONSHIPS:
        return False, f"child_table must be one of: {', '.join(RELATIONSHIPS)}."
    definition = RELATIONSHIPS[plan.child_table]
    valid_roles = {item.role for item in definition.parents}
    invalid_roles = sorted(
        {
            ROLE_ALIASES.get(reference.role.casefold(), reference.role.casefold())
            for reference in plan.parent_references
        }
        - valid_roles
    )
    if invalid_roles:
        return False, f"Invalid parent role(s) for {plan.child_table}: {', '.join(invalid_roles)}."
    allowed_fields = set(definition.mutable_fields)
    aliases = FIELD_ALIASES.get(plan.child_table, {})
    supplied_fields = {
        aliases.get(str(field).casefold(), str(field).casefold())
        for field in [*plan.values.keys(), *(item.field for item in plan.filters), *plan.requested_columns]
    }
    invalid_fields = sorted(supplied_fields - allowed_fields - {"id"})
    if invalid_fields:
        return False, f"Unapproved child field(s): {', '.join(invalid_fields)}."
    return True, ""


def _unwrap_value(value: Any, *, depth: int = 0) -> Any:
    """Normalize common typed JSON wrappers without stringifying arbitrary objects."""

    if depth > 5:
        raise ParentChildError(
            status=ResponseStatus.CLARIFICATION_REQUIRED,
            code="relationship_value_too_nested",
            message="One supplied value is too deeply nested. Please provide a plain value.",
        )
    if not isinstance(value, dict):
        if isinstance(value, list):
            return [_unwrap_value(item, depth=depth + 1) for item in value]
        return value
    known_keys = ("value", "string", "number", "integer", "boolean", "date", "values")
    present = [key for key in known_keys if key in value]
    if len(present) == 1:
        return _unwrap_value(value[present[0]], depth=depth + 1)
    raise ParentChildError(
        status=ResponseStatus.CLARIFICATION_REQUIRED,
        code="relationship_value_object_not_supported",
        message="I could not safely interpret one value. Please provide it as plain text, a number, a date, or true/false.",
    )


def _canonical_field(table_name: str, field: str) -> str:
    normalized = str(field).strip().casefold().replace(" ", "_").replace("-", "_")
    return FIELD_ALIASES.get(table_name, {}).get(normalized, normalized)


def _coerce_value(column: Any, value: Any) -> Any:
    value = _unwrap_value(value)
    if value is None:
        return None
    column_type = column.type
    try:
        if isinstance(column_type, Boolean):
            if isinstance(value, bool):
                return value
            normalized = str(value).strip().casefold()
            if normalized in {"true", "yes", "1", "active", "current"}:
                return True
            if normalized in {"false", "no", "0", "inactive", "previous"}:
                return False
            raise ValueError
        if isinstance(column_type, Integer):
            if isinstance(value, bool):
                raise ValueError
            return int(value)
        if isinstance(column_type, Numeric):
            return Decimal(str(value).replace(",", ""))
        if isinstance(column_type, DateTime):
            if isinstance(value, datetime):
                return value
            return datetime.fromisoformat(str(value))
        if isinstance(column_type, Date):
            if isinstance(value, date) and not isinstance(value, datetime):
                return value
            text_value = str(value).strip()
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text_value):
                raise ValueError
            return date.fromisoformat(text_value)
        if isinstance(column_type, (String, Text)):
            return " ".join(str(value).strip().split())
    except (ValueError, TypeError, InvalidOperation) as exc:
        raise ParentChildError(
            status=ResponseStatus.CLARIFICATION_REQUIRED,
            code="relationship_value_type_invalid",
            message=f"The value for '{column.name}' does not match its database type. Please provide a clear value.",
            details=[{"field": column.name, "expected_type": str(column.type), "received_value": json_safe(value)}],
        ) from exc
    return value


class ParentChildCrudService:
    def _generate_plan(
        self,
        question: str,
        memory_context: dict[str, Any] | None = None,
    ) -> tuple[ParentChildIntentPlan, dict[str, Any]]:
        deterministic_plan = _parse_clear_employee_experience_request(question)
        if deterministic_plan is not None:
            return deterministic_plan, {
                "model": "deterministic_parent_child_parser",
                "attempts": 0,
                "source": "deterministic_employee_experience_parser",
                "ollama_called": False,
            }

        contract = _contract_for_prompt()
        system_prompt = (
            "Interpret one natural-language parent-child database request. Return only the typed JSON plan. "
            "Do not generate SQL. Do not invent a table, parent code, child field, value, date, or operation. "
            "Use operation='clarify' with one concise question whenever essential information is missing or multiple meanings are possible. "
            "Use canonical table, role, and field names from the supplied relationship contract. "
            "Set apply_to_all_matches=true only when the user explicitly says all/every/matching records. "
            "For an employee work-history request use employee_experiences. For an employee permission use employee_permissions. "
            "For product/vendor links use product_vendor_mappings. For customer/product/owner deal relationships use sales_deals."
        )
        safe_context = memory_context if isinstance(memory_context, dict) and memory_context.get("available") else None
        context_text = json.dumps(safe_context, indent=2) if safe_context else "No prior context was explicitly referenced."
        user_prompt = (
            f"Relationship contract:\n{json.dumps(contract, indent=2)}\n\n"
            f"Explicitly referenced session context (use only to resolve words such as this/that/them):\n{context_text}\n\n"
            "Examples:\n"
            "- 'Show experience of EMP-105' => read employee_experiences with employee parent EMP-105.\n"
            "- 'Add experience for EMP-105 at Infosys as Python Developer from 2021-01-01 to 2024-02-01' => create.\n"
            "- 'Change EMP-105 Infosys role to Senior Developer' => update, filter company_name=Infosys, value job_title.\n"
            "- 'Link PRD-001 to VND-001 with quoted price 450000' => create product_vendor_mappings.\n"
            "- 'Show deals for CUS-001' => read sales_deals with customer reference.\n\n"
            f"User request: {question}"
        )
        plan, metadata = llm_service.generate_json(
            output_model=ParentChildIntentPlan,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            semantic_validator=_semantic_plan_validator,
            temperature=0.0,
            seed=43,
        )
        return plan, metadata.model_dump()

    @staticmethod
    def _definition(plan: ParentChildIntentPlan) -> RelationshipDefinition:
        if plan.child_table not in RELATIONSHIPS:
            raise ParentChildError(
                status=ResponseStatus.CLARIFICATION_REQUIRED,
                code="relationship_target_required",
                message=plan.clarification_question or "Which child record type do you want to work with?",
            )
        return RELATIONSHIPS[plan.child_table]

    @staticmethod
    def _normalize_parent_references(
        plan: ParentChildIntentPlan,
        definition: RelationshipDefinition,
    ) -> list[ParentReference]:
        allowed_roles = {item.role for item in definition.parents}
        normalized: list[ParentReference] = []
        seen: set[str] = set()
        for reference in plan.parent_references:
            role = ROLE_ALIASES.get(reference.role.casefold(), reference.role.casefold())
            if role not in allowed_roles:
                raise ParentChildError(
                    status=ResponseStatus.CLARIFICATION_REQUIRED,
                    code="relationship_parent_role_invalid",
                    message=f"'{reference.role}' is not a valid parent reference for {definition.readable_name}.",
                )
            if role in seen:
                raise ParentChildError(
                    status=ResponseStatus.CLARIFICATION_REQUIRED,
                    code="relationship_parent_reference_duplicated",
                    message=f"Please provide only one {role.replace('_', ' ')} reference.",
                )
            seen.add(role)
            normalized.append(reference.model_copy(update={"role": role}))
        return normalized

    @staticmethod
    def _resolve_parent(
        db: Session,
        link: ParentLink,
        reference: ParentReference,
    ) -> dict[str, Any]:
        table = Base.metadata.tables[link.table]
        statement = select(table)
        if reference.id is not None:
            statement = statement.where(table.c.id == reference.id)
        else:
            assert reference.code is not None
            statement = statement.where(func.lower(table.c[link.code_column]) == reference.code.casefold())
        rows = db.execute(statement.limit(2)).mappings().all()
        if not rows:
            supplied = reference.code if reference.code is not None else reference.id
            raise ParentChildError(
                status=ResponseStatus.CLARIFICATION_REQUIRED,
                code="relationship_parent_not_found",
                message=(
                    f"I could not find the {link.role.replace('_', ' ')} '{supplied}' in {link.table}. "
                    f"Please provide a valid {link.code_column} or ID."
                ),
                details=[{"parent_table": link.table, "code_column": link.code_column, "supplied": supplied}],
            )
        if len(rows) > 1:
            raise ParentChildError(
                status=ResponseStatus.CLARIFICATION_REQUIRED,
                code="relationship_parent_ambiguous",
                message=f"More than one {link.role.replace('_', ' ')} matched. Please provide its exact business code.",
            )
        return row_to_dict(rows[0])

    def _resolved_parents(
        self,
        db: Session,
        plan: ParentChildIntentPlan,
        definition: RelationshipDefinition,
    ) -> dict[str, dict[str, Any]]:
        references = self._normalize_parent_references(plan, definition)
        by_role = {reference.role: reference for reference in references}
        resolved: dict[str, dict[str, Any]] = {}
        for link in definition.parents:
            reference = by_role.get(link.role)
            if reference is None:
                if plan.operation == "create" and link.required_on_create:
                    raise ParentChildError(
                        status=ResponseStatus.CLARIFICATION_REQUIRED,
                        code="relationship_parent_required",
                        message=f"Which {link.role.replace('_', ' ')} should this {definition.readable_name} belong to? Provide its {link.code_column}.",
                        details=[{"required_parent_role": link.role, "code_column": link.code_column}],
                    )
                continue
            resolved[link.role] = self._resolve_parent(db, link, reference)
        return resolved

    @staticmethod
    def _normalized_values(
        definition: RelationshipDefinition,
        raw_values: dict[str, Any],
    ) -> dict[str, Any]:
        table = Base.metadata.tables[definition.child_table]
        allowed = set(definition.mutable_fields)
        values: dict[str, Any] = {}
        for raw_field, raw_value in raw_values.items():
            field = _canonical_field(definition.child_table, raw_field)
            if field not in allowed or field not in table.c:
                raise ParentChildError(
                    status=ResponseStatus.CLARIFICATION_REQUIRED,
                    code="relationship_child_field_invalid",
                    message=f"'{raw_field}' is not an editable field in {definition.child_table}.",
                    details=[{"allowed_fields": sorted(allowed)}],
                )
            values[field] = _coerce_value(table.c[field], raw_value)
        return values

    @staticmethod
    def _validate_business_rules(
        definition: RelationshipDefinition,
        values: dict[str, Any],
        *,
        creating: bool,
    ) -> None:
        if creating:
            missing = [field for field in definition.required_create_fields if values.get(field) in (None, "")]
            if missing:
                raise ParentChildError(
                    status=ResponseStatus.CLARIFICATION_REQUIRED,
                    code="relationship_create_fields_required",
                    message=f"Please provide: {', '.join(field.replace('_', ' ') for field in missing)}.",
                    details=[{"missing_fields": missing, "child_table": definition.child_table}],
                )
        if not values and not creating:
            raise ParentChildError(
                status=ResponseStatus.CLARIFICATION_REQUIRED,
                code="relationship_update_values_required",
                message="Which field should be changed, and what should its new value be?",
            )
        if definition.child_table == "employee_experiences":
            start = values.get("start_date")
            end = values.get("end_date")
            is_current = values.get("is_current")
            if start is not None and end is not None and end < start:
                raise ParentChildError(
                    status=ResponseStatus.CLARIFICATION_REQUIRED,
                    code="experience_date_range_invalid",
                    message="The experience end date cannot be earlier than its start date.",
                )
            if is_current is True and end is not None:
                raise ParentChildError(
                    status=ResponseStatus.CLARIFICATION_REQUIRED,
                    code="current_experience_end_date_conflict",
                    message="A current experience cannot have an end date. Remove the end date or mark it as not current.",
                )
        if definition.child_table == "sales_deals" and "probability" in values:
            probability = int(values["probability"])
            if probability < 0 or probability > 100:
                raise ParentChildError(
                    status=ResponseStatus.CLARIFICATION_REQUIRED,
                    code="deal_probability_out_of_range",
                    message="Deal probability must be between 0 and 100.",
                )

    @staticmethod
    def _filter_expression(table: Any, child_filter: ChildFilter, child_table: str) -> Any:
        field = _canonical_field(child_table, child_filter.field)
        if field == "id":
            column = table.c.id
        elif field in table.c and field not in MANAGED_COLUMNS and not field.endswith("_id"):
            column = table.c[field]
        else:
            raise ParentChildError(
                status=ResponseStatus.CLARIFICATION_REQUIRED,
                code="relationship_filter_field_invalid",
                message=f"'{child_filter.field}' cannot be used to select {child_table} records.",
            )
        operator = child_filter.operator
        if operator == "is_null":
            raw_null = _unwrap_value(child_filter.value)
            if isinstance(raw_null, bool):
                value = raw_null
            else:
                normalized_null = str(raw_null).strip().casefold()
                if normalized_null in {"true", "yes", "1", "null", "missing"}:
                    value = True
                elif normalized_null in {"false", "no", "0", "not null", "present"}:
                    value = False
                else:
                    raise ParentChildError(status=ResponseStatus.CLARIFICATION_REQUIRED, code="relationship_null_filter_invalid", message=f"Say whether {field} should be null or not null.")
        else:
            value = _coerce_value(column, child_filter.value)
        if operator == "eq":
            if isinstance(column.type, (String, Text)) and isinstance(value, str):
                return func.lower(column) == value.casefold()
            return column == value
        if operator == "ne":
            if isinstance(column.type, (String, Text)) and isinstance(value, str):
                return func.lower(column) != value.casefold()
            return column != value
        if operator == "contains":
            if not isinstance(column.type, (String, Text)):
                raise ParentChildError(status=ResponseStatus.CLARIFICATION_REQUIRED, code="relationship_text_filter_required", message=f"contains can only be used with text fields, not {field}.")
            return column.ilike(f"%{value}%")
        if operator == "starts_with":
            return column.ilike(f"{value}%")
        if operator == "ends_with":
            return column.ilike(f"%{value}")
        if operator == "gt":
            return column > value
        if operator == "gte":
            return column >= value
        if operator == "lt":
            return column < value
        if operator == "lte":
            return column <= value
        if operator == "in":
            raw_items = _unwrap_value(child_filter.value)
            if not isinstance(raw_items, list) or not raw_items:
                raise ParentChildError(status=ResponseStatus.CLARIFICATION_REQUIRED, code="relationship_in_values_required", message=f"Provide one or more values for {field}.")
            items = [_coerce_value(column, item) for item in raw_items]
            if isinstance(column.type, (String, Text)):
                return func.lower(column).in_([str(item).casefold() for item in items])
            return column.in_(items)
        if operator == "is_null":
            return column.is_(None) if value else column.is_not(None)
        raise ParentChildError(status=ResponseStatus.CLARIFICATION_REQUIRED, code="relationship_filter_operator_invalid", message=f"The filter operator '{operator}' is not supported.")

    def _matching_rows(
        self,
        db: Session,
        plan: ParentChildIntentPlan,
        definition: RelationshipDefinition,
        resolved_parents: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        table = Base.metadata.tables[definition.child_table]
        conditions: list[Any] = []
        links_by_role = {item.role: item for item in definition.parents}
        for role, parent in resolved_parents.items():
            link = links_by_role[role]
            conditions.append(table.c[link.foreign_key] == parent["id"])
        for child_filter in plan.filters:
            conditions.append(self._filter_expression(table, child_filter, definition.child_table))

        statement = select(table)
        if conditions:
            statement = statement.where(and_(*conditions))
        elif plan.operation in {"update", "delete"}:
            raise ParentChildError(
                status=ResponseStatus.CLARIFICATION_REQUIRED,
                code="relationship_record_selector_required",
                message=f"Which exact {definition.readable_name} should be {plan.operation}d? Provide a parent business code or a child field such as company, permission code, deal code, or mapping details.",
            )

        order_column = table.c.get(definition.ordering_field)
        if order_column is None:
            order_column = table.c.id
        if plan.selection == "oldest":
            statement = statement.order_by(order_column.asc(), table.c.id.asc()).limit(1)
        elif plan.selection == "latest":
            statement = statement.order_by(order_column.desc().nullslast(), table.c.id.desc()).limit(1)
        elif plan.selection == "first":
            statement = statement.order_by(table.c.id.asc()).limit(1)
        else:
            statement = statement.order_by(table.c.id.asc()).limit(MAX_RELATIONSHIP_ROWS + 1)

        rows = [row_to_dict(row) for row in db.execute(statement).mappings().all()]
        if len(rows) > MAX_RELATIONSHIP_ROWS:
            raise ParentChildError(
                status=ResponseStatus.CLARIFICATION_REQUIRED,
                code="relationship_result_too_large",
                message=f"More than {MAX_RELATIONSHIP_ROWS} records match. Please add a parent code or a narrower filter.",
            )
        return rows

    @staticmethod
    def _enrich_parent_codes(
        db: Session,
        definition: RelationshipDefinition,
        rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        enriched = [dict(row) for row in rows]
        for link in definition.parents:
            ids = {row.get(link.foreign_key) for row in enriched if row.get(link.foreign_key) is not None}
            if not ids:
                continue
            table = Base.metadata.tables[link.table]
            parent_rows = db.execute(select(table.c.id, table.c[link.code_column]).where(table.c.id.in_(ids))).mappings().all()
            code_map = {row["id"]: row[link.code_column] for row in parent_rows}
            output_key = f"{link.role}_code" if link.role != "employee" else "employee_code"
            for row in enriched:
                if row.get(link.foreign_key) in code_map:
                    row[output_key] = code_map[row[link.foreign_key]]
        return enriched

    def execute(
        self,
        db: Session,
        *,
        question: str,
        session_id: UUID,
        actor_role: UserRole,
        memory_context: dict[str, Any] | None = None,
    ) -> ParentChildResult:
        try:
            plan, model_metadata = self._generate_plan(question, memory_context=memory_context)
        except LLMOutputValidationError as exc:
            raise ParentChildError(status=ResponseStatus.CLARIFICATION_REQUIRED, code=exc.code, message="I could not interpret the parent-child request safely. Please state the parent business code, child record type, and requested action.") from exc
        except LLMServiceError as exc:
            raise ParentChildError(status=ResponseStatus.LLM_UNAVAILABLE, code=exc.code, message=exc.message) from exc

        if plan.operation == "clarify" or plan.confidence < MIN_PLAN_CONFIDENCE:
            question_text = plan.clarification_question or "Please specify the parent business code, child record type, and action you want."
            return ParentChildResult(
                route=AgentRoute.SYSTEM,
                status=ResponseStatus.CLARIFICATION_REQUIRED,
                answer=question_text,
                data={
                    "relationship_intent": plan.model_dump(mode="json"),
                    "model_metadata": model_metadata,
                    "database_touched": False,
                    "sql_generated": False,
                },
            )

        definition = self._definition(plan)
        resolved_parents = self._resolved_parents(db, plan, definition)

        if plan.operation == "read":
            rows = self._matching_rows(db, plan, definition, resolved_parents)
            rows = self._enrich_parent_codes(db, definition, rows)
            status = ResponseStatus.SUCCESS if rows else ResponseStatus.INFORMATION_NOT_AVAILABLE
            answer = (
                f"Found {len(rows)} {definition.readable_name} record{'s' if len(rows) != 1 else ''}."
                if rows
                else f"No matching {definition.readable_name} records were found."
            )
            return ParentChildResult(
                route=AgentRoute.STRUCTURED_READ,
                status=status,
                answer=answer,
                data={
                    "rows": rows,
                    "row_count": len(rows),
                    "target_table": definition.child_table,
                    "relationship_intent": plan.model_dump(mode="json"),
                    "resolved_parents": {role: {key: value for key, value in parent.items() if key in {"id", "employee_code", "product_code", "vendor_code", "customer_code"}} for role, parent in resolved_parents.items()},
                    "model_metadata": model_metadata,
                    "execution": {"tool": "sqlalchemy_parent_child_read", "executed": True, "write_execution_allowed": False},
                },
            )

        if plan.operation == "create":
            values = self._normalized_values(definition, plan.values)
            self._validate_business_rules(definition, values, creating=True)
            record = dict(values)
            links_by_role = {item.role: item for item in definition.parents}
            for role, parent in resolved_parents.items():
                record[links_by_role[role].foreign_key] = parent["id"]
            try:
                proposal = crud_write_service.propose_bulk_insert(
                    db,
                    session_id=session_id,
                    target_table=definition.child_table,
                    records=[record],
                    actor_role=actor_role,
                    user_prompt=question,
                    generation_metadata={
                        "requested_record_count": 1,
                        "generated_record_count": 1,
                        "source": "semantic_parent_child_plan",
                    },
                )
            except CrudWriteError as exc:
                raise ParentChildError(status=exc.status, code=exc.code, message=exc.message, details=exc.details) from exc
            return ParentChildResult(
                route=AgentRoute.CRUD_WRITE,
                status=ResponseStatus.PENDING_CONFIRMATION,
                answer=f"Prepared one {definition.readable_name} record. Review the preview and confirm to insert it.",
                pending_action_id=proposal.pending_action["pending_action_id"],
                data={
                    "target_table": definition.child_table,
                    "pending_action": proposal.pending_action,
                    "preview": proposal.preview,
                    "rows": proposal.preview.get("records", []),
                    "relationship_intent": plan.model_dump(mode="json"),
                    "resolved_parents": resolved_parents,
                    "model_metadata": model_metadata,
                    "write_execution_allowed": False,
                    "next_step": "Confirm or cancel the exact pending action.",
                },
            )

        rows = self._matching_rows(db, plan, definition, resolved_parents)
        if not rows:
            return ParentChildResult(
                route=AgentRoute.CRUD_WRITE,
                status=ResponseStatus.INFORMATION_NOT_AVAILABLE,
                answer=f"No matching {definition.readable_name} record was found, so no write preview was created.",
                data={
                    "rows": [],
                    "row_count": 0,
                    "target_table": definition.child_table,
                    "relationship_intent": plan.model_dump(mode="json"),
                    "write_execution_allowed": False,
                },
            )
        if len(rows) > 1 and not plan.apply_to_all_matches and plan.selection == "all":
            return ParentChildResult(
                route=AgentRoute.SYSTEM,
                status=ResponseStatus.CLARIFICATION_REQUIRED,
                answer=f"{len(rows)} {definition.readable_name} records match. Which one should I {plan.operation}? Add a company, permission code, deal code, product/vendor pair, or say 'all matching records'.",
                data={
                    "rows": self._enrich_parent_codes(db, definition, rows),
                    "row_count": len(rows),
                    "target_table": definition.child_table,
                    "relationship_intent": plan.model_dump(mode="json"),
                    "database_touched": True,
                    "write_execution_allowed": False,
                },
            )

        changes: dict[str, Any] = {}
        if plan.operation == "update":
            changes = self._normalized_values(definition, plan.values)
            self._validate_business_rules(definition, changes, creating=False)
            table = Base.metadata.tables[definition.child_table]
            for row in rows:
                merged: dict[str, Any] = {}
                for field in definition.mutable_fields:
                    if field in row:
                        merged[field] = _coerce_value(table.c[field], row[field])
                merged.update(changes)
                self._validate_business_rules(definition, merged, creating=False)
        record_ids = [int(row["id"]) for row in rows if row.get("id") is not None]
        try:
            proposal = crud_write_service.propose_structured_write(
                db,
                session_id=session_id,
                target_table=definition.child_table,
                statement_type=plan.operation.upper(),
                record_ids=record_ids,
                changes=changes,
                actor_role=actor_role,
                user_prompt=question,
            )
        except CrudWriteError as exc:
            raise ParentChildError(status=exc.status, code=exc.code, message=exc.message, details=exc.details) from exc
        return ParentChildResult(
            route=AgentRoute.CRUD_WRITE,
            status=ResponseStatus.PENDING_CONFIRMATION,
            answer=f"Prepared a confirmation preview to {plan.operation} {len(record_ids)} {definition.readable_name} record{'s' if len(record_ids) != 1 else ''}.",
            pending_action_id=proposal.pending_action["pending_action_id"],
            data={
                "target_table": definition.child_table,
                "pending_action": proposal.pending_action,
                "preview": proposal.preview,
                "rows": proposal.preview.get("affected_rows", []),
                "relationship_intent": plan.model_dump(mode="json"),
                "resolved_parents": resolved_parents,
                "model_metadata": model_metadata,
                "write_execution_allowed": False,
                "next_step": "Confirm or cancel the exact pending action.",
            },
        )


parent_child_crud_service = ParentChildCrudService()
