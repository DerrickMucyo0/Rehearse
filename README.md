# Rehearse

Rehearse is a communication practice platform in development for interviews,
public speaking, negotiations, and presentations. The current foundation contains
a minimal page and a backend health check.

## Current architecture

- `frontend/`: React, TypeScript (strict mode), and Vite.
- `backend/app/`: FastAPI application served by Uvicorn.
- `tests/`: backend tests using pytest and FastAPI TestClient.
- `docs/`: reserved for future documentation.

React requests `GET /api/health` on page load. Vite's development proxy forwards
`/api` requests to `http://127.0.0.1:8000`, keeping browser requests on the same
origin without requiring CORS configuration. The only API endpoint returns
`{"status":"ok","service":"rehearse-api"}`. The page displays “Backend connected”
on success or “Backend unavailable” on failure (including a five-second timeout).
The proxy applies to the development server only.

## Local frontend setup

Use Node.js 20.19+ or 22.12+ with npm. From the project root:

```sh
cd frontend
npm ci
npm run dev
```

Open `http://localhost:5173`. Run the backend in a separate terminal.
To type-check and build: `npm run build`. To lint: `npm run lint`.

## Local backend setup

Use Python 3.10+ (verified with 3.13). From the project root:

```sh
python3 -m venv backend/.venv
source backend/.venv/bin/activate
python -m pip install -r backend/requirements-dev.txt
python -m uvicorn app.main:app --app-dir backend --reload --host 127.0.0.1 --port 8000
```

On Windows, activate with `backend\.venv\Scripts\activate` instead.
The health endpoint is at `http://127.0.0.1:8000/api/health`.

## Run backend tests

From the project root with the backend virtual environment activated:

```sh
python -m pytest
```
