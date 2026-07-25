from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.agents.router import classify_question
from app.core.constants import AgentRoute
from app.models import Base, Customer, Employee, Product, Vendor
from app.models.business import EmployeePermission, ProductVendorMapping, SalesDeal
from app.services.synthetic_data_service import (
    SyntheticDataGenerationError,
    SyntheticDataRequest,
    generate_synthetic_records,
    parse_synthetic_data_prompt,
    supported_synthetic_tables,
)


@pytest.fixture()
def db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            Employee.__table__,
            EmployeePermission.__table__,
            Vendor.__table__,
            Customer.__table__,
            Product.__table__,
            ProductVendorMapping.__table__,
            SalesDeal.__table__,
        ],
    )
    with Session(engine) as session:
        session.add_all(
            [
                Employee(
                    employee_code="EMP-TEST-1",
                    first_name="Asha",
                    last_name="Nair",
                    email="asha@example.test",
                    phone="+910000000001",
                    department="Sales",
                    city="Kochi",
                    company_name="Neolotex",
                    salary=Decimal("60000"),
                    employment_status="active",
                ),
                Employee(
                    employee_code="EMP-TEST-2",
                    first_name="Rahul",
                    last_name="Menon",
                    email="rahul@example.test",
                    phone="+910000000002",
                    department="IT",
                    city="Chennai",
                    company_name="Neolotex",
                    salary=Decimal("70000"),
                    employment_status="active",
                ),
                Vendor(
                    vendor_code="VEN-TEST-1",
                    vendor_name="Test Vendor One",
                    contact_email="vendor1@example.test",
                    phone="+910000000011",
                    city="Kochi",
                    country="India",
                    category="Textile Automation",
                    status="active",
                ),
                Vendor(
                    vendor_code="VEN-TEST-2",
                    vendor_name="Test Vendor Two",
                    contact_email="vendor2@example.test",
                    phone="+910000000012",
                    city="Chennai",
                    country="India",
                    category="Industrial Equipment",
                    status="active",
                ),
                Customer(
                    customer_code="CUST-TEST-1",
                    customer_name="Test Customer",
                    contact_email="customer@example.test",
                    phone="+910000000021",
                    city="Bangalore",
                    country="India",
                    industry="Textiles",
                    status="active",
                ),
                Product(
                    product_code="PROD-TEST-1",
                    product_name="Test Product One",
                    category="Textile Automation",
                    description="Test product",
                    list_price=Decimal("500000"),
                    is_active=True,
                ),
                Product(
                    product_code="PROD-TEST-2",
                    product_name="Test Product Two",
                    category="Quality Control",
                    description="Test product",
                    list_price=Decimal("300000"),
                    is_active=True,
                ),
            ]
        )
        session.commit()
        yield session


def test_parser_supports_all_requested_business_targets():
    prompts = {
        "Create 3 random employees": "employees",
        "Generate 4 sample vendors": "vendors",
        "Seed 2 demo customers": "customers",
        "Add 5 random products": "products",
        "Generate 2 synthetic sales deals": "sales_deals",
        "Create 3 random employee permissions": "employee_permissions",
        "Populate 2 random product vendor mappings": "product_vendor_mappings",
        "Add 4 random people into the customers table": "customers",
    }
    for prompt, expected_table in prompts.items():
        request = parse_synthetic_data_prompt(prompt)
        assert request is not None
        assert request.target_table == expected_table


def test_non_synthetic_single_record_prompt_stays_on_llm_path():
    assert parse_synthetic_data_prompt("Add vendor ABC Supplies in Kochi") is None


def test_generate_and_seed_verbs_route_to_crud():
    assert classify_question("Generate 5 random vendors").route == AgentRoute.CRUD_WRITE
    assert classify_question("Populate 3 product vendor mappings").route == AgentRoute.CRUD_WRITE


@pytest.mark.parametrize(
    ("table_name", "count"),
    [
        ("employees", 3),
        ("vendors", 3),
        ("customers", 3),
        ("products", 3),
        ("employee_permissions", 3),
        ("sales_deals", 3),
        ("product_vendor_mappings", 3),
    ],
)
def test_registered_profiles_generate_exact_count(db: Session, table_name: str, count: int):
    batch = generate_synthetic_records(
        db,
        SyntheticDataRequest(target_table=table_name, count=count),
    )

    assert batch.target_table == table_name
    assert len(batch.records) == count
    assert batch.metadata["requested_record_count"] == count
    assert batch.metadata["generated_record_count"] == count
    assert batch.metadata["generator_mode"] == "registered_profile"


def test_relationship_profiles_use_existing_parent_rows(db: Session):
    employee_ids = {row.id for row in db.query(Employee).all()}
    customer_ids = {row.id for row in db.query(Customer).all()}
    product_ids = {row.id for row in db.query(Product).all()}
    vendor_ids = {row.id for row in db.query(Vendor).all()}

    permissions = generate_synthetic_records(
        db,
        SyntheticDataRequest(target_table="employee_permissions", count=4),
    ).records
    deals = generate_synthetic_records(
        db,
        SyntheticDataRequest(target_table="sales_deals", count=4),
    ).records
    mappings = generate_synthetic_records(
        db,
        SyntheticDataRequest(target_table="product_vendor_mappings", count=4),
    ).records

    assert {item["employee_id"] for item in permissions} <= employee_ids
    assert {item["customer_id"] for item in deals} <= customer_ids
    assert {item["product_id"] for item in mappings} <= product_ids
    assert {item["vendor_id"] for item in mappings} <= vendor_ids
    assert len({(item["product_id"], item["vendor_id"]) for item in mappings}) == 4


def test_specific_parent_code_is_respected(db: Session):
    request = parse_synthetic_data_prompt("Generate 2 random permissions for employee EMP-TEST-1")
    assert request is not None
    batch = generate_synthetic_records(db, request)
    employee = db.query(Employee).filter(Employee.employee_code == "EMP-TEST-1").one()
    assert {item["employee_id"] for item in batch.records} == {employee.id}


def test_relationship_generator_requires_parent_rows():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            Employee.__table__,
            EmployeePermission.__table__,
            Vendor.__table__,
            Customer.__table__,
            Product.__table__,
            ProductVendorMapping.__table__,
            SalesDeal.__table__,
        ],
    )
    with Session(engine) as empty_db:
        with pytest.raises(SyntheticDataGenerationError) as exc_info:
            generate_synthetic_records(
                empty_db,
                SyntheticDataRequest(target_table="sales_deals", count=1),
            )
    assert exc_info.value.code == "synthetic_parent_records_required"


def test_supported_table_status_covers_all_current_business_tables():
    names = {item["table_name"] for item in supported_synthetic_tables()}
    assert {
        "employees",
        "vendors",
        "customers",
        "products",
        "sales_deals",
        "employee_permissions",
        "product_vendor_mappings",
    } <= names
