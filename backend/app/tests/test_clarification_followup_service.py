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


def _query(prompt: str, status: str = ResponseStatus.CLARIFICATION_REQUIRED.value):
    item = QueryLog(
        request_id="test-request",
        session_id=uuid4(),
        user_prompt=prompt,
        detected_route="system",
        status=status,
        latency_ms=1,
        source_references={},
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
