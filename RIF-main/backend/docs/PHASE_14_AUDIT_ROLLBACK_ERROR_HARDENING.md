# Phase 14 — Audit Hardening, Rollback, Idempotency, and Structured Errors

## Goal

Phase 14 turns the existing write audit trail into an operator-safe recovery workflow.
It exposes bounded admin audit history and change snapshots, then allows an admin to
create and confirm a rollback **only** from stored before-snapshot evidence.

The system remains local-first and does not accept raw SQL for audit or rollback.

## What Is Added

- Admin-only action-audit list and detail APIs.
- Admin-only before/after snapshot APIs.
- Rollback preview stored as a `pending_actions` record.
- Explicit admin confirmation before rollback execution.
- Current-state precondition check immediately before restore.
- A new `action_logs` record for every completed rollback.
- New before/after snapshots for the rollback operation itself.
- Idempotent rollback confirmation: repeated confirmation does not execute a second restore.
- Controlled error responses for missing audits, expired rollbacks, changed state, unique conflicts, and database failures.

## New APIs

```text
GET  /api/audit/status                         admin only
GET  /api/audit/actions                        admin only
GET  /api/audit/actions/{action_log_id}        admin only
GET  /api/audit/actions/{action_log_id}/snapshots
                                                admin only
POST /api/audit/actions/{action_log_id}/rollback/preview
                                                admin only
POST /api/audit/rollback-actions/{pending_action_id}/confirm
                                                admin only
```

## Rollback Boundary

Supported original actions:

```text
UPDATE
DELETE
```

Not supported in this phase:

```text
INSERT
BULK_INSERT
schema changes
unlogged manual database edits
```

`INSERT` compensation is deferred because an automatic delete could remove a record
that has acquired later business relationships. The system returns a controlled
`rollback_not_supported_for_action_type` response instead of guessing.

## Safe Rollback Flow

```text
Successful audited UPDATE or DELETE
↓
Before snapshots stored in change_snapshots
↓
Admin requests rollback preview
↓
Backend verifies action type, target table, snapshots, session role, and current state
↓
Rollback plan stored as pending_actions (no data changed)
↓
Admin explicitly confirms the stored pending action
↓
Backend rechecks current state
↓
SQLAlchemy Core restore transaction
↓
New action_logs record + rollback snapshots
↓
Grounded response with original action ID and rollback action ID
```

## Safety Rules

- Rollback requires the `admin` role and an active admin session.
- The confirm request contains only `session_id` and `confirmed`; it never accepts caller-provided SQL.
- Only approved business tables can be restored.
- Only successful original actions with `before` snapshots can be considered.
- The current record state is checked both at preview time and again at confirmation time.
- A record that is missing for an UPDATE rollback, or already exists for a DELETE restore, produces `rollback_state_changed_since_*` rather than overwriting data.
- PostgreSQL unique constraint conflicts are returned as `rollback_unique_constraint_conflict`.
- A second confirm returns an idempotent result and does not restore data again.

## No Database Migration

Phase 14 uses existing tables created earlier:

```text
action_logs
change_snapshots
pending_actions
sessions
```

Do not run Alembic migrations or seed commands for this phase.

## Demo Proof

Use an approved temporary business-field update, then restore it:

```text
1. Create and confirm a normal UPDATE preview for one vendor.
2. Note the successful action_log_id and verify the changed value.
3. Create Phase 14 rollback preview for that action_log_id.
4. Confirm the stored rollback using its pending_action_id and admin session.
5. Verify the original value is restored.
6. Confirm a second time and observe idempotent=true.
7. Read the original and rollback action logs/snapshots as admin.
```
