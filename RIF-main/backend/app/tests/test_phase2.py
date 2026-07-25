"""Database-structure tests that do not need a running PostgreSQL service."""

from sqlalchemy import inspect

from app.core.config import Settings
from app.models import Base, EmployeePermission


def test_database_url_uses_psycopg_driver_and_encodes_password_safely():
    settings = Settings(
        postgres_host="127.0.0.1",
        postgres_port=5432,
        postgres_db="b2b_assistant",
        postgres_user="b2b_app",
        postgres_password="safe password with spaces",
    )

    database_url = settings.database_url
    assert database_url.drivername == "postgresql+psycopg"
    assert database_url.host == "127.0.0.1"
    assert database_url.port == 5432
    assert database_url.database == "b2b_assistant"
    assert str(database_url).startswith("postgresql+psycopg://b2b_app:***@")


def test_phase_two_metadata_contains_all_required_tables():
    expected_tables = {
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
    }
    assert expected_tables.issubset(set(Base.metadata.tables))


def test_feature_17_foreign_key_is_restrict_and_duplicate_permission_is_prevented():
    table = EmployeePermission.__table__
    foreign_key = next(iter(table.foreign_keys))

    assert foreign_key.target_fullname == "employees.id"
    assert foreign_key.ondelete == "RESTRICT"
    unique_constraints = [constraint for constraint in table.constraints if constraint.__class__.__name__ == "UniqueConstraint"]
    assert any({column.name for column in constraint.columns} == {"employee_id", "permission_code"} for constraint in unique_constraints)
