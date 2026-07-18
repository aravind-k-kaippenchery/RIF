"""Phase 8 upload, OCR, and document-viewer APIs."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from sqlalchemy.orm import Session
from starlette.status import HTTP_400_BAD_REQUEST, HTTP_404_NOT_FOUND, HTTP_409_CONFLICT, HTTP_503_SERVICE_UNAVAILABLE

from app.core.constants import AgentRoute, ResponseStatus, UserRole
from app.core.exceptions import AppError
from app.core.request_context import get_request_id
from app.core.response import ResponseBuilder
from app.core.security import get_current_role
from app.db.session import get_db_session
from app.services.document_service import DocumentServiceError, document_service

router = APIRouter(prefix="/api/documents", tags=["Document upload and OCR"])


def _raise_document_error(exc: DocumentServiceError) -> None:
    if exc.status == ResponseStatus.DUPLICATE_DETECTED:
        status_code = HTTP_409_CONFLICT
    elif exc.status in {ResponseStatus.OCR_FAILED, ResponseStatus.TOOL_FAILED}:
        status_code = HTTP_503_SERVICE_UNAVAILABLE
    else:
        status_code = HTTP_400_BAD_REQUEST
    raise AppError(
        status=exc.status,
        code=exc.code,
        message=exc.message,
        http_status_code=status_code,
        details=exc.details,
    ) from exc


@router.post("/upload", summary="Upload one PDF, DOCX, TXT, JPG, or PNG file and ingest its local text")
async def upload_document(
    request: Request,
    file: UploadFile = File(..., description="PDF, DOCX, TXT, JPG, or PNG document"),
    db: Session = Depends(get_db_session),
    role: UserRole = Depends(get_current_role),
):
    # Read once so extension, MIME hint, file signature, hash, and max-size policy are
    # evaluated before any file is persisted.
    content = await file.read()
    try:
        result = document_service.ingest_upload(
            db,
            filename=file.filename or "upload",
            content_type=file.content_type,
            content=content,
            request_id=get_request_id(request),
            actor_role=role,
        )
    except DocumentServiceError as exc:
        _raise_document_error(exc)

    document = document_service._as_summary(result.document)
    extraction = {
        "method": result.extraction.extraction_method,
        "ocr_used": result.extraction.ocr_used,
        "page_count": result.extraction.page_count,
        "ocr_duration_ms": result.extraction.ocr_duration_ms,
        "extracted_character_count": len(result.extraction.text),
        "text_preview": result.extraction.text[:1000],
    }
    return ResponseBuilder.success(
        request,
        route=AgentRoute.DOCUMENT_RAG,
        answer="Document uploaded and ingested locally. It is ready for local vector indexing through the Phase 9 RAG endpoints; no cloud service was used.",
        data={
            "document": document,
            "ingestion_job": {
                "ingestion_job_id": str(result.job.id),
                "status": result.job.status,
                "ocr_duration_ms": result.job.ocr_duration_ms,
            },
            "extraction": extraction,
            "action_log_id": result.action_log_id,
            "rag_indexed": False,
        },
        sources=[
            {
                "source_type": "document",
                "reference": document["original_filename"],
                "detail": "Locally uploaded document. Page-aware chunk citations are added in Phase 9.",
            }
        ],
    )


@router.get("", summary="List uploaded documents for the frontend document viewer")
def list_documents(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db_session),
):
    documents = document_service.list_documents(db, limit=limit)
    return ResponseBuilder.success(
        request,
        route=AgentRoute.DOCUMENT_RAG,
        answer=f"Retrieved {len(documents)} uploaded document(s).",
        data={"document_count": len(documents), "documents": documents},
    )


@router.get("/{document_id}", summary="Get metadata for one uploaded document")
def get_document(document_id: UUID, request: Request, db: Session = Depends(get_db_session)):
    document = document_service.get_document(db, document_id)
    if document is None:
        raise AppError(
            status=ResponseStatus.INFORMATION_NOT_AVAILABLE,
            code="document_not_found",
            message="No uploaded document exists with this ID.",
            http_status_code=HTTP_404_NOT_FOUND,
        )
    return ResponseBuilder.success(
        request,
        route=AgentRoute.DOCUMENT_RAG,
        answer="Uploaded document metadata retrieved.",
        data={"document": document, "rag_indexed": False},
        sources=[{"source_type": "document", "reference": document["original_filename"], "detail": "Uploaded document metadata."}],
    )


@router.get("/{document_id}/extracted-text", summary="View extracted text from one uploaded document")
def get_extracted_text(document_id: UUID, request: Request, db: Session = Depends(get_db_session)):
    payload = document_service.get_extracted_text(db, document_id)
    if payload is None:
        raise AppError(
            status=ResponseStatus.INFORMATION_NOT_AVAILABLE,
            code="document_not_found",
            message="No uploaded document exists with this ID.",
            http_status_code=HTTP_404_NOT_FOUND,
        )
    if not payload["available"]:
        raise AppError(
            status=ResponseStatus.OCR_FAILED,
            code="extracted_text_unavailable",
            message="Extracted text is not available for this document. Check its ingestion status.",
            http_status_code=HTTP_503_SERVICE_UNAVAILABLE,
        )
    document = payload["document"]
    return ResponseBuilder.success(
        request,
        route=AgentRoute.DOCUMENT_RAG,
        answer="Locally extracted document text retrieved.",
        data=payload,
        sources=[{"source_type": "document", "reference": document["original_filename"], "detail": "Local extracted text; no RAG retrieval has occurred yet."}],
    )
