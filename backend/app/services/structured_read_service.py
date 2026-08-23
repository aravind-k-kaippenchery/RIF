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
from decimal import Decimal, InvalidOperation
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

    _ENTITY_LABELS: dict[str, tuple[str, str]] = {
        "employees": ("employee", "employees"),
        "vendors": ("vendor", "vendors"),
        "customers": ("customer", "customers"),
        "products": ("product", "products"),
        "sales_deals": ("sales deal", "sales deals"),
    }

    _COUNT_COLUMN_NAMES = {
        "count",
        "count(*)",
        "row_count",
        "total",
        "total_count",
        "employee_count",
        "vendor_count",
        "customer_count",
        "product_count",
        "sales_deal_count",
    }

    def __init__(self, *, mcp_client: LocalMCPClient | None = None) -> None:
        self._mcp_client = mcp_client or LocalMCPClient()

    @staticmethod
    def _text_value(row: dict[str, Any], key: str) -> str | None:
        """Return a clean text value from a database row, or None when unavailable."""

        value = row.get(key)
        if value is None:
            return None

        text = str(value).strip()
        return text or None

    @staticmethod
    def _join_human_readable(items: list[str]) -> str:
        """Join names safely as 'A', 'A and B', or 'A, B, and C'."""

        if not items:
            return ""

        if len(items) == 1:
            return items[0]

        if len(items) == 2:
            return f"{items[0]} and {items[1]}"

        return f"{', '.join(items[:-1])}, and {items[-1]}"

    @staticmethod
    def _aggregate_count(value: Any) -> int | None:
        """Normalize a non-negative integral SQL COUNT value."""

        if isinstance(value, bool) or value is None:
            return None

        try:
            normalized = Decimal(str(value).strip())
        except (InvalidOperation, ValueError):
            return None

        if not normalized.is_finite() or normalized < 0 or normalized != normalized.to_integral_value():
            return None

        return int(normalized)

    @classmethod
    def _common_row_value(
        cls,
        rows: list[dict[str, Any]],
        key: str,
    ) -> str | None:
        """Return one shared row value only when every row has the same value."""

        values = [cls._text_value(row, key) for row in rows]
        values = [value for value in values if value is not None]

        if not values:
            return None

        first_value = values[0]
        if all(value == first_value for value in values):
            return first_value

        return None

    @classmethod
    def _row_summary(
        cls,
        *,
        table_name: str,
        row: dict[str, Any],
    ) -> str | None:
        """Create a readable summary from one real database row only."""

        if table_name == "employees":
            first_name = cls._text_value(row, "first_name")
            last_name = cls._text_value(row, "last_name")
            employee_code = cls._text_value(row, "employee_code")
            department = cls._text_value(row, "department")

            full_name = " ".join(
                part for part in [first_name, last_name] if part
            ).strip()

            display_name = full_name or employee_code
            if not display_name:
                return None

            if department:
                return f"{display_name} ({department})"

            return display_name

        if table_name == "vendors":
            return (
                cls._text_value(row, "vendor_name")
                or cls._text_value(row, "name")
                or cls._text_value(row, "vendor_code")
            )

        if table_name == "customers":
            return (
                cls._text_value(row, "customer_name")
                or cls._text_value(row, "name")
                or cls._text_value(row, "customer_code")
            )

        if table_name == "products":
            product_name = (
                cls._text_value(row, "product_name")
                or cls._text_value(row, "name")
                or cls._text_value(row, "product_code")
            )
            category = cls._text_value(row, "category")

            if product_name and category:
                return f"{product_name} ({category})"

            return product_name

        if table_name == "sales_deals":
            deal_name = (
                cls._text_value(row, "deal_name")
                or cls._text_value(row, "name")
                or cls._text_value(row, "deal_code")
            )
            status = cls._text_value(row, "status")

            if deal_name and status:
                return f"{deal_name} ({status})"

            return deal_name

        return None

    @classmethod
    def _grounded_answer(
        cls,
        *,
        row_count: int,
        rows: list[dict[str, Any]],
        source_tables: list[str],
    ) -> tuple[ResponseStatus, str]:
        """Create a factual readable answer from real PostgreSQL rows only.

        This method never calls the LLM. It only uses database values already
        returned by the validated MCP read tool.
        """

        if row_count == 0:
            return (
                ResponseStatus.INFORMATION_NOT_AVAILABLE,
                "Information not available in the current database.",
            )

        normalized_tables = [
            str(table).strip().lower()
            for table in source_tables
            if str(table).strip()
        ]

        # Friendly entity-specific answers are safe only for one clear source table.
        if len(normalized_tables) != 1:
            if row_count == 1:
                return ResponseStatus.SUCCESS, "Found 1 matching record in the current database."

            return (
                ResponseStatus.SUCCESS,
                f"Found {row_count} matching records in the current database.",
            )

        table_name = normalized_tables[0]
        entity_labels = cls._ENTITY_LABELS.get(table_name)

        # COUNT(*) produces one database result row regardless of the number it
        # counts. Use the aggregate value rather than mistaking len(rows) == 1
        # for one matching entity.
        if len(rows) == 1:
            count_values = [
                value
                for key, value in rows[0].items()
                if str(key).strip().lower() in cls._COUNT_COLUMN_NAMES
                or str(key).strip().lower().endswith("_count")
            ]
            count = cls._aggregate_count(count_values[0]) if len(count_values) == 1 else None
            if count is not None:
                if entity_labels is None:
                    label = "matching record" if count == 1 else "matching records"
                else:
                    singular_label, plural_label = entity_labels
                    label = singular_label if count == 1 else plural_label
                return (
                    ResponseStatus.SUCCESS,
                    f"Found {count} {label} in the current database.",
                )

        if entity_labels is None:
            if row_count == 1:
                return ResponseStatus.SUCCESS, "Found 1 matching record in the current database."

            return (
                ResponseStatus.SUCCESS,
                f"Found {row_count} matching records in the current database.",
            )

        singular_label, plural_label = entity_labels
        entity_label = singular_label if row_count == 1 else plural_label

        visible_row_limit = 5
        visible_rows = rows[:visible_row_limit]

        row_summaries = [
            summary
            for summary in (
                cls._row_summary(table_name=table_name, row=row)
                for row in visible_rows
            )
            if summary
        ]

        # Employees, vendors, and customers can safely include a shared city,
        # but only when all returned rows have exactly the same stored city.
        shared_city = None
        if table_name in {"employees", "vendors", "customers"}:
            shared_city = cls._common_row_value(visible_rows, "city")

        location_phrase = f" in {shared_city}" if shared_city else ""

        if not row_summaries:
            if row_count == 1:
                return (
                    ResponseStatus.SUCCESS,
                    f"Found 1 {singular_label}{location_phrase} in the current database.",
                )

            return (
                ResponseStatus.SUCCESS,
                f"Found {row_count} {plural_label}{location_phrase} in the current database.",
            )

        summaries_text = cls._join_human_readable(row_summaries)

        if row_count <= visible_row_limit:
            return (
                ResponseStatus.SUCCESS,
                f"Found {row_count} {entity_label}{location_phrase}: {summaries_text}.",
            )

        return (
            ResponseStatus.SUCCESS,
            (
                f"Found {row_count} {entity_label}{location_phrase}. "
                f"First {len(row_summaries)}: {summaries_text}."
            ),
        )

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
                    source_references={
                        "source_type": "database",
                        "tables": source_tables,
                    },
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
        expected_tables: list[str] | None = None,
    ) -> StructuredReadResult:
        """Run one LLM-proposed, validator-approved SELECT through the MCP tool boundary."""

        started_at = perf_counter()

        proposal, initial_validation, model_metadata, glossary = llm_service.generate_sql(
            question,
            AgentRoute.STRUCTURED_READ.value,
            memory_context=memory_context,
        )

        explicit_tables = {
            str(table).strip().lower()
            for table in (expected_tables or [])
            if str(table).strip()
        }
        generated_tables = {
            str(table).strip().lower()
            for table in (initial_validation.tables or [])
            if str(table).strip()
        }
        if explicit_tables and not explicit_tables.issubset(generated_tables):
            raise StructuredReadError(
                status=ResponseStatus.CLARIFICATION_REQUIRED,
                code="generated_read_table_mismatch",
                message=(
                    "The generated query did not use the table you explicitly requested, so I rejected it before database execution. "
                    "Please restate the target table and filter."
                ),
            )

        outcome = self._mcp_client.call_tool(
            "execute_validated_read",
            {"sql": initial_validation.normalized_sql or proposal.sql},
        )

        if not outcome.get("ok"):
            raise StructuredReadError(
                status=ResponseStatus.DATABASE_UNAVAILABLE,
                code=outcome.get("error_code", "mcp_read_tool_failed"),
                message=outcome.get(
                    "error_message",
                    "The validated database read tool could not be completed.",
                ),
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
            validation_code = (
                tool_validation_payload.get("error_code")
                if isinstance(tool_validation_payload, dict)
                else None
            )
            validation_message = (
                tool_validation_payload.get("error_message")
                if isinstance(tool_validation_payload, dict)
                else None
            )

            raise StructuredReadError(
                status=ResponseStatus.VALIDATION_FAILED,
                code=validation_code or "structured_read_not_executed",
                message=validation_message
                or "The generated SELECT was not executed by the safety gate.",
            )

        rows = tool_result.get("rows")

        if not isinstance(rows, list) or not all(
            isinstance(row, dict) for row in rows
        ):
            raise StructuredReadError(
                status=ResponseStatus.TOOL_FAILED,
                code="mcp_read_rows_invalid",
                message="The database read tool returned invalid row data.",
            )

        source = tool_result.get("source")

        if not isinstance(source, dict):
            source = {
                "source_type": "database",
                "tables": initial_validation.tables,
            }

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

        returned_row_count = int(tool_result.get("returned_row_count", len(rows)) or len(rows))
        row_count = int(tool_result.get("total_row_count") or tool_result.get("row_count") or len(rows))
        if row_count < returned_row_count:
            row_count = returned_row_count

        normalized_source_tables = [str(table) for table in source_tables]

        status, answer = self._grounded_answer(
            row_count=row_count,
            rows=rows,
            source_tables=normalized_source_tables,
        )

        latency_ms = round((perf_counter() - started_at) * 1000)

        audit = self._write_query_log(
            request_id=request_id,
            session_id=session_id,
            question=question,
            generated_sql=final_validation.normalized_sql or proposal.sql,
            status=status,
            latency_ms=latency_ms,
            source_tables=normalized_source_tables,
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
