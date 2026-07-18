"""Phase 9 local ChromaDB indexing, retrieval, and evidence-grounded RAG service.

Chroma persists only local vectors and chunk metadata under data/chroma. The
embedding model is loaded locally through SentenceTransformers and is never sent
an uploaded document to a cloud LLM or embedding API.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.constants import ResponseStatus, UserRole
from app.models.operations import ActionLog, Document
from app.services.llm_service import LLMServiceError, OllamaInvocationMetadata, llm_service


class DocumentRagError(Exception):
    """Expected, frontend-safe document retrieval or indexing problem."""

    def __init__(
        self,
        *,
        status: ResponseStatus,
        code: str,
        message: str,
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.details = details or []
        super().__init__(message)


@dataclass(frozen=True)
class DocumentChunk:
    """A page-aware chunk that is stored in Chroma with source metadata."""

    chunk_id: str
    document_id: UUID
    filename: str
    page_number: int
    chunk_index: int
    text: str

    def metadata(self) -> dict[str, Any]:
        return {
            "document_id": str(self.document_id),
            "filename": self.filename,
            "page_number": self.page_number,
            "chunk_index": self.chunk_index,
            "source_reference": f"{self.filename}#page={self.page_number}#chunk={self.chunk_index}",
        }


@dataclass(frozen=True)
class IndexResult:
    document_id: UUID
    filename: str
    chunk_count: int
    collection_name: str
    embedding_model: str
    indexed_at: str
    action_log_id: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": str(self.document_id),
            "filename": self.filename,
            "chunk_count": self.chunk_count,
            "collection_name": self.collection_name,
            "embedding_model": self.embedding_model,
            "indexed_at": self.indexed_at,
            "action_log_id": self.action_log_id,
        }


@dataclass(frozen=True)
class RetrievalMatch:
    chunk_id: str
    document_id: UUID
    filename: str
    page_number: int
    chunk_index: int
    text: str
    similarity: float

    @property
    def reference(self) -> str:
        return f"{self.filename}#page={self.page_number}#chunk={self.chunk_index}"

    def to_dict(self, *, preview_length: int = 500) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "document_id": str(self.document_id),
            "filename": self.filename,
            "page_number": self.page_number,
            "chunk_index": self.chunk_index,
            "similarity": round(self.similarity, 4),
            "text_preview": self.text[:preview_length],
        }


@dataclass(frozen=True)
class RagAnswerResult:
    question: str
    matches: list[RetrievalMatch]
    status: ResponseStatus
    answer: str
    source_references: list[str]
    model_metadata: OllamaInvocationMetadata | None


class DocumentRagService:
    """Own the Phase 9 local vector index and evidence-only answer path."""

    PAGE_MARKER = re.compile(r"(?m)^\[PAGE\s+(\d+)\]\s*\n")

    def __init__(self) -> None:
        self.settings = get_settings()
        self._embedding_model: Any | None = None
        self._chroma_client: Any | None = None
        self._collection: Any | None = None

    @staticmethod
    def _utc_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @property
    def embedding_model_loaded(self) -> bool:
        return self._embedding_model is not None

    def _get_chroma_client(self) -> Any:
        if self._chroma_client is not None:
            return self._chroma_client
        try:
            import chromadb  # type: ignore
        except ImportError as exc:  # pragma: no cover - host dependency only
            raise DocumentRagError(
                status=ResponseStatus.RETRIEVAL_FAILED,
                code="chromadb_not_installed",
                message="ChromaDB is not installed. Install the Phase 9 requirements, then retry.",
            ) from exc
        try:
            self._chroma_client = chromadb.PersistentClient(path=str(self.settings.resolved_chroma_persist_directory))
            return self._chroma_client
        except Exception as exc:  # pragma: no cover - host filesystem/version only
            raise DocumentRagError(
                status=ResponseStatus.RETRIEVAL_FAILED,
                code="chromadb_initialization_failed",
                message="The local ChromaDB store could not be initialized.",
            ) from exc

    def _get_collection(self) -> Any:
        if self._collection is not None:
            return self._collection
        client = self._get_chroma_client()
        try:
            self._collection = client.get_or_create_collection(
                name=self.settings.chroma_collection_name,
                metadata={"hnsw:space": "cosine"},
            )
            return self._collection
        except Exception as exc:  # pragma: no cover - host filesystem/version only
            raise DocumentRagError(
                status=ResponseStatus.RETRIEVAL_FAILED,
                code="chroma_collection_failed",
                message="The local document-chunk collection could not be opened.",
            ) from exc

    def _get_embedding_model(self) -> Any:
        if self._embedding_model is not None:
            return self._embedding_model
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
        except ImportError as exc:  # pragma: no cover - host dependency only
            raise DocumentRagError(
                status=ResponseStatus.RETRIEVAL_FAILED,
                code="embedding_library_not_installed",
                message="SentenceTransformers is not installed. Install the Phase 9 requirements, then retry.",
            ) from exc
        try:
            # The first run may download this public embedding model into the local
            # cache. Later runs reuse the cache and no documents leave the laptop.
            self._embedding_model = SentenceTransformer(
                self.settings.embedding_model_name,
                cache_folder=str(self.settings.resolved_embedding_cache_directory),
                device="cpu",
            )
            return self._embedding_model
        except Exception as exc:  # pragma: no cover - download/runtime varies by host
            raise DocumentRagError(
                status=ResponseStatus.RETRIEVAL_FAILED,
                code="embedding_model_unavailable",
                message=(
                    "The local embedding model could not be loaded. On the first run, ensure the laptop can download "
                    "the configured embedding model once; later runs use the local cache."
                ),
            ) from exc

    def _embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._get_embedding_model()
        try:
            embeddings = model.encode(
                texts,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
            return [list(map(float, row)) for row in embeddings]
        except Exception as exc:  # pragma: no cover - local ML runtime varies by host
            raise DocumentRagError(
                status=ResponseStatus.RETRIEVAL_FAILED,
                code="embedding_generation_failed",
                message="The local embedding model could not create document vectors.",
            ) from exc

    @classmethod
    def _page_sections(cls, text: str) -> list[tuple[int, str]]:
        """Preserve [PAGE n] markers emitted by Phase 8 PDF extraction."""

        matches = list(cls.PAGE_MARKER.finditer(text))
        if not matches:
            return [(1, text.strip())] if text.strip() else []
        sections: list[tuple[int, str]] = []
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            page_text = text[match.end() : end].strip()
            if page_text:
                sections.append((int(match.group(1)), page_text))
        return sections

    def _split_page(self, *, document: Document, page_number: int, text: str, starting_index: int) -> list[DocumentChunk]:
        """Split text near whitespace while retaining page and chunk provenance."""

        normalized = re.sub(r"[ \t]+", " ", text).strip()
        if not normalized:
            return []
        size = self.settings.rag_chunk_size_characters
        overlap = self.settings.rag_chunk_overlap_characters
        chunks: list[DocumentChunk] = []
        cursor = 0
        chunk_index = starting_index
        while cursor < len(normalized):
            target_end = min(cursor + size, len(normalized))
            end = target_end
            if target_end < len(normalized):
                boundary = normalized.rfind(" ", cursor + max(size // 2, 1), target_end)
                if boundary > cursor:
                    end = boundary
            piece = normalized[cursor:end].strip()
            if piece:
                chunks.append(
                    DocumentChunk(
                        chunk_id=f"{document.id}:v{document.document_version}:p{page_number}:c{chunk_index}",
                        document_id=document.id,
                        filename=document.original_filename,
                        page_number=page_number,
                        chunk_index=chunk_index,
                        text=piece,
                    )
                )
                chunk_index += 1
            if end >= len(normalized):
                break
            next_cursor = max(end - overlap, cursor + 1)
            cursor = next_cursor
        return chunks

    def build_chunks(self, document: Document, text: str) -> list[DocumentChunk]:
        chunks: list[DocumentChunk] = []
        next_index = 1
        for page_number, page_text in self._page_sections(text):
            page_chunks = self._split_page(
                document=document,
                page_number=page_number,
                text=page_text,
                starting_index=next_index,
            )
            chunks.extend(page_chunks)
            next_index += len(page_chunks)
        if not chunks:
            raise DocumentRagError(
                status=ResponseStatus.RETRIEVAL_FAILED,
                code="no_chunks_created",
                message="The extracted document text could not be split into searchable chunks.",
            )
        return chunks

    def _read_extracted_text(self, document: Document) -> str:
        if document.ingestion_status != "completed" or not document.extracted_text_path:
            raise DocumentRagError(
                status=ResponseStatus.RETRIEVAL_FAILED,
                code="document_not_ready_for_indexing",
                message="This document has no completed extracted text and cannot be indexed.",
            )
        path = self.settings.resolved_upload_directory / document.extracted_text_path
        if not path.exists():
            raise DocumentRagError(
                status=ResponseStatus.RETRIEVAL_FAILED,
                code="extracted_text_missing",
                message="The local extracted-text file is missing and cannot be indexed.",
            )
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            raise DocumentRagError(
                status=ResponseStatus.RETRIEVAL_FAILED,
                code="no_text_to_index",
                message="The document has no readable extracted text to index.",
            )
        return text

    @staticmethod
    def _action_log_request_id(*, request_id: str, document: Document, action_type: str) -> str:
        """Create one unique action-log ID per document event.

        The Phase 2 schema intentionally keeps ``action_logs.request_id`` unique.
        A bulk indexing HTTP request can create one audit event for several
        documents, so reusing the parent request ID for every row violates that
        constraint. The original HTTP request ID is preserved inside
        ``affected_record_ids.source_request_id`` for traceability.
        """
        parent_request_id = str(request_id or "unknown-request")
        event_request_id = f"{parent_request_id}:{action_type}:{document.id}"
        # The database column allows 128 characters. Normal middleware UUIDs are
        # 36 characters, but this defensive slice keeps custom request IDs safe.
        return event_request_id[:128]

    @classmethod
    def _write_action_log(
        cls,
        db: Session,
        *,
        request_id: str,
        document: Document,
        status: str,
        action_type: str,
        details: dict[str, Any],
        error_message: str | None = None,
    ) -> ActionLog:
        log = ActionLog(
            request_id=cls._action_log_request_id(
                request_id=request_id, document=document, action_type=action_type
            ),
            session_id=None,
            pending_action_id=None,
            actor_role=UserRole.NORMAL_USER.value,
            action_type=action_type,
            target_table="documents",
            affected_record_ids={
                "document_id": str(document.id),
                "source_request_id": str(request_id or "unknown-request"),
                **details,
            },
            generated_sql=None,
            confirmation_status="not_required",
            status=status,
            error_message=error_message,
            created_at=datetime.now(timezone.utc),
        )
        db.add(log)
        db.flush()
        return log

    def index_document(self, db: Session, *, document_id: UUID, request_id: str, force_reindex: bool = False) -> IndexResult:
        document = db.get(Document, document_id)
        if document is None:
            raise DocumentRagError(
                status=ResponseStatus.INFORMATION_NOT_AVAILABLE,
                code="document_not_found",
                message="No uploaded document exists with this ID.",
            )
        text = self._read_extracted_text(document)
        chunks = self.build_chunks(document, text)
        collection = self._get_collection()
        prefix = f"{document.id}:v{document.document_version}:"
        try:
            existing = collection.get(where={"document_id": str(document.id)}, include=[])
            existing_ids = list(existing.get("ids") or [])
            if existing_ids and not force_reindex:
                return IndexResult(
                    document_id=document.id,
                    filename=document.original_filename,
                    chunk_count=len(existing_ids),
                    collection_name=self.settings.chroma_collection_name,
                    embedding_model=self.settings.embedding_model_name,
                    indexed_at=self._utc_iso(),
                    action_log_id=None,
                )
            if existing_ids:
                collection.delete(ids=existing_ids)
            embeddings = self._embed([chunk.text for chunk in chunks])
            collection.upsert(
                ids=[chunk.chunk_id for chunk in chunks],
                documents=[chunk.text for chunk in chunks],
                metadatas=[chunk.metadata() for chunk in chunks],
                embeddings=embeddings,
            )
        except DocumentRagError:
            raise
        except Exception as exc:  # pragma: no cover - version/runtime dependent
            self._write_action_log(
                db,
                request_id=request_id,
                document=document,
                status="failed",
                action_type="document_vector_index",
                details={"chunk_count": len(chunks)},
                error_message=str(exc),
            )
            db.commit()
            raise DocumentRagError(
                status=ResponseStatus.RETRIEVAL_FAILED,
                code="document_indexing_failed",
                message="The local vector index could not store this document's chunks.",
            ) from exc

        log = self._write_action_log(
            db,
            request_id=request_id,
            document=document,
            status="completed",
            action_type="document_vector_index",
            details={"chunk_count": len(chunks), "collection": self.settings.chroma_collection_name, "prefix": prefix},
        )
        db.commit()
        return IndexResult(
            document_id=document.id,
            filename=document.original_filename,
            chunk_count=len(chunks),
            collection_name=self.settings.chroma_collection_name,
            embedding_model=self.settings.embedding_model_name,
            indexed_at=self._utc_iso(),
            action_log_id=log.id,
        )

    def index_completed_documents(self, db: Session, *, request_id: str, force_reindex: bool = False) -> dict[str, Any]:
        documents = list(
            db.scalars(
                select(Document)
                .where(Document.ingestion_status == "completed", Document.extracted_text_path.is_not(None))
                .order_by(desc(Document.created_at))
            ).all()
        )
        results: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for document in documents:
            try:
                results.append(self.index_document(db, document_id=document.id, request_id=request_id, force_reindex=force_reindex).to_dict())
            except DocumentRagError as exc:
                failures.append({"document_id": str(document.id), "filename": document.original_filename, "code": exc.code, "message": exc.message})
        return {"indexed_document_count": len(results), "failed_document_count": len(failures), "indexed_documents": results, "failures": failures}

    def get_document_chunks(self, *, document_id: UUID, limit: int = 100) -> list[dict[str, Any]]:
        collection = self._get_collection()
        try:
            payload = collection.get(where={"document_id": str(document_id)}, include=["documents", "metadatas"])
        except Exception as exc:  # pragma: no cover
            raise DocumentRagError(status=ResponseStatus.RETRIEVAL_FAILED, code="document_chunk_lookup_failed", message="Document chunk metadata could not be read.") from exc
        ids = list(payload.get("ids") or [])[:limit]
        documents = list(payload.get("documents") or [])[:limit]
        metadatas = list(payload.get("metadatas") or [])[:limit]
        output = []
        for chunk_id, text, metadata in zip(ids, documents, metadatas):
            metadata = metadata or {}
            output.append(
                {
                    "chunk_id": chunk_id,
                    "document_id": metadata.get("document_id"),
                    "filename": metadata.get("filename"),
                    "page_number": metadata.get("page_number", 1),
                    "chunk_index": metadata.get("chunk_index", 1),
                    "text_preview": (text or "")[:1000],
                }
            )
        return sorted(output, key=lambda item: (item["page_number"], item["chunk_index"]))

    def retrieve(self, question: str, *, top_k: int | None = None) -> list[RetrievalMatch]:
        collection = self._get_collection()
        try:
            count = int(collection.count())
        except Exception as exc:  # pragma: no cover
            raise DocumentRagError(status=ResponseStatus.RETRIEVAL_FAILED, code="chroma_count_failed", message="The local vector store could not be queried.") from exc
        if count <= 0:
            return []
        requested = top_k or self.settings.rag_top_k
        n_results = min(requested, count)
        query_embedding = self._embed([question])[0]
        try:
            payload = collection.query(
                query_embeddings=[query_embedding],
                n_results=n_results,
                include=["documents", "metadatas", "distances"],
            )
        except Exception as exc:  # pragma: no cover
            raise DocumentRagError(status=ResponseStatus.RETRIEVAL_FAILED, code="document_retrieval_failed", message="The local vector search could not retrieve document chunks.") from exc
        ids = (payload.get("ids") or [[]])[0] or []
        documents = (payload.get("documents") or [[]])[0] or []
        metadatas = (payload.get("metadatas") or [[]])[0] or []
        distances = (payload.get("distances") or [[]])[0] or []
        matches: list[RetrievalMatch] = []
        for chunk_id, text, metadata, distance in zip(ids, documents, metadatas, distances):
            metadata = metadata or {}
            try:
                similarity = max(0.0, min(1.0, 1.0 - float(distance)))
                document_id = UUID(str(metadata["document_id"]))
                page_number = int(metadata.get("page_number", 1))
                chunk_index = int(metadata.get("chunk_index", 1))
            except (KeyError, TypeError, ValueError):
                continue
            if similarity < self.settings.rag_min_similarity:
                continue
            matches.append(
                RetrievalMatch(
                    chunk_id=str(chunk_id),
                    document_id=document_id,
                    filename=str(metadata.get("filename", "uploaded_document")),
                    page_number=page_number,
                    chunk_index=chunk_index,
                    text=str(text or ""),
                    similarity=similarity,
                )
            )
        return matches

    def answer_question(self, question: str, *, top_k: int | None = None) -> RagAnswerResult:
        matches = self.retrieve(question, top_k=top_k)
        if not matches:
            return RagAnswerResult(
                question=question,
                matches=[],
                status=ResponseStatus.INFORMATION_NOT_AVAILABLE,
                answer="Information not available in the uploaded documents.",
                source_references=[],
                model_metadata=None,
            )
        evidence: list[dict[str, str]] = []
        consumed = 0
        for match in matches:
            if consumed >= self.settings.rag_max_evidence_characters:
                break
            remaining = self.settings.rag_max_evidence_characters - consumed
            content = match.text[:remaining]
            evidence.append({"source_type": "document", "reference": match.reference, "content": content})
            consumed += len(content)
        try:
            grounded, metadata = llm_service.generate_grounded_answer(question, evidence)
        except LLMServiceError as exc:
            raise DocumentRagError(status=ResponseStatus.LLM_UNAVAILABLE, code=exc.code, message=exc.message) from exc
        status = ResponseStatus.SUCCESS if grounded.supported else ResponseStatus.INFORMATION_NOT_AVAILABLE
        answer = grounded.answer if grounded.supported else "Information not available in the uploaded documents."
        return RagAnswerResult(
            question=question,
            matches=matches,
            status=status,
            answer=answer,
            source_references=grounded.source_references if grounded.supported else [],
            model_metadata=metadata,
        )

    def status(self) -> dict[str, Any]:
        try:
            collection = self._get_collection()
            count = int(collection.count())
            chromadb_available = True
        except DocumentRagError:
            count = 0
            chromadb_available = False
        return {
            "chromadb_available": chromadb_available,
            "collection_name": self.settings.chroma_collection_name,
            "indexed_chunk_count": count,
            "embedding_model": self.settings.embedding_model_name,
            "embedding_model_loaded": self.embedding_model_loaded,
            "local_only": True,
            "phase": 9,
            "notes": [
                "Document vectors and metadata are persisted locally under data/chroma.",
                "The embedding model runs locally; uploaded documents are not sent to a cloud embedding API.",
                "Grounded RAG answers are generated only from retrieved filename/page/chunk evidence.",
            ],
        }


document_rag_service = DocumentRagService()
