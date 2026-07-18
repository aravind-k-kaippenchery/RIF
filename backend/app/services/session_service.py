"""Minimal Phase 3 session and pending-action service.

This is not the full chat memory system yet. It stores enough state for future
confirmation workflows: create a session, attach a pending action, retrieve it,
and expire it safely.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.core.constants import UserRole
from app.models import PendingAction, Session as ConversationSession


DEFAULT_SESSION_TTL_MINUTES = 120
DEFAULT_PENDING_ACTION_TTL_MINUTES = 30


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _expires_in(minutes: int) -> datetime:
    return _utc_now() + timedelta(minutes=minutes)


def create_session(
    db: DbSession,
    *,
    user_role: UserRole = UserRole.NORMAL_USER,
    ttl_minutes: int = DEFAULT_SESSION_TTL_MINUTES,
) -> ConversationSession:
    """Create a short-term conversation session row."""

    session = ConversationSession(
        user_role=user_role.value,
        status="active",
        expires_at=_expires_in(ttl_minutes),
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def get_session(db: DbSession, session_id: UUID) -> Optional[ConversationSession]:
    """Return a session by ID, or None if it does not exist."""

    return db.get(ConversationSession, session_id)


def get_active_session(db: DbSession, session_id: UUID) -> Optional[ConversationSession]:
    """Return a session only if it is active and not expired."""

    session = get_session(db, session_id)
    if session is None or session.status != "active":
        return None
    if session.expires_at and session.expires_at <= _utc_now():
        session.status = "expired"
        db.commit()
        db.refresh(session)
        return None
    return session


def create_pending_action(
    db: DbSession,
    *,
    session_id: UUID,
    action_type: str,
    target_table: Optional[str],
    validated_payload: dict[str, Any],
    preview_data: dict[str, Any],
    generated_sql: Optional[str] = None,
    ttl_minutes: int = DEFAULT_PENDING_ACTION_TTL_MINUTES,
) -> PendingAction:
    """Create a pending action for a future confirmation workflow."""

    action = PendingAction(
        session_id=session_id,
        action_type=action_type,
        target_table=target_table,
        validated_payload=validated_payload,
        preview_data=preview_data,
        generated_sql=generated_sql,
        status="pending",
        expires_at=_expires_in(ttl_minutes),
    )
    db.add(action)
    db.commit()
    db.refresh(action)
    return action


def get_pending_action(db: DbSession, *, session_id: UUID, action_id: UUID) -> Optional[PendingAction]:
    """Return a pending action scoped to its session."""

    return db.scalar(select(PendingAction).where(PendingAction.id == action_id, PendingAction.session_id == session_id))


def list_pending_actions(db: DbSession, *, session_id: UUID, include_expired: bool = False) -> list[PendingAction]:
    """Return pending actions for one session."""

    statement = select(PendingAction).where(PendingAction.session_id == session_id).order_by(PendingAction.created_at.desc())
    actions = list(db.scalars(statement).all())
    if include_expired:
        return actions
    return [action for action in actions if action.status == "pending" and action.expires_at > _utc_now()]


def expire_pending_action(db: DbSession, *, session_id: UUID, action_id: UUID) -> Optional[PendingAction]:
    """Mark one pending action as expired. This is safe and does not execute SQL."""

    action = get_pending_action(db, session_id=session_id, action_id=action_id)
    if action is None:
        return None
    if action.status == "pending":
        action.status = "expired"
        db.commit()
        db.refresh(action)
    return action


def pending_action_to_dict(action: PendingAction) -> dict[str, Any]:
    """Serialize a pending action for API responses."""

    return {
        "pending_action_id": str(action.id),
        "session_id": str(action.session_id),
        "action_type": action.action_type,
        "target_table": action.target_table,
        "validated_payload": action.validated_payload,
        "preview_data": action.preview_data,
        "generated_sql": action.generated_sql,
        "status": action.status,
        "expires_at": action.expires_at.isoformat() if action.expires_at else None,
        "confirmed_at": action.confirmed_at.isoformat() if action.confirmed_at else None,
        "cancelled_at": action.cancelled_at.isoformat() if action.cancelled_at else None,
    }


def session_to_dict(session: ConversationSession) -> dict[str, Any]:
    """Serialize a session for API responses."""

    return {
        "session_id": str(session.id),
        "user_role": session.user_role,
        "status": session.status,
        "expires_at": session.expires_at.isoformat() if session.expires_at else None,
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "updated_at": session.updated_at.isoformat() if session.updated_at else None,
    }
