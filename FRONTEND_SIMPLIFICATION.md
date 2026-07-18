# Frontend Simplification

The frontend now exposes five primary workspace screens:

1. **Command Center** — health, readiness, activity, quick actions, and admin benchmark summary.
2. **AI Assistant** — Ask, Document Search, Safe Actions, Sessions, and Pipeline Evidence tabs.
3. **Documents & OCR** — upload, extraction, indexing, document details, extracted text, and chunk inspection.
4. **Data Explorer** — Data, Schema, and Relationships tabs.
5. **Activity & Audit** — action and query history for administrators.

## Merged

- Knowledge Base → AI Assistant / Document Search
- CRUD Queue → AI Assistant / Safe Actions
- Duplicate Detection → AI Assistant / Safe Actions
- Session Center → AI Assistant / Sessions
- Pipeline & Evidence → AI Assistant / Pipeline Evidence
- Schema Explorer → Data Explorer / Schema
- Relationship Explorer → Data Explorer / Relationships
- Benchmark summary → Command Center
- Chunk Explorer → document details drawer

## Removed

- Rollback Center standalone page
- Schema Console standalone page
- Benchmarks standalone page
- Settings standalone page

The Demo Center remains a hidden administrator route accessible from the Command Center quick action. Backend endpoints were not changed.

## Verification

Run from `B2B-frontend`:

```powershell
npm install
npm run build
```

The simplified frontend passes the TypeScript and Vite production build.
