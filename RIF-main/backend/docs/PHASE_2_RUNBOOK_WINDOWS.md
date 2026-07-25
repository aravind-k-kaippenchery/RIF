# Phase 2 Windows Runbook

## Commands after PostgreSQL installation and .env configuration

```powershell
.\venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m alembic upgrade head
python -m app.db.seed
python -m pytest -q
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

## Verification URLs

```text
http://127.0.0.1:8000/api/database/health
http://127.0.0.1:8000/api/database/tables
http://127.0.0.1:8000/api/database/summary
http://127.0.0.1:8000/api/database/employees/EMP-101/permissions
http://127.0.0.1:8000/api/database/employees/EMP-102/permissions
http://127.0.0.1:8000/api/database/employees/EMP-103/permissions
```

## Expected Feature 17 result

- EMP-101 → 0 permission records
- EMP-102 → 1 permission record
- EMP-103 → 3 permission records
