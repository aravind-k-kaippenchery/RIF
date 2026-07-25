# Phase 9 action-log hotfix

## Problem fixed

`POST /api/rag/index-existing` indexes several completed documents in one HTTP request. The Phase 2 database schema has a unique constraint on `action_logs.request_id`.

The original Phase 9 implementation reused the same HTTP request ID for every per-document indexing audit entry. PostgreSQL correctly rejected the second log with `uq_action_logs_request_id`.

## Fix

Each document indexing action now receives a unique event-level log request ID:

```text
<parent-request-id>:document_vector_index:<document-id>
```

The original HTTP request ID is retained as `source_request_id` in `affected_record_ids`, so audit correlation remains available.

## No database migration needed

This patch respects the existing Phase 2 unique constraint and does not change PostgreSQL schema, user data, uploaded files, ChromaDB data, OCR models, Ollama, or `.env`.
