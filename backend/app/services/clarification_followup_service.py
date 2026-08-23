"""Resolve context-dependent replies to the assistant's previous turn.

Example:
    User: employees
    Assistant: Do you want rows or columns?
    User: read

The third turn should become ``show rows from employees`` instead of another
clarification.  The helper also completes an explicit synthetic-data request when
the missing target is supplied in a later turn, or when a pronoun such as ``it``
refers to one live table from the immediately preceding successful result.

It never executes a write.  It only turns the user's bounded multi-turn request into
an explicit current-turn prompt; the normal schema-aware Faker, duplicate-check,
preview, admin-confirmation, audit, and transaction boundaries still apply.
"""

from __future__ import annotations

import re
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.core.constants import ResponseStatus
from app.models.operations import QueryLog
from app.services.request_clarification_service import analyze_request_clarity
from app.services.synthetic_data_service import supported_synthetic_tables
from app.services.table_record_question_service import detect_table_record_question

_ROW_REPLIES = {
    "read",
    "rows",
    "row",
    "records",
    "record",
    "data",
    "view rows",
    "show rows",
    "show records",
    "view records",
    "yes read",
    "yes rows",
    "yes records",
}

_SCHEMA_REPLIES = {
    "columns",
    "column",
    "schema",
    "structure",
    "fields",
    "field",
    "inspect columns",
    "show columns",
    "view columns",
    "yes columns",
    "yes schema",
}

_SYNTHETIC_SIGNAL_PATTERN = re.compile(
    r"\b(?:synthetic|fake|faker|random|randomly|randomized|generate|generated|seed|populate)\b",
    re.I,
)
_SYNTHETIC_TARGET_REFERENCE_PATTERN = re.compile(
    r"\b(?:(?:in|into|to|for|inside)\s+)(?:the\s+)?"
    r"(?:it|this\s+table|that\s+table|same\s+table|previous\s+table|last\s+table)\b",
    re.I,
)
_SHORT_TABLE_REPLY_PATTERN = re.compile(r"^[a-z][a-z0-9_ -]{0,79}(?:\s+table)?[?.!]*$", re.I)


def _normalize(value: str) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _conversation_reference(query: QueryLog) -> dict:
    references = query.source_references if isinstance(query.source_references, dict) else {}
    value = references.get("conversation_reference")
    return dict(value) if isinstance(value, dict) else {}


def _supported_live_table_names() -> set[str]:
    """Return only current reflected Faker targets; operational tables stay excluded."""

    try:
        return {
            str(item.get("table_name") or "").strip().lower()
            for item in supported_synthetic_tables()
            if isinstance(item, dict) and str(item.get("table_name") or "").strip()
        }
    except Exception:
        # Reflection/database failures must not cause a contextual write target guess.
        return set()


def _explicit_live_table_reply(question: str, live_tables: set[str]) -> str | None:
    """Resolve a short reply against exact live names/readable names only."""

    candidate = _normalize(question).strip(" .?!")
    if candidate.endswith(" table"):
        candidate = candidate[:-6].strip()
    matches = {
        table_name
        for table_name in live_tables
        if candidate in {table_name.casefold(), table_name.replace("_", " ").casefold()}
    }
    return next(iter(matches)) if len(matches) == 1 else None


def _with_explicit_synthetic_target(question: str, table_name: str) -> str:
    """Replace one table pronoun, or append the selected live table deterministically."""

    readable = table_name.replace("_", " ")
    explicit_target = f"into {readable} table"
    if _SYNTHETIC_TARGET_REFERENCE_PATTERN.search(question):
        return _SYNTHETIC_TARGET_REFERENCE_PATTERN.sub(explicit_target, question, count=1)
    return f"{question.rstrip(' .?!')} {explicit_target}"


def rewrite_short_clarification_reply(db: DbSession, *, session_id: UUID, question: str) -> str | None:
    """Return an explicit prompt when the latest turn provides safe missing context."""

    normalized = _normalize(question)
    is_synthetic_reference = bool(
        _SYNTHETIC_SIGNAL_PATTERN.search(question)
        and _SYNTHETIC_TARGET_REFERENCE_PATTERN.search(question)
    )
    table_reply_core = normalized.strip(" .?!")
    if table_reply_core.endswith(" table"):
        table_reply_core = table_reply_core[:-6].strip()
    table_reply_tokens = table_reply_core.split()
    is_short_table_reply = bool(
        _SHORT_TABLE_REPLY_PATTERN.fullmatch(normalized)
        and 1 <= len(table_reply_tokens) <= 3
        and not set(table_reply_tokens)
        & {"can", "could", "show", "display", "view", "see", "it", "them", "those", "this", "that"}
    )
    if (
        normalized not in _ROW_REPLIES
        and normalized not in _SCHEMA_REPLIES
        and not is_synthetic_reference
        and not is_short_table_reply
    ):
        return None

    latest_query = db.scalar(
        select(QueryLog)
        .where(QueryLog.session_id == session_id)
        .order_by(QueryLog.created_at.desc())
        .limit(1)
    )
    if latest_query is None:
        return None

    live_tables = _supported_live_table_names() if (is_synthetic_reference or is_short_table_reply) else set()

    # Example:
    #   User: Do we have an orders table?
    #   Assistant: Yes.
    #   User: Insert 5 synthetic records in it.
    # The target comes only from the persisted result reference and is revalidated
    # against the current reflected Faker targets before the prompt is rewritten.
    if is_synthetic_reference and latest_query.status == ResponseStatus.SUCCESS.value:
        referenced_table = str(_conversation_reference(latest_query).get("target_table") or "").strip().lower()
        if referenced_table and referenced_table in live_tables:
            return _with_explicit_synthetic_target(question, referenced_table)

    # Example:
    #   User: Generate 5 synthetic records.
    #   Assistant: Which existing business table?
    #   User: orders
    # Reuse the operation/count/constraints from the failed turn and accept the reply
    # only when it exactly identifies one current reflected Faker target.
    if latest_query.status == ResponseStatus.CLARIFICATION_REQUIRED.value and is_short_table_reply:
        reference = _conversation_reference(latest_query)
        clarification_code = str(reference.get("clarification_code") or "")
        previous_prompt = str(latest_query.user_prompt or "").strip()
        if not clarification_code:
            try:
                decision = analyze_request_clarity(previous_prompt)
                clarification_code = str(decision.code or "") if decision.needs_clarification else ""
            except Exception:
                clarification_code = ""
        if clarification_code == "synthetic_target_table_required":
            selected_table = _explicit_live_table_reply(question, live_tables)
            if selected_table:
                return _with_explicit_synthetic_target(previous_prompt, selected_table)

    if normalized not in _ROW_REPLIES and normalized not in _SCHEMA_REPLIES:
        return None
    if latest_query.status != ResponseStatus.CLARIFICATION_REQUIRED.value:
        return None

    previous_prompt = str(latest_query.user_prompt or "").strip()
    table_request = detect_table_record_question(previous_prompt)
    if not table_request.handled or not table_request.needs_clarification or not table_request.canonical_table:
        return None

    table_name = table_request.canonical_table
    readable_table = table_name.replace("_", " ")
    if normalized in _SCHEMA_REPLIES:
        return f"what columns exist in {readable_table}"
    return f"show rows from {readable_table}"
