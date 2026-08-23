# TODO — Run Frontend and Backend

## Steps
- [x] 1. Analyze project structure and environment (PostgreSQL, Ollama, Python, Node)
- [x] 2. Create `backend/.env` from `backend/.env.example`
- [x] 3. Install frontend dependencies (`npm install` in B2B-frontend)
- [x] 4. Install backend dependencies (`pip install -r requirements.txt` — 174 packages incl. uvicorn)
- [x] 5. Start FastAPI backend on port 8000
- [x] 6. Start Vite frontend on port 5173
- [x] 7. Verify both servers respond

## Result
- Backend: `http://127.0.0.1:8000` — `/health` returns 200 (version 0.15.0)
- Frontend: `http://127.0.0.1:5173` — returns 200
- Swagger docs: `http://127.0.0.1:8000/docs`
- Both servers verified running and reachable.

