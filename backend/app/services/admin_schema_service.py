"""Phase 13 restricted admin schema-confirmation workflow.

This is intentionally narrow: only a previously stored, AST-validated ``CREATE TABLE``
or ``ALTER TABLE ADD COLUMN`` preview can execute.  The tool accepts no raw DDL at apply
time and records the operation in both ``schema_change_requests`` and ``action_logs``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session as DbSession

from app.core.constants import UserRole
from app.models.operations import ActionLog, SchemaChangeRequest
from app.services.session_service import get_active_session
from app.services.sql_validation import SQLValidationResult, preview_admin_schema_change


class AdminSchemaExecutionError(RuntimeError):
    """Safe, structured schema workflow error."""

    def __init__(self, *, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def schema_change_to_dict(change: SchemaChangeRequest) -> dict[str, Any]:
    return {
        "schema_change_id": str(change.id),
        "session_id": str(change.session_id) if change.session_id else None,
        "requested_by_role": change.requested_by_role,
        "user_prompt": change.user_prompt,
        "proposed_sql": change.proposed_sql,
        "schema_preview": change.schema_preview,
        "status": change.status,
        "confirmed_at": change.confirmed_at.isoformat() if change.confirmed_at else None,
        "executed_at": change.executed_at.isoformat() if change.executed_at else None,
        "error_message": change.error_message,
        "created_at": change.created_at.isoformat() if change.created_at else None,
        "updated_at": change.updated_at.isoformat() if change.updated_at else None,
    }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _already_applied(db: DbSession, validation: SQLValidationResult) -> bool:
    """Detect safe idempotent replays before executing restricted DDL again."""

    inspector = inspect(db.bind)
    statement_type = validation.statement_type or ""
    if statement_type == "CREATE_TABLE_PREVIEW":
        return bool(validation.tables and validation.tables[0] in set(inspector.get_table_names(schema="public")))
    if statement_type == "ALTER_TABLE_ADD_COLUMN_PREVIEW":
        if not validation.tables or not validation.columns:
            return False
        table_name = validation.tables[0]
        column_name = validation.columns[0].split(".")[-1]
        if table_name not in set(inspector.get_table_names(schema="public")):
            return False
        columns = {column["name"].lower() for column in inspector.get_columns(table_name, schema="public")}
        return column_name.lower() in columns
    return False


def _record_action_log(
    db: DbSession,
    *,
    request_id: str,
    change: SchemaChangeRequest,
    validation: SQLValidationResult,
    status: str,
    error_message: str | None = None,
) -> int:
    action = ActionLog(
        request_id=request_id,
        session_id=change.session_id,
        pending_action_id=None,
        actor_role=UserRole.ADMIN.value,
        action_type="admin_schema_change",
        target_table=(validation.tables or [None])[0],
        affected_record_ids={
            "schema_change_id": str(change.id),
            "tables": validation.tables,
            "columns": validation.columns,
        },
        generated_sql=validation.normalized_sql or change.proposed_sql,
        confirmation_status="confirmed",
        status=status,
        error_message=error_message,
        created_at=_utc_now(),
    )
    db.add(action)
    db.flush()
    return int(action.id)


def execute_confirmed_schema_change(
    db: DbSession,
    *,
    schema_change_id: UUID,
    actor_role: UserRole,
    confirmed: bool,
    request_id: str | None,
    session_id: UUID | None = None,
) -> dict[str, Any]:
    """Execute one stored restricted schema preview after an explicit admin confirmation."""

    if actor_role != UserRole.ADMIN:
        raise AdminSchemaExecutionError(code="admin_role_required", message="Only an admin role can execute a restricted schema-change request.")
    if not confirmed:
        raise AdminSchemaExecutionError(code="explicit_confirmation_required", message="confirmed=true is required before an admin schema-change request can execute.")

    change = db.get(SchemaChangeRequest, schema_change_id)
    if change is None:
        raise AdminSchemaExecutionError(code="schema_change_not_found", message="No stored schema-change preview exists with this ID.")
    if change.requested_by_role != UserRole.ADMIN.value:
        raise AdminSchemaExecutionError(code="schema_change_role_mismatch", message="The stored schema-change request was not created by an admin role.")
    if session_id is not None:
        if change.session_id is not None and change.session_id != session_id:
            raise AdminSchemaExecutionError(code="schema_change_session_mismatch", message="The supplied session does not match the stored schema-change request.")
        if get_active_session(db, session_id) is None:
            raise AdminSchemaExecutionError(code="inactive_or_missing_session", message="The supplied session is missing, expired, or inactive.")

    validation = preview_admin_schema_change(change.proposed_sql or "")
    if not validation.is_valid or validation.statement_type not in {"CREATE_TABLE_PREVIEW", "ALTER_TABLE_ADD_COLUMN_PREVIEW"}:
        change.status = "failed_validation"
        change.error_message = validation.error_message or "The stored schema change is no longer valid."
        db.commit()
        raise AdminSchemaExecutionError(code=validation.error_code or "schema_change_validation_failed", message=change.error_message)

    if change.status == "executed" or change.executed_at is not None:
        return {
            "executed": False,
            "idempotent": True,
            "schema_change": schema_change_to_dict(change),
            "action_log_id": None,
            "execution_mode": "restricted_admin_confirmed_transaction",
            "raw_sql_accepted": False,
        }

    safe_request_id = request_id or f"schema-change-{change.id}-{uuid4()}"
    if _already_applied(db, validation):
        now = _utc_now()
        change.status = "executed"
        change.confirmed_at = change.confirmed_at or now
        change.executed_at = change.executed_at or now
        change.error_message = None
        preview = dict(change.schema_preview or {})
        preview["execution"] = {"mode": "already_applied_idempotent", "executed_at": now.isoformat()}
        change.schema_preview = preview
        action_log_id = _record_action_log(
            db,
            request_id=safe_request_id,
            change=change,
            validation=validation,
            status="idempotent",
        )
        db.commit()
        db.refresh(change)
        return {
            "executed": False,
            "idempotent": True,
            "schema_change": schema_change_to_dict(change),
            "action_log_id": action_log_id,
            "execution_mode": "restricted_admin_confirmed_transaction",
            "raw_sql_accepted": False,
        }

    try:
        # The SQL originated from the persisted preview and is revalidated above. No raw
        # caller-supplied DDL reaches this point.
        db.execute(text(validation.normalized_sql or change.proposed_sql or ""))
        now = _utc_now()
        change.status = "executed"
        change.confirmed_at = now
        change.executed_at = now
        change.error_message = None
        preview = dict(change.schema_preview or {})
        preview["execution"] = {
            "mode": "restricted_admin_confirmed_transaction",
            "executed_at": now.isoformat(),
            "statement_type": validation.statement_type,
            "tables": validation.tables,
            "columns": validation.columns,
            "production_note": "Production deployments should also capture this reviewed change in an Alembic migration.",
        }
        change.schema_preview = preview
        action_log_id = _record_action_log(
            db,
            request_id=safe_request_id,
            change=change,
            validation=validation,
            status="success",
        )
        db.commit()
        db.refresh(change)
    except SQLAlchemyError as exc:
        db.rollback()
        # Store a best-effort failure state without leaking vendor/database details.
        failed_change = db.get(SchemaChangeRequest, schema_change_id)
        if failed_change is not None:
            failed_change.status = "failed_execution"
            failed_change.error_message = "The restricted schema change could not be applied by PostgreSQL."
            db.commit()
        raise AdminSchemaExecutionError(
            code="admin_schema_execution_failed",
            message="The restricted schema change could not be applied. No partial schema result was reported.",
        ) from exc

    return {
        "executed": True,
        "idempotent": False,
        "schema_change": schema_change_to_dict(change),
        "action_log_id": action_log_id,
        "execution_mode": "restricted_admin_confirmed_transaction",
        "raw_sql_accepted": False,
        "migration_note": "For production, capture the reviewed operation in a version-controlled Alembic migration as well.",
    }
