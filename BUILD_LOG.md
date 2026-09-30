# Build Log

## Milestone 1 — Project Foundation

Goal:
Establish communication between the React frontend and FastAPI backend.

Implemented:
- React, TypeScript, and Vite frontend with strict type checking.
- Minimal Rehearse page with a backend status indicator.
- FastAPI `GET /api/health` endpoint and Vite development proxy.
- Backend health endpoint test, local setup instructions, and project ignore rules.

Validation:
- `backend/.venv/bin/python -m pytest -W error`: 1 passed, no warnings.
- `npm run build` in `frontend/`: strict TypeScript check and production build passed.
- `npm run lint` in `frontend/`: passed.
- Live HTTP request to `http://127.0.0.1:5173/api/health` through Vite returned
  `{"status":"ok","service":"rehearse-api"}`; the frontend HTML also served successfully.
- npm installation audit reported zero vulnerabilities.
- User manually verified the React page in the browser: it displayed
  “Backend connected” while FastAPI was running.

Notes:
- Used the existing empty `Rehearse/` directory as the project root.
- Initial downloads and local server binds failed under sandbox restrictions;
  permitted retries succeeded. pip also warned that its cache was not writable
  during the initial sandboxed attempt.
- The first test passed with a TestClient deprecation warning for `httpx`.
  Replaced it with `httpx2`. An initial 1.x constraint failed because published
  releases are 2.x; corrected the constraint and reran tests successfully.
- npm and pip showed optional upgrade notices; neither tool was upgraded.
- Final pre-commit checks: backend test passed with warnings treated as errors;
  frontend build and lint passed. Work stops at this foundation.
