"""Phase 8 document upload and OCR API contracts."""

from __future__ import annotations

from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class DocumentSummary(BaseModel):
    """Frontend-safe metadata for one uploaded document."""

    model_config = ConfigDict(extra="forbid")

    document_id: UUID
    original_filename: str
    file_type: str
    file_size_bytes: int
    page_count: Optional[int] = None
    ocr_used: bool
    ingestion_status: str
    extracted_text_available: bool
    document_version: int
    error_message: Optional[str] = None
    created_at: str
    updated_at: str


class DocumentUploadResult(BaseModel):
    """Upload completion information returned inside the common response envelope."""

    model_config = ConfigDict(extra="forbid")

    document: DocumentSummary
    ingestion_job_id: UUID
    extraction: dict
    action_log_id: Optional[int] = None


class DocumentListResponse(BaseModel):
    """List payload for the future frontend document viewer."""

    model_config = ConfigDict(extra="forbid")

    document_count: int = Field(ge=0)
    documents: list[DocumentSummary]
