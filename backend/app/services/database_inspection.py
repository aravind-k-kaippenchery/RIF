"""Read-only database inspection helpers for Phase 2 verification APIs."""

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from app.models import (
    ActionLog,
    BenchmarkRun,
    ChangeSnapshot,
    Customer,
    Document,
    DocumentIngestionJob,
    Employee,
    EmployeePermission,
    PendingAction,
    Product,
    ProductVendorMapping,
    QueryLog,
    SalesDeal,
    SchemaChangeRequest,
    Session as ConversationSession,
    Vendor,
)


APPLICATION_TABLES = [
    "employees",
    "employee_permissions",
    "vendors",
    "customers",
    "products",
    "product_vendor_mappings",
    "sales_deals",
    "sessions",
    "pending_actions",
    "query_logs",
    "action_logs",
    "change_snapshots",
    "documents",
    "document_ingestion_jobs",
    "benchmark_runs",
    "schema_change_requests",
]


def get_table_names(session: Session) -> list[str]:
    """Return approved application tables, not PostgreSQL system tables."""

    existing = set(inspect(session.bind).get_table_names(schema="public"))
    return [table_name for table_name in APPLICATION_TABLES if table_name in existing]


def get_table_summary(session: Session) -> dict:
    """Return readable counts that prove migrations and seed data worked."""

    models = {
        "employees": Employee,
        "employee_permissions": EmployeePermission,
        "vendors": Vendor,
        "customers": Customer,
        "products": Product,
        "product_vendor_mappings": ProductVendorMapping,
        "sales_deals": SalesDeal,
        "sessions": ConversationSession,
        "pending_actions": PendingAction,
        "query_logs": QueryLog,
        "action_logs": ActionLog,
        "change_snapshots": ChangeSnapshot,
        "documents": Document,
        "document_ingestion_jobs": DocumentIngestionJob,
        "benchmark_runs": BenchmarkRun,
        "schema_change_requests": SchemaChangeRequest,
    }
    counts = {name: session.scalar(select(__import__("sqlalchemy").func.count()).select_from(model)) for name, model in models.items()}

    employee_permissions = {
        employee.employee_code: len(employee.permissions)
        for employee in session.scalars(
            select(Employee).where(Employee.employee_code.in_(["EMP-101", "EMP-102", "EMP-103"]))
        ).all()
    }
    return {
        "record_counts": counts,
        "feature_17_verification": {
            "relationship": "employees → employee_permissions",
            "EMP-101_expected_permissions": 0,
            "EMP-102_expected_permissions": 1,
            "EMP-103_expected_permissions": 3,
            "actual_permissions_by_employee_code": employee_permissions,
        },
    }
