"""Pydantic request models for Phase 3 endpoints."""

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class SessionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ttl_minutes: int = Field(default=120, ge=5, le=24 * 60)


class PendingActionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    action_type: str = Field(min_length=1, max_length=64, examples=["insert"])
    target_table: Optional[str] = Field(default=None, max_length=128, examples=["employees"])
    validated_payload: dict[str, Any] = Field(default_factory=dict)
    preview_data: dict[str, Any] = Field(default_factory=dict)
    generated_sql: Optional[str] = Field(default=None, max_length=5000)
    ttl_minutes: int = Field(default=30, ge=1, le=24 * 60)
