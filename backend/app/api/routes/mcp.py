"""REST verification endpoints for the Phase 13 hardened local MCP tool boundary."""

from fastapi import APIRouter, Depends, Query, Request
from starlette.status import (
    HTTP_400_BAD_REQUEST,
    HTTP_403_FORBIDDEN,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from app.core.constants import ResponseStatus, UserRole
from app.core.exceptions import AppError
from app.core.request_context import get_request_id
from app.core.response import ResponseBuilder
from app.core.security import get_current_role
from app.mcp.client import LocalMCPClient
from app.schemas.phase12 import MCPSchemaChangeApplyRequest, MCPSchemaChangePreviewRequest
from app.schemas.phase4 import ReadExecutionRequest, SQLValidationRequest
from app.services.validated_read_service import ValidatedReadError

router = APIRouter(prefix="/api/mcp", tags=["MCP core"])


def _client() -> LocalMCPClient:
    return LocalMCPClient()


def _raise_mcp_failure(result: dict, *, fallback_code: str, fallback_message: str, forbidden: bool = False) -> None:
    """Convert one structured local MCP tool failure into the shared API envelope."""

    code = result.get("error_code", fallback_code)
    message = result.get("error_message", fallback_message)
    if code in {"admin_role_required", "admin_role_required_for_operational_table"} or forbidden:
        status_code = HTTP_403_FORBIDDEN
    elif code in {"mcp_table_read_failed", "schema_preview_storage_failed"}:
        status_code = HTTP_503_SERVICE_UNAVAILABLE
    else:
        status_code = HTTP_400_BAD_REQUEST
    raise AppError(
        status=ResponseStatus.VALIDATION_FAILED if status_code != HTTP_503_SERVICE_UNAVAILABLE else ResponseStatus.TOOL_FAILED,
        code=code,
        message=message,
        http_status_code=status_code,
    )


@router.get("/status", summary="Describe the local MCP server and hardened tool boundary")
def mcp_status(request: Request):
    return ResponseBuilder.success(
        request,
        answer="Local MCP server remains mounted with controlled schema, bounded table reads, validated reads, confirmation-gated writes, local document retrieval, and persisted restricted admin schema execution. Phase 15 benchmarking reuses this boundary and accepts no raw SQL.",
        data={
            "phase": 15,
            "server_path": "/mcp",
            "transport": "streamable_http",
            "write_execution_allowed_without_confirmation": False,
            "confirmed_write_execution_available": True,
            "document_retrieval_available": True,
            "bounded_table_read_available": True,
            "operational_table_reads_require_admin": True,
            "admin_schema_preview_available": True,
            "admin_schema_execution_available": True,
            "admin_schema_execution_mode": "restricted_confirmed_persisted_preview",
            "audit_rollback_control_plane_available": True,
            "benchmarking_reuses_controlled_mcp_tools": True,
            "raw_sql_write_tool_exposed": False,
            "unsafe_tool_exposed": False,
            "tool_count": len(_client().list_tools()),
        },
    )


@router.get("/tools", summary="List registered controlled MCP tools")
def list_mcp_tools(request: Request):
    tools = _client().list_tools()
    return ResponseBuilder.success(
        request,
        answer="Registered controlled MCP tools retrieved.",
        data={"tool_count": len(tools), "tools": tools},
    )


@router.get("/tables", summary="Call the role-aware list_tables MCP tool")
def mcp_list_tables(
    request: Request,
    role: UserRole = Depends(get_current_role),
):
    outcome = _client().call_tool("list_tables", {"user_role": role.value})
    if not outcome.get("ok"):
        _raise_mcp_failure(outcome, fallback_code="mcp_tool_failed", fallback_message="MCP list_tables tool failed.")
    result = outcome["result"]
    if not result.get("listed"):
        _raise_mcp_failure(result, fallback_code="mcp_table_listing_failed", fallback_message="MCP list_tables tool could not list approved tables.")
    return ResponseBuilder.success(
        request,
        answer="MCP list_tables tool returned approved application tables only.",
        data=result,
    )


@router.get("/tables/{table_name}/records", summary="Call the bounded get_table_records MCP tool")
def mcp_get_table_records(
    table_name: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100000),
    role: UserRole = Depends(get_current_role),
):
    outcome = _client().call_tool(
        "get_table_records",
        {
            "table_name": table_name,
            "limit": limit,
            "offset": offset,
            "user_role": role.value,
        },
    )
    if not outcome.get("ok"):
        _raise_mcp_failure(outcome, fallback_code="mcp_tool_failed", fallback_message="MCP get_table_records tool failed.")
    result = outcome["result"]
    if not result.get("retrieved"):
        _raise_mcp_failure(result, fallback_code="mcp_table_read_failed", fallback_message="MCP table-read tool could not retrieve records.")
    return ResponseBuilder.success(
        request,
        answer=f"MCP get_table_records tool returned {result['row_count']} record(s) from '{result['table_name']}'.",
        data=result,
        sources=[
            {
                "source_type": "database",
                "reference": result["table_name"],
                "detail": "Bounded role-aware table read through the MCP tool boundary.",
            }
        ],
    )


