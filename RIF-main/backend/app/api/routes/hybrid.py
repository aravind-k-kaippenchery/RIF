"""Phase 11 status endpoint for verified hybrid evidence fusion."""

from fastapi import APIRouter, Request

from app.core.response import ResponseBuilder
from app.schemas.phase11 import HybridEvidenceStatus

router = APIRouter(prefix="/api/hybrid", tags=["Hybrid Evidence"])


@router.get("/status", summary="Check verified hybrid SQL-plus-document evidence fusion")
def hybrid_status(request: Request):
    return ResponseBuilder.success(
        request,
        route="hybrid",
        answer="Phase 11 verified hybrid evidence fusion status retrieved.",
        data=HybridEvidenceStatus(
            notes=[
                "Document chunks are linked to database products only through explicit product-name or product-code identity evidence.",
                "Vendor and quoted-price facts are retrieved through a restricted MCP tool using PostgreSQL read-only access.",
                "No cross-source conclusion is returned when product identity or requested database filters cannot be verified.",
            ]
        ).model_dump(),
    )
