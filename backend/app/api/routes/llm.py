"""Phase 5 endpoints for the local Ollama adapter.

These endpoints demonstrate controlled local-model integration. They never execute
SQL, database writes, schema changes, or MCP tools from model output.
"""

from fastapi import APIRouter, Request
from starlette.status import HTTP_422_UNPROCESSABLE_CONTENT, HTTP_502_BAD_GATEWAY, HTTP_503_SERVICE_UNAVAILABLE

from app.core.constants import ResponseStatus
from app.core.exceptions import AppError
from app.core.response import ResponseBuilder
from app.core.schemas import SourceCitation
from app.schemas.phase5 import (
    GroundedAnswerRequest,
    IntentClassificationRequest,
    RecordExtractionRequest,
    SQLGenerationRequest,
)
from app.services.llm_service import LLMOutputValidationError, LLMServiceError, OllamaUnavailableError, llm_service
from app.services.request_clarification_service import (
    analyze_request_clarity,
    assert_generated_tables_match,
)

router = APIRouter(prefix="/api/llm", tags=["Local LLM"])


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


@router.get("/status", summary="Check local Ollama availability and configured-model readiness")
def llm_status(request: Request):
    health = llm_service.check_llm_health()
    return ResponseBuilder.success(
        request,
        answer="Local Ollama status retrieved. No model inference was requested.",
        data={
            **health.to_dict(),
            "phase": 5,
            "structured_output_required": True,
            "max_model_attempts": 2,
            "model_can_execute_sql": False,
            "model_can_access_database_credentials": False,
            "model_can_call_mcp_directly": False,
        },
    )


@router.post("/classify-intent", summary="Use local Ollama to classify a future agent route")
def classify_intent(payload: IntentClassificationRequest, request: Request):
    try:
        result, metadata, glossary = llm_service.classify_intent(payload.question)
    except LLMServiceError as exc:
        _raise_llm_error(exc)
    return ResponseBuilder.success(
        request,
        answer="Local LLM intent classification completed. No database or MCP tool was called.",
        data={"classification": result.model_dump(), "glossary": glossary, "model_metadata": metadata.model_dump()},
    )


@router.post("/generate-sql", summary="Generate and safety-validate a SQL proposal without executing it")
def generate_sql(payload: SQLGenerationRequest, request: Request):
    clarity = analyze_request_clarity(payload.question)
    if clarity.needs_clarification:
        return ResponseBuilder.success(
            request,
            status=ResponseStatus.CLARIFICATION_REQUIRED,
            answer=clarity.message or "Please clarify the request.",
            data={
                "clarification": clarity.to_data(),
                "execution_allowed": False,
            },
            generated_sql=None,
        )

    try:
        proposal, validation, metadata, glossary = llm_service.generate_sql(payload.question, payload.route)
    except LLMServiceError as exc:
        _raise_llm_error(exc)

    try:
        assert_generated_tables_match(
            expected_tables=clarity.resolved_tables,
            generated_tables=list(validation.tables or []),
        )
    except ValueError:
        return ResponseBuilder.success(
            request,
            status=ResponseStatus.CLARIFICATION_REQUIRED,
            answer=(
                "The generated SQL did not use the table explicitly requested, so it was rejected. "
                "Please restate the target table and exact operation or filter."
            ),
            data={
                "clarification": {
                    "clarification_code": "generated_sql_table_mismatch",
                    "missing_fields": ["target_table", "operation_or_filter"],
                    "resolved_tables": list(clarity.resolved_tables),
                    "generated_tables": list(validation.tables or []),
                    "ollama_called": True,
                    "sql_generated": False,
                    "database_touched": False,
                },
                "execution_allowed": False,
            },
            generated_sql=None,
        )

    return ResponseBuilder.success(
        request,
        answer="Local LLM generated a SQL proposal and the Phase 4 validator approved it. No SQL was executed.",
        data={
            "proposal": proposal.model_dump(),
            "validation": validation.to_dict(),
            "glossary": glossary,
            "model_metadata": metadata.model_dump(),
            "execution_allowed": False,
            "explicit_target_tables": list(clarity.resolved_tables),
        },
        generated_sql=validation.normalized_sql,
    )


@router.post("/extract-record", summary="Extract a record proposal through local Ollama without inserting anything")
def extract_record(payload: RecordExtractionRequest, request: Request):
    try:
        result, metadata = llm_service.extract_record_fields(payload.instruction, payload.target_table)
    except LLMServiceError as exc:
        _raise_llm_error(exc)
    response_status = ResponseStatus.CLARIFICATION_REQUIRED if result.requires_clarification else ResponseStatus.SUCCESS
    answer = (
        "Local LLM extracted a record proposal. Confirmation and database write are not part of Phase 5."
        if not result.requires_clarification
        else "The proposed record needs clarification before any future confirmation workflow can be created."
    )
    return ResponseBuilder.success(
        request,
        status=response_status,
        answer=answer,
        data={"record_proposal": result.model_dump(), "model_metadata": metadata.model_dump(), "execution_allowed": False},
    )


@router.post("/generate-grounded-answer", summary="Generate an answer only from caller-supplied evidence")
def generate_grounded_answer(payload: GroundedAnswerRequest, request: Request):
    evidence = [item.model_dump() for item in payload.evidence]
    try:
        result, metadata = llm_service.generate_grounded_answer(payload.question, evidence)
    except LLMServiceError as exc:
        _raise_llm_error(exc)

    evidence_by_reference = {item.reference: item for item in payload.evidence}
    citations = [
        SourceCitation(
            source_type=evidence_by_reference[reference].source_type,
            reference=reference,
            detail="Evidence supplied to the Phase 5 grounding endpoint.",
        )
        for reference in result.source_references
        if reference in evidence_by_reference
    ]
    status = ResponseStatus.SUCCESS if result.supported else ResponseStatus.INFORMATION_NOT_AVAILABLE
    return ResponseBuilder.success(
        request,
        status=status,
        answer=result.answer,
        data={
            "supported": result.supported,
            "source_references": result.source_references,
            "model_metadata": metadata.model_dump() if metadata else {"model": None, "attempts": 0},
        },
        sources=citations,
    )
