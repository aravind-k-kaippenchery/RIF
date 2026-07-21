"""Add employee work-history child table and relationship indexes.

Revision ID: 20260721_0002
Revises: 20260623_0001
Create Date: 2026-07-21
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260721_0002"
down_revision: Union[str, Sequence[str], None] = "20260623_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "employee_experiences",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("company_name", sa.String(length=200), nullable=False),
        sa.Column("job_title", sa.String(length=160), nullable=False),
        sa.Column("employment_type", sa.String(length=50), server_default="full_time", nullable=False),
        sa.Column("location", sa.String(length=160), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_current", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "employee_id",
            "company_name",
            "job_title",
            "start_date",
            name="uq_employee_experiences_employee_company_role_start",
        ),
        sa.CheckConstraint(
            "end_date IS NULL OR end_date >= start_date",
            name="ck_employee_experiences_end_after_start",
        ),
        sa.CheckConstraint(
            "is_current = false OR end_date IS NULL",
            name="ck_employee_experiences_current_has_no_end_date",
        ),
    )
    op.create_index("ix_employee_experiences_employee_id", "employee_experiences", ["employee_id"])
    op.create_index("ix_employee_experiences_company_name", "employee_experiences", ["company_name"])
    op.create_index("ix_employee_experiences_job_title", "employee_experiences", ["job_title"])
    op.create_index("ix_employee_experiences_location", "employee_experiences", ["location"])
    op.create_index("ix_employee_experiences_start_date", "employee_experiences", ["start_date"])
    op.create_index("ix_employee_experiences_end_date", "employee_experiences", ["end_date"])


def downgrade() -> None:
    op.drop_index("ix_employee_experiences_end_date", table_name="employee_experiences")
    op.drop_index("ix_employee_experiences_start_date", table_name="employee_experiences")
    op.drop_index("ix_employee_experiences_location", table_name="employee_experiences")
    op.drop_index("ix_employee_experiences_job_title", table_name="employee_experiences")
    op.drop_index("ix_employee_experiences_company_name", table_name="employee_experiences")
    op.drop_index("ix_employee_experiences_employee_id", table_name="employee_experiences")
    op.drop_table("employee_experiences")
