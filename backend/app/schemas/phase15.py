"""Phase 15 contracts for controlled local benchmarks and safety evaluation."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class BenchmarkScenario(str, Enum):
    """Approved local benchmark scenarios. None performs business-table writes."""

    SAFE_QUALITY_SUITE = "safe_quality_suite"
    FULL_EVALUATION = "full_evaluation"
    AGENT_STRUCTURED_READ = "agent_structured_read"
    AGENT_DOCUMENT_RAG = "agent_document_rag"
    AGENT_HYBRID_EVIDENCE = "agent_hybrid_evidence"
    AGENT_NO_DATA_GROUNDING = "agent_no_data_grounding"
    SECURITY_GUARDRAILS = "security_guardrails"
    RAG_RETRIEVAL = "rag_retrieval"
    HYBRID_EVIDENCE = "hybrid_evidence"
    MCP_SCHEMA = "mcp_schema"
    SESSION_MEMORY = "session_memory"
    AUDIT_ROLLBACK_EVIDENCE = "audit_rollback_evidence"
    OCR_INGESTION_HISTORY = "ocr_ingestion_history"
    RESOURCE_SAMPLE = "resource_sample"


class BenchmarkRunRequest(BaseModel):
    """Start one approved benchmark scenario, optionally with a small repeat count."""

    model_config = ConfigDict(extra="forbid")

    scenario: BenchmarkScenario = BenchmarkScenario.SAFE_QUALITY_SUITE
    repeats: int = Field(default=1, ge=1, le=3)


class BenchmarkRunQuery(BaseModel):
    """Reserved stable shape for future frontend benchmark filters."""

    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=100, ge=1, le=500)
