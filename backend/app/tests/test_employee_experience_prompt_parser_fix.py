"""Regression tests for clear employee-experience natural-language prompts."""

from unittest.mock import patch

from app.services.parent_child_crud_service import (
    ParentChildCrudService,
    _parse_clear_employee_experience_request,
    looks_like_parent_child_request,
)


def test_complete_experience_create_prompt_is_parsed_without_ollama():
    question = (
        "Add experience for EMP-105 at Infosys as Python Developer "
        "from 2021-01-01 to 2024-01-01"
    )
    service = ParentChildCrudService()

    with patch(
        "app.services.parent_child_crud_service.llm_service.generate_json",
        side_effect=AssertionError("Ollama must not be called for this clear prompt."),
    ):
        plan, metadata = service._generate_plan(question)

    assert plan.operation == "create"
    assert plan.child_table == "employee_experiences"
    assert plan.parent_references[0].role == "employee"
    assert plan.parent_references[0].code == "EMP-105"
    assert plan.values == {
        "company_name": "Infosys",
        "job_title": "Python Developer",
        "start_date": "2021-01-01",
        "is_current": False,
        "end_date": "2024-01-01",
    }
    assert metadata["ollama_called"] is False


def test_clear_employee_experience_read_update_and_delete_are_deterministic():
    read_plan = _parse_clear_employee_experience_request("Show work history of EMP-105")
    update_plan = _parse_clear_employee_experience_request(
        "Change EMP-105's Infosys role to Senior Python Developer"
    )
    delete_plan = _parse_clear_employee_experience_request(
        "Delete oldest experience of EMP-105"
    )

    assert read_plan is not None and read_plan.operation == "read"
    assert update_plan is not None and update_plan.operation == "update"
    assert update_plan.filters[0].value == "Infosys"
    assert update_plan.values["job_title"] == "Senior Python Developer"
    assert delete_plan is not None and delete_plan.operation == "delete"
    assert delete_plan.selection == "oldest"


def test_incomplete_experience_request_asks_only_for_missing_values():
    plan = _parse_clear_employee_experience_request("Add experience for EMP-105")

    assert plan is not None
    assert plan.operation == "clarify"
    assert "previous company" in plan.clarification_question
    assert "job title" in plan.clarification_question
    assert "start date" in plan.clarification_question


def test_employee_role_update_reaches_parent_child_route():
    assert looks_like_parent_child_request(
        "Change EMP-105's Infosys role to Senior Python Developer"
    ) is True
