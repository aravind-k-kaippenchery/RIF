# Phase 3 — Schema Intelligence and Session Service

Phase 3 does not install Ollama, ChromaDB, LangGraph, MCP, or OCR. It prepares the backend so those later systems can work safely.

## What this phase adds

1. Controlled schema registry for approved application tables.
2. Column metadata for LLM prompt construction.
3. Relationship metadata, including `employees → employee_permissions`.
4. Business glossary, such as `workers → employees`, `suppliers → vendors`, and `clients → customers`.
5. Session service for short-term conversation state.
6. Pending action service for future confirmation workflows.

## Why it matters

Future LLM SQL generation must not guess table names or columns. The schema registry gives the LLM only the approved tables and fields. Future confirmation workflows also need a stable place to store a pending action when the user says: "yes, confirm it".

## Key endpoints

```text
GET  /api/schema
GET  /api/schema/tables
GET  /api/schema/tables/{table_name}
GET  /api/schema/relationships
GET  /api/schema/glossary
GET  /api/schema/glossary/resolve?term=workers
GET  /api/schema/glossary/normalize?text=show workers from bangalore
POST /api/sessions
GET  /api/sessions/{session_id}
POST /api/sessions/{session_id}/pending-actions
GET  /api/sessions/{session_id}/pending-actions
GET  /api/sessions/{session_id}/pending-actions/{action_id}
POST /api/sessions/{session_id}/pending-actions/{action_id}/expire
```

## Completion checks

- `/api/schema` returns controlled schema with approved tables.
- `/api/schema/glossary/resolve?term=workers` maps to `employees`.
- `/api/schema/relationships` includes `employee_permissions.employee_id → employees.id`.
- A session can be created.
- A pending action can be stored under that session.
- The pending action can be retrieved and expired.
