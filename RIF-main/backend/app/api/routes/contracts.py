"""Contract endpoints that expose stable design decisions for later phases."""

from fastapi import APIRouter, Request

from app.core.constants import RelationshipCardinality
from app.core.response import ResponseBuilder
from app.core.schemas import ChildRelationshipContractData, ParentChildRelationContract
from app.db.health import get_database_health

router = APIRouter(prefix="/api/contracts", tags=["System contracts"])


@router.get(
    "/child-relations",
    summary="View the implemented parent/child table contract",
)
async def child_relations_contract(request: Request):
    """Return Feature 17's now-implemented Phase 2 relationship design."""

    relationship = ParentChildRelationContract(
        parent_table="employees",
        child_table="employee_permissions",
        parent_primary_key="employees.id",
        child_foreign_key="employee_permissions.employee_id",
        cardinality=RelationshipCardinality.ONE_TO_ZERO_OR_MANY,
        rule=(
            "One employee can have zero, one, or many permission records. "
            "Every permission record belongs to exactly one employee."
        ),
        sample_child_records=[
            "view_sales_dashboard",
            "edit_vendor_records",
            "approve_price_changes",
        ],
        planned_implementation_phase=2,
        deletion_policy=(
            "The PostgreSQL foreign key uses ON DELETE RESTRICT. The database blocks a parent delete while child permissions exist. "
            "Phase 7 will still preview affected child records before any delete confirmation."
        ),
    )
    database_health = get_database_health()
    return ResponseBuilder.success(
        request,
        answer="Feature 17 parent/child contract is implemented in the Phase 2 database migration.",
        data=ChildRelationshipContractData(
            database_connected=database_health.connected,
            note=(
                "The employees and employee_permissions tables are created by Alembic migration 20260623_0001. "
                "Run the seed command to create zero, one, and many child-record examples."
            ),
            relationships=[relationship],
        ).model_dump(),
    )
