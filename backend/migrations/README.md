# Alembic migrations

Do not edit an already-merged migration file.

Phase 2 contains the first baseline migration:

```text
20260623_0001_phase2_postgres_data_layer.py
```

Apply it with:

```powershell
python -m alembic upgrade head
```

Check the applied revision with:

```powershell
python -m alembic current
```

Phase 17 adds the employee work-history child table:

```text
20260721_0002_employee_experiences_parent_child.py
```

After applying the solution run:

```powershell
python -m alembic upgrade head
python -m app.db.seed
```
