"""Factories that ensure every success or error has one consistent response envelope."""

from typing import Any, Optional

from fastapi import Request
from fastapi.responses import JSONResponse

from app.core.constants import AgentRoute, ResponseStatus
from app.core.request_context import get_request_id, get_session_id
from app.core.schemas import ApiError, ApiResponse, SourceCitation


class ResponseBuilder:
    """Create frontend-safe responses without repeating envelope logic."""

    @staticmethod
    def success(
        request: Request,
        *,
        answer: str,
        data: Any = None,
        route: AgentRoute = AgentRoute.SYSTEM,
        status: ResponseStatus = ResponseStatus.SUCCESS,
        sources: Optional[list[SourceCitation]] = None,
        generated_sql: Optional[str] = None,
        pending_action_id: Optional[str] = None,
        http_status_code: int = 200,
    ) -> JSONResponse:
        payload = ApiResponse(
            request_id=get_request_id(request),
            session_id=get_session_id(request),
            status=status,
            route=route,
            answer=answer,
            data=data,
            sources=sources or [],
            generated_sql=generated_sql,
            pending_action_id=pending_action_id,
            error=None,
        )
        return JSONResponse(status_code=http_status_code, content=payload.model_dump(mode="json"))

    @staticmethod
    def error(
        request: Request,
        *,
        status: ResponseStatus,
        code: str,
        message: str,
        http_status_code: int,
        route: AgentRoute = AgentRoute.SYSTEM,
        details: Optional[list[dict[str, Any]]] = None,
    ) -> JSONResponse:
        payload = ApiResponse(
            request_id=get_request_id(request),
            session_id=get_session_id(request),
            status=status,
            route=route,
            answer=None,
            data=None,
            sources=[],
            generated_sql=None,
            pending_action_id=None,
            error=ApiError(code=code, message=message, details=details),
        )
        return JSONResponse(status_code=http_status_code, content=payload.model_dump(mode="json"))
