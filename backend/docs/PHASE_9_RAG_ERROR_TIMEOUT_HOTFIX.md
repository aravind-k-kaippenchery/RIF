# Phase 9 RAG error-route and local timeout hotfix

## What this fixes

A grounded RAG request can fail when local Ollama takes longer than the configured HTTP timeout. The RAG route correctly converts this into a `DocumentRagError`, but it previously passed `route=AgentRoute.DOCUMENT_RAG` into `AppError` even though `AppError` did not accept that argument. This caused a Python traceback instead of the standard API error envelope.

## Changes

- `AppError` now accepts an optional response route.
- The global application-error handler preserves that route in the JSON response.
- A local Ollama timeout from `POST /api/rag/query` now returns a controlled response:

```json
{
  "status": "llm_unavailable",
  "route": "document_rag",
  "error": {
    "code": "ollama_request_failed",
    "message": "Local Ollama could not complete the generation request."
  }
}
```

## Required local configuration

Set `OLLAMA_REQUEST_TIMEOUT_SECONDS=600` in `backend/.env` for a CPU-based local Llama 3 8B workflow. Restart Uvicorn after changing `.env`.

This does not change PostgreSQL, ChromaDB, document chunks, uploaded files, dependencies, or database migrations.
