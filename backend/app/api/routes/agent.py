"""Phase 11 unified LangGraph agent API."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from starlette.status import HTTP_400_BAD_REQUEST, HTTP_404_NOT_FOUND, HTTP_503_SERVICE_UNAVAILABLE

from app.agents.orchestrator import AgentOrchestrationError, agent_orchestrator
from app.core.constants import ResponseStatus, UserRole
from app.core.exceptions import AppError
from app.core.request_context import get_request_id
from app.core.response import ResponseBuilder
from app.core.security import get_current_role
from app.db.session import get_db_session
from app.schemas.phase10 import AgentQueryRequest

router = APIRouter(prefix="/api/agent", tags=["LangGraph Agent"])


@router.get("/status", summary="Check the LangGraph agent router and supported routes")
def agent_status(request: Request):
    return ResponseBuilder.success(
        request,
        answer="Phase 13 LangGraph routing, short-term session memory, and restricted admin schema workflow status retrieved.",
        data=agent_orchestrator.status(),
    )


@router.post("/query", summary="Route one natural-language request through the controlled LangGraph workflow")
def agent_query(
    payload: AgentQueryRequest,
    request: Request,
    db: Session = Depends(get_db_session),
    role: UserRole = Depends(get_current_role),
):
    try:
        result = agent_orchestrator.run(
            question=payload.question,
            db=db,
            request_id=get_request_id(request),
            session_id=payload.session_id,
            user_role=role,
            top_k=payload.top_k,
        )
    except AgentOrchestrationError as exc:
        if exc.status == ResponseStatus.INFORMATION_NOT_AVAILABLE:
            http_status = HTTP_404_NOT_FOUND
        elif exc.status in {ResponseStatus.LLM_UNAVAILABLE, ResponseStatus.DATABASE_UNAVAILABLE, ResponseStatus.RETRIEVAL_FAILED, ResponseStatus.TOOL_FAILED}:
            http_status = HTTP_503_SERVICE_UNAVAILABLE
        else:
            http_status = HTTP_400_BAD_REQUEST
        raise AppError(
            status=exc.status,
            code=exc.code,
            message=exc.message,
            http_status_code=http_status,
            route=getattr(exc, "route", None) or result_route_from_error(exc),
            details=exc.details,
        ) from exc

    return ResponseBuilder.success(
        request,
        route=result.route,
        status=result.status,
        answer=result.answer,
        data=result.data,
        sources=result.sources,
        generated_sql=result.generated_sql,
        pending_action_id=result.pending_action_id,
    )


def result_route_from_error(_: AgentOrchestrationError):
    """A safe default route for failures before the router returns a result."""

    from app.core.constants import AgentRoute

    return AgentRoute.SYSTEM
