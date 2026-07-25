"""Shared Pydantic models used by all API endpoints."""

from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.core.constants import AgentRoute, RelationshipCardinality, ResponseStatus


class ApiError(BaseModel):
    """Machine-readable and frontend-safe error information."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., examples=["validation_error"])
    message: str = Field(..., examples=["The request contains invalid data."])
    details: Optional[list[dict[str, Any]]] = None


class SourceCitation(BaseModel):
    """Reserved now for database/RAG citations added in later phases."""

    model_config = ConfigDict(extra="forbid")

    source_type: str = Field(..., examples=["database", "document"])
    reference: str = Field(..., examples=["employees", "brochure.pdf#page=2"])
    detail: Optional[str] = None


class ApiResponse(BaseModel):
    """One stable JSON envelope used by every endpoint."""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    session_id: Optional[str] = None
    status: ResponseStatus
    route: AgentRoute = AgentRoute.SYSTEM
    answer: Optional[str] = None
    data: Optional[Any] = None
    sources: list[SourceCitation] = Field(default_factory=list)
    generated_sql: Optional[str] = None
    pending_action_id: Optional[str] = None
    error: Optional[ApiError] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class QueryRequest(BaseModel):
    """Placeholder request schema for the natural-language query endpoint in Phase 13."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str = Field(min_length=1, max_length=2000)
    session_id: Optional[str] = Field(default=None, max_length=128)


class SQLProposal(BaseModel):
    """Contract that later LLM-generated SQL will have to satisfy."""

    model_config = ConfigDict(extra="forbid")

    sql: str = Field(min_length=1)
    explanation: str = Field(min_length=1)
    route: AgentRoute


class RecordExtraction(BaseModel):
    """Contract for future natural-language parent-record creation."""

    model_config = ConfigDict(extra="forbid")

    table_name: str = Field(min_length=1)
    values: dict[str, Any]


class ChildRecordExtraction(BaseModel):
    """Contract for child-row creation planned for Phase 7."""

    model_config = ConfigDict(extra="forbid")

    parent_table: str = Field(min_length=1, examples=["employees"])
    parent_record_id: str = Field(min_length=1, examples=["EMP-101"])
    child_table: str = Field(min_length=1, examples=["employee_permissions"])
    values: dict[str, Any]


class ActionPreview(BaseModel):
    """Contract for future insert/update/delete confirmation dialogs."""

    model_config = ConfigDict(extra="forbid")

    action_type: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    affected_records: list[dict[str, Any]] = Field(default_factory=list)
    requires_confirmation: bool = True


class ParentChildRelationContract(BaseModel):
    """Stable description of the Feature 17 PostgreSQL relationship."""

    model_config = ConfigDict(extra="forbid")

    parent_table: str
    child_table: str
    parent_primary_key: str
    child_foreign_key: str
    cardinality: RelationshipCardinality
    rule: str
    sample_child_records: list[str]
    planned_implementation_phase: int
    deletion_policy: str


class ChildRelationshipContractData(BaseModel):
    """Payload for the child-table relationship endpoint."""

    model_config = ConfigDict(extra="forbid")

    database_connected: bool
    note: str
    relationships: list[ParentChildRelationContract]


class HealthData(BaseModel):
    """Payload returned by the health endpoint."""

    model_config = ConfigDict(extra="forbid")

    service: str
    environment: str
    version: str


class StatusData(BaseModel):
    """Payload returned by the Phase 2 status endpoint."""

    model_config = ConfigDict(extra="forbid")

    phase: int
    phase_name: str
    database_connected: bool
    database_message: str
    ollama_connected: bool
    chromadb_connected: bool
    available_routes: list[str]
    parent_child_relationship: str
