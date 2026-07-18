"""Read-only execution service used only after Phase 4 SQL validation succeeds."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

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
        result = db.execute(text(validation.normalized_sql or ""))
        rows = [_json_safe(dict(row)) for row in result.mappings().all()]
        db.rollback()  # release SET LOCAL transaction scope without changing data
    except SQLAlchemyError as exc:
        db.rollback()
        raise ValidatedReadError("Validated SELECT could not be executed against PostgreSQL.") from exc

    return {
        "executed": True,
        "validation": validation.to_dict(),
        "rows": rows,
        "row_count": len(rows),
        "source": {"source_type": "database", "tables": validation.tables},
    }
