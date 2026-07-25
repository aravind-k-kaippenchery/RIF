"""Resolve short replies to the assistant's previous clarification.

Example:
    User: employees
    Assistant: Do you want rows or columns?
    User: read

The third turn should become ``show rows from employees`` instead of another
clarification.  This helper is intentionally limited to read-only table/schema
choices and never rewrites write requests.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.core.constants import ResponseStatus
from app.models.operations import QueryLog
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


def _normalize(value: str) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def rewrite_short_clarification_reply(db: DbSession, *, session_id: UUID, question: str) -> str | None:
    """Return an explicit read-only question when the latest turn answers a clarification."""

    normalized = _normalize(question)
    if normalized not in _ROW_REPLIES and normalized not in _SCHEMA_REPLIES:
        return None

    latest_query = db.scalar(
        select(QueryLog)
        .where(QueryLog.session_id == session_id)
        .order_by(QueryLog.created_at.desc())
        .limit(1)
    )
    if latest_query is None:
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
