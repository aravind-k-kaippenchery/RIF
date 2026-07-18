"""Pydantic contracts for the Phase 5 local Ollama adapter."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


RouteName = Literal["structured_read", "crud_write", "document_rag", "hybrid", "clarification"]
SQLRouteName = Literal["structured_read", "crud_write"]


class IntentClassificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str = Field(min_length=1, max_length=2000)


class IntentClassificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    route: RouteName
    requires_clarification: bool
    clarification_question: str | None = Field(default=None, max_length=500)


class SQLGenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str = Field(min_length=1, max_length=2000)
    route: SQLRouteName


class SQLGenerationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    route: SQLRouteName
    sql: str = Field(min_length=1, max_length=10000)
    explanation: str = Field(min_length=1, max_length=1000)


class RecordExtractionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    instruction: str = Field(min_length=1, max_length=2000)
    target_table: str = Field(min_length=1, max_length=128)


class RecordExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    table_name: str = Field(min_length=1, max_length=128)
    values: dict[str, Any] = Field(default_factory=dict)
    missing_required_fields: list[str] = Field(default_factory=list)
    requires_clarification: bool
    clarification_question: str | None = Field(default=None, max_length=500)


class GroundingEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source_type: Literal["database", "document"]
    reference: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=12000)


class GroundedAnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str = Field(min_length=1, max_length=2000)
    evidence: list[GroundingEvidence] = Field(default_factory=list, max_length=20)


class GroundedAnswerResult(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    answer: str = Field(min_length=1, max_length=4000)
    supported: bool
    source_references: list[str] = Field(default_factory=list, max_length=20)


class OllamaInvocationMetadata(BaseModel):
    """Safe telemetry returned to the API without model prompts or raw model output."""

    model_config = ConfigDict(extra="forbid")

    model: str
    attempts: int = Field(ge=0, le=2)
    total_duration_ns: int | None = None
    prompt_eval_count: int | None = None
    eval_count: int | None = None
