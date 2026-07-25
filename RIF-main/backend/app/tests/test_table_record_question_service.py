from __future__ import annotations

from app.core.constants import ResponseStatus, UserRole
from app.services.table_record_question_service import (
    TableRecordQuestion,
    detect_table_record_question,
    read_table_records,
)


class FakeMCPClient:
    def call_tool(self, name, arguments):
        assert name == "get_table_records"
        assert arguments["table_name"] == "vendors"
        assert arguments["user_role"] == UserRole.NORMAL_USER.value
        return {
            "ok": True,
            "result": {
                "retrieved": True,
                "table_name": "vendors",
                "columns": ["vendor_code", "vendor_name", "contact_email"],
                "rows": [
                    {
                        "vendor_code": "VEN-101",
                        "vendor_name": "Alpha Textiles",
                        "contact_email": "sales@alpha.example",
                    }
                ],
                "row_count": 1,
                "limit": 50,
                "offset": 0,
                "source": {"source_type": "database", "tables": ["vendors"]},
            },
        }


def test_explicit_vendor_table_data_is_deterministic_browse():
    for question in (
        "show me vendors table data",
        "show me vendors table",
        "shoe me vendors table data",
        "list vendors",
        "view vendor records",
    ):
        request = detect_table_record_question(question)
        assert request.handled is True
        assert request.needs_clarification is False
        assert request.canonical_table == "vendors"


def test_bare_vendor_table_requires_clarification():
    for question in ("vendors table", "what about vendors table"):
        request = detect_table_record_question(question)
        assert request.handled is True
        assert request.needs_clarification is True
        assert request.canonical_table == "vendors"
        assert "view rows" in (request.clarification_message or "")


def test_filtered_vendor_question_stays_in_normal_nl_sql_path():
    for question in (
        "show vendors from Chennai",
        "show active vendors",
        "vendors in Kochi",
        "count vendors",
    ):
        assert detect_table_record_question(question).handled is False


def test_bounded_mcp_result_is_returned_without_generated_sql():
    result = read_table_records(
        request=TableRecordQuestion(True, canonical_table="vendors"),
        user_role=UserRole.NORMAL_USER,
        mcp_client=FakeMCPClient(),
    )
    assert result.status == ResponseStatus.SUCCESS
    assert result.data["row_count"] == 1
    assert result.data["rows"][0]["contact_email"] == "sales@alpha.example"
    assert result.data["execution"]["ollama_called"] is False
    assert result.data["execution"]["raw_sql_accepted"] is False
