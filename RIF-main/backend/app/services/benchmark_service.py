"""Phase 15 local-only benchmarking and deterministic safety evaluation.

The service records measurements in the existing ``benchmark_runs`` table.  It never
uses arbitrary SQL and never performs business-table writes.  Agent benchmarks reuse
the existing LangGraph routes; all other checks reuse existing validators, MCP tools,
RAG retrieval, audit history, and operational records.
"""

from __future__ import annotations

import statistics
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Callable

import psutil
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.agents.orchestrator import AgentOrchestrationError, agent_orchestrator
from app.core.constants import AgentRoute, ResponseStatus, UserRole
from app.core.config import get_settings
from app.mcp.client import LocalMCPClient
from app.models.operations import ActionLog, BenchmarkRun, DocumentIngestionJob, QueryLog
from app.rag.document_rag_service import DocumentRagError, document_rag_service
from app.schemas.phase15 import BenchmarkScenario
from app.services.hybrid_evidence_service import HybridEvidenceError, hybrid_evidence_service
from app.services.llm_service import llm_service
from app.services.sql_validation import validate_dml_sql


BENCHMARK_PHASE = 15
BENCHMARK_ROUTE_QUESTION = "Show workers from Bangalore"
RAG_QUESTION = "What warranty is mentioned for TextileBot X?"
HYBRID_QUESTION = "Which vendor offers textile automation products under 5 lakh according to the brochure?"
NO_DATA_QUESTION = "Show employees from Mars"


@dataclass(frozen=True)
class BenchmarkMetric:
    """One numeric or availability metric to persist in benchmark_runs."""

    metric_type: str
    metric_value: float | None
    metric_unit: str | None
    status: str
    details: dict[str, Any]


@dataclass(frozen=True)
class ScenarioOutcome:
    """Safe result from one benchmark scenario."""

    test_name: str
    route: str | None
    status: str
    summary: str
    metrics: list[BenchmarkMetric]
    details: dict[str, Any]


