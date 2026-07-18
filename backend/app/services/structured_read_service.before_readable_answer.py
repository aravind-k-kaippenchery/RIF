"""Phase 6 structured-read orchestration.

This service is the first end-to-end read-only path:

natural-language question
    -> Phase 3 glossary/schema context
    -> Phase 5 local LLM SQL proposal
    -> Phase 4 AST validation
    -> MCP execute_validated_read tool
    -> PostgreSQL rows
    -> deterministic grounded answer

The model never executes SQL. The final answer is assembled deterministically from
real tool output so that a read response cannot invent records or values.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter
from typing import Any
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from app.core.constants import AgentRoute, ResponseStatus
from app.core.logging_config import log_event
from app.db.session import get_session_factory
from app.mcp.client import LocalMCPClient
from app.models.operations import QueryLog
from app.schemas.phase5 import OllamaInvocationMetadata, SQLGenerationResult
from app.services.llm_service import LLMServiceError, llm_service
from app.services.sql_validation import SQLValidationResult


class StructuredReadError(RuntimeError):
    """Safe error raised after the LLM proposal boundary."""

    def __init__(self, *, status: ResponseStatus, code: str, message: str) -> None:
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class StructuredReadResult:
    """Controlled result returned by the Phase 6 read pipeline."""

    question: str
    proposal: SQLGenerationResult
    validation: SQLValidationResult
    glossary: dict[str, Any]
    model_metadata: OllamaInvocationMetadata
    rows: list[dict[str, Any]]
    row_count: int
    source: dict[str, Any]
    answer: str
    status: ResponseStatus
    audit: dict[str, Any]


class StructuredReadService:
    """Orchestrate only the allowed Phase 6 read path."""

    def __init__(self, *, mcp_client: LocalMCPClient | None = None) -> None:
        self._mcp_client = mcp_client or LocalMCPClient()

    @staticmethod
    def _grounded_answer(row_count: int) -> tuple[ResponseStatus, str]:
        """Produce a factual answer from the row count only, without another model call."""

        if row_count == 0:
            return ResponseStatus.INFORMATION_NOT_AVAILABLE, "Information not available in the current database."
        if row_count == 1:
            return ResponseStatus.SUCCESS, "Found 1 matching record in the current database."
        return ResponseStatus.SUCCESS, f"Found {row_count} matching records in the current database."

    @staticmethod
    def _safe_session_id(raw_session_id: str | None) -> UUID | None:
        """Accept only a UUID-shaped request session ID for optional query history."""

        if not raw_session_id:
            return None
        try:
            return UUID(raw_session_id)
        except (ValueError, AttributeError):
            return None

    def _write_query_log(
        self,
        *,
        request_id: str | None,
        session_id: str | None,
        question: str,
        generated_sql: str,
        status: ResponseStatus,
        latency_ms: int,
        source_tables: list[str],
        error_code: str | None = None,
    ) -> dict[str, Any]:
        """Best-effort PostgreSQL history record; logging failure never hides read results."""

        if not request_id:
            return {"stored": False, "reason": "request_id_unavailable"}

        db = None
        try:
            db = get_session_factory()()
            db.add(
                QueryLog(
                    request_id=request_id,
                    session_id=self._safe_session_id(session_id),
                    user_prompt=question,
                    detected_route=AgentRoute.STRUCTURED_READ.value,
                    generated_sql=generated_sql,
                    status=status.value,
                    latency_ms=max(0, latency_ms),
                    source_references={"source_type": "database", "tables": source_tables},
                    error_code=error_code,
                    created_at=datetime.now(timezone.utc),
                )
            )
            db.commit()
            return {"stored": True, "storage": "query_logs"}
        except SQLAlchemyError:
            if db is not None:
                db.rollback()
            log_event(
                level="WARNING",
                event="structured_read_query_log_unavailable",
                request_id=request_id,
                error_code="query_log_write_failed",
            )
            return {"stored": False, "reason": "query_log_write_failed"}
        finally:
            if db is not None:
                db.close()

    def execute(
        self,
        *,
        question: str,
        request_id: str | None = None,
        session_id: str | None = None,
        memory_context: dict[str, Any] | None = None,
    ) -> StructuredReadResult:
        """Run one LLM-proposed, validator-approved SELECT through the MCP tool boundary."""

        started_at = perf_counter()
        proposal, initial_validation, model_metadata, glossary = llm_service.generate_sql(
            question,
            AgentRoute.STRUCTURED_READ.value,
            memory_context=memory_context,
        )

        outcome = self._mcp_client.call_tool(
            "execute_validated_read",
            {"sql": initial_validation.normalized_sql or proposal.sql},
        )
        if not outcome.get("ok"):
            raise StructuredReadError(
                status=ResponseStatus.DATABASE_UNAVAILABLE,
                code=outcome.get("error_code", "mcp_read_tool_failed"),
                message=outcome.get("error_message", "The validated database read tool could not be completed."),
            )

        tool_result = outcome.get("result")
        if not isinstance(tool_result, dict):
            raise StructuredReadError(
                status=ResponseStatus.TOOL_FAILED,
                code="mcp_read_result_invalid",
                message="The validated database read tool returned an invalid result.",
            )

        tool_validation_payload = tool_result.get("validation")
        if not tool_result.get("executed"):
            validation_code = tool_validation_payload.get("error_code") if isinstance(tool_validation_payload, dict) else None
            validation_message = tool_validation_payload.get("error_message") if isinstance(tool_validation_payload, dict) else None
            raise StructuredReadError(
                status=ResponseStatus.VALIDATION_FAILED,
                code=validation_code or "structured_read_not_executed",
                message=validation_message or "The generated SELECT was not executed by the safety gate.",
            )

        rows = tool_result.get("rows")
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise StructuredReadError(
                status=ResponseStatus.TOOL_FAILED,
                code="mcp_read_rows_invalid",
                message="The database read tool returned invalid row data.",
            )

        source = tool_result.get("source")
        if not isinstance(source, dict):
            source = {"source_type": "database", "tables": initial_validation.tables}
        source_tables = source.get("tables")
        if not isinstance(source_tables, list):
            source_tables = initial_validation.tables
            source["tables"] = source_tables

        final_validation = initial_validation
        if isinstance(tool_validation_payload, dict):
            final_validation = SQLValidationResult(
                is_valid=bool(tool_validation_payload.get("is_valid")),
                statement_type=tool_validation_payload.get("statement_type"),
                normalized_sql=tool_validation_payload.get("normalized_sql"),
                tables=list(tool_validation_payload.get("tables") or []),
                columns=list(tool_validation_payload.get("columns") or []),
                warnings=list(tool_validation_payload.get("warnings") or []),
                error_code=tool_validation_payload.get("error_code"),
                error_message=tool_validation_payload.get("error_message"),
                applied_limit=tool_validation_payload.get("applied_limit"),
            )

        row_count = int(tool_result.get("row_count", len(rows)))
        if row_count != len(rows):
            row_count = len(rows)
        status, answer = self._grounded_answer(row_count)
        latency_ms = round((perf_counter() - started_at) * 1000)
        audit = self._write_query_log(
            request_id=request_id,
            session_id=session_id,
            question=question,
            generated_sql=final_validation.normalized_sql or proposal.sql,
            status=status,
            latency_ms=latency_ms,
            source_tables=[str(table) for table in source_tables],
        )
        log_event(
            level="INFO",
            event="structured_read_completed",
            request_id=request_id,
            session_id=session_id,
            route=AgentRoute.STRUCTURED_READ.value,
            row_count=row_count,
            source_tables=source_tables,
            status=status.value,
            latency_ms=latency_ms,
        )

        return StructuredReadResult(
            question=question,
            proposal=proposal,
            validation=final_validation,
            glossary=glossary,
            model_metadata=model_metadata,
            rows=rows,
            row_count=row_count,
            source=source,
            answer=answer,
            status=status,
            audit=audit,
        )


structured_read_service = StructuredReadService()
