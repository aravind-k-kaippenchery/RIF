# Phase 7 — CRUD Write Path, Duplicate Detection, Confirmation, and Snapshots

Phase 7 adds the first controlled database write path. It does **not** allow an LLM,
frontend, or MCP client to run arbitrary SQL.

## Safety sequence

```text
DML proposal
→ Phase 4 AST validator
→ approved business-table check
→ duplicate detection
→ affected-row / child-record preview
→ pending_actions row
→ explicit confirmation
→ one PostgreSQL transaction
→ action_logs + snapshots
→ response
```

## New endpoints

- `POST /api/crud/propose` — preview one already-generated `INSERT`, `UPDATE`, or `DELETE`.
- `POST /api/crud/propose-from-prompt` — ask local Llama 3 for a DML proposal, then preview only.
- `POST /api/crud/bulk-propose` — preview up to 50 parameterized INSERT records after duplicate checks.
- `POST /api/crud/actions/{pending_action_id}/confirm` — execute the exact stored proposal.
- `POST /api/crud/actions/{pending_action_id}/cancel` — cancel safely.

## Guarantees

- Every write requires a valid active `session_id` and the same temporary role that created it.
- Confirmation does not accept new SQL. It uses only the SQL or records stored in the pending action.
- `INSERT` checks known unique business keys before preview and again at confirmation.
- Batch records are checked for duplicate keys within the batch and against PostgreSQL.
- `UPDATE` stores **before** and **after** snapshots.
- `DELETE` stores **before** snapshots.
- Deleting an employee with `employee_permissions` is blocked before confirmation because the relationship uses `ON DELETE RESTRICT`.
- Confirmation is idempotent: confirming the same completed action twice does not execute it twice.
- No migration is needed because `pending_actions`, `action_logs`, and `change_snapshots` were created in Phase 2.

## Demo sequence

1. Create a session with `POST /api/sessions`.
2. Call `POST /api/crud/propose` using a safe UPDATE or INSERT SQL proposal.
3. Inspect the preview and `pending_action_id`.
4. Confirm it with the same `session_id`.
5. Re-run the confirmation endpoint once to prove idempotency.
6. Use a duplicate INSERT to prove it is blocked.
7. Propose deleting EMP-103 to prove the child-permission safety preview blocks the action.
