"""Read-only execution service used only after Phase 4 SQL validation succeeds."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlglot import exp, parse_one

from app.core.config import get_settings
from app.services.sql_validation import SQLValidationResult, validate_dml_sql


class ValidatedReadError(RuntimeError):
    """Raised when a SELECT cannot be executed after validation."""


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value




def _total_count_for_simple_select(db: Session, sql: str) -> int | None:
    """Return total matches for a simple non-aggregate SELECT before LIMIT/OFFSET.

    The SQL has already passed the project validator.  This helper is only used to
    make read answers honest when a safe LIMIT is applied.  It deliberately avoids
    aggregate, grouped, distinct, and compound queries because their row-count
    meaning is different from "business records matching the filter".
    """

    try:
        expression = parse_one(sql, read="postgres")
    except Exception:
        return None
    if not isinstance(expression, exp.Select):
        return None
    if expression.args.get("group") or expression.args.get("distinct"):
        return None
    if expression.find(exp.AggFunc) is not None:
        return None
    if expression.find(exp.Union) is not None:
        return None
    counted = expression.copy()
    counted.set("limit", None)
    counted.set("offset", None)
    counted.set("order", None)
    count_sql = f"SELECT COUNT(*) AS total_count FROM ({counted.sql(dialect='postgres')}) AS rif_counted_rows"
    try:
        return int(db.scalar(text(count_sql)) or 0)
    except Exception:
        return None

def execute_validated_read(db: Session, sql: str) -> dict[str, Any]:
    """Validate and execute one SELECT statement with fixed server-side safeguards."""

    validation: SQLValidationResult = validate_dml_sql(sql)
    if not validation.is_valid:
        return {
            "executed": False,
            "validation": validation.to_dict(),
            "rows": [],
            "row_count": 0,
        }
    if validation.statement_type != "SELECT":
        return {
            "executed": False,
            "validation": validation.to_dict(),
            "rows": [],
            "row_count": 0,
            "error": {
                "code": "read_tool_select_only",
                "message": "The execute_validated_read tool can run SELECT statements only.",
            },
        }

    settings = get_settings()
    try:
        # Controlled literal, never user-provided, is safe to use in the timeout statement.
        db.execute(text(f"SET LOCAL statement_timeout = '{settings.sql_statement_timeout_ms}ms'"))
        normalized_sql = validation.normalized_sql or ""
        result = db.execute(text(normalized_sql))
        rows = [_json_safe(dict(row)) for row in result.mappings().all()]
        total_row_count = _total_count_for_simple_select(db, normalized_sql)
        db.rollback()  # release SET LOCAL transaction scope without changing data
    except SQLAlchemyError as exc:
        db.rollback()
        raise ValidatedReadError("Validated SELECT could not be executed against PostgreSQL.") from exc

    return {
        "executed": True,
        "validation": validation.to_dict(),
        "rows": rows,
        "row_count": total_row_count if total_row_count is not None else len(rows),
        "returned_row_count": len(rows),
        "total_row_count": total_row_count,
        "limit_reached": total_row_count is not None and len(rows) < total_row_count,
        "source": {"source_type": "database", "tables": validation.tables},
    }
