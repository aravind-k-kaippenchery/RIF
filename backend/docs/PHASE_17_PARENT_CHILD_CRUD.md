# Phase 17 — Parent–Child CRUD and Employee Work History

This phase adds a schema-grounded `employee_experiences` child table and a reusable parent–child CRUD planner.

## Safety model

The local Ollama model converts natural language into a typed semantic plan only. It does not generate or execute SQL for this workflow. The backend then:

1. resolves stable business codes such as `EMP-105`, `PRD-001`, `VND-001`, and `CUS-001`;
2. validates child fields against SQLAlchemy metadata;
3. asks one concise clarification question when required information is missing;
4. performs bounded SQLAlchemy reads;
5. creates confirmation-gated previews for every update, insert, or delete;
6. records snapshots and audit information through the existing CRUD service.

## Employee experience relationship

`employees.id` → `employee_experiences.employee_id`

The child table stores company, role, employment type, location, start/end dates, description, and current-role status. It prevents duplicate employee/company/role/start-date combinations and invalid date ranges.

## Supported relationship workflows

- `employees` → `employee_experiences`
- `employees` → `employee_permissions`
- `products` + `vendors` → `product_vendor_mappings`
- `customers` + optional `products` + optional owner `employees` → `sales_deals`

## Example prompts

### Employee experiences

- `Show the work history of EMP-105`
- `Add experience for EMP-105 at Infosys as Python Developer from 2021-01-01 to 2024-01-01`
- `Change EMP-105's Infosys role to Senior Python Developer`
- `Delete the oldest experience of EMP-105`
- `Generate 3 random previous jobs for EMP-105`

### Permissions

- `Show permissions for EMP-103`
- `Add permission VIEW_REPORTS to EMP-105 with description Can view reports`
- `Disable the VIEW_REPORTS permission for EMP-105`

### Product–vendor mappings

- `Link PRD-001 to VND-001 with quoted price 450000 and SKU NTX-001`
- `Show vendors linked to PRD-001`
- `Update the quote for PRD-001 from VND-001 to 425000`
- `Delete the mapping between PRD-001 and VND-001`

### Sales deals

- `Show deals for CUS-001`
- `Create a deal for CUS-001 for PRD-001 owned by EMP-104`
- `Update DEAL-001 probability to 75`

All writes remain pending until the user explicitly confirms the action.

## Installation

From `backend`:

```powershell
python -m alembic upgrade head
python -m app.db.seed
```

The seed command is idempotent and can be run repeatedly.
