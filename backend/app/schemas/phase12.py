"""Phase 12 contracts for hardened MCP tool access."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class MCPTableRecordsQuery(BaseModel):
    """Bounded, non-SQL request for approved table records."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    table_name: str = Field(min_length=1, max_length=128)
    limit: int = Field(default=50, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=100000)

    @field_validator("table_name")
    @classmethod
    def normalize_table_name(cls, value: str) -> str:
        return value.strip().lower()


class MCPSchemaChangePreviewRequest(BaseModel):
    """Admin-only schema-change preview; it cannot execute DDL."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    sql: str = Field(min_length=1, max_length=10000)
    user_prompt: str = Field(min_length=1, max_length=2000)
    session_id: UUID | None = None


class MCPSchemaChangeApplyRequest(BaseModel):
    """Explicit acknowledgement request for a stored preview.

    Phase 12 intentionally keeps the operation preview-only. The endpoint exists so
    callers have a stable, confirmation-shaped contract without exposing raw DDL.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: UUID | None = None
    confirmed: bool = False
