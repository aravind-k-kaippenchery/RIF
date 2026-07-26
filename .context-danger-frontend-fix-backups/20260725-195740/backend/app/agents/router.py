"""Conservative deterministic route selection for the Phase 10 LangGraph workflow.

The router is deliberately rule-first. It never uses a generative model merely to decide
which service receives a request. This makes initial routing explainable and prevents a
local model failure from changing a CRUD request into an unsafe database action.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from app.core.constants import AgentRoute


@dataclass(frozen=True)
class RouteDecision:
    """A frontend-safe route decision with an explanation."""

    route: AgentRoute
    confidence: float
    reason: str


_WRITE_VERBS = {
    "add", "create", "insert", "generate", "seed", "populate", "make", "update", "delete", "remove", "change", "modify", "set", "rename",
}
_DOCUMENT_TERMS = {
    "brochure", "document", "documents", "manual", "guide", "faq", "specification", "specifications", "warranty",
    "policy", "pdf", "docx", "upload", "uploaded", "scanned", "image", "contract", "supports", "feature", "features",
    "ticket", "tickets", "escalate", "escalated", "escalation", "response", "refund", "support", "onboarding", "compliance", "checklist", "catalog", "summary",
    "keyword", "submit", "submitted", "required", "requirements", "must",
}
_DATABASE_TERMS = {
    "employee", "employees", "worker", "workers", "vendor", "vendors", "supplier", "suppliers",
    "customer", "customers", "client", "clients", "product", "products", "permission", "permissions", "experience", "experiences", "history", "mapping", "mappings", "deal", "deals", "sales", "salary", "city", "department", "price", "prices",
    "revenue", "record", "records", "table", "tables", "under", "below", "above", "top", "highest",
}
_HYBRID_DATABASE_TERMS = {
    "vendor", "vendors", "supplier", "suppliers", "price", "prices", "cost", "costs", "under", "below",
    "above", "sales", "deal", "deals", "customer", "customers", "inventory",
}


def _tokens(question: str) -> set[str]:
    return set(re.findall(r"[a-z0-9_]+", question.lower()))


def classify_question(question: str) -> RouteDecision:
    """Choose one safe primary route without invoking an LLM.

    Priority is intentional: write verbs always win because write requests must reach the
    confirmation-gated CRUD path; hybrid terms are checked before document-only terms;
    remaining questions default to the validated structured read route.
    """

    normalized = question.strip().lower()
    tokens = _tokens(normalized)
    has_write_verb = bool(tokens & _WRITE_VERBS)
    has_document_terms = bool(tokens & _DOCUMENT_TERMS)
    has_database_terms = bool(tokens & _DATABASE_TERMS)
    has_hybrid_database_terms = bool(tokens & _HYBRID_DATABASE_TERMS)
    is_open_question = bool(re.match(r"^(what|when|why|how|who|which|explain|summarize)\b", normalized))

    if has_write_verb:
        return RouteDecision(
            route=AgentRoute.CRUD_WRITE,
            confidence=0.96,
            reason="A create, update, or delete-style verb was detected; the request must use the confirmation-gated CRUD path.",
        )

    document_only_shape = bool(
        re.search(r"\b(?:what|which)\s+documents?\b", normalized)
        or re.search(r"\b(?:unique|reference)\s+.*\bkeyword\b", normalized)
        or re.search(r"\b(?:find|search)\b.*\b(?:document|keyword|policy|faq|checklist|catalog)\b", normalized)
    )

    if has_document_terms and has_hybrid_database_terms and not document_only_shape:
        return RouteDecision(
            route=AgentRoute.HYBRID,
            confidence=0.90,
            reason="The question contains document-evidence terms plus structured business filters, so it needs the hybrid route.",
        )

    if has_document_terms:
        return RouteDecision(
            route=AgentRoute.DOCUMENT_RAG,
            confidence=0.88,
            reason="The question asks for product/document knowledge, so local document retrieval is the primary evidence source.",
        )

    if is_open_question and not has_database_terms and has_document_terms:
        return RouteDecision(
            route=AgentRoute.DOCUMENT_RAG,
            confidence=0.72,
            reason="The question is read-only and contains an uploaded-document signal, so document RAG is used.",
        )

    if has_database_terms:
        return RouteDecision(
            route=AgentRoute.STRUCTURED_READ,
            confidence=0.86,
            reason="The question requests structured company records, filters, or aggregates, so the validated database-read path is appropriate.",
        )

    return RouteDecision(
        route=AgentRoute.STRUCTURED_READ,
        confidence=0.55,
        reason="No document or write-specific signal was found; the safe default is a read-only structured-data query.",
    )
