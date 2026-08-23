"""Application-wide constants and controlled status values."""

from enum import Enum


class ResponseStatus(str, Enum):
    """Statuses that the frontend can safely rely on."""

    SUCCESS = "success"
    PENDING_CONFIRMATION = "pending_confirmation"
    CANCELLED = "cancelled"
    CLARIFICATION_REQUIRED = "clarification_required"
    INFORMATION_NOT_AVAILABLE = "information_not_available"
    VALIDATION_FAILED = "validation_failed"
    DUPLICATE_DETECTED = "duplicate_detected"
    TOOL_FAILED = "tool_failed"
    OCR_FAILED = "ocr_failed"
    RETRIEVAL_FAILED = "retrieval_failed"
    LLM_UNAVAILABLE = "llm_unavailable"
    DATABASE_UNAVAILABLE = "database_unavailable"
    SENSITIVE_DATA_BLOCKED = "sensitive_data_blocked"


class AgentRoute(str, Enum):
    """Routes planned for the full agentic system."""

    SYSTEM = "system"
    STRUCTURED_READ = "structured_read"
    CRUD_WRITE = "crud_write"
    DOCUMENT_RAG = "document_rag"
    HYBRID = "hybrid"


class UserRole(str, Enum):
    """Temporary Phase 1 roles; real authentication is deliberately out of scope."""

    NORMAL_USER = "normal_user"
    ADMIN = "admin"


class RelationshipCardinality(str, Enum):
    """Supported parent-to-child relationship shape planned for Phase 2."""

    ONE_TO_ZERO_OR_MANY = "one_to_zero_or_many"


REQUEST_ID_HEADER = "X-Request-ID"
SESSION_ID_HEADER = "X-Session-ID"
USER_ROLE_HEADER = "X-User-Role"
