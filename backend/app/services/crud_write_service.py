"""Phase 7 confirmation-gated CRUD write service.

The service accepts only Phase 4 validator-approved DML. It creates a preview in
``pending_actions`` first. Confirmation later executes exactly that stored proposal
inside one transaction, writes action logs, and stores before/after snapshots for
UPDATE and DELETE. INSERT/UPDATE/DELETE are never executed from a fresh confirm
request body.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import sqlglot
from sqlglot import exp
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session as DbSession

from app.core.constants import AgentRoute, ResponseStatus, UserRole
from app.models import Base
from app.models.operations import ActionLog, ChangeSnapshot, PendingAction
from app.services.duplicate_service import (
    detect_record_duplicates,
    find_batch_duplicates,
    json_safe,
    row_to_dict,
)
from app.services.schema_registry import BUSINESS_TABLES, get_allowed_columns
from app.services.session_service import (
    _utc_now,
    create_pending_action,
    get_active_session,
    pending_action_to_dict,
)
from app.services.sql_validation import SQLValidationResult, validate_dml_sql


MAX_PREVIEW_ROWS = 25
MANAGED_COLUMNS = {"id", "created_at", "updated_at"}


class CrudWriteError(RuntimeError):
    """Safe error that can be mapped to the common API response envelope."""

    def __init__(self, *, status: ResponseStatus, code: str, message: str, details: list[dict[str, Any]] | None = None) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.details = details
        super().__init__(message)


@dataclass(frozen=True)
class WriteProposalResult:
    pending_action: dict[str, Any]
    validation: SQLValidationResult | None
    preview: dict[str, Any]
    duplicate_matches: list[dict[str, Any]]
    generated_sql: str | None
    model_metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class WriteConfirmationResult:
    pending_action: dict[str, Any]
    action_log_id: int | None
    affected_row_count: int
    before_snapshot_count: int
    after_snapshot_count: int
    idempotent: bool = False


class CrudWriteService:
    """Implement confirmation-gated write behavior over approved business tables."""

    @staticmethod
    def _require_session(db: DbSession, session_id: UUID, actor_role: UserRole) -> None:
        session = get_active_session(db, session_id)
        if session is None:
            raise CrudWriteError(
                status=ResponseStatus.INFORMATION_NOT_AVAILABLE,
                code="inactive_or_missing_session",
                message="The session is missing, expired, or inactive.",
            )
        if session.user_role != actor_role.value:
            raise CrudWriteError(
                status=ResponseStatus.VALIDATION_FAILED,
                code="session_role_mismatch",
                message="The supplied role does not match the role that created this session.",
            )

    @staticmethod
    def _business_table(table_name: str) -> Any:
        normalized = table_name.strip().lower()
        if normalized not in BUSINESS_TABLES:
            raise CrudWriteError(
                status=ResponseStatus.VALIDATION_FAILED,
                code="write_target_not_allowed",
                message="Phase 7 writes are limited to approved business tables.",
            )
        table = Base.metadata.tables.get(normalized)
        if table is None:
            raise CrudWriteError(
                status=ResponseStatus.VALIDATION_FAILED,
                code="unknown_table",
                message=f"Table '{normalized}' is not available for controlled writes.",
            )
        return table

    @staticmethod
    def _parse(sql: str) -> exp.Expression:
        try:
            return sqlglot.parse_one(sql, read="postgres")
        except sqlglot.errors.ParseError as exc:
            raise CrudWriteError(
                status=ResponseStatus.VALIDATION_FAILED,
                code="sql_parse_error",
                message="The stored SQL proposal could not be parsed safely.",
            ) from exc

    @staticmethod
    def _statement_type(expression: exp.Expression) -> str:
        if isinstance(expression, exp.Insert):
            return "INSERT"
        if isinstance(expression, exp.Update):
            return "UPDATE"
        if isinstance(expression, exp.Delete):
            return "DELETE"
        raise CrudWriteError(
            status=ResponseStatus.VALIDATION_FAILED,
            code="unsupported_write_statement",
            message="Only INSERT, UPDATE, and DELETE can use the Phase 7 confirmation path.",
        )

    @staticmethod
    def _target_table_name(validation: SQLValidationResult) -> str:
        if len(validation.tables) != 1:
            raise CrudWriteError(
                status=ResponseStatus.VALIDATION_FAILED,
                code="single_target_table_required",
                message="A Phase 7 write proposal must target exactly one approved business table.",
            )
        return validation.tables[0]

    @staticmethod
    def _literal_value(expression: exp.Expression) -> Any:
        """Accept only literal INSERT values so preview/duplicate checks are deterministic."""

        if isinstance(expression, exp.Null):
            return None
        if isinstance(expression, exp.Boolean):
            return bool(expression.this)
        if isinstance(expression, exp.Literal):
            raw = expression.this
            if expression.is_string:
                return str(raw)
            try:
                return int(str(raw)) if "." not in str(raw) else float(str(raw))
            except ValueError:
                return str(raw)
        if isinstance(expression, exp.Neg) and isinstance(expression.this, exp.Literal):
            value = CrudWriteService._literal_value(expression.this)
            return -value if isinstance(value, (int, float)) else value
        if isinstance(expression, exp.Cast):
            return CrudWriteService._literal_value(expression.this)
        raise CrudWriteError(
            status=ResponseStatus.VALIDATION_FAILED,
            code="nonliteral_insert_value_blocked",
            message="INSERT preview supports literal values only in Phase 7.",
        )

    def _insert_records_from_sql(self, expression: exp.Expression, target_table: str) -> list[dict[str, Any]]:
        if not isinstance(expression, exp.Insert):
            return []
        target = expression.this
        if not isinstance(target, exp.Schema):
            raise CrudWriteError(
                status=ResponseStatus.VALIDATION_FAILED,
                code="insert_columns_required",
                message="INSERT proposals must explicitly name target columns.",
            )
        columns = [column.name.lower() for column in target.expressions if isinstance(column, exp.Identifier)]
        values = expression.expression
        if not isinstance(values, exp.Values) or not values.expressions:
            raise CrudWriteError(
                status=ResponseStatus.VALIDATION_FAILED,
                code="insert_values_required",
                message="INSERT proposals require one or more explicit VALUES tuples.",
            )
        records: list[dict[str, Any]] = []
        for value_tuple in values.expressions:
            items = list(getattr(value_tuple, "expressions", []) or [])
            if len(items) != len(columns):
                raise CrudWriteError(
                    status=ResponseStatus.VALIDATION_FAILED,
                    code="insert_values_column_mismatch",
                    message="Each INSERT VALUES tuple must match the named column count.",
                )
            records.append({column: self._literal_value(item) for column, item in zip(columns, items)})
        self._validate_records(target_table, records)
        return records

    def _validate_records(self, target_table: str, records: list[dict[str, Any]]) -> None:
        table = self._business_table(target_table)
        allowed = set(get_allowed_columns(target_table)) - MANAGED_COLUMNS
        required = [
            column.name
            for column in table.columns
            if not column.primary_key
            and not column.nullable
            and column.default is None
            and column.server_default is None
            and column.name not in MANAGED_COLUMNS
        ]
        for index, record in enumerate(records):
            unknown = sorted(set(record) - allowed)
            managed = sorted(set(record) & MANAGED_COLUMNS)
            missing = [name for name in required if record.get(name) in (None, "")]
            if unknown or managed or missing:
                details = [{"record_index": index, "unknown_columns": unknown, "managed_columns": managed, "missing_required_fields": missing}]
                raise CrudWriteError(
                    status=ResponseStatus.CLARIFICATION_REQUIRED,
                    code="record_fields_incomplete_or_unsafe",
                    message="The proposed record is missing required fields or contains unsafe columns.",
                    details=details,
                )

    @staticmethod
    def _where_sql(expression: exp.Expression) -> str | None:
        where = expression.args.get("where")
        if isinstance(where, exp.Where):
            return where.this.sql(dialect="postgres")
        return None

    def _affected_rows(self, db: DbSession, *, target_table: str, expression: exp.Expression) -> list[dict[str, Any]]:
        table = self._business_table(target_table)
        statement = select(table)
        where_sql = self._where_sql(expression)
        if where_sql:
            # where_sql comes from an AST that has already passed the strict Phase 4 validator.
            statement = statement.where(text(where_sql))
        rows = [row_to_dict(row) for row in db.execute(statement.limit(MAX_PREVIEW_ROWS + 1)).mappings().all()]
        if len(rows) > MAX_PREVIEW_ROWS:
            raise CrudWriteError(
                status=ResponseStatus.CLARIFICATION_REQUIRED,
                code="too_many_affected_rows",
                message=f"The proposed write affects more than {MAX_PREVIEW_ROWS} rows. Narrow the WHERE condition before requesting confirmation.",
                details=[{"preview_limit": MAX_PREVIEW_ROWS}],
            )
        return rows

    def _rows_by_primary_ids(self, db: DbSession, *, target_table: str, before_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Reload updated records by stable primary key rather than reusing an old WHERE clause.

        Example: ``UPDATE employees SET city='Kochi' WHERE city='Bangalore'`` no longer
        matches the same WHERE condition after execution, so post-update snapshots must
        use IDs captured before the transaction.
        """

        table = self._business_table(target_table)
        ids = [row.get("id") for row in before_rows if row.get("id") is not None]
        if not ids or "id" not in table.c:
            return []
        statement = select(table).where(table.c.id.in_(ids)).order_by(table.c.id)
        return [row_to_dict(row) for row in db.execute(statement).mappings().all()]

    @staticmethod
    def _update_changes(expression: exp.Expression) -> dict[str, Any]:
        changes: dict[str, Any] = {}
        if not isinstance(expression, exp.Update):
            return changes
        for assignment in expression.expressions:
            if isinstance(assignment, exp.EQ) and isinstance(assignment.left, exp.Column):
                try:
                    changes[assignment.left.name] = CrudWriteService._literal_value(assignment.right)
                except CrudWriteError:
                    changes[assignment.left.name] = assignment.right.sql(dialect="postgres")
        return changes

    def _employee_child_preview(self, db: DbSession, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not rows:
            return []
        ids = [row.get("id") for row in rows if row.get("id") is not None]
        if not ids:
            return []
        permissions = Base.metadata.tables["employee_permissions"]
        child_rows = [row_to_dict(row) for row in db.execute(select(permissions).where(permissions.c.employee_id.in_(ids))).mappings().all()]
        return child_rows

    @staticmethod
    def _summary(action_type: str, target_table: str, affected_count: int) -> str:
        noun = "record" if affected_count == 1 else "records"
        return f"Preview: {action_type.lower()} {affected_count} {noun} in {target_table}. Confirmation is required before execution."

    def propose_sql_write(
        self,
        db: DbSession,
        *,
        session_id: UUID,
        sql: str,
        actor_role: UserRole,
        user_prompt: str | None = None,
        ttl_minutes: int = 30,
        model_metadata: dict[str, Any] | None = None,
    ) -> WriteProposalResult:
        self._require_session(db, session_id, actor_role)
        validation = validate_dml_sql(sql, role=actor_role)
        if not validation.is_valid:
            raise CrudWriteError(
                status=ResponseStatus.VALIDATION_FAILED,
                code=validation.error_code or "sql_validation_failed",
                message=validation.error_message or "The SQL proposal failed safety validation.",
            )
        if validation.statement_type not in {"INSERT", "UPDATE", "DELETE"}:
            raise CrudWriteError(
                status=ResponseStatus.VALIDATION_FAILED,
                code="write_statement_required",
                message="Phase 7 confirmation requires INSERT, UPDATE, or DELETE. SELECT stays on the read-only path.",
            )
        target_table = self._target_table_name(validation)
        self._business_table(target_table)
        expression = self._parse(validation.normalized_sql or sql)
        statement_type = self._statement_type(expression)
        duplicates: list[dict[str, Any]] = []
        preview: dict[str, Any]
        validated_payload: dict[str, Any]

        if statement_type == "INSERT":
            records = self._insert_records_from_sql(expression, target_table)
            batch_duplicates = find_batch_duplicates(target_table, records)
            if batch_duplicates:
                raise CrudWriteError(
                    status=ResponseStatus.DUPLICATE_DETECTED,
                    code="duplicate_records_inside_batch",
                    message="Duplicate business keys were found inside the proposed INSERT batch.",
                    details=batch_duplicates,
                )
            for index, record in enumerate(records):
                matches = detect_record_duplicates(db, table_name=target_table, values=record)
                for match in matches:
                    duplicates.append({"record_index": index, **match.to_dict()})
            if duplicates:
                raise CrudWriteError(
                    status=ResponseStatus.DUPLICATE_DETECTED,
                    code="duplicate_record_detected",
                    message="A matching record already exists. No pending write action was created.",
                    details=duplicates,
                )
            preview = {
                "summary": self._summary("INSERT", target_table, len(records)),
                "requires_confirmation": True,
                "record_count": len(records),
                "records": records,
                "duplicate_check": {"status": "clear", "checked_record_count": len(records)},
            }
            validated_payload = {
                "mode": "sql",
                "statement_type": statement_type,
                "target_table": target_table,
                "sql": validation.normalized_sql,
                "records": records,
                "user_prompt": user_prompt,
            }
        else:
            affected_rows = self._affected_rows(db, target_table=target_table, expression=expression)
            child_rows: list[dict[str, Any]] = []
            if statement_type == "DELETE" and target_table == "employees":
                child_rows = self._employee_child_preview(db, affected_rows)
                if child_rows:
                    raise CrudWriteError(
                        status=ResponseStatus.CLARIFICATION_REQUIRED,
                        code="parent_child_records_exist",
                        message="The requested employee deletion is blocked because child permission records exist. Resolve the child records first.",
                        details=[{"parent_records": affected_rows, "child_records": child_rows, "delete_rule": "RESTRICT"}],
                    )
            preview = {
                "summary": self._summary(statement_type, target_table, len(affected_rows)),
                "requires_confirmation": True,
                "affected_row_count": len(affected_rows),
                "affected_rows": affected_rows,
                "changes": self._update_changes(expression) if statement_type == "UPDATE" else {},
                "child_records": child_rows,
                "snapshot_plan": ["before", "after"] if statement_type == "UPDATE" else ["before"],
            }
            validated_payload = {
                "mode": "sql",
                "statement_type": statement_type,
                "target_table": target_table,
                "sql": validation.normalized_sql,
                "user_prompt": user_prompt,
            }

        action = create_pending_action(
            db,
            session_id=session_id,
            action_type=statement_type.lower(),
            target_table=target_table,
            validated_payload=validated_payload,
            preview_data=preview,
            generated_sql=validation.normalized_sql,
            ttl_minutes=ttl_minutes,
        )
        return WriteProposalResult(
            pending_action=pending_action_to_dict(action),
            validation=validation,
            preview=preview,
            duplicate_matches=duplicates,
            generated_sql=validation.normalized_sql,
            model_metadata=model_metadata,
        )

    def propose_bulk_insert(
        self,
        db: DbSession,
        *,
        session_id: UUID,
        target_table: str,
        records: list[dict[str, Any]],
        actor_role: UserRole,
        user_prompt: str | None = None,
        ttl_minutes: int = 30,
        generation_metadata: dict[str, Any] | None = None,
    ) -> WriteProposalResult:
        self._require_session(db, session_id, actor_role)
        target_table = target_table.strip().lower()
        self._business_table(target_table)
        self._validate_records(target_table, records)
        inside_batch = find_batch_duplicates(target_table, records)
        if inside_batch:
            raise CrudWriteError(
                status=ResponseStatus.DUPLICATE_DETECTED,
                code="duplicate_records_inside_batch",
                message="Duplicate business keys were found inside the proposed bulk batch.",
                details=inside_batch,
            )
        duplicates: list[dict[str, Any]] = []
        for index, record in enumerate(records):
            for match in detect_record_duplicates(db, table_name=target_table, values=record):
                duplicates.append({"record_index": index, **match.to_dict()})
        if duplicates:
            raise CrudWriteError(
                status=ResponseStatus.DUPLICATE_DETECTED,
                code="duplicate_record_detected",
                message="One or more bulk records already exist. No pending write action was created.",
                details=duplicates,
            )
        preview = {
            "summary": self._summary("BULK_INSERT", target_table, len(records)),
            "requires_confirmation": True,
            "record_count": len(records),
            "records": records,
            "duplicate_check": {"status": "clear", "checked_record_count": len(records)},
            "generation_metadata": generation_metadata,
        }
        action = create_pending_action(
            db,
            session_id=session_id,
            action_type="bulk_insert",
            target_table=target_table,
            validated_payload={
                "mode": "bulk_records",
                "statement_type": "INSERT",
                "target_table": target_table,
                "records": records,
                "user_prompt": user_prompt,
                "generation_metadata": generation_metadata,
            },
            preview_data=preview,
            generated_sql=None,
            ttl_minutes=ttl_minutes,
        )
        return WriteProposalResult(
            pending_action=pending_action_to_dict(action),
            validation=None,
            preview=preview,
            duplicate_matches=[],
            generated_sql=None,
        )

    @staticmethod
    def _snapshot(db: DbSession, *, action_log_id: int, table_name: str, record: dict[str, Any], snapshot_type: str) -> None:
        record_id = str(record.get("id") or record.get("employee_code") or record.get("vendor_code") or record.get("customer_code") or record.get("product_code") or record.get("deal_code") or "unknown")
        db.add(
            ChangeSnapshot(
                action_log_id=action_log_id,
                table_name=table_name,
                record_id=record_id,
                snapshot_type=snapshot_type,
                snapshot_data={key: json_safe(value) for key, value in record.items()},
                created_at=datetime.now(timezone.utc),
            )
        )

    @staticmethod
    def _create_action_log(
        db: DbSession,
        *,
        request_id: str,
        session_id: UUID,
        pending_action_id: UUID,
        actor_role: UserRole,
        action_type: str,
        target_table: str,
        generated_sql: str | None,
        status: str,
        confirmation_status: str,
        affected_record_ids: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> ActionLog:
        action_log = ActionLog(
            request_id=request_id,
            session_id=session_id,
            pending_action_id=pending_action_id,
            actor_role=actor_role.value,
            action_type=action_type,
            target_table=target_table,
            affected_record_ids=affected_record_ids,
            generated_sql=generated_sql,
            confirmation_status=confirmation_status,
            status=status,
            error_message=error_message,
            created_at=datetime.now(timezone.utc),
        )
        db.add(action_log)
        db.flush()
        return action_log

    def _load_pending_for_mutation(self, db: DbSession, *, session_id: UUID, action_id: UUID) -> PendingAction:
        statement = select(PendingAction).where(PendingAction.id == action_id, PendingAction.session_id == session_id).with_for_update()
        action = db.scalar(statement)
        if action is None:
            raise CrudWriteError(
                status=ResponseStatus.INFORMATION_NOT_AVAILABLE,
                code="pending_action_not_found",
                message="No pending action exists for this session.",
            )
        if action.status == "confirmed":
            return action
        if action.status != "pending":
            raise CrudWriteError(
                status=ResponseStatus.VALIDATION_FAILED,
                code="pending_action_not_confirmable",
                message=f"This pending action is '{action.status}' and cannot be confirmed.",
            )
        if action.expires_at <= _utc_now():
            action.status = "expired"
            db.commit()
            raise CrudWriteError(
                status=ResponseStatus.VALIDATION_FAILED,
                code="confirmation_expired",
                message="The pending action expired before confirmation and was not executed.",
            )
        return action

    def confirm_action(
        self,
        db: DbSession,
        *,
        session_id: UUID,
        pending_action_id: UUID,
        actor_role: UserRole,
        request_id: str,
    ) -> WriteConfirmationResult:
        self._require_session(db, session_id, actor_role)
        action = self._load_pending_for_mutation(db, session_id=session_id, action_id=pending_action_id)
        if action.status == "confirmed":
            return WriteConfirmationResult(
                pending_action=pending_action_to_dict(action),
                action_log_id=None,
                affected_row_count=0,
                before_snapshot_count=0,
                after_snapshot_count=0,
                idempotent=True,
            )

        payload = dict(action.validated_payload or {})
        mode = payload.get("mode")
        target_table = str(action.target_table or payload.get("target_table") or "").lower()
        table = self._business_table(target_table)
        action_log: ActionLog | None = None
        before_rows: list[dict[str, Any]] = []
        after_rows: list[dict[str, Any]] = []
        affected_count = 0
        try:
            if mode == "sql":
                sql = payload.get("sql")
                if not isinstance(sql, str) or not sql:
                    raise CrudWriteError(status=ResponseStatus.VALIDATION_FAILED, code="stored_sql_missing", message="The stored SQL proposal is missing.")
                validation = validate_dml_sql(sql, role=actor_role)
                if not validation.is_valid or validation.statement_type not in {"INSERT", "UPDATE", "DELETE"}:
                    raise CrudWriteError(
                        status=ResponseStatus.VALIDATION_FAILED,
                        code=validation.error_code or "stored_sql_validation_failed",
                        message=validation.error_message or "The stored SQL proposal no longer passes safety validation.",
                    )
                expression = self._parse(validation.normalized_sql or sql)
                statement_type = self._statement_type(expression)
                if statement_type in {"UPDATE", "DELETE"}:
                    before_rows = self._affected_rows(db, target_table=target_table, expression=expression)
                    if statement_type == "DELETE" and target_table == "employees":
                        child_rows = self._employee_child_preview(db, before_rows)
                        if child_rows:
                            raise CrudWriteError(
                                status=ResponseStatus.CLARIFICATION_REQUIRED,
                                code="parent_child_records_exist",
                                message="Deletion is blocked because child permission records exist.",
                                details=[{"parent_records": before_rows, "child_records": child_rows, "delete_rule": "RESTRICT"}],
                            )
                action_log = self._create_action_log(
                    db,
                    request_id=request_id,
                    session_id=session_id,
                    pending_action_id=action.id,
                    actor_role=actor_role,
                    action_type=action.action_type,
                    target_table=target_table,
                    generated_sql=validation.normalized_sql,
                    status="executing",
                    confirmation_status="confirmed",
                )
                for row in before_rows:
                    self._snapshot(db, action_log_id=action_log.id, table_name=target_table, record=row, snapshot_type="before")
                result = db.execute(text(validation.normalized_sql or sql))
                affected_count = max(0, int(result.rowcount or 0))
                if statement_type == "UPDATE":
                    after_rows = self._rows_by_primary_ids(db, target_table=target_table, before_rows=before_rows)
                    for row in after_rows:
                        self._snapshot(db, action_log_id=action_log.id, table_name=target_table, record=row, snapshot_type="after")
            elif mode == "bulk_records":
                records = payload.get("records")
                if not isinstance(records, list) or not records or not all(isinstance(record, dict) for record in records):
                    raise CrudWriteError(status=ResponseStatus.VALIDATION_FAILED, code="stored_bulk_records_missing", message="The stored bulk record payload is invalid.")
                self._validate_records(target_table, records)
                for record in records:
                    matches = detect_record_duplicates(db, table_name=target_table, values=record)
                    if matches:
                        raise CrudWriteError(
                            status=ResponseStatus.DUPLICATE_DETECTED,
                            code="duplicate_detected_at_confirmation",
                            message="A record became a duplicate after the preview. No bulk insert was executed.",
                            details=[match.to_dict() for match in matches],
                        )
                action_log = self._create_action_log(
                    db,
                    request_id=request_id,
                    session_id=session_id,
                    pending_action_id=action.id,
                    actor_role=actor_role,
                    action_type=action.action_type,
                    target_table=target_table,
                    generated_sql=None,
                    status="executing",
                    confirmation_status="confirmed",
                )
                result = db.execute(table.insert(), records)
                affected_count = max(0, int(result.rowcount or len(records)))
                for index, record in enumerate(records):
                    snapshot_record = {**record, "record_index": index}
                    self._snapshot(db, action_log_id=action_log.id, table_name=target_table, record=snapshot_record, snapshot_type="after")
                after_rows = records
            else:
                raise CrudWriteError(status=ResponseStatus.VALIDATION_FAILED, code="unsupported_pending_action_mode", message="The pending action does not contain an executable Phase 7 payload.")

            action.status = "confirmed"
            action.confirmed_at = _utc_now()
            action_log.status = "success"
            action_log.affected_record_ids = {
                "affected_row_count": affected_count,
                "before_snapshot_count": len(before_rows),
                "after_snapshot_count": len(after_rows),
            }
            db.commit()
            db.refresh(action)
            return WriteConfirmationResult(
                pending_action=pending_action_to_dict(action),
                action_log_id=action_log.id,
                affected_row_count=affected_count,
                before_snapshot_count=len(before_rows),
                after_snapshot_count=len(after_rows),
                idempotent=False,
            )
        except CrudWriteError:
            db.rollback()
            raise
        except IntegrityError as exc:
            db.rollback()
            raise CrudWriteError(
                status=ResponseStatus.DUPLICATE_DETECTED,
                code="database_unique_constraint_blocked_write",
                message="PostgreSQL blocked the write because it conflicts with an existing unique record.",
            ) from exc
        except SQLAlchemyError as exc:
            db.rollback()
            raise CrudWriteError(
                status=ResponseStatus.DATABASE_UNAVAILABLE,
                code="confirmed_write_failed",
                message="PostgreSQL could not complete the confirmed write transaction.",
            ) from exc

    def cancel_action(
        self,
        db: DbSession,
        *,
        session_id: UUID,
        pending_action_id: UUID,
        actor_role: UserRole,
        request_id: str,
    ) -> dict[str, Any]:
        self._require_session(db, session_id, actor_role)
        action = self._load_pending_for_mutation(db, session_id=session_id, action_id=pending_action_id)
        if action.status == "confirmed":
            raise CrudWriteError(
                status=ResponseStatus.VALIDATION_FAILED,
                code="confirmed_action_cannot_be_cancelled",
                message="A confirmed action cannot be cancelled. Use a future rollback workflow for completed writes.",
            )
        action.status = "cancelled"
        action.cancelled_at = _utc_now()
        log = self._create_action_log(
            db,
            request_id=request_id,
            session_id=session_id,
            pending_action_id=action.id,
            actor_role=actor_role,
            action_type=action.action_type,
            target_table=str(action.target_table or ""),
            generated_sql=action.generated_sql,
            status="cancelled",
            confirmation_status="cancelled",
        )
        db.commit()
        db.refresh(action)
        return {"pending_action": pending_action_to_dict(action), "action_log_id": log.id}


crud_write_service = CrudWriteService()
