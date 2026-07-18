# Phase 4 — SQL Validation Gate and Minimal MCP Core

## Purpose

Phase 4 creates the safety boundary that must exist before a local LLM is allowed to suggest SQL. It validates every proposal using a PostgreSQL SQL abstract syntax tree (AST), checks it against the controlled Phase 3 schema registry, and keeps all writes proposal-only.

## Normal-user SQL policy

The validator can accept a **single** proposal of one of these types:

- `SELECT`
- `INSERT`
- `UPDATE`
- `DELETE`

However, Phase 4 executes **only validated `SELECT` statements** through the `execute_validated_read` MCP tool. `INSERT`, `UPDATE`, and `DELETE` can be validated and stored as future confirmation proposals, but they cannot execute until Phase 7.

Always blocked:

- `DROP`, `TRUNCATE`, `GRANT`, `REVOKE`, `COPY`, `VACUUM`, `CALL`, `DO`
- comments (`--`, `/* ... */`)
- multiple statements
- PostgreSQL/system schema qualification
- unknown tables, aliases, or columns
- `SELECT *` (except `COUNT(*)`)
- unbounded reads: a server-controlled limit is added when missing
- `UPDATE` or `DELETE` without `WHERE`
- joins that do not match an approved foreign-key relationship

## Restricted admin schema previews

Admin requests are not executed in Phase 4. The preview endpoint permits only:

- `CREATE TABLE ...` with explicit allowed data types
- `ALTER TABLE <approved_table> ADD COLUMN ...`

Each preview returns validated normalized SQL and warnings that future execution requires a confirmation and audit workflow. Destructive schema changes such as `DROP COLUMN` remain blocked.

## MCP tools

The FastAPI application mounts a real local Streamable HTTP MCP server at:

```text
http://127.0.0.1:8000/mcp
```

Tools exposed:

1. `get_schema`
2. `validate_sql`
3. `execute_validated_read`
4. `create_pending_action`
5. `get_pending_action`

There is intentionally no `execute_any_sql` tool.

The REST verifier endpoints under `/api/mcp` use the same safe tool implementations, while `python -m app.mcp.verify_client` uses the official Python MCP client SDK to connect to the mounted MCP endpoint.

## Completion evidence

- validation endpoint accepts safe explicit-column `SELECT`
- validation endpoint blocks `DROP`, comments, unsafe writes, unknown schema names, and multiple statements
- admin-only schema preview blocks normal users
- MCP tool catalog is visible
- MCP client can list tools and call `get_schema`
- tests pass
