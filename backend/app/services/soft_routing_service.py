"""Soft read-only routing helpers to reduce unnecessary clarifications.

These helpers are deliberately read-only.  They never approve inserts, updates,
deletes, confirmations, or schema changes.  Their purpose is to avoid asking the
user to restate obvious low-risk document/FAQ questions.
"""

from __future__ import annotations

import re

from app.services.request_clarification_service import resolve_business_tables

_WRITE_VERBS = {
    "add",
    "create",
    "insert",
    "generate",
    "seed",
    "populate",
    "make",
    "update",
    "delete",
    "remove",
    "change",
    "modify",
    "set",
    "rename",
    "confirm",
    "approve",
    "execute",
}

_QUESTION_STARTERS = {
    "what",
    "when",
    "where",
    "why",
    "how",
    "who",
    "which",
    "explain",
    "summarize",
    "describe",
    "tell",
}

_DOCUMENT_HINTS = {
    "document",
    "documents",
    "uploaded",
    "upload",
    "pdf",
    "txt",
    "file",
    "files",
    "faq",
    "policy",
    "guide",
    "manual",
    "contract",
    "brochure",
    "report",
    "summary",
    "keyword",
    "ticket",
    "tickets",
    "escalated",
    "escalation",
    "response",
    "support",
}

_DEPARTMENT_WORDS = {
    "finance",
    "hr",
    "it",
    "sales",
    "marketing",
    "engineering",
    "design",
    "operations",
    "support",
    "procurement",
    "admin",
}

_PEOPLE_HINTS = {
    "who",
    "works",
    "work",
    "working",
    "employee",
    "employees",
    "worker",
    "workers",
    "staff",
    "people",
    "person",
}


def _tokens(question: str) -> set[str]:
    return set(re.findall(r"[a-z0-9_]+", str(question or "").casefold()))


def is_write_like_question(question: str) -> bool:
    """Return True for requests that must remain strict and clarification-gated."""

    tokens = _tokens(question)
    return bool(tokens & _WRITE_VERBS)


def infer_readonly_business_table(question: str) -> str | None:
    """Infer an approved table only for narrow read-only personnel questions.

    Example: ``who works under finance?`` means employees filtered by department.
    This inference is intentionally narrow and never applies to writes.
    """

    if is_write_like_question(question):
        return None

    normalized = " ".join(str(question or "").casefold().split())
    tokens = _tokens(normalized)
    if tokens & _PEOPLE_HINTS and (tokens & _DEPARTMENT_WORDS or "under" in tokens or "department" in tokens):
        if re.search(r"\b(?:who|which|show|list|find|get)\b", normalized) and re.search(
            r"\b(?:works?|working|employees?|workers?|staff|people|under|department)\b",
            normalized,
        ):
            return "employees"

    return None


def should_route_open_question_to_documents(question: str) -> bool:
    """Route safe open-ended questions to uploaded-document RAG before clarification.

    This is the core over-clarification fix.  If the prompt is a read-only question
    and it does not explicitly mention an approved business table, we first try
    local uploaded-document retrieval.  If the documents do not contain the answer,
    the RAG service returns an information-not-available response instead of a
    misleading database clarification.
    """

    normalized = " ".join(str(question or "").strip().split())
    if not normalized or is_write_like_question(normalized):
        return False
    if resolve_business_tables(normalized):
        return False

    tokens = _tokens(normalized)
    first = normalized.casefold().split()[0] if normalized.split() else ""
    looks_like_question = normalized.endswith("?") or first in _QUESTION_STARTERS
    has_document_hint = bool(tokens & _DOCUMENT_HINTS)

    return looks_like_question or has_document_hint
