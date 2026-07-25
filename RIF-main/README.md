# RIF — Local Agentic AI B2B Product Intelligence Assistant

A local-first proof of concept that lets a user query a PostgreSQL business database and
uploaded documents in natural language, get evidence-grounded answers, and perform
confirmation-gated CRUD — all routed through LangGraph and a hardened local MCP tool
boundary, with a local Llama 3 model served by Ollama.

This is a monorepo with two projects:

- **`backend/`** — FastAPI + SQLAlchemy + Alembic + LangGraph + MCP + PostgreSQL +
  ChromaDB. See `backend/README.md` for setup, phase-by-phase feature notes, and how to
  run the app and test suite.
- **`B2B-frontend/`** — React 19 + TypeScript + Vite single-page app that consumes the
  backend API.

## Architecture at a glance

```
Ollama + Llama 3  →  LangGraph (route selection)  →  Local MCP tool boundary  →  PostgreSQL / ChromaDB
```

- The LLM never talks to the database directly; all reads go through an AST-validated,
  SELECT-only path, and all writes are confirmation-gated previews.
- Document answers cite filename/page/chunk; database answers cite tables; hybrid answers
  require an explicit product-identity match before database and document evidence are
  fused.
- No unrestricted SQL tool is exposed anywhere in the system.

## Quick start

```powershell
cd backend
python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/docs` for the interactive API docs, then see
`backend/README.md` for document upload, RAG indexing, and test-run instructions.

For the frontend:

```bash
cd B2B-frontend
npm install
npm run dev
```

## Status

This is a POC, not a production system. See `backend/docs/PHASE_16_FINAL_DEMO_RELEASE_GATE.md`
and the `/api/demo/report` endpoint for the full 17-feature matrix, current readiness
checks, and explicitly stated known limitations (temporary header-based role guard instead
of real auth, restricted admin-schema DDL surface, hardware-dependent local-model latency,
and other production hardening left as future work).
