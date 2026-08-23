"""Phase 13 short-term session memory backed by existing operational tables.

No cloud memory service or new database table is required.  Query history is stored in
``query_logs`` and write/audit events are already stored in ``action_logs``.  This service
creates one bounded, frontend-safe conversation history from those persisted records.
"""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session as DbSession

from app.models.operations import ActionLog, QueryLog, Session as ConversationSession

MAX_MEMORY_EVENTS = 6


_CONTEXT_REFERENCE_PATTERNS = (
    re.compile(r"\b(?:it|them|those|that|these|this|ones|they|previous|last|latest|earlier|above)\b", re.I),
    re.compile(r"\b(?:same|again|continue|what about|as before|like before)\b", re.I),
    # Existential and implicit-list follow-ups do not contain a pronoun, but still
    # depend on the immediately preceding successful table result.
    re.compile(r"\bhow\s+many\s+(?:are|were)\s+there\b", re.I),
    re.compile(r"\b(?:who\s+are\s+)?(?:the\s+)?first\s+(?:\d{1,3}|one|two|three|four|five|six|seven|eight|nine|ten)\b", re.I),
)


def memory_context_for_question(question: str, context: dict[str, Any]) -> dict[str, Any]:
    """Attach prior-query context only when the current turn explicitly refers to it.

    Standalone questions must be understood from their own words. This prevents an old
    filter such as ``city = Bangalore`` from leaking into a new question such as
    ``Do we have an employee table?``. Deterministic action references are still handled
    by ``conversation_context_service`` before the model is called.
    """

    normalized = " ".join(str(question or "").strip().split())
    references_context = any(pattern.search(normalized) for pattern in _CONTEXT_REFERENCE_PATTERNS)
    if references_context:
        return context
    return {
        "available": False,
        "event_count": 0,
        "events": [],
        "policy": (
            "standalone current-turn request; prior SQL and filters were deliberately "
            "excluded because the user did not explicitly reference earlier context"
        ),
    }


class SessionMemoryError(RuntimeError):
    """Safe memory-service failure that callers can convert to the shared API envelope."""

    def __init__(self, *, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _serialize_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _serialize_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize_value(item) for item in value]
    return str(value)


def _query_event(query: QueryLog) -> dict[str, Any]:
    references = _serialize_value(query.source_references or {})
    answer = references.get("assistant_answer") if isinstance(references, dict) else None
    return {
        "event_type": "query",
        "event_id": query.id,
        "request_id": query.request_id,
        "timestamp": query.created_at.isoformat() if query.created_at else None,
        "user_prompt": query.user_prompt,
        "route": query.detected_route,
        "status": query.status,
        "generated_sql": query.generated_sql,
        "answer": answer,
        "source_references": references,
        "error_code": query.error_code,
    }


def _action_event(action: ActionLog) -> dict[str, Any]:
    return {
        "event_type": "action",
        "event_id": action.id,
        "request_id": action.request_id,
        "timestamp": action.created_at.isoformat() if action.created_at else None,
        "action_type": action.action_type,
        "target_table": action.target_table,
        "status": action.status,
        "confirmation_status": action.confirmation_status,
        "pending_action_id": str(action.pending_action_id) if action.pending_action_id else None,
        "affected_record_ids": _serialize_value(action.affected_record_ids or {}),
        "generated_sql": action.generated_sql,
        "error_message": action.error_message,
    }


def get_session_history(db: DbSession, *, session_id: UUID, limit: int = 20) -> dict[str, Any]:
    """Return bounded chronological query/action history for one active or closed session."""

    session = db.get(ConversationSession, session_id)
    if session is None:
        raise SessionMemoryError(code="session_not_found", message=f"No session exists with id '{session_id}'.")

    normalized_limit = max(1, min(int(limit), 100))
    queries = list(
        db.scalars(
            select(QueryLog)
            .where(QueryLog.session_id == session_id)
            .order_by(QueryLog.created_at.desc())
            .limit(normalized_limit)
        ).all()
    )
    actions = list(
        db.scalars(
            select(ActionLog)
            .where(ActionLog.session_id == session_id)
            .order_by(ActionLog.created_at.desc())
            .limit(normalized_limit)
        ).all()
    )
    events = [_query_event(item) for item in queries] + [_action_event(item) for item in actions]
    events.sort(key=lambda item: item.get("timestamp") or "", reverse=True)
    events = events[:normalized_limit]
    events.reverse()
    return {
        "session": {
            "session_id": str(session.id),
            "user_role": session.user_role,
            "status": session.status,
            "expires_at": session.expires_at.isoformat() if session.expires_at else None,
            "created_at": session.created_at.isoformat() if session.created_at else None,
            "updated_at": session.updated_at.isoformat() if session.updated_at else None,
        },
        "event_count": len(events),
        "query_event_count": len(queries),
        "action_event_count": len(actions),
        "events": events,
        "memory_scope": "short_term_session_history",
    }


