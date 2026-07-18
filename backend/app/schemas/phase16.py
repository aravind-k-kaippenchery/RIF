"""Phase 16 contracts for final demo, release readiness, and evidence handoff."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class DemoScenario(BaseModel):
    """One reviewer-friendly final demonstration scenario."""

    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    title: str
    purpose: str
    request: dict[str, object]
    expected_evidence: list[str] = Field(default_factory=list)
    safety_boundary: str
    business_write_executed: bool = False
    raw_sql_accepted: bool = False


class FeatureEvidence(BaseModel):
    """One final evidence row for a required POC capability."""

    model_config = ConfigDict(extra="forbid")

    feature_id: int = Field(ge=1, le=17)
    capability: str
    implemented_in_phase: int
    evidence: str
    primary_endpoint: str
    safety_boundary: str
