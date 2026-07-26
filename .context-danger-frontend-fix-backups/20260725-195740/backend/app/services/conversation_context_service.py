"""Deterministic conversation-reference memory for the frontend assistant.

This service deliberately resolves short follow-ups such as ``Can I see it?`` before
an LLM or SQL generator is called.  The canonical state is reconstructed from the
existing persisted Phase 7/13 tables:

* ``pending_actions`` stores the exact validated preview/bulk batch;
* ``action_logs`` stores confirmation/cancellation outcomes and affected identities;
* ``query_logs`` stores the previous user turn and whether it succeeded or failed.

No hidden model reasoning is stored and no unrestricted database rows are copied into
model prompts.  Ambiguous references are clarified instead of guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import ast
import json
import re
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session as DbSession

from app.core.constants import AgentRoute, ResponseStatus
from app.models.operations import ActionLog, PendingAction, QueryLog


_FAILURE_STATUSES = {
    ResponseStatus.CLARIFICATION_REQUIRED.value,
    ResponseStatus.INFORMATION_NOT_AVAILABLE.value,
    ResponseStatus.VALIDATION_FAILED.value,
    ResponseStatus.DUPLICATE_DETECTED.value,
    ResponseStatus.TOOL_FAILED.value,
    ResponseStatus.LLM_UNAVAILABLE.value,
    ResponseStatus.DATABASE_UNAVAILABLE.value,
}

_SHOW_REFERENCE_PATTERNS = (
    # A bare "show" is treated as a follow-up only when persisted session state exists.
    re.compile(r"^(?:show|display|list|view|see|check)(?:\s+me)?\??$", re.I),
    re.compile(r"^(?:can|could|may|would)\s+(?:you\s+)?(?:please\s+)?(?:show|display|list|let\s+me\s+see|help\s+me\s+see|view|see|check)\s+(?:me\s+)?(?:it|them|those|that|these|the\s+batch|the\s+list|the\s+records?|the\s+new\s+(?:ones|records?))\??$", re.I),
    re.compile(r"^(?:can|could|may)\s+i\s+(?:see|view|check)\s+(?:it|them|those|that|these|the\s+batch|the\s+list|the\s+records?|the\s+new\s+(?:ones|records?))\??$", re.I),
    re.compile(r"^(?:show|display|list|view|see|check)\s+(?:me\s+)?(?:it|them|those|that|these|the\s+batch|the\s+list|the\s+records?|the\s+new\s+(?:ones|records?))\??$", re.I),
    re.compile(r"^(?:what|which)\s+(?:did\s+you|was)\s+(?:add|create|generate|insert)(?:ed)?\??$", re.I),
    re.compile(r"^(?:show|display|list)\s+(?:me\s+)?(?:what|which)\s+(?:you\s+)?(?:just\s+)?(?:added|created|generated|inserted)\??$", re.I),
    re.compile(r"^(?:can|could|may)\s+i\s+(?:see|view)\s+(?:the\s+)?(?:records?|employees?|workers?|vendors?|customers?|products?|deals?)\s+(?:i|you)\s+(?:just\s+)?(?:added|created|generated|inserted)\??$", re.I),
    re.compile(r"^(?:show|display|list|view)\s+(?:me\s+)?(?:the\s+)?(?:records?|employees?|workers?|vendors?|customers?|products?|deals?)\s+(?:i|you)\s+(?:just\s+)?(?:added|created|generated|inserted)\??$", re.I),
    re.compile(r"^(?:show|display|list|view)\s+(?:me\s+)?(?:the\s+)?(?:last|latest|previous)\s+(?:created\s+|generated\s+|inserted\s+)?(?:batch|records?|list)\??$", re.I),
    re.compile(r"^what\s+about\s+(?:it|them|those|that|these)\??$", re.I),
)

_MUTATION_REFERENCE_PATTERN = re.compile(
    r"^(?P<verb>update|change|modify|edit|delete|remove|rename|set)\s+"
    r"(?P<reference>that\s+one|this\s+one|the\s+new\s+ones|the\s+new\s+one|the\s+records|the\s+record|it|them|those|that|this)"
    r"(?:\s+(?P<remainder>.+))?\??$",
    re.I,
)

_RECORD_ID_KEYS = (
    "id",
    "employee_code",
    "vendor_code",
    "customer_code",
    "product_code",
    "deal_code",
    "permission_code",
    "vendor_sku",
    "email",
    "contact_email",
)


@dataclass(frozen=True)
class ConversationResolution:
    """A short-circuit response created without invoking Ollama or SQL generation."""

    handled: bool
    route: AgentRoute = AgentRoute.SYSTEM
    status: ResponseStatus = ResponseStatus.SUCCESS
    answer: str = ""
    data: dict[str, Any] | None = None
    pending_action_id: str | None = None


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (UUID, Decimal)):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def _record_identity(record: dict[str, Any]) -> dict[str, Any]:
    identity: dict[str, Any] = {}
    for key in _RECORD_ID_KEYS:
        value = record.get(key)
        if value not in (None, ""):
            identity[key] = _json_safe(value)
            # A stable business code or primary key is sufficient by itself.
            if key == "id" or key.endswith("_code"):
                break
    if not identity:
        # Keep a deterministic non-sensitive ordinal fallback out of the model prompt.
        identity["record_reference"] = "stored_preview_record"
    return identity


def _coerce_record(value: Any) -> dict[str, Any] | None:
    """Convert persisted JSON/Python-literal record strings back into mappings safely.

    Older previews may contain stringified dictionaries.  ``ast.literal_eval`` is used
    only as a compatibility fallback; arbitrary code is never evaluated.
    """

    if isinstance(value, dict):
        return _json_safe(dict(value))
    if not isinstance(value, str):
        return None

    candidate = value.strip()
    if not candidate:
        return None

    parsed: Any
    try:
        parsed = json.loads(candidate)
    except (json.JSONDecodeError, TypeError):
        try:
            parsed = ast.literal_eval(candidate)
        except (ValueError, SyntaxError):
            return None

    return _json_safe(dict(parsed)) if isinstance(parsed, dict) else None


def _records_from_pending(action: PendingAction) -> list[dict[str, Any]]:
    payload = action.validated_payload if isinstance(action.validated_payload, dict) else {}
    preview = action.preview_data if isinstance(action.preview_data, dict) else {}
    candidates = (
        preview.get("records"),
        payload.get("records"),
        preview.get("affected_rows"),
    )
    for candidate in candidates:
        if not isinstance(candidate, list):
            continue
        records = [_coerce_record(item) for item in candidate]
        normalized = [item for item in records if item is not None]
        if normalized and len(normalized) == len(candidate):
            return normalized
    return []


def _pending_to_state(action: PendingAction) -> dict[str, Any]:
    records = _records_from_pending(action)
    payload = action.validated_payload if isinstance(action.validated_payload, dict) else {}
    preview = action.preview_data if isinstance(action.preview_data, dict) else {}
    record_count = preview.get("record_count")
    if not isinstance(record_count, int):
        record_count = preview.get("affected_row_count")
    if not isinstance(record_count, int):
        record_count = len(records)
    return {
        "pending_action_id": str(action.id),
        "target_table": action.target_table,
        "action_type": action.action_type,
        "status": action.status,
        "statement_type": payload.get("statement_type"),
        "record_count": int(record_count or 0),
        "records": records,
        "record_ids": [_record_identity(item) for item in records],
        "user_prompt": payload.get("user_prompt"),
        "generation_metadata": payload.get("generation_metadata") or preview.get("generation_metadata"),
        "created_at": action.created_at.isoformat() if action.created_at else None,
        "updated_at": action.updated_at.isoformat() if action.updated_at else None,
        "confirmed_at": action.confirmed_at.isoformat() if action.confirmed_at else None,
        "cancelled_at": action.cancelled_at.isoformat() if action.cancelled_at else None,
        "event_timestamp": action.updated_at.isoformat() if action.updated_at else (action.created_at.isoformat() if action.created_at else None),
    }


def _query_to_state(query: QueryLog) -> dict[str, Any]:
    references = query.source_references if isinstance(query.source_references, dict) else {}
    return {
        "query_id": query.id,
        "request_id": query.request_id,
        "question": query.user_prompt,
        "route": query.detected_route,
        "status": query.status,
        "answer": references.get("assistant_answer"),
        "error_code": query.error_code,
        "conversation_reference": references.get("conversation_reference"),
        "created_at": query.created_at.isoformat() if query.created_at else None,
    }


def _action_log_to_state(action: ActionLog) -> dict[str, Any]:
    affected = action.affected_record_ids if isinstance(action.affected_record_ids, dict) else {}
    return {
        "action_log_id": action.id,
        "pending_action_id": str(action.pending_action_id) if action.pending_action_id else None,
        "target_table": action.target_table,
        "action_type": action.action_type,
        "status": action.status,
        "confirmation_status": action.confirmation_status,
        "affected_record_ids": _json_safe(affected),
        "created_at": action.created_at.isoformat() if action.created_at else None,
    }


def build_conversation_state(db: DbSession, *, session_id: UUID, limit: int = 25) -> dict[str, Any]:
    """Reconstruct the current conversation subject from persisted action/query state."""

    normalized_limit = max(1, min(int(limit), 100))
    try:
        pending_actions = list(
            db.scalars(
                select(PendingAction)
                .where(PendingAction.session_id == session_id)
                .order_by(PendingAction.updated_at.desc(), PendingAction.created_at.desc())
                .limit(normalized_limit)
            ).all()
        )
        action_logs = list(
            db.scalars(
                select(ActionLog)
                .where(ActionLog.session_id == session_id)
                .order_by(ActionLog.created_at.desc())
                .limit(normalized_limit)
            ).all()
        )
        query_logs = list(
            db.scalars(
                select(QueryLog)
                .where(QueryLog.session_id == session_id)
                .order_by(QueryLog.created_at.desc())
                .limit(normalized_limit)
            ).all()
        )
    except SQLAlchemyError:
        return {
            "available": False,
            "last_query": None,
            "last_action": None,
            "last_pending_action": None,
            "last_confirmed_action": None,
            "last_created_batch": None,
            "policy": "conversation state unavailable; do not guess references",
        }

    pending_states = [_pending_to_state(item) for item in pending_actions]
    pending_by_id = {item["pending_action_id"]: item for item in pending_states}
    log_states = [_action_log_to_state(item) for item in action_logs]

    last_pending = next((item for item in pending_states if item["status"] == "pending"), None)
    last_confirmed = next((item for item in pending_states if item["status"] == "confirmed"), None)
    last_created_batch = next(
        (
            item
            for item in pending_states
            if item["action_type"] in {"insert", "bulk_insert"} and item["records"]
        ),
        None,
    )

    latest_log = log_states[0] if log_states else None
    last_action = pending_states[0] if pending_states else None
    if latest_log and latest_log.get("pending_action_id") in pending_by_id:
        linked = dict(pending_by_id[latest_log["pending_action_id"]])
        linked["action_log"] = latest_log
        linked["event_timestamp"] = latest_log.get("created_at") or linked.get("event_timestamp")
        if last_action is None or _iso_after(linked.get("event_timestamp"), last_action.get("event_timestamp")):
            last_action = linked

    latest_query = _query_to_state(query_logs[0]) if query_logs else None
    return {
        "available": bool(last_action or latest_query),
        "last_query": latest_query,
        "last_action": last_action,
        "last_pending_action": last_pending,
        "last_confirmed_action": last_confirmed,
        "last_created_batch": last_created_batch,
        "latest_action_log": latest_log,
        "affected_table": last_action.get("target_table") if last_action else None,
        "affected_ids": last_action.get("record_ids") if last_action else [],
        "policy": "persisted action-aware session state; ambiguous references must be clarified, never guessed",
    }


def _is_show_reference(question: str) -> bool:
    normalized = " ".join(question.strip().split())
    return any(pattern.fullmatch(normalized) for pattern in _SHOW_REFERENCE_PATTERNS)


def _iso_after(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    try:
        return datetime.fromisoformat(left) > datetime.fromisoformat(right)
    except ValueError:
        return left > right


def _table_label(table_name: str | None, count: int) -> str:
    if not table_name:
        return "record" if count == 1 else "records"
    singular = table_name[:-1] if table_name.endswith("s") else table_name
    return singular.replace("_", " ") if count == 1 else table_name.replace("_", " ")


def _reference_matches_action(latest_query: dict[str, Any] | None, action: dict[str, Any]) -> bool:
    if not latest_query:
        return True
    reference = latest_query.get("conversation_reference")
    if isinstance(reference, dict) and reference.get("pending_action_id") == action.get("pending_action_id"):
        return True
    return latest_query.get("route") == AgentRoute.CRUD_WRITE.value and latest_query.get("status") in {
        ResponseStatus.PENDING_CONFIRMATION.value,
        ResponseStatus.SUCCESS.value,
        ResponseStatus.CANCELLED.value,
    }


def resolve_followup_from_state(question: str, state: dict[str, Any]) -> ConversationResolution:
    """Resolve a vague follow-up from a prebuilt state; pure and unit-test friendly."""

    normalized = " ".join(question.strip().split())
    mutation_match = _MUTATION_REFERENCE_PATTERN.fullmatch(normalized)
    is_show = _is_show_reference(normalized)
    if not is_show and mutation_match is None:
        return ConversationResolution(handled=False)

    action = state.get("last_action") or state.get("last_created_batch")
    latest_query = state.get("last_query") if isinstance(state.get("last_query"), dict) else None
    latest_query_is_failed_reference = bool(
        latest_query
        and latest_query.get("status") in _FAILURE_STATUSES
        and _is_show_reference(str(latest_query.get("question") or ""))
    )

    if latest_query and latest_query.get("status") in _FAILURE_STATUSES:
        action_time = action.get("event_timestamp") if isinstance(action, dict) else None
        previous_question = str(latest_query.get("question") or "").strip()
        if (action is None or _iso_after(latest_query.get("created_at"), action_time)) and not latest_query_is_failed_reference:
            previous = previous_question or "the previous request"
            previous_answer = latest_query.get("answer") or "That request did not complete successfully."
            return ConversationResolution(
                handled=True,
                route=AgentRoute.SYSTEM,
                status=ResponseStatus.INFORMATION_NOT_AVAILABLE,
                answer=f"I cannot show records from '{previous}' because it failed. {previous_answer}",
                data={
                    "conversation_resolution": "previous_request_failed",
                    "previous_request": latest_query,
                    "rows": [],
                    "clarification_needed": False,
                },
            )

    if not isinstance(action, dict):
        return ConversationResolution(
            handled=True,
            route=AgentRoute.SYSTEM,
            status=ResponseStatus.CLARIFICATION_REQUIRED,
            answer="I do not have a previous created or modified record in this session to resolve 'it' or 'them'. Please name the table or record you want to see.",
            data={
                "conversation_resolution": "no_resolvable_reference",
                "clarification_needed": True,
                "rows": [],
            },
        )

    action_time = action.get("event_timestamp")
    if (
        latest_query
        and not latest_query_is_failed_reference
        and _iso_after(latest_query.get("created_at"), action_time)
        and not _reference_matches_action(latest_query, action)
    ):
        return ConversationResolution(
            handled=True,
            route=AgentRoute.SYSTEM,
            status=ResponseStatus.CLARIFICATION_REQUIRED,
            answer=(
                "Your last conversation contains more than one possible subject. Please specify whether you mean "
                f"the previous database answer or the {action.get('record_count', 0)} record(s) from the latest "
                f"{action.get('action_type', 'write')} action on '{action.get('target_table', 'business data')}'."
            ),
            data={
                "conversation_resolution": "multiple_possible_references",
                "clarification_needed": True,
                "candidate_action": {
                    "pending_action_id": action.get("pending_action_id"),
                    "target_table": action.get("target_table"),
                    "action_type": action.get("action_type"),
                    "record_count": action.get("record_count"),
                    "status": action.get("status"),
                },
                "previous_request": latest_query,
                "rows": [],
            },
        )

    records = action.get("records") if isinstance(action.get("records"), list) else []
    count = int(action.get("record_count") or len(records))
    table = action.get("target_table")
    label = _table_label(table, count)
    status = str(action.get("status") or "unknown")

    if mutation_match is not None:
        verb = mutation_match.group("verb").lower()
        remainder = (mutation_match.group("remainder") or "").strip()
        if count != 1:
            answer = (
                f"The last action refers to {count} {label}, so '{mutation_match.group('reference')}' is not a single safe target. "
                f"Specify the exact record identifier and what you want to {verb}."
            )
        elif not remainder:
            identity = action.get("record_ids", [{}])[0] if action.get("record_ids") else {}
            answer = f"I resolved the previous record as {identity}, but you still need to specify what field and value to {verb}."
        else:
            # Even with a remainder, a deterministic guard should not silently rewrite a write request.
            answer = (
                f"I resolved one previous {label}, but I will not guess a write target from a pronoun. "
                f"Repeat the request with its identifier and the exact change: '{verb} <record-id> {remainder}'."
            )
        return ConversationResolution(
            handled=True,
            route=AgentRoute.CRUD_WRITE,
            status=ResponseStatus.CLARIFICATION_REQUIRED,
            answer=answer,
            data={
                "conversation_resolution": "write_reference_requires_explicit_target",
                "clarification_needed": True,
                "resolved_reference": {
                    "pending_action_id": action.get("pending_action_id"),
                    "target_table": table,
                    "record_count": count,
                    "record_ids": action.get("record_ids") or [],
                    "status": status,
                },
                "rows": records,
            },
            pending_action_id=action.get("pending_action_id") if status == "pending" else None,
        )

    if not records:
        return ConversationResolution(
            handled=True,
            route=AgentRoute.CRUD_WRITE,
            status=ResponseStatus.CLARIFICATION_REQUIRED,
            answer=(
                f"I found the previous {action.get('action_type', 'write')} action on '{table}', but its stored preview does not contain a record list. "
                "Please name the record or ask for the table explicitly."
            ),
            data={
                "conversation_resolution": "action_has_no_displayable_records",
                "clarification_needed": True,
                "resolved_reference": action,
                "rows": [],
            },
        )

    if status == "pending":
        answer = f"Here are the {count} {label} from your latest pending preview. They have not been added to the database yet; confirm the pending action to insert them."
        response_status = ResponseStatus.PENDING_CONFIRMATION
        pending_id = action.get("pending_action_id")
    elif status == "confirmed":
        answer = f"Here are the {count} {label} from your latest confirmed action."
        response_status = ResponseStatus.SUCCESS
        pending_id = None
    elif status == "cancelled":
        answer = f"Here is the {count}-record preview you referred to. That action was cancelled, so these records were not added to the database."
        response_status = ResponseStatus.CANCELLED
        pending_id = None
    elif status == "expired":
        answer = f"Here is the {count}-record preview you referred to. The action expired, so these records were not added to the database."
        response_status = ResponseStatus.INFORMATION_NOT_AVAILABLE
        pending_id = None
    else:
        answer = f"Here are the {count} {label} stored with your latest action. Its current state is '{status}'."
        response_status = ResponseStatus.SUCCESS
        pending_id = None

    public_action = {
        key: action.get(key)
        for key in (
            "pending_action_id",
            "target_table",
            "action_type",
            "status",
            "record_count",
            "record_ids",
            "generation_metadata",
            "confirmed_at",
            "cancelled_at",
        )
    }
    return ConversationResolution(
        handled=True,
        route=AgentRoute.CRUD_WRITE,
        status=response_status,
        answer=answer,
        data={
            "conversation_resolution": "resolved_last_action_records",
            "clarification_needed": False,
            "resolved_reference": public_action,
            "rows": records,
            "record_count": count,
            "pending_action": public_action if status == "pending" else None,
            "preview": {"target_table": table, "record_count": count, "records": records} if status == "pending" else None,
        },
        pending_action_id=pending_id,
    )


def resolve_conversation_followup(db: DbSession, *, session_id: UUID, question: str) -> ConversationResolution:
    """Build session state and resolve one vague reference without model guessing."""

    # Fast lexical gate avoids three database queries for ordinary explicit prompts.
    normalized = " ".join(question.strip().split())
    if not _is_show_reference(normalized) and _MUTATION_REFERENCE_PATTERN.fullmatch(normalized) is None:
        return ConversationResolution(handled=False)
    state = build_conversation_state(db, session_id=session_id)
    return resolve_followup_from_state(normalized, state)


def conversation_reference_from_result(
    *,
    pending_action_id: str | None,
    route: str,
    status: str,
    data: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Create a compact query-log reference tying a turn to its persisted action."""

    payload = data if isinstance(data, dict) else {}
    pending = payload.get("pending_action") if isinstance(payload.get("pending_action"), dict) else {}
    preview = payload.get("preview") if isinstance(payload.get("preview"), dict) else {}
    resolved = payload.get("resolved_reference") if isinstance(payload.get("resolved_reference"), dict) else {}
    action_id = pending_action_id or pending.get("pending_action_id") or resolved.get("pending_action_id")
    target_table = pending.get("target_table") or preview.get("target_table") or resolved.get("target_table")
    record_count = (
        payload.get("record_count")
        or payload.get("generated_record_count")
        or preview.get("record_count")
        or resolved.get("record_count")
    )
    if not action_id and route != AgentRoute.CRUD_WRITE.value:
        return None
    return {
        "pending_action_id": str(action_id) if action_id else None,
        "target_table": target_table,
        "record_count": int(record_count) if isinstance(record_count, int) else None,
        "route": route,
        "status": status,
    }
