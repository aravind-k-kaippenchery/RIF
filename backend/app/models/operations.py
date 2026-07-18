"""Operational SQLAlchemy models required by later backend phases."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.mixins import TimestampMixin


class Session(TimestampMixin, Base):
    __tablename__ = "sessions"

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    user_role: Mapped[str] = mapped_column(String(32), nullable=False, default="normal_user", server_default="normal_user")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", server_default="active")
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    pending_actions: Mapped[list[PendingAction]] = relationship(back_populates="session")
    query_logs: Mapped[list[QueryLog]] = relationship(back_populates="session")
    action_logs: Mapped[list[ActionLog]] = relationship(back_populates="session")
    schema_change_requests: Mapped[list[SchemaChangeRequest]] = relationship(back_populates="session")


class PendingAction(TimestampMixin, Base):
    __tablename__ = "pending_actions"

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(ForeignKey("sessions.id", ondelete="RESTRICT"), nullable=False, index=True)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_table: Mapped[Optional[str]] = mapped_column(String(128))
    validated_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    preview_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    generated_sql: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", server_default="pending", index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    session: Mapped[Session] = relationship(back_populates="pending_actions")
    action_logs: Mapped[list[ActionLog]] = relationship(back_populates="pending_action")


class QueryLog(Base):
    __tablename__ = "query_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    session_id: Mapped[Optional[UUID]] = mapped_column(ForeignKey("sessions.id", ondelete="SET NULL"), index=True)
    user_prompt: Mapped[Optional[str]] = mapped_column(Text)
    detected_route: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    generated_sql: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer)
    source_references: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    error_code: Mapped[Optional[str]] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    session: Mapped[Optional[Session]] = relationship(back_populates="query_logs")


class ActionLog(Base):
    __tablename__ = "action_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    session_id: Mapped[Optional[UUID]] = mapped_column(ForeignKey("sessions.id", ondelete="SET NULL"), index=True)
    pending_action_id: Mapped[Optional[UUID]] = mapped_column(ForeignKey("pending_actions.id", ondelete="SET NULL"), index=True)
    actor_role: Mapped[str] = mapped_column(String(32), nullable=False)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_table: Mapped[Optional[str]] = mapped_column(String(128))
    affected_record_ids: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    generated_sql: Mapped[Optional[str]] = mapped_column(Text)
    confirmation_status: Mapped[Optional[str]] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    session: Mapped[Optional[Session]] = relationship(back_populates="action_logs")
    pending_action: Mapped[Optional[PendingAction]] = relationship(back_populates="action_logs")
    snapshots: Mapped[list[ChangeSnapshot]] = relationship(back_populates="action_log")


class ChangeSnapshot(Base):
    __tablename__ = "change_snapshots"

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    action_log_id: Mapped[int] = mapped_column(ForeignKey("action_logs.id", ondelete="RESTRICT"), nullable=False, index=True)
    table_name: Mapped[str] = mapped_column(String(128), nullable=False)
    record_id: Mapped[str] = mapped_column(String(128), nullable=False)
    snapshot_type: Mapped[str] = mapped_column(String(16), nullable=False)
    snapshot_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    action_log: Mapped[ActionLog] = relationship(back_populates="snapshots")


class Document(TimestampMixin, Base):
    __tablename__ = "documents"

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    file_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    file_type: Mapped[str] = mapped_column(String(32), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    page_count: Mapped[Optional[int]] = mapped_column(Integer)
    ocr_used: Mapped[bool] = mapped_column(nullable=False, default=False, server_default="false")
    ingestion_status: Mapped[str] = mapped_column(String(64), nullable=False, default="pending", server_default="pending", index=True)
    extracted_text_path: Mapped[Optional[str]] = mapped_column(String(500))
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    document_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    ingestion_jobs: Mapped[list[DocumentIngestionJob]] = relationship(back_populates="document")


class DocumentIngestionJob(Base):
    __tablename__ = "document_ingestion_jobs"

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="queued", server_default="queued", index=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    ocr_duration_ms: Mapped[Optional[int]] = mapped_column(Integer)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    document: Mapped[Document] = relationship(back_populates="ingestion_jobs")


class BenchmarkRun(Base):
    __tablename__ = "benchmark_runs"

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    test_name: Mapped[str] = mapped_column(String(200), nullable=False)
    route: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    metric_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    metric_value: Mapped[Optional[float]] = mapped_column(nullable=True)
    metric_unit: Mapped[Optional[str]] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    details: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class SchemaChangeRequest(TimestampMixin, Base):
    __tablename__ = "schema_change_requests"

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    session_id: Mapped[Optional[UUID]] = mapped_column(ForeignKey("sessions.id", ondelete="SET NULL"), index=True)
    requested_by_role: Mapped[str] = mapped_column(String(32), nullable=False)
    user_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    proposed_sql: Mapped[Optional[str]] = mapped_column(Text)
    schema_preview: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="pending", server_default="pending", index=True)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[Optional[str]] = mapped_column(Text)

    session: Mapped[Optional[Session]] = relationship(back_populates="schema_change_requests")
