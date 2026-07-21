"""Deterministic compilation and execution of grounded semantic plans.

Ollama never writes SQL here.  SQLAlchemy compiles approved tables, columns, values,
filters, aggregates, and sort expressions from a schema-grounded plan.  The resulting
statement still passes through the existing SQL validator and MCP execution boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, Integer, Numeric, String, Text, and_, delete, func, insert, or_, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import ColumnElement
from sqlalchemy.sql.schema import Column, Table
from sqlalchemy.orm import Session

from app.core.constants import ResponseStatus, UserRole
from app.mcp.client import LocalMCPClient
from app.models import Base
from app.schemas.phase5 import SQLGenerationResult
from app.schemas.semantic_plan import ResolvedSemanticPlan, SemanticFilter
from app.services.crud_write_service import CrudWriteError, crud_write_service
from app.services.schema_metadata_question_service import SchemaQuestionRequest, answer_schema_metadata_question
from app.services.sql_validation import SQLValidationResult, validate_dml_sql
from app.services.synthetic_data_service import SyntheticDataRequest, generate_synthetic_records
from app.services.structured_read_service import StructuredReadService


class SemanticPlanExecutionError(RuntimeError):
    def __init__(self, *, status: ResponseStatus, code: str, message: str, details: list[dict[str, Any]] | None = None) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.details = details or []
        super().__init__(message)


@dataclass(frozen=True)
class SemanticReadResult:
    status: ResponseStatus
    answer: str
    rows: list[dict[str, Any]]
    row_count: int
    generated_sql: str
    validation: SQLValidationResult
    source_tables: list[str]
    plan_data: dict[str, Any]


@dataclass(frozen=True)
class SemanticWriteProposal:
    answer: str
    generated_sql: str | None
    pending_action_id: str
    data: dict[str, Any]


def _compile(statement: Any) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def _column(table: Table, name: str) -> Column[Any]:
    try:
        return table.c[name]
    except KeyError as exc:
        raise SemanticPlanExecutionError(
            status=ResponseStatus.CLARIFICATION_REQUIRED,
            code="semantic_column_not_found",
            message=f"The `{table.name}` table does not contain an approved `{name}` column.",
        ) from exc


def _coerce_value(column: Column[Any], value: Any) -> Any:
    if value is None:
        return None
    if isinstance(column.type, Boolean):
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().casefold()
        if normalized in {"true", "yes", "1", "active", "enabled"}:
            return True
        if normalized in {"false", "no", "0", "inactive", "disabled"}:
            return False
        raise SemanticPlanExecutionError(
            status=ResponseStatus.CLARIFICATION_REQUIRED,
            code="boolean_value_unclear",
            message=f"What boolean value should `{column.name}` use: true or false?",
        )
    if isinstance(column.type, Integer):
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise SemanticPlanExecutionError(
                status=ResponseStatus.CLARIFICATION_REQUIRED,
                code="integer_value_invalid",
                message=f"`{column.name}` requires an integer value.",
            ) from exc
    if isinstance(column.type, Numeric):
        try:
            return Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise SemanticPlanExecutionError(
                status=ResponseStatus.CLARIFICATION_REQUIRED,
                code="numeric_value_invalid",
                message=f"`{column.name}` requires a numeric value.",
            ) from exc
    if isinstance(column.type, DateTime):
        if isinstance(value, datetime):
            return value
        try:
            return datetime.fromisoformat(str(value))
        except ValueError as exc:
            raise SemanticPlanExecutionError(
                status=ResponseStatus.CLARIFICATION_REQUIRED,
                code="datetime_value_invalid",
                message=f"`{column.name}` requires an ISO date-time value.",
            ) from exc
    if isinstance(column.type, Date):
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value))
        except ValueError as exc:
            raise SemanticPlanExecutionError(
                status=ResponseStatus.CLARIFICATION_REQUIRED,
                code="date_value_invalid",
                message=f"`{column.name}` requires a date in YYYY-MM-DD format.",
            ) from exc
    return str(value).strip() if isinstance(column.type, (String, Text)) else value


def _filter_expression(table: Table, item: SemanticFilter) -> ColumnElement[bool]:
    column = _column(table, item.field)
    operator = item.operator
    if operator == "is_null":
        return column.is_(None)
    if operator == "not_null":
        return column.is_not(None)
    is_text = isinstance(column.type, (String, Text))
    if operator in {"in", "not_in"}:
        raw_values = item.value if isinstance(item.value, list) else [item.value]
        values = [_coerce_value(column, value) for value in raw_values]
        if is_text:
            comparisons = [column.ilike(str(value)) for value in values]
            expression = or_(*comparisons) if comparisons else column.is_(None) & column.is_not(None)
        else:
            expression = column.in_(values)
        return ~expression if operator == "not_in" else expression

    value = _coerce_value(column, item.value)
    if operator == "eq":
        return column.ilike(str(value)) if is_text else column == value
    if operator == "ne":
        return ~column.ilike(str(value)) if is_text else column != value
    if operator == "gt":
        return column > value
    if operator == "gte":
        return column >= value
    if operator == "lt":
        return column < value
    if operator == "lte":
        return column <= value
    if operator == "contains":
        return column.ilike(f"%{value}%")
    if operator == "starts_with":
        return column.ilike(f"{value}%")
    if operator == "ends_with":
        return column.ilike(f"%{value}")
    raise SemanticPlanExecutionError(
        status=ResponseStatus.CLARIFICATION_REQUIRED,
        code="filter_operator_unsupported",
        message=f"The filter operator `{operator}` is not supported.",
    )


def _where_clause(table: Table, filters: list[SemanticFilter]) -> ColumnElement[bool] | None:
    expressions = [_filter_expression(table, item) for item in filters]
    return and_(*expressions) if expressions else None


def _read_answer(plan: ResolvedSemanticPlan, rows: list[dict[str, Any]]) -> tuple[ResponseStatus, str]:
    intent = plan.plan.intent
    table = plan.canonical_table or "records"
    if intent in {"data.count", "data.aggregate"} and rows:
        row = rows[0]
        key = next(iter(row), "value")
        value = row.get(key)
        label = table.replace("_", " ")
        if intent == "data.count":
            return ResponseStatus.SUCCESS, f"The current database contains {value} matching {label} record{'s' if value != 1 else ''}."
        aggregate = plan.plan.aggregate or "aggregate"
        field = plan.canonical_aggregate_field or "value"
        return ResponseStatus.SUCCESS, f"The {aggregate} of `{field}` for the matching `{table}` records is {value}."
    return StructuredReadService._grounded_answer(
        row_count=len(rows),
        rows=rows,
        source_tables=[table],
    )


class SemanticQueryService:
    def __init__(self, *, mcp_client: LocalMCPClient | None = None) -> None:
        self._mcp_client = mcp_client or LocalMCPClient()

    @staticmethod
    def _table(plan: ResolvedSemanticPlan) -> Table:
        if not plan.canonical_table or plan.canonical_table not in Base.metadata.tables:
            raise SemanticPlanExecutionError(
                status=ResponseStatus.CLARIFICATION_REQUIRED,
                code="semantic_target_table_required",
                message="Which approved business table should this request use?",
            )
        return Base.metadata.tables[plan.canonical_table]

    def execute_schema(self, db: Session, *, plan: ResolvedSemanticPlan, role: UserRole) -> dict[str, Any]:
        intent = plan.plan.intent
        kind_map = {
            "schema.list_tables": "list_tables",
            "schema.table_exists": "table_exists",
            "schema.list_columns": "list_columns",
            "schema.column_exists": "column_exists",
        }
        if intent == "schema.relationships":
            table_name = plan.canonical_table
            if not table_name:
                raise SemanticPlanExecutionError(
                    status=ResponseStatus.CLARIFICATION_REQUIRED,
                    code="relationship_table_required",
                    message="Which table's relationships do you want to inspect?",
                )
            from app.services.schema_registry import get_table_schema

            schema = get_table_schema(table_name)
            relationships = schema.get("relationships") or []
            answer = (
                f"The `{table_name}` table has these relationships: "
                + "; ".join(
                    f"`{item['from_column']}` → `{item['to_table']}.{item['to_column']}`" for item in relationships
                )
                + "."
                if relationships
                else f"No approved foreign-key relationships were found for `{table_name}`."
            )
            return {
                "status": ResponseStatus.SUCCESS,
                "answer": answer,
                "data": {"schema_metadata": {"table": table_name, "relationships": relationships, "source": "sqlalchemy_metadata"}},
            }

        kind = kind_map.get(intent)
        if not kind:
            raise SemanticPlanExecutionError(
                status=ResponseStatus.CLARIFICATION_REQUIRED,
                code="schema_intent_unsupported",
                message="Please clarify which schema information you want: tables, columns, or relationships.",
            )
        requested_table = plan.plan.requested_table_text or plan.plan.target_table or plan.plan.target_entity
        requested_column = plan.plan.requested_columns[0] if plan.plan.requested_columns else None
        request = SchemaQuestionRequest(
            handled=True,
            kind=kind,  # type: ignore[arg-type]
            requested_table=requested_table,
            canonical_table=plan.canonical_table,
            requested_column=requested_column,
        )
        result = answer_schema_metadata_question(db, request=request, role=role)
        return {"status": result.status, "answer": result.answer, "data": result.data}

    def execute_read(self, *, plan: ResolvedSemanticPlan) -> SemanticReadResult:
        table = self._table(plan)
        intent = plan.plan.intent
        where_clause = _where_clause(table, plan.canonical_filters)

        if intent == "data.count":
            statement = select(func.count().label("count")).select_from(table)
        elif intent == "data.aggregate":
            aggregate = plan.plan.aggregate
            field = plan.canonical_aggregate_field
            if not aggregate or not field:
                raise SemanticPlanExecutionError(
                    status=ResponseStatus.CLARIFICATION_REQUIRED,
                    code="aggregate_definition_required",
                    message="Which aggregate and column should I use, for example average salary or maximum price?",
                )
            column = _column(table, field)
            aggregate_function = {
                "count": func.count,
                "sum": func.sum,
                "avg": func.avg,
                "min": func.min,
                "max": func.max,
            }[aggregate]
            statement = select(aggregate_function(column).label(f"{aggregate}_{field}")).select_from(table)
        else:
            selected_names = plan.canonical_columns or [column.name for column in table.columns]
            selected_columns = [_column(table, name) for name in selected_names]
            statement = select(*selected_columns).select_from(table)

        if where_clause is not None:
            statement = statement.where(where_clause)
        for sort_item in plan.canonical_sort:
            column = _column(table, sort_item.field)
            statement = statement.order_by(column.desc() if sort_item.direction == "desc" else column.asc())
        statement = statement.limit(plan.plan.limit or 50)

        sql = _compile(statement)
        validation = validate_dml_sql(sql)
        if not validation.is_valid:
            raise SemanticPlanExecutionError(
                status=ResponseStatus.VALIDATION_FAILED,
                code=validation.error_code or "semantic_read_validation_failed",
                message=validation.error_message or "The deterministic query failed the SQL safety validator.",
            )
        outcome = self._mcp_client.call_tool("execute_validated_read", {"sql": validation.normalized_sql or sql})
        if not outcome.get("ok"):
            raise SemanticPlanExecutionError(
                status=ResponseStatus.DATABASE_UNAVAILABLE,
                code=outcome.get("error_code", "semantic_read_tool_failed"),
                message=outcome.get("error_message", "The validated database read could not be completed."),
            )
        result = outcome.get("result")
        if not isinstance(result, dict) or not result.get("executed"):
            detail = result if isinstance(result, dict) else {}
            raise SemanticPlanExecutionError(
                status=ResponseStatus.VALIDATION_FAILED,
                code=detail.get("error", {}).get("code", "semantic_read_not_executed"),
                message=detail.get("error", {}).get("message", "The deterministic SELECT was not executed."),
            )
        rows = result.get("rows") if isinstance(result.get("rows"), list) else []
        rows = [row for row in rows if isinstance(row, dict)]
        status, answer = _read_answer(plan, rows)
        tool_validation = result.get("validation") if isinstance(result.get("validation"), dict) else validation.to_dict()
        final_validation = SQLValidationResult(
            is_valid=bool(tool_validation.get("is_valid", True)),
            statement_type=tool_validation.get("statement_type", "SELECT"),
            normalized_sql=tool_validation.get("normalized_sql", validation.normalized_sql or sql),
            tables=list(tool_validation.get("tables") or [table.name]),
            columns=list(tool_validation.get("columns") or []),
            warnings=list(tool_validation.get("warnings") or []),
            error_code=tool_validation.get("error_code"),
            error_message=tool_validation.get("error_message"),
            applied_limit=tool_validation.get("applied_limit"),
        )
        return SemanticReadResult(
            status=status,
            answer=answer,
            rows=rows,
            row_count=len(rows),
            generated_sql=final_validation.normalized_sql or sql,
            validation=final_validation,
            source_tables=[table.name],
            plan_data=plan.model_dump(mode="json"),
        )

    def propose_write(
        self,
        db: Session,
        *,
        plan: ResolvedSemanticPlan,
        session_id: Any,
        user_role: UserRole,
        user_prompt: str,
        model_metadata: dict[str, Any],
    ) -> SemanticWriteProposal:
        table = self._table(plan)
        intent = plan.plan.intent

        if intent == "synthetic.generate":
            request = SyntheticDataRequest(
                target_table=table.name,
                count=int(plan.plan.count or 0),
                constraints=dict(plan.canonical_values),
                source_text=user_prompt,
            )
            batch = generate_synthetic_records(db, request)
            result = crud_write_service.propose_bulk_insert(
                db,
                session_id=session_id,
                target_table=batch.target_table,
                records=batch.records,
                actor_role=user_role,
                user_prompt=user_prompt,
                generation_metadata=batch.metadata,
            )
            record_count = len(batch.records)
            return SemanticWriteProposal(
                answer=(
                    f"Generated exactly {record_count} synthetic `{table.name}` record{'s' if record_count != 1 else ''}. "
                    "The complete preview is shown below. No row has been inserted; explicit confirmation is required."
                ),
                generated_sql=None,
                pending_action_id=result.pending_action["pending_action_id"],
                data={
                    "target_table": table.name,
                    "pending_action": result.pending_action,
                    "preview": result.preview,
                    "rows": result.preview.get("records", []),
                    "requested_record_count": request.count,
                    "generated_record_count": record_count,
                    "preview_record_count": int(result.preview.get("record_count") or 0),
                    "count_verified": request.count == record_count == int(result.preview.get("record_count") or 0),
                    "duplicate_matches": result.duplicate_matches,
                    "generator": "faker",
                    "synthetic_generation": batch.metadata,
                    "semantic_plan": plan.model_dump(mode="json"),
                    "write_execution_allowed": False,
                },
            )

        values = {key: _coerce_value(_column(table, key), value) for key, value in plan.canonical_values.items()}
        where_clause = _where_clause(table, plan.canonical_filters)
        if intent == "write.insert":
            statement = insert(table).values(**values)
        elif intent == "write.update":
            if where_clause is None:
                raise SemanticPlanExecutionError(
                    status=ResponseStatus.CLARIFICATION_REQUIRED,
                    code="update_filter_required",
                    message="Which exact record should be updated? Provide its ID, code, or another unique filter.",
                )
            statement = update(table).where(where_clause).values(**values)
        elif intent == "write.delete":
            if where_clause is None:
                raise SemanticPlanExecutionError(
                    status=ResponseStatus.CLARIFICATION_REQUIRED,
                    code="delete_filter_required",
                    message="Which exact record should be deleted? Provide its ID, code, or another unique filter.",
                )
            statement = delete(table).where(where_clause)
        else:
            raise SemanticPlanExecutionError(
                status=ResponseStatus.CLARIFICATION_REQUIRED,
                code="write_intent_unsupported",
                message="Please clarify whether you want to insert, update, delete, or generate records.",
            )

        sql = _compile(statement)
        validation = validate_dml_sql(sql)
        if not validation.is_valid:
            raise SemanticPlanExecutionError(
                status=ResponseStatus.VALIDATION_FAILED,
                code=validation.error_code or "semantic_write_validation_failed",
                message=validation.error_message or "The deterministic write failed validation.",
            )
        try:
            result = crud_write_service.propose_sql_write(
                db,
                session_id=session_id,
                sql=validation.normalized_sql or sql,
                actor_role=user_role,
                user_prompt=user_prompt,
                model_metadata={**model_metadata, "generation_mode": "semantic_plan_sqlalchemy_compiler"},
            )
        except CrudWriteError as exc:
            raise SemanticPlanExecutionError(status=exc.status, code=exc.code, message=exc.message, details=exc.details) from exc
        return SemanticWriteProposal(
            answer="I understood the requested database change and created a validator-approved preview. No row was changed; explicit confirmation is required.",
            generated_sql=result.generated_sql,
            pending_action_id=result.pending_action["pending_action_id"],
            data={
                "target_table": table.name,
                "pending_action": result.pending_action,
                "preview": result.preview,
                "rows": result.preview.get("records", []) if isinstance(result.preview, dict) else [],
                "validation": result.validation.to_dict() if result.validation else validation.to_dict(),
                "duplicate_matches": result.duplicate_matches,
                "semantic_plan": plan.model_dump(mode="json"),
                "model_metadata": model_metadata,
                "write_execution_allowed": False,
            },
        )


semantic_query_service = SemanticQueryService()
