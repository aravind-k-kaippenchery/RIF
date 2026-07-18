"""Dedicated, reviewable Phase 5 prompt templates.

Prompts receive only controlled schema metadata and evidence supplied by the backend.
They never receive database credentials and they never instruct the model to call a
network, database, MCP tool, or file system directly.
"""

from __future__ import annotations

import json
from typing import Any


BASE_SYSTEM = """You are the local B2B Product Intelligence Assistant model adapter.
Return only JSON that matches the supplied schema. Do not include Markdown, prose outside
JSON, chain-of-thought, credentials, API keys, or tool calls. Do not claim to have queried
PostgreSQL, ChromaDB, files, or MCP directly. You may use only the controlled context supplied
in this request. When information is insufficient, state that clearly in the JSON fields."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def with_output_schema(system_prompt: str, json_schema: dict[str, Any]) -> str:
    """Append the Pydantic output schema in a predictable format."""

    return f"{BASE_SYSTEM}\n\n{system_prompt}\n\nRequired JSON schema:\n{_json(json_schema)}"


def render_intent_prompt(*, question: str, glossary: dict[str, Any], schema_summary: dict[str, Any]) -> str:
    return f"""Classify exactly one primary route for the user's question.

Allowed routes:
- structured_read: read-only question about rows, aggregates, filters, IDs, or tables.
- crud_write: a requested INSERT, UPDATE, or DELETE. This remains proposal-only.
- document_rag: a question whose answer would need an uploaded document.
- hybrid: a question requiring both structured records and document evidence.
- clarification: the request is too ambiguous to classify safely.

User question:
{question}

Resolved business glossary hints:
{_json(glossary)}

Controlled schema summary:
{_json(schema_summary)}

Do not generate SQL, do not call tools, and do not execute anything."""

def render_sql_prompt(
    *,
    question: str,
    route: str,
    glossary: dict[str, Any],
    schema_contract: dict[str, Any],
    memory_context: dict[str, Any] | None = None,
) -> str:
    return f"""Generate one PostgreSQL SQL proposal for a future safety validator.

Requested route: {route}

User question:
{question}

Resolved business glossary hints:
{_json(glossary)}

Bounded short-term session context:
{_json(memory_context or {"available": False, "events": []})}

Controlled schema contract:
{_json(schema_contract)}

Mandatory SQL rules:
- Use only tables, columns, and relationships present in the controlled schema contract.
- Generate exactly one SQL statement.
- Never use SELECT *.
- Never use table.* or alias.*.
- COUNT(*) is the only permitted star expression.
- For structured_read, generate SELECT only.
- For structured_read, use explicit column names in every SELECT projection.
- Always include LIMIT 100 for structured reads unless the user explicitly asks for a lower limit.
- Preserve every explicit user filter exactly.
- If the user asks for employees from, based in, or living in a city, use:
  WHERE city = '<requested city>'
- Never replace the user's requested filter with a default filter such as:
  employment_status = 'active'
- For crud_write, generate INSERT, UPDATE, or DELETE only.
- UPDATE and DELETE must include a WHERE clause.
- Never generate DROP, TRUNCATE, GRANT, REVOKE, comments, CREATE, ALTER, schema-qualified names, or multiple statements.
- This is only a SQL proposal. Do not claim that it was executed.
- The explanation must be short and user-facing. Do not provide hidden reasoning.

Safe default projections:

For employees, prefer:
employee_code, first_name, last_name, department, city, company_name, salary, employment_status

For vendors, prefer:
vendor_code, vendor_name, city, email, phone, company_name

For customers, prefer:
customer_code, customer_name, city, email, phone, company_name

For products, prefer:
product_code, product_name, category, price, vendor_id

Example question:
Show employees from Bangalore

Correct SQL:
SELECT employee_code, first_name, last_name, department, city, company_name, salary, employment_status
FROM employees
WHERE city = 'Bangalore'
LIMIT 100

Incorrect SQL:
SELECT * FROM employees WHERE city = 'Bangalore'

Incorrect SQL:
SELECT employees.* FROM employees WHERE city = 'Bangalore'
"""


def render_record_extraction_prompt(*, instruction: str, target_table: str, table_schema: dict[str, Any]) -> str:
    return f"""Extract a proposed record from the user's instruction for one approved table.

User instruction: {instruction}
Target table: {target_table}
Controlled target-table schema:
{_json(table_schema)}

Rules:
- table_name must equal the target table exactly.
- values may contain only approved non-managed columns from the target schema.
- Never include id, created_at, or updated_at.
- Do not invent missing values. List missing fields and set requires_clarification true when necessary.
- This is a proposal only. Do not insert, update, or delete data."""


def render_grounded_answer_prompt(*, question: str, evidence: list[dict[str, str]]) -> str:
    return f"""Answer only from the supplied evidence records.

User question: {question}
Evidence records:
{_json(evidence)}

Rules:
- Do not use general knowledge or infer facts that are absent from the evidence.
- When the evidence cannot support an answer, set supported=false and use the answer exactly:
  'Information not available in the provided evidence.'
- Every source_references value must exactly match one supplied evidence reference.
- When supported=true, provide at least one source reference.
- Keep the answer concise and factual."""


def render_admin_schema_prompt(*, user_prompt: str, allowed_tables: list[str]) -> str:
    """Render a deliberately narrow admin DDL proposal prompt.

    This does not grant the model DDL execution.  Python validates the output and stores
    it as a preview for explicit admin confirmation.
    """

    return f"""Generate one restricted PostgreSQL admin schema proposal.

Admin request:
{user_prompt}

Approved existing application tables:
{_json(allowed_tables)}

Only two operations are permitted:
- CREATE TABLE with explicit simple columns
- ALTER TABLE <approved existing table> ADD COLUMN <simple column>

Strict rules:
- Generate exactly one statement.
- Never generate DROP, TRUNCATE, ALTER DROP, GRANT, REVOKE, comments, schema-qualified names, indexes, constraints, foreign keys, or multiple statements.
- Permitted types: BIGINT, BOOLEAN, DATE, INT, INTEGER, NUMERIC, TEXT, TIMESTAMP, TIMESTAMPTZ, UUID, VARCHAR.
- operation must be exactly create_table or alter_table_add_column.
- This is a preview only. Do not claim it was executed.
"""
