from app.prompts.phase5 import render_sql_prompt
from app.services.schema_registry import get_schema_contract


def test_default_projection_names_match_real_vendor_customer_product_columns():
    prompt = render_sql_prompt(
        question="show vendors",
        route="structured_read",
        schema_contract=get_schema_contract(),
        glossary={},
        memory_context=None,
    )
    assert "vendor_code, vendor_name, contact_email, phone, city, country, category, status" in prompt
    assert "customer_code, customer_name, contact_email, phone, city, country, industry, status" in prompt
    assert "product_code, product_name, category, description, list_price, is_active" in prompt
    assert "vendor_code, vendor_name, city, email, phone, company_name" not in prompt
    assert "product_code, product_name, category, price, vendor_id" not in prompt
