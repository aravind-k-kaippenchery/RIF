"""Phase 9 local vector indexing, retrieval, and grounded document-RAG endpoints."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session
from starlette.status import HTTP_400_BAD_REQUEST, HTTP_404_NOT_FOUND, HTTP_503_SERVICE_UNAVAILABLE

from app.core.constants import AgentRoute, ResponseStatus
from app.core.exceptions import AppError
from app.core.request_context import get_request_id
from app.core.response import ResponseBuilder
from app.db.session import get_db_session
from app.rag.document_rag_service import DocumentRagError, document_rag_service
from app.schemas.phase9 import RagIndexRequest, RagQueryRequest, RagRetrieveRequest

router = APIRouter(prefix="/api/rag", tags=["Document RAG"])


def _raise_rag_error(exc: DocumentRagError) -> None:
    if exc.status == ResponseStatus.INFORMATION_NOT_AVAILABLE:
        http_status = HTTP_404_NOT_FOUND
    elif exc.status in {ResponseStatus.LLM_UNAVAILABLE, ResponseStatus.RETRIEVAL_FAILED, ResponseStatus.TOOL_FAILED}:
        http_status = HTTP_503_SERVICE_UNAVAILABLE
    else:
        http_status = HTTP_400_BAD_REQUEST
    raise AppError(
        status=exc.status,
        code=exc.code,
        message=exc.message,
        http_status_code=http_status,
        route=AgentRoute.DOCUMENT_RAG,
        details=exc.details,
    ) from exc


@router.get("/status", summary="Check local ChromaDB and embedding configuration")
def rag_status(request: Request):
    payload = document_rag_service.status()
    return ResponseBuilder.success(
        request,
        route=AgentRoute.DOCUMENT_RAG,
        answer="Local document-RAG status retrieved. No document retrieval or LLM generation was requested.",
        data=payload,
    )


@router.post("/documents/{document_id}/index", summary="Chunk and index one completed local document in ChromaDB")
def index_document(document_id: UUID, payload: RagIndexRequest, request: Request, db: Session = Depends(get_db_session)):
    try:
        result = document_rag_service.index_document(
            db,
            document_id=document_id,
            request_id=get_request_id(request),
            force_reindex=payload.force_reindex,
        )
    except DocumentRagError as exc:
        _raise_rag_error(exc)
    return ResponseBuilder.success(
        request,
        route=AgentRoute.DOCUMENT_RAG,
        answer=f"Document indexed locally into {result.chunk_count} searchable chunk(s).",
        data={"index": result.to_dict()},
        sources=[
            {
                "source_type": "document",
                "reference": f"{result.filename}#indexed",
                "detail": f"Local ChromaDB index with {result.chunk_count} page-aware chunk(s).",
            }
        ],
    )


@router.post("/index-existing", summary="Index every completed uploaded document that has extracted text")
def index_existing_documents(payload: RagIndexRequest, request: Request, db: Session = Depends(get_db_session)):
    try:
        result = document_rag_service.index_completed_documents(
            db,
            request_id=get_request_id(request),
            force_reindex=payload.force_reindex,
        )
    except DocumentRagError as exc:
        _raise_rag_error(exc)
    status = ResponseStatus.SUCCESS if result["indexed_document_count"] else ResponseStatus.INFORMATION_NOT_AVAILABLE
    answer = (
        f"Indexed {result['indexed_document_count']} completed document(s) into the local vector store."
        if result["indexed_document_count"]
        else "No completed documents with extracted text are available for indexing."
    )
    return ResponseBuilder.success(request, route=AgentRoute.DOCUMENT_RAG, status=status, answer=answer, data=result)


@router.get("/documents/{document_id}/chunks", summary="View page-aware local vector chunks for one document")
def get_document_chunks(
    document_id: UUID,
    request: Request,
    limit: int = Query(default=100, ge=1, le=200),
):
    try:
        chunks = document_rag_service.get_document_chunks(document_id=document_id, limit=limit)
    except DocumentRagError as exc:
        _raise_rag_error(exc)
    status = ResponseStatus.SUCCESS if chunks else ResponseStatus.INFORMATION_NOT_AVAILABLE
    answer = f"Retrieved {len(chunks)} indexed chunk(s)." if chunks else "This document has not been indexed into local vector chunks yet."
    return ResponseBuilder.success(
        request,
        route=AgentRoute.DOCUMENT_RAG,
        status=status,
        answer=answer,
        data={"document_id": str(document_id), "chunk_count": len(chunks), "chunks": chunks},
    )


@router.post("/retrieve", summary="Retrieve local document chunks by semantic similarity without calling the LLM")
def retrieve_documents(payload: RagRetrieveRequest, request: Request):
    try:
        matches = document_rag_service.retrieve(payload.question, top_k=payload.top_k)
    except DocumentRagError as exc:
        _raise_rag_error(exc)
    status = ResponseStatus.SUCCESS if matches else ResponseStatus.INFORMATION_NOT_AVAILABLE
    answer = f"Retrieved {len(matches)} grounded document chunk(s)." if matches else "Information not available in the uploaded documents."
    return ResponseBuilder.success(
        request,
        route=AgentRoute.DOCUMENT_RAG,
        status=status,
        answer=answer,
        data={"question": payload.question, "match_count": len(matches), "matches": [match.to_dict() for match in matches], "llm_called": False},
        sources=[
            {"source_type": "document", "reference": match.reference, "detail": f"Similarity {match.similarity:.3f}; local ChromaDB retrieval."}
            for match in matches
        ],
    )


@router.post("/query", summary="Answer a document question using only retrieved local chunks")
def query_documents(payload: RagQueryRequest, request: Request):
    try:
        result = document_rag_service.answer_question(payload.question, top_k=payload.top_k)
    except DocumentRagError as exc:
        _raise_rag_error(exc)
    citations = [
        {
            "source_type": "document",
            "reference": match.reference,
            "detail": f"Local retrieval similarity {match.similarity:.3f}; filename/page/chunk source.",
        }
        for match in result.matches
        if not result.source_references or match.reference in result.source_references
    ]
    return ResponseBuilder.success(
        request,
        route=AgentRoute.DOCUMENT_RAG,
        status=result.status,
        answer=result.answer,
        data={
            "question": result.question,
            "retrieved_chunk_count": len(result.matches),
            "retrieved_chunks": [match.to_dict() for match in result.matches],
            "source_references": result.source_references,
            "model_metadata": result.model_metadata.model_dump() if result.model_metadata else {"model": None, "attempts": 0},
            "grounding_policy": "answer_from_retrieved_document_evidence_only",
        },
        sources=citations,
    )
