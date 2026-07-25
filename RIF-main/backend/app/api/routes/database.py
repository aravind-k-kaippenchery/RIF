"""Read-only Phase 2 database verification endpoints."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.status import HTTP_404_NOT_FOUND, HTTP_503_SERVICE_UNAVAILABLE

from app.core.constants import ResponseStatus
from app.core.exceptions import AppError
from app.core.response import ResponseBuilder
from app.db.health import get_database_health
from app.db.session import get_db_session
from app.models import Employee, EmployeePermission
from app.services.database_inspection import get_table_names, get_table_summary

router = APIRouter(prefix="/api/database", tags=["Database verification"])


def _database_unavailable_error() -> AppError:
    health = get_database_health()
    return AppError(
        status=ResponseStatus.DATABASE_UNAVAILABLE,
        code="database_unavailable",
        message=f"PostgreSQL is unavailable. {health.message}",
        http_status_code=HTTP_503_SERVICE_UNAVAILABLE,
    )


@router.get("/health", summary="Check the PostgreSQL connection")
def database_health(request: Request):
    health = get_database_health()
    return ResponseBuilder.success(
        request,
        answer="PostgreSQL connection check completed.",
        data=health.model_dump(),
    )


@router.get("/tables", summary="List Phase 2 PostgreSQL application tables")
def list_database_tables(request: Request, db: Session = Depends(get_db_session)):
    try:
        tables = get_table_names(db)
    except SQLAlchemyError as exc:
        raise _database_unavailable_error() from exc
    return ResponseBuilder.success(
        request,
        answer="Application database tables retrieved.",
        data={"table_count": len(tables), "tables": tables},
    )


@router.get("/summary", summary="View seeded business data counts")
def database_summary(request: Request, db: Session = Depends(get_db_session)):
    try:
        summary = get_table_summary(db)
    except SQLAlchemyError as exc:
        raise _database_unavailable_error() from exc
    return ResponseBuilder.success(
        request,
        answer="Phase 2 database summary retrieved.",
        data=summary,
    )


@router.get(
    "/employees/{employee_code}/permissions",
    summary="Verify Feature 17 child records for one employee",
)
def employee_permissions(
    employee_code: str,
    request: Request,
    db: Session = Depends(get_db_session),
):
    try:
        employee = db.scalar(select(Employee).where(Employee.employee_code == employee_code.upper()))
    except SQLAlchemyError as exc:
        raise _database_unavailable_error() from exc

    if employee is None:
        raise AppError(
            status=ResponseStatus.INFORMATION_NOT_AVAILABLE,
            code="employee_not_found",
            message=f"No employee exists with employee_code '{employee_code.upper()}'.",
            http_status_code=HTTP_404_NOT_FOUND,
        )

    permissions = [
        {
            "permission_code": permission.permission_code,
            "description": permission.description,
            "is_active": permission.is_active,
        }
        for permission in sorted(employee.permissions, key=lambda item: item.permission_code)
    ]
    return ResponseBuilder.success(
        request,
        answer=(
            f"Employee {employee.employee_code} has {len(permissions)} permission record(s). "
            "This verifies the one-to-zero-or-many parent/child relationship."
        ),
        data={
            "employee": {
                "employee_code": employee.employee_code,
                "full_name": employee.full_name,
                "department": employee.department,
                "city": employee.city,
            },
            "permission_count": len(permissions),
            "permissions": permissions,
        },
    )
