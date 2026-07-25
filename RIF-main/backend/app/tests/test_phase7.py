"""Phase 7 deterministic CRUD safety tests. No local PostgreSQL/Ollama service is required."""

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.core.constants import ResponseStatus, UserRole
from app.services.crud_write_service import CrudWriteError, CrudWriteService
from app.services.duplicate_service import candidate_key_sets, find_batch_duplicates
from app.services.sql_validation import validate_dml_sql


INSERT_SQL = (
    "INSERT INTO employees "
    "(employee_code, first_name, last_name, email, department, city, company_name, salary, employment_status) "
    "VALUES ('EMP-P7MAYA', 'Maya', 'Nair', 'maya.phase7@example.com', 'Sales', 'Bangalore', 'Neolotex', 50000, 'active')"
)


def test_phase_seven_preserves_phase_four_insert_validation():
    result = validate_dml_sql(INSERT_SQL)
    assert result.is_valid is True
    assert result.statement_type == "INSERT"


def test_insert_preview_extracts_literal_records_and_rejects_nonliteral_values():
    service = CrudWriteService()
    expression = service._parse(INSERT_SQL)
    records = service._insert_records_from_sql(expression, "employees")
    assert records[0]["employee_code"] == "EMP-P7MAYA"
    assert records[0]["salary"] == 50000

    unsafe = service._parse(
        "INSERT INTO employees (employee_code, first_name, last_name, email, department, city, company_name, salary, employment_status) "
        "VALUES (UPPER('EMP-X'), 'Maya', 'Nair', 'x@example.com', 'Sales', 'Bangalore', 'Neolotex', 50000, 'active')"
    )
    with pytest.raises(CrudWriteError) as exc_info:
        service._insert_records_from_sql(unsafe, "employees")
    assert exc_info.value.code == "nonliteral_insert_value_blocked"


def test_duplicate_candidate_keys_prioritize_database_unique_fields():
    keys = candidate_key_sets(
        "employees",
        {"employee_code": "EMP-P7", "email": "maya@example.com", "phone": None},
    )
    assert ("employee_code",) in keys
    assert ("email",) in keys
    assert ("phone",) not in keys


def test_bulk_duplicate_detection_catches_duplicate_email_inside_one_batch():
    duplicates = find_batch_duplicates(
        "employees",
        [
            {"employee_code": "EMP-P7A", "email": "same@example.com"},
            {"employee_code": "EMP-P7B", "email": "same@example.com"},
        ],
    )
    assert len(duplicates) == 1
    assert duplicates[0]["key_fields"] == ["email"]


def test_update_and_delete_previews_require_where_via_existing_safety_gate():
    assert validate_dml_sql("UPDATE employees SET city = 'Kochi'").error_code == "where_clause_required"
    assert validate_dml_sql("DELETE FROM employees").error_code == "where_clause_required"


def test_write_target_rejects_operational_tables():
    service = CrudWriteService()
    with pytest.raises(CrudWriteError) as exc_info:
        service._business_table("query_logs")
    assert exc_info.value.code == "write_target_not_allowed"


def test_parent_delete_preview_is_blocked_when_child_permissions_exist():
    service = CrudWriteService()
    db = MagicMock()
    expression = service._parse("DELETE FROM employees WHERE employee_code = 'EMP-103'")
    with patch.object(service, "_require_session"), patch("app.services.crud_write_service.detect_record_duplicates", return_value=[]), patch.object(service, "_affected_rows", return_value=[{"id": 3, "employee_code": "EMP-103"}]), patch.object(service, "_employee_child_preview", return_value=[{"employee_id": 3, "permission_code": "approve_price_changes"}]):
        with pytest.raises(CrudWriteError) as exc_info:
            service.propose_sql_write(
                db,
                session_id=uuid4(),
                sql="DELETE FROM employees WHERE employee_code = 'EMP-103'",
                actor_role=UserRole.NORMAL_USER,
            )
    assert exc_info.value.status == ResponseStatus.CLARIFICATION_REQUIRED
    assert exc_info.value.code == "parent_child_records_exist"


