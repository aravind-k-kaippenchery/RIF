"""Phase 14 contracts for admin audit lookup and confirmation-gated rollback."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RollbackPreviewRequest(BaseModel):
    """Request an admin-only rollback preview for one audited action."""

    model_config = ConfigDict(extra="forbid")

    session_id: UUID | None = None
    ttl_minutes: int = Field(default=30, ge=1, le=120)


class RollbackConfirmRequest(BaseModel):
    """Confirm exactly one stored rollback pending action; raw SQL is never accepted."""

    model_config = ConfigDict(extra="forbid")

    session_id: UUID
    confirmed: bool = False
