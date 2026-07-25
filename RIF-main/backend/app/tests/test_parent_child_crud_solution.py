"""Focused tests for employee experiences and semantic parent-child CRUD."""

from datetime import date
from unittest.mock import MagicMock, patch
from uuid import uuid4

from sqlalchemy import create_engine, insert
from sqlalchemy.orm import Session

from app.core.constants import AgentRoute, ResponseStatus, UserRole
from app.models import Base, EmployeeExperience
from app.schemas.phase17 import ChildFilter, ParentChildIntentPlan, ParentReference
from app.services.parent_child_crud_service import (
    ParentChildCrudService,
    looks_like_parent_child_request,
    supported_parent_child_tables,
)
from app.services.schema_registry import BUSINESS_TABLES, get_relationships


def _business_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    for table_name in (
        "employees",
        "vendors",
        "customers",
        "products",
        "employee_permissions",
        "employee_experiences",
        "product_vendor_mappings",
        "sales_deals",
    ):
        Base.metadata.tables[table_name].create(engine, checkfirst=True)
    return Session(engine)


def _seed_parents(db: Session) -> None:
    employees = Base.metadata.tables["employees"]
    products = Base.metadata.tables["products"]
    vendors = Base.metadata.tables["vendors"]
    customers = Base.metadata.tables["customers"]
    db.execute(
        insert(employees),
        [
            {
                "employee_code": "EMP-105",
                "first_name": "Vishnu",
                "last_name": "Menon",
                "email": "vishnu@example.test",
                "department": "HR",
                "city": "Bangalore",
                "company_name": "Neolotex",
                "salary": 50000,
                "employment_status": "active",
            }
        ],
    )
    db.execute(
        insert(products),
        [{"product_code": "PRD-001", "product_name": "TextileBot X", "category": "Automation", "description": "Demo", "list_price": 480000, "is_active": True}],
    )
    db.execute(
        insert(vendors),
        [{"vendor_code": "VND-001", "vendor_name": "Neolotex Systems", "contact_email": "vendor@example.test", "city": "Bangalore", "country": "India", "category": "Automation", "status": "active"}],
    )
    db.execute(
        insert(customers),
        [{"customer_code": "CUS-001", "customer_name": "Bangalore Weaves", "contact_email": "customer@example.test", "city": "Bangalore", "country": "India", "industry": "Textiles", "status": "active"}],
    )
    db.commit()


def test_employee_experience_model_and_registry_are_present():
    assert EmployeeExperience.__tablename__ == "employee_experiences"
    assert "employee_experiences" in BUSINESS_TABLES
    relationships = [item for item in get_relationships() if item["from_table"] == "employee_experiences"]
    assert relationships == [
        {
            "from_table": "employee_experiences",
            "from_column": "employee_id",
            "to_table": "employees",
            "to_column": "id",
            "relationship_type": "many_to_one",
            "delete_rule": "RESTRICT",
        }
    ]


def test_parent_child_detector_and_supported_tables():
    assert looks_like_parent_child_request("Show the work history of EMP-105") is True
    assert looks_like_parent_child_request("Link PRD-001 to VND-001") is True
    assert looks_like_parent_child_request("Show workers from Bangalore") is False
    assert "employee_experiences" in supported_parent_child_tables()


def test_read_experience_resolves_employee_code_and_returns_enriched_rows():
    db = _business_session()
    _seed_parents(db)
    employees = Base.metadata.tables["employees"]
    experiences = Base.metadata.tables["employee_experiences"]
    employee_id = db.execute(employees.select().where(employees.c.employee_code == "EMP-105")).mappings().one()["id"]
    db.execute(
        insert(experiences),
        [{
            "employee_id": employee_id,
            "company_name": "Infosys",
            "job_title": "Python Developer",
            "employment_type": "full_time",
            "location": "Bangalore",
            "start_date": date(2021, 1, 1),
            "end_date": date(2024, 1, 1),
            "description": "Backend development",
            "is_current": False,
        }],
    )
    db.commit()

    plan = ParentChildIntentPlan(
        operation="read",
        child_table="employee_experiences",
        parent_references=[ParentReference(role="employee", code="EMP-105")],
        confidence=0.99,
        interpretation="Read employee work history.",
    )
    service = ParentChildCrudService()
    with patch.object(service, "_generate_plan", return_value=(plan, {"model": "test"})):
        result = service.execute(
            db,
            question="Show the work history of EMP-105",
            session_id=uuid4(),
            actor_role=UserRole.NORMAL_USER,
        )

    assert result.route == AgentRoute.STRUCTURED_READ
    assert result.status == ResponseStatus.SUCCESS
    assert result.data["row_count"] == 1
    assert result.data["rows"][0]["employee_code"] == "EMP-105"
    assert result.data["rows"][0]["company_name"] == "Infosys"
    db.close()


