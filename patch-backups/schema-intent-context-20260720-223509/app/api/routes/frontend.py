"""Phase 13 frontend-ready API surface.

These endpoints are intentionally thin adapters over the existing LangGraph and MCP
boundaries.  They do not create a second unsafe data-access path.
"""

from __future__ import annotations

from time import perf_counter
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.status import HTTP_400_BAD_REQUEST, HTTP_404_NOT_FOUND, HTTP_503_SERVICE_UNAVAILABLE

from app.agents.orchestrator import AgentOrchestrationError, agent_orchestrator
from app.agents.router import classify_question
from app.core.constants import ResponseStatus, UserRole
from app.core.exceptions import AppError
from app.core.request_context import get_request_id
from app.core.response import ResponseBuilder
from app.core.security import get_current_role, require_role
from app.db.session import get_db_session
from app.mcp.client import LocalMCPClient
from app.schemas.phase13 import FrontendQueryRequest
from app.models.operations import ActionLog, QueryLog
from app.services.session_memory_service import build_memory_context, get_session_history, write_agent_memory_event
from app.services.conversation_context_service import (
    conversation_reference_from_result,
    resolve_conversation_followup,
)
from app.services.session_service import create_session, get_active_session

router = APIRouter(prefix="/api", tags=["Frontend-ready integration"])


def _mcp_client() -> LocalMCPClient:
    return LocalMCPClient()


def _raise_agent_error(exc: AgentOrchestrationError) -> None:
    if exc.status == ResponseStatus.INFORMATION_NOT_AVAILABLE:
        status_code = HTTP_404_NOT_FOUND
    elif exc.status in {ResponseStatus.LLM_UNAVAILABLE, ResponseStatus.DATABASE_UNAVAILABLE, ResponseStatus.RETRIEVAL_FAILED, ResponseStatus.TOOL_FAILED}:
        status_code = HTTP_503_SERVICE_UNAVAILABLE
    else:
        status_code = HTTP_400_BAD_REQUEST
    from app.core.constants import AgentRoute

    raise AppError(
        status=exc.status,
        code=exc.code,
        message=exc.message,
        http_status_code=status_code,
        route=AgentRoute.SYSTEM,
        details=exc.details,
    ) from exc


def _ensure_session(db: Session, session_id: UUID | None, role: UserRole):
    if session_id is None:
        return create_session(db, user_role=role)
    session = get_active_session(db, session_id)
    if session is None:
        raise AppError(
            status=ResponseStatus.INFORMATION_NOT_AVAILABLE,
            code="inactive_or_missing_session",
            message="The supplied session is missing, expired, or inactive. Start a new chat session.",
            http_status_code=HTTP_404_NOT_FOUND,
        )
    return session


@router.post("/query", summary="Frontend-friendly natural-language query with short-term session memory")
def frontend_query(
    payload: FrontendQueryRequest,
    request: Request,
    db: Session = Depends(get_db_session),
    role: UserRole = Depends(get_current_role),
):
    """Create/reuse a session, load bounded context, then invoke the existing safe graph."""

    session = _ensure_session(db, payload.session_id, role)
    # ResponseBuilder and middleware read this value, so the frontend receives the
    # session ID in both the JSON envelope and the response header.
    request.state.session_id = str(session.id)
    memory_context = build_memory_context(db, session_id=session.id)
    started = perf_counter()

    # Resolve vague references deterministically before routing to Ollama/SQL.  This is
    # the guard that makes "Can I see it?" refer to the exact persisted pending or
    # confirmed action instead of allowing the model to reuse an unrelated old filter.
    resolution = resolve_conversation_followup(db, session_id=session.id, question=payload.question)
    if resolution.handled:
        resolution_data = dict(resolution.data or {})
        conversation_reference = conversation_reference_from_result(
            pending_action_id=resolution.pending_action_id,
            route=resolution.route.value,
            status=resolution.status.value,
            data=resolution_data,
        )
        memory_audit = write_agent_memory_event(
            db,
            request_id=get_request_id(request),
            session_id=session.id,
            question=payload.question,
            route=resolution.route.value,
            status=resolution.status.value,
            answer=resolution.answer,
            generated_sql=None,
            sources=[],
            latency_ms=round((perf_counter() - started) * 1000),
            conversation_reference=conversation_reference,
        )
        resolution_data["session"] = {
            "session_id": str(session.id),
            "created_for_request": payload.session_id is None,
            "memory_context_event_count": memory_context["event_count"],
            "memory_policy": memory_context["policy"],
            "memory_log": memory_audit,
        }
        return ResponseBuilder.success(
            request,
            route=resolution.route,
            status=resolution.status,
            answer=resolution.answer,
            data=resolution_data,
            sources=[],
            generated_sql=None,
            pending_action_id=resolution.pending_action_id,
        )

    try:
        result = agent_orchestrator.run(
            question=payload.question,
            db=db,
            request_id=get_request_id(request),
            session_id=str(session.id),
            user_role=role,
            top_k=payload.top_k,
            memory_context=memory_context,
        )
    except AgentOrchestrationError as exc:
        # Failed requests are part of conversation state.  Without this event, a later
        # "show it" could incorrectly fall back to an older successful action.
        decision = classify_question(payload.question)
        write_agent_memory_event(
            db,
            request_id=get_request_id(request),
            session_id=session.id,
            question=payload.question,
            route=decision.route.value,
            status=exc.status.value,
            answer=exc.message,
            generated_sql=None,
            sources=[],
            latency_ms=round((perf_counter() - started) * 1000),
            error_code=exc.code,
        )
        _raise_agent_error(exc)

    conversation_reference = conversation_reference_from_result(
        pending_action_id=result.pending_action_id,
        route=result.route.value,
        status=result.status.value,
        data=result.data,
    )
    memory_audit = write_agent_memory_event(
        db,
        request_id=get_request_id(request),
        session_id=session.id,
        question=payload.question,
        route=result.route.value,
        status=result.status.value,
        answer=result.answer,
        generated_sql=result.generated_sql,
        sources=[item.model_dump() for item in result.sources],
        latency_ms=round((perf_counter() - started) * 1000),
        conversation_reference=conversation_reference,
    )
    data = dict(result.data)
    data["session"] = {
        "session_id": str(session.id),
        "created_for_request": payload.session_id is None,
        "memory_context_event_count": memory_context["event_count"],
        "memory_policy": memory_context["policy"],
        "memory_log": memory_audit,
    }
    return ResponseBuilder.success(
        request,
        route=result.route,
        status=result.status,
        answer=result.answer,
        data=data,
        sources=result.sources,
        generated_sql=result.generated_sql,
        pending_action_id=result.pending_action_id,
    )


