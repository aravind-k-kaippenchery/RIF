"""Phase 6 endpoint for the first end-to-end grounded PostgreSQL read path."""

from fastapi import APIRouter, Request
from starlette.status import (
    HTTP_400_BAD_REQUEST,
    HTTP_422_UNPROCESSABLE_CONTENT,
    HTTP_502_BAD_GATEWAY,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from app.core.constants import AgentRoute, ResponseStatus
from app.core.exceptions import AppError
from app.core.request_context import get_request_id, get_session_id
from app.core.response import ResponseBuilder
from app.core.schemas import SourceCitation
from app.schemas.phase6 import StructuredReadRequest
from app.services.llm_service import LLMOutputValidationError, LLMServiceError, OllamaUnavailableError
from app.services.structured_read_service import StructuredReadError, structured_read_service

router = APIRouter(prefix="/api/structured-read", tags=["Structured read"])


def _raise_llm_error(exc: LLMServiceError) -> None:
    if isinstance(exc, OllamaUnavailableError):
        raise AppError(
            status=ResponseStatus.LLM_UNAVAILABLE,
            code=exc.code,
            message=exc.message,
            http_status_code=HTTP_503_SERVICE_UNAVAILABLE,
        ) from exc
    if isinstance(exc, LLMOutputValidationError):
        raise AppError(
            status=ResponseStatus.VALIDATION_FAILED,
            code=exc.code,
            message=exc.message,
            http_status_code=HTTP_422_UNPROCESSABLE_CONTENT,
        ) from exc
    raise AppError(
        status=ResponseStatus.TOOL_FAILED,
        code=exc.code,
        message=exc.message,
        http_status_code=HTTP_502_BAD_GATEWAY,
    ) from exc


@router.post("", summary="Answer a natural-language database question through the safe SELECT-only path")
def structured_read(payload: StructuredReadRequest, request: Request):
    """Execute one approved SELECT and return only PostgreSQL-derived rows and answer text."""

    try:
        result = structured_read_service.execute(
            question=payload.question,
            request_id=get_request_id(request),
            session_id=get_session_id(request),
        )
    except LLMServiceError as exc:
        _raise_llm_error(exc)
    except StructuredReadError as exc:
        http_status = HTTP_400_BAD_REQUEST if exc.status == ResponseStatus.VALIDATION_FAILED else HTTP_503_SERVICE_UNAVAILABLE
        raise AppError(
            status=exc.status,
            code=exc.code,
            message=exc.message,
            http_status_code=http_status,
        ) from exc

    table_references = [str(table) for table in result.source.get("tables", [])]
    sources = [
        SourceCitation(
            source_type="database",
            reference=table_name,
            detail="PostgreSQL table queried through the validated MCP read tool.",
        )
        for table_name in table_references
    ]
    return ResponseBuilder.success(
        request,
        route=AgentRoute.STRUCTURED_READ,
        status=result.status,
        answer=result.answer,
        data={
            "question": result.question,
            "row_count": result.row_count,
            "rows": result.rows,
            "database_source": result.source,
            "sql_proposal_explanation": result.proposal.explanation,
            "validation": result.validation.to_dict(),
            "glossary": result.glossary,
            "model_metadata": result.model_metadata.model_dump(),
            "execution": {
                "tool": "execute_validated_read",
                "via": "local_mcp_client_facade",
                "executed": True,
                "write_execution_allowed": False,
            },
            "query_log": result.audit,
        },
        sources=sources,
        generated_sql=result.validation.normalized_sql or result.proposal.sql,
    )
