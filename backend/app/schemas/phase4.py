"""Pydantic request models for Phase 4 SQL validation and MCP verification."""

from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SQLValidationRequest(BaseModel):
    """Request for validating one SQL proposal without executing a write."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    sql: str = Field(min_length=1, max_length=10000)


class ReadExecutionRequest(SQLValidationRequest):
    """Request for executing a validated SELECT-only statement through MCP."""


class SchemaChangePreviewRequest(SQLValidationRequest):
    """Admin-only request for a CREATE TABLE or ALTER TABLE ADD COLUMN preview."""


class MCPPendingActionRequest(BaseModel):
    """Input shape shared by the MCP pending-action tool and its REST verifier."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    session_id: UUID
    action_type: str = Field(min_length=1, max_length=64)
    target_table: Optional[str] = Field(default=None, max_length=128)
    validated_payload: dict[str, Any] = Field(default_factory=dict)
    preview_data: dict[str, Any] = Field(default_factory=dict)
    generated_sql: Optional[str] = Field(default=None, max_length=5000)
    ttl_minutes: int = Field(default=30, ge=1, le=24 * 60)


class MCPGetPendingActionRequest(BaseModel):
    """Input for reading one session-scoped pending action through MCP."""

    model_config = ConfigDict(extra="forbid")

    session_id: UUID
    pending_action_id: UUID
