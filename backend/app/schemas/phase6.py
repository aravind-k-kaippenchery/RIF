"""Pydantic contracts for the Phase 6 structured-read workflow."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class StructuredReadRequest(BaseModel):
    """One natural-language request that may execute a validated SELECT only."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str = Field(min_length=1, max_length=2000)
