# P0 live PostgreSQL reflection changes

This build keeps the existing demo-specific workflows while making generic table
operations use the live PostgreSQL `public` schema as their source of truth.

## Changed for deployment

- Ask/read/schema prompts resolve reflected non-operational tables, including a real
  `orders` or `failed_orders` table created outside the original ORM models.
- Simple equality questions such as `show vendors where city is Chennai` validate the
  table and column against live reflection and execute a bounded, parameterized read.
  Unknown filter columns request clarification instead of returning unfiltered rows.
- Natural value/column prompts such as `show products from Textile Automation category`
  and `show employees from Sales department` derive the filter column from the live
  table schema instead of using product-, employee-, or category-specific lists.
- Deterministic reflected reads now persist compact follow-up state (table, applied
  filters, and row count only). Referential prompts such as `show only the active ones`
  reuse those filters in the same session without storing business rows or requiring
  generated SQL.
- Conversational table memory has no employee/vendor/product fallback. Validated SQL
  reads are reduced through a SQL AST to one live reflected table and safe literal
  equality filters. Joins, ranges, OR conditions, missing columns, and ambiguous status
  columns require clarification instead of being replayed incompletely.
- Direct relationship prompts such as `show permissions for employee EMP-001` resolve
  both tables, the child foreign key, the parent referenced key, and the parent code
  column from live reflection. The bounded MCP reader constructs the join internally;
  prompts cannot provide raw JOIN or SQL text. The same path supports any unambiguous
  direct reflected parent-child relationship.
- The old `orders` → `sales_deals` demo alias runs only when a real `orders` table does
  not exist.
- LLM SQL context contains live business tables, columns, primary keys, and foreign keys.
- Generic confirmation-gated CRUD accepts reflected business tables and validates
  required/database-generated fields from their real columns.
- Duplicate checks use reflected primary keys and unique constraints. The frontend
  Duplicate Center gets its table/key list from `/api/tables` and calls the read-only
  `/api/crud/check-duplicates` endpoint.
- Update snapshots, delete protection, and rollback use real single or composite primary
  keys instead of assuming every table has an `id` column.
- Faker remains schema-driven for every reflected non-operational table and still uses
  specialized profiles for the original demo tables.

## Intentionally retained

- Operational/audit tables remain blocked from normal-user reads and all business writes.
- Writes still require preview plus explicit admin confirmation.
- PostgreSQL constraints remain the final concurrency-safe duplicate protection.
- Employee, vendor, customer, product, sales-deal, and parent/child demo aliases remain
  as compatibility conveniences; they no longer limit generic tables.
- Destructive DDL, unrestricted UPDATE/DELETE, raw multi-statement SQL, and credential
  disclosure protections remain unchanged.

## Quick verification after restart

1. `Count every row in the orders table.`
2. `Show failed orders table data.`
3. `What columns are in failed_orders?`
4. `Generate 5 synthetic records for failed_orders.`
5. Open **Duplicate Center** and confirm reflected tables/unique constraints appear.
6. `Show vendors where city is Chennai.` and verify every returned row has that city.
7. `Show products from Textile Automation category.` and verify every returned row has
   that reflected category value.
8. In the same chat, run `Show employees from Bangalore.` followed by
   `Show only the active ones.` and verify both city and live status filters are applied.
9. `Show permissions for employee EMP-001.` and verify only child permission rows linked
   through the reflected employee foreign key are returned.

If PostgreSQL does not contain `orders`, prompt 1 may intentionally use the legacy
`sales_deals` compatibility alias. Restart the backend after replacing the files so all
process-local reflection caches start clean.
