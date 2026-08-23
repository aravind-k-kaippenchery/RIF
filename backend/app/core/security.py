"""Local demo role selection and role authorization."""

from collections.abc import Callable
from fastapi import Depends, Request
from starlette.status import HTTP_403_FORBIDDEN

from app.core.constants import USER_ROLE_HEADER, ResponseStatus, UserRole
from app.core.exceptions import AppError


def get_current_role(request: Request) -> UserRole:
    """Resolve the locally selected role; ordinary requests default to normal_user."""

    supplied_role = request.headers.get(USER_ROLE_HEADER, UserRole.NORMAL_USER.value).strip().lower()
    try:
        requested_role = UserRole(supplied_role)
    except ValueError as exc:
        allowed_roles = ", ".join(role.value for role in UserRole)
        raise AppError(
            status=ResponseStatus.VALIDATION_FAILED,
            code="invalid_user_role",
            message=f"Invalid X-User-Role header. Allowed roles: {allowed_roles}.",
            http_status_code=HTTP_403_FORBIDDEN,
        ) from exc

    return requested_role


def require_role(required_role: UserRole) -> Callable:
    """Return a FastAPI dependency that permits only the selected role."""

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
