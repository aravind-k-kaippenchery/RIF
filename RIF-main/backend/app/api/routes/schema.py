"""Phase 3 schema intelligence and business glossary endpoints."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.status import HTTP_404_NOT_FOUND, HTTP_503_SERVICE_UNAVAILABLE

from app.core.constants import ResponseStatus
from app.core.exceptions import AppError
from app.core.response import ResponseBuilder
from app.db.health import get_database_health
from app.db.session import get_db_session
from app.services.business_glossary import list_glossary_entries, normalize_business_text, resolve_business_term
from app.services.schema_registry import (
    get_allowed_columns,
    get_allowed_tables,
    get_live_schema_status,
    get_relationships,
    get_schema_contract,
    get_table_schema,
)

router = APIRouter(prefix="/api/schema", tags=["Schema intelligence"])


def _database_unavailable_error() -> AppError:
    health = get_database_health()
    return AppError(
        status=ResponseStatus.DATABASE_UNAVAILABLE,
        code="database_unavailable",
        message=f"PostgreSQL is unavailable. {health.message}",
        http_status_code=HTTP_503_SERVICE_UNAVAILABLE,
    )


@router.get("", summary="Get the controlled schema contract for LLM prompts")
def schema_contract(request: Request):
    health = get_database_health()
    contract = get_schema_contract()
    contract["database_connected"] = health.connected
    contract["database_message"] = health.message
    return ResponseBuilder.success(
        request,
        answer="Controlled schema contract retrieved. Only approved application tables and columns are exposed.",
        data=contract,
    )


@router.get("/live-status", summary="Check whether live PostgreSQL tables match the controlled schema")
def live_schema_status(request: Request, db: Session = Depends(get_db_session)):
    try:
        status = get_live_schema_status(db)
    except SQLAlchemyError as exc:
        raise _database_unavailable_error() from exc
    return ResponseBuilder.success(
        request,
        answer="Live PostgreSQL schema status retrieved.",
        data=status,
    )


@router.get("/tables", summary="List approved tables available to the future LLM")
def allowed_tables(request: Request):
    tables = get_allowed_tables()
    return ResponseBuilder.success(
        request,
        answer="Approved application tables retrieved.",
        data={"table_count": len(tables), "tables": tables},
    )


@router.get("/tables/{table_name}", summary="Get controlled column metadata for one table")
def table_schema(table_name: str, request: Request):
    try:
        table = get_table_schema(table_name)
    except KeyError as exc:
        raise AppError(
            status=ResponseStatus.INFORMATION_NOT_AVAILABLE,
            code="table_not_available",
            message=str(exc),
            http_status_code=HTTP_404_NOT_FOUND,
        ) from exc
    return ResponseBuilder.success(
        request,
        answer=f"Controlled schema for table '{table['table_name']}' retrieved.",
        data=table,
    )


@router.get("/tables/{table_name}/columns", summary="List allowed columns for one table")
def table_columns(table_name: str, request: Request):
    columns = get_allowed_columns(table_name)
    if not columns:
        raise AppError(
            status=ResponseStatus.INFORMATION_NOT_AVAILABLE,
            code="table_not_available",
            message=f"Table '{table_name}' is not an approved application table.",
            http_status_code=HTTP_404_NOT_FOUND,
        )
    return ResponseBuilder.success(
        request,
        answer=f"Allowed columns for table '{table_name.lower()}' retrieved.",
        data={"table_name": table_name.lower(), "columns": columns},
    )


@router.get("/relationships", summary="List approved table relationships")
def relationships(request: Request):
    rels = get_relationships()
    return ResponseBuilder.success(
        request,
        answer="Approved database relationships retrieved.",
        data={"relationship_count": len(rels), "relationships": rels},
    )


@router.get("/glossary", summary="List business terms mapped to schema targets")
def glossary(request: Request):
    entries = list_glossary_entries()
    return ResponseBuilder.success(
        request,
        answer="Business glossary retrieved.",
        data={"entry_count": len(entries), "entries": entries},
    )


@router.get("/glossary/resolve", summary="Resolve one business term to a table or column")
def resolve_term(term: str, request: Request):
    resolved = resolve_business_term(term)
    if resolved is None:
        raise AppError(
            status=ResponseStatus.INFORMATION_NOT_AVAILABLE,
            code="business_term_not_found",
            message=f"No glossary entry exists for '{term}'.",
            http_status_code=HTTP_404_NOT_FOUND,
        )
    return ResponseBuilder.success(
        request,
        answer=f"Business term '{term}' resolved to '{resolved['target_name']}'.",
        data=resolved,
    )


@router.get("/glossary/normalize", summary="Find known business terms in a natural-language prompt")
def normalize_text(text: str, request: Request):
    normalized = normalize_business_text(text)
    return ResponseBuilder.success(
        request,
        answer="Business terms in the prompt were analyzed.",
        data=normalized,
    )