class BenchmarkService:
    """Run approved local measurements and persist each individual metric."""

    _SAFE_QUALITY_SUITE = (
        BenchmarkScenario.SECURITY_GUARDRAILS,
        BenchmarkScenario.MCP_SCHEMA,
        BenchmarkScenario.RAG_RETRIEVAL,
        BenchmarkScenario.HYBRID_EVIDENCE,
        BenchmarkScenario.SESSION_MEMORY,
        BenchmarkScenario.AUDIT_ROLLBACK_EVIDENCE,
        BenchmarkScenario.OCR_INGESTION_HISTORY,
        BenchmarkScenario.RESOURCE_SAMPLE,
    )
    _FULL_EVALUATION = _SAFE_QUALITY_SUITE + (
        BenchmarkScenario.AGENT_STRUCTURED_READ,
        BenchmarkScenario.AGENT_DOCUMENT_RAG,
        BenchmarkScenario.AGENT_HYBRID_EVIDENCE,
        BenchmarkScenario.AGENT_NO_DATA_GROUNDING,
    )

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @classmethod
    def supported_scenarios(cls) -> list[dict[str, Any]]:
        """Return frontend-safe metadata.  Full evaluation can be slow on CPU laptops."""

        metadata: dict[BenchmarkScenario, dict[str, Any]] = {
            BenchmarkScenario.SAFE_QUALITY_SUITE: {
                "description": "Runs validator, MCP, semantic retrieval, verified hybrid read, historical OCR/ingestion, memory, rollback-evidence, and resource checks. No local LLM generation is requested.",
                "uses_local_llm": False,
                "performs_business_writes": False,
            },
            BenchmarkScenario.FULL_EVALUATION: {
                "description": "Runs safe_quality_suite plus the four LangGraph agent route checks. This can take several minutes on CPU because it includes local LLM calls.",
                "uses_local_llm": True,
                "performs_business_writes": False,
            },
            BenchmarkScenario.AGENT_STRUCTURED_READ: {
                "description": "Measures LangGraph structured-read route, local LLM SQL generation, validator, MCP SELECT, and grounded database response.",
                "uses_local_llm": True,
                "performs_business_writes": False,
            },
            BenchmarkScenario.AGENT_DOCUMENT_RAG: {
                "description": "Measures document RAG route with local retrieval and local grounded LLM answer.",
                "uses_local_llm": True,
                "performs_business_writes": False,
            },
            BenchmarkScenario.AGENT_HYBRID_EVIDENCE: {
                "description": "Measures verified hybrid route using local document retrieval and restricted MCP evidence read.",
                "uses_local_llm": False,
                "performs_business_writes": False,
            },
            BenchmarkScenario.AGENT_NO_DATA_GROUNDING: {
                "description": "Measures controlled no-data behavior for a database question with no matching row.",
                "uses_local_llm": True,
                "performs_business_writes": False,
            },
            BenchmarkScenario.SECURITY_GUARDRAILS: {
                "description": "Measures SQL validator blocking for DROP, comment bypass, unrestricted UPDATE, and multi-statement attempts.",
                "uses_local_llm": False,
                "performs_business_writes": False,
            },
            BenchmarkScenario.RAG_RETRIEVAL: {
                "description": "Measures local semantic retrieval relevance without generating an LLM answer.",
                "uses_local_llm": False,
                "performs_business_writes": False,
            },
            BenchmarkScenario.HYBRID_EVIDENCE: {
                "description": "Measures explicit document-to-product identity matching and restricted PostgreSQL vendor/price evidence retrieval.",
                "uses_local_llm": False,
                "performs_business_writes": False,
            },
            BenchmarkScenario.MCP_SCHEMA: {
                "description": "Measures a controlled get_schema MCP tool call.",
                "uses_local_llm": False,
                "performs_business_writes": False,
            },
            BenchmarkScenario.SESSION_MEMORY: {
                "description": "Checks that persisted short-term query history exists without exposing full rows or hidden reasoning.",
                "uses_local_llm": False,
                "performs_business_writes": False,
            },
            BenchmarkScenario.AUDIT_ROLLBACK_EVIDENCE: {
                "description": "Checks that a completed rollback has action-log and snapshot evidence, without executing another rollback.",
                "uses_local_llm": False,
                "performs_business_writes": False,
            },
            BenchmarkScenario.OCR_INGESTION_HISTORY: {
                "description": "Reports observed OCR and document-ingestion timings from existing completed ingestion records; it does not re-upload files.",
                "uses_local_llm": False,
                "performs_business_writes": False,
            },
            BenchmarkScenario.RESOURCE_SAMPLE: {
                "description": "Captures current backend-process CPU/RAM and any visible local Ollama-process RAM. It reports unavailable rather than estimating model-only memory.",
                "uses_local_llm": False,
                "performs_business_writes": False,
            },
        }
        return [
            {
                "scenario": scenario.value,
                **metadata[scenario],
            }
            for scenario in BenchmarkScenario
        ]

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        if len(ordered) == 1:
            return round(ordered[0], 3)
        index = (len(ordered) - 1) * percentile
        lower = int(index)
        upper = min(lower + 1, len(ordered) - 1)
        fraction = index - lower
        return round(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction, 3)

    @staticmethod
    def _metric(metric_type: str, metric_value: float | None, metric_unit: str | None, status: str, **details: Any) -> BenchmarkMetric:
        return BenchmarkMetric(
            metric_type=metric_type,
            metric_value=None if metric_value is None else round(float(metric_value), 3),
            metric_unit=metric_unit,
            status=status,
            details=details,
        )

    @staticmethod
    def _status_from_boolean(value: bool) -> str:
        return ResponseStatus.SUCCESS.value if value else ResponseStatus.TOOL_FAILED.value

    def _security_guardrails(self, _: DbSession) -> ScenarioOutcome:
        started = perf_counter()
        cases = [
            ("drop_table", "DROP TABLE vendors", "forbidden_sql_operation"),
            ("comment_bypass", "SELECT * FROM vendors -- bypass", "sql_comments_blocked"),
            ("unrestricted_update", "UPDATE vendors SET city = 'Kochi'", "where_clause_required"),
            ("multiple_statements", "SELECT * FROM vendors; SELECT * FROM employees", "multiple_statements_blocked"),
        ]
        details: list[dict[str, Any]] = []
        blocked = 0
        for name, sql, expected_code in cases:
            result = validate_dml_sql(sql)
            passed = (not result.is_valid) and result.error_code == expected_code
            blocked += int(passed)
            details.append({"case": name, "blocked": passed, "error_code": result.error_code})
        elapsed = (perf_counter() - started) * 1000
        rate = blocked / len(cases)
        passed = blocked == len(cases)
        return ScenarioOutcome(
            test_name=BenchmarkScenario.SECURITY_GUARDRAILS.value,
            route=AgentRoute.SYSTEM.value,
            status=self._status_from_boolean(passed),
            summary=f"Blocked {blocked}/{len(cases)} controlled unsafe SQL proposals.",
            metrics=[
                self._metric("security_block_rate", rate, "ratio", self._status_from_boolean(passed), cases=details),
                self._metric("security_validation_latency_ms", elapsed, "ms", self._status_from_boolean(passed), case_count=len(cases)),
            ],
            details={"cases": details, "raw_sql_executed": False},
        )

    def _mcp_schema(self, _: DbSession) -> ScenarioOutcome:
        started = perf_counter()
        outcome = LocalMCPClient().call_tool("get_schema", {})
        elapsed = (perf_counter() - started) * 1000
        result = outcome.get("result", {}) if outcome.get("ok") else {}
        table_count = result.get("table_count") if isinstance(result, dict) else None
        passed = bool(outcome.get("ok")) and isinstance(result, dict) and int(table_count or 0) >= 1
        return ScenarioOutcome(
            test_name=BenchmarkScenario.MCP_SCHEMA.value,
            route=AgentRoute.SYSTEM.value,
            status=self._status_from_boolean(passed),
            summary="Controlled MCP schema tool was invoked without raw SQL.",
            metrics=[
                self._metric("mcp_tool_success_rate", 1.0 if passed else 0.0, "ratio", self._status_from_boolean(passed), tool="get_schema", table_count=table_count),
                self._metric("mcp_tool_latency_ms", elapsed, "ms", self._status_from_boolean(passed), tool="get_schema"),
            ],
            details={"tool": "get_schema", "table_count": table_count, "raw_sql_accepted": False},
        )

    def _rag_retrieval(self, _: DbSession) -> ScenarioOutcome:
        started = perf_counter()
        try:
            matches = document_rag_service.retrieve("Which product supports textile automation?", top_k=4)
            elapsed = (perf_counter() - started) * 1000
            explicit_product_match = any("textilebot x" in match.text.lower() for match in matches)
            passed = bool(matches) and explicit_product_match
            status = self._status_from_boolean(passed)
            return ScenarioOutcome(
                test_name=BenchmarkScenario.RAG_RETRIEVAL.value,
                route=AgentRoute.DOCUMENT_RAG.value,
                status=status,
                summary=f"Retrieved {len(matches)} local document chunk(s) for the textile-automation benchmark.",
                metrics=[
                    self._metric("rag_retrieval_latency_ms", elapsed, "ms", status, match_count=len(matches)),
                    self._metric("retrieval_relevance_rate", 1.0 if passed else 0.0, "ratio", status, explicit_product_identity_found=explicit_product_match),
                ],
                details={"match_count": len(matches), "references": [match.reference for match in matches], "llm_called": False},
            )
        except DocumentRagError as exc:
            elapsed = (perf_counter() - started) * 1000
            return ScenarioOutcome(
                test_name=BenchmarkScenario.RAG_RETRIEVAL.value,
                route=AgentRoute.DOCUMENT_RAG.value,
                status=exc.status.value,
                summary=exc.message,
                metrics=[self._metric("rag_retrieval_latency_ms", elapsed, "ms", exc.status.value, error_code=exc.code)],
                details={"error_code": exc.code, "llm_called": False},
            )

    def _hybrid_evidence(self, _: DbSession) -> ScenarioOutcome:
        started = perf_counter()
        try:
            matches = document_rag_service.retrieve(HYBRID_QUESTION, top_k=4)
            result = hybrid_evidence_service.fuse(question=HYBRID_QUESTION, document_matches=matches)
            elapsed = (perf_counter() - started) * 1000
            passed = result.status == ResponseStatus.SUCCESS and len(result.matches) >= 1
            status = self._status_from_boolean(passed)
            return ScenarioOutcome(
                test_name=BenchmarkScenario.HYBRID_EVIDENCE.value,
                route=AgentRoute.HYBRID.value,
                status=status,
                summary="Verified hybrid evidence used explicit product identity before reading vendor/price mappings.",
                metrics=[
                    self._metric("hybrid_query_latency_ms", elapsed, "ms", status, verified_match_count=len(result.matches)),
                    self._metric("hybrid_evidence_accuracy_rate", 1.0 if passed else 0.0, "ratio", status, verified_match_count=len(result.matches)),
                ],
                details={"verified_match_count": len(result.matches), "document_match_count": len(matches), "sources": [source.reference for source in result.source_citations], "llm_called": False},
            )
        except (DocumentRagError, HybridEvidenceError) as exc:
            elapsed = (perf_counter() - started) * 1000
            return ScenarioOutcome(
                test_name=BenchmarkScenario.HYBRID_EVIDENCE.value,
                route=AgentRoute.HYBRID.value,
                status=exc.status.value,
                summary=exc.message,
                metrics=[self._metric("hybrid_query_latency_ms", elapsed, "ms", exc.status.value, error_code=exc.code)],
                details={"error_code": exc.code, "llm_called": False},
            )

    def _session_memory(self, db: DbSession) -> ScenarioOutcome:
        started = perf_counter()
        session_ids = list(
            db.scalars(
                select(QueryLog.session_id)
                .where(QueryLog.session_id.is_not(None))
                .order_by(QueryLog.created_at.desc())
                .limit(100)
            ).all()
        )
        elapsed = (perf_counter() - started) * 1000
        available = len(session_ids) >= 1
        status = ResponseStatus.SUCCESS.value if available else ResponseStatus.INFORMATION_NOT_AVAILABLE.value
        return ScenarioOutcome(
            test_name=BenchmarkScenario.SESSION_MEMORY.value,
            route=AgentRoute.SYSTEM.value,
            status=status,
            summary=("Persisted session-scoped query history is available." if available else "No persisted session-scoped query history is currently available."),
            metrics=[
                self._metric("session_memory_available_rate", 1.0 if available else 0.0, "ratio", status, sampled_query_log_count=len(session_ids)),
                self._metric("session_memory_lookup_latency_ms", elapsed, "ms", status),
            ],
            details={"sampled_query_log_count": len(session_ids), "stores_hidden_reasoning": False, "stores_full_rows": False},
        )

    def _rollback_audit_evidence(self, db: DbSession) -> ScenarioOutcome:
        started = perf_counter()
        action = db.scalars(
            select(ActionLog)
            .where(ActionLog.action_type == "rollback", ActionLog.status == "success")
            .order_by(ActionLog.created_at.desc())
            .limit(1)
        ).first()
        elapsed = (perf_counter() - started) * 1000
        available = action is not None
        status = ResponseStatus.SUCCESS.value if available else ResponseStatus.INFORMATION_NOT_AVAILABLE.value
        return ScenarioOutcome(
            test_name=BenchmarkScenario.AUDIT_ROLLBACK_EVIDENCE.value,
            route=AgentRoute.SYSTEM.value,
            status=status,
            summary=("A completed rollback action is present in the audit trail." if available else "No completed rollback action is currently available for audit benchmarking."),
            metrics=[
                self._metric("rollback_audit_evidence_rate", 1.0 if available else 0.0, "ratio", status, rollback_action_log_id=action.id if action else None),
                self._metric("rollback_audit_lookup_latency_ms", elapsed, "ms", status),
            ],
            details={"rollback_action_log_id": action.id if action else None, "raw_sql_accepted": False},
        )

    def _ocr_ingestion_history(self, db: DbSession) -> ScenarioOutcome:
        started = perf_counter()
        jobs = list(
            db.scalars(
                select(DocumentIngestionJob)
                .where(DocumentIngestionJob.ocr_duration_ms.is_not(None))
                .order_by(DocumentIngestionJob.completed_at.desc())
                .limit(100)
            ).all()
        )
        elapsed = (perf_counter() - started) * 1000
        ocr_values = [float(job.ocr_duration_ms) for job in jobs if job.ocr_duration_ms is not None]
        ingestion_values = [
            (job.completed_at - job.started_at).total_seconds() * 1000
            for job in jobs
            if job.started_at is not None and job.completed_at is not None and job.completed_at >= job.started_at
        ]
        available = bool(ocr_values or ingestion_values)
        status = ResponseStatus.SUCCESS.value if available else ResponseStatus.INFORMATION_NOT_AVAILABLE.value
        metrics = [self._metric("ocr_history_lookup_latency_ms", elapsed, "ms", status, sampled_job_count=len(jobs))]
        if ocr_values:
            metrics.append(self._metric("ocr_processing_time_ms", statistics.mean(ocr_values), "ms", status, sample_count=len(ocr_values), p50_ms=self._percentile(ocr_values, 0.50), p95_ms=self._percentile(ocr_values, 0.95)))
        if ingestion_values:
            metrics.append(self._metric("document_ingestion_time_ms", statistics.mean(ingestion_values), "ms", status, sample_count=len(ingestion_values), p50_ms=self._percentile(ingestion_values, 0.50), p95_ms=self._percentile(ingestion_values, 0.95)))
        return ScenarioOutcome(
            test_name=BenchmarkScenario.OCR_INGESTION_HISTORY.value,
            route=AgentRoute.DOCUMENT_RAG.value,
            status=status,
            summary=("Historical OCR and ingestion timings were read from completed local jobs." if available else "No completed OCR timing is available yet; upload and OCR one image to collect this metric."),
            metrics=metrics,
            details={"sampled_job_count": len(jobs), "ocr_sample_count": len(ocr_values), "ingestion_sample_count": len(ingestion_values), "reprocessed_documents": False},
        )

    @staticmethod
    def _process_memory_mib(process: psutil.Process) -> float | None:
        try:
            return process.memory_info().rss / (1024 * 1024)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None

    def _resource_sample(self, _: DbSession) -> ScenarioOutcome:
        started = perf_counter()
        process = psutil.Process()
        try:
            process_cpu = process.cpu_percent(interval=0.10)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            process_cpu = None
        backend_rss = self._process_memory_mib(process)
        ollama_rss_values: list[float] = []
        for candidate in psutil.process_iter(["name", "cmdline"]):
            try:
                name = (candidate.info.get("name") or "").lower()
                cmdline = " ".join(candidate.info.get("cmdline") or []).lower()
                if "ollama" in name or "ollama" in cmdline:
                    value = self._process_memory_mib(candidate)
                    if value is not None:
                        ollama_rss_values.append(value)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        elapsed = (perf_counter() - started) * 1000
        status = ResponseStatus.SUCCESS.value
        metrics = [
            self._metric("benchmark_resource_sample_latency_ms", elapsed, "ms", status),
            self._metric("backend_process_rss_mib", backend_rss, "MiB", status),
            self._metric("backend_process_cpu_percent", process_cpu, "percent", status),
            self._metric("ollama_process_rss_mib", sum(ollama_rss_values) if ollama_rss_values else None, "MiB", status, observed=bool(ollama_rss_values)),
        ]
        return ScenarioOutcome(
            test_name=BenchmarkScenario.RESOURCE_SAMPLE.value,
            route=AgentRoute.SYSTEM.value,
            status=status,
            summary="Captured local backend process resources and visible Ollama process memory without estimating model-only memory.",
            metrics=metrics,
            details={"ollama_processes_found": len(ollama_rss_values), "model_only_memory_estimated": False},
        )

    def _agent_route(self, db: DbSession, *, scenario: BenchmarkScenario, question: str, expected_route: AgentRoute, expected_success: ResponseStatus) -> ScenarioOutcome:
        started = perf_counter()
        request_id = f"benchmark-{scenario.value}-{uuid.uuid4()}"
        try:
            result = agent_orchestrator.run(
                question=question,
                db=db,
                request_id=request_id,
                session_id=None,
                user_role=UserRole.ADMIN,
                top_k=4,
            )
            elapsed = (perf_counter() - started) * 1000
            route_ok = result.route == expected_route
            status_ok = result.status == expected_success
            sources = result.sources
            if scenario == BenchmarkScenario.AGENT_STRUCTURED_READ:
                evidence_ok = bool(result.data.get("row_count", 0)) and any(source.source_type == "database" for source in sources)
                latency_metric = "structured_read_latency_ms"
            elif scenario == BenchmarkScenario.AGENT_DOCUMENT_RAG:
                evidence_ok = any(source.source_type == "document" for source in sources)
                latency_metric = "rag_answer_latency_ms"
            elif scenario == BenchmarkScenario.AGENT_HYBRID_EVIDENCE:
                evidence_ok = int(result.data.get("verified_match_count", 0) or 0) >= 1 and {source.source_type for source in sources}.issuperset({"database", "document"})
                latency_metric = "hybrid_query_latency_ms"
            else:
                evidence_ok = result.status == ResponseStatus.INFORMATION_NOT_AVAILABLE and not sources
                latency_metric = "no_data_response_latency_ms"
            passed = route_ok and status_ok and evidence_ok
            status = self._status_from_boolean(passed)
            return ScenarioOutcome(
                test_name=scenario.value,
                route=expected_route.value,
                status=status,
                summary=f"LangGraph route '{result.route.value}' returned status '{result.status.value}' for the benchmark question.",
                metrics=[
                    self._metric(latency_metric, elapsed, "ms", status, request_id=request_id, observed_route=result.route.value, observed_status=result.status.value),
                    self._metric("route_accuracy_rate", 1.0 if route_ok else 0.0, "ratio", status, expected_route=expected_route.value, observed_route=result.route.value),
                    self._metric("grounded_result_rate", 1.0 if evidence_ok else 0.0, "ratio", status, source_count=len(sources), expected_no_data=(expected_success == ResponseStatus.INFORMATION_NOT_AVAILABLE)),
                ],
                details={
                    "question": question,
                    "expected_route": expected_route.value,
                    "observed_route": result.route.value,
                    "expected_status": expected_success.value,
                    "observed_status": result.status.value,
                    "source_references": [source.reference for source in sources],
                    "generated_sql_present": bool(result.generated_sql),
                    "business_write_executed": False,
                },
            )
        except AgentOrchestrationError as exc:
            elapsed = (perf_counter() - started) * 1000
            return ScenarioOutcome(
                test_name=scenario.value,
                route=expected_route.value,
                status=exc.status.value,
                summary=exc.message,
                metrics=[self._metric("agent_route_latency_ms", elapsed, "ms", exc.status.value, error_code=exc.code)],
                details={"question": question, "error_code": exc.code, "business_write_executed": False},
            )

    def _run_one(self, db: DbSession, scenario: BenchmarkScenario) -> ScenarioOutcome:
        handlers: dict[BenchmarkScenario, Callable[[DbSession], ScenarioOutcome]] = {
            BenchmarkScenario.SECURITY_GUARDRAILS: self._security_guardrails,
            BenchmarkScenario.MCP_SCHEMA: self._mcp_schema,
            BenchmarkScenario.RAG_RETRIEVAL: self._rag_retrieval,
            BenchmarkScenario.HYBRID_EVIDENCE: self._hybrid_evidence,
            BenchmarkScenario.SESSION_MEMORY: self._session_memory,
            BenchmarkScenario.AUDIT_ROLLBACK_EVIDENCE: self._rollback_audit_evidence,
            BenchmarkScenario.OCR_INGESTION_HISTORY: self._ocr_ingestion_history,
            BenchmarkScenario.RESOURCE_SAMPLE: self._resource_sample,
        }
        if scenario == BenchmarkScenario.AGENT_STRUCTURED_READ:
            return self._agent_route(db, scenario=scenario, question=BENCHMARK_ROUTE_QUESTION, expected_route=AgentRoute.STRUCTURED_READ, expected_success=ResponseStatus.SUCCESS)
        if scenario == BenchmarkScenario.AGENT_DOCUMENT_RAG:
            return self._agent_route(db, scenario=scenario, question=RAG_QUESTION, expected_route=AgentRoute.DOCUMENT_RAG, expected_success=ResponseStatus.SUCCESS)
        if scenario == BenchmarkScenario.AGENT_HYBRID_EVIDENCE:
            return self._agent_route(db, scenario=scenario, question=HYBRID_QUESTION, expected_route=AgentRoute.HYBRID, expected_success=ResponseStatus.SUCCESS)
        if scenario == BenchmarkScenario.AGENT_NO_DATA_GROUNDING:
            return self._agent_route(db, scenario=scenario, question=NO_DATA_QUESTION, expected_route=AgentRoute.STRUCTURED_READ, expected_success=ResponseStatus.INFORMATION_NOT_AVAILABLE)
        return handlers[scenario](db)

    @staticmethod
    def _outcome_to_dict(outcome: ScenarioOutcome) -> dict[str, Any]:
        return {
            "test_name": outcome.test_name,
            "route": outcome.route,
            "status": outcome.status,
            "summary": outcome.summary,
            "details": outcome.details,
            "metrics": [
                {
                    "metric_type": metric.metric_type,
                    "metric_value": metric.metric_value,
                    "metric_unit": metric.metric_unit,
                    "status": metric.status,
                    "details": metric.details,
                }
                for metric in outcome.metrics
            ],
        }

    def _persist_outcome(self, db: DbSession, *, batch_id: str, outcome: ScenarioOutcome, started_at: datetime, completed_at: datetime) -> int:
        for metric in outcome.metrics:
            details = {
                "phase": BENCHMARK_PHASE,
                "batch_id": batch_id,
                "scenario_status": outcome.status,
                "scenario_summary": outcome.summary,
                "scenario_details": outcome.details,
                "metric_details": metric.details,
                "business_write_executed": False,
                "raw_sql_accepted": False,
            }
            db.add(
                BenchmarkRun(
                    test_name=outcome.test_name,
                    route=outcome.route,
                    metric_type=metric.metric_type,
                    metric_value=metric.metric_value,
                    metric_unit=metric.metric_unit,
                    status=metric.status,
                    details=details,
                    started_at=started_at,
                    completed_at=completed_at,
                )
            )
        db.commit()
        return len(outcome.metrics)

    @staticmethod
    def _scenario_plan(scenario: BenchmarkScenario) -> tuple[BenchmarkScenario, ...]:
        if scenario == BenchmarkScenario.SAFE_QUALITY_SUITE:
            return BenchmarkService._SAFE_QUALITY_SUITE
        if scenario == BenchmarkScenario.FULL_EVALUATION:
            return BenchmarkService._FULL_EVALUATION
        return (scenario,)

    def run(self, db: DbSession, *, scenario: BenchmarkScenario, repeats: int) -> dict[str, Any]:
        """Run approved benchmarks and persist measurements. No business row is mutated."""

        batch_id = str(uuid.uuid4())
        outcomes: list[dict[str, Any]] = []
        total_metric_runs = 0
        for iteration in range(1, repeats + 1):
            for planned_scenario in self._scenario_plan(scenario):
                started_at = self._now()
                outcome = self._run_one(db, planned_scenario)
                completed_at = self._now()
                try:
                    stored_count = self._persist_outcome(db, batch_id=batch_id, outcome=outcome, started_at=started_at, completed_at=completed_at)
                except Exception as exc:
                    db.rollback()
                    outcome = ScenarioOutcome(
                        test_name=planned_scenario.value,
                        route=None,
                        status=ResponseStatus.DATABASE_UNAVAILABLE.value,
                        summary="Benchmark measurement could not be persisted to benchmark_runs.",
                        metrics=[self._metric("benchmark_persistence_failure", None, None, ResponseStatus.DATABASE_UNAVAILABLE.value, error_type=type(exc).__name__)],
                        details={"error_type": type(exc).__name__},
                    )
                    stored_count = 0
                outcome_dict = self._outcome_to_dict(outcome)
                outcome_dict["iteration"] = iteration
                outcome_dict["persisted_metric_count"] = stored_count
                outcomes.append(outcome_dict)
                total_metric_runs += stored_count
        successful = sum(1 for item in outcomes if item["status"] == ResponseStatus.SUCCESS.value)
        return {
            "phase": BENCHMARK_PHASE,
            "batch_id": batch_id,
            "requested_scenario": scenario.value,
            "repeats": repeats,
            "scenario_result_count": len(outcomes),
            "successful_scenario_count": successful,
            "overall_status": "success" if successful == len(outcomes) else "partial_success",
            "stored_metric_run_count": total_metric_runs,
            "business_write_executed": False,
            "raw_sql_accepted": False,
            "results": outcomes,
        }

    @staticmethod
    def _serialize_run(run: BenchmarkRun) -> dict[str, Any]:
        return {
            "benchmark_run_id": str(run.id),
            "test_name": run.test_name,
            "route": run.route,
            "metric_type": run.metric_type,
            "metric_value": run.metric_value,
            "metric_unit": run.metric_unit,
            "status": run.status,
            "details": run.details,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        }

    def list_runs(self, db: DbSession, *, limit: int, route: str | None = None, metric_type: str | None = None) -> list[dict[str, Any]]:
        statement = select(BenchmarkRun).order_by(BenchmarkRun.started_at.desc()).limit(limit)
        if route:
            statement = statement.where(BenchmarkRun.route == route)
        if metric_type:
            statement = statement.where(BenchmarkRun.metric_type == metric_type)
        return [self._serialize_run(item) for item in db.scalars(statement).all()]

    def summary(self, db: DbSession, *, limit: int) -> dict[str, Any]:
        runs = list(db.scalars(select(BenchmarkRun).order_by(BenchmarkRun.started_at.desc()).limit(limit)).all())
        grouped: dict[tuple[str, str | None, str | None], list[BenchmarkRun]] = {}
        for run in runs:
            key = (run.metric_type, run.route, run.metric_unit)
            grouped.setdefault(key, []).append(run)
        metrics: list[dict[str, Any]] = []
        for (metric_type, route, unit), items in sorted(grouped.items()):
            numeric = [float(item.metric_value) for item in items if item.metric_value is not None]
            success_count = sum(1 for item in items if item.status == ResponseStatus.SUCCESS.value)
            metrics.append(
                {
                    "metric_type": metric_type,
                    "route": route,
                    "metric_unit": unit,
                    "sample_count": len(items),
                    "success_count": success_count,
                    "success_rate": round(success_count / len(items), 3) if items else 0.0,
                    "average": round(statistics.mean(numeric), 3) if numeric else None,
                    "minimum": round(min(numeric), 3) if numeric else None,
                    "maximum": round(max(numeric), 3) if numeric else None,
                    "p50": self._percentile(numeric, 0.50),
                    "p95": self._percentile(numeric, 0.95),
                }
            )
        return {
            "phase": BENCHMARK_PHASE,
            "sample_limit": limit,
            "benchmark_run_count": len(runs),
            "metrics": metrics,
            "interpretation_notes": [
                "Report the measurements from this laptop; do not claim one universal latency target.",
                "Structured, RAG, hybrid, OCR, and local-model timings must be compared separately.",
                "A missing OCR or Ollama metric is reported as unavailable rather than estimated.",
                "Benchmark requests never execute a business-table INSERT, UPDATE, DELETE, or DDL statement.",
            ],
        }

    def status(self) -> dict[str, Any]:
        settings = get_settings()
        llm_health = llm_service.check_llm_health()
        return {
            "phase": BENCHMARK_PHASE,
            "benchmark_runs_persisted": True,
            "benchmark_table": "benchmark_runs",
            "admin_only_run_control": True,
            "safe_suite_available": True,
            "full_evaluation_available": True,
            "business_write_benchmarking_allowed": False,
            "raw_sql_accepted": False,
            "max_repeats": settings.benchmark_max_repeats,
            "local_llm_ready_for_agent_benchmarks": llm_health.connected and llm_health.model_installed,
            "resource_sampling_available": True,
            "notes": [
                "Agent benchmark scenarios reuse the existing LangGraph routes and their validators/MCP controls.",
                "Safe quality scenarios perform no business writes and use existing local data only.",
                "Quality metrics include validator block rate, route accuracy, evidence grounding, retrieval relevance, and controlled no-data behavior.",
                "Resource sampling reports the backend process and visible Ollama process; it does not invent model-only memory values.",
            ],
        }


benchmark_service = BenchmarkService()