@router.post("/validate-sql", summary="Call the validate_sql MCP tool through the local client facade")
def mcp_validate_sql(
    payload: SQLValidationRequest,
    request: Request,
    role: UserRole = Depends(get_current_role),
):
    outcome = _client().call_tool("validate_sql", {"sql": payload.sql, "user_role": role.value})
    if not outcome.get("ok"):
        _raise_mcp_failure(outcome, fallback_code="mcp_tool_failed", fallback_message="MCP validation tool failed.")
    result = outcome["result"]
    if not result.get("is_valid"):
        raise AppError(
            status=ResponseStatus.VALIDATION_FAILED,
            code=result.get("error_code", "sql_validation_failed"),
            message=result.get("error_message", "The SQL proposal failed validation."),
            http_status_code=HTTP_400_BAD_REQUEST,
        )
    return ResponseBuilder.success(request, answer="MCP validate_sql tool completed successfully. No SQL was executed.", data=result)


@router.post("/execute-read", summary="Call the SELECT-only execute_validated_read MCP tool")
def mcp_execute_read(payload: ReadExecutionRequest, request: Request):
    try:
        outcome = _client().call_tool("execute_validated_read", {"sql": payload.sql})
    except ValidatedReadError as exc:  # pragma: no cover - tool facade catches defensive errors
        raise AppError(
            status=ResponseStatus.DATABASE_UNAVAILABLE,
            code="validated_read_database_error",
            message="PostgreSQL could not run the validated SELECT.",
            http_status_code=HTTP_503_SERVICE_UNAVAILABLE,
        ) from exc

    if not outcome.get("ok"):
        _raise_mcp_failure(outcome, fallback_code="mcp_tool_failed", fallback_message="MCP read tool failed.")
    result = outcome["result"]
    if not result.get("executed"):
        validation = result.get("validation", {})
        code = validation.get("error_code") or result.get("error", {}).get("code", "read_not_executed")
        message = validation.get("error_message") or result.get("error", {}).get("message", "The SELECT was not executed.")
        raise AppError(
            status=ResponseStatus.VALIDATION_FAILED,
            code=code,
            message=message,
            http_status_code=HTTP_400_BAD_REQUEST,
        )
    return ResponseBuilder.success(
        request,
        answer=f"MCP execute_validated_read tool returned {result['row_count']} row(s).",
        data=result,
        generated_sql=result["validation"].get("normalized_sql"),
    )


@router.post("/schema-changes/preview", summary="Store an admin-only schema preview through the MCP boundary")
def mcp_schema_change_preview(
    payload: MCPSchemaChangePreviewRequest,
    request: Request,
    role: UserRole = Depends(get_current_role),
):
    outcome = _client().call_tool(
        "create_schema_change_preview",
        {
            "sql": payload.sql,
            "user_prompt": payload.user_prompt,
            "user_role": role.value,
            "session_id": str(payload.session_id) if payload.session_id else None,
        },
    )
    if not outcome.get("ok"):
        _raise_mcp_failure(outcome, fallback_code="mcp_tool_failed", fallback_message="MCP schema-preview tool failed.")
    result = outcome["result"]
    if not result.get("created"):
        _raise_mcp_failure(result, fallback_code="schema_preview_failed", fallback_message="The schema-change preview could not be created.")
    return ResponseBuilder.success(
        request,
        answer="Admin schema-change preview was validated and stored. No DDL was executed.",
        data=result,
    )


@router.post("/schema-changes/{schema_change_id}/apply", summary="Execute one persisted restricted admin schema request after explicit confirmation")
def mcp_schema_change_apply(
    schema_change_id: str,
    payload: MCPSchemaChangeApplyRequest,
    request: Request,
    role: UserRole = Depends(get_current_role),
):
    outcome = _client().call_tool(
        "apply_admin_schema_change",
        {
            "schema_change_id": schema_change_id,
            "confirmed": payload.confirmed,
            "user_role": role.value,
            "session_id": str(payload.session_id) if payload.session_id else None,
            "request_id": get_request_id(request),
        },
    )
    if not outcome.get("ok"):
        _raise_mcp_failure(outcome, fallback_code="mcp_tool_failed", fallback_message="MCP schema-apply tool failed.")
    result = outcome["result"]
    if not result.get("executed") and not result.get("idempotent"):
        _raise_mcp_failure(result, fallback_code="schema_change_not_applied", fallback_message="The schema-change request was not applied.")
    answer = (
        "Restricted admin schema change executed from the persisted, validator-approved preview."
        if result.get("executed")
        else "Restricted admin schema change was already applied; returned idempotent audit state."
    )
    return ResponseBuilder.success(request, answer=answer, data=result)
