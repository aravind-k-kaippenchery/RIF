"""Idempotent Phase 2 seed command for realistic local demo data.

Run from backend/ after Alembic migration:
    python -m app.db.seed
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging_config import configure_logging, log_event
from app.db.session import get_session_factory
from app.models import (
    Customer,
    Employee,
    EmployeePermission,
    Product,
    ProductVendorMapping,
    SalesDeal,
    Vendor,
)


def _get_or_create(session: Session, model, lookup: dict, defaults: dict):
    instance = session.scalar(select(model).filter_by(**lookup))
    if instance is not None:
        return instance, False
    instance = model(**lookup, **defaults)
    session.add(instance)
    session.flush()
    return instance, True


def seed_phase_two_data() -> dict[str, int]:
    """Create deterministic demo records without duplicating existing rows."""

    created = {
        "employees": 0,
        "employee_permissions": 0,
        "vendors": 0,
        "customers": 0,
        "products": 0,
        "product_vendor_mappings": 0,
        "sales_deals": 0,
    }
    session = get_session_factory()()
    try:
        employee_rows = [
            ("EMP-101", {"first_name": "Ravi", "last_name": "Kumar", "email": "ravi.kumar@neolotex.local", "phone": "9000000101", "department": "HR", "city": "Chennai", "company_name": "Neolotex", "salary": Decimal("45000.00")}),
            ("EMP-102", {"first_name": "Anjali", "last_name": "Nair", "email": "anjali.nair@neolotex.local", "phone": "9000000102", "department": "Finance", "city": "Bangalore", "company_name": "Neolotex", "salary": Decimal("65000.00")}),
            ("EMP-103", {"first_name": "Arjun", "last_name": "Das", "email": "arjun.das@neolotex.local", "phone": "9000000103", "department": "IT", "city": "Kochi", "company_name": "Neolotex", "salary": Decimal("75000.00")}),
            ("EMP-104", {"first_name": "Sneha", "last_name": "Roy", "email": "sneha.roy@neolotex.local", "phone": "9000000104", "department": "Sales", "city": "Chennai", "company_name": "Neolotex", "salary": Decimal("50000.00")}),
            ("EMP-105", {"first_name": "Vishnu", "last_name": "Menon", "email": "vishnu.menon@neolotex.local", "phone": "9000000105", "department": "HR", "city": "Bangalore", "company_name": "Neolotex", "salary": Decimal("48000.00")}),
            ("EMP-106", {"first_name": "Meera", "last_name": "Joseph", "email": "meera.joseph@neolotex.local", "phone": "9000000106", "department": "IT", "city": "Chennai", "company_name": "Neolotex", "salary": Decimal("82000.00")}),
            ("EMP-107", {"first_name": "Akash", "last_name": "Patel", "email": "akash.patel@neolotex.local", "phone": "9000000107", "department": "Finance", "city": "Mumbai", "company_name": "Neolotex", "salary": Decimal("70000.00")}),
            ("EMP-108", {"first_name": "Priya", "last_name": "Sharma", "email": "priya.sharma@neolotex.local", "phone": "9000000108", "department": "Sales", "city": "Delhi", "company_name": "Neolotex", "salary": Decimal("55000.00")}),
            ("EMP-109", {"first_name": "Rahul", "last_name": "Verma", "email": "rahul.verma@neolotex.local", "phone": "9000000109", "department": "IT", "city": "Bangalore", "company_name": "Neolotex", "salary": Decimal("90000.00")}),
            ("EMP-110", {"first_name": "Neha", "last_name": "Gupta", "email": "neha.gupta@neolotex.local", "phone": "9000000110", "department": "HR", "city": "Kochi", "company_name": "Neolotex", "salary": Decimal("46000.00")}),
        ]
        employees: dict[str, Employee] = {}
        for employee_code, defaults in employee_rows:
            employee, was_created = _get_or_create(session, Employee, {"employee_code": employee_code}, defaults)
            employees[employee_code] = employee
            created["employees"] += int(was_created)

        permission_rows = [
            ("EMP-102", "view_sales_dashboard", "Can view sales dashboard metrics."),
            ("EMP-103", "view_sales_dashboard", "Can view sales dashboard metrics."),
            ("EMP-103", "edit_vendor_records", "Can edit vendor records after confirmation."),
            ("EMP-103", "approve_price_changes", "Can approve product price changes."),
            ("EMP-104", "view_customer_records", "Can view customer records."),
        ]
        for employee_code, permission_code, description in permission_rows:
            permission, was_created = _get_or_create(
                session,
                EmployeePermission,
                {"employee_id": employees[employee_code].id, "permission_code": permission_code},
                {"description": description, "is_active": True},
            )
            created["employee_permissions"] += int(was_created)

        vendor_rows = [
            ("VND-001", {"vendor_name": "Neolotex Systems", "contact_email": "contact@neolotexsystems.local", "phone": "8000000001", "city": "Bangalore", "country": "India", "category": "Textile Automation", "status": "active"}),
            ("VND-002", {"vendor_name": "Ravi Textiles", "contact_email": "sales@ravitextiles.local", "phone": "8000000002", "city": "Chennai", "country": "India", "category": "Industrial Textiles", "status": "active"}),
            ("VND-003", {"vendor_name": "Kochi Machinery Works", "contact_email": "info@kochimachinery.local", "phone": "8000000003", "city": "Kochi", "country": "India", "category": "Industrial Automation", "status": "active"}),
            ("VND-004", {"vendor_name": "Metro Fabric Solutions", "contact_email": "hello@metrofabric.local", "phone": "8000000004", "city": "Mumbai", "country": "India", "category": "Textile Automation", "status": "active"}),
        ]
        vendors: dict[str, Vendor] = {}
        for vendor_code, defaults in vendor_rows:
            vendor, was_created = _get_or_create(session, Vendor, {"vendor_code": vendor_code}, defaults)
            vendors[vendor_code] = vendor
            created["vendors"] += int(was_created)

        customer_rows = [
            ("CUS-001", {"customer_name": "Bangalore Weaves Pvt Ltd", "contact_email": "ops@bangaloreweaves.local", "phone": "8100000001", "city": "Bangalore", "country": "India", "industry": "Textiles", "status": "active"}),
            ("CUS-002", {"customer_name": "Chennai Fabric House", "contact_email": "procurement@chennaifabric.local", "phone": "8100000002", "city": "Chennai", "country": "India", "industry": "Textiles", "status": "active"}),
            ("CUS-003", {"customer_name": "Kochi Smart Mills", "contact_email": "contact@kochismartmills.local", "phone": "8100000003", "city": "Kochi", "country": "India", "industry": "Manufacturing", "status": "active"}),
        ]
        customers: dict[str, Customer] = {}
        for customer_code, defaults in customer_rows:
            customer, was_created = _get_or_create(session, Customer, {"customer_code": customer_code}, defaults)
            customers[customer_code] = customer
            created["customers"] += int(was_created)

        product_rows = [
            ("PRD-001", {"product_name": "TextileBot X", "category": "Textile Automation", "description": "Automated weaving and fabric handling unit.", "list_price": Decimal("480000.00"), "is_active": True}),
            ("PRD-002", {"product_name": "FabricTrack Pro", "category": "Quality Control", "description": "Fabric inspection and defect tracking system.", "list_price": Decimal("325000.00"), "is_active": True}),
            ("PRD-003", {"product_name": "LoomSync 360", "category": "Industrial Automation", "description": "Production monitoring dashboard for connected looms.", "list_price": Decimal("520000.00"), "is_active": True}),
        ]
        products: dict[str, Product] = {}
        for product_code, defaults in product_rows:
            product, was_created = _get_or_create(session, Product, {"product_code": product_code}, defaults)
            products[product_code] = product
            created["products"] += int(was_created)

        mapping_rows = [
            ("PRD-001", "VND-001", "NTX-TBX-01", Decimal("475000.00"), True),
            ("PRD-001", "VND-004", "MFS-TBX-01", Decimal("490000.00"), False),
            ("PRD-002", "VND-002", "RT-FTP-02", Decimal("318000.00"), True),
            ("PRD-003", "VND-003", "KMW-LS-03", Decimal("515000.00"), True),
        ]
        for product_code, vendor_code, vendor_sku, quoted_price, is_preferred in mapping_rows:
            mapping, was_created = _get_or_create(
                session,
                ProductVendorMapping,
                {"product_id": products[product_code].id, "vendor_id": vendors[vendor_code].id},
                {"vendor_sku": vendor_sku, "quoted_price": quoted_price, "is_preferred": is_preferred},
            )
            created["product_vendor_mappings"] += int(was_created)

        deal_rows = [
            ("DEAL-001", {"title": "Bangalore Weaves TextileBot rollout", "customer_id": customers["CUS-001"].id, "product_id": products["PRD-001"].id, "owner_employee_id": employees["EMP-104"].id, "amount": Decimal("950000.00"), "stage": "proposal", "probability": 65, "expected_close_date": date(2026, 7, 15), "status": "open"}),
            ("DEAL-002", {"title": "Chennai Fabric quality-control upgrade", "customer_id": customers["CUS-002"].id, "product_id": products["PRD-002"].id, "owner_employee_id": employees["EMP-108"].id, "amount": Decimal("636000.00"), "stage": "negotiation", "probability": 80, "expected_close_date": date(2026, 7, 2), "status": "open"}),
            ("DEAL-003", {"title": "Kochi Smart Mills monitoring pilot", "customer_id": customers["CUS-003"].id, "product_id": products["PRD-003"].id, "owner_employee_id": employees["EMP-104"].id, "amount": Decimal("515000.00"), "stage": "qualification", "probability": 40, "expected_close_date": date(2026, 8, 20), "status": "open"}),
        ]
        for deal_code, defaults in deal_rows:
            deal, was_created = _get_or_create(session, SalesDeal, {"deal_code": deal_code}, defaults)
            created["sales_deals"] += int(was_created)

        session.commit()
        return created
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def main() -> None:
    configure_logging()
    created = seed_phase_two_data()
    log_event(level="INFO", event="phase2_seed_completed", **created)
    print("Phase 2 seed completed successfully.")
    for name, count in created.items():
        print(f"- {name}: {count} new record(s)")
    print("Verification cases: EMP-101 has 0 permissions, EMP-102 has 1, EMP-103 has 3.")


if __name__ == "__main__":
    main()