def test_confirmed_action_is_idempotent_without_reexecuting():
    from app.models.operations import PendingAction

    service = CrudWriteService()
    action = MagicMock(spec=PendingAction)
    action.status = "confirmed"
    action.id = uuid4()
    action.session_id = uuid4()
    action.action_type = "insert"
    action.target_table = "employees"
    action.validated_payload = {}
    action.preview_data = {}
    action.generated_sql = None
    action.expires_at = None
    action.confirmed_at = None
    action.cancelled_at = None

    db = MagicMock()
    with patch.object(service, "_require_session"), patch.object(service, "_load_pending_for_mutation", return_value=action):
        result = service.confirm_action(
            db,
            session_id=action.session_id,
            pending_action_id=action.id,
            actor_role=UserRole.NORMAL_USER,
            request_id="phase7-idempotent",
        )
    assert result.idempotent is True
    db.execute.assert_not_called()


@pytest.mark.parametrize("count", [1, 5, 10, 20, 50])
def test_bulk_preview_preserves_requested_generated_preview_and_stored_counts(count):
    service = CrudWriteService()
    records = [
        {
            "employee_code": f"EMP-COUNT-{index:02d}",
            "first_name": "Demo",
            "last_name": f"Employee{index}",
            "email": f"demo.employee.{index}@example.test",
            "department": "IT",
            "city": "Chennai",
            "company_name": "Neolotex",
            "salary": 50000,
            "employment_status": "active",
        }
        for index in range(1, count + 1)
    ]
    action = MagicMock()

    with (
        patch.object(service, "_require_session"),
        patch.object(service, "_business_table"),
        patch.object(service, "_validate_records"),
        patch("app.services.crud_write_service.find_batch_duplicates", return_value=[]),
        patch("app.services.crud_write_service.detect_record_duplicates", return_value=[]),
        patch("app.services.crud_write_service.create_pending_action", return_value=action),
        patch("app.services.crud_write_service.pending_action_to_dict", return_value={"pending_action_id": str(uuid4())}),
    ):
        result = service.propose_bulk_insert(
            MagicMock(),
            session_id=uuid4(),
            target_table="employees",
            records=records,
            actor_role=UserRole.NORMAL_USER,
            generation_metadata={
                "requested_record_count": count,
                "generated_record_count": count,
            },
        )

    assert result.preview["record_count"] == count
    assert result.preview["requested_record_count"] == count
    assert result.preview["generated_record_count"] == count
    assert result.preview["preview_record_count"] == count
    assert len(result.preview["records"]) == count
    assert result.preview["count_contract"] == {
        "requested_record_count": count,
        "generated_record_count": count,
        "preview_record_count": count,
        "stored_record_count": count,
        "confirmed_record_count": None,
        "count_verified": True,
    }


def test_bulk_preview_rejects_a_requested_generated_count_mismatch():
    service = CrudWriteService()
    records = [{"employee_code": f"EMP-MISMATCH-{index}"} for index in range(5)]

    with (
        patch.object(service, "_require_session"),
        patch.object(service, "_business_table"),
        patch.object(service, "_validate_records"),
    ):
        with pytest.raises(CrudWriteError) as exc_info:
            service.propose_bulk_insert(
                MagicMock(),
                session_id=uuid4(),
                target_table="employees",
                records=records,
                actor_role=UserRole.NORMAL_USER,
                generation_metadata={
                    "requested_record_count": 10,
                    "generated_record_count": 5,
                },
            )

    assert exc_info.value.code == "bulk_count_contract_mismatch"


