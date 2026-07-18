# Phase 9 — Local ChromaDB, Embeddings, and Grounded Document RAG

Phase 9 turns Phase 8 extracted text into searchable local knowledge.

## Local pipeline

```text
Phase 8 extracted text
  → preserve PDF page markers
  → split into page-aware chunks
  → encode chunks using sentence-transformers/all-MiniLM-L6-v2 on CPU
  → persist embeddings + metadata in local ChromaDB
  → retrieve top matching chunks
  → local Llama 3 answers from retrieved evidence only
  → return filename, page, and chunk sources
```

## Local data boundaries

- Uploaded source files remain in `uploads/`.
- Extracted text remains in `uploads/extracted_text/`.
- ChromaDB data remains in `data/chroma/`.
- The embedding-model cache remains in `data/embedding_models/`.
- These directories are Git-ignored.
- On first embedding use, the sentence-transformer model may download once into the local cache. After it is cached, document text and embeddings remain local.

## API surface

- `GET /api/rag/status`
- `POST /api/rag/index-existing`
- `POST /api/rag/documents/{document_id}/index`
- `GET /api/rag/documents/{document_id}/chunks`
- `POST /api/rag/retrieve`
- `POST /api/rag/query`

## Grounding rule

`POST /api/rag/query` never answers from general model knowledge. When retrieval returns no sufficiently relevant chunk, it returns:

```text
Information not available in the uploaded documents.
```

A supported answer includes source references such as:

```text
Neolotex_Brochure.pdf#page=2#chunk=3
```
