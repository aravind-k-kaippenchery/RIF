# B2B Intelligence — Frontend

A React + Vite frontend for the B2B Product Intelligence Assistant.

## What is included

- Cinematic local-workspace access screen using `Space Grotesk`, `Manrope`, and `JetBrains Mono`.
- Animated background image in `public/images/login-code.jpg`, with a code-to-message visual stream, scan line, grid, aurora, boot console, and kinetic text reveal.
- Live FastAPI integration through `VITE_API_BASE_URL`.
- Role-aware local access via `sessionStorage` and the `X-User-Role` backend header.
- Command Center, AI Assistant, Data Explorer, Documents & OCR, Knowledge Base/RAG, Activity & Audit, Benchmarks, Demo Center, and Settings.
- Controlled confirmation UX for backend-provided pending CRUD actions; the UI never auto-confirms writes.
- No raw SQL editor.

## Setup

1. Place this `frontend` folder beside your existing `backend` folder:

```text
B2B/
├── backend/
└── frontend/
```

2. Create a local `.env` file from the example:

```powershell
Copy-Item .env.example .env
```

The default should remain:

```env
VITE_API_BASE_URL=http://127.0.0.1:8000
```

3. Install and run:

```powershell
cd frontend
npm install
npm run dev
```

Open the address Vite prints, normally `http://127.0.0.1:5173`.

## Backend prerequisite

Run the FastAPI backend in a second terminal:

```powershell
cd ..\backend
.\venv\Scripts\Activate.ps1
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

The backend CORS configuration must include `http://localhost:5173` or `http://127.0.0.1:5173`.

## Important local-access note

The opening screen is a local workspace access gate, not real password authentication. It saves the name, email, and selected backend role in session storage. Every frontend request sends the matching `X-User-Role` value expected by the existing backend.

## Login background

The provided laptop/code photo was added at:

```text
public/images/login-code.jpg
```

It is animated with a slow camera drift, background grid, scan line, aurora glows, code-to-message visual overlay, and a mobile-responsive fallback. Replace this asset anytime with another image using the same filename.
