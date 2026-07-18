"""Phase 13 contracts for frontend-ready APIs, memory, and admin schema confirmation."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class FrontendQueryRequest(BaseModel):
    """Natural-language request used by the frontend-friendly unified query endpoint."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str = Field(min_length=1, max_length=2000)
    session_id: UUID | None = None
    top_k: int | None = Field(default=None, ge=1, le=10)


class SessionHistoryQuery(BaseModel):
    """Bounded session-history query shape, retained for frontend contracts."""

    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=20, ge=1, le=100)


class AdminSchemaConfirmRequest(BaseModel):
    """Explicit confirmation required to execute a previously validated admin schema request."""

    model_config = ConfigDict(extra="forbid")

    confirmed: bool = False
    session_id: UUID | None = None


class AdminSchemaPromptRequest(BaseModel):
    """Natural-language admin request that becomes a preview only after LLM+AST validation."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    user_prompt: str = Field(min_length=1, max_length=2000)
    session_id: UUID | None = None


class AdminSchemaProposalResult(BaseModel):
    """Constrained local-model output for a restricted admin schema proposal."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    operation: str = Field(min_length=1, max_length=64)
    sql: str = Field(min_length=1, max_length=10000)
    explanation: str = Field(min_length=1, max_length=1000)
