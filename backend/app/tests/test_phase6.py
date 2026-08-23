"""Phase 6 tests. LLM, MCP, and query-history writes are mocked; no live services are required."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.api.routes.structured_read import structured_read_service as routed_structured_read_service
from app.main import create_app
from app.schemas.phase5 import OllamaInvocationMetadata, SQLGenerationResult
from app.services.llm_service import OllamaUnavailableError
from app.services.sql_validation import SQLValidationResult
from app.services.structured_read_service import StructuredReadError, StructuredReadService
from app.core.constants import ResponseStatus

client = TestClient(create_app())


class FakeMCPClient:
    def __init__(self, outcome: dict):
        self.outcome = outcome
        self.calls: list[tuple[str, dict]] = []

    def call_tool(self, name: str, arguments: dict) -> dict:
        self.calls.append((name, arguments))
        return self.outcome


def _proposal() -> SQLGenerationResult:
    return SQLGenerationResult(
        route="structured_read",
        sql="SELECT employee_code, first_name, city FROM employees WHERE city = 'Bangalore'",
        explanation="Read employees in Bangalore.",
    )


def _validation() -> SQLValidationResult:
    return SQLValidationResult(
        is_valid=True,
        statement_type="SELECT",
        normalized_sql="SELECT employee_code, first_name, city FROM employees WHERE city = 'Bangalore' LIMIT 100",
        tables=["employees"],
        columns=["employees.employee_code", "employees.first_name", "employees.city"],
        warnings=["A server-controlled LIMIT of 100 row(s) is enforced."],
        applied_limit=100,
    )


def _metadata() -> OllamaInvocationMetadata:
    return OllamaInvocationMetadata(model="llama3:8b", attempts=1, total_duration_ns=100, prompt_eval_count=10, eval_count=5)


def _mcp_success(rows: list[dict]) -> dict:
    validation = _validation().to_dict()
    return {
        "ok": True,
        "result": {
            "executed": True,
            "validation": validation,
            "rows": rows,
            "row_count": len(rows),
            "source": {"source_type": "database", "tables": ["employees"]},
        },
    }


def test_structured_read_executes_only_via_mcp_and_returns_grounded_rows():
    fake_client = FakeMCPClient(
        _mcp_success(
            [
                {"employee_code": "EMP-102", "first_name": "Anjali", "city": "Bangalore"},
                {"employee_code": "EMP-105", "first_name": "Vishnu", "city": "Bangalore"},
            ]
        )
    )
    service = StructuredReadService(mcp_client=fake_client)

    with patch("app.services.structured_read_service.llm_service.generate_sql", return_value=(_proposal(), _validation(), _metadata(), {"schema_hints": ["employees"]})), patch.object(service, "_write_query_log", return_value={"stored": True, "storage": "query_logs"}):
        result = service.execute(question="Show workers from Bangalore", request_id="req-1")

    assert result.status == ResponseStatus.SUCCESS
    assert result.row_count == 2
    assert result.answer == "Found 2 employees in Bangalore: Anjali and Vishnu."
    assert result.rows[0]["employee_code"] == "EMP-102"
    assert fake_client.calls == [
        (
            "execute_validated_read",
            {"sql": "SELECT employee_code, first_name, city FROM employees WHERE city = 'Bangalore' LIMIT 100"},
        )
    ]


def test_structured_read_returns_controlled_no_data_answer_without_second_llm_call():
    fake_client = FakeMCPClient(_mcp_success([]))
    service = StructuredReadService(mcp_client=fake_client)

    with patch("app.services.structured_read_service.llm_service.generate_sql", return_value=(_proposal(), _validation(), _metadata(), {"schema_hints": ["employees"]})), patch.object(service, "_write_query_log", return_value={"stored": True}):
        result = service.execute(question="Show employees from Mars", request_id="req-2")

    assert result.status == ResponseStatus.INFORMATION_NOT_AVAILABLE
    assert result.answer == "Information not available in the current database."
    assert result.row_count == 0


def test_structured_read_uses_count_value_instead_of_aggregate_result_row_count():
    fake_client = FakeMCPClient(_mcp_success([{"count": 94}]))
    service = StructuredReadService(mcp_client=fake_client)
    count_proposal = SQLGenerationResult(
        route="structured_read",
        sql="SELECT COUNT(*) FROM employees",
        explanation="Count all employees.",
    )
    count_validation = SQLValidationResult(
        is_valid=True,
        statement_type="SELECT",
        normalized_sql="SELECT COUNT(*) FROM employees LIMIT 100",
        tables=["employees"],
        columns=[],
        warnings=[],
        applied_limit=100,
    )

    with patch(
        "app.services.structured_read_service.llm_service.generate_sql",
        return_value=(count_proposal, count_validation, _metadata(), {}),
    ), patch.object(service, "_write_query_log", return_value={"stored": True}):
        result = service.execute(
            question="Count every row in the employees table without applying filters.",
            request_id="req-count",
        )

    assert result.status == ResponseStatus.SUCCESS
    assert result.answer == "Found 94 employees in the current database."
    assert result.rows == [{"count": 94}]


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"COUNT": 94}, "Found 94 employees in the current database."),
        ({"total_count": "94"}, "Found 94 employees in the current database."),
        ({"employee_count": 94.0}, "Found 94 employees in the current database."),
    ],
)
def test_structured_read_normalizes_postgres_count_aliases(row, expected):
    status, answer = StructuredReadService._grounded_answer(
        row_count=1,
        rows=[row],
        source_tables=["employees"],
    )

    assert status == ResponseStatus.SUCCESS
    assert answer == expected


def test_structured_read_rejects_unexecuted_tool_result():
    fake_client = FakeMCPClient(
        {
            "ok": True,
            "result": {
                "executed": False,
                "validation": {
                    "is_valid": False,
                    "error_code": "unknown_table",
                    "error_message": "Unknown table.",
                },
            },
        }
    )
    service = StructuredReadService(mcp_client=fake_client)

    with patch("app.services.structured_read_service.llm_service.generate_sql", return_value=(_proposal(), _validation(), _metadata(), {})):
        with pytest.raises(StructuredReadError) as exc_info:
            service.execute(question="Show workers", request_id="req-3")

    assert exc_info.value.status == ResponseStatus.VALIDATION_FAILED
    assert exc_info.value.code == "unknown_table"


def test_structured_read_endpoint_returns_database_source_and_generated_sql():
    from app.services.structured_read_service import StructuredReadResult

    result = StructuredReadResult(
        question="Show workers from Bangalore",
        proposal=_proposal(),
        validation=_validation(),
        glossary={"schema_hints": ["employees"]},
        model_metadata=_metadata(),
        rows=[{"employee_code": "EMP-102", "first_name": "Anjali", "city": "Bangalore"}],
        row_count=1,
        source={"source_type": "database", "tables": ["employees"]},
        answer="Found 1 matching record in the current database.",
        status=ResponseStatus.SUCCESS,
        audit={"stored": True, "storage": "query_logs"},
    )
    with patch.object(routed_structured_read_service, "execute", return_value=result) as mock_execute:
        response = client.post("/api/structured-read", json={"question": "Show workers from Bangalore"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["route"] == "structured_read"
    assert body["data"]["execution"]["tool"] == "execute_validated_read"
    assert body["data"]["execution"]["write_execution_allowed"] is False
    assert body["generated_sql"].endswith("LIMIT 100")
    assert body["sources"][0]["reference"] == "employees"
    mock_execute.assert_called_once()


def test_structured_read_endpoint_returns_information_not_available_for_zero_rows():
    from app.services.structured_read_service import StructuredReadResult

    result = StructuredReadResult(
        question="Show employees from Mars",
        proposal=_proposal(),
        validation=_validation(),
        glossary={"schema_hints": ["employees"]},
        model_metadata=_metadata(),
        rows=[],
        row_count=0,
        source={"source_type": "database", "tables": ["employees"]},
        answer="Information not available in the current database.",
        status=ResponseStatus.INFORMATION_NOT_AVAILABLE,
        audit={"stored": True},
    )
    with patch.object(routed_structured_read_service, "execute", return_value=result):
        response = client.post("/api/structured-read", json={"question": "Show employees from Mars"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "information_not_available"
    assert body["answer"] == "Information not available in the current database."
    assert body["data"]["row_count"] == 0


def test_structured_read_endpoint_maps_model_unavailable_to_safe_503():
    with patch.object(
        routed_structured_read_service,
        "execute",
        side_effect=OllamaUnavailableError(code="ollama_unavailable", message="Start local Ollama."),
    ):
        response = client.post("/api/structured-read", json={"question": "Show vendors"})

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "llm_unavailable"
    assert body["error"]["code"] == "ollama_unavailable"
