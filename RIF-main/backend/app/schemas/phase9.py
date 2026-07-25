"""Phase 9 ChromaDB, embeddings, and grounded document-RAG API contracts."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RagIndexRequest(BaseModel):
    """Request optional re-indexing of one already-ingested document."""

    model_config = ConfigDict(extra="forbid")

    force_reindex: bool = False


class RagRetrieveRequest(BaseModel):
    """Retrieve document chunks only; this does not call the LLM."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str = Field(min_length=1, max_length=2000)
    top_k: int | None = Field(default=None, ge=1, le=10)


class RagQueryRequest(RagRetrieveRequest):
    """Retrieve grounded evidence and ask the local LLM to answer from that evidence only."""

    pass


class RagChunkSource(BaseModel):
    """Traceable document source metadata returned to the frontend."""

    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    document_id: UUID
    filename: str
    page_number: int = Field(ge=1)
    chunk_index: int = Field(ge=1)
    similarity: float = Field(ge=0.0, le=1.0)
    text_preview: str


class RagIndexResult(BaseModel):
    """Indexing result returned after chunking and local embedding creation."""

    model_config = ConfigDict(extra="forbid")

    document_id: UUID
    filename: str
    chunk_count: int = Field(ge=0)
    collection_name: str
    embedding_model: str
    indexed_at: str
    action_log_id: int | None = None


class RagStatusData(BaseModel):
    """Frontend-safe local RAG status without exposing model paths or credentials."""

    model_config = ConfigDict(extra="forbid")

    chromadb_available: bool
    collection_name: str
    indexed_chunk_count: int = Field(ge=0)
    embedding_model: str
    embedding_model_loaded: bool
    local_only: bool
    phase: int = 9
    notes: list[str]
