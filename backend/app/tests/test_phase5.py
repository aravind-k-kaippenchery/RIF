"""Phase 5 tests. All model HTTP calls are mocked; no local Ollama install is required."""

from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.routes.llm import llm_service as routed_llm_service
from app.main import create_app
from app.schemas.phase5 import IntentClassificationResult
from app.services.llm_service import (
    LLMOutputValidationError,
    LLMService,
    OllamaHealth,
    OllamaUnavailableError,
)
from app.services.sql_validation import validate_dml_sql

client = TestClient(create_app())

READY = OllamaHealth(
    connected=True,
    model_installed=True,
    base_url="http://127.0.0.1:11434",
    configured_model="llama3:8b",
    installed_model_names=["llama3:8b"],
    message="Ready.",
)


def _chat_body(content: str, *, model: str = "llama3:8b") -> dict:
    return {
        "model": model,
        "message": {"role": "assistant", "content": content},
        "total_duration": 123,
        "prompt_eval_count": 11,
        "eval_count": 7,
    }


def test_model_match_accepts_default_latest_alias():
    assert LLMService._model_matches("llama3", "llama3:latest") is True
    assert LLMService._model_matches("llama3:8b", "llama3:8b") is True
    assert LLMService._model_matches("llama3:8b", "llama3:70b") is False


def test_generate_json_sends_pydantic_schema_and_validates_response():
    service = LLMService()
    captured: list[dict] = []

    def fake_send(payload: dict) -> dict:
        captured.append(payload)
        return _chat_body('{"route":"structured_read","requires_clarification":false,"clarification_question":null}')

    with patch.object(service, "check_llm_health", return_value=READY), patch.object(service, "_send_chat_request", side_effect=fake_send):
        result, metadata = service.generate_json(
            output_model=IntentClassificationResult,
            system_prompt="Classify safely.",
            user_prompt="Show employees from Bangalore",
        )

    assert result.route == "structured_read"
    assert metadata.attempts == 1
    assert captured[0]["stream"] is False
    assert captured[0]["format"]["type"] == "object"
    assert "title" not in captured[0]["format"]
    assert "properties" in captured[0]["format"]
    assert captured[0]["options"]["temperature"] == 0.0


def test_generate_json_falls_back_to_plain_json_when_schema_format_gets_http_400():
    service = LLMService()
    request = httpx.Request("POST", "http://127.0.0.1:11434/api/chat")
    rejected = httpx.Response(400, request=request, text="unsupported schema keyword")
    calls: list[dict] = []

    def fake_send(payload: dict) -> dict:
        calls.append(payload)
        if len(calls) == 1:
            raise httpx.HTTPStatusError("bad request", request=request, response=rejected)
        return _chat_body('{"route":"structured_read","requires_clarification":false,"clarification_question":null}')

    with patch.object(service, "check_llm_health", return_value=READY), patch.object(service, "_send_chat_request", side_effect=fake_send):
        result, metadata = service.generate_json(
            output_model=IntentClassificationResult,
            system_prompt="Classify safely.",
            user_prompt="Show employees from Chennai",
        )

    assert result.route == "structured_read"
    assert metadata.attempts == 1
    assert len(calls) == 2
    assert isinstance(calls[0]["format"], dict)
    assert calls[1]["format"] == "json"


def test_generate_json_retries_once_when_first_output_breaks_schema():
    service = LLMService()
    responses = [
        _chat_body('{"route":"not_a_route"}'),
        _chat_body('{"route":"clarification","requires_clarification":true,"clarification_question":"Which table should I use?"}'),
    ]

    with patch.object(service, "check_llm_health", return_value=READY), patch.object(service, "_send_chat_request", side_effect=responses) as mock_send:
        result, metadata = service.generate_json(
            output_model=IntentClassificationResult,
            system_prompt="Classify safely.",
            user_prompt="Do something with it",
        )

    assert result.route == "clarification"
    assert metadata.attempts == 2
    assert mock_send.call_count == 2


def test_generate_json_stops_after_maximum_two_total_attempts():
    service = LLMService()
    responses = [_chat_body("not-json"), _chat_body("still-not-json")]

    with patch.object(service, "check_llm_health", return_value=READY), patch.object(service, "_send_chat_request", side_effect=responses) as mock_send:
        with pytest.raises(LLMOutputValidationError) as exc_info:
            service.generate_json(
                output_model=IntentClassificationResult,
                system_prompt="Classify safely.",
                user_prompt="hello",
            )

    assert exc_info.value.code == "llm_output_validation_failed"
    assert mock_send.call_count == 2


def test_generate_sql_retries_once_using_phase_four_validator_feedback():
    service = LLMService()
    responses = [
        _chat_body('{"route":"structured_read","sql":"DROP TABLE employees","explanation":"Bad proposal."}'),
        _chat_body('{"route":"structured_read","sql":"SELECT employee_code, first_name FROM employees WHERE city = \'Bangalore\'","explanation":"Read employees in Bangalore."}'),
    ]

    with patch.object(service, "check_llm_health", return_value=READY), patch.object(service, "_send_chat_request", side_effect=responses) as mock_send:
        proposal, validation, metadata, glossary = service.generate_sql(
            "Show workers from Bangalore",
            "structured_read",
        )

    assert proposal.route == "structured_read"
    assert validation.is_valid is True
    assert validation.statement_type == "SELECT"
    assert metadata.attempts == 2
    assert "employees" in glossary["schema_hints"]
    assert mock_send.call_count == 2


