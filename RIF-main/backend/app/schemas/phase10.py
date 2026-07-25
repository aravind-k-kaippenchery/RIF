"""Phase 10 unified agent-query contracts."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class AgentQueryRequest(BaseModel):
    """One natural-language request submitted to the LangGraph router."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str = Field(min_length=1, max_length=2000)
    session_id: str | None = Field(default=None, max_length=128)
    top_k: int | None = Field(default=None, ge=1, le=10)


class AgentGraphStatus(BaseModel):
    """Frontend-safe status for the Phase 10 LangGraph orchestrator."""

    model_config = ConfigDict(extra="forbid")

    phase: int = 11
    graph_engine: str = "langgraph"
    graph_compiled: bool
    supported_routes: list[str]
    write_execution_allowed_without_confirmation: bool = False
    hybrid_final_answer_available: bool = True
    notes: list[str]