@pytest.mark.parametrize("count", [1, 5, 10, 20, 50])
def test_bulk_confirmation_uses_returning_and_verifies_exact_inserted_count(count):
    service = CrudWriteService()
    records = [
        {
            "employee_code": f"EMP-CONFIRM-{index:02d}",
            "first_name": "Demo",
            "last_name": f"Employee{index}",
            "email": f"confirm.employee.{index}@example.test",
            "department": "IT",
            "city": "Chennai",
            "company_name": "Neolotex",
            "salary": 50000,
            "employment_status": "active",
        }
        for index in range(1, count + 1)
    ]
    returned_records = [{"id": index, **record} for index, record in enumerate(records, start=101)]

    action = MagicMock()
    action.status = "pending"
    action.id = uuid4()
    action.action_type = "bulk_insert"
    action.target_table = "employees"
    action.validated_payload = {
        "mode": "bulk_records",
        "target_table": "employees",
        "records": records,
        "generation_metadata": {
            "requested_record_count": count,
            "generated_record_count": count,
        },
        "count_contract": {
            "requested_record_count": count,
            "generated_record_count": count,
            "preview_record_count": count,
            "stored_record_count": count,
        },
    }

    table = MagicMock()
    table.columns = [MagicMock(name="id")]
    insert_statement = MagicMock()
    table.insert.return_value.returning.return_value = insert_statement

    execute_result = MagicMock()
    execute_result.mappings.return_value.all.return_value = returned_records
    db = MagicMock()
    db.execute.return_value = execute_result

    action_log = MagicMock()
    action_log.id = 901

    with (
        patch.object(service, "_require_session"),
        patch.object(service, "_load_pending_for_mutation", return_value=action),
        patch.object(service, "_business_table", return_value=table),
        patch.object(service, "_validate_records"),
        patch.object(service, "_create_action_log", return_value=action_log),
        patch.object(service, "_snapshot"),
        patch("app.services.crud_write_service.detect_record_duplicates", return_value=[]),
        patch("app.services.crud_write_service.pending_action_to_dict", return_value={"pending_action_id": str(action.id)}),
    ):
        result = service.confirm_action(
            db,
            session_id=uuid4(),
            pending_action_id=action.id,
            actor_role=UserRole.NORMAL_USER,
            request_id="bulk-count-confirm",
        )

    assert result.expected_row_count == count
    assert result.affected_row_count == count
    assert result.after_snapshot_count == count
    assert result.count_verified is True
    assert len(result.affected_records) == count
    assert isinstance(result.affected_records[0], dict)
    assert result.affected_records[0]["employee_code"] == "EMP-CONFIRM-01"
    db.execute.assert_called_once_with(insert_statement, records)
    db.commit.assert_called_once()


def test_bulk_confirmation_rolls_back_when_postgres_returns_fewer_rows_than_requested():
    service = CrudWriteService()
    count = 10
    records = [{"employee_code": f"EMP-ROLLBACK-{index:02d}"} for index in range(1, count + 1)]

    action = MagicMock()
    action.status = "pending"
    action.id = uuid4()
    action.action_type = "bulk_insert"
    action.target_table = "employees"
    action.validated_payload = {
        "mode": "bulk_records",
        "target_table": "employees",
        "records": records,
        "generation_metadata": {"requested_record_count": count, "generated_record_count": count},
        "count_contract": {
            "requested_record_count": count,
            "generated_record_count": count,
            "preview_record_count": count,
            "stored_record_count": count,
        },
    }

    table = MagicMock()
    table.columns = [MagicMock(name="id")]
    execute_result = MagicMock()
    execute_result.mappings.return_value.all.return_value = [{"id": index, **records[index - 1]} for index in range(1, 6)]
    db = MagicMock()
    db.execute.return_value = execute_result

    with (
        patch.object(service, "_require_session"),
        patch.object(service, "_load_pending_for_mutation", return_value=action),
        patch.object(service, "_business_table", return_value=table),
        patch.object(service, "_validate_records"),
        patch.object(service, "_create_action_log", return_value=MagicMock(id=902)),
        patch("app.services.crud_write_service.detect_record_duplicates", return_value=[]),
    ):
        with pytest.raises(CrudWriteError) as exc_info:
            service.confirm_action(
                db,
                session_id=uuid4(),
                pending_action_id=action.id,
                actor_role=UserRole.NORMAL_USER,
                request_id="bulk-count-rollback",
            )

    assert exc_info.value.code == "confirmed_bulk_count_mismatch"
    db.rollback.assert_called_once()
    db.commit.assert_not_called()
