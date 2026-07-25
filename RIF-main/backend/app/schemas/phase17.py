"""Typed contracts for semantic parent-child CRUD planning.

Ollama may interpret the user's language, but it never receives permission to execute
SQL.  The backend resolves parent business codes, validates child fields, builds bounded
SQLAlchemy statements, and asks for confirmation before any write.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


ParentChildOperation = Literal["read", "create", "update", "delete", "clarify"]
FilterOperator = Literal[
    "eq",
    "ne",
    "contains",
    "starts_with",
    "ends_with",
    "gt",
    "gte",
    "lt",
    "lte",
    "in",
    "is_null",
]
SelectionMode = Literal["all", "first", "oldest", "latest"]


class ParentReference(BaseModel):
    """One parent record referenced by a stable code or explicit numeric ID."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    role: str = Field(min_length=1, max_length=64)
    code: str | None = Field(default=None, min_length=1, max_length=128)
    id: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def require_one_identifier(self) -> "ParentReference":
        if self.code is None and self.id is None:
            raise ValueError("A parent reference requires either code or id.")
        return self


class ChildFilter(BaseModel):
    """One field-level selector over the child table."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    field: str = Field(min_length=1, max_length=128)
    operator: FilterOperator = "eq"
    value: Any = None


class ParentChildIntentPlan(BaseModel):
    """Semantic plan produced by Ollama and revalidated by deterministic Python code."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    operation: ParentChildOperation
    child_table: str | None = Field(default=None, max_length=128)
    parent_references: list[ParentReference] = Field(default_factory=list, max_length=4)
    filters: list[ChildFilter] = Field(default_factory=list, max_length=12)
    values: dict[str, Any] = Field(default_factory=dict)
    requested_columns: list[str] = Field(default_factory=list, max_length=30)
    selection: SelectionMode = "all"
    apply_to_all_matches: bool = False
    confidence: float = Field(ge=0.0, le=1.0)
    clarification_question: str | None = Field(default=None, max_length=500)
    interpretation: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def validate_clarification_shape(self) -> "ParentChildIntentPlan":
        if self.operation == "clarify" and not self.clarification_question:
            raise ValueError("clarification_question is required when operation is clarify.")
        return self
