# B2B Product Intelligence Assistant — Backend

Local-first backend for the Agentic AI B2B Product Intelligence POC.

## Completed phases

- **Phase 1:** FastAPI foundation, request IDs, response envelope, local logging, and a temporary role guard.
- **Phase 2:** PostgreSQL, migrations, 16 application tables, seeded business data, and the `employees → employee_permissions` relationship.
- **Phase 3:** Controlled schema registry, business glossary, sessions, and pending actions.
- **Phase 4:** AST-based SQL validation, SELECT-only validated reads, and a local Streamable HTTP MCP core.
- **Phase 5:** Local Ollama adapter, Pydantic structured outputs, SQL-proposal validation, record-proposal extraction, and evidence-only answers.
- **Phase 6:** Grounded structured read path using Llama 3, SQL validation, MCP, and PostgreSQL.
- **Phase 7:** Confirmation-gated CRUD previews, duplicate checks, snapshots, action logs, and parent/child delete protection.
- **Phase 8:** Local document upload, native text extraction, PaddleOCR for scans, and document-ingestion audit records.
- **Phase 9:** Local ChromaDB persistence, local sentence-transformer embeddings, page-aware chunking, semantic retrieval, and evidence-grounded document answers.

## Run locally

```powershell
cd backend
.\venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/docs`.

## Phase 8 document upload

```powershell
curl.exe -X POST "http://127.0.0.1:8000/api/documents/upload" `
  -F "file=@C:\path\to\brochure.pdf"
```

Native TXT, DOCX, and text PDFs are extracted locally. JPG/PNG and scanned PDF pages use local PaddleOCR. Use Phase 9 /api/rag/index-existing to vector-index completed documents, then /api/rag/query for grounded answers.

## Tests

```powershell
python -m pytest -q
```

## Important local files

Do not commit:

```text
.env
venv/
logs/*.log
uploads/*
data/chroma/
```

## Phase 13

Phase 13 completes the LangGraph `hybrid` route. It retrieves local ChromaDB document chunks, verifies product identity against PostgreSQL through the restricted MCP tool `get_verified_hybrid_evidence`, and returns vendor/product/quoted-price facts only when the retrieved document explicitly names the product.

The final response includes:

- document filename/page/chunk source references
- PostgreSQL evidence from `vendors`, `products`, and `product_vendor_mappings`
- a deterministic answer built only from verified evidence
- `information_not_available` when either source cannot be verified


## Phase 13 — MCP Expansion and Tool Hardening

Phase 13 provides the stable controlled-tool boundary used by the local agent. The MCP
server exposes approved schema access, bounded table inspection, validated reads,
confirmation-gated writes, local document retrieval, verified hybrid evidence, and
admin-only schema previews. It intentionally exposes **no** unrestricted SQL tool.

New hardened MCP tools:

- `list_tables` — approved application-table catalog only
- `get_table_records` — role-aware bounded reads, with a 100-row server limit
- `create_schema_change_preview` — admin-only `CREATE TABLE` / `ALTER TABLE ADD COLUMN` preview and audit record
- `apply_admin_schema_change` — confirmation-shaped request that deliberately blocks direct DDL until Phase 13

Operational/audit tables require the temporary `X-User-Role: admin` header for record reads.


## Phase 13

Phase 13 adds frontend-ready API adapters, persisted short-term session memory, and a
restricted admin schema workflow. See `docs/PHASE_13_FRONTEND_MEMORY_ADMIN_SCHEMA.md`.

Important: `POST /api/query` uses the existing LangGraph routes but creates/reuses a
short-term session. `POST /api/admin/schema-changes/propose-from-prompt` only creates a
validator-approved preview; a second explicit confirmation request is required before the
narrow allowed DDL subset can execute.


## Phase 15 — Audit Hardening and Rollback

Phase 15 adds admin-only action-audit and snapshot inspection, plus an explicit
confirmation-gated rollback workflow for successful audited `UPDATE` and `DELETE`
actions. Rollback reads only stored before-snapshot evidence, rechecks the current
PostgreSQL state before restoration, and never accepts raw SQL. See
`docs/PHASE_14_AUDIT_ROLLBACK_ERROR_HARDENING.md`.


## Phase 15 — Benchmarking, Quality Metrics, and Final Safety Evaluation

Admin-only benchmark endpoints store local latency, quality, security-blocking, OCR/ingestion-history, and resource metrics in `benchmark_runs`. Benchmark requests never execute business-table writes or accept raw SQL.


## Local Fallback Model

The backend can retry generation against a second local model (`OLLAMA_FALLBACK_MODEL`,
default `llama3.2:3b`) if the primary model is unreachable, not installed, times out, or
errors. This stays fully local -- the fallback model is served by the same loopback
Ollama instance and is subject to the same local-only endpoint guard. It does not fall
back for output-validation failures, and it does not add a cloud dependency. See
`docs/LOCAL_FALLBACK_MODEL.md`.

## Phase 16 — Final Integration and Demo Readiness

Phase 16 adds reviewer-ready non-destructive endpoints: `/api/demo/status`,
`/api/demo/features`, `/api/demo/scenarios`, and admin-only `/api/demo/readiness`,
`/api/demo/report`, and `/api/demo/smoke`. The feature matrix covers all 17 required
POC capabilities. These endpoints never accept raw SQL, run a model generation, or
execute a business-table write. See `docs/PHASE_16_FINAL_DEMO_RELEASE_GATE.md`.
