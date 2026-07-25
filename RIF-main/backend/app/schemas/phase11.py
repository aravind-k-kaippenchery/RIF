"""Phase 11 hybrid evidence-fusion API contracts."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class HybridEvidenceStatus(BaseModel):
    """Frontend-safe status for verified SQL-plus-document evidence fusion."""

    model_config = ConfigDict(extra="forbid")

    phase: int = 11
    hybrid_final_answer_available: bool = True
    evidence_join_mode: str = "explicit_product_identity_match"
    database_access_boundary: str = "restricted_mcp_tool"
    answer_generation: str = "deterministic_verified_evidence_synthesis"
    notes: list[str] = Field(default_factory=list)
