"""Phase 14 admin audit, snapshots, and confirmation-gated rollback endpoints."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session
from starlette.status import HTTP_400_BAD_REQUEST, HTTP_404_NOT_FOUND, HTTP_409_CONFLICT, HTTP_503_SERVICE_UNAVAILABLE

from app.core.constants import ResponseStatus, UserRole
from app.core.exceptions import AppError
from app.core.request_context import get_request_id
from app.core.response import ResponseBuilder
from app.core.security import require_role
from app.db.session import get_db_session
from app.schemas.phase14 import RollbackConfirmRequest, RollbackPreviewRequest
from app.services.audit_rollback_service import AuditRollbackError, audit_rollback_service
from app.services.session_service import create_session

router = APIRouter(prefix="/api/audit", tags=["Audit hardening and rollback"])


def _raise_audit_error(exc: AuditRollbackError) -> None:
    status_code = HTTP_400_BAD_REQUEST
    if exc.status == ResponseStatus.INFORMATION_NOT_AVAILABLE:
        status_code = HTTP_404_NOT_FOUND
    elif exc.status == ResponseStatus.DUPLICATE_DETECTED:
        status_code = HTTP_409_CONFLICT
    elif exc.status == ResponseStatus.DATABASE_UNAVAILABLE:
        status_code = HTTP_503_SERVICE_UNAVAILABLE
    raise AppError(
        status=exc.status,
        code=exc.code,
        message=exc.message,
        http_status_code=status_code,
        details=exc.details,
    ) from exc


@router.get("/status", summary="View Phase 14 audit and rollback capabilities")
def audit_status(request: Request, _: UserRole = Depends(require_role(UserRole.ADMIN))):
    return ResponseBuilder.success(
        request,
        answer="Phase 14 audit hardening and confirmation-gated rollback status retrieved.",
        data={
            "phase": 14,
            "audit_action_history_available": True,
            "change_snapshot_view_available": True,
            "rollback_preview_available": True,
            "rollback_confirmation_required": True,
            "rollback_supported_original_actions": ["update", "delete"],
            "rollback_insert_compensation_available": False,
            "rollback_requires_admin": True,
            "raw_sql_accepted": False,
            "notes": [
                "Rollback uses only stored before snapshots from successful audited business-table writes.",
                "A rollback is previewed first, stored as a pending action, and requires explicit admin confirmation.",
                "The final restore is revalidated against current PostgreSQL state before execution.",
                "Every completed rollback creates a new action log and snapshots of the rollback itself.",
            ],
        },
    )


@router.get("/actions", summary="List bounded action audit history with snapshot counts")
def list_audit_actions(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    action_type: str | None = Query(default=None, max_length=64),
    target_table: str | None = Query(default=None, max_length=128),
    status: str | None = Query(default=None, max_length=64),
    db: Session = Depends(get_db_session),
    _: UserRole = Depends(require_role(UserRole.ADMIN)),
):
    actions = audit_rollback_service.list_actions(
        db,
        limit=limit,
        action_type=action_type,
        target_table=target_table,
        status=status,
    )
    return ResponseBuilder.success(
        request,
        answer=f"Retrieved {len(actions)} bounded action-audit record(s).",
        data={
            "action_count": len(actions),
            "limit": limit,
            "filters": {"action_type": action_type, "target_table": target_table, "status": status},
            "actions": actions,
            "raw_sql_accepted": False,
        },
        sources=[{"source_type": "database", "reference": "action_logs, change_snapshots", "detail": "Admin-only bounded audit history."}],
    )


@router.get("/actions/{action_log_id}", summary="Read one action audit record and its snapshots")
def get_audit_action(
    action_log_id: int,
    request: Request,
    db: Session = Depends(get_db_session),
    _: UserRole = Depends(require_role(UserRole.ADMIN)),
):
    try:
        result = audit_rollback_service.get_action_detail(db, action_log_id=action_log_id)
    except AuditRollbackError as exc:
        _raise_audit_error(exc)
    return ResponseBuilder.success(
        request,
        answer="Action audit record and snapshot metadata retrieved.",
        data=result,
        sources=[{"source_type": "database", "reference": "action_logs, change_snapshots", "detail": "Admin-only audited write state."}],
    )


@router.get("/actions/{action_log_id}/snapshots", summary="Read snapshots for one audited action")
def get_action_snapshots(
    action_log_id: int,
    request: Request,
    db: Session = Depends(get_db_session),
    _: UserRole = Depends(require_role(UserRole.ADMIN)),
):
    try:
        result = audit_rollback_service.get_action_detail(db, action_log_id=action_log_id)
    except AuditRollbackError as exc:
        _raise_audit_error(exc)
    return ResponseBuilder.success(
        request,
        answer=f"Retrieved {len(result['snapshots'])} change snapshot(s) for action #{action_log_id}.",
        data={"action": result["action"], "snapshot_count": len(result["snapshots"]), "snapshots": result["snapshots"], "raw_sql_accepted": False},
        sources=[{"source_type": "database", "reference": "change_snapshots", "detail": "Before/after rollback evidence."}],
    )


@router.post("/actions/{action_log_id}/rollback/preview", summary="Create a confirmation-gated admin rollback preview")
def preview_rollback(
    action_log_id: int,
    payload: RollbackPreviewRequest,
    request: Request,
    db: Session = Depends(get_db_session),
    role: UserRole = Depends(require_role(UserRole.ADMIN)),
):
    session = create_session(db, user_role=UserRole.ADMIN) if payload.session_id is None else None
    session_id = session.id if session is not None else payload.session_id
    try:
        result = audit_rollback_service.preview_rollback(
            db,
            original_action_log_id=action_log_id,
            session_id=session_id,
            actor_role=role,
            ttl_minutes=payload.ttl_minutes,
        )
    except AuditRollbackError as exc:
        _raise_audit_error(exc)
    return ResponseBuilder.success(
        request,
        status=ResponseStatus.PENDING_CONFIRMATION,
        answer="Rollback preview created from stored before snapshots. No data was restored; explicit admin confirmation is required.",
        data={
            "session_id": str(session_id),
            "session_created_for_rollback": session is not None,
            "original_action": result.original_action,
            "rollback_plan": result.rollback_plan,
            "pending_action": result.pending_action,
            "write_execution_allowed": False,
            "raw_sql_accepted": False,
            "next_step": "Confirm this exact rollback pending action using the same session ID.",
        },
        pending_action_id=result.pending_action["pending_action_id"],
    )


@router.post("/rollback-actions/{pending_action_id}/confirm", summary="Execute one stored admin rollback plan")
def confirm_rollback(
    pending_action_id: UUID,
    payload: RollbackConfirmRequest,
    request: Request,
    db: Session = Depends(get_db_session),
    role: UserRole = Depends(require_role(UserRole.ADMIN)),
):
    if not payload.confirmed:
        raise AppError(
            status=ResponseStatus.PENDING_CONFIRMATION,
            code="explicit_confirmation_required",
            message="Set confirmed to true to execute this stored rollback plan.",
            http_status_code=HTTP_400_BAD_REQUEST,
        )
    try:
        result = audit_rollback_service.confirm_rollback(
            db,
            session_id=payload.session_id,
            pending_action_id=pending_action_id,
            actor_role=role,
            request_id=get_request_id(request) or "missing-request-id",
        )
    except AuditRollbackError as exc:
        _raise_audit_error(exc)
    return ResponseBuilder.success(
        request,
        answer=(
            "Rollback confirmation was already processed earlier; no duplicate restore was executed."
            if result.idempotent
            else "Confirmed rollback restored audited business data and created a new rollback audit trail."
        ),
        data={
            "pending_action": result.pending_action,
            "rollback_action_log_id": result.rollback_action_log_id,
            "original_action_log_id": result.original_action_log_id,
            "restored_record_count": result.restored_record_count,
            "before_snapshot_count": result.before_snapshot_count,
            "after_snapshot_count": result.after_snapshot_count,
            "idempotent": result.idempotent,
            "execution_mode": "stored_before_snapshot_reconciliation_transaction",
            "raw_sql_accepted": False,
        },
        pending_action_id=result.pending_action["pending_action_id"],
        sources=[{"source_type": "database", "reference": "action_logs, change_snapshots", "detail": "Rollback executed only from stored audited snapshot evidence."}],
    )
