from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from app.agents.orchestrator import agent_orchestrator
from app.core.constants import AgentRoute, ResponseStatus, UserRole
from app.services.soft_routing_service import (
    infer_readonly_business_table,
    should_route_open_question_to_documents,
)


def test_open_policy_question_routes_to_document_rag_without_generic_clarification():
    with patch("app.agents.orchestrator.document_rag_service.answer_question") as answer_question:
        answer_question.return_value = SimpleNamespace(
            status=ResponseStatus.SUCCESS,
            answer="Escalate a ticket for repeated login failure.",
            matches=[],
            source_references=[],
            question="When should a ticket be escalated?",
            model_metadata=None,
        )

        result = agent_orchestrator.run(
            question="When should a ticket be escalated?",
            db=None,
            request_id=None,
            session_id=None,
            user_role=UserRole.NORMAL_USER,
            top_k=None,
            memory_context=None,
        )

    assert result.route == AgentRoute.DOCUMENT_RAG
    assert result.status == ResponseStatus.SUCCESS
    assert result.answer == "Escalate a ticket for repeated login failure."


def test_people_department_question_infers_employees_table():
    assert infer_readonly_business_table("who works under finance?") == "employees"

    fake_result = SimpleNamespace(
        status=ResponseStatus.SUCCESS,
        answer="Found employees in Finance.",
        row_count=2,
        rows=[],
        source={"tables": ["employees"]},
        validation=SimpleNamespace(normalized_sql="select * from employees where department = 'Finance'"),
        proposal=SimpleNamespace(sql="select * from employees where department = 'Finance'", explanation="department filter"),
        question="who works under finance?",
    )
    with patch("app.agents.orchestrator.structured_read_service.execute", return_value=fake_result) as execute:
        result = agent_orchestrator.run(
            question="who works under finance?",
            db=None,
            request_id=None,
            session_id=None,
            user_role=UserRole.NORMAL_USER,
            top_k=None,
            memory_context=None,
        )

    assert result.route == AgentRoute.STRUCTURED_READ
    execute.assert_called_once()
    assert execute.call_args.kwargs["expected_tables"] == ["employees"]


def test_writes_are_not_soft_routed_to_documents():
    assert should_route_open_question_to_documents("delete the ticket document") is False
    assert should_route_open_question_to_documents("update employee EMP-101 to Finance") is False
