"""Phase 8 deterministic document-validation and API contract tests."""

from __future__ import annotations

import io
import zipfile
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.constants import ResponseStatus, UserRole
from app.main import create_app
from app.ocr.paddle_ocr import PaddleOcrAdapter
from app.services.document_service import DocumentIngestionResult, DocumentService, DocumentServiceError, ExtractedDocument

client = TestClient(create_app())


def _docx_bytes() -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types></Types>")
        archive.writestr("word/document.xml", "<w:document></w:document>")
    return stream.getvalue()


def _document_summary(filename: str = "brochure.txt"):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=uuid4(),
        original_filename=filename,
        file_type="txt",
        file_size_bytes=31,
        page_count=1,
        ocr_used=False,
        ingestion_status="completed",
        extracted_text_path="extracted_text/demo.txt",
        document_version=1,
        error_message=None,
        created_at=now,
        updated_at=now,
    )


def test_phase_eight_status_exposes_document_upload_route():
    response = client.get("/api/status")
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["phase"] >= 8
    assert "document_upload_ocr" in body["data"]["available_routes"]


def test_txt_upload_validation_accepts_utf8_text():
    filename, document_type = DocumentService.validate_upload(
        filename="notes.txt", content_type="text/plain", content=b"Neolotex product brochure"
    )
    assert filename == "notes.txt"
    assert document_type == "txt"


def test_pdf_upload_validation_blocks_wrong_signature():
    with pytest.raises(DocumentServiceError) as exc_info:
        DocumentService.validate_upload(filename="not-a-pdf.pdf", content_type="application/pdf", content=b"plain text")
    assert exc_info.value.code == "file_signature_mismatch"


def test_image_upload_validation_blocks_wrong_extension():
    with pytest.raises(DocumentServiceError) as exc_info:
        DocumentService.validate_upload(filename="image.exe", content_type="application/octet-stream", content=b"MZ")
    assert exc_info.value.code == "unsupported_file_type"


def test_docx_upload_validation_checks_zip_contents():
    filename, document_type = DocumentService.validate_upload(
        filename="spec.docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        content=_docx_bytes(),
    )
    assert filename == "spec.docx"
    assert document_type == "docx"


def test_duplicate_document_is_blocked_before_file_is_saved():
    service = DocumentService()
    db = MagicMock()
    existing = _document_summary("existing.txt")
    db.scalar.return_value = existing
    with pytest.raises(DocumentServiceError) as exc_info:
        service.ingest_upload(
            db,
            filename="new.txt",
            content_type="text/plain",
            content=b"same document",
            request_id="request-doc-duplicate",
            actor_role=UserRole.NORMAL_USER,
        )
    assert exc_info.value.status == ResponseStatus.DUPLICATE_DETECTED
    assert exc_info.value.code == "duplicate_document"


def test_txt_extraction_reads_local_content(tmp_path):
    path = tmp_path / "catalog.txt"
    path.write_text("Textile automation product details", encoding="utf-8")
    result = DocumentService._extract_txt(path)
    assert result.ocr_used is False
    assert result.extraction_method == "native_txt"
    assert "Textile automation" in result.text


def test_paddle_adapter_collects_legacy_line_result():
    legacy = [[[[0, 0], [1, 0]], ("Machine warranty: 24 months", 0.99)]]
    text = PaddleOcrAdapter._collect_text(legacy)
    assert text == ["Machine warranty: 24 months"]


def test_paddle_adapter_collects_v3_rec_texts_result():
    payload = {"rec_texts": ["TextileBot X", "Warranty 24 months"], "rec_scores": [0.9, 0.9]}
    text = PaddleOcrAdapter._collect_text(payload)
    assert text == ["TextileBot X", "Warranty 24 months"]


def test_document_list_endpoint_returns_frontend_safe_metadata():
    document = _document_summary()
    with patch("app.api.routes.documents.document_service.list_documents", return_value=[DocumentService._as_summary(document)]):
        response = client.get("/api/documents")
    assert response.status_code == 200
    body = response.json()
    assert body["route"] == "document_rag"
    assert body["data"]["document_count"] == 1
    assert body["data"]["documents"][0]["original_filename"] == "brochure.txt"


def test_document_upload_endpoint_returns_ingestion_metadata_without_cloud_rag():
    document = _document_summary("product.txt")
    job = SimpleNamespace(id=uuid4(), status="completed", ocr_duration_ms=None)
    result = DocumentIngestionResult(
        document=document,
        job=job,
        extraction=ExtractedDocument(
            text="Product X supports textile automation.",
            page_count=1,
            ocr_used=False,
            extraction_method="native_txt",
            ocr_duration_ms=None,
        ),
        action_log_id=101,
    )
    with patch("app.api.routes.documents.document_service.ingest_upload", return_value=result):
        response = client.post(
            "/api/documents/upload",
            files={"file": ("product.txt", b"Product X supports textile automation.", "text/plain")},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["route"] == "document_rag"
    assert body["data"]["rag_indexed"] is False
    assert body["data"]["extraction"]["ocr_used"] is False


def test_document_upload_endpoint_maps_ocr_failure_to_safe_response():
    error = DocumentServiceError(
        status=ResponseStatus.OCR_FAILED,
        code="paddleocr_not_installed",
        message="PaddleOCR is required for scanned file OCR.",
    )
    with patch("app.api.routes.documents.document_service.ingest_upload", side_effect=error):
        response = client.post(
            "/api/documents/upload",
            files={"file": ("scan.png", b"\x89PNG\r\n\x1a\n", "image/png")},
        )
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "ocr_failed"
    assert body["error"]["code"] == "paddleocr_not_installed"


def test_extracted_text_endpoint_returns_document_source():
    document = _document_summary("catalog.txt")
    payload = {
        "document": DocumentService._as_summary(document),
        "text": "Local catalog content",
        "truncated": False,
        "available": True,
        "character_count": 21,
    }
    with patch("app.api.routes.documents.document_service.get_extracted_text", return_value=payload):
        response = client.get(f"/api/documents/{document.id}/extracted-text")
    assert response.status_code == 200
    body = response.json()
    assert body["sources"][0]["reference"] == "catalog.txt"
    assert body["data"]["text"] == "Local catalog content"
