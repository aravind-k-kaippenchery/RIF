"""Phase 7 endpoints for confirmation-gated CRUD writes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.status import HTTP_400_BAD_REQUEST, HTTP_403_FORBIDDEN, HTTP_409_CONFLICT, HTTP_503_SERVICE_UNAVAILABLE

from app.core.constants import AgentRoute, ResponseStatus, UserRole
from app.core.exceptions import AppError
from app.core.request_context import get_request_id
from app.core.response import ResponseBuilder
from app.core.security import get_current_role
from app.db.session import get_db_session
from app.schemas.phase7 import BulkWriteProposalRequest, DuplicateCheckRequest, PendingActionMutationRequest, PromptWriteProposalRequest, WriteProposalRequest
from app.services.crud_write_service import CrudWriteError, crud_write_service
from app.services.llm_service import LLMOutputValidationError, LLMServiceError, OllamaUnavailableError, llm_service

router = APIRouter(prefix="/api/crud", tags=["Confirmation-gated CRUD"])


def _raise_crud_error(exc: CrudWriteError) -> None:
    status_code = HTTP_400_BAD_REQUEST
    if exc.code in {"admin_role_required_for_confirmation"}:
        status_code = HTTP_403_FORBIDDEN
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


def _raise_llm_error(exc: LLMServiceError) -> None:
    if isinstance(exc, OllamaUnavailableError):
        raise AppError(status=ResponseStatus.LLM_UNAVAILABLE, code=exc.code, message=exc.message, http_status_code=HTTP_503_SERVICE_UNAVAILABLE) from exc
    if isinstance(exc, LLMOutputValidationError):
        raise AppError(status=ResponseStatus.VALIDATION_FAILED, code=exc.code, message=exc.message, http_status_code=HTTP_400_BAD_REQUEST) from exc
    raise AppError(status=ResponseStatus.TOOL_FAILED, code=exc.code, message=exc.message, http_status_code=HTTP_503_SERVICE_UNAVAILABLE) from exc


def _proposal_response(request: Request, result, *, answer: str):
    return ResponseBuilder.success(
        request,
        route=AgentRoute.CRUD_WRITE,
        status=ResponseStatus.PENDING_CONFIRMATION,
        answer=answer,
        data={
            "pending_action": result.pending_action,
            "preview": result.preview,
            "validation": result.validation.to_dict() if result.validation else None,
            "duplicate_matches": result.duplicate_matches,
            "model_metadata": result.model_metadata,
            "write_execution_allowed": False,
            "next_step": "Call the confirm endpoint with the same session_id and pending_action_id to execute this exact stored proposal.",
        },
        pending_action_id=result.pending_action["pending_action_id"],
        generated_sql=result.generated_sql,
    )


@router.post("/propose", summary="Preview one validator-approved INSERT, UPDATE, or DELETE without executing it")
def propose_write(
    payload: WriteProposalRequest,
    request: Request,
    db: Session = Depends(get_db_session),
    role: UserRole = Depends(get_current_role),
):
    try:
        result = crud_write_service.propose_sql_write(
            db,
            session_id=payload.session_id,
            sql=payload.sql,
            actor_role=role,
            user_prompt=payload.user_prompt,
            ttl_minutes=payload.ttl_minutes,
        )
    except CrudWriteError as exc:
        _raise_crud_error(exc)
    except SQLAlchemyError as exc:
        raise AppError(status=ResponseStatus.DATABASE_UNAVAILABLE, code="database_unavailable", message="PostgreSQL is unavailable for write preview.", http_status_code=HTTP_503_SERVICE_UNAVAILABLE) from exc
    return _proposal_response(request, result, answer="Write preview created. No database row was changed; explicit confirmation is required.")


@router.post("/propose-from-prompt", summary="Use local Llama 3 to create a CRUD SQL proposal, then store only a confirmation preview")
def propose_write_from_prompt(
    payload: PromptWriteProposalRequest,
    request: Request,
    db: Session = Depends(get_db_session),
    role: UserRole = Depends(get_current_role),
):
    try:
        proposal, validation, metadata, _ = llm_service.generate_sql(payload.question, "crud_write")
        result = crud_write_service.propose_sql_write(
            db,
            session_id=payload.session_id,
            sql=validation.normalized_sql or proposal.sql,
            actor_role=role,
            user_prompt=payload.question,
            ttl_minutes=payload.ttl_minutes,
            model_metadata=metadata.model_dump(),
        )
    except LLMServiceError as exc:
        _raise_llm_error(exc)
    except CrudWriteError as exc:
        _raise_crud_error(exc)
    return _proposal_response(request, result, answer="Local LLM proposal was validated and stored as a preview. No database row was changed.")


@router.post("/bulk-propose", summary="Preview a parameterized bulk INSERT after duplicate checks")
def propose_bulk_insert(
    payload: BulkWriteProposalRequest,
    request: Request,
    db: Session = Depends(get_db_session),
    role: UserRole = Depends(get_current_role),
):
    try:
        result = crud_write_service.propose_bulk_insert(
            db,
            session_id=payload.session_id,
            target_table=payload.target_table,
            records=payload.records,
            actor_role=role,
            user_prompt=payload.user_prompt,
            ttl_minutes=payload.ttl_minutes,
        )
    except CrudWriteError as exc:
        _raise_crud_error(exc)
    return _proposal_response(request, result, answer="Bulk insert preview created after duplicate checks. No database rows were inserted.")


@router.post("/check-duplicates", summary="Check reflected PostgreSQL unique keys without creating or changing data")
def check_duplicates(
    payload: DuplicateCheckRequest,
    request: Request,
    db: Session = Depends(get_db_session),
    role: UserRole = Depends(get_current_role),
):
    del role  # Authentication/role parsing still runs; this endpoint is read-only.
    try:
        result = crud_write_service.check_duplicates(
            db,
            target_table=payload.target_table,
            values=payload.values,
        )
    except CrudWriteError as exc:
        _raise_crud_error(exc)
    return ResponseBuilder.success(
        request,
        route=AgentRoute.STRUCTURED_READ,
        answer=(
            "A matching record was found for at least one reflected unique key."
            if result["duplicate_found"]
            else "No matching record was found for the complete reflected unique keys supplied."
        ),
        data=result,
    )


@router.post("/actions/{pending_action_id}/confirm", summary="Execute exactly one stored pending action in one transaction")
def confirm_write(
    pending_action_id: str,
    payload: PendingActionMutationRequest,
    request: Request,
    db: Session = Depends(get_db_session),
    role: UserRole = Depends(get_current_role),
):
    from uuid import UUID

    try:
        result = crud_write_service.confirm_action(
            db,
            session_id=payload.session_id,
            pending_action_id=UUID(pending_action_id),
            actor_role=role,
            request_id=get_request_id(request) or "missing-request-id",
        )
    except ValueError as exc:
        raise AppError(status=ResponseStatus.VALIDATION_FAILED, code="invalid_pending_action_id", message="pending_action_id must be a UUID.", http_status_code=HTTP_400_BAD_REQUEST) from exc
    except CrudWriteError as exc:
        _raise_crud_error(exc)
    record_word = "record" if result.affected_row_count == 1 else "records"
    answer = (
        "Confirmation was already processed earlier; no duplicate write was executed."
        if result.idempotent
        else f"Confirmed write executed successfully. Exactly {result.affected_row_count} {record_word} were affected, with audit logging and snapshots."
    )
    return ResponseBuilder.success(
        request,
        route=AgentRoute.CRUD_WRITE,
        answer=answer,
        data={
            "pending_action": result.pending_action,
            "action_log_id": result.action_log_id,
            "affected_row_count": result.affected_row_count,
            "affected_record_ids": result.affected_record_ids,
            "affected_records": result.affected_records,
            "rows": result.affected_records,
            "before_snapshot_count": result.before_snapshot_count,
            "after_snapshot_count": result.after_snapshot_count,
            "expected_row_count": result.expected_row_count,
            "confirmed_record_count": result.affected_row_count,
            "count_verified": result.count_verified,
            "idempotent": result.idempotent,
            "write_execution_allowed": True,
            "write_execution_mode": "confirmed_pending_action_only",
            "next_step": "Use View created records to inspect the exact database rows affected by this confirmation.",
        },
        pending_action_id=result.pending_action["pending_action_id"],
    )


@router.post("/actions/{pending_action_id}/cancel", summary="Cancel a pending write action without changing business data")
def cancel_write(
    pending_action_id: str,
    payload: PendingActionMutationRequest,
    request: Request,
    db: Session = Depends(get_db_session),
    role: UserRole = Depends(get_current_role),
):
    from uuid import UUID

    try:
        result = crud_write_service.cancel_action(
            db,
            session_id=payload.session_id,
            pending_action_id=UUID(pending_action_id),
            actor_role=role,
            request_id=get_request_id(request) or "missing-request-id",
        )
    except ValueError as exc:
        raise AppError(status=ResponseStatus.VALIDATION_FAILED, code="invalid_pending_action_id", message="pending_action_id must be a UUID.", http_status_code=HTTP_400_BAD_REQUEST) from exc
    except CrudWriteError as exc:
        _raise_crud_error(exc)
    return ResponseBuilder.success(
        request,
        route=AgentRoute.CRUD_WRITE,
        status=ResponseStatus.CANCELLED,
        answer="Pending write action cancelled. No business data was changed.",
        data=result,
        pending_action_id=result["pending_action"]["pending_action_id"],
    )
