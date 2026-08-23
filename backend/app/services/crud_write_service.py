"""Phase 7 confirmation-gated CRUD write service.

The service accepts only Phase 4 validator-approved DML. It creates a preview in
``pending_actions`` first. Confirmation later executes exactly that stored proposal
inside one transaction, writes action logs, and stores before/after snapshots for
UPDATE and DELETE. INSERT/UPDATE/DELETE are never executed from a fresh confirm
request body.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from typing import Any
from uuid import UUID

import sqlglot
from sqlglot import exp
from sqlalchemy import and_, or_, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session as DbSession

from app.core.constants import AgentRoute, ResponseStatus, UserRole
from app.models import Base
from app.models.operations import ActionLog, ChangeSnapshot, PendingAction
from app.services.dynamic_pgsql_schema import get_runtime_table, has_public_table
from app.services.duplicate_service import (
    detect_record_duplicates,
    find_batch_duplicates,
    get_unique_key_sets,
    json_safe,
    row_to_dict,
)
from app.services.schema_registry import OPERATIONAL_TABLES, get_allowed_columns, get_relationships
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
    affected_record_ids: list[dict[str, Any]] = field(default_factory=list)
    affected_records: list[dict[str, Any]] = field(default_factory=list)
    expected_row_count: int | None = None
    count_verified: bool = False
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
        if not has_public_table(normalized):
            raise CrudWriteError(
                status=ResponseStatus.VALIDATION_FAILED,
                code="unknown_table",
                message=f"Table '{normalized}' is not available in the reflected PostgreSQL public schema.",
            )
        if normalized in OPERATIONAL_TABLES:
            raise CrudWriteError(
                status=ResponseStatus.VALIDATION_FAILED,
                code="write_target_not_allowed",
                message="Operational/audit tables are not valid business write targets.",
            )
        try:
            return get_runtime_table(normalized)
        except KeyError as exc:
            raise CrudWriteError(
                status=ResponseStatus.VALIDATION_FAILED,
                code="unknown_table",
                message=f"Table '{normalized}' is not available for controlled writes.",
            ) from exc

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
        audit_managed = {name for name in ("created_at", "updated_at") if name in table.c}

        def database_generated(column: Any) -> bool:
            try:
                python_type = column.type.python_type
            except (AttributeError, NotImplementedError):
                python_type = None
            return bool(
                column.identity is not None
                or column.computed is not None
                or (column.primary_key and python_type is int and column.autoincrement in (True, "auto"))
            )

        generated = {column.name for column in table.columns if database_generated(column)}
        allowed = {column.name for column in table.columns} - audit_managed - generated
        required = [
            column.name
            for column in table.columns
            if not column.nullable
            and column.name not in audit_managed
            and column.name not in generated
            and column.default is None
            and column.server_default is None
        ]
        for index, record in enumerate(records):
            unknown = sorted(set(record) - allowed)
            managed = sorted(set(record) & (audit_managed | generated))
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
    def _json_safe_record(record: dict[str, Any]) -> dict[str, Any]:
        return {str(key): json_safe(value) for key, value in record.items()}

    @staticmethod
    def _coerce_stored_value(column: Any, value: Any) -> Any:
        """Restore JSONB-safe values to the Python type expected by SQLAlchemy."""

        if value is None:
            return None
        try:
            python_type = column.type.python_type
        except (AttributeError, NotImplementedError):
            return value
        if python_type is date and not isinstance(value, date):
            return date.fromisoformat(str(value))
        if python_type is datetime and not isinstance(value, datetime):
            return datetime.fromisoformat(str(value))
        if python_type is Decimal and not isinstance(value, Decimal):
            return Decimal(str(value))
        if python_type is bool and not isinstance(value, bool):
            normalized = str(value).strip().casefold()
            if normalized in {"true", "1", "yes"}:
                return True
            if normalized in {"false", "0", "no"}:
                return False
        if python_type is int and not isinstance(value, bool):
            return int(value)
        return value

    @classmethod
    def _coerce_stored_record(cls, table: Any, record: dict[str, Any]) -> dict[str, Any]:
        return {
            key: cls._coerce_stored_value(table.c[key], value) if key in table.c else value
            for key, value in record.items()
        }

    @staticmethod
    def _expected_bulk_count(
        records: list[dict[str, Any]],
        *,
        generation_metadata: dict[str, Any] | None = None,
        count_contract: dict[str, Any] | None = None,
    ) -> int:
        """Resolve and enforce one exact count across generation, preview, storage, and confirmation."""

        candidates: list[tuple[str, Any]] = [("stored_records", len(records))]
        metadata = generation_metadata if isinstance(generation_metadata, dict) else {}
        contract = count_contract if isinstance(count_contract, dict) else {}
        for name in ("requested_record_count", "generated_record_count"):
            if name in metadata:
                candidates.append((name, metadata.get(name)))
        for name in ("requested_record_count", "generated_record_count", "preview_record_count", "stored_record_count"):
            if name in contract:
                candidates.append((name, contract.get(name)))

        normalized: list[tuple[str, int]] = []
        for name, raw in candidates:
            if isinstance(raw, bool):
                continue
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            normalized.append((name, value))

        if not normalized:
            raise CrudWriteError(
                status=ResponseStatus.VALIDATION_FAILED,
                code="bulk_count_contract_missing",
                message="The bulk action does not contain a valid record count.",
            )

        expected = normalized[0][1]
        if expected < 1:
            raise CrudWriteError(
                status=ResponseStatus.VALIDATION_FAILED,
                code="bulk_count_must_be_positive",
                message="A bulk insert must contain at least one record.",
            )

        mismatches = [{"stage": name, "count": value} for name, value in normalized if value != expected]
        if mismatches:
            raise CrudWriteError(
                status=ResponseStatus.VALIDATION_FAILED,
                code="bulk_count_contract_mismatch",
                message="The requested, generated, previewed, and stored bulk counts do not match. No write was executed.",
                details=[{"expected_count": expected, "observed_counts": dict(normalized), "mismatches": mismatches}],
            )
        return expected

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
        primary_keys = list(table.primary_key.columns)
        if not primary_keys:
            return []
        identities = [
            {column.name: row[column.name] for column in primary_keys}
            for row in before_rows
            if all(row.get(column.name) is not None for column in primary_keys)
        ]
        if not identities:
            return []
        filters = [
            and_(*(table.c[name] == value for name, value in identity.items()))
            for identity in identities
        ]
        statement = select(table).where(or_(*filters)).order_by(*primary_keys)
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
        """Backward-compatible employee-child preview used by existing Phase 7 tests.

        New code uses metadata-driven relationship protection, but retaining this helper
        keeps older integrations stable while also including employee experiences.
        """

        ids = [row.get("id") for row in rows if row.get("id") is not None]
        if not ids:
            return []
        child_rows: list[dict[str, Any]] = []
        for table_name in ("employee_permissions", "employee_experiences"):
            table = Base.metadata.tables.get(table_name)
            if table is None or "employee_id" not in table.c:
                continue
            for row in db.execute(select(table).where(table.c.employee_id.in_(ids))).mappings().all():
                child_rows.append({"child_table": table_name, **row_to_dict(row)})
        return child_rows

    def _restricting_child_records(
        self,
        db: DbSession,
        *,
        parent_table: str,
        parent_rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Return child records protected by ON DELETE RESTRICT for any business parent.

        Relationship metadata is the source of truth, so new approved child tables are
        protected without another employee-specific branch.
        """

        details: list[dict[str, Any]] = []
        for relationship in get_relationships():
            if relationship.get("to_table") != parent_table:
                continue
            delete_rule = str(relationship.get("delete_rule") or "NO ACTION").upper()
            if delete_rule not in {"RESTRICT", "NO ACTION"}:
                continue
            child_table_name = str(relationship.get("from_table") or "")
            child_column_name = str(relationship.get("from_column") or "")
            parent_column_name = str(relationship.get("to_column") or "")
            parent_values = [
                row.get(parent_column_name)
                for row in parent_rows
                if row.get(parent_column_name) is not None
            ]
            if not parent_values:
                continue
            try:
                child_table = self._business_table(child_table_name)
            except CrudWriteError:
                continue
            if child_column_name not in child_table.c:
                continue
            rows = [
                row_to_dict(row)
                for row in db.execute(
                    select(child_table)
                    .where(child_table.c[child_column_name].in_(parent_values))
                    .limit(MAX_PREVIEW_ROWS + 1)
                ).mappings().all()
            ]
            if rows:
                details.append(
                    {
                        "child_table": child_table_name,
                        "child_foreign_key": child_column_name,
                        "delete_rule": delete_rule,
                        "record_count": len(rows),
                        "records": rows[:MAX_PREVIEW_ROWS],
                    }
                )
        return details

    @staticmethod
    def _summary(action_type: str, target_table: str, affected_count: int) -> str:
        noun = "record" if affected_count == 1 else "records"
        return f"Preview: {action_type.lower()} {affected_count} {noun} in {target_table}. Confirmation is required before execution."

    @staticmethod
    def _record_identities(records: list[dict[str, Any]], table: Any | None = None) -> list[dict[str, Any]]:
        """Return stable business identifiers for action memory without storing full rows."""

        identity_keys = (
            "id",
            "employee_code",
            "vendor_code",
            "customer_code",
            "product_code",
            "deal_code",
            "permission_code",
            "vendor_sku",
            "email",
            "contact_email",
        )
        identities: list[dict[str, Any]] = []
        primary_keys = list(table.primary_key.columns) if table is not None else []
        for index, record in enumerate(records):
            identity: dict[str, Any] = {}
            if primary_keys and all(record.get(column.name) is not None for column in primary_keys):
                identity = {
                    column.name: json_safe(record[column.name])
                    for column in primary_keys
                }
            for key in identity_keys:
                if identity:
                    break
                value = record.get(key)
                if value not in (None, ""):
                    identity[key] = json_safe(value)
                    if key == "id" or key.endswith("_code"):
                        break
            if not identity:
                identity = {"record_index": index}
            identities.append(identity)
        return identities

    def check_duplicates(
        self,
        db: DbSession,
        *,
        target_table: str,
        values: dict[str, Any],
    ) -> dict[str, Any]:
        """Run a read-only duplicate check without creating a pending write action."""

        normalized = target_table.strip().lower()
        table = self._business_table(normalized)
        unknown = sorted(set(values) - {column.name for column in table.columns})
        if unknown:
            raise CrudWriteError(
                status=ResponseStatus.VALIDATION_FAILED,
                code="duplicate_check_unknown_columns",
                message="Duplicate-check values contain columns that are not present in the reflected table.",
                details=[{"target_table": normalized, "unknown_columns": unknown}],
            )
        key_sets = get_unique_key_sets(db, normalized)
        checked_keys = [
            list(keys)
            for keys in key_sets
            if all(values.get(key) not in (None, "") for key in keys)
        ]
        matches = [
            match.to_dict()
            for match in detect_record_duplicates(db, table_name=normalized, values=values)
        ]
        return {
            "target_table": normalized,
            "checked_key_sets": checked_keys,
            "duplicate_matches": matches,
            "duplicate_found": bool(matches),
            "write_execution_allowed": False,
            "schema_source": "postgresql_reflection",
        }

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
            batch_duplicates = find_batch_duplicates(target_table, records, db)
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
            if statement_type == "DELETE":
                child_rows = self._restricting_child_records(
                    db,
                    parent_table=target_table,
                    parent_rows=affected_rows,
                )
                if target_table == "employees" and not child_rows:
                    legacy_rows = self._employee_child_preview(db, affected_rows)
                    if legacy_rows:
                        child_rows = [{"child_table": "employee_children", "delete_rule": "RESTRICT", "records": legacy_rows}]
                if child_rows:
                    raise CrudWriteError(
                        status=ResponseStatus.CLARIFICATION_REQUIRED,
                        code="parent_child_records_exist",
                        message="Deletion is blocked because related child records use ON DELETE RESTRICT. Resolve those child records first.",
                        details=[{"parent_records": affected_rows, "child_relationships": child_rows}],
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
        metadata = dict(generation_metadata or {})
        requested_count = self._expected_bulk_count(records, generation_metadata=metadata)
        generated_count = len(records)
        stored_records = [self._json_safe_record(record) for record in records]
        count_contract = {
            "requested_record_count": requested_count,
            "generated_record_count": generated_count,
            "preview_record_count": generated_count,
            "stored_record_count": generated_count,
            "confirmed_record_count": None,
            "count_verified": True,
        }
        metadata.update(
            {
                "requested_record_count": requested_count,
                "generated_record_count": generated_count,
                "count_verified": True,
            }
        )
        inside_batch = find_batch_duplicates(target_table, records, db)
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
            "summary": self._summary("BULK_INSERT", target_table, generated_count),
            "requires_confirmation": True,
            "target_table": target_table,
            "record_count": generated_count,
            "requested_record_count": requested_count,
            "generated_record_count": generated_count,
            "preview_record_count": generated_count,
            "records": stored_records,
            "duplicate_check": {"status": "clear", "checked_record_count": generated_count},
            "generation_metadata": metadata,
            "count_contract": count_contract,
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
                "records": stored_records,
                "user_prompt": user_prompt,
                "generation_metadata": metadata,
                "count_contract": count_contract,
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

    def propose_structured_write(
        self,
        db: DbSession,
        *,
        session_id: UUID,
        target_table: str,
        statement_type: str,
        record_ids: list[int],
        changes: dict[str, Any] | None,
        actor_role: UserRole,
        user_prompt: str | None = None,
        ttl_minutes: int = 30,
    ) -> WriteProposalResult:
        """Create a confirmation preview for a backend-compiled UPDATE or DELETE.

        This path is used by semantic parent-child CRUD. It accepts only resolved primary
        keys and validated literal changes; no model-generated SQL is stored or executed.
        """

        self._require_session(db, session_id, actor_role)
        normalized_table = target_table.strip().lower()
        table = self._business_table(normalized_table)
        operation = statement_type.strip().upper()
        if operation not in {"UPDATE", "DELETE"}:
            raise CrudWriteError(
                status=ResponseStatus.VALIDATION_FAILED,
                code="structured_write_operation_invalid",
                message="Structured writes support UPDATE or DELETE only.",
            )
        normalized_ids = sorted({int(value) for value in record_ids if int(value) > 0})
        if not normalized_ids:
            raise CrudWriteError(
                status=ResponseStatus.CLARIFICATION_REQUIRED,
                code="structured_write_record_ids_required",
                message="No exact records were selected for the requested write.",
            )
        if len(normalized_ids) > MAX_PREVIEW_ROWS:
            raise CrudWriteError(
                status=ResponseStatus.CLARIFICATION_REQUIRED,
                code="too_many_affected_rows",
                message=f"The requested write selects more than {MAX_PREVIEW_ROWS} records. Narrow the request first.",
            )

        before_rows = [
            row_to_dict(row)
            for row in db.execute(
                select(table).where(table.c.id.in_(normalized_ids)).order_by(table.c.id)
            ).mappings().all()
        ]
        if len(before_rows) != len(normalized_ids):
            found_ids = {int(row["id"]) for row in before_rows if row.get("id") is not None}
            missing_ids = [value for value in normalized_ids if value not in found_ids]
            raise CrudWriteError(
                status=ResponseStatus.INFORMATION_NOT_AVAILABLE,
                code="structured_write_records_missing",
                message="One or more selected records no longer exist. Create a new preview.",
                details=[{"missing_record_ids": missing_ids}],
            )

        normalized_changes: dict[str, Any] = {}
        if operation == "UPDATE":
            raw_changes = dict(changes or {})
            if not raw_changes:
                raise CrudWriteError(
                    status=ResponseStatus.CLARIFICATION_REQUIRED,
                    code="structured_write_changes_required",
                    message="Which field should be changed, and what should its new value be?",
                )
            allowed = set(get_allowed_columns(normalized_table)) - MANAGED_COLUMNS
            foreign_key_columns = {column.name for column in table.columns if column.foreign_keys}
            invalid = sorted(set(raw_changes) - allowed)
            reparenting = sorted(set(raw_changes) & foreign_key_columns)
            if invalid or reparenting:
                raise CrudWriteError(
                    status=ResponseStatus.CLARIFICATION_REQUIRED,
                    code="structured_write_fields_invalid",
                    message="The requested update contains an unknown, managed, or relationship key field.",
                    details=[{"invalid_fields": invalid, "relationship_fields_blocked": reparenting}],
                )
            normalized_changes = raw_changes

        child_relationships: list[dict[str, Any]] = []
        if operation == "DELETE":
            child_relationships = self._restricting_child_records(
                db,
                parent_table=normalized_table,
                parent_rows=before_rows,
            )
            if child_relationships:
                raise CrudWriteError(
                    status=ResponseStatus.CLARIFICATION_REQUIRED,
                    code="parent_child_records_exist",
                    message="Deletion is blocked because related child records use ON DELETE RESTRICT. Resolve those child records first.",
                    details=[{"parent_records": before_rows, "child_relationships": child_relationships}],
                )

        preview = {
            "summary": self._summary(operation, normalized_table, len(before_rows)),
            "requires_confirmation": True,
            "affected_row_count": len(before_rows),
            "affected_rows": before_rows,
            "changes": {key: json_safe(value) for key, value in normalized_changes.items()},
            "child_records": child_relationships,
            "snapshot_plan": ["before", "after"] if operation == "UPDATE" else ["before"],
            "execution_mode": "structured_relationship",
        }
        action = create_pending_action(
            db,
            session_id=session_id,
            action_type=operation.lower(),
            target_table=normalized_table,
            validated_payload={
                "mode": "structured_relationship",
                "statement_type": operation,
                "target_table": normalized_table,
                "record_ids": normalized_ids,
                "changes": {key: json_safe(value) for key, value in normalized_changes.items()},
                "user_prompt": user_prompt,
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
        try:
            table = CrudWriteService._business_table(table_name)
        except CrudWriteError:
            table = None
        identity = CrudWriteService._record_identities([record], table)[0]
        record_id = json.dumps(identity, sort_keys=True, default=str)
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
        if actor_role != UserRole.ADMIN:
            raise CrudWriteError(
                status=ResponseStatus.VALIDATION_FAILED,
                code="admin_role_required_for_confirmation",
                message="Only admin users can confirm pending write actions. Normal workspace users may create previews, but cannot execute database writes.",
            )
        action = self._load_pending_for_mutation(db, session_id=session_id, action_id=pending_action_id)
        if action.status == "confirmed":
            payload = action.validated_payload if isinstance(action.validated_payload, dict) else {}
            records = payload.get("records") if isinstance(payload.get("records"), list) else []
            records = [dict(item) for item in records if isinstance(item, dict)]
            existing_log = db.scalar(
                select(ActionLog)
                .where(ActionLog.pending_action_id == action.id, ActionLog.status == "success")
                .order_by(ActionLog.created_at.desc())
            )
            affected = existing_log.affected_record_ids if existing_log and isinstance(existing_log.affected_record_ids, dict) else {}
            existing_table = self._business_table(str(action.target_table or ""))
            identities = affected.get("record_ids") if isinstance(affected.get("record_ids"), list) else self._record_identities(records, existing_table)
            generation_metadata = payload.get("generation_metadata") if isinstance(payload.get("generation_metadata"), dict) else {}
            count_contract = payload.get("count_contract") if isinstance(payload.get("count_contract"), dict) else {}
            expected_count = self._expected_bulk_count(records, generation_metadata=generation_metadata, count_contract=count_contract) if records else None
            affected_count = int(affected.get("affected_row_count") or len(records))
            return WriteConfirmationResult(
                pending_action=pending_action_to_dict(action),
                action_log_id=existing_log.id if existing_log else None,
                affected_row_count=affected_count,
                before_snapshot_count=int(affected.get("before_snapshot_count") or 0),
                after_snapshot_count=int(affected.get("after_snapshot_count") or len(records)),
                affected_record_ids=identities,
                affected_records=[{str(key): json_safe(value) for key, value in item.items()} for item in records],
                expected_row_count=expected_count,
                count_verified=expected_count is None or affected_count == expected_count,
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
        expected_row_count: int | None = None
        count_verified = False
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
                    if statement_type == "DELETE":
                        child_rows = self._restricting_child_records(
                            db,
                            parent_table=target_table,
                            parent_rows=before_rows,
                        )
                        if target_table == "employees" and not child_rows:
                            legacy_rows = self._employee_child_preview(db, before_rows)
                            if legacy_rows:
                                child_rows = [{"child_table": "employee_children", "delete_rule": "RESTRICT", "records": legacy_rows}]
                        if child_rows:
                            raise CrudWriteError(
                                status=ResponseStatus.CLARIFICATION_REQUIRED,
                                code="parent_child_records_exist",
                                message="Deletion is blocked because related child records use ON DELETE RESTRICT.",
                                details=[{"parent_records": before_rows, "child_relationships": child_rows}],
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
                if statement_type == "INSERT":
                    stored_records = payload.get("records") if isinstance(payload.get("records"), list) else []
                    after_rows = [dict(item) for item in stored_records if isinstance(item, dict)]
                    for row in after_rows:
                        self._snapshot(db, action_log_id=action_log.id, table_name=target_table, record=row, snapshot_type="after")
                if statement_type == "UPDATE":
                    after_rows = self._rows_by_primary_ids(db, target_table=target_table, before_rows=before_rows)
                    for row in after_rows:
                        self._snapshot(db, action_log_id=action_log.id, table_name=target_table, record=row, snapshot_type="after")
            elif mode == "bulk_records":
                records = payload.get("records")
                if not isinstance(records, list) or not records or not all(isinstance(record, dict) for record in records):
                    raise CrudWriteError(status=ResponseStatus.VALIDATION_FAILED, code="stored_bulk_records_missing", message="The stored bulk record payload is invalid.")
                records = [self._coerce_stored_record(table, dict(record)) for record in records]
                generation_metadata = payload.get("generation_metadata") if isinstance(payload.get("generation_metadata"), dict) else {}
                count_contract = payload.get("count_contract") if isinstance(payload.get("count_contract"), dict) else {}
                expected_row_count = self._expected_bulk_count(
                    records,
                    generation_metadata=generation_metadata,
                    count_contract=count_contract,
                )
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
                insert_statement = table.insert().returning(*table.columns)
                result = db.execute(insert_statement, records)
                after_rows = [row_to_dict(row) for row in result.mappings().all()]
                affected_count = len(after_rows)
                if affected_count != expected_row_count:
                    raise CrudWriteError(
                        status=ResponseStatus.TOOL_FAILED,
                        code="confirmed_bulk_count_mismatch",
                        message=(
                            f"PostgreSQL returned {affected_count} inserted records, but {expected_row_count} were stored in the confirmed preview. "
                            "The transaction was rolled back."
                        ),
                        details=[
                            {
                                "requested_record_count": expected_row_count,
                                "confirmed_record_count": affected_count,
                                "pending_action_id": str(action.id),
                                "target_table": target_table,
                            }
                        ],
                    )
                count_verified = True
                for index, record in enumerate(after_rows):
                    snapshot_record = {**record, "record_index": index}
                    self._snapshot(db, action_log_id=action_log.id, table_name=target_table, record=snapshot_record, snapshot_type="after")
            elif mode == "structured_relationship":
                statement_type = str(payload.get("statement_type") or "").upper()
                raw_ids = payload.get("record_ids")
                record_ids = sorted({int(value) for value in raw_ids or [] if int(value) > 0}) if isinstance(raw_ids, list) else []
                if statement_type not in {"UPDATE", "DELETE"} or not record_ids:
                    raise CrudWriteError(
                        status=ResponseStatus.VALIDATION_FAILED,
                        code="stored_structured_relationship_invalid",
                        message="The stored structured relationship action is invalid.",
                    )
                before_rows = [
                    row_to_dict(row)
                    for row in db.execute(
                        select(table).where(table.c.id.in_(record_ids)).order_by(table.c.id).with_for_update()
                    ).mappings().all()
                ]
                if len(before_rows) != len(record_ids):
                    raise CrudWriteError(
                        status=ResponseStatus.INFORMATION_NOT_AVAILABLE,
                        code="structured_write_records_changed",
                        message="One or more selected records changed or disappeared after preview. Create a new preview.",
                    )
                if statement_type == "DELETE":
                    child_rows = self._restricting_child_records(
                        db,
                        parent_table=target_table,
                        parent_rows=before_rows,
                    )
                    if child_rows:
                        raise CrudWriteError(
                            status=ResponseStatus.CLARIFICATION_REQUIRED,
                            code="parent_child_records_exist",
                            message="Deletion is blocked because related child records use ON DELETE RESTRICT.",
                            details=[{"parent_records": before_rows, "child_relationships": child_rows}],
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
                for row in before_rows:
                    self._snapshot(db, action_log_id=action_log.id, table_name=target_table, record=row, snapshot_type="before")
                if statement_type == "UPDATE":
                    stored_changes = payload.get("changes") if isinstance(payload.get("changes"), dict) else {}
                    changes = self._coerce_stored_record(table, dict(stored_changes))
                    if not changes:
                        raise CrudWriteError(
                            status=ResponseStatus.VALIDATION_FAILED,
                            code="stored_structured_changes_missing",
                            message="The stored structured update contains no changes.",
                        )
                    update_result = db.execute(
                        table.update()
                        .where(table.c.id.in_(record_ids))
                        .values(**changes)
                        .returning(*table.columns)
                    )
                    after_rows = [row_to_dict(row) for row in update_result.mappings().all()]
                    affected_count = len(after_rows)
                    for row in after_rows:
                        self._snapshot(db, action_log_id=action_log.id, table_name=target_table, record=row, snapshot_type="after")
                else:
                    delete_result = db.execute(
                        table.delete().where(table.c.id.in_(record_ids))
                    )
                    affected_count = max(0, int(delete_result.rowcount or 0))
                if affected_count != len(record_ids):
                    raise CrudWriteError(
                        status=ResponseStatus.TOOL_FAILED,
                        code="structured_write_count_mismatch",
                        message="The confirmed write affected a different number of records than the preview. The transaction was rolled back.",
                        details=[{"preview_record_count": len(record_ids), "affected_row_count": affected_count}],
                    )
            else:
                raise CrudWriteError(status=ResponseStatus.VALIDATION_FAILED, code="unsupported_pending_action_mode", message="The pending action does not contain an executable Phase 7 payload.")

            action.status = "confirmed"
            action.confirmed_at = _utc_now()
            action_log.status = "success"
            affected_records = after_rows if after_rows else before_rows
            affected_record_ids = self._record_identities(affected_records, table)
            action_log.affected_record_ids = {
                "affected_row_count": affected_count,
                "before_snapshot_count": len(before_rows),
                "after_snapshot_count": len(after_rows),
                "record_ids": affected_record_ids,
                "pending_action_id": str(action.id),
                "target_table": target_table,
                "action_type": action.action_type,
                "expected_row_count": expected_row_count,
                "count_verified": count_verified if expected_row_count is not None else True,
            }
            db.commit()
            db.refresh(action)
            return WriteConfirmationResult(
                pending_action=pending_action_to_dict(action),
                action_log_id=action_log.id,
                affected_row_count=affected_count,
                before_snapshot_count=len(before_rows),
                after_snapshot_count=len(after_rows),
                affected_record_ids=affected_record_ids,
                affected_records=[{str(key): json_safe(value) for key, value in item.items()} for item in affected_records],
                expected_row_count=expected_row_count,
                count_verified=count_verified if expected_row_count is not None else True,
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
        if actor_role != UserRole.ADMIN:
            raise CrudWriteError(
                status=ResponseStatus.VALIDATION_FAILED,
                code="admin_role_required_for_confirmation",
                message="Only admin users can confirm pending write actions. Normal workspace users may create previews, but cannot execute database writes.",
            )
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
