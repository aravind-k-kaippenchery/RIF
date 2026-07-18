"""Phase 15 benchmarking, quality metrics, and final safety evaluation APIs."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.core.constants import UserRole
from app.core.response import ResponseBuilder
from app.core.security import require_role
from app.db.session import get_db_session
from app.schemas.phase15 import BenchmarkRunRequest
from app.services.benchmark_service import benchmark_service

router = APIRouter(prefix="/api/benchmarks", tags=["Benchmarking and safety evaluation"])


@router.get("/status", summary="Describe local benchmark, quality, and safety-evaluation capabilities")
def benchmark_status(request: Request):
    return ResponseBuilder.success(
        request,
        answer="Phase 15 local benchmarking, quality metrics, and final safety evaluation status retrieved.",
        data=benchmark_service.status(),
    )


@router.get("/scenarios", summary="List approved benchmark scenarios and their safety boundaries")
def benchmark_scenarios(request: Request):
    scenarios = benchmark_service.supported_scenarios()
    return ResponseBuilder.success(
        request,
        answer="Approved local benchmark scenarios retrieved.",
        data={
            "phase": 15,
            "scenario_count": len(scenarios),
            "scenarios": scenarios,
            "raw_sql_accepted": False,
            "business_write_benchmarking_allowed": False,
        },
    )


@router.post("/run", summary="Run one approved local benchmark scenario and persist metric rows")
def run_benchmark(
    payload: BenchmarkRunRequest,
    request: Request,
    db: Session = Depends(get_db_session),
    _: UserRole = Depends(require_role(UserRole.ADMIN)),
):
    result = benchmark_service.run(db, scenario=payload.scenario, repeats=payload.repeats)
    return ResponseBuilder.success(
        request,
        answer="Local benchmark scenario completed and measurement rows were stored in benchmark_runs.",
        data=result,
        sources=[{"source_type": "database", "reference": "benchmark_runs", "detail": "Persisted local benchmark and quality metrics."}],
    )


@router.get("/runs", summary="List bounded persisted benchmark measurement rows")
def list_benchmark_runs(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    route: str | None = Query(default=None, max_length=64),
    metric_type: str | None = Query(default=None, max_length=128),
    db: Session = Depends(get_db_session),
    _: UserRole = Depends(require_role(UserRole.ADMIN)),
):
    runs = benchmark_service.list_runs(db, limit=limit, route=route, metric_type=metric_type)
    return ResponseBuilder.success(
        request,
        answer=f"Retrieved {len(runs)} bounded benchmark measurement row(s).",
        data={
            "phase": 15,
            "run_count": len(runs),
            "limit": limit,
            "filters": {"route": route, "metric_type": metric_type},
            "runs": runs,
            "raw_sql_accepted": False,
        },
        sources=[{"source_type": "database", "reference": "benchmark_runs", "detail": "Admin-only bounded benchmark history."}],
    )


@router.get("/summary", summary="Summarize local benchmark metrics with average, p50, p95, and success rate")
def benchmark_summary(
    request: Request,
    limit: int = Query(default=250, ge=1, le=500),
    db: Session = Depends(get_db_session),
    _: UserRole = Depends(require_role(UserRole.ADMIN)),
):
    result = benchmark_service.summary(db, limit=limit)
    return ResponseBuilder.success(
        request,
        answer="Local benchmark summary calculated from persisted measurement rows.",
        data=result,
        sources=[{"source_type": "database", "reference": "benchmark_runs", "detail": "Aggregated local benchmark metrics."}],
    )
