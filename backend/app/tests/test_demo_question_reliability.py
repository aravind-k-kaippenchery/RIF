from app.agents.router import classify_question
from app.core.constants import AgentRoute
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models.business import Employee
from app.services.demo_question_service import detect_demo_question, execute_demo_question
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
        "Group employees by department and show the count in each department.": ("employee_count_by_department", "employees"),
        "Show a department-wise employee count.": ("employee_count_by_department", "employees"),
        "Show employees whose salary is greater than 50000.": ("employee_list", "employees"),
        "Show the top 5 highest-paid employees.": ("employee_list_by_salary_desc", "employees"),
        "What is the average salary of employees?": ("employee_average_salary", "employees"),
        "Count employees per city.": ("employee_count_by_city", "employees"),
        "Show the employee count for each status.": ("employee_count_by_status", "employees"),
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


def test_department_grouping_executes_aggregate_query_and_reports_total():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Employee.__table__.create(engine)
    with Session(engine) as db:
        db.add_all(
            [
                Employee(employee_code="E1", first_name="A", last_name="One", email="a@example.com", department="HR", city="Kochi", company_name="Acme", salary=1, employment_status="active"),
                Employee(employee_code="E2", first_name="B", last_name="Two", email="b@example.com", department="IT", city="Kochi", company_name="Acme", salary=1, employment_status="active"),
                Employee(employee_code="E3", first_name="C", last_name="Three", email="c@example.com", department="IT", city="Bangalore", company_name="Acme", salary=1, employment_status="inactive"),
            ]
        )
        db.commit()
        question = "Group employees by department and show the count in each department."
        result = execute_demo_question(db, request=detect_demo_question(question), question=question)

    assert result.generated_sql == "SELECT department, count(*) AS employee_count FROM employees GROUP BY department ORDER BY department"
    assert result.data["rows"] == [
        {"department": "HR", "employee_count": 1},
        {"department": "IT", "employee_count": 2},
    ]
    assert result.data["employee_total"] == 3
    assert result.answer == "Employee count by department: HR: 1, IT: 2. Total: 3."


def test_employee_salary_filter_is_not_silently_dropped():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Employee.__table__.create(engine)
    with Session(engine) as db:
        db.add_all(
            [
                Employee(employee_code="E1", first_name="A", last_name="One", email="a@example.com", department="HR", city="Kochi", company_name="Acme", salary=45000, employment_status="active"),
                Employee(employee_code="E2", first_name="B", last_name="Two", email="b@example.com", department="IT", city="Kochi", company_name="Acme", salary=65000, employment_status="active"),
            ]
        )
        db.commit()
        question = "Show employees whose salary is greater than 50000."
        request = detect_demo_question(question)
        result = execute_demo_question(db, request=request, question=question)

    assert request.filters == {"salary_gt": 50000.0}
    assert result.generated_sql == "SELECT * FROM employees WHERE salary > 50000.0 ORDER BY id LIMIT 50"
    assert result.data["row_count"] == 1
    assert result.data["rows"][0]["employee_code"] == "E2"


def test_highest_paid_and_average_salary_queries_are_deterministic():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Employee.__table__.create(engine)
    with Session(engine) as db:
        db.add_all(
            [
                Employee(employee_code="E1", first_name="A", last_name="One", email="a@example.com", department="HR", city="Kochi", company_name="Acme", salary=45000, employment_status="active"),
                Employee(employee_code="E2", first_name="B", last_name="Two", email="b@example.com", department="IT", city="Kochi", company_name="Acme", salary=65000, employment_status="active"),
            ]
        )
        db.commit()
        top_question = "Show the top 5 highest-paid employees."
        top_result = execute_demo_question(db, request=detect_demo_question(top_question), question=top_question)
        avg_question = "What is the average salary of employees?"
        avg_result = execute_demo_question(db, request=detect_demo_question(avg_question), question=avg_question)

    assert top_result.generated_sql == "SELECT * FROM employees ORDER BY salary DESC, id LIMIT 5"
    assert top_result.data["rows"][0]["employee_code"] == "E2"
    assert avg_result.generated_sql == "SELECT avg(salary) AS average_salary FROM employees"
    assert avg_result.data["rows"] == [{"average_salary": 55000.0}]
