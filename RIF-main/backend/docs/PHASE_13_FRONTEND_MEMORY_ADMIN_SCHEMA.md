# Phase 13 — Frontend APIs, Short-Term Memory, and Restricted Admin Schema Workflow

Phase 13 makes the local backend directly usable by a frontend while retaining the earlier
MCP, SQL-validation, confirmation, and source-grounding boundaries.

## Added frontend API surface

- `POST /api/query` — frontend-friendly natural-language query endpoint. It creates or reuses a short-term session, loads bounded persisted memory, invokes the existing LangGraph graph, and stores the result in `query_logs`.
- `GET /api/tables` — controlled table directory through the `list_tables` MCP tool.
- `GET /api/tables/{table_name}/records` — bounded table viewer through the `get_table_records` MCP tool; no raw SQL input is accepted.
- `GET /api/documents` and `GET /api/documents/{document_id}` — existing Phase 8 document viewer endpoints remain frontend-ready.
- `GET /api/logs` — bounded admin-only query/action history.
- `GET /api/sessions/{session_id}/history` — persisted short-term query/action context.
- `DELETE /api/sessions/{session_id}` — closes a session without deleting its audit history.

## Short-term memory

No external memory service is used. The backend reuses the existing operational tables:

- `query_logs` stores user prompts, route, generated SQL, status, source references, and a frontend-safe answer reference.
- `action_logs` stores confirmation, cancellation, write, and schema-change events.

For `POST /api/query`, the backend reads at most six previous query events from the same
session and passes only a compact safe context to the structured-read prompt. The context
contains previous prompts, routes, validated SQL, and source references. It deliberately
excludes credentials, hidden reasoning, raw database rows, and full document text.

## Restricted admin schema workflow

The only schema operations that can execute are those already allowed by the existing AST
validator:

- `CREATE TABLE` with simple explicit columns
- `ALTER TABLE <approved table> ADD COLUMN <simple column>`

Workflow:

```text
Admin natural-language request
  -> local Llama proposal (`POST /api/admin/schema-changes/propose-from-prompt`)
  -> AST validation
  -> persisted `schema_change_requests` preview
  -> explicit `confirmed: true`
  -> stored SQL revalidated
  -> one restricted PostgreSQL DDL transaction
  -> action_logs audit record
```

The confirmation API never accepts raw SQL. It accepts only a stored `schema_change_id`.
`DROP`, `TRUNCATE`, `ALTER DROP COLUMN`, grants, revokes, comments, schema-qualified names,
and multiple statements remain blocked.

For a production deployment, a reviewer should also capture approved schema changes as
version-controlled Alembic migrations. This POC executes only its narrow validator-approved
DDL subset so the frontend demo can prove the confirmation/audit workflow end-to-end.

## Validation highlights

- Memory is bounded and session-scoped.
- Frontend table reads use the existing MCP boundary and maximum 100-row page limit.
- Operational and audit tables require the temporary admin role.
- Admin-created tables are also admin-only in the table viewer.
- The schema execution tool accepts no caller-provided DDL at confirmation time.
- Repeated confirmation of an already executed schema request returns an idempotent audit state.
