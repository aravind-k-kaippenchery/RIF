from app.services.parent_child_crud_service import (
    _parse_new_employee_with_deferred_experience,
    looks_like_parent_child_request,
    parent_child_crud_service,
)


def test_show_products_and_their_vendors_is_parent_child_read_without_ollama():
    question = "Show products and their vendors."

    assert looks_like_parent_child_request(question) is True
    plan, metadata = parent_child_crud_service._generate_plan(question)

    assert plan.operation == "read"
    assert plan.child_table == "product_vendor_mappings"
    assert plan.parent_references == []
    assert metadata["ollama_called"] is False
    assert metadata["source"] == "deterministic_product_vendor_parser"


def test_product_vendor_create_accepts_ids_and_quoted_price_without_ollama():
    question = "Add vendor ID 1 as a supplier for product ID 1 with quoted price 450000."

    assert looks_like_parent_child_request(question) is True
    plan, metadata = parent_child_crud_service._generate_plan(question)

    assert plan.operation == "create"
    assert plan.child_table == "product_vendor_mappings"
    assert {ref.role for ref in plan.parent_references} == {"product", "vendor"}
    assert plan.values["quoted_price"] == "450000"
    assert metadata["ollama_called"] is False


def test_employee_experience_create_accepts_emp_space_and_duration_without_ollama():
    question = "Add experience for EMP 105 at Infosys as Python Developer for 2 years."

    assert looks_like_parent_child_request(question) is True
    plan, metadata = parent_child_crud_service._generate_plan(question)

    assert plan.operation == "create"
    assert plan.child_table == "employee_experiences"
    assert plan.parent_references[0].role == "employee"
    assert plan.parent_references[0].code == "EMP-105"
    assert plan.values["company_name"] == "Infosys"
    assert plan.values["job_title"] == "Python Developer"
    assert plan.values["start_date"]
    assert plan.values["end_date"]
    assert metadata["ollama_called"] is False


def test_combined_new_employee_and_experience_is_staged_parent_first():
    question = (
        "Create an employee named Arjun Menon with email arjun.menon@example.com, "
        "department IT, city Kochi, active status, and add experience at Infosys "
        "as Python Developer for 2 years."
    )

    assert looks_like_parent_child_request(question) is True
    parsed = _parse_new_employee_with_deferred_experience(question)

    assert parsed is not None
    assert parsed["target_table"] == "employees"
    assert parsed["record"]["first_name"] == "Arjun"
    assert parsed["record"]["last_name"] == "Menon"
    assert parsed["record"]["email"] == "arjun.menon@example.com"
    assert parsed["record"]["department"] == "It"
    assert parsed["record"]["city"] == "Kochi"
    assert parsed["deferred_child"]["child_table"] == "employee_experiences"
    assert "Add experience for" in parsed["deferred_child"]["next_prompt"]
