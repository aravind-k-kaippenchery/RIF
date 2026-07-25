"""Tests for the optional local fallback model added to the Phase 5 Ollama adapter.

The fallback model is a second model pulled on the same loopback Ollama server. It is
tried only when the primary model is unavailable (unreachable, not installed, timed out,
or returned an HTTP error) -- never for output-validation failures, and never against a
non-local endpoint.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from app.core.config import clear_settings_cache, get_settings
from app.schemas.phase5 import IntentClassificationResult
from app.services.llm_service import (
    LLMOutputValidationError,
    LLMService,
    OllamaHealth,
    OllamaUnavailableError,
)


def _health(*, connected: bool, model_installed: bool, model: str) -> OllamaHealth:
    return OllamaHealth(
        connected=connected,
        model_installed=model_installed,
        base_url="http://127.0.0.1:11434",
        configured_model=model,
        installed_model_names=[model] if connected and model_installed else [],
        message="ok" if connected and model_installed else "not ready",
    )


def _chat_body(content: str, *, model: str) -> dict:
    return {
        "model": model,
        "message": {"role": "assistant", "content": content},
        "total_duration": 1,
        "prompt_eval_count": 1,
        "eval_count": 1,
    }


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    clear_settings_cache()
    yield
    clear_settings_cache()


def test_fallback_model_is_used_when_primary_model_is_not_installed(monkeypatch):
    monkeypatch.setenv("OLLAMA_FALLBACK_MODEL", "llama3.2:3b")
    monkeypatch.setenv("OLLAMA_FALLBACK_ENABLED", "true")
    clear_settings_cache()
    settings = get_settings()
    service = LLMService()

    def fake_health(model_name: str | None = None) -> OllamaHealth:
        target = model_name or settings.ollama_model
        if target == settings.ollama_model:
            return _health(connected=True, model_installed=False, model=target)
        return _health(connected=True, model_installed=True, model=target)

    fallback_response = _chat_body(
        '{"route":"structured_read","requires_clarification":false,"clarification_question":null}',
        model="llama3.2:3b",
    )

    with (
        patch.object(service, "check_llm_health", side_effect=fake_health),
        patch.object(service, "_send_chat_request", return_value=fallback_response) as mock_send,
    ):
        result, metadata = service.generate_json(
            output_model=IntentClassificationResult,
            system_prompt="Classify safely.",
            user_prompt="Show employees from Bangalore",
        )

    assert result.route == "structured_read"
    assert metadata.model == "llama3.2:3b"
    # Only the fallback model attempt should have sent a chat request.
    assert mock_send.call_count == 1
    assert mock_send.call_args.args[0]["model"] == "llama3.2:3b"


def test_no_fallback_attempted_when_fallback_disabled(monkeypatch):
    monkeypatch.setenv("OLLAMA_FALLBACK_ENABLED", "false")
    clear_settings_cache()
    settings = get_settings()
    service = LLMService()

    def fake_health(model_name: str | None = None) -> OllamaHealth:
        return _health(connected=False, model_installed=False, model=model_name or settings.ollama_model)

    with patch.object(service, "check_llm_health", side_effect=fake_health):
        with pytest.raises(OllamaUnavailableError) as exc_info:
            service.generate_json(
                output_model=IntentClassificationResult,
                system_prompt="Classify safely.",
                user_prompt="Show employees from Bangalore",
            )

    assert exc_info.value.code == "ollama_unavailable"


def test_fallback_not_attempted_for_output_validation_failure(monkeypatch):
    monkeypatch.setenv("OLLAMA_FALLBACK_MODEL", "llama3.2:3b")
    monkeypatch.setenv("OLLAMA_FALLBACK_ENABLED", "true")
    clear_settings_cache()
    settings = get_settings()
    service = LLMService()

    def fake_health(model_name: str | None = None) -> OllamaHealth:
        return _health(connected=True, model_installed=True, model=model_name or settings.ollama_model)

    # Both allowed attempts against the primary model return invalid JSON; this is a
    # validation failure, not an availability failure, so no fallback call should occur.
    responses = [_chat_body("not-json", model=settings.ollama_model), _chat_body("still-not-json", model=settings.ollama_model)]

    with (
        patch.object(service, "check_llm_health", side_effect=fake_health),
        patch.object(service, "_send_chat_request", side_effect=responses) as mock_send,
    ):
        with pytest.raises(LLMOutputValidationError):
            service.generate_json(
                output_model=IntentClassificationResult,
                system_prompt="Classify safely.",
                user_prompt="hello",
            )

    assert mock_send.call_count == 2
    for call in mock_send.call_args_list:
        assert call.args[0]["model"] == settings.ollama_model


def test_error_reported_when_both_primary_and_fallback_are_unavailable(monkeypatch):
    monkeypatch.setenv("OLLAMA_FALLBACK_MODEL", "llama3.2:3b")
    monkeypatch.setenv("OLLAMA_FALLBACK_ENABLED", "true")
    clear_settings_cache()
    service = LLMService()

    def fake_health(model_name: str | None = None) -> OllamaHealth:
        return _health(connected=False, model_installed=False, model=model_name or "unknown")

    with patch.object(service, "check_llm_health", side_effect=fake_health):
        with pytest.raises(OllamaUnavailableError) as exc_info:
            service.generate_json(
                output_model=IntentClassificationResult,
                system_prompt="Classify safely.",
                user_prompt="Show employees from Bangalore",
            )

    assert exc_info.value.code == "ollama_all_local_models_unavailable"
    assert "primary local model" in exc_info.value.message
    assert "local fallback model" in exc_info.value.message


def test_fallback_health_reported_via_fallback_model_health(monkeypatch):
    monkeypatch.setenv("OLLAMA_FALLBACK_MODEL", "llama3.2:3b")
    monkeypatch.setenv("OLLAMA_FALLBACK_ENABLED", "true")
    clear_settings_cache()
    service = LLMService()

    with patch.object(
        service,
        "check_llm_health",
        return_value=_health(connected=True, model_installed=True, model="llama3.2:3b"),
    ) as mock_health:
        health = service.fallback_model_health()

    assert health is not None
    assert health.configured_model == "llama3.2:3b"
    mock_health.assert_called_once_with(model_name="llama3.2:3b")


def test_fallback_model_health_is_none_when_disabled(monkeypatch):
    monkeypatch.setenv("OLLAMA_FALLBACK_ENABLED", "false")
    clear_settings_cache()
    service = LLMService()

    assert service.fallback_model_health() is None


def test_fallback_never_targets_a_non_local_endpoint(monkeypatch):
    """The local-only guard applies to the fallback model exactly like the primary model."""

    monkeypatch.setenv("OLLAMA_BASE_URL", "https://example.com")
    monkeypatch.setenv("OLLAMA_FALLBACK_MODEL", "llama3.2:3b")
    monkeypatch.setenv("OLLAMA_FALLBACK_ENABLED", "true")
    clear_settings_cache()
    service = LLMService()

    with pytest.raises(OllamaUnavailableError) as exc_info:
        service._ensure_model_ready("llama3.2:3b")

    assert exc_info.value.code == "ollama_unavailable"
    assert "loopback Ollama endpoint" in exc_info.value.message
