"""Semantic natural-language understanding backed by the local Ollama model.

Unlike the legacy keyword router, this service evaluates the complete current sentence
and returns a typed intent plan.  It never asks the model for SQL.  Tables, columns,
filters, and write values are grounded against the controlled SQLAlchemy schema after
generation.  Missing or ambiguous information becomes a clarification response rather
than a guessed query.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any

from pydantic import BaseModel

from app.services.llm_service import LLMOutputValidationError, LLMServiceError, llm_service
from app.services.schema_registry import BUSINESS_TABLES, OPERATIONAL_TABLES, get_allowed_columns, get_allowed_tables, get_relationships
from app.schemas.semantic_plan import ResolvedSemanticPlan, SemanticFilter, SemanticIntentPlan, SemanticSort


MIN_CONFIDENCE = 0.76

_TABLE_ALIASES: dict[str, tuple[str, ...]] = {
    "employees": ("employee", "employees", "worker", "workers", "staff", "staff member", "people", "person"),
    "employee_permissions": ("employee permission", "employee permissions", "permission", "permissions"),
    "vendors": ("vendor", "vendors", "supplier", "suppliers"),
    "customers": ("customer", "customers", "client", "clients"),
    "products": ("product", "products", "item", "items"),
    "product_vendor_mappings": ("product vendor mapping", "product vendor mappings", "vendor mapping", "vendor mappings"),
    "sales_deals": ("sales deal", "sales deals", "deal", "deals", "opportunity", "opportunities"),
}

_COLUMN_ALIASES: dict[str, dict[str, tuple[str, ...]]] = {
    "employees": {
        "employee_code": ("employee code", "worker code", "code"),
        "first_name": ("first name",),
        "last_name": ("last name", "surname"),
        "email": ("email", "email address"),
        "phone": ("phone", "phone number", "mobile"),
        "department": ("department", "team"),
        "city": ("city", "location"),
        "company_name": ("company", "company name", "employer"),
        "salary": ("salary", "pay"),
        "employment_status": ("employment status", "status"),
    },
    "vendors": {
        "vendor_code": ("vendor code", "supplier code", "code"),
        "vendor_name": ("vendor name", "supplier name", "company", "company name", "name"),
        "contact_email": ("contact email", "email", "email address"),
        "phone": ("phone", "phone number", "mobile"),
        "city": ("city", "location"),
        "country": ("country",),
        "category": ("category", "type"),
        "status": ("status",),
    },
    "customers": {
        "customer_code": ("customer code", "client code", "code"),
        "customer_name": ("customer name", "client name", "company", "company name", "name"),
        "contact_email": ("contact email", "email", "email address"),
        "phone": ("phone", "phone number", "mobile"),
        "city": ("city", "location"),
        "country": ("country",),
        "industry": ("industry", "sector"),
        "status": ("status",),
    },
    "products": {
        "product_code": ("product code", "item code", "code", "sku"),
        "product_name": ("product name", "item name", "name"),
        "category": ("category", "type"),
        "description": ("description", "details"),
        "list_price": ("list price", "price", "cost"),
        "is_active": ("active", "is active", "status"),
    },
    "sales_deals": {
        "deal_code": ("deal code", "code"),
        "title": ("title", "deal title", "name"),
        "customer_id": ("customer", "customer id", "client"),
        "product_id": ("product", "product id"),
        "owner_employee_id": ("owner", "employee owner", "salesperson"),
        "amount": ("amount", "value", "deal value"),
        "stage": ("stage",),
        "probability": ("probability", "chance"),
        "expected_close_date": ("expected close date", "close date"),
        "status": ("status",),
    },
    "employee_permissions": {
        "employee_id": ("employee", "employee id", "worker"),
        "permission_code": ("permission code", "code", "permission"),
        "description": ("description", "details"),
        "is_active": ("active", "is active", "status"),
    },
    "product_vendor_mappings": {
        "product_id": ("product", "product id"),
        "vendor_id": ("vendor", "vendor id", "supplier"),
        "vendor_sku": ("vendor sku", "supplier sku", "sku"),
        "quoted_price": ("quoted price", "price", "quote"),
        "is_preferred": ("preferred", "is preferred"),
    },
}

_REFERENCE_PATTERN = re.compile(
    r"\b(it|them|those|that|these|previous|last|same|again|just created|new ones?|that one|the batch|their)\b",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class SemanticUnderstandingResult:
    resolved: ResolvedSemanticPlan
    model_metadata: dict[str, Any]
    context_used: bool


def _normalize(value: str | None) -> str:
    return re.sub(r"[\s_-]+", " ", str(value or "").strip().casefold())


def _compact_schema() -> dict[str, Any]:
    tables: list[dict[str, Any]] = []
    for table_name in get_allowed_tables():
        if table_name not in BUSINESS_TABLES and table_name not in OPERATIONAL_TABLES:
            continue
        tables.append(
            {
                "table_name": table_name,
                "category": "business" if table_name in BUSINESS_TABLES else "operational",
                "columns": get_allowed_columns(table_name),
                "aliases": list(_TABLE_ALIASES.get(table_name, (table_name.replace("_", " "),))),
            }
        )
    return {
        "tables": tables,
        "relationships": get_relationships(),
        "normal_user_business_tables": list(BUSINESS_TABLES),
    }


def _compact_context(question: str, memory_context: dict[str, Any] | None) -> tuple[dict[str, Any], bool]:
    if not _REFERENCE_PATTERN.search(question):
        return {"available": False, "policy": "current turn is standalone; no prior filters or SQL may be inherited"}, False
    context = memory_context if isinstance(memory_context, dict) else {}
    events = context.get("events") if isinstance(context.get("events"), list) else []
    compact_events = []
    for event in events[-3:]:
        if not isinstance(event, dict):
            continue
        compact_events.append(
            {
                "prior_question": event.get("prior_question") or event.get("user_prompt"),
                "route": event.get("route"),
                "status": event.get("status"),
            }
        )
    return {
        "available": bool(compact_events),
        "events": compact_events,
        "policy": "use only because the current turn explicitly refers to previous context; never inherit an unstated filter",
    }, bool(compact_events)


def _resolve_table(value: str | None) -> str | None:
    normalized = _normalize(value)
    if not normalized:
        return None
    allowed = get_allowed_tables()
    direct = normalized.replace(" ", "_")
    if direct in allowed:
        return direct
    for table_name, aliases in _TABLE_ALIASES.items():
        if table_name not in allowed:
            continue
        if normalized in {_normalize(alias) for alias in aliases}:
            return table_name
    return None


def _column_candidates(table_name: str, value: str) -> list[str]:
    normalized = _normalize(value)
    columns = get_allowed_columns(table_name)
    matches: list[str] = []
    direct = normalized.replace(" ", "_")
    if direct in columns:
        matches.append(direct)
    for column_name, aliases in _COLUMN_ALIASES.get(table_name, {}).items():
        if column_name not in columns:
            continue
        if normalized == _normalize(column_name) or normalized in {_normalize(alias) for alias in aliases}:
            if column_name not in matches:
                matches.append(column_name)
    return matches


def _resolve_single_column(table_name: str, value: str, *, purpose: str) -> tuple[str | None, str | None]:
    candidates = _column_candidates(table_name, value)
    if len(candidates) == 1:
        return candidates[0], None
    if not candidates:
        return None, f"I could not match `{value}` to a column in the `{table_name}` table. Which column do you mean?"
    return None, f"`{value}` could mean {', '.join(f'`{item}`' for item in candidates)} in `{table_name}`. Which column do you mean for {purpose}?"


def _clarification(plan: SemanticIntentPlan, question: str, missing: list[str], ambiguities: list[str]) -> str:
    if plan.clarification_question:
        return plan.clarification_question
    if "target_table" in missing:
        return "Which database table do you want to use?"
    if "requested_action" in missing:
        return "What would you like to do: view records, inspect the schema, add data, update data, or delete data?"
    if "record_filter" in missing:
        return "Which exact record should be changed? Please provide its ID, code, or another unique filter."
    if "update_values" in missing:
        return "Which field should be updated, and what should its new value be?"
    if "insert_values" in missing:
        return "Which values should be added for the new record?"
    if "table_definition" in missing:
        return "What should the new table be called, and which columns and data types should it contain?"
    if ambiguities:
        return f"I found more than one possible meaning: {'; '.join(ambiguities)}. Please clarify which one you intend."
    return "I am not confident enough to act on that request. Please clarify the table, action, and any filters or values."


def resolve_semantic_plan(plan: SemanticIntentPlan, *, question: str) -> ResolvedSemanticPlan:
    missing = list(dict.fromkeys(plan.missing_information))
    ambiguities = list(dict.fromkeys(plan.ambiguities))

    if plan.requires_clarification or plan.confidence < MIN_CONFIDENCE or plan.intent in {"clarification", "unsupported"}:
        if plan.confidence < MIN_CONFIDENCE and "low_confidence" not in missing:
            missing.append("low_confidence")
        return ResolvedSemanticPlan(
            plan=plan,
            clarification_required=True,
            clarification_question=_clarification(plan, question, missing, ambiguities),
            missing_information=missing,
            ambiguities=ambiguities,
            schema_grounded=False,
        )

    table_required = plan.intent.startswith("data.") or plan.intent.startswith("write.") or plan.intent == "synthetic.generate"
    table_required = table_required or plan.intent in {"schema.table_exists", "schema.list_columns", "schema.column_exists", "schema.relationships"}

    canonical_table = _resolve_table(plan.target_table or plan.target_entity or plan.requested_table_text)
    if table_required and canonical_table is None:
        # For a table-existence check, an unknown exact name is valid and should return No.
        if plan.intent == "schema.table_exists" and (plan.requested_table_text or plan.target_table or plan.target_entity):
            return ResolvedSemanticPlan(plan=plan, canonical_table=None, schema_grounded=True)
        missing.append("target_table")
        return ResolvedSemanticPlan(
            plan=plan,
            clarification_required=True,
            clarification_question=_clarification(plan, question, missing, ambiguities),
            missing_information=missing,
            ambiguities=ambiguities,
            schema_grounded=False,
        )

    if canonical_table in OPERATIONAL_TABLES and plan.intent.startswith(("data.", "write.", "synthetic.")):
        return ResolvedSemanticPlan(
            plan=plan,
            canonical_table=canonical_table,
            clarification_required=True,
            clarification_question=f"The `{canonical_table}` table is operational and cannot be manipulated through ordinary natural-language business prompts.",
            missing_information=["admin_or_supported_business_table"],
            schema_grounded=True,
        )

    canonical_columns: list[str] = []
    canonical_filters: list[SemanticFilter] = []
    canonical_sort: list[SemanticSort] = []
    canonical_values: dict[str, Any] = {}
    canonical_aggregate_field: str | None = None

    if canonical_table:
        for requested in plan.requested_columns:
            resolved, error = _resolve_single_column(canonical_table, requested, purpose="the requested output")
            if error:
                ambiguities.append(error)
            elif resolved and resolved not in canonical_columns:
                canonical_columns.append(resolved)

        for item in plan.filters:
            resolved, error = _resolve_single_column(canonical_table, item.field, purpose="the filter")
            if error:
                ambiguities.append(error)
            elif resolved:
                canonical_filters.append(item.model_copy(update={"field": resolved}))

        for item in plan.sort:
            resolved, error = _resolve_single_column(canonical_table, item.field, purpose="sorting")
            if error:
                ambiguities.append(error)
            elif resolved:
                canonical_sort.append(item.model_copy(update={"field": resolved}))

        for key, value in plan.values.items():
            resolved, error = _resolve_single_column(canonical_table, key, purpose="the write value")
            if error:
                ambiguities.append(error)
            elif resolved:
                canonical_values[resolved] = value

        if plan.aggregate_field:
            resolved, error = _resolve_single_column(canonical_table, plan.aggregate_field, purpose="the aggregate")
            if error:
                ambiguities.append(error)
            else:
                canonical_aggregate_field = resolved

    if plan.intent == "write.insert" and not canonical_values:
        missing.append("insert_values")
    if plan.intent == "write.update":
        if not canonical_filters:
            missing.append("record_filter")
        if not canonical_values:
            missing.append("update_values")
    if plan.intent == "write.delete" and not canonical_filters:
        missing.append("record_filter")
    if plan.intent == "synthetic.generate" and plan.count is None:
        missing.append("record_count")
    if plan.intent == "schema.create_table" and not plan.values:
        missing.append("table_definition")

    if ambiguities or missing:
        return ResolvedSemanticPlan(
            plan=plan,
            canonical_table=canonical_table,
            canonical_columns=canonical_columns,
            canonical_filters=canonical_filters,
            canonical_sort=canonical_sort,
            canonical_values=canonical_values,
            canonical_aggregate_field=canonical_aggregate_field,
            clarification_required=True,
            clarification_question=_clarification(plan, question, missing, ambiguities),
            missing_information=list(dict.fromkeys(missing)),
            ambiguities=list(dict.fromkeys(ambiguities)),
            schema_grounded=True,
        )

    return ResolvedSemanticPlan(
        plan=plan,
        canonical_table=canonical_table,
        canonical_columns=canonical_columns,
        canonical_filters=canonical_filters,
        canonical_sort=canonical_sort,
        canonical_values=canonical_values,
        canonical_aggregate_field=canonical_aggregate_field,
        clarification_required=False,
        missing_information=[],
        ambiguities=[],
        schema_grounded=True,
    )


class SemanticUnderstandingService:
    """Ask Ollama for meaning, then let Python ground and validate the result."""

    def understand(self, *, question: str, memory_context: dict[str, Any] | None = None) -> SemanticUnderstandingResult:
        schema = _compact_schema()
        context, context_used = _compact_context(question, memory_context)
        system_prompt = (
            "You are the semantic understanding layer for a database assistant. Analyze the complete current sentence, not isolated keywords. "
            "Never guess a missing table, column, record, action, filter, value, relationship, or reference. Never generate SQL. "
            "Use only actual table and column names from the supplied schema. Distinguish schema questions from row-data questions. "
            "Examples: 'do we have a vendor table' is schema.table_exists; 'show vendors' is data.list; 'vendors table' is ambiguous and requires clarification; "
            "'what about vendors table' is ambiguous unless the conversation makes one interpretation explicit. "
            "Use prior context only when the current sentence explicitly references it. If confidence is below 0.76, requires_clarification must be true."
        )
        user_prompt = json.dumps(
            {
                "current_user_message": question,
                "approved_schema": schema,
                "allowed_intents": list(SemanticIntentPlan.model_fields["intent"].annotation.__args__),
                "conversation_context": context,
                "rules": [
                    "Return a semantic plan only; do not return SQL.",
                    "target_table must be an actual supplied table name when known.",
                    "Filter and value fields should use actual supplied column names when known.",
                    "A bare table phrase is ambiguous between records and schema.",
                    "A new standalone request must not inherit an old filter.",
                    "For insert/update/delete, identify all missing values and ask rather than inventing them.",
                    "For a new table request without a complete definition, use schema.create_table and require clarification.",
                ],
            },
            ensure_ascii=False,
            default=str,
        )

        result, metadata = llm_service.generate_json(
            output_model=SemanticIntentPlan,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.0,
            seed=101,
        )
        resolved = resolve_semantic_plan(result, question=question)
        return SemanticUnderstandingResult(
            resolved=resolved,
            model_metadata=metadata.model_dump(),
            context_used=context_used,
        )


semantic_understanding_service = SemanticUnderstandingService()
