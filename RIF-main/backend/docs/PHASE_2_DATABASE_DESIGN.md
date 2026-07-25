# Phase 2 — PostgreSQL Database Design

## Business tables

| Table | Purpose | Key duplicate rule |
|---|---|---|
| `employees` | Company employee records | `employee_code`, `email`, and non-null `phone` are unique |
| `employee_permissions` | Feature 17 child records | `employee_id + permission_code` is unique |
| `vendors` | Vendor/supplier records | `vendor_code`, `vendor_name`, email, and non-null phone are unique |
| `customers` | Customer records | `customer_code`, `customer_name`, email, and non-null phone are unique |
| `products` | Product catalog | `product_code` and `product_name` are unique |
| `product_vendor_mappings` | Links products and vendors | one mapping per `product_id + vendor_id` |
| `sales_deals` | Sales pipeline records | `deal_code` is unique |

## Operational tables

| Table | Created now because later phases depend on it |
|---|---|
| `sessions` | Short-term conversation/session state |
| `pending_actions` | Required to confirm INSERT/UPDATE/DELETE across two API calls |
| `query_logs` | Future prompt/query execution history |
| `action_logs` | Future mutation/confirmation history |
| `change_snapshots` | Future before/after rollback snapshots |
| `documents` | Uploaded document registry |
| `document_ingestion_jobs` | OCR/document processing state |
| `benchmark_runs` | Latency and quality measurements |
| `schema_change_requests` | Admin-only schema-change approval records |

## Phase 2 safety choices

- A local application role called `b2b_app` owns the POC database. The backend does not use the PostgreSQL `postgres` superuser.
- No API endpoint accepts free-form SQL in Phase 2.
- `employee_permissions.employee_id` uses `ON DELETE RESTRICT`.
- Migrations are committed as source code so every teammate creates the same schema.
- Seed data is deterministic and idempotent. Running it twice does not create duplicate business records.
