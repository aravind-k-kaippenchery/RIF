"""Phase 9 deterministic RAG contracts; heavy Chroma and embedding calls are mocked."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app.core.constants import ResponseStatus
from app.main import create_app
from app.rag.document_rag_service import DocumentRagService, RagAnswerResult, RetrievalMatch

client = TestClient(create_app())


def _document(filename: str = "brochure.pdf"):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=uuid4(),
        original_filename=filename,
        document_version=1,
        ingestion_status="completed",
        extracted_text_path="extracted_text/demo.txt",
        created_at=now,
        updated_at=now,
    )


def _match(filename: str = "brochure.pdf") -> RetrievalMatch:
    return RetrievalMatch(
        chunk_id="chunk-1",
        document_id=uuid4(),
        filename=filename,
        page_number=2,
        chunk_index=3,
        text="TextileBot X supports textile automation and includes a 24 month warranty.",
        similarity=0.88,
    )


def test_phase_nine_status_exposes_document_rag_route():
    with patch("app.api.routes.health.get_database_health") as db_health, patch("app.api.routes.health.llm_service.check_llm_health") as llm_health, patch("app.rag.document_rag_service.document_rag_service.status", return_value={"chromadb_available": True, "indexed_chunk_count": 2}):
        db_health.return_value = SimpleNamespace(connected=True, message="ok")
        llm_health.return_value = SimpleNamespace(connected=True, model_installed=True)
        response = client.get("/api/status")
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["phase"] >= 9
    assert body["data"]["chromadb_connected"] is True
    assert "document_rag" in body["data"]["available_routes"]


def test_page_markers_are_preserved_as_page_aware_sections():
    sections = DocumentRagService._page_sections("[PAGE 1]\nAlpha\n\n[PAGE 2]\nBeta")
    assert sections == [(1, "Alpha"), (2, "Beta")]


def test_text_without_page_marker_uses_page_one():
    assert DocumentRagService._page_sections("Plain TXT content") == [(1, "Plain TXT content")]


def test_chunking_keeps_filename_page_and_chunk_provenance():
    service = DocumentRagService()
    service.settings.rag_chunk_size_characters = 24
    service.settings.rag_chunk_overlap_characters = 4
    document = _document("catalog.pdf")
    chunks = service.build_chunks(document, "[PAGE 3]\nTextileBot X supports textile automation with reliable machine controls.")
    assert chunks
    assert all(chunk.filename == "catalog.pdf" for chunk in chunks)
    assert all(chunk.page_number == 3 for chunk in chunks)
    assert chunks[0].chunk_index == 1
    assert "#page=3#chunk=1" in chunks[0].metadata()["source_reference"]


def test_retrieval_filters_low_similarity_results():
    service = DocumentRagService()
    service.settings.rag_min_similarity = 0.5
    collection = MagicMock()
    collection.count.return_value = 2
    collection.query.return_value = {
        "ids": [["good", "weak"]],
        "documents": [["Good chunk", "Weak chunk"]],
        "metadatas": [[
            {"document_id": str(uuid4()), "filename": "good.pdf", "page_number": 1, "chunk_index": 1},
            {"document_id": str(uuid4()), "filename": "weak.pdf", "page_number": 1, "chunk_index": 2},
        ]],
        "distances": [[0.1, 0.8]],
    }
    service._collection = collection
    service._embed = MagicMock(return_value=[[0.0, 1.0]])
    matches = service.retrieve("automation")
    assert len(matches) == 1
    assert matches[0].filename == "good.pdf"
    assert matches[0].similarity == 0.9


def test_rag_no_match_returns_controlled_unavailable_without_llm():
    service = DocumentRagService()
    with patch.object(service, "retrieve", return_value=[]), patch("app.rag.document_rag_service.llm_service.generate_grounded_answer") as generate:
        result = service.answer_question("What is the warranty?")
    assert result.status == ResponseStatus.INFORMATION_NOT_AVAILABLE
    assert result.answer == "Information not available in the uploaded documents."
    generate.assert_not_called()


def test_rag_answer_uses_only_retrieved_source_reference():
    service = DocumentRagService()
    match = _match()
    grounded = SimpleNamespace(answer="TextileBot X has a 24 month warranty.", supported=True, source_references=[match.reference])
    metadata = SimpleNamespace(model_dump=lambda: {"model": "llama3:8b", "attempts": 1})
    with patch.object(service, "retrieve", return_value=[match]), patch("app.rag.document_rag_service.llm_service.generate_grounded_answer", return_value=(grounded, metadata)):
        result = service.answer_question("What is the warranty?")
    assert result.status == ResponseStatus.SUCCESS
    assert result.source_references == [match.reference]


def test_retrieve_endpoint_returns_filename_page_chunk_sources():
    match = _match("neolotex.pdf")
    with patch("app.api.routes.rag.document_rag_service.retrieve", return_value=[match]):
        response = client.post("/api/rag/retrieve", json={"question": "textile automation"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["sources"][0]["reference"] == "neolotex.pdf#page=2#chunk=3"
    assert body["data"]["llm_called"] is False


def test_rag_query_endpoint_returns_grounded_sources_only():
    match = _match("neolotex.pdf")
    result = RagAnswerResult(
        question="What warranty is offered?",
        matches=[match],
        status=ResponseStatus.SUCCESS,
        answer="The brochure states a 24 month warranty.",
        source_references=[match.reference],
        model_metadata=None,
    )
    with patch("app.api.routes.rag.document_rag_service.answer_question", return_value=result):
        response = client.post("/api/rag/query", json={"question": "What warranty is offered?"})
    assert response.status_code == 200
    body = response.json()
    assert body["route"] == "document_rag"
    assert body["sources"][0]["reference"] == match.reference
    assert body["data"]["grounding_policy"] == "answer_from_retrieved_document_evidence_only"


def test_mcp_tool_catalog_includes_document_retrieval_tools():
    response = client.get("/api/mcp/tools")
    assert response.status_code == 200
    names = {tool["name"] for tool in response.json()["data"]["tools"]}
    assert {"retrieve_docs", "get_document_sources"}.issubset(names)


def test_bulk_index_action_log_id_is_unique_per_document_and_preserves_parent_request():
    service = DocumentRagService()
    request_id = "011c5579-f925-455e-b3bf-e162009c85c5"
    first = _document("first.pdf")
    second = _document("second.pdf")

    first_log_id = service._action_log_request_id(
        request_id=request_id, document=first, action_type="document_vector_index"
    )
    second_log_id = service._action_log_request_id(
        request_id=request_id, document=second, action_type="document_vector_index"
    )

    assert first_log_id != second_log_id
    assert first_log_id.startswith(request_id)
    assert second_log_id.startswith(request_id)
    assert len(first_log_id) <= 128
