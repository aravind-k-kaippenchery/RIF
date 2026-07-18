"""Create the Phase 2 PostgreSQL business and operational data layer.

Revision ID: 20260623_0001
Revises:
Create Date: 2026-06-23
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20260623_0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _timestamp_columns() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "employees",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("employee_code", sa.String(length=32), nullable=False),
        sa.Column("first_name", sa.String(length=100), nullable=False),
        sa.Column("last_name", sa.String(length=100), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("department", sa.String(length=100), nullable=False),
        sa.Column("city", sa.String(length=100), nullable=False),
        sa.Column("company_name", sa.String(length=150), nullable=False),
        sa.Column("salary", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("employment_status", sa.String(length=32), server_default="active", nullable=False),
        *_timestamp_columns(),
        sa.CheckConstraint("salary >= 0", name="ck_employees_salary_nonnegative"),
        sa.UniqueConstraint("employee_code", name="uq_employees_employee_code"),
        sa.UniqueConstraint("email", name="uq_employees_email"),
        sa.UniqueConstraint("phone", name="uq_employees_phone"),
    )
    op.create_index("ix_employees_city", "employees", ["city"])
    op.create_index("ix_employees_department", "employees", ["department"])
    op.create_index("ix_employees_company_name", "employees", ["company_name"])

    op.create_table(
        "vendors",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("vendor_code", sa.String(length=32), nullable=False),
        sa.Column("vendor_name", sa.String(length=200), nullable=False),
        sa.Column("contact_email", sa.String(length=255), nullable=False),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("city", sa.String(length=100), nullable=False),
        sa.Column("country", sa.String(length=100), server_default="India", nullable=False),
        sa.Column("category", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="active", nullable=False),
        *_timestamp_columns(),
        sa.UniqueConstraint("vendor_code", name="uq_vendors_vendor_code"),
        sa.UniqueConstraint("vendor_name", name="uq_vendors_vendor_name"),
        sa.UniqueConstraint("contact_email", name="uq_vendors_contact_email"),
        sa.UniqueConstraint("phone", name="uq_vendors_phone"),
    )
    op.create_index("ix_vendors_city", "vendors", ["city"])
    op.create_index("ix_vendors_category", "vendors", ["category"])

    op.create_table(
        "customers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("customer_code", sa.String(length=32), nullable=False),
        sa.Column("customer_name", sa.String(length=200), nullable=False),
        sa.Column("contact_email", sa.String(length=255), nullable=False),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("city", sa.String(length=100), nullable=False),
        sa.Column("country", sa.String(length=100), server_default="India", nullable=False),
        sa.Column("industry", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="active", nullable=False),
        *_timestamp_columns(),
        sa.UniqueConstraint("customer_code", name="uq_customers_customer_code"),
        sa.UniqueConstraint("customer_name", name="uq_customers_customer_name"),
        sa.UniqueConstraint("contact_email", name="uq_customers_contact_email"),
        sa.UniqueConstraint("phone", name="uq_customers_phone"),
    )
    op.create_index("ix_customers_city", "customers", ["city"])
    op.create_index("ix_customers_industry", "customers", ["industry"])

    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product_code", sa.String(length=32), nullable=False),
        sa.Column("product_name", sa.String(length=200), nullable=False),
        sa.Column("category", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("list_price", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        *_timestamp_columns(),
        sa.CheckConstraint("list_price >= 0", name="ck_products_list_price_nonnegative"),
        sa.UniqueConstraint("product_code", name="uq_products_product_code"),
        sa.UniqueConstraint("product_name", name="uq_products_product_name"),
    )
    op.create_index("ix_products_category", "products", ["category"])

    op.create_table(
        "sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_role", sa.String(length=32), server_default="normal_user", nullable=False),
        sa.Column("status", sa.String(length=32), server_default="active", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamp_columns(),
    )

    # Feature 17: required zero/one/many child table with database-level delete protection.
    op.create_table(
        "employee_permissions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("permission_code", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=300), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        *_timestamp_columns(),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("employee_id", "permission_code", name="uq_employee_permissions_employee_permission"),
    )
    op.create_index("ix_employee_permissions_employee_id", "employee_permissions", ["employee_id"])

    op.create_table(
        "product_vendor_mappings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("vendor_id", sa.Integer(), nullable=False),
        sa.Column("vendor_sku", sa.String(length=100), nullable=True),
        sa.Column("quoted_price", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("is_preferred", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        *_timestamp_columns(),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["vendor_id"], ["vendors.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("quoted_price >= 0", name="ck_product_vendor_mappings_quoted_price_nonnegative"),
        sa.UniqueConstraint("product_id", "vendor_id", name="uq_product_vendor_mappings_product_vendor"),
    )
    op.create_index("ix_product_vendor_mappings_product_id", "product_vendor_mappings", ["product_id"])
    op.create_index("ix_product_vendor_mappings_vendor_id", "product_vendor_mappings", ["vendor_id"])

    op.create_table(
        "sales_deals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("deal_code", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=250), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("owner_employee_id", sa.Integer(), nullable=True),
        sa.Column("amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("stage", sa.String(length=50), nullable=False),
        sa.Column("probability", sa.Integer(), nullable=False),
        sa.Column("expected_close_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="open", nullable=False),
        *_timestamp_columns(),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["owner_employee_id"], ["employees.id"], ondelete="SET NULL"),
        sa.CheckConstraint("amount >= 0", name="ck_sales_deals_amount_nonnegative"),
        sa.CheckConstraint("probability >= 0 AND probability <= 100", name="ck_sales_deals_probability_range"),
        sa.UniqueConstraint("deal_code", name="uq_sales_deals_deal_code"),
    )
    op.create_index("ix_sales_deals_customer_id", "sales_deals", ["customer_id"])
    op.create_index("ix_sales_deals_product_id", "sales_deals", ["product_id"])
    op.create_index("ix_sales_deals_owner_employee_id", "sales_deals", ["owner_employee_id"])
    op.create_index("ix_sales_deals_stage", "sales_deals", ["stage"])
    op.create_index("ix_sales_deals_expected_close_date", "sales_deals", ["expected_close_date"])

    op.create_table(
        "pending_actions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action_type", sa.String(length=64), nullable=False),
        sa.Column("target_table", sa.String(length=128), nullable=True),
        sa.Column("validated_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("preview_data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("generated_sql", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamp_columns(),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_pending_actions_session_id", "pending_actions", ["session_id"])
    op.create_index("ix_pending_actions_status", "pending_actions", ["status"])
    op.create_index("ix_pending_actions_expires_at", "pending_actions", ["expires_at"])

    op.create_table(
        "query_logs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("user_prompt", sa.Text(), nullable=True),
        sa.Column("detected_route", sa.String(length=64), nullable=True),
        sa.Column("generated_sql", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("source_references", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("request_id", name="uq_query_logs_request_id"),
    )
    op.create_index("ix_query_logs_session_id", "query_logs", ["session_id"])
    op.create_index("ix_query_logs_detected_route", "query_logs", ["detected_route"])
    op.create_index("ix_query_logs_status", "query_logs", ["status"])

    op.create_table(
        "action_logs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("pending_action_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_role", sa.String(length=32), nullable=False),
        sa.Column("action_type", sa.String(length=64), nullable=False),
        sa.Column("target_table", sa.String(length=128), nullable=True),
        sa.Column("affected_record_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("generated_sql", sa.Text(), nullable=True),
        sa.Column("confirmation_status", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["pending_action_id"], ["pending_actions.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("request_id", name="uq_action_logs_request_id"),
    )
    op.create_index("ix_action_logs_session_id", "action_logs", ["session_id"])
    op.create_index("ix_action_logs_pending_action_id", "action_logs", ["pending_action_id"])
    op.create_index("ix_action_logs_status", "action_logs", ["status"])

    op.create_table(
        "change_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("action_log_id", sa.BigInteger(), nullable=False),
        sa.Column("table_name", sa.String(length=128), nullable=False),
        sa.Column("record_id", sa.String(length=128), nullable=False),
        sa.Column("snapshot_type", sa.String(length=16), nullable=False),
        sa.Column("snapshot_data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["action_log_id"], ["action_logs.id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_change_snapshots_action_log_id", "change_snapshots", ["action_log_id"])

    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("original_filename", sa.String(length=500), nullable=False),
        sa.Column("file_hash", sa.String(length=128), nullable=False),
        sa.Column("file_type", sa.String(length=32), nullable=False),
        sa.Column("file_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("ocr_used", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("ingestion_status", sa.String(length=64), server_default="pending", nullable=False),
        sa.Column("extracted_text_path", sa.String(length=500), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("document_version", sa.Integer(), server_default="1", nullable=False),
        *_timestamp_columns(),
        sa.UniqueConstraint("file_hash", name="uq_documents_file_hash"),
    )
    op.create_index("ix_documents_ingestion_status", "documents", ["ingestion_status"])

    op.create_table(
        "document_ingestion_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=64), server_default="queued", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ocr_duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_document_ingestion_jobs_document_id", "document_ingestion_jobs", ["document_id"])
    op.create_index("ix_document_ingestion_jobs_status", "document_ingestion_jobs", ["status"])

    op.create_table(
        "benchmark_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("test_name", sa.String(length=200), nullable=False),
        sa.Column("route", sa.String(length=64), nullable=True),
        sa.Column("metric_type", sa.String(length=64), nullable=False),
        sa.Column("metric_value", sa.Float(), nullable=True),
        sa.Column("metric_unit", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_benchmark_runs_route", "benchmark_runs", ["route"])
    op.create_index("ix_benchmark_runs_metric_type", "benchmark_runs", ["metric_type"])
    op.create_index("ix_benchmark_runs_status", "benchmark_runs", ["status"])
    op.create_index("ix_benchmark_runs_started_at", "benchmark_runs", ["started_at"])

    op.create_table(
        "schema_change_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("requested_by_role", sa.String(length=32), nullable=False),
        sa.Column("user_prompt", sa.Text(), nullable=False),
        sa.Column("proposed_sql", sa.Text(), nullable=True),
        sa.Column("schema_preview", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.String(length=64), server_default="pending", nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        *_timestamp_columns(),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_schema_change_requests_session_id", "schema_change_requests", ["session_id"])
    op.create_index("ix_schema_change_requests_status", "schema_change_requests", ["status"])


def downgrade() -> None:
    # Reverse order protects foreign-key dependencies.
    op.drop_index("ix_schema_change_requests_status", table_name="schema_change_requests")
    op.drop_index("ix_schema_change_requests_session_id", table_name="schema_change_requests")
    op.drop_table("schema_change_requests")

    op.drop_index("ix_benchmark_runs_started_at", table_name="benchmark_runs")
    op.drop_index("ix_benchmark_runs_status", table_name="benchmark_runs")
    op.drop_index("ix_benchmark_runs_metric_type", table_name="benchmark_runs")
    op.drop_index("ix_benchmark_runs_route", table_name="benchmark_runs")
    op.drop_table("benchmark_runs")

    op.drop_index("ix_document_ingestion_jobs_status", table_name="document_ingestion_jobs")
    op.drop_index("ix_document_ingestion_jobs_document_id", table_name="document_ingestion_jobs")
    op.drop_table("document_ingestion_jobs")

    op.drop_index("ix_documents_ingestion_status", table_name="documents")
    op.drop_table("documents")

    op.drop_index("ix_change_snapshots_action_log_id", table_name="change_snapshots")
    op.drop_table("change_snapshots")

    op.drop_index("ix_action_logs_status", table_name="action_logs")
    op.drop_index("ix_action_logs_pending_action_id", table_name="action_logs")
    op.drop_index("ix_action_logs_session_id", table_name="action_logs")
    op.drop_table("action_logs")

    op.drop_index("ix_query_logs_status", table_name="query_logs")
    op.drop_index("ix_query_logs_detected_route", table_name="query_logs")
    op.drop_index("ix_query_logs_session_id", table_name="query_logs")
    op.drop_table("query_logs")

    op.drop_index("ix_pending_actions_expires_at", table_name="pending_actions")
    op.drop_index("ix_pending_actions_status", table_name="pending_actions")
    op.drop_index("ix_pending_actions_session_id", table_name="pending_actions")
    op.drop_table("pending_actions")

    op.drop_index("ix_sales_deals_expected_close_date", table_name="sales_deals")
    op.drop_index("ix_sales_deals_stage", table_name="sales_deals")
    op.drop_index("ix_sales_deals_owner_employee_id", table_name="sales_deals")
    op.drop_index("ix_sales_deals_product_id", table_name="sales_deals")
    op.drop_index("ix_sales_deals_customer_id", table_name="sales_deals")
    op.drop_table("sales_deals")

    op.drop_index("ix_product_vendor_mappings_vendor_id", table_name="product_vendor_mappings")
    op.drop_index("ix_product_vendor_mappings_product_id", table_name="product_vendor_mappings")
    op.drop_table("product_vendor_mappings")

    op.drop_index("ix_employee_permissions_employee_id", table_name="employee_permissions")
    op.drop_table("employee_permissions")

    op.drop_table("sessions")

    op.drop_index("ix_products_category", table_name="products")
    op.drop_table("products")

    op.drop_index("ix_customers_industry", table_name="customers")
    op.drop_index("ix_customers_city", table_name="customers")
    op.drop_table("customers")

    op.drop_index("ix_vendors_category", table_name="vendors")
    op.drop_index("ix_vendors_city", table_name="vendors")
    op.drop_table("vendors")

    op.drop_index("ix_employees_company_name", table_name="employees")
    op.drop_index("ix_employees_department", table_name="employees")
    op.drop_index("ix_employees_city", table_name="employees")
    op.drop_table("employees")
