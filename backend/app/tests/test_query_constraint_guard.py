"""Deterministic tests for explicit location-filter preservation."""

from app.services.query_constraint_guard import validate_structured_read_constraints


def test_rejects_safe_but_broad_select_when_city_filter_is_dropped():
    result = validate_structured_read_constraints(
        question="Show employees from Mars",
        sql="SELECT employee_code, first_name FROM employees WHERE employment_status = 'active'",
    )

    assert result.is_valid is False
    assert result.expected_city == "Mars"
    assert "city filter" in result.error_message


def test_accepts_matching_city_filter_for_requested_location():
    result = validate_structured_read_constraints(
        question="Show users who live in Bangalore",
        sql="SELECT employee_code, first_name FROM employees WHERE city = 'Bangalore'",
    )

    assert result.is_valid is True
    assert result.expected_city == "Bangalore"


def test_does_not_invent_a_city_constraint_for_non_location_question():
    result = validate_structured_read_constraints(
        question="Show active employees",
        sql="SELECT employee_code, first_name FROM employees WHERE employment_status = 'active'",
    )

    assert result.is_valid is True
    assert result.expected_city is None
