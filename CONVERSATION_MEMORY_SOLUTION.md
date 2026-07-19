# Conversation Memory and Ambiguity Protection

This patch fixes action-focused follow-up messages without allowing Ollama to guess what a pronoun means.

## What changed

- `Can I see it?`, `show them`, `show what you just added`, and similar requests are resolved against the exact persisted pending/confirmed action.
- The exact generated batch remains in `pending_actions.validated_payload.records` and `preview_data.records`.
- Confirmation logs now store stable affected record identities in `action_logs.affected_record_ids.record_ids`.
- Failed requests are stored in session query history, so a later `show it` reports that the previous operation failed instead of falling back to an older result.
- `update that one`, `delete them`, and similar vague write requests return `clarification_required`. They are never rewritten into a guessed write.
- Session memory now includes both query and action events, while still excluding credentials and hidden reasoning.
- The frontend displays the exact resolved batch (up to the existing maximum of 50 records) and the record count.
- A debugging endpoint is available at:

```text
GET /api/sessions/{session_id}/context
```

## Files added

```text
backend/app/services/conversation_context_service.py
backend/app/tests/test_conversation_context_service.py
CONVERSATION_MEMORY_SOLUTION.md
```

## Files updated

```text
backend/app/api/routes/frontend.py
backend/app/api/routes/crud.py
backend/app/api/routes/sessions.py
backend/app/services/session_memory_service.py
backend/app/services/crud_write_service.py
B2B-frontend/src/App.tsx
B2B-frontend/src/styles.css
```

## No migration required

The solution uses the existing persisted tables:

- `pending_actions`
- `action_logs`
- `query_logs`
- `sessions`

The JSONB columns already present in these tables are sufficient for this phase.

## Demo

### Pending batch

```text
User: Create 10 random employees
Assistant: Pending preview created for 10 employees.
User: Can I see it?
Assistant: Here are the 10 employees from your latest pending preview. They have not been added yet.
```

The same pending action ID is returned again, so the user may confirm it.

### Confirmed batch

```text
User: Confirm
Assistant: Confirmed write executed successfully.
User: Show them
Assistant: Here are the 10 employees from your latest confirmed action.
```

### Failed operation

```text
User: Create 10 random employees
Assistant: The operation fails validation.
User: Can I see it?
Assistant: I cannot show records from the previous request because it failed.
```

The assistant does not return an older Bangalore/department query.

### Ambiguous write

```text
User: Update that one
Assistant: The last action contains 10 records. Specify the exact record identifier and the field/value to update.
```

## Run

Backend:

```powershell
cd C:\Users\hp\Downloads\rif\POSTGRRE-main\backend
.\venv\Scripts\activate
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Frontend:

```powershell
cd C:\Users\hp\Downloads\rif\POSTGRRE-main\B2B-frontend
npm install
npm run dev
```

## Tests completed

```text
Backend: 154 passed
Frontend: production build passed
```
