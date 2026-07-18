# Phase 10 — LangGraph Router and Four-Route Agent Orchestration

Phase 10 adds one unified agent entry point:

```text
POST /api/agent/query
```

The workflow is built using LangGraph `StateGraph` nodes and conditional edges:

```text
START
  ↓
Router
  ├─ structured_read → validated Phase 6 SQL/MCP read → Finalize
  ├─ crud_write     → Phase 7 confirmation preview → Finalize
  ├─ document_rag   → Phase 9 local ChromaDB answer → Finalize
  └─ hybrid         → preliminary document evidence → Finalize
```

## Safety boundaries preserved

- The router is deterministic and rule-first. It does not call a cloud or local LLM merely to decide a route.
- Structured reads reuse the existing Phase 6 validator and MCP `execute_validated_read` tool.
- CRUD requests reuse Phase 7 and can only create a pending confirmation action. No agent request can execute a business write directly.
- Document answers reuse Phase 9, where the LLM answers only from retrieved filename/page/chunk evidence.
- Hybrid intent is recognized in Phase 10, but Phase 10 does **not** create a combined business conclusion. Phase 11 adds verified database/document evidence fusion and the final hybrid answer.

## Endpoints

```text
GET  /api/agent/status
POST /api/agent/query
```

## Example requests

### Structured read

```json
{"question":"Show workers from Bangalore"}
```

### CRUD preview

```json
{"question":"Add employee Maya from Bangalore working for Neolotex"}
```

A session is created automatically only when a write preview needs one. The response always requires explicit confirmation through the Phase 7 endpoint before execution.

### Document RAG

```json
{"question":"What warranty is mentioned for TextileBot X?"}
```

### Hybrid route preparation

```json
{"question":"Which vendor offers textile automation products under 5 lakh according to the brochure?"}
```

This response is intentionally transparent that the final joined answer is a Phase 11 feature.
