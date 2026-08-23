"""Actual Phase 4 MCP server mounted in the FastAPI application at /mcp."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from app.core.config import get_settings
from app.mcp.tools import (
    create_pending_action_tool,
    execute_validated_read_tool,
    get_pending_action_tool,
    get_schema_tool,
    propose_write_action_tool,
    execute_confirmed_write_tool,
    cancel_pending_action_tool,
    validate_sql_tool,
    retrieve_docs_tool,
    get_document_sources_tool,
    get_verified_hybrid_evidence_tool,
    list_tables_tool,
    get_table_records_tool,
    create_schema_change_preview_tool,
    apply_admin_schema_change_tool,
)


mcp_server = FastMCP(
    get_settings().mcp_server_name,
    stateless_http=True,
    json_response=True,
    streamable_http_path="/",
)


@mcp_server.tool()
def get_schema() -> dict[str, Any]:
    """Get the approved B2B PostgreSQL schema, columns, and relationships."""

    return get_schema_tool()


@mcp_server.tool()
def validate_sql(sql: str, user_role: str = "normal_user") -> dict[str, Any]:
    """Validate one safe SQL proposal. Normal users allow SELECT/INSERT/UPDATE/DELETE proposals only."""

    return validate_sql_tool(sql=sql, user_role=user_role)


@mcp_server.tool()
def execute_validated_read(sql: str) -> dict[str, Any]:
    """Execute one validated SELECT statement. It cannot execute writes or unrestricted SQL."""

    return execute_validated_read_tool(sql=sql)


@mcp_server.tool()
def create_pending_action(
    session_id: str,
    action_type: str,
    target_table: str | None,
    validated_payload: dict[str, Any],
    preview_data: dict[str, Any],
    generated_sql: str | None = None,
    ttl_minutes: int = 30,
) -> dict[str, Any]:
    """Store a safe write preview for later user confirmation. No write is executed."""

    return create_pending_action_tool(
        session_id=session_id,
        action_type=action_type,
        target_table=target_table,
        validated_payload=validated_payload,
        preview_data=preview_data,
        generated_sql=generated_sql,
        ttl_minutes=ttl_minutes,
    )


@mcp_server.tool()
def get_pending_action(session_id: str, pending_action_id: str) -> dict[str, Any]:
    """Read a pending confirmation preview scoped to a specific session."""

    return get_pending_action_tool(session_id=session_id, pending_action_id=pending_action_id)



@mcp_server.tool()
def propose_write_action(
    session_id: str,
    sql: str,
    user_role: str = "normal_user",
    user_prompt: str | None = None,
    ttl_minutes: int = 30,
) -> dict[str, Any]:
    """Create a duplicate-checked preview for one INSERT, UPDATE, or DELETE. It cannot execute immediately."""

    return propose_write_action_tool(
        session_id=session_id,
        sql=sql,
        user_role=user_role,
        user_prompt=user_prompt,
        ttl_minutes=ttl_minutes,
    )


@mcp_server.tool()
def execute_confirmed_write(
    session_id: str,
    pending_action_id: str,
    request_id: str,
    user_role: str = "normal_user",
) -> dict[str, Any]:
    """Execute exactly one previously previewed action after explicit confirmation."""

    return execute_confirmed_write_tool(
        session_id=session_id,
        pending_action_id=pending_action_id,
        request_id=request_id,
        user_role=user_role,
    )


@mcp_server.tool()
def cancel_pending_action(
    session_id: str,
    pending_action_id: str,
    request_id: str,
    user_role: str = "normal_user",
) -> dict[str, Any]:
    """Cancel one pending write action. It does not change business data."""

    return cancel_pending_action_tool(
        session_id=session_id,
        pending_action_id=pending_action_id,
        request_id=request_id,
        user_role=user_role,
    )


@mcp_server.tool()
def retrieve_docs(question: str, top_k: int = 4) -> dict[str, Any]:
    """Retrieve local document chunks by semantic similarity. It never sends documents to a cloud API."""

    return retrieve_docs_tool(question=question, top_k=top_k)


@mcp_server.tool()
def get_document_sources(document_id: str, limit: int = 100) -> dict[str, Any]:
    """Return page-aware source chunks for one locally indexed document."""

    return get_document_sources_tool(document_id=document_id, limit=limit)


@mcp_server.tool()
def get_verified_hybrid_evidence(
    document_matches: list[dict[str, Any]],
    max_price: float | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Fuse document evidence with active vendor/product price rows using explicit product identity matching only."""

    return get_verified_hybrid_evidence_tool(
        document_matches=document_matches,
        max_price=max_price,
        limit=limit,
    )



@mcp_server.tool()
def list_tables(user_role: str = "normal_user") -> dict[str, Any]:
    """List approved B2B application tables and role-aware record-read capability."""

    return list_tables_tool(user_role=user_role)


@mcp_server.tool()
def get_table_records(
    table_name: str,
    limit: int = 50,
    offset: int = 0,
    user_role: str = "normal_user",
    filters: dict[str, Any] | None = None,
    relationship_filter: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read one bounded page from an approved table. Raw SQL is not accepted."""

    return get_table_records_tool(
        table_name=table_name,
        limit=limit,
        offset=offset,
        user_role=user_role,
        filters=filters,
        relationship_filter=relationship_filter,
    )


@mcp_server.tool()
def create_schema_change_preview(
    sql: str,
    user_prompt: str,
    user_role: str = "normal_user",
    session_id: str | None = None,
) -> dict[str, Any]:
    """Store an admin-only CREATE/ADD-COLUMN preview. It never executes DDL."""

    return create_schema_change_preview_tool(
        sql=sql,
        user_prompt=user_prompt,
        user_role=user_role,
        session_id=session_id,
    )


@mcp_server.tool()
def apply_admin_schema_change(
    schema_change_id: str,
    confirmed: bool = False,
    user_role: str = "normal_user",
    session_id: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Execute one stored, validator-approved restricted DDL preview after explicit admin confirmation."""

    return apply_admin_schema_change_tool(
        schema_change_id=schema_change_id,
        confirmed=confirmed,
        user_role=user_role,
        session_id=session_id,
        request_id=request_id,
    )
