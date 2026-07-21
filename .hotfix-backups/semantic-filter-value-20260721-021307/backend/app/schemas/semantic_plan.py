"""Typed semantic plans produced by the local Ollama understanding layer.

The model is never asked to generate SQL in this contract.  It only describes the
user's intended operation.  Backend services resolve the plan against the approved
live schema, ask for clarification when information is missing, and compile any SQL
deterministically with SQLAlchemy.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


SemanticIntent = Literal[
    "schema.list_tables",
    "schema.table_exists",
    "schema.list_columns",
    "schema.column_exists",
    "schema.relationships",
    "schema.create_table",
    "data.list",
    "data.filter",
    "data.count",
    "data.aggregate",
    "write.insert",
    "write.update",
    "write.delete",
    "synthetic.generate",
    "document.query",
    "hybrid.query",
    "conversation.show_previous",
    "conversation.confirm",
    "conversation.cancel",
    "clarification",
    "unsupported",
]

FilterOperator = Literal[
    "eq",
    "ne",
    "gt",
    "gte",
    "lt",
    "lte",
    "contains",
    "starts_with",
    "ends_with",
    "in",
    "not_in",
    "is_null",
    "not_null",
]

SortDirection = Literal["asc", "desc"]
AggregateFunction = Literal["count", "sum", "avg", "min", "max"]


class SemanticFilter(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    field: str = Field(min_length=1, max_length=128)
    operator: FilterOperator = "eq"
    value: Any | None = None


class SemanticSort(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    field: str = Field(min_length=1, max_length=128)
    direction: SortDirection = "asc"


class SemanticIntentPlan(BaseModel):
    """One conservative interpretation of the complete current user turn."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    intent: SemanticIntent
    operation: Literal["read", "insert", "update", "delete", "generate", "schema", "document", "hybrid", "conversation", "none"]
    target_entity: str | None = Field(default=None, max_length=128)
    target_table: str | None = Field(default=None, max_length=128)
    requested_table_text: str | None = Field(default=None, max_length=128)
    requested_columns: list[str] = Field(default_factory=list, max_length=30)
    filters: list[SemanticFilter] = Field(default_factory=list, max_length=20)
    sort: list[SemanticSort] = Field(default_factory=list, max_length=10)
    limit: int | None = Field(default=None, ge=1, le=100)
    aggregate: AggregateFunction | None = None
    aggregate_field: str | None = Field(default=None, max_length=128)
    values: dict[str, Any] = Field(default_factory=dict)
    count: int | None = Field(default=None, ge=1, le=50)
    parent_table: str | None = Field(default=None, max_length=128)
    parent_identifier: dict[str, Any] = Field(default_factory=dict)
    child_table: str | None = Field(default=None, max_length=128)
    references_previous_result: bool = False
    reference_expression: str | None = Field(default=None, max_length=160)
    confidence: float = Field(ge=0.0, le=1.0)
    requires_clarification: bool = False
    clarification_question: str | None = Field(default=None, max_length=500)
    missing_information: list[str] = Field(default_factory=list, max_length=20)
    ambiguities: list[str] = Field(default_factory=list, max_length=20)
    reasoning_summary: str = Field(min_length=1, max_length=500)


class ResolvedSemanticPlan(BaseModel):
    """Backend-grounded plan after table/column resolution and ambiguity checks."""

    model_config = ConfigDict(extra="forbid")

    plan: SemanticIntentPlan
    canonical_table: str | None = None
    canonical_columns: list[str] = Field(default_factory=list)
    canonical_filters: list[SemanticFilter] = Field(default_factory=list)
    canonical_sort: list[SemanticSort] = Field(default_factory=list)
    canonical_values: dict[str, Any] = Field(default_factory=dict)
    canonical_aggregate_field: str | None = None
    clarification_required: bool = False
    clarification_question: str | None = None
    missing_information: list[str] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)
    schema_grounded: bool = False