def test_record_extraction_retries_when_model_uses_managed_column():
    service = LLMService()
    responses = [
        _chat_body('{"table_name":"employees","values":{"id":99,"first_name":"Maya"},"missing_required_fields":[],"requires_clarification":false,"clarification_question":null}'),
        _chat_body('{"table_name":"employees","values":{"first_name":"Maya","city":"Bangalore","company_name":"Neolotex"},"missing_required_fields":["last_name","email","department","salary","employment_status"],"requires_clarification":true,"clarification_question":"Please provide the missing employee details."}'),
    ]

    with patch.object(service, "check_llm_health", return_value=READY), patch.object(service, "_send_chat_request", side_effect=responses) as mock_send:
        result, metadata = service.extract_record_fields(
            "Add Maya from Bangalore working for Neolotex",
            "employees",
        )

    assert result.table_name == "employees"
    assert "id" not in result.values
    assert result.requires_clarification is True
    assert metadata.attempts == 2
    assert mock_send.call_count == 2


def test_grounded_answer_retries_when_unknown_source_is_cited_then_returns_allowed_source():
    service = LLMService()
    evidence = [{"source_type": "database", "reference": "employees", "content": "Maya works in Bangalore."}]
    responses = [
        _chat_body('{"answer":"Maya works in Bangalore.","supported":true,"source_references":["invented_source"]}'),
        _chat_body('{"answer":"Maya works in Bangalore.","supported":true,"source_references":["employees"]}'),
    ]

    with patch.object(service, "check_llm_health", return_value=READY), patch.object(service, "_send_chat_request", side_effect=responses) as mock_send:
        result, metadata = service.generate_grounded_answer("Where does Maya work?", evidence)

    assert result.supported is True
    assert result.source_references == ["employees"]
    assert metadata is not None and metadata.attempts == 2
    assert mock_send.call_count == 2


def test_grounded_answer_without_evidence_does_not_call_ollama():
    service = LLMService()

    result, metadata = service.generate_grounded_answer("What is the warranty?", [])

    assert result.supported is False
    assert result.answer == "Information not available in the provided evidence."
    assert metadata is None


def test_llm_status_endpoint_reports_safe_unavailable_state_without_inference():
    unavailable = OllamaHealth(
        connected=False,
        model_installed=False,
        base_url="http://127.0.0.1:11434",
        configured_model="llama3:8b",
        installed_model_names=[],
        message="Not running.",
    )
    with patch.object(routed_llm_service, "check_llm_health", return_value=unavailable):
        response = client.get("/api/llm/status")

    assert response.status_code == 200
    body = response.json()
    assert body["data"]["connected"] is False
    assert body["data"]["model_can_execute_sql"] is False
    assert body["data"]["max_model_attempts"] == 2


def test_llm_endpoint_returns_controlled_503_when_local_model_unavailable():
    with patch.object(
        routed_llm_service,
        "classify_intent",
        side_effect=OllamaUnavailableError(code="ollama_unavailable", message="Start local Ollama."),
    ):
        response = client.post("/api/llm/classify-intent", json={"question": "Show vendors from Chennai"})

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "llm_unavailable"
    assert body["error"]["code"] == "ollama_unavailable"


def test_generate_sql_uses_compact_business_only_schema_context():
    service = LLMService()
    captured: list[dict] = []

    def fake_send(payload: dict) -> dict:
        captured.append(payload)
        return _chat_body(
            '{"route":"structured_read","sql":"SELECT employee_code, first_name FROM employees WHERE city = \'Bangalore\'","explanation":"Read employees in Bangalore."}'
        )

    with patch.object(service, "check_llm_health", return_value=READY), patch.object(service, "_send_chat_request", side_effect=fake_send):
        proposal, validation, _, _ = service.generate_sql("Show workers from Bangalore", "structured_read")

    prompt_text = "\n".join(message["content"] for message in captured[0]["messages"])
    assert proposal.route == "structured_read"
    assert validation.is_valid is True
    assert "controlled_compact_business_schema" in prompt_text
    assert "schema_change_requests" not in prompt_text
    assert len(prompt_text) < 10000


def test_generate_sql_retries_when_model_drops_explicit_city_filter():
    """A safe broad SELECT must not run when the user explicitly asked for Mars."""

    service = LLMService()
    responses = [
        _chat_body(
            '{"route":"structured_read","sql":"SELECT employee_code, first_name, city FROM employees WHERE employment_status = \'active\'","explanation":"Read active employees."}'
        ),
        _chat_body(
            '{"route":"structured_read","sql":"SELECT employee_code, first_name, city FROM employees WHERE city = \'Mars\'","explanation":"Read employees in Mars."}'
        ),
    ]

    with patch.object(service, "check_llm_health", return_value=READY), patch.object(service, "_send_chat_request", side_effect=responses) as mock_send:
        proposal, validation, metadata, _ = service.generate_sql("Show employees from Mars", "structured_read")

    assert metadata.attempts == 2
    assert "city = 'Mars'" in (validation.normalized_sql or proposal.sql)
    assert mock_send.call_count == 2
    correction_prompt = "\n".join(message["content"] for message in mock_send.call_args_list[1].args[0]["messages"])
    assert "Mars" in correction_prompt
    assert "city filter" in correction_prompt