@router.get("/tables", summary="Frontend table directory through the hardened MCP catalog")
def frontend_tables(request: Request, role: UserRole = Depends(get_current_role)):
    outcome = _mcp_client().call_tool("list_tables", {"user_role": role.value})
    if not outcome.get("ok") or not outcome.get("result", {}).get("listed"):
        result = outcome.get("result", {})
        raise AppError(
            status=ResponseStatus.TOOL_FAILED,
            code=result.get("error_code", outcome.get("error_code", "mcp_table_listing_failed")),
            message=result.get("error_message", outcome.get("error_message", "Approved tables could not be listed.")),
            http_status_code=HTTP_503_SERVICE_UNAVAILABLE,
        )
    return ResponseBuilder.success(request, answer="Frontend table directory retrieved through the controlled MCP boundary.", data=outcome["result"])


@router.get("/tables/{table_name}/records", summary="Frontend bounded records viewer through MCP")
def frontend_table_records(
    table_name: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100000),
    role: UserRole = Depends(get_current_role),
):
    outcome = _mcp_client().call_tool(
        "get_table_records",
        {"table_name": table_name, "limit": limit, "offset": offset, "user_role": role.value},
    )
    result = outcome.get("result", {}) if outcome.get("ok") else outcome
    if not result.get("retrieved"):
        code = result.get("error_code", "mcp_table_read_failed")
        status_code = 403 if code in {"admin_role_required", "admin_role_required_for_operational_table"} else HTTP_400_BAD_REQUEST
        raise AppError(
            status=ResponseStatus.VALIDATION_FAILED,
            code=code,
            message=result.get("error_message", "The bounded table read could not be completed."),
            http_status_code=status_code,
        )
    return ResponseBuilder.success(
        request,
        answer=f"Frontend records viewer returned {result['row_count']} record(s) from '{result['table_name']}'.",
        data=result,
        sources=[{"source_type": "database", "reference": result["table_name"], "detail": "Bounded frontend read through the MCP boundary."}],
    )


@router.get("/logs", summary="Frontend admin viewer for bounded query and action audit history")
def frontend_logs(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    category: str = Query(default="all", pattern="^(all|queries|actions)$"),
    db: Session = Depends(get_db_session),
    _: UserRole = Depends(require_role(UserRole.ADMIN)),
):
    query_logs = []
    action_logs = []
    if category in {"all", "queries"}:
        query_logs = list(db.scalars(select(QueryLog).order_by(QueryLog.created_at.desc()).limit(limit)).all())
    if category in {"all", "actions"}:
        action_logs = list(db.scalars(select(ActionLog).order_by(ActionLog.created_at.desc()).limit(limit)).all())

    def query_item(item):
        return {
            "id": item.id,
            "request_id": item.request_id,
            "session_id": str(item.session_id) if item.session_id else None,
            "user_prompt": item.user_prompt,
            "detected_route": item.detected_route,
            "generated_sql": item.generated_sql,
            "status": item.status,
            "latency_ms": item.latency_ms,
            "source_references": item.source_references,
            "error_code": item.error_code,
            "created_at": item.created_at.isoformat() if item.created_at else None,
        }

    def action_item(item):
        return {
            "id": item.id,
            "request_id": item.request_id,
            "session_id": str(item.session_id) if item.session_id else None,
            "pending_action_id": str(item.pending_action_id) if item.pending_action_id else None,
            "actor_role": item.actor_role,
            "action_type": item.action_type,
            "target_table": item.target_table,
            "affected_record_ids": item.affected_record_ids,
            "generated_sql": item.generated_sql,
            "confirmation_status": item.confirmation_status,
            "status": item.status,
            "error_message": item.error_message,
            "created_at": item.created_at.isoformat() if item.created_at else None,
        }

    return ResponseBuilder.success(
        request,
        answer="Bounded admin audit history retrieved for the frontend viewer.",
        data={
            "category": category,
            "limit": limit,
            "query_log_count": len(query_logs),
            "action_log_count": len(action_logs),
            "query_logs": [query_item(item) for item in query_logs],
            "action_logs": [action_item(item) for item in action_logs],
        },
        sources=[{"source_type": "database", "reference": "query_logs, action_logs", "detail": "Admin-only audit history through the frontend API surface."}],
    )
