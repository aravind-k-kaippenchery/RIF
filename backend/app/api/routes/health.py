"""System endpoints: health, version, status, and temporary role-guard check."""

from fastapi import APIRouter, Depends, Request

from app.core.config import get_settings
from app.core.constants import UserRole
from app.core.response import ResponseBuilder
from app.core.schemas import HealthData, StatusData
from app.core.security import require_role
from app.db.health import get_database_health
from app.services.llm_service import llm_service

router = APIRouter(tags=["System"])


@router.get("/health", summary="Check whether the FastAPI service is running")
async def health_check(request: Request):
    settings = get_settings()
    return ResponseBuilder.success(
        request,
        answer="The API service is running.",
        data=HealthData(
            service=settings.app_name,
            environment=settings.app_env,
            version=settings.app_version,
        ).model_dump(),
    )


@router.get("/version", summary="Get the backend version")
async def version(request: Request):
    settings = get_settings()
    return ResponseBuilder.success(
        request,
        answer="Backend version information retrieved.",
        data={"version": settings.app_version},
    )


@router.get("/api/status", summary="View the current implementation status")
async def api_status(request: Request):
    database_health = get_database_health()
    ollama_health = llm_service.check_llm_health()
    rag_status = __import__("app.rag.document_rag_service", fromlist=["document_rag_service"]).document_rag_service.status()
    return ResponseBuilder.success(
        request,
        answer="Phase 16 final integration, reviewer-ready demo evidence, release readiness, and all prior controlled routes are active.",
        data=StatusData(
            phase=16,
            phase_name="Final integration, demonstration readiness, and release evidence",
            database_connected=database_health.connected,
            database_message=database_health.message,
            ollama_connected=ollama_health.connected and ollama_health.model_installed,
            chromadb_connected=bool(rag_status.get("chromadb_available", False)),
            available_routes=[
                "system",
                "database_verification",
                "schema_intelligence",
                "session_service",
                "sql_validation",
                "mcp_core",
                "llm_service",
                "structured_read",
                "crud_write",
                "document_upload_ocr",
                "document_rag",
                "agent_orchestration",
                "hybrid_evidence_fusion",
                "mcp_tool_hardening",
                "frontend_api_surface",
                "short_term_memory",
                "admin_schema_workflow",
                "audit_hardening",
                "rollback_workflow",
                "structured_error_verification",
                "benchmarking",
                "quality_metrics",
                "resource_sampling",
                "final_safety_evaluation",
                "final_demo_readiness",
                "release_evidence",
                "integration_smoke_check",
            ],
            parent_child_relationship=(
                "employees (parent) → employee_permissions (child): one employee can have zero, one, or many permissions."
            ),
        ).model_dump(),
    )


@router.get("/api/admin/phase1-check", summary="Verify the temporary admin role guard")
async def admin_phase1_check(
    request: Request,
    _: UserRole = Depends(require_role(UserRole.ADMIN)),
):
    return ResponseBuilder.success(
        request,
        answer="Admin role guard is working.",
        data={"role": UserRole.ADMIN.value, "note": "This is a temporary header-based guard until authentication is added."},
    )
