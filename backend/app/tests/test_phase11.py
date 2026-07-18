"""Phase 11 deterministic tests for verified SQL-plus-document evidence fusion."""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

from fastapi.testclient import TestClient

from app.agents.orchestrator import agent_orchestrator
from app.core.constants import ResponseStatus
from app.main import create_app
from app.mcp.client import LocalMCPClient
from app.mcp.tools import _document_explicitly_names_product
from app.rag.document_rag_service import RetrievalMatch
from app.services.hybrid_evidence_service import HybridEvidenceService

client = TestClient(create_app())


def _match(text: str = "TextileBot X supports textile automation.") -> RetrievalMatch:
    return RetrievalMatch(
        chunk_id="doc-1",
        document_id=uuid4(),
        filename="textilebot_brochure.pdf",
        page_number=2,
        chunk_index=1,
        text=text,
        similarity=0.91,
    )


def test_price_parser_under_lakh_uses_rupee_value():
    assert HybridEvidenceService.parse_max_price("Which product is under 5 lakh?") == 500000.0
    assert HybridEvidenceService.parse_max_price("Show products below ₹4,75,000") == 475000.0


def test_price_parser_does_not_guess_when_filter_is_missing():
    assert HybridEvidenceService.parse_max_price("Which vendor offers TextileBot X?") is None


def test_document_product_identity_requires_explicit_name_or_code():
    assert _document_explicitly_names_product(
        "The TextileBot X brochure describes textile automation.", "TextileBot X", "PRD-001"
    )
    assert _document_explicitly_names_product(
        "Model PRD-001 includes automatic weaving controls.", "TextileBot X", "PRD-001"
    )
    assert not _document_explicitly_names_product(
        "A general automation solution is described.", "TextileBot X", "PRD-001"
    )


def test_hybrid_service_returns_unavailable_without_document_evidence():
    mcp = MagicMock()
    service = HybridEvidenceService(mcp_client=mcp)
    result = service.fuse(question="Which vendor offers textile automation products under 5 lakh?", document_matches=[])
    assert result.status == ResponseStatus.INFORMATION_NOT_AVAILABLE
    assert result.matches == []
    mcp.call_tool.assert_not_called()


def test_hybrid_service_composes_answer_from_verified_mcp_evidence():
    mcp = MagicMock()
    mcp.call_tool.return_value = {
        "ok": True,
        "result": {
            "retrieved": True,
            "matches": [
                {
                    "vendor_code": "VND-001",
                    "vendor_name": "Neolotex Systems",
                    "product_code": "PRD-001",
                    "product_name": "TextileBot X",
                    "quoted_price": 475000.0,
                    "document_source_references": ["textilebot_brochure.pdf#page=2#chunk=1"],
                }
            ],
        },
    }
    service = HybridEvidenceService(mcp_client=mcp)
    result = service.fuse(
        question="Which vendor offers textile automation products under 5 lakh according to the brochure?",
        document_matches=[_match()],
    )
    assert result.status == ResponseStatus.SUCCESS
    assert "Neolotex Systems offers TextileBot X for ₹475,000" in result.answer
    assert {citation.source_type for citation in result.source_citations} == {"database", "document"}
    call = mcp.call_tool.call_args
    assert call.args[0] == "get_verified_hybrid_evidence"
    assert call.args[1]["max_price"] == 500000.0


def test_hybrid_service_returns_unavailable_when_mcp_finds_no_verified_mapping():
    mcp = MagicMock()
    mcp.call_tool.return_value = {
        "ok": True,
        "result": {
            "retrieved": True,
            "matches": [],
            "no_data_reason": "No active vendor/product mapping satisfied the price filter.",
        },
    }
    service = HybridEvidenceService(mcp_client=mcp)
    result = service.fuse(
        question="Which vendor offers textile automation products under 5 lakh according to the brochure?",
        document_matches=[_match()],
    )
    assert result.status == ResponseStatus.INFORMATION_NOT_AVAILABLE
    assert "verified combination" in result.answer.lower()


def test_phase_eleven_status_and_mcp_catalog_expose_verified_hybrid_capability():
    status = agent_orchestrator.status()
    assert status["phase"] >= 11
    assert status["hybrid_final_answer_available"] is True
    assert "get_verified_hybrid_evidence" in {tool["name"] for tool in LocalMCPClient().list_tools()}

    response = client.get("/api/hybrid/status")
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["phase"] == 11
    assert body["data"]["database_access_boundary"] == "restricted_mcp_tool"
