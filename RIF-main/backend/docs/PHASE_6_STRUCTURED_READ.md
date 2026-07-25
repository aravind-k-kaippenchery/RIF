# Phase 6 — Structured Read Path

## Goal

Phase 6 is the first end-to-end business-question workflow that reads real PostgreSQL data. It remains strictly read-only.

```text
Natural-language question
    ↓
Phase 3 business glossary + controlled schema
    ↓
Phase 5 local Ollama SQL proposal
    ↓
Phase 4 AST SQL validator
    ↓
Phase 4 MCP execute_validated_read tool
    ↓
PostgreSQL SELECT
    ↓
Deterministic grounded response + source tables + query history record
```

## Endpoint

```text
POST /api/structured-read
```

Request:

```json
{
  "question": "Show workers from Bangalore"
}
```

The glossary maps `workers` to `employees`. The local model can propose SQL, but it cannot execute it. Only `execute_validated_read` executes a validated `SELECT` through the MCP tool boundary.

## Safety guarantees

- Only `SELECT` can execute.
- The generated SQL is validated twice: once immediately after generation and again inside the read tool.
- The validator enforces approved tables/columns, one statement, no comments, no unsafe joins, and server-controlled row limits.
- `INSERT`, `UPDATE`, `DELETE`, and all schema changes remain non-executing in this phase.
- The natural-language answer is deterministic from the returned row count. It does not make a second LLM call and cannot invent records.
- No matching rows return `information_not_available` with exactly: `Information not available in the current database.`
- Each response cites the PostgreSQL table(s) used.

## Query history

A successful or no-data structured read attempts to write a record to `query_logs`. This is best effort: a query result is not hidden if optional history logging is temporarily unavailable. The normal request JSON log remains active through the Phase 1 middleware.

## Phase 6 scope boundary

This phase does not add LangGraph, document retrieval, ChromaDB, OCR, write confirmation, duplicate handling, or actual database writes. Those remain later phases.
