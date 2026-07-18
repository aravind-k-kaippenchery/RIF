"""Server-side semantic guard for explicit structured-read location filters.

The SQL validator proves that a model-generated SELECT is syntactically safe and uses
approved schema. It cannot, by itself, prove that every explicit user filter was kept.
This small guard closes that gap for unambiguous location requests such as:

    Show employees from Mars
    Show workers who live in Bangalore
    Find vendors based in Chennai

When such a location is present, the SQL proposal must contain a city predicate with the
same value. A proposal that silently drops the location filter is rejected and may use the
existing one safe correction retry. If it still fails, no database read is executed.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

import sqlglot
from sqlglot import exp


@dataclass(frozen=True)
class QueryConstraintCheck:
    """Result of checking explicit structured-read constraints against generated SQL."""

    is_valid: bool
    error_message: str = ""
    expected_city: str | None = None


# The first two patterns are intentionally explicit. They cover common natural-language
# location requests without treating every word after "in" as a city.
_LOCATION_PATTERNS = (
    re.compile(
        r"\b(?:from|based\s+in|located\s+in|live\s+in|lives\s+in|living\s+in)\s+(?P<city>[A-Za-z][A-Za-z .'-]{0,80})",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"\bin\s+(?P<city>[A-Z][A-Za-z .'-]{0,80})",
        flags=re.IGNORECASE,
    ),
)

# These words normally begin another filter clause, not part of a city name.
_LOCATION_STOPWORDS = re.compile(
    r"\b(?:with|whose|who|and|where|having|salary|department|company|status|earning|below|above|under|over|paid)\b",
    flags=re.IGNORECASE,
)

# Avoid false positives for phrases such as "employees in the Sales department".
_NON_LOCATION_TERMS = {
    "sales",
    "finance",
    "hr",
    "it",
    "department",
    "company",
    "team",
    "office",
}


def _normalize(value: str) -> str:
    return " ".join(value.casefold().strip().split())


def _extract_explicit_city(question: str) -> str | None:
    """Return one explicit location value only when the wording is unambiguous enough."""

    for pattern in _LOCATION_PATTERNS:
        match = pattern.search(question)
        if match is None:
            continue
        candidate = _LOCATION_STOPWORDS.split(match.group("city"), maxsplit=1)[0]
        candidate = candidate.strip(" .,!?:;\t\n")
        candidate = re.sub(r"^the\s+", "", candidate, flags=re.IGNORECASE).strip()
        words = candidate.split()
        if not words or len(words) > 3:
            continue
        normalized = _normalize(candidate)
        if normalized in _NON_LOCATION_TERMS:
            continue
        return candidate
    return None


def _has_matching_city_predicate(where_clause: exp.Expression, expected_city: str) -> bool:
    """Check whether a WHERE predicate compares city with the requested location literal."""

    expected = _normalize(expected_city)

    def predicate_matches(predicate: exp.Expression) -> bool:
        columns = list(predicate.find_all(exp.Column))
        literals = [literal for literal in predicate.find_all(exp.Literal) if literal.is_string]
        has_city = any(column.name.casefold() == "city" for column in columns)
        has_expected_value = any(_normalize(str(literal.this)) == expected for literal in literals)
        return has_city and has_expected_value

    for comparison in where_clause.find_all(exp.EQ):
        if predicate_matches(comparison):
            return True
    for membership in where_clause.find_all(exp.In):
        if predicate_matches(membership):
            return True
    return False


def validate_structured_read_constraints(*, question: str, sql: str) -> QueryConstraintCheck:
    """Reject a safe-but-semantically-incomplete SELECT before it can reach PostgreSQL."""

    expected_city = _extract_explicit_city(question)
    if expected_city is None:
        return QueryConstraintCheck(is_valid=True)

    try:
        expression = sqlglot.parse_one(sql, read="postgres")
    except sqlglot.errors.ParseError:
        # The normal AST validator returns the canonical parse error. Do not duplicate it.
        return QueryConstraintCheck(is_valid=True, expected_city=expected_city)

    where_clause = expression.args.get("where")
    if where_clause is None or not _has_matching_city_predicate(where_clause, expected_city):
        return QueryConstraintCheck(
            is_valid=False,
            expected_city=expected_city,
            error_message=(
                f"The user explicitly requested records in '{expected_city}', but the SQL proposal did not preserve "
                f"that constraint as a city filter. Generate a SELECT with WHERE city = '{expected_city}' (or an "
                f"equivalent approved city predicate)."
            ),
        )

    return QueryConstraintCheck(is_valid=True, expected_city=expected_city)