def build_memory_context(db: DbSession, *, session_id: UUID, limit: int = MAX_MEMORY_EVENTS) -> dict[str, Any]:
    """Build a compact prior-query context for the local SQL model.

    Action/batch references are resolved by ``conversation_context_service`` before the
    model is called. They are intentionally not copied into the Ollama prompt. Keeping
    this prompt query-only prevents a growing session from causing oversized/slow local
    generation requests while preserving deterministic conversation memory.
    """

    history = get_session_history(db, session_id=session_id, limit=limit)
    events: list[dict[str, Any]] = []
    for event in history["events"]:
        if event.get("event_type") != "query":
            continue
        events.append(
            {
                "prior_question": event.get("user_prompt"),
                "route": event.get("route"),
                "status": event.get("status"),
                "generated_sql": event.get("generated_sql"),
                "conversation_reference": (
                    event.get("source_references", {}).get("conversation_reference")
                    if isinstance(event.get("source_references"), dict)
                    else None
                ),
            }
        )
    return {
        "available": bool(events),
        "event_count": len(events),
        "events": events,
        "policy": (
            "compact query-only session context for Ollama; action references are "
            "resolved deterministically outside the model"
        ),
    }


def write_agent_memory_event(
    db: DbSession,
    *,
    request_id: str | None,
    session_id: UUID,
    question: str,
    route: str,
    status: str,
    answer: str,
    generated_sql: str | None,
    sources: list[dict[str, Any]],
    latency_ms: int | None = None,
    error_code: str | None = None,
    conversation_reference: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist one completed agent result in query_logs without duplicating request IDs.

    Structured reads already create a ``query_logs`` row in Phase 6.  When this function
    sees that existing row, it enriches its source payload instead of inserting a duplicate.
    """

    if not request_id:
        return {"stored": False, "reason": "request_id_unavailable"}

    source_payload = {
        "source_type": "agent_memory",
        "assistant_answer": answer,
        "sources": sources[:10],
        "conversation_reference": _serialize_value(conversation_reference) if conversation_reference else None,
    }
    try:
        existing = db.scalar(select(QueryLog).where(QueryLog.request_id == request_id))
        if existing is not None:
            # Assign a new mapping so SQLAlchemy reliably detects the JSONB change.
            existing_references = dict(existing.source_references) if isinstance(existing.source_references, dict) else {}
            existing_references.update(source_payload)
            existing.source_references = existing_references
            if existing.session_id is None:
                existing.session_id = session_id
            db.commit()
            return {"stored": True, "storage": "query_logs", "mode": "enriched_existing"}

        db.add(
            QueryLog(
                request_id=request_id,
                session_id=session_id,
                user_prompt=question,
                detected_route=route,
                generated_sql=generated_sql,
                status=status,
                latency_ms=max(0, int(latency_ms or 0)),
                source_references=source_payload,
                error_code=error_code,
                created_at=datetime.now(timezone.utc),
            )
        )
        db.commit()
        return {"stored": True, "storage": "query_logs", "mode": "created"}
    except SQLAlchemyError as exc:
        db.rollback()
        return {"stored": False, "reason": "session_memory_log_failed"}


def close_session(db: DbSession, *, session_id: UUID) -> ConversationSession:
    """Close a session without deleting logs or pending-action audit history."""

    session = db.get(ConversationSession, session_id)
    if session is None:
        raise SessionMemoryError(code="session_not_found", message=f"No session exists with id '{session_id}'.")
    if session.status != "closed":
        session.status = "closed"
        db.commit()
        db.refresh(session)
    return session
