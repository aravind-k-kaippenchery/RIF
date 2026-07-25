"""Temporary Phase 1 role guard. It is deliberately simple and not authentication."""

from collections.abc import Callable

from fastapi import Depends, Request
from starlette.status import HTTP_403_FORBIDDEN

from app.core.constants import USER_ROLE_HEADER, ResponseStatus, UserRole
from app.core.exceptions import AppError


def get_current_role(request: Request) -> UserRole:
    """Read a safe temporary role from a request header; defaults to normal_user."""

    supplied_role = request.headers.get(USER_ROLE_HEADER, UserRole.NORMAL_USER.value).strip().lower()
    try:
        return UserRole(supplied_role)
    except ValueError as exc:
        allowed_roles = ", ".join(role.value for role in UserRole)
        raise AppError(
            status=ResponseStatus.VALIDATION_FAILED,
            code="invalid_user_role",
            message=f"Invalid X-User-Role header. Allowed roles: {allowed_roles}.",
            http_status_code=HTTP_403_FORBIDDEN,
        ) from exc


def require_role(required_role: UserRole) -> Callable:
    """Return a FastAPI dependency that permits only a specified temporary role."""

    def dependency(current_role: UserRole = Depends(get_current_role)) -> UserRole:
        if current_role != required_role:
            raise AppError(
                status=ResponseStatus.VALIDATION_FAILED,
                code="insufficient_role",
                message=f"This endpoint requires the '{required_role.value}' role.",
                http_status_code=HTTP_403_FORBIDDEN,
            )
        return current_role

    return dependency
