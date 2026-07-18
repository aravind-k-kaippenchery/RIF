"""Phase 12 LangGraph orchestration of the four B2B agent routes.

This graph deliberately calls existing, hardened services instead of reimplementing their
security controls. SQL still goes through the Phase 4 validator/MCP tool boundary, writes
still become confirmation-gated previews, document answers remain grounded in local
ChromaDB sources, and hybrid answers use restricted MCP evidence fusion.
"""

from __future__ import annotations

from dataclasses import dataclass
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
from app.services.synthetic_employee_service import (
    SyntheticEmployeeGenerationError,
    generate_synthetic_employees,
    parse_synthetic_employee_prompt,
)
from app.services.structured_read_service import StructuredReadError, structured_read_service


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
    def _append_trace(state: AgentState, node: str, detail: str) -> list[dict[str, Any]]:
        trace = list(state.get("graph_trace") or [])
        trace.append({"node": node, "detail": detail})
        return trace

    def _classify(self, state: AgentState) -> dict[str, Any]:
        decision: RouteDecision = classify_question(state["question"])
        return {
            "route": decision.route.value,
            "route_confidence": decision.confidence,
            "routing_reason": decision.reason,
            "graph_trace": self._append_trace(state, "router", f"Selected {decision.route.value}."),
        }

    @staticmethod
    def _route_selector(state: AgentState) -> Literal["structured_read", "crud_write", "document_rag", "hybrid"]:
        route = state.get("route", AgentRoute.STRUCTURED_READ.value)
        if route == AgentRoute.CRUD_WRITE.value:
            return "crud_write"
        if route == AgentRoute.DOCUMENT_RAG.value:
            return "document_rag"
        if route == AgentRoute.HYBRID.value:
            return "hybrid"
        return "structured_read"

    def _structured_read(self, state: AgentState) -> dict[str, Any]:
        try:
            result = structured_read_service.execute(
                question=state["question"],
                request_id=state.get("request_id"),
                session_id=state.get("session_id"),
                memory_context=state.get("memory_context"),
            )
        except LLMOutputValidationError as exc:
            raise AgentOrchestrationError(status=ResponseStatus.VALIDATION_FAILED, code=exc.code, message=exc.message) from exc
        except LLMServiceError as exc:
            raise AgentOrchestrationError(status=ResponseStatus.LLM_UNAVAILABLE, code=exc.code, message=exc.message) from exc
        except StructuredReadError as exc:
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

        # A request such as "Create 10 random synthetic employees" does not need the
        # LLM to invent ten sets of field values. Generate safe demo-only records, then
        # pass them through the existing Phase 7 bulk-preview/duplicate/confirmation flow.
        try:
            synthetic_request = parse_synthetic_employee_prompt(state["question"])
        except SyntheticEmployeeGenerationError as exc:
            raise AgentOrchestrationError(
                status=ResponseStatus.CLARIFICATION_REQUIRED,
                code=exc.code,
                message=exc.message,
            ) from exc

        if synthetic_request is not None:
            try:
                batch = generate_synthetic_employees(synthetic_request)
                result = crud_write_service.propose_bulk_insert(
                    state["db"],
                    session_id=UUID(session_id),
                    target_table="employees",
                    records=batch.records,
                    actor_role=user_role,
                    user_prompt=state["question"],
                    generation_metadata=batch.metadata,
                )
            except SyntheticEmployeeGenerationError as exc:
                raise AgentOrchestrationError(
                    status=ResponseStatus.CLARIFICATION_REQUIRED,
                    code=exc.code,
                    message=exc.message,
                ) from exc
            except CrudWriteError as exc:
                raise AgentOrchestrationError(status=exc.status, code=exc.code, message=exc.message, details=exc.details or []) from exc

            return {
                "session_id": session_id,
                "status": ResponseStatus.PENDING_CONFIRMATION.value,
                "answer": "Faker generated a synthetic employee batch and the backend completed duplicate checks. No database row was changed; explicit confirmation is required.",
                "generated_sql": None,
                "pending_action_id": result.pending_action["pending_action_id"],
                "sources": [],
                "data": {
                    "question": state["question"],
                    "route_decision": self._route_metadata(state),
                    "session_id": session_id,
                    "pending_action": result.pending_action,
                    "preview": result.preview,
                    "validation": None,
                    "duplicate_matches": result.duplicate_matches,
                    "generated_record_count": len(batch.records),
                    "generator": "faker",
                    "synthetic_generation": batch.metadata,
                    "write_execution_allowed": False,
                    "next_step": "Confirm or cancel this exact pending action using the existing Phase 7 CRUD endpoints.",
                },
                "graph_trace": self._append_trace(
                    state,
                    "crud_write",
                    "Generated synthetic employee records with Faker, then created the existing duplicate-checked confirmation preview; no write executed.",
                ),
            }

        try:
            proposal, validation, metadata, glossary = llm_service.generate_sql(state["question"], AgentRoute.CRUD_WRITE.value)
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
        """Fuse document and database facts only through verified product identity.

        ChromaDB supplies the candidate document chunks. The Phase 11 restricted MCP tool
        then returns vendor/product/price facts only where a product name or code is
        explicitly present in those chunks. This prevents a model from inventing a join.
        """
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
                "structured_read": "structured_read",
                "crud_write": "crud_write",
                "document_rag": "document_rag",
                "hybrid": "hybrid_evidence_fusion",
            },
        )
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
                "Structured reads reuse Phase 6 validated SQL and MCP execution.",
                "CRUD requests reuse Phase 7 confirmation-gated previews and never execute a write automatically.",
                "Requests for random synthetic employees use Faker and then follow the same duplicate-check and confirmation-gated bulk insert path; they do not generate real people or bypass CRUD safeguards.",
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
