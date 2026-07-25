# Feature 17 — Parent/Child Table Implementation

## Implemented in Phase 2

Feature 17 requires a child table with **zero, one, or many** records for one parent record.

The Phase 2 PostgreSQL migration now implements:

```text
employees (parent table)
    employees.id
        ↓ one employee has zero, one, or many rows
employee_permissions (child table)
    employee_permissions.employee_id
```

## Foreign key rule

```text
employee_permissions.employee_id
    REFERENCES employees.id
    ON DELETE RESTRICT
```

`RESTRICT` is intentional. PostgreSQL will block an employee deletion while permission child rows exist. This is safer than silently deleting permissions. Phase 7 will later display all affected permission rows and require confirmation before a controlled delete workflow can continue.

## Seed test cases

After running `python -m app.db.seed`:

- `EMP-101` has zero child permission records.
- `EMP-102` has one child permission record.
- `EMP-103` has three child permission records.

Verify through:

```text
GET /api/database/employees/EMP-101/permissions
GET /api/database/employees/EMP-102/permissions
GET /api/database/employees/EMP-103/permissions
```

## Future Feature 17 work

- Phase 3: schema discovery exposes this relationship.
- Phase 4: SQL validator validates safe joins involving this child table.
- Phase 6: read queries can retrieve employee permissions.
- Phase 7: create/update/delete child rows through confirmation workflows.
- Phase 7: parent deletion preview lists affected child rows.
