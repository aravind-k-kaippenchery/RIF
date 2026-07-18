"""Phase 4 SQL validation gate.

This module validates SQL with sqlglot's PostgreSQL AST before any tool can use it.
It deliberately accepts only one simple DML statement for normal users. SELECT can
be executed through the read-only MCP tool; INSERT/UPDATE/DELETE are proposal-only
until the confirmation workflow is implemented in Phase 7.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

import sqlglot
from sqlglot import exp

from app.core.config import get_settings
from app.core.constants import UserRole
from app.services.schema_registry import get_allowed_columns, get_allowed_tables, get_relationships


ALLOWED_DML = {"SELECT", "INSERT", "UPDATE", "DELETE"}
FORBIDDEN_TOKENS = {
    "DROP",
    "TRUNCATE",
    "GRANT",
    "REVOKE",
    "COPY",
    "VACUUM",
    "ANALYZE",
    "CALL",
    "DO",
    "CREATE EXTENSION",
    "SET ROLE",
    "SET SESSION",
    "PG_SLEEP",
    "DBLINK",
    "LO_IMPORT",
    "LO_EXPORT",
}
ALLOWED_ADMIN_TYPES = {
    "BIGINT",
    "BOOLEAN",
    "DATE",
    "INT",
    "INTEGER",
    "NUMERIC",
    "TEXT",
    "TIMESTAMP",
    "TIMESTAMPTZ",
    "UUID",
    "VARCHAR",
}


@dataclass
class SQLValidationResult:
    """Structured validation result returned by REST endpoints and MCP tools."""

    is_valid: bool
    statement_type: str | None = None
    normalized_sql: str | None = None
    tables: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    applied_limit: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_valid": self.is_valid,
            "statement_type": self.statement_type,
            "normalized_sql": self.normalized_sql,
            "tables": self.tables,
            "columns": self.columns,
            "warnings": self.warnings,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "applied_limit": self.applied_limit,
        }


def _invalid(code: str, message: str, *, statement_type: str | None = None, tables: list[str] | None = None, columns: list[str] | None = None) -> SQLValidationResult:
    return SQLValidationResult(
        is_valid=False,
        statement_type=statement_type,
        tables=tables or [],
        columns=columns or [],
        error_code=code,
        error_message=message,
    )


def _compact_sql(sql: str) -> str:
    return " ".join(sql.strip().split())


def _contains_disallowed_text(sql: str) -> tuple[str, str] | None:
    upper = sql.upper()
    if "--" in sql or "/*" in sql or "*/" in sql:
        return "sql_comments_blocked", "SQL comments are not allowed in model-generated SQL."
    for token in FORBIDDEN_TOKENS:
        token_pattern = re.escape(token).replace(r"\ ", r"\s+")
        if re.search(rf"(?<![A-Z0-9_]){token_pattern}(?![A-Z0-9_])", upper):
            return "forbidden_sql_operation", f"The SQL operation '{token}' is never allowed."
    return None


def _statement_type(expression: exp.Expression) -> str | None:
    if isinstance(expression, exp.Select):
        return "SELECT"
    if isinstance(expression, exp.Insert):
        return "INSERT"
    if isinstance(expression, exp.Update):
        return "UPDATE"
    if isinstance(expression, exp.Delete):
        return "DELETE"
    return None


def _extract_tables(expression: exp.Expression) -> tuple[list[str], dict[str, str], SQLValidationResult | None]:
    allowed = set(get_allowed_tables())
    tables: list[str] = []
    aliases: dict[str, str] = {}

    for table in expression.find_all(exp.Table):
        if table.args.get("db") is not None or table.args.get("catalog") is not None:
            return [], {}, _invalid("schema_qualified_table_blocked", "Schema-qualified or catalog-qualified table names are not allowed.")
        table_name = table.name.lower()
        if table_name not in allowed:
            return [], {}, _invalid("unknown_table", f"Table '{table_name}' is not in the approved application schema.")
        if table_name not in tables:
            tables.append(table_name)
        alias = table.alias_or_name.lower()
        aliases[alias] = table_name
        aliases[table_name] = table_name

    if not tables:
        return [], {}, _invalid("missing_table", "The SQL statement must reference an approved application table.")
    return tables, aliases, None


def _validate_columns(expression: exp.Expression, tables: list[str], aliases: dict[str, str]) -> tuple[list[str], SQLValidationResult | None]:
    stars = list(expression.find_all(exp.Star))
    if any(not isinstance(star.parent, exp.Count) for star in stars):
        return [], _invalid("select_star_blocked", "Use explicit approved columns instead of SELECT *. COUNT(*) is the only allowed star expression.", tables=tables)

    referenced: list[str] = []
    allowed_by_table = {table: set(get_allowed_columns(table)) for table in tables}

    for column in expression.find_all(exp.Column):
        column_name = column.name.lower()
        qualifier = column.table.lower() if column.table else ""
        target_table: str | None = None

        if qualifier:
            target_table = aliases.get(qualifier)
            if target_table is None:
                return [], _invalid(
                    "unknown_table_alias",
                    f"Column '{column.sql(dialect='postgres')}' uses unknown table or alias '{qualifier}'.",
                    tables=tables,
                )
            if column_name not in allowed_by_table[target_table]:
                return [], _invalid(
                    "unknown_column",
                    f"Column '{column_name}' is not approved for table '{target_table}'.",
                    tables=tables,
                )
        else:
            matches = [table for table, columns in allowed_by_table.items() if column_name in columns]
            if not matches:
                return [], _invalid("unknown_column", f"Column '{column_name}' is not approved for the referenced tables.", tables=tables)
            if len(matches) > 1:
                return [], _invalid(
                    "ambiguous_column",
                    f"Column '{column_name}' exists in multiple tables. Qualify it with a table name or alias.",
                    tables=tables,
                )
            target_table = matches[0]
        reference = f"{target_table}.{column_name}"
        if reference not in referenced:
            referenced.append(reference)

    return referenced, None


def _validate_joins(expression: exp.Expression, tables: list[str], aliases: dict[str, str]) -> SQLValidationResult | None:
    """Require joins to use a declared application foreign-key relationship."""

    relationships = {
        frozenset(((item["from_table"], item["from_column"]), (item["to_table"], item["to_column"])))
        for item in get_relationships()
    }
    for join in expression.find_all(exp.Join):
        on_clause = join.args.get("on")
        if on_clause is None:
            return _invalid("join_condition_required", "Every JOIN must use an explicit ON condition.", tables=tables)
        comparisons = list(on_clause.find_all(exp.EQ))
        if len(comparisons) != 1:
            return _invalid("unsafe_join", "JOIN conditions must contain exactly one approved foreign-key equality.", tables=tables)
        left, right = comparisons[0].left, comparisons[0].right
        if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
            return _invalid("unsafe_join", "JOIN conditions must compare two approved columns.", tables=tables)
        left_table = aliases.get(left.table.lower() if left.table else "")
        right_table = aliases.get(right.table.lower() if right.table else "")
        if left_table is None or right_table is None:
            return _invalid("unsafe_join", "JOIN columns must be qualified with approved table aliases.", tables=tables)
        pair = frozenset(((left_table, left.name.lower()), (right_table, right.name.lower())))
        if pair not in relationships:
            return _invalid("unsafe_join", "JOIN condition does not match an approved application foreign-key relationship.", tables=tables)
    return None

def _limit_value(expression: exp.Expression) -> int | None:
    limit = expression.args.get("limit")
    if limit is None:
        return None
    literal = limit.expression
    if not isinstance(literal, exp.Literal) or not literal.is_int:
        return -1
    return int(literal.this)


def _apply_select_limit(expression: exp.Select) -> tuple[exp.Select, int | None, SQLValidationResult | None]:
    settings = get_settings()
    current_limit = _limit_value(expression)
    if current_limit == -1:
        return expression, None, _invalid("invalid_limit", "SELECT LIMIT must be a positive integer literal.")
    if current_limit is not None:
        if current_limit < 1:
            return expression, None, _invalid("invalid_limit", "SELECT LIMIT must be at least 1.")
        if current_limit > settings.sql_read_max_rows:
            return expression, None, _invalid(
                "select_limit_too_large",
                f"SELECT LIMIT cannot exceed {settings.sql_read_max_rows} rows.",
            )
        return expression, current_limit, None

    expression.set("limit", exp.Limit(expression=exp.Literal.number(settings.sql_read_max_rows)))
    return expression, settings.sql_read_max_rows, None


def _validate_insert_columns(expression: exp.Insert, tables: list[str]) -> SQLValidationResult | None:
    target = expression.this
    if not isinstance(target, exp.Schema):
        return _invalid(
            "insert_columns_required",
            "INSERT must explicitly name approved target columns.",
            statement_type="INSERT",
            tables=tables,
        )
    table = target.this
    if not isinstance(table, exp.Table):
        return _invalid("invalid_insert_target", "INSERT target must be an approved table.", statement_type="INSERT", tables=tables)
    target_table = table.name.lower()
    allowed_columns = set(get_allowed_columns(target_table))
    column_names = [identifier.name.lower() for identifier in target.expressions if isinstance(identifier, exp.Identifier)]
    if not column_names:
        return _invalid("insert_columns_required", "INSERT must name at least one target column.", statement_type="INSERT", tables=tables)
    blocked_managed = {"id", "created_at", "updated_at"}
    for column in column_names:
        if column not in allowed_columns:
            return _invalid("unknown_column", f"Column '{column}' is not approved for table '{target_table}'.", statement_type="INSERT", tables=tables)
        if column in blocked_managed:
            return _invalid("managed_column_blocked", f"Column '{column}' is managed by the server and cannot be set directly.", statement_type="INSERT", tables=tables)
    return None


def validate_dml_sql(sql: str, *, role: UserRole = UserRole.NORMAL_USER) -> SQLValidationResult:
    """Validate exactly one supported DML statement without executing it."""

    compact = _compact_sql(sql)
    if not compact:
        return _invalid("empty_sql", "SQL cannot be empty.")

    text_error = _contains_disallowed_text(compact)
    if text_error:
        return _invalid(*text_error)

    try:
        expressions = sqlglot.parse(compact, read="postgres")
    except sqlglot.errors.ParseError as exc:
        return _invalid("sql_parse_error", f"SQL could not be parsed safely: {exc}")

    expressions = [expression for expression in expressions if expression is not None]
    if len(expressions) != 1:
        return _invalid("multiple_statements_blocked", "Exactly one SQL statement is allowed per request.")

    expression = expressions[0]
    statement_type = _statement_type(expression)
    if statement_type not in ALLOWED_DML:
        return _invalid(
            "unsupported_sql_operation",
            "Only SELECT, INSERT, UPDATE, and DELETE proposals are allowed for normal users.",
        )

    tables, aliases, table_error = _extract_tables(expression)
    if table_error:
        table_error.statement_type = statement_type
        return table_error

    if statement_type == "SELECT":
        if not isinstance(expression, exp.Select):
            return _invalid("invalid_select", "Only a direct SELECT statement is accepted.", statement_type=statement_type, tables=tables)
        if expression.args.get("with_") is not None or list(expression.find_all(exp.Subquery)):
            return _invalid("subquery_blocked", "Common table expressions and subqueries are not allowed in Phase 4.", statement_type=statement_type, tables=tables)
        join_error = _validate_joins(expression, tables, aliases)
        if join_error:
            join_error.statement_type = statement_type
            return join_error
        columns, column_error = _validate_columns(expression, tables, aliases)
        if column_error:
            column_error.statement_type = statement_type
            return column_error
        expression, applied_limit, limit_error = _apply_select_limit(expression)
        if limit_error:
            limit_error.statement_type = statement_type
            limit_error.tables = tables
            limit_error.columns = columns
            return limit_error
        return SQLValidationResult(
            is_valid=True,
            statement_type=statement_type,
            normalized_sql=expression.sql(dialect="postgres"),
            tables=tables,
            columns=columns,
            warnings=[f"A server-controlled LIMIT of {applied_limit} row(s) is enforced."] if applied_limit == get_settings().sql_read_max_rows else [],
            applied_limit=applied_limit,
        )

    if statement_type == "INSERT":
        if not isinstance(expression, exp.Insert):
            return _invalid("invalid_insert", "Invalid INSERT syntax.", statement_type=statement_type, tables=tables)
        insert_error = _validate_insert_columns(expression, tables)
        if insert_error:
            return insert_error
        return SQLValidationResult(
            is_valid=True,
            statement_type=statement_type,
            normalized_sql=expression.sql(dialect="postgres"),
            tables=tables,
            warnings=["INSERT is proposal-only in Phase 4 and cannot be executed until confirmation workflow implementation."],
        )

    if statement_type in {"UPDATE", "DELETE"}:
        if expression.args.get("where") is None:
            return _invalid(
                "where_clause_required",
                f"{statement_type} requires a WHERE clause to protect all rows.",
                statement_type=statement_type,
                tables=tables,
            )
        columns, column_error = _validate_columns(expression, tables, aliases)
        if column_error:
            column_error.statement_type = statement_type
            return column_error
        return SQLValidationResult(
            is_valid=True,
            statement_type=statement_type,
            normalized_sql=expression.sql(dialect="postgres"),
            tables=tables,
            columns=columns,
            warnings=[f"{statement_type} is proposal-only in Phase 4 and requires confirmation in Phase 7."],
        )

    return _invalid("validation_failed", "SQL validation failed unexpectedly.", statement_type=statement_type, tables=tables)


def _data_type_name(column_def: exp.ColumnDef) -> str | None:
    kind = column_def.args.get("kind")
    if not isinstance(kind, exp.DataType):
        return None
    return kind.this.name.upper() if hasattr(kind.this, "name") else str(kind.this).upper()


def _column_preview(column_def: exp.ColumnDef) -> dict[str, Any]:
    kind = column_def.args.get("kind")
    return {
        "column_name": column_def.this.name.lower(),
        "data_type": kind.sql(dialect="postgres") if kind is not None else None,
        "nullable": not bool(column_def.args.get("constraints")),
    }


def preview_admin_schema_change(sql: str) -> SQLValidationResult:
    """Validate a restricted admin-only CREATE TABLE or ALTER TABLE ADD COLUMN proposal.

    This function validates and previews only. It never applies a DDL statement.
    """

    compact = _compact_sql(sql)
    if not compact:
        return _invalid("empty_sql", "SQL cannot be empty.")
    text_error = _contains_disallowed_text(compact)
    if text_error:
        return _invalid(*text_error)
    try:
        expressions = sqlglot.parse(compact, read="postgres")
    except sqlglot.errors.ParseError as exc:
        return _invalid("sql_parse_error", f"SQL could not be parsed safely: {exc}")
    expressions = [expression for expression in expressions if expression is not None]
    if len(expressions) != 1:
        return _invalid("multiple_statements_blocked", "Exactly one schema statement is allowed.")

    expression = expressions[0]
    if isinstance(expression, exp.Create):
        if str(expression.args.get("kind", "")).upper() != "TABLE" or not isinstance(expression.this, exp.Schema):
            return _invalid("restricted_ddl_only", "Admin mode allows only CREATE TABLE with explicit columns.")
        table = expression.this.this
        if not isinstance(table, exp.Table):
            return _invalid("invalid_create_table", "CREATE TABLE must use a simple table name.")
        table_name = table.name.lower()
        if table_name in set(get_allowed_tables()):
            return _invalid("table_already_exists", f"Table '{table_name}' already exists in the approved schema.")
        columns = [item for item in expression.this.expressions if isinstance(item, exp.ColumnDef)]
        if not columns:
            return _invalid("columns_required", "CREATE TABLE needs at least one explicit column.")
        previews = []
        for column in columns:
            type_name = _data_type_name(column)
            if type_name not in ALLOWED_ADMIN_TYPES:
                return _invalid("unsupported_column_type", f"Column type '{type_name}' is not allowed in restricted admin mode.")
            previews.append(_column_preview(column))
        return SQLValidationResult(
            is_valid=True,
            statement_type="CREATE_TABLE_PREVIEW",
            normalized_sql=expression.sql(dialect="postgres"),
            tables=[table_name],
            columns=[f"{table_name}.{item['column_name']}" for item in previews],
            warnings=["Preview only. No schema change is executed in Phase 4.", "Explicit admin confirmation and an audit record are required before any future execution."],
        )

    if isinstance(expression, exp.Alter):
        if str(expression.args.get("kind", "")).upper() != "TABLE" or not isinstance(expression.this, exp.Table):
            return _invalid("restricted_ddl_only", "Admin mode allows only ALTER TABLE ADD COLUMN.")
        table_name = expression.this.name.lower()
        if table_name not in set(get_allowed_tables()):
            return _invalid("unknown_table", f"Table '{table_name}' is not in the approved application schema.")
        actions = expression.args.get("actions") or []
        if len(actions) != 1 or not isinstance(actions[0], exp.ColumnDef):
            return _invalid("restricted_ddl_only", "Admin mode allows exactly one ALTER TABLE ADD COLUMN operation.")
        column_def = actions[0]
        type_name = _data_type_name(column_def)
        if type_name not in ALLOWED_ADMIN_TYPES:
            return _invalid("unsupported_column_type", f"Column type '{type_name}' is not allowed in restricted admin mode.")
        existing = set(get_allowed_columns(table_name))
        if column_def.this.name.lower() in existing:
            return _invalid("column_already_exists", f"Column '{column_def.this.name.lower()}' already exists in table '{table_name}'.")
        return SQLValidationResult(
            is_valid=True,
            statement_type="ALTER_TABLE_ADD_COLUMN_PREVIEW",
            normalized_sql=expression.sql(dialect="postgres"),
            tables=[table_name],
            columns=[f"{table_name}.{column_def.this.name.lower()}"],
            warnings=["Preview only. No schema change is executed in Phase 4.", "Explicit admin confirmation and an audit record are required before any future execution."],
        )

    return _invalid("restricted_ddl_only", "Admin mode permits only CREATE TABLE and ALTER TABLE ADD COLUMN previews.")
