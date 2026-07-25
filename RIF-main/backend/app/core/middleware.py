"""Middleware that creates traceable request and temporary session context."""

import time
from uuid import uuid4

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app.core.constants import REQUEST_ID_HEADER, SESSION_ID_HEADER
from app.core.logging_config import log_event


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach IDs to a request, response header, and JSON request log."""

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER, "").strip() or str(uuid4())
        session_id = request.headers.get(SESSION_ID_HEADER, "").strip() or None

        # Avoid accepting unreasonably long untrusted header values.
        request.state.request_id = request_id[:128]
        request.state.session_id = session_id[:128] if session_id else None

        started_at = time.perf_counter()
        response: Response
        try:
            response = await call_next(request)
        except Exception:
            # The global exception handler returns the safe JSON body.
            # Re-raise so FastAPI can call it.
            raise
        finally:
            latency_ms = round((time.perf_counter() - started_at) * 1000, 2)
            # Response status is unavailable only when an exception was re-raised.
            response_status = locals().get("response").status_code if "response" in locals() else 500
            log_event(
                level="INFO",
                event="request_completed",
                request_id=request.state.request_id,
                session_id=request.state.session_id,
                method=request.method,
                path=request.url.path,
                status="success" if response_status < 400 else "error",
                status_code=response_status,
                route="system",
                user_prompt=None,
                latency_ms=latency_ms,
            )

        response.headers[REQUEST_ID_HEADER] = request.state.request_id
        if request.state.session_id:
            response.headers[SESSION_ID_HEADER] = request.state.session_id
        return response
