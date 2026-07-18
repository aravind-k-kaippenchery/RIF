# Phase 5 — Local Ollama Adapter and Structured LLM Service

## Goal

Phase 5 connects the backend to a **local Ollama server** and introduces the only application service allowed to contact the local model API: `app/services/llm_service.py`.

This is deliberately **not** the full agent, LangGraph router, RAG system, or CRUD executor.

## Safety boundary

```text
FastAPI request
    ↓
LLMService
    ↓ local HTTP only
Ollama /api/chat
    ↓ structured JSON only
Pydantic validation
    ↓
Phase 4 SQL validator (for SQL proposals)
    ↓
JSON response
```

The model:

- does not receive PostgreSQL credentials;
- does not connect directly to PostgreSQL, ChromaDB, MCP, files, or the network;
- cannot execute SQL;
- cannot execute INSERT, UPDATE, DELETE, CREATE, ALTER, DROP, or TRUNCATE;
- returns Pydantic JSON only;
- receives at most **two total calls** for a request: one initial attempt and one correction attempt.

## Real Phase 5 operations

| Method | Purpose | Can execute data changes? |
|---|---|---|
| `check_llm_health()` | Checks local Ollama and model availability | No |
| `generate_json()` | Calls Ollama with a Pydantic JSON schema | No |
| `classify_intent()` | Classifies a future route | No |
| `generate_sql()` | Generates a SQL proposal and passes it through Phase 4 validation | No |
| `extract_record_fields()` | Extracts a proposed row payload | No |
| `generate_grounded_answer()` | Answers only from caller-provided evidence | No |

## Endpoints

```text
GET  /api/llm/status
POST /api/llm/classify-intent
POST /api/llm/generate-sql
POST /api/llm/extract-record
POST /api/llm/generate-grounded-answer
```

### `POST /api/llm/generate-sql`

This endpoint is only a **proposal generator**.

1. Backend sends controlled schema and glossary context to Ollama.
2. Ollama returns JSON containing `route`, `sql`, and a short explanation.
3. Python validates the JSON with Pydantic.
4. Phase 4's AST-based SQL validator validates the SQL.
5. Invalid SQL gets at most one correction retry.
6. The endpoint returns the proposal. It never executes it.

### `POST /api/llm/generate-grounded-answer`

This is a Phase 5 grounding demonstration, not the final RAG route.

- Evidence is supplied in the request by the backend/test caller.
- Every cited reference must be one of the supplied evidence references.
- With no evidence, the server returns `information_not_available` without calling Ollama.
- Full document ingestion and ChromaDB retrieval come later.

## Local configuration

The local `.env` must have an Ollama model name that was actually pulled:

```env
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_MODEL=llama3:8b
OLLAMA_REQUEST_TIMEOUT_SECONDS=120
OLLAMA_KEEP_ALIVE=5m
OLLAMA_TEMPERATURE=0.0
OLLAMA_MAX_ATTEMPTS=2
```

The service rejects non-loopback URLs, so it cannot silently switch to a cloud model endpoint.

## What comes next

Phase 6 will turn **validated SQL proposals** into a structured-read workflow. It will use:

```text
question
→ glossary + controlled schema
→ local LLM SQL proposal
→ Phase 4 validator
→ MCP execute_validated_read
→ PostgreSQL rows
→ grounded response
```

Phase 5 never performs that execution path itself.
