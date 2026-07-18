"""Phase 3 session and pending-action verification endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, Request, Query
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.status import HTTP_404_NOT_FOUND, HTTP_503_SERVICE_UNAVAILABLE

from app.core.constants import ResponseStatus, UserRole
from app.core.exceptions import AppError
from app.core.response import ResponseBuilder
from app.core.security import get_current_role
from app.db.health import get_database_health
from app.db.session import get_db_session
from app.schemas.phase3 import PendingActionCreateRequest, SessionCreateRequest
from app.services.session_service import (
    create_pending_action,
    create_session,
    expire_pending_action,
    get_session,
    list_pending_actions,
    pending_action_to_dict,
    session_to_dict,
)
from app.services.session_memory_service import SessionMemoryError, close_session, get_session_history

router = APIRouter(prefix="/api/sessions", tags=["Session service"])


def _database_unavailable_error() -> AppError:
    health = get_database_health()
    return AppError(
        status=ResponseStatus.DATABASE_UNAVAILABLE,
        code="database_unavailable",
        message=f"PostgreSQL is unavailable. {health.message}",
        http_status_code=HTTP_503_SERVICE_UNAVAILABLE,
    )


def _session_not_found(session_id: UUID) -> AppError:
    return AppError(
        status=ResponseStatus.INFORMATION_NOT_AVAILABLE,
        code="session_not_found",
        message=f"No session exists with id '{session_id}'.",
        http_status_code=HTTP_404_NOT_FOUND,
    )


@router.post("", summary="Create a short-term session row")
def create_session_endpoint(
    payload: SessionCreateRequest,
    request: Request,
    db: Session = Depends(get_db_session),
    role: UserRole = Depends(get_current_role),
):
    try:
        session = create_session(db, user_role=role, ttl_minutes=payload.ttl_minutes)
    except SQLAlchemyError as exc:
        raise _database_unavailable_error() from exc
    return ResponseBuilder.success(
        request,
        answer="Short-term session created. This prepares Phase 7 confirmation workflows.",
        data=session_to_dict(session),
    )


@router.get("/{session_id}", summary="Read one short-term session")
def get_session_endpoint(session_id: UUID, request: Request, db: Session = Depends(get_db_session)):
    try:
        session = get_session(db, session_id)
    except SQLAlchemyError as exc:
        raise _database_unavailable_error() from exc
    if session is None:
        raise _session_not_found(session_id)
    return ResponseBuilder.success(request, answer="Session retrieved.", data=session_to_dict(session))


@router.post("/{session_id}/pending-actions", summary="Create a safe pending action preview")
def create_pending_action_endpoint(
    session_id: UUID,
    payload: PendingActionCreateRequest,
    request: Request,
    db: Session = Depends(get_db_session),
):
    try:
        session = get_session(db, session_id)
        if session is None:
            raise _session_not_found(session_id)
        action = create_pending_action(
            db,
            session_id=session_id,
            action_type=payload.action_type,
            target_table=payload.target_table,
            validated_payload=payload.validated_payload,
            preview_data=payload.preview_data,
            generated_sql=payload.generated_sql,
            ttl_minutes=payload.ttl_minutes,
        )
    except AppError:
        raise
    except SQLAlchemyError as exc:
        raise _database_unavailable_error() from exc
    return ResponseBuilder.success(
        request,
        answer="Pending action preview stored. No database write action was executed.",
        data=pending_action_to_dict(action),
        status=ResponseStatus.PENDING_CONFIRMATION,
        pending_action_id=str(action.id),
    )


@router.get("/{session_id}/pending-actions", summary="List pending actions for one session")
def list_pending_actions_endpoint(session_id: UUID, request: Request, db: Session = Depends(get_db_session)):
    try:
        session = get_session(db, session_id)
        if session is None:
            raise _session_not_found(session_id)
        actions = list_pending_actions(db, session_id=session_id)
    except AppError:
        raise
    except SQLAlchemyError as exc:
        raise _database_unavailable_error() from exc
    return ResponseBuilder.success(
        request,
        answer="Pending actions for this session retrieved.",
        data={"session_id": str(session_id), "pending_action_count": len(actions), "pending_actions": [pending_action_to_dict(action) for action in actions]},
    )


@router.get("/{session_id}/pending-actions/{action_id}", summary="Read one pending action")
def get_pending_action_endpoint(session_id: UUID, action_id: UUID, request: Request, db: Session = Depends(get_db_session)):
    from app.services.session_service import get_pending_action

    try:
        action = get_pending_action(db, session_id=session_id, action_id=action_id)
    except SQLAlchemyError as exc:
        raise _database_unavailable_error() from exc
    if action is None:
        raise AppError(
            status=ResponseStatus.INFORMATION_NOT_AVAILABLE,
            code="pending_action_not_found",
            message=f"No pending action '{action_id}' exists for session '{session_id}'.",
            http_status_code=HTTP_404_NOT_FOUND,
        )
    return ResponseBuilder.success(request, answer="Pending action retrieved.", data=pending_action_to_dict(action))


@router.post("/{session_id}/pending-actions/{action_id}/expire", summary="Expire one pending action")
def expire_pending_action_endpoint(session_id: UUID, action_id: UUID, request: Request, db: Session = Depends(get_db_session)):
    try:
        action = expire_pending_action(db, session_id=session_id, action_id=action_id)
    except SQLAlchemyError as exc:
        raise _database_unavailable_error() from exc
    if action is None:
        raise AppError(
            status=ResponseStatus.INFORMATION_NOT_AVAILABLE,
            code="pending_action_not_found",
            message=f"No pending action '{action_id}' exists for session '{session_id}'.",
            http_status_code=HTTP_404_NOT_FOUND,
        )
    return ResponseBuilder.success(request, answer="Pending action expired safely.", data=pending_action_to_dict(action))


@router.get("/{session_id}/history", summary="Read bounded short-term conversation history for one session")
def get_session_history_endpoint(
    session_id: UUID,
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db_session),
):
    try:
        history = get_session_history(db, session_id=session_id, limit=limit)
    except SessionMemoryError as exc:
        raise AppError(
            status=ResponseStatus.INFORMATION_NOT_AVAILABLE,
            code=exc.code,
            message=exc.message,
            http_status_code=HTTP_404_NOT_FOUND,
        ) from exc
    except SQLAlchemyError as exc:
        raise _database_unavailable_error() from exc
    return ResponseBuilder.success(
        request,
        answer="Short-term session history retrieved.",
        data=history,
    )


@router.delete("/{session_id}", summary="Close a short-term session without deleting audit history")
def close_session_endpoint(
    session_id: UUID,
    request: Request,
    db: Session = Depends(get_db_session),
):
    try:
        session = close_session(db, session_id=session_id)
    except SessionMemoryError as exc:
        raise AppError(
            status=ResponseStatus.INFORMATION_NOT_AVAILABLE,
            code=exc.code,
            message=exc.message,
            http_status_code=HTTP_404_NOT_FOUND,
        ) from exc
    except SQLAlchemyError as exc:
        raise _database_unavailable_error() from exc
    return ResponseBuilder.success(
        request,
        answer="Session closed. Persisted query and action audit history was retained.",
        data=session_to_dict(session),
    )
