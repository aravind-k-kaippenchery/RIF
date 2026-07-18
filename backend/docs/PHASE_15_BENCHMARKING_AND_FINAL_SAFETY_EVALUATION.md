# Phase 15 — Benchmarking, Quality Metrics, and Final Safety Evaluation

## Goal

Phase 15 proves this local B2B Agentic AI POC with **persisted measurements** instead
of only a manual demo. It stores every measurement in the existing `benchmark_runs`
table and exposes controlled APIs for running scenarios, reviewing raw metric rows, and
viewing summary statistics.

## Benchmark Boundary

- Runs are **admin-only** because they create operational metric records.
- No benchmark endpoint accepts raw SQL.
- No benchmark scenario performs a business-table `INSERT`, `UPDATE`, `DELETE`, or DDL operation.
- Existing local PostgreSQL rows, ChromaDB chunks, audit records, OCR timings, and LangGraph routes are reused.
- Agent route scenarios may write normal `query_logs`, just as a real read-only user query would. They do not change business data.
- Resource sampling reports actual observed backend/Ollama process usage. It never guesses model-only memory.

## New APIs

```text
GET  /api/benchmarks/status
GET  /api/benchmarks/scenarios
POST /api/benchmarks/run                 admin only
GET  /api/benchmarks/runs                admin only
GET  /api/benchmarks/summary             admin only
```

## Approved Scenarios

### Safe Quality Suite

`safe_quality_suite` is the recommended first run. It measures:

```text
SQL guardrail block rate
MCP get_schema tool success + latency
RAG retrieval relevance + latency
verified hybrid evidence latency + evidence accuracy
session-memory availability
rollback audit evidence availability
historical OCR and document-ingestion timings
backend and visible Ollama process resources
```

It does **not** request local LLM generation.

### Agent Route Benchmarks

These reuse the real LangGraph workflow:

```text
agent_structured_read
agent_document_rag
agent_hybrid_evidence
agent_no_data_grounding
```

They report route accuracy, grounded-result rate, source references, and end-to-end
latency. The RAG answer and no-data scenarios can take longer because they call the
local Llama model through Ollama.

### Full Evaluation

`full_evaluation` runs the safe suite plus all agent-route benchmarks. Use it only once
your local Ollama model is already warm and you have enough time for local CPU inference.

## Persisted Metric Types

Examples include:

```text
structured_read_latency_ms
rag_answer_latency_ms
rag_retrieval_latency_ms
hybrid_query_latency_ms
security_block_rate
route_accuracy_rate
grounded_result_rate
retrieval_relevance_rate
mcp_tool_success_rate
ocr_processing_time_ms
document_ingestion_time_ms
backend_process_rss_mib
backend_process_cpu_percent
ollama_process_rss_mib
```

Each measurement record includes a `batch_id` in `details`, a status, timestamps, and
explicit statements that no business write and no raw SQL were accepted.

## Interpreting Results

Report values measured on the actual laptop. Do not promise a universal latency target:

```text
Structured reads, document RAG, hybrid evidence, OCR, and local model generation have different costs.
```

A lack of historical OCR timing or visible Ollama process is a controlled `information_not_available`
condition, not a fabricated value.

## Completion Gate

Phase 15 is complete only when:

```text
1. The full test suite passes.
2. safe_quality_suite has stored metrics in benchmark_runs.
3. Agent structured-read, RAG, hybrid, and no-data benchmarks have been run at least once.
4. Security guardrails show all controlled malicious SQL attempts were blocked.
5. RAG and hybrid results report grounded source evidence.
6. A benchmark summary shows separate measurements for each path.
7. OCR/document-ingestion timing is recorded from historical completed jobs.
8. Resource sampling reports actual backend values and either actual Ollama values or unavailable.
```
