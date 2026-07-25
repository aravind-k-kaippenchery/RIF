# Phase 8 — Document Upload, OCR, and Immediate Smoke Tests

Phase 8 adds local ingestion for `PDF`, `DOCX`, `TXT`, `JPG/JPEG`, and `PNG` files.

## Safety and storage rules

- The backend validates extension, MIME hint, binary file signature, and file size before writing a file.
- Each document is hashed with SHA-256. Uploading the exact same file again returns `duplicate_document`; no second copy is stored.
- Uploads are stored below `uploads/documents/<document-id>/`.
- Extracted text is stored below `uploads/extracted_text/<document-id>.txt`.
- `documents`, `document_ingestion_jobs`, and `action_logs` record the ingestion result.
- Uploaded files and extracted text remain ignored by Git through the existing `uploads/*` rule.

## Extraction behavior

| File type | Local extraction path |
|---|---|
| TXT | UTF-8 native extraction with a Latin-1 fallback |
| DOCX | Paragraph and top-level table extraction via `python-docx` |
| Text PDF | Native page text via PyMuPDF |
| Scanned PDF | Page render via PyMuPDF, then local PaddleOCR |
| JPG / PNG | Local PaddleOCR |

The first PaddleOCR request may take longer because its local model assets must be present on the laptop. Once downloaded, OCR runs locally and no cloud OCR API is used.

## APIs

- `POST /api/documents/upload` — multipart form field: `file`
- `GET /api/documents` — uploaded-document list
- `GET /api/documents/{document_id}` — metadata
- `GET /api/documents/{document_id}/extracted-text` — extracted local text preview

Phase 8 does **not** create embeddings, use ChromaDB, or answer document questions. That begins in Phase 9.

## Smoke-test set

Use one item of each type:

1. Native-text PDF
2. Scanned PDF
3. DOCX
4. JPG or PNG containing readable text
5. TXT

For each successful upload confirm:

- `ingestion_status = completed`
- `extracted_character_count > 0`
- `ocr_used = false` for native TXT/DOCX/text-PDF
- `ocr_used = true` for scanned PDF/image
- `GET /api/documents/{id}/extracted-text` returns text

For a deliberately invalid extension or mismatched signature, confirm the response is `validation_failed`. For missing OCR runtime or unreadable scans, confirm `ocr_failed` rather than a traceback.
