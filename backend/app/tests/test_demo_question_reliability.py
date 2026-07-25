from app.agents.router import classify_question
from app.core.constants import AgentRoute
from app.services.demo_question_service import detect_demo_question
from app.services.request_clarification_service import analyze_request_clarity


def test_common_database_demo_questions_are_deterministic():
    cases = {
        "Show approved vendors.": ("vendor_list", "vendors"),
        "Who works under Finance?": ("employee_list", "employees"),
        "Show products with price greater than 500.": ("product_list", "products"),
        "Show active employees in IT.": ("employee_list", "employees"),
        "Show vendors whose company name contains Tech.": ("vendor_list", "vendors"),
        "How many employees are there?": ("employee_count", "employees"),
        "Count employees by department.": ("employee_count_by_department", "employees"),
        "Show orders placed recently.": ("recent_sales_deals_alias_orders", "sales_deals"),
    }
    for question, expected in cases.items():
        request = detect_demo_question(question)
        assert request.handled, question
        assert (request.kind, request.table) == expected


def test_business_tables_and_capabilities_are_system_answers():
    for question in ["Which tables are business tables?", "What can you do?"]:
        request = detect_demo_question(question)
        assert request.handled
        assert request.route == AgentRoute.SYSTEM


def test_open_document_questions_do_not_force_database_clarification():
    decision = analyze_request_clarity("When should a ticket be escalated?")
    assert decision.needs_clarification is False
    route = classify_question("When should a ticket be escalated?")
    assert route.route == AgentRoute.DOCUMENT_RAG


def test_write_requests_still_use_normal_safety_path():
    request = detect_demo_question("Update employee 5 salary to 70000")
    assert not request.handled
