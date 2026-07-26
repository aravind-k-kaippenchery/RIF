"""Local MCP client facade for FastAPI and LangGraph integration tests.

The application mounts a real Streamable HTTP MCP server at ``/mcp``. This small
in-process facade dispatches to the very same hardened implementations so internal
services and REST verification routes cannot bypass the MCP policy boundary.
"""

from __future__ import annotations

from typing import Any, Callable

from app.mcp import tools


_TOOL_CATALOG: list[dict[str, Any]] = [
    {
        "name": "get_schema",
        "description": "Return the controlled approved B2B schema contract.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_tables",
        "description": "List reflected public PostgreSQL tables and role-aware record-read capability. PostgreSQL system tables are never exposed.",
        "input_schema": {
            "type": "object",
            "properties": {"user_role": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "get_table_records",
        "description": "Read one bounded page from a reflected public PostgreSQL table without accepting raw SQL. Operational table records require admin role.",
        "input_schema": {
            "type": "object",
            "properties": {
                "table_name": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                "offset": {"type": "integer", "minimum": 0, "maximum": 500000},
                "user_role": {"type": "string"},
            },
            "required": ["table_name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "validate_sql",
        "description": "Validate one SQL proposal without executing a write.",
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string"}, "user_role": {"type": "string"}},
            "required": ["sql"],
            "additionalProperties": False,
        },
    },
    {
        "name": "execute_validated_read",
        "description": "Execute one validated SELECT statement only.",
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string"}},
            "required": ["sql"],
            "additionalProperties": False,
        },
    },
    {
        "name": "create_pending_action",
        "description": "Store a write preview without executing it.",
        "input_schema": {
            "type": "object",
            "properties": {"session_id": {"type": "string"}, "action_type": {"type": "string"}},
            "required": ["session_id", "action_type"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_pending_action",
        "description": "Read a session-scoped pending action.",
        "input_schema": {
            "type": "object",
            "properties": {"session_id": {"type": "string"}, "pending_action_id": {"type": "string"}},
            "required": ["session_id", "pending_action_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "propose_write_action",
        "description": "Create a duplicate-checked INSERT, UPDATE, or DELETE preview. It does not execute business writes.",
        "input_schema": {
            "type": "object",
            "properties": {"session_id": {"type": "string"}, "sql": {"type": "string"}, "user_role": {"type": "string"}},
            "required": ["session_id", "sql"],
            "additionalProperties": False,
        },
    },
    {
        "name": "execute_confirmed_write",
        "description": "Execute exactly one stored confirmation-gated pending action in a transaction.",
        "input_schema": {
            "type": "object",
            "properties": {"session_id": {"type": "string"}, "pending_action_id": {"type": "string"}, "request_id": {"type": "string"}, "user_role": {"type": "string"}},
            "required": ["session_id", "pending_action_id", "request_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "cancel_pending_action",
        "description": "Cancel one pending write without changing business records.",
        "input_schema": {
            "type": "object",
            "properties": {"session_id": {"type": "string"}, "pending_action_id": {"type": "string"}, "request_id": {"type": "string"}, "user_role": {"type": "string"}},
            "required": ["session_id", "pending_action_id", "request_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "retrieve_docs",
        "description": "Retrieve page-aware local document chunks by semantic similarity without calling a cloud service.",
        "input_schema": {
            "type": "object",
            "properties": {"question": {"type": "string"}, "top_k": {"type": "integer"}},
            "required": ["question"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_document_sources",
        "description": "Return filename/page/chunk source metadata for one local indexed document.",
        "input_schema": {
            "type": "object",
            "properties": {"document_id": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["document_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_verified_hybrid_evidence",
        "description": "Return vendor/product/quoted-price rows only when retrieved document chunks explicitly name the product.",
        "input_schema": {
            "type": "object",
            "properties": {"document_matches": {"type": "array"}, "max_price": {"type": ["number", "null"]}, "limit": {"type": "integer"}},
            "required": ["document_matches"],
            "additionalProperties": False,
        },
    },
    {
        "name": "create_schema_change_preview",
        "description": "Store an admin-only CREATE TABLE or ALTER TABLE ADD COLUMN preview. It never executes DDL.",
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string"}, "user_prompt": {"type": "string"}, "user_role": {"type": "string"}, "session_id": {"type": ["string", "null"]}},
            "required": ["sql", "user_prompt"],
            "additionalProperties": False,
        },
    },
    {
        "name": "apply_admin_schema_change",
        "description": "Execute one persisted admin-approved CREATE TABLE or ALTER TABLE ADD COLUMN request after explicit confirmation. It accepts no raw DDL.",
        "input_schema": {
            "type": "object",
            "properties": {"schema_change_id": {"type": "string"}, "confirmed": {"type": "boolean"}, "user_role": {"type": "string"}, "session_id": {"type": ["string", "null"]}, "request_id": {"type": ["string", "null"]}},
            "required": ["schema_change_id"],
            "additionalProperties": False,
        },
    },
]


class LocalMCPClient:
    """A small client facade that dispatches to the same functions exposed by FastMCP."""

    _handlers: dict[str, Callable[..., dict[str, Any]]] = {
        "get_schema": tools.get_schema_tool,
        "list_tables": tools.list_tables_tool,
        "get_table_records": tools.get_table_records_tool,
        "validate_sql": tools.validate_sql_tool,
        "execute_validated_read": tools.execute_validated_read_tool,
        "create_pending_action": tools.create_pending_action_tool,
        "get_pending_action": tools.get_pending_action_tool,
        "propose_write_action": tools.propose_write_action_tool,
        "execute_confirmed_write": tools.execute_confirmed_write_tool,
        "cancel_pending_action": tools.cancel_pending_action_tool,
        "retrieve_docs": tools.retrieve_docs_tool,
        "get_document_sources": tools.get_document_sources_tool,
        "get_verified_hybrid_evidence": tools.get_verified_hybrid_evidence_tool,
        "create_schema_change_preview": tools.create_schema_change_preview_tool,
        "apply_admin_schema_change": tools.apply_admin_schema_change_tool,
    }

    def list_tools(self) -> list[dict[str, Any]]:
        return _TOOL_CATALOG

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handler = self._handlers.get(name)
        if handler is None:
            return {
                "ok": False,
                "error_code": "mcp_tool_not_found",
                "error_message": f"MCP tool '{name}' is not registered.",
            }
        try:
            return {"ok": True, "result": handler(**arguments)}
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error_code": "mcp_tool_input_error", "error_message": str(exc)}
        except Exception as exc:  # pragma: no cover - defensive boundary for an MCP tool call
            return {"ok": False, "error_code": "mcp_tool_failed", "error_message": str(exc)}
