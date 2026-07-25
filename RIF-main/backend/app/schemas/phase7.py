"""Pydantic contracts for Phase 7 confirmation-based CRUD writes."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class WriteProposalRequest(BaseModel):
    """A validated single INSERT, UPDATE, or DELETE proposal requiring confirmation."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    session_id: UUID
    sql: str = Field(min_length=1, max_length=10000)
    user_prompt: str | None = Field(default=None, max_length=2000)
    ttl_minutes: int = Field(default=30, ge=1, le=24 * 60)


class PromptWriteProposalRequest(BaseModel):
    """Ask the local model for a CRUD SQL proposal; execution still requires confirmation."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    session_id: UUID
    question: str = Field(min_length=1, max_length=2000)
    ttl_minutes: int = Field(default=30, ge=1, le=24 * 60)


class BulkWriteProposalRequest(BaseModel):
    """Bulk INSERT preview request. Records are parameterized during confirmation."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    session_id: UUID
    target_table: str = Field(min_length=1, max_length=128)
    records: list[dict[str, Any]] = Field(min_length=1, max_length=50)
    user_prompt: str | None = Field(default=None, max_length=2000)
    ttl_minutes: int = Field(default=30, ge=1, le=24 * 60)

    @field_validator("target_table")
    @classmethod
    def normalize_table_name(cls, value: str) -> str:
        return value.strip().lower()


class SyntheticEmployeeProposalRequest(BaseModel):
    """Generate synthetic employee records, then create the usual confirmation-gated bulk preview."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    session_id: UUID
    count: int = Field(ge=1, le=50, description="Number of synthetic employee records to preview.")
    department: str | None = Field(default=None, max_length=120)
    city: str | None = Field(default=None, max_length=120)
    company_name: str | None = Field(default=None, max_length=120)
    ttl_minutes: int = Field(default=30, ge=1, le=24 * 60)


class SyntheticDataProposalRequest(BaseModel):
    """Generate schema-aware synthetic records for one approved business table."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    session_id: UUID
    target_table: str = Field(min_length=1, max_length=128)
    count: int = Field(ge=1, le=50, description="Exact number of synthetic records to preview.")
    constraints: dict[str, Any] = Field(default_factory=dict)
    user_prompt: str | None = Field(default=None, max_length=2000)
    ttl_minutes: int = Field(default=30, ge=1, le=24 * 60)

    @field_validator("target_table")
    @classmethod
    def normalize_synthetic_table_name(cls, value: str) -> str:
        return value.strip().lower()


class PendingActionMutationRequest(BaseModel):
    """Explicit confirmation/cancellation request with the session scope retained."""

    model_config = ConfigDict(extra="forbid")

    session_id: UUID


class ActionPreviewResponse(BaseModel):
    """Small documented shape used by API consumers; endpoint returns the common envelope."""

    pending_action_id: UUID
    status: str
    target_table: str
    action_type: str
