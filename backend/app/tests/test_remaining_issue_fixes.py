from unittest.mock import patch

import psutil
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.main import create_app
from app.services.benchmark_service import benchmark_service
from app.services.demo_question_service import detect_demo_question
from app.services.dynamic_pgsql_schema import get_runtime_table
from app.services.session_memory_service import memory_context_for_question


def test_explicit_employee_count_is_not_misrouted_as_followup():
    request = detect_demo_question("How many employees are there?")
    assert request.handled is True
    assert request.kind == "employee_count"
    assert request.table == "employees"
    assert request.filters == {}


def test_count_without_an_explicit_entity_remains_a_followup():
    request = detect_demo_question("How many are there?")
    assert request.handled is True
    assert request.kind == "context_followup_count"
    assert request.table == "__memory__"


def test_existential_count_followup_keeps_previous_query_memory():
    context = {
        "available": True,
        "event_count": 1,
        "events": [
            {
                "prior_question": "Show employees from Bangalore.",
                "route": "structured_read",
                "status": "success",
                "generated_sql": "SELECT * FROM employees WHERE lower(city) = 'bangalore' ORDER BY id LIMIT 50",
            }
        ],
        "policy": "bounded",
    }

    selected = memory_context_for_question("How many are there?", context)

    assert selected is context
    assert selected["events"][0]["generated_sql"].startswith("SELECT * FROM employees")


def test_implicit_first_five_followup_keeps_previous_query_memory():
    context = {
        "available": True,
        "event_count": 1,
        "events": [{"prior_question": "Show employees from Bangalore.", "status": "success"}],
        "policy": "bounded",
    }

    assert memory_context_for_question("Who are the first five?", context) is context


def test_active_ones_followup_keeps_previous_query_memory():
    context = {
        "available": True,
        "event_count": 1,
        "events": [{"prior_question": "Show employees from Bangalore.", "status": "success"}],
        "policy": "bounded",
    }

    assert memory_context_for_question("Show only the active ones.", context) is context


def test_sqlite_reflection_does_not_force_public_schema():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE parents (id INTEGER PRIMARY KEY)")
        table = get_runtime_table("parents", bind=connection)
    assert table.schema is None
    assert list(table.c.keys()) == ["id"]


def test_resource_sample_handles_missing_current_process():
    with patch("app.services.benchmark_service.psutil.Process", side_effect=psutil.NoSuchProcess(999999)):
        outcome = benchmark_service._resource_sample(None)
    assert outcome.status == "success"
    assert next(item for item in outcome.metrics if item.metric_type == "backend_process_rss_mib").metric_value is None


def test_admin_role_header_is_accepted_for_local_demo():
    client = TestClient(create_app())
    response = client.get("/api/audit/status", headers={"X-User-Role": "admin"})
    assert response.status_code == 200


def test_admin_api_key_header_is_not_required():
    client = TestClient(create_app())
    response = client.get("/api/audit/status", headers={"X-User-Role": "admin"})
    assert response.status_code == 200
