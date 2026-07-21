# Generalized schema-aware Faker

Synthetic bulk generation is handled before Ollama. It only activates for explicit
`random`, `synthetic`, `sample`, `demo`, `faker`, `generate`, `seed`, or `populate`
requests and always creates a confirmation preview before any database write.

## Supported business tables

- `employees`
- `vendors`
- `customers`
- `products`
- `sales_deals`
- `employee_permissions`
- `product_vendor_mappings`

## Example prompts

```text
Create 10 random employees
Generate 8 vendors
Create 5 random customers in Chennai
Generate 6 sample products in Textile Automation category
Create 4 synthetic sales deals
Generate 3 permissions for employee EMP-101
Populate 5 random product vendor mappings
Add 6 random people into the vendors table
```

The requested count must be between 1 and 50 and is passed through the existing exact
count contract: requested, generated, previewed, stored, inserted, returned, and displayed
counts must match.

## Relationship rules

- Employee permissions select existing employees. A prompt can specify an employee code.
- Sales deals require at least one existing customer and reuse existing products/employees
  when available. Customer, product, and employee codes can be supplied in the prompt.
- Product-vendor mappings select only unused existing product/vendor pairs. If there are
  not enough unique unused combinations, the request returns a clarification error rather
  than generating invalid rows.

## API

`POST /api/crud/generate-propose`

```json
{
  "session_id": "SESSION_UUID",
  "target_table": "vendors",
  "count": 5,
  "constraints": {
    "city": "Kochi",
    "category": "Textile Automation"
  },
  "user_prompt": "Generate 5 random vendors in Kochi"
}
```

The legacy `POST /api/crud/generate-employees-propose` endpoint remains available.

## Future tables

An approved future table is automatically eligible for the conservative schema fallback
once it is present in SQLAlchemy metadata and added to `schema_registry.BUSINESS_TABLES`.
The fallback fills ordinary required columns and existing foreign keys from metadata.
Complex tables can register a dedicated generator with:

```python
@register_synthetic_profile("future_table")
def generate_future_table(db, faker, request, batch_id):
    ...
```

Operational tables remain protected and cannot use Faker generation.
