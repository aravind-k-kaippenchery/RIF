"""Phase 4 SQL validation and restricted admin schema-preview endpoints."""

from fastapi import APIRouter, Depends, Request
from starlette.status import HTTP_400_BAD_REQUEST

from app.core.constants import ResponseStatus, UserRole
from app.core.response import ResponseBuilder
from app.core.security import get_current_role, require_role
from app.schemas.phase4 import SQLValidationRequest, SchemaChangePreviewRequest
from app.services.sql_validation import preview_admin_schema_change, validate_dml_sql

router = APIRouter(prefix="/api/validation", tags=["SQL validation"])


def _validation_response(request: Request, result, *, answer: str):
    if result.is_valid:
        return ResponseBuilder.success(request, answer=answer, data=result.to_dict())
    return ResponseBuilder.error(
        request,
        status=ResponseStatus.VALIDATION_FAILED,
        code=result.error_code or "sql_validation_failed",
        message=result.error_message or "The SQL proposal failed validation.",
        http_status_code=HTTP_400_BAD_REQUEST,
        details=[{"statement_type": result.statement_type, "tables": result.tables, "columns": result.columns}],
    )


@router.post("/sql", summary="Validate one DML proposal without executing it")
def validate_sql_endpoint(
    payload: SQLValidationRequest,
    request: Request,
    role: UserRole = Depends(get_current_role),
):
    result = validate_dml_sql(payload.sql, role=role)
    return _validation_response(
        request,
        result,
        answer="SQL proposal validated successfully. No SQL was executed.",
    )


@router.post("/schema-changes/preview", summary="Admin-only preview for restricted CREATE TABLE or ALTER TABLE ADD COLUMN")
def schema_change_preview_endpoint(
    payload: SchemaChangePreviewRequest,
    request: Request,
    _: UserRole = Depends(require_role(UserRole.ADMIN)),
):
    result = preview_admin_schema_change(payload.sql)
    return _validation_response(
        request,
        result,
        answer="Restricted admin schema-change preview validated. No schema change was executed.",
    )
