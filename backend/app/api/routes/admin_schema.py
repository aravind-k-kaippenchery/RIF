"""Phase 13 frontend/admin routes for persisted restricted schema-change approvals."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.status import HTTP_400_BAD_REQUEST, HTTP_404_NOT_FOUND, HTTP_503_SERVICE_UNAVAILABLE

from app.core.constants import ResponseStatus, UserRole
from app.core.exceptions import AppError
from app.core.request_context import get_request_id
from app.core.response import ResponseBuilder
from app.core.security import require_role
from app.db.session import get_db_session
from app.mcp.client import LocalMCPClient
from app.models.operations import SchemaChangeRequest
from app.schemas.phase12 import MCPSchemaChangePreviewRequest
from app.schemas.phase13 import AdminSchemaConfirmRequest, AdminSchemaPromptRequest
from app.services.admin_schema_service import schema_change_to_dict
from app.services.llm_service import LLMOutputValidationError, LLMServiceError, OllamaUnavailableError, llm_service

router = APIRouter(prefix="/api/admin/schema-changes", tags=["Admin schema workflow"])


def _admin() -> UserRole:
    return UserRole.ADMIN


def _raise_result(result: dict, *, fallback: str) -> None:
    code = result.get("error_code", "admin_schema_workflow_failed")
    message = result.get("error_message", fallback)
    status_code = HTTP_404_NOT_FOUND if code == "schema_change_not_found" else HTTP_400_BAD_REQUEST
    if code in {"admin_schema_execution_failed", "schema_preview_storage_failed"}:
        status_code = HTTP_503_SERVICE_UNAVAILABLE
    raise AppError(
        status=ResponseStatus.INFORMATION_NOT_AVAILABLE if status_code == HTTP_404_NOT_FOUND else ResponseStatus.VALIDATION_FAILED,
        code=code,
        message=message,
        http_status_code=status_code,
    )


@router.get("", summary="List auditable restricted admin schema-change requests")
def list_schema_changes(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db_session),
    _: UserRole = Depends(require_role(UserRole.ADMIN)),
):
    changes = list(db.scalars(select(SchemaChangeRequest).order_by(SchemaChangeRequest.created_at.desc()).limit(limit)).all())
    return ResponseBuilder.success(
        request,
        answer=f"Retrieved {len(changes)} restricted admin schema-change request(s).",
        data={"schema_change_count": len(changes), "schema_changes": [schema_change_to_dict(item) for item in changes]},
    )


@router.get("/{schema_change_id}", summary="Read one persisted admin schema-change request")
def get_schema_change(
    schema_change_id: UUID,
    request: Request,
    db: Session = Depends(get_db_session),
    _: UserRole = Depends(require_role(UserRole.ADMIN)),
):
    change = db.get(SchemaChangeRequest, schema_change_id)
    if change is None:
        raise AppError(status=ResponseStatus.INFORMATION_NOT_AVAILABLE, code="schema_change_not_found", message="No stored schema-change request exists with this ID.", http_status_code=HTTP_404_NOT_FOUND)
    return ResponseBuilder.success(request, answer="Admin schema-change request retrieved.", data={"schema_change": schema_change_to_dict(change)})


@router.post("/propose-from-prompt", summary="Generate and store a restricted admin schema preview from natural language")
def admin_schema_prompt_proposal(
    payload: AdminSchemaPromptRequest,
    request: Request,
    _: UserRole = Depends(require_role(UserRole.ADMIN)),
):
    try:
        proposal, validation, metadata = llm_service.generate_admin_schema_sql(payload.user_prompt)
    except OllamaUnavailableError as exc:
        raise AppError(status=ResponseStatus.LLM_UNAVAILABLE, code=exc.code, message=exc.message, http_status_code=HTTP_503_SERVICE_UNAVAILABLE) from exc
    except (LLMOutputValidationError, LLMServiceError) as exc:
        raise AppError(status=ResponseStatus.VALIDATION_FAILED, code=exc.code, message=exc.message, http_status_code=HTTP_400_BAD_REQUEST) from exc

    outcome = LocalMCPClient().call_tool(
        "create_schema_change_preview",
        {
            "sql": validation.normalized_sql or proposal.sql,
            "user_prompt": payload.user_prompt,
            "user_role": UserRole.ADMIN.value,
            "session_id": str(payload.session_id) if payload.session_id else None,
        },
    )
    result = outcome.get("result", {}) if outcome.get("ok") else outcome
    if not result.get("created"):
        _raise_result(result, fallback="The restricted admin schema preview could not be stored.")
    data = dict(result)
    data["model_proposal"] = {
        "operation": proposal.operation,
        "explanation": proposal.explanation,
        "model_metadata": metadata.model_dump(),
        "validation": validation.to_dict(),
    }
    return ResponseBuilder.success(
        request,
        status=ResponseStatus.PENDING_CONFIRMATION,
        answer="Restricted admin schema proposal was generated locally, validated, and stored as a preview. Explicit confirmation is still required.",
        data=data,
    )


@router.post("/preview", summary="Create an admin-only restricted schema preview through the MCP boundary")
def admin_schema_preview(
    payload: MCPSchemaChangePreviewRequest,
    request: Request,
    _: UserRole = Depends(require_role(UserRole.ADMIN)),
):
    outcome = LocalMCPClient().call_tool(
        "create_schema_change_preview",
        {
            "sql": payload.sql,
            "user_prompt": payload.user_prompt,
            "user_role": UserRole.ADMIN.value,
            "session_id": str(payload.session_id) if payload.session_id else None,
        },
    )
    result = outcome.get("result", {}) if outcome.get("ok") else outcome
    if not result.get("created"):
        _raise_result(result, fallback="The restricted admin schema preview could not be created.")
    return ResponseBuilder.success(request, answer="Admin schema-change preview stored. Explicit confirmation is required before execution.", data=result)


@router.post("/{schema_change_id}/confirm", summary="Confirm and execute one previously stored restricted admin schema change")
def confirm_schema_change(
    schema_change_id: UUID,
    payload: AdminSchemaConfirmRequest,
    request: Request,
    _: UserRole = Depends(require_role(UserRole.ADMIN)),
):
    outcome = LocalMCPClient().call_tool(
        "apply_admin_schema_change",
        {
            "schema_change_id": str(schema_change_id),
            "confirmed": payload.confirmed,
            "user_role": UserRole.ADMIN.value,
            "session_id": str(payload.session_id) if payload.session_id else None,
            "request_id": get_request_id(request),
        },
    )
    result = outcome.get("result", {}) if outcome.get("ok") else outcome
    if not result.get("executed") and not result.get("idempotent"):
        _raise_result(result, fallback="The restricted schema change was not executed.")
    return ResponseBuilder.success(
        request,
        answer="Restricted admin schema change executed from the persisted, validated preview." if result.get("executed") else "Restricted admin schema change was already applied; returned idempotent audit state.",
        data=result,
    )
