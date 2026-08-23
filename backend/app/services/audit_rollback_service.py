"""Phase 14 audit hardening and confirmation-gated rollback service.

Rollback is deliberately limited to previously audited UPDATE and DELETE actions on
approved business tables.  It never accepts raw SQL.  A rollback is first stored as a
pending action and must be confirmed by an administrator.  The final execution uses
SQLAlchemy Core statements and creates its own audit log plus snapshots.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session as DbSession

from app.core.constants import ResponseStatus, UserRole
from app.models.operations import ActionLog, ChangeSnapshot, PendingAction
from app.services.duplicate_service import json_safe, row_to_dict
from app.services.dynamic_pgsql_schema import get_runtime_table
from app.services.schema_registry import OPERATIONAL_TABLES
from app.services.session_service import _utc_now, create_pending_action, get_active_session, pending_action_to_dict


ROLLBACK_SUPPORTED_ACTIONS = {"update", "delete"}


class AuditRollbackError(RuntimeError):
    """Controlled Phase 14 audit/rollback failure."""

    def __init__(self, *, status: ResponseStatus, code: str, message: str, details: list[dict[str, Any]] | None = None) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.details = details or []
        super().__init__(message)


@dataclass(frozen=True)
class RollbackPreviewResult:
    pending_action: dict[str, Any]
    original_action: dict[str, Any]
    rollback_plan: dict[str, Any]
    created_session_id: str | None


@dataclass(frozen=True)
class RollbackConfirmationResult:
    pending_action: dict[str, Any]
    rollback_action_log_id: int | None
    original_action_log_id: int
    restored_record_count: int
    before_snapshot_count: int
    after_snapshot_count: int
    idempotent: bool = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _serialize_snapshot(snapshot: ChangeSnapshot) -> dict[str, Any]:
    return {
        "snapshot_id": str(snapshot.id),
        "action_log_id": snapshot.action_log_id,
        "table_name": snapshot.table_name,
        "record_id": snapshot.record_id,
        "snapshot_type": snapshot.snapshot_type,
        "snapshot_data": snapshot.snapshot_data,
        "created_at": snapshot.created_at.isoformat() if snapshot.created_at else None,
    }


def action_log_to_dict(action: ActionLog, *, snapshot_count: int | None = None) -> dict[str, Any]:
    data = {
        "action_log_id": action.id,
        "request_id": action.request_id,
        "session_id": str(action.session_id) if action.session_id else None,
        "pending_action_id": str(action.pending_action_id) if action.pending_action_id else None,
        "actor_role": action.actor_role,
        "action_type": action.action_type,
        "target_table": action.target_table,
        "affected_record_ids": action.affected_record_ids,
        "generated_sql": action.generated_sql,
        "confirmation_status": action.confirmation_status,
        "status": action.status,
        "error_message": action.error_message,
        "created_at": action.created_at.isoformat() if action.created_at else None,
    }
    if snapshot_count is not None:
        data["snapshot_count"] = snapshot_count
    return data


class AuditRollbackService:
    """Admin-only audit lookup and safe rollback service."""

    @staticmethod
    def _require_admin(role: UserRole) -> None:
        if role != UserRole.ADMIN:
            raise AuditRollbackError(
                status=ResponseStatus.VALIDATION_FAILED,
                code="insufficient_role",
                message="Audit rollback operations require the 'admin' role.",
            )

    @staticmethod
    def _require_active_admin_session(db: DbSession, session_id: UUID) -> None:
        session = get_active_session(db, session_id)
        if session is None:
            raise AuditRollbackError(
                status=ResponseStatus.INFORMATION_NOT_AVAILABLE,
                code="inactive_or_missing_session",
                message="The supplied rollback session is missing, expired, or inactive.",
            )
        if session.user_role != UserRole.ADMIN.value:
            raise AuditRollbackError(
                status=ResponseStatus.VALIDATION_FAILED,
                code="session_role_mismatch",
                message="Rollback requires a session created with the admin role.",
            )

    @staticmethod
    def _business_table(table_name: str | None, db: DbSession | None = None):
        normalized = (table_name or "").strip().lower()
        if not normalized or normalized in OPERATIONAL_TABLES:
            raise AuditRollbackError(
                status=ResponseStatus.VALIDATION_FAILED,
                code="rollback_target_not_allowed",
                message="Rollback is limited to audited public business-table writes.",
            )
        try:
            table = get_runtime_table(normalized, bind=db.bind if db is not None else None)
        except KeyError as exc:
            raise AuditRollbackError(
                status=ResponseStatus.VALIDATION_FAILED,
                code="rollback_table_metadata_unavailable",
                message="The audited rollback target cannot be safely resolved from PostgreSQL reflection.",
            ) from exc
        if not list(table.primary_key.columns):
            raise AuditRollbackError(
                status=ResponseStatus.VALIDATION_FAILED,
                code="rollback_primary_key_required",
                message="Safe rollback requires the target table to have a primary key.",
            )
        return table

    @staticmethod
    def _action_with_snapshots(db: DbSession, action_log_id: int) -> tuple[ActionLog, list[ChangeSnapshot]]:
        action = db.get(ActionLog, action_log_id)
        if action is None:
            raise AuditRollbackError(
                status=ResponseStatus.INFORMATION_NOT_AVAILABLE,
                code="action_log_not_found",
                message="No audit action exists with this ID.",
            )
        snapshots = list(
            db.scalars(
                select(ChangeSnapshot)
                .where(ChangeSnapshot.action_log_id == action.id)
                .order_by(ChangeSnapshot.created_at.asc())
            ).all()
        )
        return action, snapshots

    @staticmethod
    def _snapshot_by_type(snapshots: list[ChangeSnapshot], snapshot_type: str) -> list[ChangeSnapshot]:
        return [snapshot for snapshot in snapshots if snapshot.snapshot_type == snapshot_type]

    @staticmethod
    def _identity_filter(table, identity: dict[str, Any]):
        primary_keys = list(table.primary_key.columns)
        if not primary_keys or not all(column.name in identity for column in primary_keys):
            raise AuditRollbackError(
                status=ResponseStatus.VALIDATION_FAILED,
                code="rollback_primary_key_missing",
                message="The audit snapshot does not contain the complete primary key required for rollback.",
            )
        return and_(*(column == identity[column.name] for column in primary_keys))

    @staticmethod
    def _snapshot_identity(table, snapshot: ChangeSnapshot, record: dict[str, Any]) -> dict[str, Any]:
        primary_keys = list(table.primary_key.columns)
        identity = {
            column.name: record[column.name]
            for column in primary_keys
            if record.get(column.name) is not None
        }
        if len(identity) == len(primary_keys):
            return identity
        try:
            stored = json.loads(str(snapshot.record_id or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            stored = None
        if isinstance(stored, dict):
            identity = {
                column.name: stored[column.name]
                for column in primary_keys
                if stored.get(column.name) is not None
            }
        elif len(primary_keys) == 1 and snapshot.record_id not in (None, "", "unknown"):
            identity = {primary_keys[0].name: snapshot.record_id}
        if len(identity) != len(primary_keys):
            raise AuditRollbackError(
                status=ResponseStatus.INFORMATION_NOT_AVAILABLE,
                code="rollback_snapshot_primary_key_missing",
                message="The original snapshot does not contain a complete primary key, so it cannot be rolled back safely.",
            )
        return identity

    @classmethod
    def _row_by_identity(cls, db: DbSession, table, identity: dict[str, Any]) -> dict[str, Any] | None:
        row = db.execute(select(table).where(cls._identity_filter(table, identity)).limit(1)).mappings().first()
        return row_to_dict(row) if row is not None else None

    def _build_plan(self, db: DbSession, action: ActionLog, snapshots: list[ChangeSnapshot]) -> dict[str, Any]:
        action_type = (action.action_type or "").strip().lower()
        if action_type not in ROLLBACK_SUPPORTED_ACTIONS:
            raise AuditRollbackError(
                status=ResponseStatus.VALIDATION_FAILED,
                code="rollback_not_supported_for_action_type",
                message="Phase 14 rollback supports only completed UPDATE and DELETE actions. INSERT/bulk-insert compensation is intentionally deferred.",
                details=[{"action_type": action_type}],
            )
        if action.status != "success":
            raise AuditRollbackError(
                status=ResponseStatus.VALIDATION_FAILED,
                code="rollback_requires_successful_action",
                message="Only a successful audited write can be rolled back.",
            )
        table = self._business_table(action.target_table, db)
        primary_key_names = {column.name for column in table.primary_key.columns}
        before = self._snapshot_by_type(snapshots, "before")
        if not before:
            raise AuditRollbackError(
                status=ResponseStatus.INFORMATION_NOT_AVAILABLE,
                code="rollback_before_snapshot_missing",
                message="The original action has no before snapshot, so a safe rollback cannot be planned.",
            )

        operations: list[dict[str, Any]] = []
        stale_or_conflicting: list[dict[str, Any]] = []
        for snapshot in before:
            original = dict(snapshot.snapshot_data or {})
            identity = self._snapshot_identity(table, snapshot, original)
            record_id: Any = next(iter(identity.values())) if len(identity) == 1 else identity
            current = self._row_by_identity(db, table, identity)

            if action_type == "update":
                if current is None:
                    stale_or_conflicting.append({"record_id": record_id, "reason": "current_record_missing"})
                    continue
                restore_values = {
                    name: value
                    for name, value in original.items()
                    if name in table.c
                    and name not in primary_key_names
                    and name not in {"created_at", "updated_at"}
                }
                operations.append(
                    {
                        "mode": "restore_update",
                        "record_id": record_id,
                        "primary_key": {name: json_safe(value) for name, value in identity.items()},
                        "restore_values": restore_values,
                        "current_record": current,
                        "original_before_record": original,
                    }
                )
            else:  # delete
                if current is not None:
                    stale_or_conflicting.append({"record_id": record_id, "reason": "record_already_exists"})
                    continue
                insert_values = {name: value for name, value in original.items() if name in table.c}
                operations.append(
                    {
                        "mode": "restore_delete",
                        "record_id": record_id,
                        "primary_key": {name: json_safe(value) for name, value in identity.items()},
                        "restore_values": insert_values,
                        "current_record": None,
                        "original_before_record": original,
                    }
                )

        if stale_or_conflicting:
            raise AuditRollbackError(
                status=ResponseStatus.CLARIFICATION_REQUIRED,
                code="rollback_state_changed_since_original_action",
                message="Current records no longer match the safe rollback preconditions. No rollback preview was created.",
                details=stale_or_conflicting,
            )
        if not operations:
            raise AuditRollbackError(
                status=ResponseStatus.INFORMATION_NOT_AVAILABLE,
                code="rollback_no_records_available",
                message="No records are available for a safe rollback.",
            )
        return {
            "original_action_log_id": action.id,
            "original_action_type": action_type,
            "target_table": table.name,
            "operation_count": len(operations),
            "operations": operations,
            "raw_sql_accepted": False,
            "rollback_policy": "restores only audited before-snapshot data through a stored confirmation-gated plan",
        }

    def list_actions(
        self,
        db: DbSession,
        *,
        limit: int = 50,
        action_type: str | None = None,
        target_table: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        statement = select(ActionLog)
        if action_type:
            statement = statement.where(ActionLog.action_type == action_type.strip().lower())
        if target_table:
            statement = statement.where(ActionLog.target_table == target_table.strip().lower())
        if status:
            statement = statement.where(ActionLog.status == status.strip().lower())
        statement = statement.order_by(ActionLog.created_at.desc()).limit(limit)
        actions = list(db.scalars(statement).all())
        counts = {
            action.id: int(db.scalar(select(func.count(ChangeSnapshot.id)).where(ChangeSnapshot.action_log_id == action.id)) or 0)
            for action in actions
        }
        return [action_log_to_dict(action, snapshot_count=counts.get(action.id, 0)) for action in actions]

    def get_action_detail(self, db: DbSession, *, action_log_id: int) -> dict[str, Any]:
        action, snapshots = self._action_with_snapshots(db, action_log_id)
        return {
            "action": action_log_to_dict(action, snapshot_count=len(snapshots)),
            "snapshots": [_serialize_snapshot(snapshot) for snapshot in snapshots],
            "rollback_supported": (action.action_type or "").strip().lower() in ROLLBACK_SUPPORTED_ACTIONS and action.status == "success" and bool(self._snapshot_by_type(snapshots, "before")),
            "raw_sql_accepted": False,
        }

    def preview_rollback(
        self,
        db: DbSession,
        *,
        original_action_log_id: int,
        session_id: UUID,
        actor_role: UserRole,
        ttl_minutes: int = 30,
    ) -> RollbackPreviewResult:
        self._require_admin(actor_role)
        self._require_active_admin_session(db, session_id)
        action, snapshots = self._action_with_snapshots(db, original_action_log_id)
        plan = self._build_plan(db, action, snapshots)
        preview = {
            "summary": f"Preview: restore {plan['operation_count']} record(s) affected by audited {plan['original_action_type'].upper()} action #{action.id}. Explicit admin confirmation is required.",
            "requires_confirmation": True,
            "original_action_log_id": action.id,
            "target_table": plan["target_table"],
            "operation_count": plan["operation_count"],
            "operations": plan["operations"],
            "raw_sql_accepted": False,
        }
        pending = create_pending_action(
            db,
            session_id=session_id,
            action_type="rollback",
            target_table=plan["target_table"],
            validated_payload={"mode": "rollback", "rollback_plan": plan},
            preview_data=preview,
            generated_sql=None,
            ttl_minutes=ttl_minutes,
        )
        return RollbackPreviewResult(
            pending_action=pending_action_to_dict(pending),
            original_action=action_log_to_dict(action, snapshot_count=len(snapshots)),
            rollback_plan=plan,
            created_session_id=None,
        )

    @staticmethod
    def _lock_pending_rollback(db: DbSession, *, session_id: UUID, pending_action_id: UUID) -> PendingAction:
        action = db.scalar(
            select(PendingAction)
            .where(PendingAction.id == pending_action_id, PendingAction.session_id == session_id)
            .with_for_update()
        )
        if action is None:
            raise AuditRollbackError(
                status=ResponseStatus.INFORMATION_NOT_AVAILABLE,
                code="rollback_pending_action_not_found",
                message="No rollback pending action exists for this session.",
            )
        if action.action_type != "rollback":
            raise AuditRollbackError(
                status=ResponseStatus.VALIDATION_FAILED,
                code="rollback_action_type_required",
                message="The supplied pending action is not a rollback plan.",
            )
        if action.status == "confirmed":
            return action
        if action.status != "pending":
            raise AuditRollbackError(
                status=ResponseStatus.VALIDATION_FAILED,
                code="rollback_pending_action_not_confirmable",
                message=f"This rollback action is '{action.status}' and cannot be confirmed.",
            )
        if action.expires_at <= _utc_now():
            action.status = "expired"
            db.commit()
            raise AuditRollbackError(
                status=ResponseStatus.VALIDATION_FAILED,
                code="rollback_confirmation_expired",
                message="The rollback preview expired before confirmation and was not executed.",
            )
        return action

    @classmethod
    def _add_snapshot(cls, db: DbSession, *, action_log_id: int, table_name: str, record: dict[str, Any], snapshot_type: str) -> None:
        table = cls._business_table(table_name, db)
        identity = {
            column.name: json_safe(record[column.name])
            for column in table.primary_key.columns
            if record.get(column.name) is not None
        }
        record_id = json.dumps(identity, sort_keys=True, default=str)
        db.add(
            ChangeSnapshot(
                action_log_id=action_log_id,
                table_name=table_name,
                record_id=record_id,
                snapshot_type=snapshot_type,
                snapshot_data={name: json_safe(value) for name, value in record.items()},
                created_at=_now(),
            )
        )

    def _create_rollback_log(
        self,
        db: DbSession,
        *,
        request_id: str,
        session_id: UUID,
        pending_action_id: UUID,
        target_table: str,
        original_action_log_id: int,
    ) -> ActionLog:
        log = ActionLog(
            request_id=request_id,
            session_id=session_id,
            pending_action_id=pending_action_id,
            actor_role=UserRole.ADMIN.value,
            action_type="rollback",
            target_table=target_table,
            affected_record_ids={"original_action_log_id": original_action_log_id},
            generated_sql=None,
            confirmation_status="confirmed",
            status="executing",
            error_message=None,
            created_at=_now(),
        )
        db.add(log)
        db.flush()
        return log

    def confirm_rollback(
        self,
        db: DbSession,
        *,
        session_id: UUID,
        pending_action_id: UUID,
        actor_role: UserRole,
        request_id: str,
    ) -> RollbackConfirmationResult:
        self._require_admin(actor_role)
        self._require_active_admin_session(db, session_id)
        pending = self._lock_pending_rollback(db, session_id=session_id, pending_action_id=pending_action_id)
        payload = dict(pending.validated_payload or {})
        plan = dict(payload.get("rollback_plan") or {})
        original_action_log_id = int(plan.get("original_action_log_id") or 0)
        if pending.status == "confirmed":
            existing = db.scalar(
                select(ActionLog)
                .where(ActionLog.pending_action_id == pending.id, ActionLog.action_type == "rollback")
                .order_by(ActionLog.id.desc())
                .limit(1)
            )
            return RollbackConfirmationResult(
                pending_action=pending_action_to_dict(pending),
                rollback_action_log_id=existing.id if existing else None,
                original_action_log_id=original_action_log_id,
                restored_record_count=0,
                before_snapshot_count=0,
                after_snapshot_count=0,
                idempotent=True,
            )
        if payload.get("mode") != "rollback" or not original_action_log_id:
            raise AuditRollbackError(
                status=ResponseStatus.VALIDATION_FAILED,
                code="stored_rollback_plan_missing",
                message="The stored rollback plan is incomplete or unsafe.",
            )

        original_action, original_snapshots = self._action_with_snapshots(db, original_action_log_id)
        fresh_plan = self._build_plan(db, original_action, original_snapshots)
        target_table = fresh_plan["target_table"]
        table = self._business_table(target_table, db)
        rollback_log: ActionLog | None = None
        before_rows: list[dict[str, Any]] = []
        after_rows: list[dict[str, Any]] = []
        try:
            rollback_log = self._create_rollback_log(
                db,
                request_id=request_id,
                session_id=session_id,
                pending_action_id=pending.id,
                target_table=target_table,
                original_action_log_id=original_action_log_id,
            )
            for operation in fresh_plan["operations"]:
                identity = dict(operation.get("primary_key") or {})
                mode = operation["mode"]
                if mode == "restore_update":
                    current = self._row_by_identity(db, table, identity)
                    if current is None:
                        raise AuditRollbackError(
                            status=ResponseStatus.CLARIFICATION_REQUIRED,
                            code="rollback_state_changed_since_preview",
                            message="A record selected for rollback no longer exists. Create a new rollback preview.",
                        )
                    before_rows.append(current)
                    self._add_snapshot(db, action_log_id=rollback_log.id, table_name=target_table, record=current, snapshot_type="before")
                    restore_values = dict(operation["restore_values"])
                    db.execute(table.update().where(self._identity_filter(table, identity)).values(**restore_values))
                    restored = self._row_by_identity(db, table, identity)
                    if restored is None:
                        raise AuditRollbackError(
                            status=ResponseStatus.DATABASE_UNAVAILABLE,
                            code="rollback_restore_verification_failed",
                            message="PostgreSQL did not return the restored record after rollback.",
                        )
                    after_rows.append(restored)
                    self._add_snapshot(db, action_log_id=rollback_log.id, table_name=target_table, record=restored, snapshot_type="after")
                elif mode == "restore_delete":
                    current = self._row_by_identity(db, table, identity)
                    if current is not None:
                        raise AuditRollbackError(
                            status=ResponseStatus.CLARIFICATION_REQUIRED,
                            code="rollback_state_changed_since_preview",
                            message="A deleted record now exists again. Create a new rollback preview instead of overwriting it.",
                        )
                    restore_values = dict(operation["restore_values"])
                    db.execute(table.insert().values(**restore_values))
                    restored = self._row_by_identity(db, table, identity)
                    if restored is None:
                        raise AuditRollbackError(
                            status=ResponseStatus.DATABASE_UNAVAILABLE,
                            code="rollback_restore_verification_failed",
                            message="PostgreSQL did not return the restored deleted record after rollback.",
                        )
                    after_rows.append(restored)
                    self._add_snapshot(db, action_log_id=rollback_log.id, table_name=target_table, record=restored, snapshot_type="after")
                else:
                    raise AuditRollbackError(
                        status=ResponseStatus.VALIDATION_FAILED,
                        code="rollback_plan_operation_invalid",
                        message="The stored rollback operation is not recognized.",
                    )

            pending.status = "confirmed"
            pending.confirmed_at = _utc_now()
            rollback_log.status = "success"
            rollback_log.affected_record_ids = {
                "original_action_log_id": original_action_log_id,
                "restored_record_count": len(after_rows),
                "before_snapshot_count": len(before_rows),
                "after_snapshot_count": len(after_rows),
            }
            db.commit()
            db.refresh(pending)
            return RollbackConfirmationResult(
                pending_action=pending_action_to_dict(pending),
                rollback_action_log_id=rollback_log.id,
                original_action_log_id=original_action_log_id,
                restored_record_count=len(after_rows),
                before_snapshot_count=len(before_rows),
                after_snapshot_count=len(after_rows),
                idempotent=False,
            )
        except AuditRollbackError:
            db.rollback()
            raise
        except IntegrityError as exc:
            db.rollback()
            raise AuditRollbackError(
                status=ResponseStatus.DUPLICATE_DETECTED,
                code="rollback_unique_constraint_conflict",
                message="PostgreSQL blocked the rollback because a unique record conflict now exists.",
            ) from exc
        except SQLAlchemyError as exc:
            db.rollback()
            raise AuditRollbackError(
                status=ResponseStatus.DATABASE_UNAVAILABLE,
                code="rollback_transaction_failed",
                message="PostgreSQL could not complete the confirmed rollback transaction.",
            ) from exc


audit_rollback_service = AuditRollbackService()
