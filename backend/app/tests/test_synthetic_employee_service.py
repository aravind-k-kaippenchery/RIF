from app.services.synthetic_employee_service import (
    SyntheticEmployeeGenerationError,
    generate_synthetic_employees,
    parse_synthetic_employee_prompt,
)


def test_parse_and_generate_ten_synthetic_sales_employees():
    request = parse_synthetic_employee_prompt(
        "Create 10 random synthetic employees for the Sales department in Bangalore."
    )

    assert request is not None
    assert request.count == 10
    assert request.department == "Sales"
    assert request.city == "Bangalore"

    batch = generate_synthetic_employees(request)

    assert batch.metadata["generator"] == "faker"
    assert batch.metadata["generated_record_count"] == 10
    assert len(batch.records) == 10
    assert len({record["employee_code"] for record in batch.records}) == 10
    assert len({record["email"] for record in batch.records}) == 10
    assert all(record["email"].endswith("@example.test") for record in batch.records)
    assert all(record["department"] == "Sales" for record in batch.records)
    assert all(record["city"] == "Bangalore" for record in batch.records)


def test_non_synthetic_prompt_stays_on_existing_llm_crud_path():
    assert parse_synthetic_employee_prompt("Add employee Maya Nair in Bangalore") is None


def test_synthetic_prompt_requires_explicit_count():
    try:
        parse_synthetic_employee_prompt("Create random employees for demo testing")
    except SyntheticEmployeeGenerationError as exc:
        assert exc.code == "synthetic_employee_count_required"
    else:
        raise AssertionError("Expected a controlled count-required error.")

def test_parse_add_ten_workers_randomly():
    request = parse_synthetic_employee_prompt(
        "add 10 workers randomly"
    )

    assert request is not None
    assert request.count == 10