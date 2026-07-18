"""Regression test for Phase 9 grounded-RAG error handling."""

from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.constants import ResponseStatus
from app.main import create_app
from app.rag.document_rag_service import DocumentRagError


client = TestClient(create_app())


def test_rag_llm_timeout_returns_controlled_document_route_error():
    timeout_error = DocumentRagError(
        status=ResponseStatus.LLM_UNAVAILABLE,
        code="ollama_request_failed",
        message="Local Ollama could not complete the generation request.",
    )

    with patch(
        "app.api.routes.rag.document_rag_service.answer_question",
        side_effect=timeout_error,
    ):
        response = client.post(
            "/api/rag/query",
            json={"question": "What warranty is mentioned for TextileBot X?", "top_k": 4},
        )

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "llm_unavailable"
    assert body["route"] == "document_rag"
    assert body["error"]["code"] == "ollama_request_failed"
