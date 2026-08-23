"""Safe implementations behind the Phase 4 MCP tools.

The same functions are used by the actual MCP server and the in-process FastAPI
verification gateway. No tool has an unrestricted SQL execution capability.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.constants import UserRole
from app.db.session import get_session_factory
from app.services.schema_registry import get_schema_contract
from app.services.session_service import (
    create_pending_action,
    get_active_session,
    get_pending_action,
    pending_action_to_dict,
)
from app.services.sql_validation import preview_admin_schema_change, validate_dml_sql
from app.services.crud_write_service import CrudWriteError, crud_write_service
from app.services.validated_read_service import execute_validated_read
from app.services.mcp_table_service import (
    MCPTableAccessError,
    get_bounded_table_records,
    list_table_capabilities,
    parse_mcp_role,
)


def get_schema_tool() -> dict[str, Any]:
    """Return only the controlled schema contract approved for AI tooling."""

    return get_schema_contract()


def validate_sql_tool(sql: str, user_role: str = UserRole.NORMAL_USER.value) -> dict[str, Any]:
    """Validate one SQL proposal; does not execute INSERT, UPDATE, DELETE, or DDL."""

    try:
        role = UserRole(user_role.strip().lower())
    except ValueError:
        return {
            "is_valid": False,
            "error_code": "invalid_user_role",
            "error_message": "user_role must be normal_user or admin.",
        }

    if role == UserRole.ADMIN and sql.strip().upper().startswith(("CREATE", "ALTER")):
        return preview_admin_schema_change(sql).to_dict()
    return validate_dml_sql(sql, role=role).to_dict()


def execute_validated_read_tool(sql: str) -> dict[str, Any]:
    """Run a validated SELECT only. DML writes are never executed by this tool."""

    with get_session_factory()() as db:
        return execute_validated_read(db, sql)


def create_pending_action_tool(
    session_id: str,
    action_type: str,
    target_table: str | None,
    validated_payload: dict[str, Any],
    preview_data: dict[str, Any],
    generated_sql: str | None = None,
    ttl_minutes: int = 30,
) -> dict[str, Any]:
    """Store a validated preview; it never executes a database write."""

    parsed_session_id = UUID(session_id)
    with get_session_factory()() as db:
        session = get_active_session(db, parsed_session_id)
        if session is None:
            return {
                "created": False,
                "error_code": "inactive_or_missing_session",
                "error_message": "The session is missing, expired, or inactive.",
            }
        action = create_pending_action(
            db,
            session_id=parsed_session_id,
            action_type=action_type,
            target_table=target_table,
            validated_payload=validated_payload,
            preview_data=preview_data,
            generated_sql=generated_sql,
            ttl_minutes=ttl_minutes,
        )
        return {"created": True, "pending_action": pending_action_to_dict(action)}


def get_pending_action_tool(session_id: str, pending_action_id: str) -> dict[str, Any]:
    """Retrieve one confirmation preview scoped to one session."""

    parsed_session_id = UUID(session_id)
    parsed_action_id = UUID(pending_action_id)
    with get_session_factory()() as db:
        action = get_pending_action(db, session_id=parsed_session_id, action_id=parsed_action_id)
        if action is None:
            return {
                "found": False,
                "error_code": "pending_action_not_found",
                "error_message": "No pending action exists for this session.",
            }
        return {"found": True, "pending_action": pending_action_to_dict(action)}



def propose_write_action_tool(
    session_id: str,
    sql: str,
    user_role: str = UserRole.NORMAL_USER.value,
    user_prompt: str | None = None,
    ttl_minutes: int = 30,
) -> dict[str, Any]:
    """Create a confirmation-gated DML preview. No business row is changed."""

    try:
        role = UserRole(user_role.strip().lower())
        with get_session_factory()() as db:
            result = crud_write_service.propose_sql_write(
                db,
                session_id=UUID(session_id),
                sql=sql,
                actor_role=role,
                user_prompt=user_prompt,
                ttl_minutes=ttl_minutes,
            )
            return {
                "created": True,
                "pending_action": result.pending_action,
                "preview": result.preview,
                "validation": result.validation.to_dict() if result.validation else None,
            }
    except CrudWriteError as exc:
        return {"created": False, "error_code": exc.code, "error_message": exc.message, "details": exc.details or []}
    except (ValueError, TypeError) as exc:
        return {"created": False, "error_code": "mcp_tool_input_error", "error_message": str(exc)}


def execute_confirmed_write_tool(
    session_id: str,
    pending_action_id: str,
    request_id: str,
    user_role: str = UserRole.NORMAL_USER.value,
) -> dict[str, Any]:
    """Execute exactly one stored pending action after explicit confirmation."""

    try:
        role = UserRole(user_role.strip().lower())
        with get_session_factory()() as db:
            result = crud_write_service.confirm_action(
                db,
                session_id=UUID(session_id),
                pending_action_id=UUID(pending_action_id),
                actor_role=role,
                request_id=request_id,
            )
            return {
                "executed": not result.idempotent,
                "idempotent": result.idempotent,
                "pending_action": result.pending_action,
                "action_log_id": result.action_log_id,
                "affected_row_count": result.affected_row_count,
                "before_snapshot_count": result.before_snapshot_count,
                "after_snapshot_count": result.after_snapshot_count,
            }
    except CrudWriteError as exc:
        return {"executed": False, "error_code": exc.code, "error_message": exc.message, "details": exc.details or []}
    except (ValueError, TypeError) as exc:
        return {"executed": False, "error_code": "mcp_tool_input_error", "error_message": str(exc)}


def cancel_pending_action_tool(
    session_id: str,
    pending_action_id: str,
    request_id: str,
    user_role: str = UserRole.NORMAL_USER.value,
) -> dict[str, Any]:
    """Cancel a stored pending write without changing business records."""

    try:
        role = UserRole(user_role.strip().lower())
        with get_session_factory()() as db:
            return crud_write_service.cancel_action(
                db,
                session_id=UUID(session_id),
                pending_action_id=UUID(pending_action_id),
                actor_role=role,
                request_id=request_id,
            )
    except CrudWriteError as exc:
        return {"cancelled": False, "error_code": exc.code, "error_message": exc.message, "details": exc.details or []}
    except (ValueError, TypeError) as exc:
        return {"cancelled": False, "error_code": "mcp_tool_input_error", "error_message": str(exc)}


def retrieve_docs_tool(question: str, top_k: int = 4) -> dict[str, Any]:
    """Search locally indexed document chunks by semantic similarity only."""

    try:
        from app.rag.document_rag_service import DocumentRagError, document_rag_service

        matches = document_rag_service.retrieve(question, top_k=top_k)
        return {
            "retrieved": True,
            "match_count": len(matches),
            "matches": [match.to_dict() for match in matches],
            "local_only": True,
            "llm_called": False,
        }
    except DocumentRagError as exc:
        return {"retrieved": False, "error_code": exc.code, "error_message": exc.message, "details": exc.details}


def get_document_sources_tool(document_id: str, limit: int = 100) -> dict[str, Any]:
    """Return filename/page/chunk metadata for one already-indexed document."""

    try:
        from app.rag.document_rag_service import DocumentRagError, document_rag_service

        chunks = document_rag_service.get_document_chunks(document_id=UUID(document_id), limit=limit)
        return {"found": bool(chunks), "chunk_count": len(chunks), "chunks": chunks, "local_only": True}
    except (DocumentRagError, ValueError) as exc:
        code = getattr(exc, "code", "mcp_tool_input_error")
        message = getattr(exc, "message", str(exc))
        return {"found": False, "error_code": code, "error_message": message}


def _normalize_identity(value: str) -> str:
    """Normalize product text for explicit identity matching only."""

    import re

    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _document_explicitly_names_product(document_text: str, product_name: str, product_code: str) -> bool:
    """Return true only when a product name/code appears in document evidence.

    This intentionally avoids vector-to-row fuzzy matching. A product has to be named in
    the retrieved evidence before its vendor/price database facts can be used in a hybrid
    conclusion.
    """

    haystack = _normalize_identity(document_text)
    name = _normalize_identity(product_name)
    code = _normalize_identity(product_code)
    return bool((name and name in haystack) or (code and code in haystack))


def get_verified_hybrid_evidence_tool(
    document_matches: list[dict[str, Any]],
    max_price: float | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Return verified vendor/product rows only when retrieved docs explicitly name products.

    The tool accepts no SQL and uses an ORM SELECT over only the approved
    products/vendor/product_vendor_mappings tables. It never writes data.
    """

    from decimal import Decimal

    from sqlalchemy import select

    from app.models.business import Product, ProductVendorMapping, Vendor

    try:
        normalized_limit = max(1, min(int(limit), 100))
        normalized_price = None if max_price is None else float(max_price)
        if normalized_price is not None and normalized_price < 0:
            return {
                "retrieved": False,
                "error_code": "invalid_max_price",
                "error_message": "max_price must be zero or greater when supplied.",
            }
        if not isinstance(document_matches, list):
            return {
                "retrieved": False,
                "error_code": "invalid_document_matches",
                "error_message": "document_matches must be a list of retrieved document evidence objects.",
            }

        safe_documents = [
            item
            for item in document_matches
            if isinstance(item, dict) and isinstance(item.get("text"), str) and isinstance(item.get("reference"), str)
        ]
        if not safe_documents:
            return {
                "retrieved": True,
                "matches": [],
                "no_data_reason": "No usable retrieved document text was supplied for verified identity matching.",
            }

        with get_session_factory()() as db:
            products = list(db.scalars(select(Product).where(Product.is_active.is_(True))).all())
            product_document_references: dict[int, list[str]] = {}
            for product in products:
                references: list[str] = []
                for document in safe_documents:
                    if _document_explicitly_names_product(
                        str(document["text"]), product.product_name, product.product_code
                    ):
                        references.append(str(document["reference"]))
                if references:
                    product_document_references[product.id] = sorted(set(references))

            if not product_document_references:
                return {
                    "retrieved": True,
                    "matches": [],
                    "no_data_reason": (
                        "Document evidence was retrieved, but no active PostgreSQL product name or product code "
                        "was explicitly present in the retrieved chunks."
                    ),
                }

            statement = (
                select(Product, Vendor, ProductVendorMapping)
                .join(ProductVendorMapping, ProductVendorMapping.product_id == Product.id)
                .join(Vendor, Vendor.id == ProductVendorMapping.vendor_id)
                .where(Product.id.in_(list(product_document_references)))
                .where(Vendor.status == "active")
                .order_by(ProductVendorMapping.quoted_price.asc(), Vendor.vendor_name.asc())
                .limit(normalized_limit)
            )
            if normalized_price is not None:
                statement = statement.where(ProductVendorMapping.quoted_price < Decimal(str(normalized_price)))

            results = db.execute(statement).all()
            matches: list[dict[str, Any]] = []
            for product, vendor, mapping in results:
                matches.append(
                    {
                        "vendor_id": vendor.id,
                        "vendor_code": vendor.vendor_code,
                        "vendor_name": vendor.vendor_name,
                        "vendor_city": vendor.city,
                        "product_id": product.id,
                        "product_code": product.product_code,
                        "product_name": product.product_name,
                        "product_category": product.category,
                        "quoted_price": float(mapping.quoted_price),
                        "vendor_sku": mapping.vendor_sku,
                        "is_preferred": bool(mapping.is_preferred),
                        "document_source_references": product_document_references[product.id],
                    }
                )

            reason = None
            if not matches:
                reason = (
                    "Verified product identities were found in document evidence, but no active vendor/product "
                    "mapping satisfied the requested PostgreSQL price filter."
                    if normalized_price is not None
                    else "Verified product identities were found in document evidence, but no active vendor/product mapping was available."
                )
            return {
                "retrieved": True,
                "matches": matches,
                "no_data_reason": reason,
                "max_price": normalized_price,
                "database_source_tables": ["vendors", "products", "product_vendor_mappings"],
            }
    except Exception as exc:  # Defensive MCP boundary; raw database details remain local.
        return {
            "retrieved": False,
            "error_code": "hybrid_database_evidence_failed",
            "error_message": "The verified hybrid database-evidence tool could not complete.",
        }


