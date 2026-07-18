"""Small helpers for reading request-scoped IDs created by middleware."""

from typing import Optional

from fastapi import Request


def get_request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "unknown-request")


def get_session_id(request: Request) -> Optional[str]:
    return getattr(request.state, "session_id", None)
