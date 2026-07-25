"""Phase 16 final POC demo, readiness, and release-evidence endpoints."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.constants import UserRole
from app.core.response import ResponseBuilder
from app.core.security import require_role
from app.db.session import get_db_session
from app.services.demo_readiness_service import demo_readiness_service

router = APIRouter(prefix="/api/demo", tags=["Final demo and release evidence"])


@router.get("/status", summary="View final local POC readiness without operational metric details")
def demo_status(request: Request):
    return ResponseBuilder.success(
        request,
        answer="Phase 16 final local POC demo readiness retrieved.",
        data=demo_readiness_service.public_status(),
    )


@router.get("/features", summary="View the required 17-feature implementation matrix")
def demo_features(request: Request):
    matrix = demo_readiness_service.feature_matrix()
    return ResponseBuilder.success(
        request,
        answer="Final 17-feature implementation matrix retrieved.",
        data={
            "phase": 16,
            "feature_count": len(matrix),
            "features": matrix,
            "raw_sql_accepted": False,
            "business_write_executed": False,
        },
    )


@router.get("/scenarios", summary="View the ordered reviewer-facing final demo scenarios")
def demo_scenarios(request: Request):
    scenarios = demo_readiness_service.demo_scenarios()
    return ResponseBuilder.success(
        request,
        answer="Ordered final demonstration scenarios retrieved. The endpoint only describes scenarios and does not execute them.",
        data={
            "phase": 16,
            "scenario_count": len(scenarios),
            "scenarios": scenarios,
            "raw_sql_accepted": False,
            "business_write_executed": False,
            "model_generation_triggered": False,
        },
    )


@router.get("/readiness", summary="View admin-only final readiness and persisted benchmark evidence")
def demo_readiness(
    request: Request,
    db: Session = Depends(get_db_session),
    _: UserRole = Depends(require_role(UserRole.ADMIN)),
):
    return ResponseBuilder.success(
        request,
        answer="Final POC readiness and bounded persisted benchmark evidence retrieved.",
        data=demo_readiness_service.readiness(db),
        sources=[{
            "source_type": "database",
            "reference": "benchmark_runs",
            "detail": "Bounded local benchmark evidence used for final readiness only.",
        }],
    )


@router.get("/report", summary="Generate an admin-only presentation-ready final POC report")
def demo_report(
    request: Request,
    db: Session = Depends(get_db_session),
    _: UserRole = Depends(require_role(UserRole.ADMIN)),
):
    return ResponseBuilder.success(
        request,
        answer="Presentation-ready final POC report generated from implemented capabilities and local observed readiness.",
        data=demo_readiness_service.final_report(db),
        sources=[{
            "source_type": "database",
            "reference": "benchmark_runs",
            "detail": "Only bounded persisted measurement highlights are included; no credentials or raw logs are exposed.",
        }],
    )


@router.get("/smoke", summary="Run an admin-only non-destructive final integration smoke check")
def demo_smoke(
    request: Request,
    db: Session = Depends(get_db_session),
    _: UserRole = Depends(require_role(UserRole.ADMIN)),
):
    return ResponseBuilder.success(
        request,
        answer="Final integration smoke check completed without raw SQL, model generation, rollback, or business-table writes.",
        data=demo_readiness_service.smoke(db),
        sources=[{
            "source_type": "database",
            "reference": "benchmark_runs",
            "detail": "Read-only persisted benchmark availability check.",
        }],
    )
