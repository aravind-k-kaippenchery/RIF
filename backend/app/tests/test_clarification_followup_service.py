from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from app.core.constants import ResponseStatus
from app.models.operations import QueryLog
from app.services.clarification_followup_service import rewrite_short_clarification_reply


class FakeScalarDB:
    def __init__(self, item):
        self.item = item

    def scalar(self, _statement):
        return self.item


def _query(
    prompt: str,
    status: str = ResponseStatus.CLARIFICATION_REQUIRED.value,
    *,
    conversation_reference: dict | None = None,
):
    item = QueryLog(
        request_id="test-request",
        session_id=uuid4(),
        user_prompt=prompt,
        detected_route="system",
        status=status,
        latency_ms=1,
        source_references={"conversation_reference": conversation_reference} if conversation_reference else {},
        created_at=datetime.now(timezone.utc),
    )
    return item


def test_read_reply_after_bare_table_becomes_explicit_row_read():
    rewritten = rewrite_short_clarification_reply(
        FakeScalarDB(_query("employees")),
        session_id=uuid4(),
        question="read",
    )
    assert rewritten == "show rows from employees"


def test_columns_reply_after_bare_table_becomes_schema_question():
    rewritten = rewrite_short_clarification_reply(
        FakeScalarDB(_query("vendors table")),
        session_id=uuid4(),
        question="columns",
    )
    assert rewritten == "what columns exist in vendors"


def test_unrelated_reply_is_not_rewritten():
    assert rewrite_short_clarification_reply(FakeScalarDB(_query("employees")), session_id=uuid4(), question="hello") is None
    assert rewrite_short_clarification_reply(FakeScalarDB(_query("employees", ResponseStatus.SUCCESS.value)), session_id=uuid4(), question="read") is None


def test_synthetic_pronoun_uses_live_table_from_successful_schema_turn(monkeypatch):
    monkeypatch.setattr(
        "app.services.clarification_followup_service.supported_synthetic_tables",
        lambda: [{"table_name": "orders"}, {"table_name": "manager_demo_shipments"}],
    )
    query = _query(
        "Do we have an orders table?",
        ResponseStatus.SUCCESS.value,
        conversation_reference={"reference_type": "table_metadata", "target_table": "orders"},
    )

    rewritten = rewrite_short_clarification_reply(
        FakeScalarDB(query),
        session_id=uuid4(),
        question="insert 5 synthetic records in it",
    )

    assert rewritten == "insert 5 synthetic records into orders table"


def test_short_live_table_reply_completes_pending_synthetic_request(monkeypatch):
    monkeypatch.setattr(
        "app.services.clarification_followup_service.supported_synthetic_tables",
        lambda: [{"table_name": "orders"}, {"table_name": "manager_demo_shipments"}],
    )
    query = _query(
        "Generate 5 synthetic records",
        conversation_reference={
            "reference_type": "clarification",
            "clarification_code": "synthetic_target_table_required",
            "missing_fields": ["target_table"],
        },
    )

    rewritten = rewrite_short_clarification_reply(
        FakeScalarDB(query),
        session_id=uuid4(),
        question="orders",
    )

    assert rewritten == "Generate 5 synthetic records into orders table"


def test_synthetic_target_is_not_reused_when_it_is_no_longer_live(monkeypatch):
    monkeypatch.setattr(
        "app.services.clarification_followup_service.supported_synthetic_tables",
        lambda: [{"table_name": "manager_demo_shipments"}],
    )
    query = _query(
        "Do we have an orders table?",
        ResponseStatus.SUCCESS.value,
        conversation_reference={"reference_type": "table_metadata", "target_table": "orders"},
    )

    assert rewrite_short_clarification_reply(
        FakeScalarDB(query),
        session_id=uuid4(),
        question="insert 5 synthetic records in it",
    ) is None