# ---------------------------------------------------------------------------
# Phase 12: MCP expansion and hardening
# ---------------------------------------------------------------------------


def list_tables_tool(user_role: str = UserRole.NORMAL_USER.value) -> dict[str, Any]:
    """List approved application tables without exposing PostgreSQL system metadata."""

    try:
        return list_table_capabilities(user_role=user_role)
    except MCPTableAccessError as exc:
        return {"listed": False, "error_code": exc.code, "error_message": exc.message}


def get_table_records_tool(
    table_name: str,
    limit: int = 50,
    offset: int = 0,
    user_role: str = UserRole.NORMAL_USER.value,
    filters: dict[str, Any] | None = None,
    relationship_filter: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a bounded page from one approved table; raw SQL is never accepted."""

    try:
        return get_bounded_table_records(
            table_name=table_name,
            limit=limit,
            offset=offset,
            user_role=user_role,
            filters=filters,
            relationship_filter=relationship_filter,
        )
    except MCPTableAccessError as exc:
        return {"retrieved": False, "error_code": exc.code, "error_message": exc.message}


def _schema_change_to_dict(change: Any) -> dict[str, Any]:
    """Return a minimal frontend-safe stored schema-preview representation."""

    return {
        "schema_change_id": str(change.id),
        "session_id": str(change.session_id) if change.session_id else None,
        "requested_by_role": change.requested_by_role,
        "user_prompt": change.user_prompt,
        "proposed_sql": change.proposed_sql,
        "schema_preview": change.schema_preview,
        "status": change.status,
        "confirmed_at": change.confirmed_at.isoformat() if change.confirmed_at else None,
        "executed_at": change.executed_at.isoformat() if change.executed_at else None,
        "error_message": change.error_message,
        "created_at": change.created_at.isoformat() if change.created_at else None,
        "updated_at": change.updated_at.isoformat() if change.updated_at else None,
    }


def create_schema_change_preview_tool(
    sql: str,
    user_prompt: str,
    user_role: str = UserRole.NORMAL_USER.value,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Store an admin-only CREATE/ADD-COLUMN preview; it never executes DDL.

    Phase 12 intentionally makes the MCP boundary explicit and auditable while keeping
    actual schema execution deferred to the Phase 13 persisted confirmation workflow.
    """

    try:
        role = parse_mcp_role(user_role)
    except MCPTableAccessError as exc:
        return {"created": False, "error_code": exc.code, "error_message": exc.message}
    if role != UserRole.ADMIN:
        return {
            "created": False,
            "error_code": "admin_role_required",
            "error_message": "Only an admin role can create a restricted schema-change preview.",
        }

    validation = preview_admin_schema_change(sql)
    if not validation.is_valid:
        return {
            "created": False,
            "error_code": validation.error_code or "schema_change_validation_failed",
            "error_message": validation.error_message or "The restricted schema-change preview failed validation.",
            "validation": validation.to_dict(),
        }

    parsed_session_id = None
    if session_id:
        try:
            parsed_session_id = UUID(session_id)
        except (TypeError, ValueError):
            return {
                "created": False,
                "error_code": "invalid_session_id",
                "error_message": "session_id must be a valid UUID when supplied.",
            }

    try:
        from app.models.operations import SchemaChangeRequest

        with get_session_factory()() as db:
            if parsed_session_id is not None and get_active_session(db, parsed_session_id) is None:
                return {
                    "created": False,
                    "error_code": "inactive_or_missing_session",
                    "error_message": "The supplied session is missing, expired, or inactive.",
                }
            change = SchemaChangeRequest(
                session_id=parsed_session_id,
                requested_by_role=role.value,
                user_prompt=user_prompt.strip(),
                proposed_sql=validation.normalized_sql or sql.strip(),
                schema_preview=validation.to_dict(),
                status="pending_confirmation",
            )
            db.add(change)
            db.commit()
            db.refresh(change)
            return {
                "created": True,
                "schema_change": _schema_change_to_dict(change),
                "execution_allowed": False,
                "next_step": "Phase 13 admin confirmation/migration workflow is required before any DDL can execute.",
            }
    except Exception:
        return {
            "created": False,
            "error_code": "schema_preview_storage_failed",
            "error_message": "The validated schema-change preview could not be stored for audit.",
        }


def apply_admin_schema_change_tool(
    schema_change_id: str,
    confirmed: bool = False,
    user_role: str = UserRole.NORMAL_USER.value,
    session_id: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Execute one persisted restricted admin schema preview after explicit confirmation.

    The caller never supplies SQL here.  The service revalidates the SQL stored in the
    preview record before executing it and records an audit event.
    """

    try:
        role = parse_mcp_role(user_role)
    except MCPTableAccessError as exc:
        return {"executed": False, "error_code": exc.code, "error_message": exc.message}
    if role != UserRole.ADMIN:
        return {
            "executed": False,
            "error_code": "admin_role_required",
            "error_message": "Only an admin role can execute a restricted schema-change request.",
        }
    try:
        change_id = UUID(schema_change_id)
    except (TypeError, ValueError):
        return {
            "executed": False,
            "error_code": "invalid_schema_change_id",
            "error_message": "schema_change_id must be a valid UUID.",
        }
    parsed_session_id = None
    if session_id:
        try:
            parsed_session_id = UUID(session_id)
        except (TypeError, ValueError):
            return {
                "executed": False,
                "error_code": "invalid_session_id",
                "error_message": "session_id must be a valid UUID when supplied.",
            }

    try:
        from app.services.admin_schema_service import AdminSchemaExecutionError, execute_confirmed_schema_change

        with get_session_factory()() as db:
            return execute_confirmed_schema_change(
                db,
                schema_change_id=change_id,
                actor_role=role,
                confirmed=confirmed,
                request_id=request_id,
                session_id=parsed_session_id,
            )
    except AdminSchemaExecutionError as exc:
        return {"executed": False, "error_code": exc.code, "error_message": exc.message, "raw_sql_accepted": False}
    except Exception:
        return {
            "executed": False,
            "error_code": "admin_schema_execution_failed",
            "error_message": "The restricted admin schema change could not be completed.",
            "raw_sql_accepted": False,
        }
