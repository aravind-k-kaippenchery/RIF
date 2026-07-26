"""Phase 12 LangGraph orchestration of the four B2B agent routes.

This graph deliberately calls existing, hardened services instead of reimplementing their
security controls. SQL still goes through the Phase 4 validator/MCP tool boundary, writes
still become confirmation-gated previews, document answers remain grounded in local
ChromaDB sources, and hybrid answers use restricted MCP evidence fusion.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Literal, TypedDict
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from sqlalchemy.orm import Session

from app.agents.router import RouteDecision, classify_question
from app.core.constants import AgentRoute, ResponseStatus, UserRole
from app.core.schemas import SourceCitation
from app.rag.document_rag_service import DocumentRagError, document_rag_service
from app.services.hybrid_evidence_service import HybridEvidenceError, hybrid_evidence_service
from app.services.crud_write_service import CrudWriteError, crud_write_service
from app.services.llm_service import LLMOutputValidationError, LLMServiceError, llm_service
from app.services.session_service import create_session, get_active_session
from app.services.synthetic_data_service import (
    SyntheticDataGenerationError,
    generate_synthetic_records,
    parse_synthetic_data_prompt,
    supported_synthetic_tables,
)
from app.services.structured_read_service import StructuredReadError, structured_read_service
from app.services.request_clarification_service import (
    ClarificationDecision,
    analyze_request_clarity,
    assert_generated_tables_match,
)
from app.services.schema_metadata_question_service import (
    SchemaMetadataQuestionError,
    answer_schema_metadata_question,
    detect_schema_metadata_question,
)
from app.services.table_record_question_service import (
    TableRecordQuestion,
    TableRecordQuestionError,
    detect_table_record_question,
    read_table_records,
)
from app.services.demo_question_service import (
    DemoQuestionError,
    DemoQuestionRequest,
    detect_demo_question,
    execute_demo_question,
)
from app.services.explicit_insert_service import detect_explicit_insert
from app.services.soft_routing_service import infer_readonly_business_table
from app.services.parent_child_crud_service import (
    ParentChildError,
    looks_like_parent_child_request,
    parent_child_crud_service,
    supported_parent_child_tables,
)


class AgentOrchestrationError(RuntimeError):
    """Safe service-level error returned from the unified agent endpoint."""

    def __init__(self, *, status: ResponseStatus, code: str, message: str, details: list[dict[str, Any]] | None = None) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.details = details or []
        super().__init__(message)


class AgentState(TypedDict, total=False):
    question: str
    session_id: str | None
    request_id: str | None
    user_role: str
    top_k: int | None
    db: Session
    route: str
    route_confidence: float
    routing_reason: str
    graph_trace: list[dict[str, Any]]
    status: str
    answer: str
    data: dict[str, Any]
    sources: list[dict[str, Any]]
    generated_sql: str | None
    pending_action_id: str | None
    memory_context: dict[str, Any] | None
    resolved_tables: list[str]
    clarification_code: str | None
    clarification_message: str | None
    clarification_missing_fields: list[str]
    clarification_details: list[dict[str, Any]]
    detected_intent: str | None
    schema_metadata_request: dict[str, Any] | None
    table_record_request: dict[str, Any] | None
    demo_question_request: dict[str, Any] | None
    parent_child_request: bool


@dataclass(frozen=True)
class AgentRunResult:
    """Normalized result emitted after the LangGraph workflow reaches its terminal node."""

    route: AgentRoute
    status: ResponseStatus
    answer: str
    data: dict[str, Any]
    sources: list[SourceCitation]
    generated_sql: str | None
    pending_action_id: str | None


class AgentOrchestrator:
    """Compile once and invoke the Phase 12 route graph for each request."""

    def __init__(self) -> None:
        self._graph = self._build_graph()

    @staticmethod
    def _looks_like_hybrid_question(question: str) -> bool:
        normalized = " ".join(str(question or "").casefold().split())
        if not normalized:
            return False
        database_signal = bool(
            re.search(r"\b(?:employees?|workers?|staff|vendors?|suppliers?|customers?|clients?|products?|items?|sales\s+deals?)\b", normalized)
            and re.search(r"\b(?:show|list|count|how\s+many|active|inactive|from|in|approved|price|department|city)\b", normalized)
        )
        document_signal = bool(
            re.search(r"\b(?:summarize|explain|policy|documents?|uploaded|faq|onboarding|compliance|checklist|catalog|ticket|support|response\s+time|tasks?|requirements?|submit|submitted)\b", normalized)
        )
        connector = bool(re.search(r"\b(?:and|also|along\s+with|together\s+with)\b", normalized))
        return database_signal and document_signal and connector

    @staticmethod
    def _looks_like_dangerous_sql(question: str) -> bool:
        return bool(re.search(r"\b(?:drop|truncate|alter)\s+(?:table\s+)?[a-z][a-z0-9_]*\b", str(question or "").casefold()))

    @staticmethod
    def _append_trace(state: AgentState, node: str, detail: str) -> list[dict[str, Any]]:
        """Return a new graph_trace list with one more entry appended.

        LangGraph state updates are merged by replacing the ``graph_trace`` key with
        whatever this returns, so a new list is built rather than mutating
        ``state["graph_trace"]`` in place -- mutating the existing list would let two
        concurrent branches silently share and corrupt each other's trace history.
        """

        existing = list(state.get("graph_trace") or [])
        existing.append({"node": node, "detail": detail})
        return existing

    def _classify(self, state: AgentState) -> dict[str, Any]:
        schema_request = detect_schema_metadata_question(state["question"])
        if schema_request.handled:
            return {
                "route": AgentRoute.SYSTEM.value,
                "route_confidence": 1.0,
                "routing_reason": (
                    "The current turn is a database schema-metadata question, so live metadata "
                    "inspection is used instead of Ollama or business-row SQL."
                ),
                "schema_metadata_request": schema_request.to_state(),
                "detected_intent": "schema_metadata",
                "graph_trace": self._append_trace(
                    state,
                    "schema_intent_guard",
                    "Detected a table/column metadata question from the current turn; prior filters were ignored and Ollama was not called.",
                ),
            }

        if self._looks_like_dangerous_sql(state["question"]):
            return {
                "route": AgentRoute.SYSTEM.value,
                "route_confidence": 1.0,
                "routing_reason": "Dangerous SQL was rejected before route execution.",
                "clarification_code": "dangerous_sql_rejected",
                "clarification_message": "Dangerous SQL or schema-destructive commands are not allowed through the assistant.",
                "clarification_missing_fields": [],
                "clarification_details": [{"blocked_before_ollama": True, "database_touched": False}],
                "resolved_tables": [],
                "detected_intent": "unsafe_sql",
                "graph_trace": self._append_trace(state, "safety_guard", "Rejected dangerous SQL before Ollama, SQL validation, or database access."),
            }

        if self._looks_like_hybrid_question(state["question"]):
            return {
                "route": AgentRoute.HYBRID.value,
                "route_confidence": 0.93,
                "routing_reason": "The question explicitly asks for both structured database facts and uploaded-document evidence.",
                "detected_intent": "hybrid_database_document",
                "graph_trace": self._append_trace(state, "hybrid_intent_guard", "Detected combined database and document intent before deterministic demo-read routing."),
            }

        inferred_table = infer_readonly_business_table(state["question"])
        if inferred_table and state.get("db") is None:
            return {
                "route": AgentRoute.STRUCTURED_READ.value,
                "route_confidence": 0.91,
                "routing_reason": "A narrow read-only people/department question inferred the employees table without guessing a write target.",
                "resolved_tables": [inferred_table],
                "detected_intent": "soft_readonly_business_table",
                "graph_trace": self._append_trace(state, "soft_readonly_table_inference", f"Inferred {inferred_table} for a read-only personnel question."),
            }

        demo_request = detect_demo_question(state["question"])
        if demo_request.handled:
            return {
                "route": demo_request.route.value,
                "route_confidence": 0.99,
                "routing_reason": (
                    "A high-confidence demo-safe read/capability question was recognized, so the backend will answer it deterministically "
                    "without asking Ollama to invent table names or columns."
                ),
                "demo_question_request": demo_request.to_state(),
                "resolved_tables": [demo_request.table] if demo_request.table else [],
                "detected_intent": demo_request.kind or "deterministic_demo_question",
                "graph_trace": self._append_trace(
                    state,
                    "demo_question_guard",
                    "Detected a read-only demo question and routed it to deterministic SQLAlchemy/schema handling before parent-child or LLM SQL generation.",
                ),
            }

        if looks_like_parent_child_request(state["question"]):
            return {
                "route": AgentRoute.CRUD_WRITE.value,
                "route_confidence": 0.96,
                "routing_reason": (
                    "The request concerns a parent-child business relationship. Ollama will produce only a typed semantic plan; "
                    "the backend will resolve business codes and compile bounded SQLAlchemy operations."
                ),
                "parent_child_request": True,
                "detected_intent": "parent_child_crud",
                "graph_trace": self._append_trace(
                    state,
                    "parent_child_intent_guard",
                    "Detected relationship-oriented language and routed it to schema-grounded parent-child planning before generic SQL generation.",
                ),
            }

        table_record_request = detect_table_record_question(state["question"])
        if table_record_request.handled:
            if table_record_request.needs_clarification:
                return {
                    "route": AgentRoute.SYSTEM.value,
                    "route_confidence": 1.0,
                    "routing_reason": "A table was named, but the user did not specify whether they want rows or schema metadata.",
                    "clarification_code": "ambiguous_table_reference",
                    "clarification_message": table_record_request.clarification_message,
                    "clarification_missing_fields": ["table_view_intent"],
                    "clarification_details": [],
                    "resolved_tables": [table_record_request.canonical_table] if table_record_request.canonical_table else [],
                    "detected_intent": "ambiguous_table_reference",
                    "graph_trace": self._append_trace(
                        state,
                        "table_record_intent_guard",
                        "A bare table reference was ambiguous, so clarification was requested without calling Ollama or generating SQL.",
                    ),
                }
            return {
                "route": AgentRoute.STRUCTURED_READ.value,
                "route_confidence": 1.0,
                "routing_reason": "A simple whole-table row request was resolved deterministically and will use the bounded MCP table-read tool without Ollama.",
                "table_record_request": table_record_request.to_state(),
                "resolved_tables": [table_record_request.canonical_table] if table_record_request.canonical_table else [],
                "detected_intent": "table_record_browse",
                "graph_trace": self._append_trace(
                    state,
                    "table_record_intent_guard",
                    "Detected a simple table-row request; Ollama and generated SQL were bypassed.",
                ),
            }

        clarity: ClarificationDecision = analyze_request_clarity(state["question"])
        if clarity.needs_clarification:
            return {
                "route": AgentRoute.SYSTEM.value,
                "route_confidence": 1.0,
                "routing_reason": "The deterministic ambiguity gate stopped the request before Ollama or SQL generation.",
                "clarification_code": clarity.code,
                "clarification_message": clarity.message,
                "clarification_missing_fields": list(clarity.missing_fields),
                "clarification_details": list(clarity.details),
                "resolved_tables": list(clarity.resolved_tables),
                "detected_intent": clarity.detected_intent,
                "graph_trace": self._append_trace(
                    state,
                    "ambiguity_guard",
                    f"Clarification required ({clarity.code}); Ollama was not called and no SQL was generated.",
                ),
            }

        decision: RouteDecision = classify_question(state["question"])
        return {
            "route": decision.route.value,
            "route_confidence": decision.confidence,
            "routing_reason": decision.reason,
            "resolved_tables": list(clarity.resolved_tables),
            "detected_intent": clarity.detected_intent,
            "graph_trace": self._append_trace(state, "router", f"Selected {decision.route.value} after deterministic clarity validation."),
        }

    @staticmethod
    def _route_selector(state: AgentState) -> Literal["schema_metadata", "demo_question", "clarification", "parent_child", "table_records", "structured_read", "crud_write", "document_rag", "hybrid"]:
        route = state.get("route", AgentRoute.STRUCTURED_READ.value)
        if state.get("demo_question_request"):
            return "demo_question"
        if route == AgentRoute.SYSTEM.value and state.get("schema_metadata_request"):
            return "schema_metadata"
        if route == AgentRoute.SYSTEM.value and state.get("clarification_message"):
            return "clarification"
        if state.get("parent_child_request"):
            return "parent_child"
        if state.get("table_record_request"):
            return "table_records"
        if route == AgentRoute.CRUD_WRITE.value:
            return "crud_write"
        if route == AgentRoute.DOCUMENT_RAG.value:
            return "document_rag"
        if route == AgentRoute.HYBRID.value:
            return "hybrid"
        return "structured_read"

    def _demo_question(self, state: AgentState) -> dict[str, Any]:
        request = DemoQuestionRequest.from_state(dict(state.get("demo_question_request") or {}))
        try:
            result = execute_demo_question(
                state["db"],
                request=request,
                question=state["question"],
                memory_context=state.get("memory_context"),
            )
        except DemoQuestionError as exc:
            raise AgentOrchestrationError(
                status=exc.status,
                code=exc.code,
                message=exc.message,
            ) from exc

        data = dict(result.data or {})
        data["question"] = state["question"]
        data["route_decision"] = self._route_metadata(state)
        return {
            "route": result.route.value,
            "status": result.status.value,
            "answer": result.answer,
            "generated_sql": result.generated_sql,
            "pending_action_id": None,
            "sources": result.sources,
            "data": data,
            "graph_trace": self._append_trace(
                state,
                "demo_question",
                "Answered a high-confidence demo question deterministically using schema metadata or bounded SQLAlchemy SELECT queries; no LLM SQL was generated.",
            ),
        }

    def _schema_metadata(self, state: AgentState) -> dict[str, Any]:
        from app.services.schema_metadata_question_service import SchemaQuestionRequest

        request_data = dict(state.get("schema_metadata_request") or {})
        request = SchemaQuestionRequest(
            handled=bool(request_data.get("handled")),
            kind=request_data.get("kind"),
            requested_table=request_data.get("requested_table"),
            canonical_table=request_data.get("canonical_table"),
            requested_column=request_data.get("requested_column"),
            ambiguous_tables=tuple(request_data.get("ambiguous_tables") or ()),
        )
        try:
            role = UserRole(str(state.get("user_role") or UserRole.NORMAL_USER.value))
            result = answer_schema_metadata_question(state["db"], request=request, role=role)
        except SchemaMetadataQuestionError as exc:
            raise AgentOrchestrationError(
                status=ResponseStatus.DATABASE_UNAVAILABLE,
                code=exc.code,
                message=exc.message,
            ) from exc

        data = dict(result.data)
        data["question"] = state["question"]
        data["route_decision"] = self._route_metadata(state)
        return {
            "route": AgentRoute.SYSTEM.value,
            "status": result.status.value,
            "answer": result.answer,
            "generated_sql": None,
            "pending_action_id": None,
            "sources": [],
            "data": data,
            "graph_trace": self._append_trace(
                state,
                "schema_metadata",
                "Answered from live PostgreSQL schema metadata only; no business rows, prior filters, Ollama generation, or SQL proposal were used.",
            ),
        }

    def _table_records(self, state: AgentState) -> dict[str, Any]:
        request_data = dict(state.get("table_record_request") or {})
        request = TableRecordQuestion(
            handled=bool(request_data.get("handled")),
            canonical_table=request_data.get("canonical_table"),
            needs_clarification=bool(request_data.get("needs_clarification")),
            clarification_message=request_data.get("clarification_message"),
            limit=int(request_data.get("limit") or 50),
        )
        try:
            role = UserRole(str(state.get("user_role") or UserRole.NORMAL_USER.value))
            result = read_table_records(request=request, user_role=role)
        except TableRecordQuestionError as exc:
            raise AgentOrchestrationError(
                status=ResponseStatus.DATABASE_UNAVAILABLE,
                code=exc.code,
                message=exc.message,
            ) from exc

        data = dict(result.data)
        data["question"] = state["question"]
        data["route_decision"] = self._route_metadata(state)
        return {
            "route": AgentRoute.STRUCTURED_READ.value,
            "status": result.status.value,
            "answer": result.answer,
            "generated_sql": None,
            "pending_action_id": None,
            "sources": [
                {
                    "source_type": "database",
                    "reference": str(request.canonical_table),
                    "detail": "Rows were read through the bounded approved-table MCP tool; no generated SQL was used.",
                }
            ],
            "data": data,
            "graph_trace": self._append_trace(
                state,
                "table_records",
                "Read one approved business table through get_table_records; Ollama was not called and no SQL was generated.",
            ),
        }

    def _clarification(self, state: AgentState) -> dict[str, Any]:
        return {
            "route": AgentRoute.SYSTEM.value,
            "status": ResponseStatus.CLARIFICATION_REQUIRED.value,
            "answer": state.get("clarification_message") or "Please clarify the request.",
            "generated_sql": None,
            "pending_action_id": None,
            "sources": [],
            "data": {
                "question": state["question"],
                "clarification": {
                    "code": state.get("clarification_code"),
                    "missing_fields": list(state.get("clarification_missing_fields") or []),
                    "details": list(state.get("clarification_details") or []),
                    "resolved_tables": list(state.get("resolved_tables") or []),
                    "detected_intent": state.get("detected_intent"),
                    "ollama_called": False,
                    "sql_generated": False,
                    "database_touched": False,
                },
                "route_decision": self._route_metadata(state),
            },
            "graph_trace": self._append_trace(
                state,
                "clarification",
                "Returned a clarification question without calling Ollama, generating SQL, creating a pending action, or touching the database.",
            ),
        }

    def _structured_read(self, state: AgentState) -> dict[str, Any]:
        try:
            result = structured_read_service.execute(
                question=state["question"],
                request_id=state.get("request_id"),
                session_id=state.get("session_id"),
                memory_context=state.get("memory_context"),
                expected_tables=list(state.get("resolved_tables") or []),
            )
        except LLMOutputValidationError as exc:
            raise AgentOrchestrationError(status=ResponseStatus.VALIDATION_FAILED, code=exc.code, message=exc.message) from exc
        except LLMServiceError as exc:
            raise AgentOrchestrationError(status=ResponseStatus.LLM_UNAVAILABLE, code=exc.code, message=exc.message) from exc
        except StructuredReadError as exc:
            if exc.status == ResponseStatus.CLARIFICATION_REQUIRED:
                return {
                    "route": AgentRoute.SYSTEM.value,
                    "status": ResponseStatus.CLARIFICATION_REQUIRED.value,
                    "answer": exc.message,
                    "generated_sql": None,
                    "pending_action_id": None,
                    "sources": [],
                    "data": {
                        "question": state["question"],
                        "clarification": {
                            "code": exc.code,
                            "missing_fields": ["target_table"],
                            "resolved_tables": list(state.get("resolved_tables") or []),
                            "ollama_called": True,
                            "sql_generated": False,
                            "database_touched": False,
                        },
                        "route_decision": self._route_metadata(state),
                    },
                    "graph_trace": self._append_trace(
                        state,
                        "table_alignment_guard",
                        "Rejected model SQL because it did not include the explicitly requested table; no database read was executed.",
                    ),
                }
            raise AgentOrchestrationError(status=exc.status, code=exc.code, message=exc.message) from exc

        citations = [
            {"source_type": "database", "reference": str(table), "detail": "PostgreSQL table queried through the validated MCP read tool."}
            for table in result.source.get("tables", [])
        ]
        return {
            "status": result.status.value,
            "answer": result.answer,
            "generated_sql": result.validation.normalized_sql or result.proposal.sql,
            "sources": citations,
            "data": {
                "question": result.question,
                "route_decision": self._route_metadata(state),
                "row_count": result.row_count,
                "returned_row_count": len(result.rows),
                "rows": result.rows,
                "database_source": result.source,
                "sql_proposal_explanation": result.proposal.explanation,
                "validation": result.validation.to_dict(),
                "glossary": result.glossary,
                "model_metadata": result.model_metadata.model_dump(),
                "execution": {"tool": "execute_validated_read", "via": "local_mcp_client_facade", "executed": True, "write_execution_allowed": False},
                "query_log": result.audit,
            },
            "graph_trace": self._append_trace(state, "structured_read", "Executed one validated SELECT via the existing MCP read tool."),
        }

    @staticmethod
    def _parse_or_create_session(state: AgentState) -> str:
        db = state["db"]
        raw = state.get("session_id")
        if raw:
            try:
                parsed = UUID(raw)
            except (ValueError, TypeError) as exc:
                raise AgentOrchestrationError(
                    status=ResponseStatus.VALIDATION_FAILED,
                    code="invalid_session_id",
                    message="session_id must be a valid UUID when supplied.",
                ) from exc
            if get_active_session(db, parsed) is None:
                raise AgentOrchestrationError(
                    status=ResponseStatus.INFORMATION_NOT_AVAILABLE,
                    code="inactive_or_missing_session",
                    message="The supplied session is missing, expired, or inactive. Create a new session or omit session_id.",
                )
            return str(parsed)

        try:
            role = UserRole(state.get("user_role", UserRole.NORMAL_USER.value))
        except ValueError:
            role = UserRole.NORMAL_USER
        return str(create_session(db, user_role=role).id)

    def _crud_write(self, state: AgentState) -> dict[str, Any]:
        session_id = self._parse_or_create_session(state)
        user_role = UserRole(state.get("user_role", UserRole.NORMAL_USER.value))

        explicit_insert = detect_explicit_insert(state["question"])
        if explicit_insert is not None:
            try:
                result = crud_write_service.propose_bulk_insert(
                    state["db"],
                    session_id=UUID(session_id),
                    target_table=explicit_insert.target_table,
                    records=explicit_insert.records,
                    actor_role=user_role,
                    user_prompt=state["question"],
                    generation_metadata={
                        "requested_record_count": len(explicit_insert.records),
                        "generated_record_count": len(explicit_insert.records),
                        "source": "deterministic_explicit_insert_parser",
                        "summary": explicit_insert.summary,
                    },
                )
            except CrudWriteError as exc:
                raise AgentOrchestrationError(status=exc.status, code=exc.code, message=exc.message, details=exc.details or []) from exc

            preview_count = int(result.preview.get("record_count") or 0)
            return {
                "session_id": session_id,
                "status": ResponseStatus.PENDING_CONFIRMATION.value,
                "answer": (
                    f"Created a deterministic insert preview for `{explicit_insert.target_table}` with {preview_count} record"
                    f"{'s' if preview_count != 1 else ''}. No database row was changed; explicit admin confirmation is required."
                ),
                "generated_sql": None,
                "pending_action_id": result.pending_action["pending_action_id"],
                "sources": [],
                "data": {
                    "question": state["question"],
                    "route_decision": self._route_metadata(state),
                    "session_id": session_id,
                    "target_table": explicit_insert.target_table,
                    "pending_action": result.pending_action,
                    "preview": result.preview,
                    "validation": None,
                    "duplicate_matches": result.duplicate_matches,
                    "requested_record_count": len(explicit_insert.records),
                    "generated_record_count": len(explicit_insert.records),
                    "preview_record_count": preview_count,
                    "count_verified": len(explicit_insert.records) == preview_count,
                    "rows": result.preview.get("records", []),
                    "parser": "deterministic_explicit_insert_parser",
                    "write_execution_allowed": False,
                    "next_step": "Review the preview, then confirm as admin or cancel this exact pending action.",
                },
                "graph_trace": self._append_trace(state, "explicit_insert", "Built a deterministic single-record insert preview and reused the confirmation-gated bulk-insert path; no LLM SQL was generated."),
            }

        # Explicit random/demo generation is deterministic and schema-aware. Faker
        # produces records for the requested approved business table, foreign keys use
        # existing parent rows, and the result still enters the normal exact-count,
        # duplicate-checked confirmation path. Ollama is not asked to invent bulk rows.
        try:
            synthetic_request = parse_synthetic_data_prompt(state["question"])
        except SyntheticDataGenerationError as exc:
            raise AgentOrchestrationError(
                status=ResponseStatus.CLARIFICATION_REQUIRED,
                code=exc.code,
                message=exc.message,
                details=exc.details,
            ) from exc

        if synthetic_request is not None:
            try:
                batch = generate_synthetic_records(state["db"], synthetic_request)
                result = crud_write_service.propose_bulk_insert(
                    state["db"],
                    session_id=UUID(session_id),
                    target_table=batch.target_table,
                    records=batch.records,
                    actor_role=user_role,
                    user_prompt=state["question"],
                    generation_metadata=batch.metadata,
                )
            except SyntheticDataGenerationError as exc:
                raise AgentOrchestrationError(
                    status=ResponseStatus.CLARIFICATION_REQUIRED,
                    code=exc.code,
                    message=exc.message,
                    details=exc.details,
                ) from exc
            except CrudWriteError as exc:
                raise AgentOrchestrationError(status=exc.status, code=exc.code, message=exc.message, details=exc.details or []) from exc

            readable_table = batch.target_table.replace("_", " ")
            preview_count = int(result.preview.get("record_count") or 0)
            return {
                "session_id": session_id,
                "status": ResponseStatus.PENDING_CONFIRMATION.value,
                "answer": (
                    f"Faker generated exactly {len(batch.records)} synthetic {readable_table} record"
                    f"{'s' if len(batch.records) != 1 else ''}. The backend verified the count, schema, "
                    "foreign-key relationships, and duplicate checks. No database row was changed; explicit confirmation is required."
                ),
                "generated_sql": None,
                "pending_action_id": result.pending_action["pending_action_id"],
                "sources": [],
                "data": {
                    "question": state["question"],
                    "route_decision": self._route_metadata(state),
                    "session_id": session_id,
                    "target_table": batch.target_table,
                    "pending_action": result.pending_action,
                    "preview": result.preview,
                    "validation": None,
                    "duplicate_matches": result.duplicate_matches,
                    "requested_record_count": synthetic_request.count,
                    "generated_record_count": len(batch.records),
                    "preview_record_count": preview_count,
                    "count_verified": synthetic_request.count == len(batch.records) == preview_count,
                    "count_contract": result.preview.get("count_contract"),
                    "rows": result.preview.get("records", []),
                    "generator": "faker",
                    "synthetic_generation": batch.metadata,
                    "write_execution_allowed": False,
                    "next_step": "Review the complete evidence preview, then confirm or cancel this exact pending action.",
                },
                "graph_trace": self._append_trace(
                    state,
                    "crud_write",
                    f"Generated {len(batch.records)} schema-aware synthetic records for {batch.target_table} with Faker, then created the exact-count duplicate-checked confirmation preview; no write executed.",
                ),
            }

        try:
            proposal, validation, metadata, glossary = llm_service.generate_sql(state["question"], AgentRoute.CRUD_WRITE.value)
            try:
                assert_generated_tables_match(
                    expected_tables=list(state.get("resolved_tables") or []),
                    generated_tables=list(validation.tables or []),
                )
            except ValueError:
                return {
                    "route": AgentRoute.SYSTEM.value,
                    "status": ResponseStatus.CLARIFICATION_REQUIRED.value,
                    "answer": (
                        "The generated write did not target the table you explicitly requested, so I rejected it. "
                        "Please restate the target table and the exact values or change you want."
                    ),
                    "generated_sql": None,
                    "pending_action_id": None,
                    "sources": [],
                    "data": {
                        "question": state["question"],
                        "clarification": {
                            "code": "generated_write_table_mismatch",
                            "missing_fields": ["target_table", "record_values_or_change"],
                            "resolved_tables": list(state.get("resolved_tables") or []),
                            "generated_tables": list(validation.tables or []),
                            "ollama_called": True,
                            "sql_generated": False,
                            "database_touched": False,
                        },
                        "route_decision": self._route_metadata(state),
                    },
                    "graph_trace": self._append_trace(
                        state,
                        "table_alignment_guard",
                        "Rejected model DML because its target table did not match the explicit user request; no pending action was created.",
                    ),
                }
            result = crud_write_service.propose_sql_write(
                state["db"],
                session_id=UUID(session_id),
                sql=validation.normalized_sql or proposal.sql,
                actor_role=user_role,
                user_prompt=state["question"],
                model_metadata=metadata.model_dump(),
            )
        except LLMServiceError as exc:
            raise AgentOrchestrationError(status=ResponseStatus.LLM_UNAVAILABLE, code=exc.code, message=exc.message) from exc
        except CrudWriteError as exc:
            raise AgentOrchestrationError(status=exc.status, code=exc.code, message=exc.message, details=exc.details or []) from exc

        return {
            "session_id": session_id,
            "status": ResponseStatus.PENDING_CONFIRMATION.value,
            "answer": "The LangGraph router created a validator-approved write preview. No database row was changed; explicit confirmation is required.",
            "generated_sql": result.generated_sql,
            "pending_action_id": result.pending_action["pending_action_id"],
            "sources": [],
            "data": {
                "question": state["question"],
                "route_decision": self._route_metadata(state),
                "session_id": session_id,
                "pending_action": result.pending_action,
                "preview": result.preview,
                "validation": result.validation.to_dict() if result.validation else None,
                "duplicate_matches": result.duplicate_matches,
                "model_metadata": result.model_metadata,
                "glossary": glossary,
                "write_execution_allowed": False,
                "next_step": "Confirm or cancel this exact pending action using the existing Phase 7 CRUD endpoints.",
            },
            "graph_trace": self._append_trace(state, "crud_write", "Generated and stored a confirmation-gated DML preview; no write executed."),
        }

    def _parent_child(self, state: AgentState) -> dict[str, Any]:
        session_id = self._parse_or_create_session(state)
        user_role = UserRole(state.get("user_role", UserRole.NORMAL_USER.value))
        try:
            result = parent_child_crud_service.execute(
                state["db"],
                question=state["question"],
                session_id=UUID(session_id),
                actor_role=user_role,
                memory_context=state.get("memory_context"),
            )
        except ParentChildError as exc:
            if exc.status == ResponseStatus.CLARIFICATION_REQUIRED:
                return {
                    "route": AgentRoute.SYSTEM.value,
                    "status": exc.status.value,
                    "answer": exc.message,
                    "generated_sql": None,
                    "pending_action_id": None,
                    "sources": [],
                    "data": {
                        "question": state["question"],
                        "clarification": {
                            "code": exc.code,
                            "details": exc.details,
                            "sql_generated": False,
                            "write_executed": False,
                        },
                        "route_decision": self._route_metadata(state),
                    },
                    "graph_trace": self._append_trace(
                        state,
                        "parent_child_clarification",
                        f"Parent-child request required clarification ({exc.code}); no SQL was generated and no write was executed.",
                    ),
                }
            raise AgentOrchestrationError(
                status=exc.status,
                code=exc.code,
                message=exc.message,
                details=exc.details,
            ) from exc

        data = dict(result.data)
        data["question"] = state["question"]
        data["session_id"] = session_id
        data["route_decision"] = self._route_metadata(state)
        sources = []
        target_table = data.get("target_table")
        if result.route == AgentRoute.STRUCTURED_READ and target_table:
            sources = [{
                "source_type": "database",
                "reference": str(target_table),
                "detail": "Parent and child records resolved through approved SQLAlchemy metadata and stable business codes.",
            }]
        return {
            "route": result.route.value,
            "session_id": session_id,
            "status": result.status.value,
            "answer": result.answer,
            "generated_sql": result.generated_sql,
            "pending_action_id": result.pending_action_id,
            "sources": sources,
            "data": data,
            "graph_trace": self._append_trace(
                state,
                "parent_child_crud",
                "Resolved a typed relationship plan, verified parent business codes, and used bounded SQLAlchemy read/confirmation-gated write execution.",
            ),
        }

    def _document_rag(self, state: AgentState) -> dict[str, Any]:
        try:
            result = document_rag_service.answer_question(state["question"], top_k=state.get("top_k"))
        except DocumentRagError as exc:
            raise AgentOrchestrationError(status=exc.status, code=exc.code, message=exc.message, details=exc.details or []) from exc

        sources = [
            {
                "source_type": "document",
                "reference": match.reference,
                "detail": f"Local retrieval similarity {match.similarity:.3f}; filename/page/chunk source.",
            }
            for match in result.matches
            if not result.source_references or match.reference in result.source_references
        ]
        return {
            "status": result.status.value,
            "answer": result.answer,
            "sources": sources,
            "generated_sql": None,
            "data": {
                "question": result.question,
                "route_decision": self._route_metadata(state),
                "retrieved_chunk_count": len(result.matches),
                "retrieved_chunks": [match.to_dict() for match in result.matches],
                "source_references": result.source_references,
                "model_metadata": result.model_metadata.model_dump() if result.model_metadata else {"model": None, "attempts": 0},
                "grounding_policy": "answer_from_retrieved_document_evidence_only",
            },
            "graph_trace": self._append_trace(state, "document_rag", "Retrieved local ChromaDB evidence and generated an answer from those sources only."),
        }

    def _hybrid_evidence_fusion(self, state: AgentState) -> dict[str, Any]:
        """Combine a deterministic database read with uploaded-document RAG when possible.

        The original Phase 11 hybrid service is product-identity specific. Demo prompts now
        also ask broad combinations such as "How many active employees are in IT, and what
        onboarding tasks should they complete?". For those, use the existing deterministic
        demo-read service for the database side and the grounded RAG service for the document
        side. If no deterministic database side is available, fall back to the original
        restricted product/vendor hybrid service.
        """

        database_question = re.split(r"\s*,?\s+and\s+|\s+also\s+|\s+along\s+with\s+", state["question"], maxsplit=1, flags=re.IGNORECASE)[0].strip(" ,.;") or state["question"]
        demo_request = detect_demo_question(database_question)
        if demo_request.handled and demo_request.table and state.get("db") is not None:
            try:
                database_result = execute_demo_question(
                    state["db"],
                    request=demo_request,
                    question=database_question,
                    memory_context=state.get("memory_context"),
                )
            except DemoQuestionError as exc:
                raise AgentOrchestrationError(status=exc.status, code=exc.code, message=exc.message) from exc

            try:
                document_result = document_rag_service.answer_question(state["question"], top_k=state.get("top_k"))
            except DocumentRagError as exc:
                raise AgentOrchestrationError(status=exc.status, code=exc.code, message=exc.message, details=exc.details or []) from exc

            document_sources = [
                {
                    "source_type": "document",
                    "reference": match.reference,
                    "detail": f"Local retrieval similarity {match.similarity:.3f}; filename/page/chunk source.",
                }
                for match in document_result.matches
                if not document_result.source_references or match.reference in document_result.source_references
            ]
            database_sources = list(database_result.sources or [])
            database_ok = database_result.status == ResponseStatus.SUCCESS
            document_ok = document_result.status == ResponseStatus.SUCCESS
            combined_status = ResponseStatus.SUCCESS if database_ok or document_ok else ResponseStatus.INFORMATION_NOT_AVAILABLE
            answer_parts = []
            if database_result.answer:
                answer_parts.append(f"Database result: {database_result.answer}")
            if document_result.answer:
                answer_parts.append(f"Document result: {document_result.answer}")
            answer = "\n\n".join(answer_parts) if answer_parts else "Information not available in the current database or uploaded documents."
            return {
                "status": combined_status.value,
                "answer": answer,
                "sources": database_sources + document_sources,
                "generated_sql": database_result.generated_sql,
                "pending_action_id": None,
                "data": {
                    "question": state["question"],
                    "route_decision": self._route_metadata(state),
                    "database_result": database_result.data,
                    "document_result": {
                        "status": document_result.status.value,
                        "answer": document_result.answer,
                        "retrieved_chunk_count": len(document_result.matches),
                        "retrieved_chunks": [match.to_dict() for match in document_result.matches],
                        "source_references": document_result.source_references,
                    },
                    "grounding_policy": "database_answer_from_deterministic_sqlalchemy_and_document_answer_from_retrieved_rag_evidence",
                },
                "graph_trace": self._append_trace(
                    state,
                    "hybrid_generic_fusion",
                    "Answered the database part with deterministic SQLAlchemy handling and the document part with grounded uploaded-document RAG.",
                ),
            }

        try:
            document_matches = document_rag_service.retrieve(state["question"], top_k=state.get("top_k"))
            result = hybrid_evidence_service.fuse(
                question=state["question"],
                document_matches=document_matches,
            )
        except DocumentRagError as exc:
            raise AgentOrchestrationError(status=exc.status, code=exc.code, message=exc.message, details=exc.details or []) from exc
        except HybridEvidenceError as exc:
            raise AgentOrchestrationError(status=exc.status, code=exc.code, message=exc.message, details=exc.details or []) from exc

        citations = result.source_citations
        if not citations and document_matches:
            citations = [
                SourceCitation(
                    source_type="document",
                    reference=match.reference,
                    detail="Retrieved document evidence was available, but no verified database combination satisfied the hybrid query.",
                )
                for match in document_matches
            ]
        data = result.to_data(question=state["question"])
        data.update(
            {
                "route_decision": self._route_metadata(state),
                "retrieved_document_chunks": [match.to_dict() for match in document_matches],
                "grounding_policy": "answer_from_verified_document_product_identity_and_postgresql_vendor_price_evidence_only",
            }
        )
        trace_detail = (
            "Verified document-product identity through the restricted MCP tool and returned a grounded cross-source answer."
            if result.status == ResponseStatus.SUCCESS
            else "Retrieved preliminary document evidence but found no verified database/document combination; returned information_not_available."
        )
        return {
            "status": result.status.value,
            "answer": result.answer,
            "sources": [citation.model_dump() for citation in citations],
            "generated_sql": None,
            "pending_action_id": None,
            "data": data,
            "graph_trace": self._append_trace(state, "hybrid_evidence_fusion", trace_detail),
        }

    def _finalize(self, state: AgentState) -> dict[str, Any]:
        trace = self._append_trace(state, "finalize", "Returned the route result in the shared API response contract.")
        data = dict(state.get("data") or {})
        decision = dict(data.get("route_decision") or {})
        decision["graph_trace"] = trace
        data["route_decision"] = decision
        return {"graph_trace": trace, "data": data}

    @staticmethod
    def _route_metadata(state: AgentState) -> dict[str, Any]:
        return {
            "route": state.get("route"),
            "confidence": state.get("route_confidence"),
            "reason": state.get("routing_reason"),
            "graph_trace": state.get("graph_trace", []),
        }

    def _build_graph(self):
        builder = StateGraph(AgentState)
        builder.add_node("router", self._classify)
        builder.add_node("schema_metadata", self._schema_metadata)
        builder.add_node("demo_question", self._demo_question)
        builder.add_node("clarification", self._clarification)
        builder.add_node("table_records", self._table_records)
        builder.add_node("parent_child", self._parent_child)
        builder.add_node("structured_read", self._structured_read)
        builder.add_node("crud_write", self._crud_write)
        builder.add_node("document_rag", self._document_rag)
        builder.add_node("hybrid_evidence_fusion", self._hybrid_evidence_fusion)
        builder.add_node("finalize", self._finalize)
        builder.add_edge(START, "router")
        builder.add_conditional_edges(
            "router",
            self._route_selector,
            {
                "schema_metadata": "schema_metadata",
                "demo_question": "demo_question",
                "clarification": "clarification",
                "table_records": "table_records",
                "parent_child": "parent_child",
                "structured_read": "structured_read",
                "crud_write": "crud_write",
                "document_rag": "document_rag",
                "hybrid": "hybrid_evidence_fusion",
            },
        )
        builder.add_edge("schema_metadata", "finalize")
        builder.add_edge("demo_question", "finalize")
        builder.add_edge("clarification", "finalize")
        builder.add_edge("table_records", "finalize")
        builder.add_edge("parent_child", "finalize")
        builder.add_edge("structured_read", "finalize")
        builder.add_edge("crud_write", "finalize")
        builder.add_edge("document_rag", "finalize")
        builder.add_edge("hybrid_evidence_fusion", "finalize")
        builder.add_edge("finalize", END)
        return builder.compile()

    def run(
        self,
        *,
        question: str,
        db: Session,
        request_id: str | None,
        session_id: str | None,
        user_role: UserRole,
        top_k: int | None,
        memory_context: dict[str, Any] | None = None,
    ) -> AgentRunResult:
        try:
            result = self._graph.invoke(
                {
                    "question": question,
                    "db": db,
                    "request_id": request_id,
                    "session_id": session_id,
                    "user_role": user_role.value,
                    "top_k": top_k,
                    "memory_context": memory_context,
                    "graph_trace": [],
                }
            )
        except AgentOrchestrationError:
            raise
        except Exception as exc:
            raise AgentOrchestrationError(
                status=ResponseStatus.TOOL_FAILED,
                code="agent_graph_execution_failed",
                message="The LangGraph workflow could not complete this request.",
            ) from exc

        try:
            route = AgentRoute(str(result["route"]))
            status = ResponseStatus(str(result["status"]))
        except (KeyError, ValueError) as exc:
            raise AgentOrchestrationError(
                status=ResponseStatus.TOOL_FAILED,
                code="agent_graph_result_invalid",
                message="The LangGraph workflow returned an invalid route result.",
            ) from exc
        return AgentRunResult(
            route=route,
            status=status,
            answer=str(result.get("answer") or "Information not available."),
            data=dict(result.get("data") or {}),
            sources=[SourceCitation.model_validate(item) for item in result.get("sources") or []],
            generated_sql=result.get("generated_sql"),
            pending_action_id=result.get("pending_action_id"),
        )

    def status(self) -> dict[str, Any]:
        return {
            "phase": 16,
            "graph_engine": "langgraph",
            "graph_compiled": self._graph is not None,
            "supported_routes": [route.value for route in (AgentRoute.STRUCTURED_READ, AgentRoute.CRUD_WRITE, AgentRoute.DOCUMENT_RAG, AgentRoute.HYBRID)],
            "write_execution_allowed_without_confirmation": False,
            "synthetic_employee_generation_available": True,
            "schema_aware_synthetic_generation_available": True,
            "pre_llm_ambiguity_guard_available": True,
            "deterministic_schema_question_answering_available": True,
            "deterministic_simple_table_browse_available": True,
            "deterministic_demo_question_router_available": True,
            "semantic_parent_child_crud_available": True,
            "parent_child_supported_tables": supported_parent_child_tables(),
            "employee_experience_child_table_available": "employee_experiences" in supported_parent_child_tables(),
            "business_code_parent_resolution_available": True,
            "standalone_turn_memory_isolation_available": True,
            "unknown_target_defaulting_allowed": False,
            "generated_table_alignment_enforced": True,
            "synthetic_supported_tables": supported_synthetic_tables(),
            "hybrid_final_answer_available": True,
            "mcp_hardening_phase": 12,
            "short_term_memory_available": True,
            "admin_schema_execution_available": True,
            "audit_rollback_available": True,
            "rollback_confirmation_required": True,
            "benchmark_evaluation_available": True,
            "benchmark_runs_persisted": True,
            "final_demo_readiness_available": True,
            "release_evidence_available": True,
            "rollback_supported_original_actions": ["update", "delete"],
            "mcp_tool_count": len(__import__("app.mcp.client", fromlist=["LocalMCPClient"]).LocalMCPClient().list_tools()),
            "raw_sql_write_tool_exposed": False,
            "notes": [
                "The router is rule-first and explainable; it does not use an LLM merely to choose a route.",
                "Table/column existence and schema questions are answered from live PostgreSQL metadata before Ollama; they never query business rows or inherit old filters.",
                "Prior SQL context is attached to the model only when the current turn explicitly references earlier context; standalone questions are isolated.",
                "Ambiguous requests are returned as clarification_required before Ollama, SQL generation, pending-action creation, or database access; the system never defaults an unknown target to employees.",
                "Model SQL is rejected before execution or preview when it omits the business table explicitly requested by the user.",
                "Structured reads reuse Phase 6 validated SQL and MCP execution.",
                "CRUD requests reuse Phase 7 confirmation-gated previews and never execute a write automatically.",
                "Explicit random, synthetic, demo, seed, and populate requests use a schema-aware Faker registry across approved business tables; foreign keys reuse existing parent rows and every batch follows exact-count, duplicate checks, and confirmation-gated insertion.",
                "Parent-child prompts use an Ollama typed intent plan only; Python resolves employee, product, vendor, and customer codes, validates fields, compiles SQLAlchemy operations, and requires confirmation for writes.",
                "Document requests reuse Phase 9 local ChromaDB retrieval and evidence-grounded answers.",
                "Hybrid answers use a restricted MCP read tool and only fuse a document chunk with a PostgreSQL row when product identity is explicit.",
                "Phase 12 exposes bounded table inspection through controlled MCP tools; no unrestricted SQL tool exists.",
                "Phase 13 adds bounded persisted session memory and confirmed execution of only previously validated restricted admin schema previews.",
                "Phase 14 adds admin-only snapshot inspection and a confirmation-gated rollback path for audited UPDATE and DELETE actions; rollback never accepts raw SQL.",
                "Phase 15 persists local latency, quality, guardrail, historical OCR/ingestion, and resource measurements without executing business-table writes.",
                "Phase 16 adds a non-destructive final demo/readiness layer, a 17-feature evidence matrix, and a presentation-ready local release report.",
            ],
        }


agent_orchestrator = AgentOrchestrator()