def test_create_experience_resolves_parent_and_uses_confirmation_preview():
    db = _business_session()
    _seed_parents(db)
    plan = ParentChildIntentPlan(
        operation="create",
        child_table="employee_experiences",
        parent_references=[ParentReference(role="employee", code="EMP-105")],
        values={
            "company_name": "Infosys",
            "job_title": "Python Developer",
            "start_date": "2021-01-01",
            "end_date": "2024-01-01",
            "is_current": False,
        },
        confidence=0.99,
        interpretation="Create one experience.",
    )
    proposal = MagicMock()
    proposal.pending_action = {"pending_action_id": str(uuid4())}
    proposal.preview = {"records": [{"employee_id": 1, "company_name": "Infosys"}]}
    proposal.duplicate_matches = []

    service = ParentChildCrudService()
    with (
        patch.object(service, "_generate_plan", return_value=(plan, {"model": "test"})),
        patch("app.services.parent_child_crud_service.crud_write_service.propose_bulk_insert", return_value=proposal) as propose,
    ):
        result = service.execute(
            db,
            question="Add experience for EMP-105 at Infosys",
            session_id=uuid4(),
            actor_role=UserRole.NORMAL_USER,
        )

    assert result.status == ResponseStatus.PENDING_CONFIRMATION
    call = propose.call_args.kwargs
    assert call["target_table"] == "employee_experiences"
    assert call["records"][0]["employee_id"] == 1
    assert call["records"][0]["start_date"] == date(2021, 1, 1)
    db.close()


def test_product_vendor_mapping_resolves_both_business_codes():
    db = _business_session()
    _seed_parents(db)
    plan = ParentChildIntentPlan(
        operation="create",
        child_table="product_vendor_mappings",
        parent_references=[
            ParentReference(role="product", code="PRD-001"),
            ParentReference(role="vendor", code="VND-001"),
        ],
        values={"quoted_price": 450000, "vendor_sku": "SKU-001"},
        confidence=0.99,
        interpretation="Link product and vendor.",
    )
    proposal = MagicMock()
    proposal.pending_action = {"pending_action_id": str(uuid4())}
    proposal.preview = {"records": []}
    proposal.duplicate_matches = []
    service = ParentChildCrudService()
    with (
        patch.object(service, "_generate_plan", return_value=(plan, {"model": "test"})),
        patch("app.services.parent_child_crud_service.crud_write_service.propose_bulk_insert", return_value=proposal) as propose,
    ):
        service.execute(
            db,
            question="Link PRD-001 to VND-001 with price 450000",
            session_id=uuid4(),
            actor_role=UserRole.NORMAL_USER,
        )
    record = propose.call_args.kwargs["records"][0]
    assert record["product_id"] == 1
    assert record["vendor_id"] == 1
    db.close()


def test_update_multiple_matches_requests_clarification_instead_of_guessing():
    db = _business_session()
    _seed_parents(db)
    employees = Base.metadata.tables["employees"]
    experiences = Base.metadata.tables["employee_experiences"]
    employee_id = db.execute(employees.select().where(employees.c.employee_code == "EMP-105")).mappings().one()["id"]
    db.execute(
        insert(experiences),
        [
            {"employee_id": employee_id, "company_name": "A", "job_title": "Developer", "start_date": date(2020, 1, 1), "is_current": False},
            {"employee_id": employee_id, "company_name": "B", "job_title": "Developer", "start_date": date(2022, 1, 1), "is_current": False},
        ],
    )
    db.commit()
    plan = ParentChildIntentPlan(
        operation="update",
        child_table="employee_experiences",
        parent_references=[ParentReference(role="employee", code="EMP-105")],
        values={"job_title": "Senior Developer"},
        confidence=0.99,
        interpretation="Update an experience.",
    )
    service = ParentChildCrudService()
    with patch.object(service, "_generate_plan", return_value=(plan, {"model": "test"})):
        result = service.execute(
            db,
            question="Update the experience of EMP-105",
            session_id=uuid4(),
            actor_role=UserRole.NORMAL_USER,
        )
    assert result.status == ResponseStatus.CLARIFICATION_REQUIRED
    assert "2 employee experience records match" in result.answer
    db.close()


def test_case_insensitive_child_company_filter():
    db = _business_session()
    _seed_parents(db)
    employees = Base.metadata.tables["employees"]
    experiences = Base.metadata.tables["employee_experiences"]
    employee_id = db.execute(employees.select().where(employees.c.employee_code == "EMP-105")).mappings().one()["id"]
    db.execute(insert(experiences), [{"employee_id": employee_id, "company_name": "Infosys", "job_title": "Developer", "start_date": date(2020, 1, 1), "is_current": False}])
    db.commit()
    plan = ParentChildIntentPlan(
        operation="read",
        child_table="employee_experiences",
        parent_references=[ParentReference(role="employee", code="EMP-105")],
        filters=[ChildFilter(field="company", operator="eq", value="infosys")],
        confidence=0.99,
        interpretation="Read matching experience.",
    )
    service = ParentChildCrudService()
    with patch.object(service, "_generate_plan", return_value=(plan, {"model": "test"})):
        result = service.execute(db, question="Show infosys experience of EMP-105", session_id=uuid4(), actor_role=UserRole.NORMAL_USER)
    assert result.data["row_count"] == 1
    db.close()
