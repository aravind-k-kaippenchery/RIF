"""Application exceptions and global FastAPI exception handlers."""

from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.status import HTTP_422_UNPROCESSABLE_CONTENT, HTTP_500_INTERNAL_SERVER_ERROR

from app.core.constants import AgentRoute, ResponseStatus
from app.core.logging_config import log_event
from app.core.request_context import get_request_id, get_session_id
from app.core.response import ResponseBuilder


class AppError(Exception):
    """Expected, safe application error with an API status and HTTP status."""

    def __init__(
        self,
        *,
        status: ResponseStatus,
        code: str,
        message: str,
        http_status_code: int,
        route: AgentRoute = AgentRoute.SYSTEM,
        details: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.http_status_code = http_status_code
        self.route = route
        self.details = details
        super().__init__(message)


def _validation_details(exc: RequestValidationError) -> list[dict[str, Any]]:
    """Expose useful input-validation details without exposing internal data."""

    return [
        {
            "location": ".".join(str(part) for part in error.get("loc", [])),
            "message": error.get("msg", "Invalid input."),
            "type": error.get("type", "validation_error"),
        }
        for error in exc.errors()
    ]


def register_exception_handlers(app: FastAPI) -> None:
    """Register one centralized error behavior for the entire API."""

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        log_event(
            level="WARNING",
            event="application_error",
            request_id=get_request_id(request),
            session_id=get_session_id(request),
            method=request.method,
            path=request.url.path,
            status=exc.status.value,
            error_code=exc.code,
            error_message=exc.message,
        )
        return ResponseBuilder.error(
            request,
            status=exc.status,
            code=exc.code,
            message=exc.message,
            http_status_code=exc.http_status_code,
            route=exc.route,
            details=exc.details,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = _validation_details(exc)
        log_event(
            level="WARNING",
            event="request_validation_failed",
            request_id=get_request_id(request),
            session_id=get_session_id(request),
            method=request.method,
            path=request.url.path,
            status=ResponseStatus.VALIDATION_FAILED.value,
            error_code="request_validation_failed",
        )
        return ResponseBuilder.error(
            request,
            status=ResponseStatus.VALIDATION_FAILED,
            code="request_validation_failed",
            message="The request contains invalid or missing data.",
            http_status_code=HTTP_422_UNPROCESSABLE_CONTENT,
            details=details,
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        detail: Any = exc.detail
        message = detail if isinstance(detail, str) else "The request could not be completed."
        log_event(
            level="WARNING",
            event="http_exception",
            request_id=get_request_id(request),
            session_id=get_session_id(request),
            method=request.method,
            path=request.url.path,
            status=ResponseStatus.VALIDATION_FAILED.value,
            error_code="http_error",
            error_message=message,
        )
        return ResponseBuilder.error(
            request,
            status=ResponseStatus.VALIDATION_FAILED,
            code="http_error",
            message=message,
            http_status_code=exc.status_code,
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        # The actual exception text is intentionally written only to the local log,
        # never returned to a frontend user.
        log_event(
            level="ERROR",
            event="unhandled_exception",
            request_id=get_request_id(request),
            session_id=get_session_id(request),
            method=request.method,
            path=request.url.path,
            status=ResponseStatus.TOOL_FAILED.value,
            error_code="internal_server_error",
            error_message=str(exc),
        )
        return ResponseBuilder.error(
            request,
            status=ResponseStatus.TOOL_FAILED,
            code="internal_server_error",
            message="An unexpected server error occurred. Check the local log using the request ID.",
            http_status_code=HTTP_500_INTERNAL_SERVER_ERROR,
        )
