# Phase 11 — Verified Hybrid SQL + RAG Evidence Fusion

Phase 11 completes the `hybrid` agent route. It answers questions that need both uploaded-document evidence and live PostgreSQL records, for example:

```text
Which vendor offers textile automation products under 5 lakh according to the brochure?
```

## Controlled flow

```text
Question
  -> LangGraph selects hybrid
  -> ChromaDB retrieves page-aware document chunks
  -> restricted MCP tool verifies product identity against PostgreSQL
  -> MCP tool retrieves vendor/product/quoted-price rows
  -> deterministic answer is built only from verified evidence
  -> database and filename/page/chunk sources are returned
```

## Critical safety rule

A document chunk is linked to a database product only when that chunk explicitly contains the product name or product code. The system does not make fuzzy or guessed joins between unrelated document prose and database rows.

When any required evidence is missing, the route returns `information_not_available` rather than inventing a vendor, product, price, or capability.

## MCP tool added

```text
get_verified_hybrid_evidence(document_matches, max_price, limit)
```

This is a restricted read-only tool. It accepts retrieved document evidence plus a parsed price filter; it never accepts SQL and never performs a write.

## Expected demo proof

For the seeded Phase 2 data and Phase 8/9 TextileBot documents, the hybrid query should return:

```text
Neolotex Systems — TextileBot X — ₹475,000
Metro Fabric Solutions — TextileBot X — ₹490,000
```

Both entries have document evidence showing that TextileBot X supports textile automation, and the prices come from PostgreSQL `product_vendor_mappings`.
