"""Phase 16 final integration, demo-readiness, and release-evidence service.

This service intentionally reuses existing status services and persisted benchmark rows.
It does not invoke arbitrary SQL, trigger model generation, create a business-table write,
or perform a rollback.  It exists to make the completed local POC easy to review and demo.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session as DbSession

from app.agents.orchestrator import agent_orchestrator
from app.db.health import get_database_health
from app.mcp.client import LocalMCPClient
from app.models.operations import BenchmarkRun
from app.rag.document_rag_service import document_rag_service
from app.services.llm_service import llm_service


@dataclass(frozen=True)
class _CapabilityCheck:
    component: str
    ready: bool
    status: str
    detail: str


class DemoReadinessService:
    """Build final-demo evidence without bypassing any prior safety boundary."""

    PHASE = 16
    EXPECTED_MCP_TOOL_COUNT = 15

    @staticmethod
    def feature_matrix() -> list[dict[str, Any]]:
        """Return the 17 required capabilities in presentation-ready language."""

        rows = [
            (1, "Natural-language record creation", 7, "CRUD proposals extract record fields and require explicit confirmation before INSERT.", "POST /api/agent/query", "No write executes before confirmation."),
            (2, "Dynamic bulk record creation", 7, "Bulk proposals validate each generated record, perform duplicate checks, and use one confirmation-gated transaction.", "POST /api/crud/bulk-propose", "Batch writes are previewed and transaction-bound."),
            (3, "Duplicate detection and prevention", 7, "Existing records and in-batch collisions are checked before insert execution.", "POST /api/crud/propose", "Potential duplicates are not silently overwritten."),
            (4, "PDF, DOCX, TXT, JPG and PNG upload with local OCR", 8, "Native extraction and local PaddleOCR are recorded through document and ingestion-job metadata.", "POST /api/documents/upload", "File extension, MIME type, signature and size are validated."),
            (5, "Natural-language data search", 6, "Business terms are normalized, then a local model proposes SQL that is validated before a read-only MCP execution.", "POST /api/query", "Only validated SELECT statements reach PostgreSQL on the read path."),
            (6, "DML support", 7, "INSERT, UPDATE and DELETE exist as controlled preview-and-confirm workflows.", "POST /api/crud/propose", "UPDATE and DELETE require scoped targets and confirmation."),
            (7, "No-hallucination behavior", 6, "Missing database or document evidence produces information_not_available instead of invented facts.", "POST /api/query", "Answers are evidence-gated by database rows and/or retrieved chunks."),
            (8, "Logs and history", 1, "Requests, queries, actions, confirmations, schema changes, uploads and benchmark rows are retained in operational tables.", "GET /api/logs", "Operational history is admin-only and bounded."),
            (9, "Grounded answers with sources", 9, "Document answers expose filename/page/chunk references; database answers expose queried tables; hybrid answers expose both.", "POST /api/agent/query", "Hybrid joins require explicit product identity evidence."),
            (10, "Frontend table, document and log viewers", 13, "Frontend-ready table, document, log, schema and session endpoints reuse hardened MCP and backend boundaries.", "GET /api/tables", "Table reads are bounded and raw SQL is never accepted."),
            (11, "Confirmation for business writes", 7, "Every INSERT, UPDATE and DELETE becomes a pending action before execution.", "POST /api/crud/actions/{pending_action_id}/confirm", "Confirmation uses the stored action, not caller-supplied SQL."),
            (12, "Short-term session memory", 13, "Recent bounded query/action context is retained per active session for multi-turn interaction.", "GET /api/sessions/{session_id}/history", "No credentials, hidden reasoning or full unrestricted rows are stored."),
            (13, "Benchmark logging and quality metrics", 15, "Security, MCP, retrieval, hybrid, route, OCR history and resource metrics persist in benchmark_runs.", "GET /api/benchmarks/summary", "Benchmarks never execute business-table writes."),
            (14, "Backup and rollback", 14, "Audited UPDATE and DELETE actions store snapshots and support a confirmation-gated, idempotent rollback flow.", "POST /api/audit/actions/{action_log_id}/rollback/preview", "Rollback restores only stored before-snapshot evidence."),
            (15, "Structured error handling", 1, "Controlled response statuses cover validation, no-data, tool, OCR, retrieval, LLM and database failures.", "GET /api/status", "Raw Python tracebacks are not returned to frontend users."),
            (16, "Restricted admin schema workflow", 13, "Admin-only CREATE TABLE and ALTER TABLE ADD COLUMN previews require stored approval and explicit confirmation.", "POST /api/admin/schema-changes/{schema_change_id}/confirm", "DROP, TRUNCATE, ALTER DROP COLUMN, GRANT and REVOKE remain blocked."),
            (17, "Zero, one or many child records", 2, "employees → employee_permissions proves a parent may have zero, one or many child rows.", "GET /api/database/employees/{employee_code}/permissions", "Foreign-key RESTRICT prevents unsafe parent deletion."),
        ]
        return [
            {
                "feature_id": feature_id,
                "capability": capability,
                "implemented_in_phase": phase,
                "evidence": evidence,
                "primary_endpoint": endpoint,
                "safety_boundary": safety,
            }
            for feature_id, capability, phase, evidence, endpoint, safety in rows
        ]

    @staticmethod
    def demo_scenarios() -> list[dict[str, Any]]:
        """Return a safe ordered live-demo script.  No scenario is auto-executed here."""

        return [
            {
                "scenario_id": "readiness",
                "title": "Local system readiness",
                "purpose": "Show the backend, local PostgreSQL, local Ollama, ChromaDB, LangGraph and MCP boundaries are available.",
                "request": {"method": "GET", "path": "/api/demo/status"},
                "expected_evidence": ["phase = 16", "local_only = true", "raw_sql_accepted = false"],
                "safety_boundary": "Status inspection does not execute a business write or an LLM generation.",
                "business_write_executed": False,
                "raw_sql_accepted": False,
            },
            {
                "scenario_id": "structured_read",
                "title": "Grounded structured read",
                "purpose": "Demonstrate a natural-language database query through the validated SQL and MCP read path.",
                "request": {"method": "POST", "path": "/api/query", "body": {"question": "Show workers from Bangalore"}},
                "expected_evidence": ["route = structured_read", "database source = employees", "validated SELECT", "real rows or information_not_available"],
                "safety_boundary": "Only a server-limited validated SELECT can execute.",
                "business_write_executed": False,
                "raw_sql_accepted": False,
            },
            {
                "scenario_id": "document_rag",
                "title": "Grounded local document RAG",
                "purpose": "Show a brochure answer with filename/page/chunk sources.",
                "request": {"method": "POST", "path": "/api/query", "body": {"question": "What warranty is mentioned for TextileBot X?"}},
                "expected_evidence": ["route = document_rag", "answer from retrieved evidence", "document filename/page/chunk sources"],
                "safety_boundary": "No evidence returns information_not_available instead of a fabricated answer.",
                "business_write_executed": False,
                "raw_sql_accepted": False,
            },
            {
                "scenario_id": "hybrid_evidence",
                "title": "Verified hybrid evidence",
                "purpose": "Combine brochure product evidence with live vendor and price data without a guessed join.",
                "request": {"method": "POST", "path": "/api/query", "body": {"question": "Which vendor offers textile automation products under 5 lakh according to the brochure?"}},
                "expected_evidence": ["route = hybrid", "database + document sources", "explicit product identity", "verified vendor mappings"],
                "safety_boundary": "Document chunks and database rows are fused only when product identity is explicit.",
                "business_write_executed": False,
                "raw_sql_accepted": False,
            },
            {
                "scenario_id": "crud_preview",
                "title": "Confirmation-gated CRUD",
                "purpose": "Show that a natural-language write request becomes a preview rather than an immediate database change.",
                "request": {"method": "POST", "path": "/api/agent/query", "body": {"question": "Add employee EMP-DEMO named Demo User with email demo.user@example.com, department Sales, city Bangalore, company Neolotex, salary 50000, employment status active."}},
                "expected_evidence": ["route = crud_write", "status = pending_confirmation", "pending_action_id", "write_execution_allowed = false"],
                "safety_boundary": "Cancel the preview after showing it; do not confirm it during a read-only demo.",
                "business_write_executed": False,
                "raw_sql_accepted": False,
            },
            {
                "scenario_id": "frontend_tables",
                "title": "Frontend table and audit evidence",
                "purpose": "Show approved tables and bounded records via the MCP-backed frontend API surface.",
                "request": {"method": "GET", "path": "/api/tables"},
                "expected_evidence": ["approved tables", "bounded rows", "role-aware operational table protection"],
                "safety_boundary": "No raw SQL input and a server-side maximum row limit.",
                "business_write_executed": False,
                "raw_sql_accepted": False,
            },
            {
                "scenario_id": "rollback_audit",
                "title": "Audit and rollback proof",
                "purpose": "Show the prior confirmed rollback audit record rather than executing another change during the presentation.",
                "request": {"method": "GET", "path": "/api/audit/actions?limit=20", "headers": {"X-User-Role": "admin"}},
                "expected_evidence": ["before/after snapshots", "rollback action log", "idempotent confirmation evidence"],
                "safety_boundary": "Rollback requires an admin, stored snapshot evidence and a separate explicit confirmation.",
                "business_write_executed": False,
                "raw_sql_accepted": False,
            },
            {
                "scenario_id": "benchmark_evidence",
                "title": "Measured safety and quality",
                "purpose": "Show persisted local metrics instead of only manual claims.",
                "request": {"method": "GET", "path": "/api/benchmarks/summary?limit=250", "headers": {"X-User-Role": "admin"}},
                "expected_evidence": ["security block rate", "MCP success", "RAG relevance", "hybrid evidence accuracy", "separate latency metrics"],
                "safety_boundary": "Benchmark runs never accept raw SQL or execute business-table writes.",
                "business_write_executed": False,
                "raw_sql_accepted": False,
            },
        ]

    @staticmethod
    def _safe_operational_counts(db: DbSession) -> dict[str, int | None]:
        try:
            all_runs = list(db.scalars(select(BenchmarkRun).order_by(BenchmarkRun.completed_at.desc()).limit(500)).all())
        except SQLAlchemyError:
            return {"benchmark_run_count": None, "latest_security_block_rate": None, "latest_mcp_success_rate": None, "latest_retrieval_relevance_rate": None, "latest_hybrid_evidence_accuracy_rate": None}

        def latest(metric_type: str) -> float | None:
            for item in all_runs:
                if item.metric_type == metric_type and item.metric_value is not None:
                    return float(item.metric_value)
            return None

        return {
            "benchmark_run_count": len(all_runs),
            "latest_security_block_rate": latest("security_block_rate"),
            "latest_mcp_success_rate": latest("mcp_tool_success_rate"),
            "latest_retrieval_relevance_rate": latest("retrieval_relevance_rate"),
            "latest_hybrid_evidence_accuracy_rate": latest("hybrid_evidence_accuracy_rate"),
        }

    def public_status(self) -> dict[str, Any]:
        """Return a redacted readiness view that is safe for non-admin reviewers."""

        database = get_database_health()
        ollama = llm_service.check_llm_health()
        rag = document_rag_service.status()
        agent = agent_orchestrator.status()
        tool_count = len(LocalMCPClient().list_tools())

        checks = [
            _CapabilityCheck("FastAPI response layer", True, "ready", "Unified response and error envelopes are active."),
            _CapabilityCheck("PostgreSQL", bool(database.connected), "ready" if database.connected else "attention", database.message),
            _CapabilityCheck("Local Ollama model", bool(ollama.connected and ollama.model_installed), "ready" if ollama.connected and ollama.model_installed else "attention", ollama.message),
            _CapabilityCheck("Local ChromaDB", bool(rag.get("chromadb_available", False)), "ready" if rag.get("chromadb_available", False) else "attention", "Persistent local vector retrieval is available when ChromaDB is reachable."),
            _CapabilityCheck("LangGraph orchestration", bool(agent.get("graph_compiled", False)), "ready" if agent.get("graph_compiled", False) else "attention", "Four controlled routes are compiled."),
            _CapabilityCheck("MCP tool boundary", tool_count >= self.EXPECTED_MCP_TOOL_COUNT, "ready" if tool_count >= self.EXPECTED_MCP_TOOL_COUNT else "attention", f"{tool_count} controlled MCP tools are registered; no unrestricted SQL tool is exposed."),
        ]
        full_demo_ready = all(item.ready for item in checks)
        return {
            "phase": self.PHASE,
            "phase_name": "Final integration, demonstration readiness, and release evidence",
            "full_demo_ready": full_demo_ready,
            "local_only": True,
            "raw_sql_accepted": False,
            "business_write_executed": False,
            "mcp_tool_count": tool_count,
            "checks": [item.__dict__ for item in checks],
            "feature_count": 17,
            "available_demo_scenario_count": len(self.demo_scenarios()),
            "notes": [
                "Phase 16 does not bypass the existing LangGraph, MCP, SQL validation, RAG, audit, rollback, or benchmark boundaries.",
                "Use the report endpoint with an admin role for persisted metric highlights and final presentation evidence.",
                "Authentication remains a future production enhancement; the current role mechanism is intentionally a temporary header-based POC guard.",
            ],
        }

    def readiness(self, db: DbSession) -> dict[str, Any]:
        """Add bounded persisted benchmark evidence to the public readiness view."""

        report = self.public_status()
        evidence = self._safe_operational_counts(db)
        report["benchmark_evidence"] = evidence
        report["benchmark_evidence_available"] = bool((evidence.get("benchmark_run_count") or 0) > 0)
        report["release_gate"] = {
            "all_core_local_services_ready": report["full_demo_ready"],
            "persisted_benchmark_evidence_available": report["benchmark_evidence_available"],
            "raw_sql_write_tool_exposed": False,
            "business_write_executed_by_readiness_check": False,
        }
        return report

    def final_report(self, db: DbSession) -> dict[str, Any]:
        """Return a concise, presentation-ready release report using observed local state."""

        readiness = self.readiness(db)
        evidence = readiness["benchmark_evidence"]
        strengths = [
            "All four agent routes are available: structured read, CRUD preview, document RAG, and verified hybrid evidence.",
            "Reads use validated MCP-backed access; writes are confirmation-gated; operational history is auditable.",
            "Document and hybrid answers carry source evidence rather than relying on unsupported model memory.",
            "Rollback uses stored snapshots and idempotent confirmation instead of caller-supplied SQL.",
        ]
        limitations = [
            "The current role guard is header-based POC authorization, not full production authentication or RBAC.",
            "Admin schema execution is deliberately restricted to previously validated CREATE TABLE and ALTER TABLE ADD COLUMN requests.",
            "The local model's latency depends on the laptop, current RAM/CPU load, and whether Ollama is already warm.",
            "Benchmark results are local measurements and must be reported as device-specific, not universal performance guarantees.",
        ]
        return {
            "phase": self.PHASE,
            "project_title": "Local Agentic AI B2B Product Intelligence Assistant",
            "implementation_state": "final_poc_demo_ready" if readiness["full_demo_ready"] else "final_poc_needs_environment_attention",
            "architecture": {
                "local_model_runtime": "Ollama + Llama 3",
                "structured_data": "PostgreSQL",
                "unstructured_data": "Local ChromaDB + local embeddings",
                "agent_orchestration": "LangGraph",
                "tool_boundary": "Local Streamable HTTP MCP",
                "api_backend": "FastAPI",
            },
            "readiness": readiness,
            "measured_metric_highlights": evidence,
            "implemented_feature_matrix": self.feature_matrix(),
            "demo_order": [scenario["scenario_id"] for scenario in self.demo_scenarios()],
            "strengths": strengths,
            "known_limitations": limitations,
            "safety_statement": "No Phase 16 report, readiness check, feature matrix, or demo catalog accepts raw SQL or executes a business-table write.",
        }

    def smoke(self, db: DbSession) -> dict[str, Any]:
        """Run a non-destructive final integration check made solely of status inspection."""

        readiness = self.readiness(db)
        checks = [
            {"check": item["component"], "passed": item["ready"], "status": item["status"], "detail": item["detail"]}
            for item in readiness["checks"]
        ]
        benchmark_available = readiness["benchmark_evidence_available"]
        checks.append({
            "check": "Persisted benchmark evidence",
            "passed": benchmark_available,
            "status": "ready" if benchmark_available else "attention",
            "detail": "At least one benchmark_runs metric row is available." if benchmark_available else "Run Phase 15 benchmark scenarios before the final presentation.",
        })
        return {
            "phase": self.PHASE,
            "overall_status": "success" if all(item["passed"] for item in checks) else "partial_success",
            "check_count": len(checks),
            "passed_check_count": sum(1 for item in checks if item["passed"]),
            "checks": checks,
            "business_write_executed": False,
            "raw_sql_accepted": False,
            "model_generation_triggered": False,
            "next_step": "Use GET /api/demo/scenarios for the reviewer-facing live-demo order.",
        }


demo_readiness_service = DemoReadinessService()
