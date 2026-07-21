"""Phase 7 endpoints for confirmation-gated CRUD writes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.status import HTTP_400_BAD_REQUEST, HTTP_409_CONFLICT, HTTP_503_SERVICE_UNAVAILABLE

from app.core.constants import AgentRoute, ResponseStatus, UserRole
from app.core.exceptions import AppError
from app.core.request_context import get_request_id
from app.core.response import ResponseBuilder
from app.core.security import get_current_role
from app.db.session import get_db_session
from app.schemas.phase7 import (
    BulkWriteProposalRequest,
    PendingActionMutationRequest,
    PromptWriteProposalRequest,
    SyntheticEmployeeProposalRequest,
    WriteProposalRequest,
)
from app.services.crud_write_service import CrudWriteError, crud_write_service
from app.services.llm_service import LLMOutputValidationError, LLMServiceError, OllamaUnavailableError, llm_service
from app.services.synthetic_employee_service import (
    SyntheticEmployeeGenerationError,
    SyntheticEmployeeRequest,
    generate_synthetic_employees,
)

router = APIRouter(prefix="/api/crud", tags=["Confirmation-gated CRUD"])


def _raise_crud_error(exc: CrudWriteError) -> None:
    status_code = HTTP_400_BAD_REQUEST
    if exc.status == ResponseStatus.DUPLICATE_DETECTED:
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


def _proposal_response(request: Request, result, *, answer: str, extra_data: dict | None = None):
    data = {
        "pending_action": result.pending_action,
        "preview": result.preview,
        "validation": result.validation.to_dict() if result.validation else None,
        "duplicate_matches": result.duplicate_matches,
        "model_metadata": result.model_metadata,
        "write_execution_allowed": False,
        "next_step": "Call the confirm endpoint with the same session_id and pending_action_id to execute this exact stored proposal.",
    }
    if extra_data:
        data.update(extra_data)
    return ResponseBuilder.success(
        request,
        route=AgentRoute.CRUD_WRITE,
        status=ResponseStatus.PENDING_CONFIRMATION,
        answer=answer,
        data=data,
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


@router.post("/generate-employees-propose", summary="Generate synthetic employees with Faker, then create the usual duplicate-checked bulk confirmation preview")
def propose_synthetic_employee_batch(
    payload: SyntheticEmployeeProposalRequest,
    request: Request,
    db: Session = Depends(get_db_session),
    role: UserRole = Depends(get_current_role),
):
    try:
        batch = generate_synthetic_employees(
            SyntheticEmployeeRequest(
                count=payload.count,
                department=payload.department,
                city=payload.city,
                company_name=payload.company_name,
            )
        )
        result = crud_write_service.propose_bulk_insert(
            db,
            session_id=payload.session_id,
            target_table="employees",
            records=batch.records,
            actor_role=role,
            user_prompt="Generate synthetic employee records with Faker.",
            ttl_minutes=payload.ttl_minutes,
            generation_metadata=batch.metadata,
        )
    except SyntheticEmployeeGenerationError as exc:
        raise AppError(
            status=ResponseStatus.VALIDATION_FAILED,
            code=exc.code,
            message=exc.message,
            http_status_code=HTTP_400_BAD_REQUEST,
        ) from exc
    except CrudWriteError as exc:
        _raise_crud_error(exc)

    return _proposal_response(
        request,
        result,
        answer="Synthetic employee batch preview created with Faker. No database rows were inserted; confirmation is still required.",
        extra_data={
            "generated_record_count": len(batch.records),
            "generator": "faker",
            "synthetic_generation": batch.metadata,
        },
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
    return ResponseBuilder.success(
        request,
        route=AgentRoute.CRUD_WRITE,
        answer=("Confirmation was already processed earlier; no duplicate write was executed." if result.idempotent else "Confirmed write executed successfully with audit logging and snapshots."),
        data={
            "pending_action": result.pending_action,
            "action_log_id": result.action_log_id,
            "affected_row_count": result.affected_row_count,
            "before_snapshot_count": result.before_snapshot_count,
            "after_snapshot_count": result.after_snapshot_count,
            "idempotent": result.idempotent,
            "write_execution_allowed": True,
            "write_execution_mode": "confirmed_pending_action_only",
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
