from unittest.mock import patch

from fastapi.testclient import TestClient

from app.db.health import DatabaseHealth
from app.services.llm_service import OllamaHealth
from app.main import create_app


client = TestClient(create_app())


def test_health_has_consistent_response_envelope():
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["route"] == "system"
    assert body["error"] is None
    assert body["data"]["service"] == "B2B Product Intelligence Assistant API"
    assert response.headers["X-Request-ID"] == body["request_id"]


@patch(
    "app.rag.document_rag_service.document_rag_service.status",
    return_value={"chromadb_available": False, "indexed_chunk_count": 0},
)
@patch("app.api.routes.health.llm_service.check_llm_health")
@patch("app.api.routes.health.get_database_health")
def test_status_reports_current_phase_without_needing_a_real_database(
    mock_database_health,
    mock_ollama_health,
    mock_rag_status,
):
    """Status tests must not depend on a real PostgreSQL, Ollama, or ChromaDB service."""
    mock_database_health.return_value = DatabaseHealth(
        connected=False,
        database=None,
        message="Database test is intentionally mocked.",
    )
    mock_ollama_health.return_value = OllamaHealth(
        connected=False,
        model_installed=False,
        base_url="http://127.0.0.1:11434",
        configured_model="llama3:8b",
        installed_model_names=[],
        message="Ollama test is intentionally mocked.",
    )

    response = client.get("/api/status")

    assert response.status_code == 200
    body = response.json()
    assert body["data"]["phase"] >= 11
    assert body["data"]["database_connected"] is False
    assert body["data"]["ollama_connected"] is False
    assert body["data"]["chromadb_connected"] is False
    assert "document_upload_ocr" in body["data"]["available_routes"]
    assert "agent_orchestration" in body["data"]["available_routes"]
    assert "hybrid_evidence_fusion" in body["data"]["available_routes"]
    assert "employee_permissions" in body["data"]["parent_child_relationship"]


@patch("app.api.routes.contracts.get_database_health")
def test_child_relationship_contract_reports_implemented_phase_two_design(mock_database_health):
    mock_database_health.return_value = DatabaseHealth(
        connected=True,
        database="b2b_assistant",
        message="Connected.",
    )

    response = client.get("/api/contracts/child-relations")

    assert response.status_code == 200
    body = response.json()
    relation = body["data"]["relationships"][0]
    assert body["data"]["database_connected"] is True
    assert relation["parent_table"] == "employees"
    assert relation["child_table"] == "employee_permissions"
    assert relation["cardinality"] == "one_to_zero_or_many"
    assert relation["planned_implementation_phase"] == 2
    assert "RESTRICT" in relation["deletion_policy"]


def test_admin_endpoint_blocks_default_normal_user():
    response = client.get("/api/admin/phase1-check")

    assert response.status_code == 403
    body = response.json()
    assert body["status"] == "validation_failed"
    assert body["error"]["code"] == "insufficient_role"


def test_admin_endpoint_allows_admin_header():
    response = client.get("/api/admin/phase1-check", headers={"X-User-Role": "admin"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["data"]["role"] == "admin"


def test_invalid_role_returns_safe_error_envelope():
    response = client.get("/api/admin/phase1-check", headers={"X-User-Role": "owner"})

    assert response.status_code == 403
    body = response.json()
    assert body["status"] == "validation_failed"
    assert body["error"]["code"] == "invalid_user_role"
