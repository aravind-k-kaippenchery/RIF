"""Document persistence diagnostics for the local POC."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.constants import AgentRoute
from app.core.response import ResponseBuilder
from app.db.session import get_db_session
from app.models.operations import Document
from app.services.document_service import document_service

router = APIRouter(prefix="/api/document-storage", tags=["Document storage diagnostics"])


@router.get("/status", summary="Check persistent document metadata and local files")
def document_storage_status(request: Request, db: Session = Depends(get_db_session)):
    upload_root = document_service.upload_root
    text_root = document_service.text_root
    total_documents = int(db.scalar(select(func.count()).select_from(Document)) or 0)
    recent_documents = list(
        db.scalars(select(Document).order_by(Document.created_at.desc()).limit(20)).all()
    )

    missing_text_count = 0
    document_summaries: list[dict[str, Any]] = []

    for document in recent_documents:
        text_path: Path | None = None
        text_exists = False
        if document.extracted_text_path:
            text_path = document_service.settings.resolved_upload_directory / document.extracted_text_path
            text_exists = text_path.exists()
            if not text_exists:
                missing_text_count += 1

        binary_dir = upload_root / str(document.id)
        binary_files = sorted(path.name for path in binary_dir.glob("*")) if binary_dir.exists() else []

        document_summaries.append(
            {
                "document_id": str(document.id),
                "original_filename": document.original_filename,
                "ingestion_status": document.ingestion_status,
                "created_at": document.created_at.isoformat(),
                "extracted_text_path": document.extracted_text_path,
                "extracted_text_exists": text_exists,
                "binary_file_count": len(binary_files),
                "binary_files": binary_files[:5],
            }
        )

    data = {
        "persistent_upload_directory": str(document_service.settings.resolved_upload_directory),
        "binary_document_directory": str(upload_root),
        "extracted_text_directory": str(text_root),
        "document_metadata_count": total_documents,
        "recent_document_count": len(document_summaries),
        "missing_extracted_text_count": missing_text_count,
        "recent_documents": document_summaries,
    }

    return ResponseBuilder.success(
        request,
        route=AgentRoute.DOCUMENT_RAG,
        answer=(
            f"Document storage check completed. PostgreSQL has {total_documents} document metadata row(s); "
            f"{missing_text_count} recent document(s) are missing extracted-text files."
        ),
        data=data,
    )
