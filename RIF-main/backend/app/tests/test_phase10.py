"""Phase 10 deterministic router and LangGraph contract tests.

These tests do not require PostgreSQL, Ollama, ChromaDB, or a network connection. Route
execution integrations continue to be validated by their Phase 6, 7, and 9 suites.
"""

from fastapi.testclient import TestClient

from app.agents.orchestrator import agent_orchestrator
from app.agents.router import classify_question
from app.core.constants import AgentRoute
from app.main import create_app

client = TestClient(create_app())


def test_router_selects_structured_read_for_employee_filter():
    decision = classify_question("Show workers from Bangalore")
    assert decision.route == AgentRoute.STRUCTURED_READ
    assert decision.confidence >= 0.5


def test_router_selects_crud_write_before_any_database_execution():
    decision = classify_question("Add employee Maya from Bangalore working for Neolotex")
    assert decision.route == AgentRoute.CRUD_WRITE
    assert "confirmation" in decision.reason.lower()


def test_router_selects_document_rag_for_warranty_question():
    decision = classify_question("What warranty is mentioned in the product brochure?")
    assert decision.route == AgentRoute.DOCUMENT_RAG


def test_router_selects_hybrid_for_document_capability_plus_price_filter():
    decision = classify_question("Which vendor offers textile automation products under 5 lakh according to the brochure?")
    assert decision.route == AgentRoute.HYBRID


def test_graph_status_is_compiled_and_keeps_confirmation_gate():
    status = agent_orchestrator.status()
    assert status["phase"] >= 10
    assert status["graph_engine"] == "langgraph"
    assert status["graph_compiled"] is True
    assert status["write_execution_allowed_without_confirmation"] is False
    assert status["hybrid_final_answer_available"] is True


def test_agent_status_endpoint_exposes_four_routes():
    response = client.get("/api/agent/status")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["data"]["phase"] >= 10
    assert set(body["data"]["supported_routes"]) == {
        "structured_read",
        "crud_write",
        "document_rag",
        "hybrid",
    }
