# Phase 12 — MCP Expansion and Tool Hardening

## Goal

Phase 12 makes MCP the explicit, controlled tool boundary for the B2B assistant. It
adds safe frontend-ready table inspection and admin-schema-preview tools without
introducing an unrestricted SQL or direct DDL execution capability.

## MCP Tool Catalog

The local Streamable HTTP MCP server remains mounted at:

```text
http://127.0.0.1:8000/mcp
```

Phase 12 registers 15 tools:

```text
get_schema
list_tables
get_table_records
validate_sql
execute_validated_read
create_pending_action
get_pending_action
propose_write_action
execute_confirmed_write
cancel_pending_action
retrieve_docs
get_document_sources
get_verified_hybrid_evidence
create_schema_change_preview
apply_admin_schema_change
```

The following tools do **not** exist and must never be introduced:

```text
execute_any_sql
run_unrestricted_sql
drop_table
truncate_table
```

## New Bounded Table Tools

### `list_tables`

Returns only the 16 approved application tables from the schema registry. PostgreSQL
system tables, arbitrary schemas, and catalogs are not exposed.

The result marks business tables as readable by `normal_user` and operational/audit
tables as admin-only for record access.

### `get_table_records`

Inputs:

```text
table_name
limit: 1–100
offset: 0–100000
user_role
```

It accepts **no SQL string**. It uses SQLAlchemy Core over approved metadata and
returns JSON-safe rows from one table only. `normal_user` can read business tables;
`admin` is required for operational tables such as logs, sessions, snapshots, and
pending actions.

## Confirmed DML Boundary

`propose_write_action` can create a duplicate-checked write preview. Only
`execute_confirmed_write` can execute a previously stored pending action. A caller
cannot provide an arbitrary INSERT, UPDATE, or DELETE to an execution tool.

## Admin Schema Boundary

`create_schema_change_preview` is admin-only and permits only:

```text
CREATE TABLE with explicit allowed columns
ALTER TABLE ADD COLUMN
```

The tool validates and stores an auditable `schema_change_requests` record with
`pending_confirmation` status. It does not execute the SQL.

`apply_admin_schema_change` exists as a stable confirmation-shaped contract but is
hard-blocked in Phase 12. Even with `confirmed=true`, it returns
`admin_schema_execution_deferred` and performs no DDL. Phase 13 will add the
persistent admin approval and migration workflow.

## REST Verification APIs

```text
GET  /api/mcp/status
GET  /api/mcp/tools
GET  /api/mcp/tables
GET  /api/mcp/tables/{table_name}/records?limit=50&offset=0
POST /api/mcp/validate-sql
POST /api/mcp/execute-read
POST /api/mcp/schema-changes/preview
POST /api/mcp/schema-changes/{schema_change_id}/apply
```

Use the temporary header below for admin-only operations:

```text
X-User-Role: admin
```

## Safety Guarantees

- No raw SQL is accepted by table-inspection tools.
- Read pagination is server-bounded to 100 records per request.
- Operational/audit records are protected by the temporary admin role gate.
- All business writes remain confirmation-gated and idempotent.
- Local document retrieval remains local-only.
- Hybrid evidence remains explicit-product-identity-based.
- Schema DDL remains preview-only and cannot be directly executed from MCP.
- Tool errors are returned in the shared structured error envelope.
