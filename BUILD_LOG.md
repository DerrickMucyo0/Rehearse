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

## Milestone 2 — Interview Session Engine

Goal:
Build a deterministic text interview flow from session creation to completion.

Built:
- UUID-based session creation, retrieval, and answer submission endpoints.
- Five fixed questions, ordered answers, and active/completed state.
- Pydantic validation and 404/409/422 responses for invalid operations.
- Minimal interview form, progress, completion state, and visible request errors.
- Preserved the health endpoint and frontend backend-status indicator.

Design decisions:
- Routes delegate state management to a small in-memory service. Storage can be
  replaced behind its start/get/submit interface without rewriting route behavior.
- A lock protects each state transition; responses are detached copies.
- Answers include the current question index to reject duplicate/stale writes.
- The frontend checks server state before retrying an answer, retains text on
  failure, disables pending submissions, and applies a ten-second request timeout.
- Whitespace is trimmed; blank, non-string, and over-10,000-character answers are rejected.
- Completed sessions have no current question; their index equals the question count.
- No runtime dependencies added; frontend regression testing adds Vitest, jsdom,
  React Testing Library, and Playwright. Development stays on `feat/interview-session`.

Verification:
- `backend/.venv/bin/python -m pytest -W error`: 24 passed, no warnings.
- Frontend build/type check and lint: passed.
- Tests cover health, creation/retrieval, unique IDs, session independence,
  advancement, final completion, immutable state after rejected submissions,
  stale/future indices, unknown sessions, malformed UUIDs, and invalid answers/bodies.
- User manually verified the complete flow in Chrome; see final verification below.

Problems encountered:
- Initial API/interface checks passed; regression-test development and the
  completion investigation are recorded below.
- Lost answer responses could otherwise cause an accidental second submission;
  index validation and frontend state reconciliation handle this case.

Current limitations:
- Process-local sessions disappear on restart/reload and require a single worker.
- No persistence, expiry, session listing, or browser-refresh recovery. An abandoned
  session remains in memory until the backend stops.
- A lost creation response may leave an unused session; starting again creates another.
- No authentication, AI, voice, recording, scoring, analytics, or database.
- Work is limited to Issue #1; no next milestone started.

### Completion-screen follow-up

- Investigated the reported return to the initial screen after answer five.
  A direct TestClient run confirmed HTTP 200 with `status: completed`, five
  answers, index 5, and `current_question: null`.
- The frontend regression reproduction retained the completed session; no
  automatic state reset was reproduced. The existing completion screen reused
  “Start Interview”, making its action indistinguishable from the initial action.
- Updated only completion wording to “Interview Complete”, confirmation that all
  questions were completed, and “Start New Interview”. Backend logic is unchanged.
- Added Vitest, jsdom, and React Testing Library as development dependencies and
  `npm test`. Two tests exercise all five submissions through the frontend API
  helper with mocked HTTP responses, explicit restart, and failed restart while
  preserving completion. Both failed on the old wording before the change.
- Initial type checking caught an unsupported `exact` option in the new role
  queries; removed it (string role names already match exactly).
- At this stage, browser recheck was still required; the reported state reset
  was not reproduced in the automated environment. Final verification follows.
- Final checks: backend 24 passed with warnings treated as errors; frontend
  2 regression tests passed; strict TypeScript/build and lint passed.

### Live-browser completion investigation

- The user reported that the reset persisted after the wording change. The
  previous change did not establish or fix the cause of that reported reset.
- Inspected the actual Vite server on localhost:5173: its working directory is
  this project's frontend, and served source maps match App.tsx, Interview.tsx,
  and interviewApi.ts on disk. A fresh Chrome context connected to Vite normally.
- Traced five real submissions in Chrome against the existing Vite/FastAPI pair.
  The fifth POST returned HTTP 200, status completed, five answers, index 5,
  and current_question null. The page retained the requested completion content.
- App mounts Interview unconditionally. There is no completion callback, changing
  key, or explicit session-clearing operation. submitAnswer returns the completed
  response, submit stores it, and the completed branch renders in place.
- Added a Playwright full-App integration regression using actual API responses,
  with no API or component mocks. It verifies the mounted region remains the
  same DOM node, no extra navigation/creation occurs, completion remains visible
  during an idle observation, and only an explicit restart creates a second session.
- Checks passed: backend 24 tests with warnings treated as errors; frontend 2
  component tests; 1 real-browser integration test; strict type check/build; lint.
- The live browser logged a favicon 404, but no JavaScript exceptions. A favicon
  request does not change React session state. No unrelated favicon change made.
- No production application code changed in this investigation. No state reset
  reproduced; the exact cause of the earlier behavior was not established.
  Requested its preserved Network and Console trace to distinguish navigation,
  reload/remount, stale tab code, or an unexpected response.

### Final manual verification

- The user verified Issue #1 in Chrome: Start Interview opens question 1, answers
  advance through all five questions, and empty answers do not advance.
- The final answer displays “Interview Complete”, “You completed all 5 questions.”,
  and “Start New Interview”. Explicit restart creates a new interview at question 1.
- The backend connection remains healthy. The earlier reported reset is no longer
  observed in this verification; its historical cause was not established.
- Retained both component and real-browser regression tests for this verified flow.
- Final pre-commit verification: backend 24 passed with warnings treated as errors;
  frontend component tests 2 passed; Playwright Chrome E2E 1 passed; strict
  TypeScript/build, lint, and `git diff --check` passed.

## Milestone 3 — GitHub Actions CI

- Added `.github/workflows/ci.yml` for pushes to `main` and pull requests targeting
  `main`, with independent backend and frontend jobs on GitHub-hosted Ubuntu.
- Backend uses Python 3.13, installs `backend/requirements-dev.txt` (which includes
  runtime requirements), and runs the full `python -m pytest -W error` suite.
- Frontend uses Node.js 24 LTS and runs `npm ci`, `npm test`, `npm run build`
  (TypeScript plus Vite), and `npm run lint` from `frontend/`.
- Uses official checkout/setup actions pinned to stable `v7` majors, pip/npm
  download caches, read-only contents permission, and no persisted Git credentials.
- Excluded Playwright: its existing configuration expects manually started Vite
  and FastAPI servers and an installed browser; CI orchestration is outside this
  minimal milestone. Application code and dependency files are unchanged.
- README documents the triggers, checks, and matching local commands.

Verification:
- Local Python 3.13.0: requirements installation succeeded; 24 backend tests passed
  with warnings treated as errors.
- Local Node.js 24.21.0: clean `npm ci` succeeded; 2 component tests, strict
  TypeScript/production build, and lint passed.
- Ruby YAML parsing and workflow structure checks passed (triggers, permissions,
  jobs, runners, and step structure); `git diff --check` passed.
- Reviewed the final diff and status: only the workflow and documentation changed;
  no secrets or generated artifacts included. No commit made.
- Sandbox DNS restrictions required a network-enabled npm retry; a checksum-verified
  Node.js 24 runtime was downloaded into a temporary directory for validation.
  pip reported an unwritable local cache; npm reported an optional macOS `fsevents`
  install-script notice. Both installations and all checks completed successfully.
- GitHub-hosted execution remains to be verified on a PR/push. Backend requirements
  retain their existing version ranges, so fresh CI installs can resolve newer
  compatible dependencies than the local environment.

## Milestone 4 — Voice Recording Foundation

- Confirmed `feat/voice-recording` and inspected session services/routes, React
  components/API helpers, all existing tests, dependency files, and CI commands.
- Added browser-native MediaRecorder support through `useAudioRecorder` and an
  `AudioAnswer` control keyed by session/question. Permission is requested only on
  Record Answer; the UI handles recording, stopping, upload, acceptance, and failures.
- Typed answers retain their existing behavior. Audio acceptance confirms validation
  only: it neither advances the question nor adds an answer/transcription.
- Added `POST /api/sessions/{session_id}/audio` with multipart `audio` and
  `question_index`, a structured Pydantic metadata response, and shared session
  validation under the existing service lock. Rechecks the question after transfer.
- Added only `python-multipart` for multipart parsing. No new frontend dependencies.
- Audio handling is separate from routes. Enforces a 10 MiB audio limit and a total
  request limit of 10 MiB plus 64 KiB, including requests without Content-Length;
  permits one file/one field, rejects empty uploads and unsupported MIME types, and
  generates a safe response filename rather than reflecting user-supplied paths.
- Browser MIME selection checks WebM/Opus, Ogg/Opus, and MP4 support, falls back to
  the browser's default, and preserves the resulting MIME type and codec parameters.
- Releases microphone tracks on stop, failures, unmount, and question changes;
  releases streams received after a pending permission request outlives the component.
  Guards duplicate starts/uploads, limits recording to five minutes, and cancels
  uploads on unmount with a 30-second upload timeout.
- No permanent storage or raw-audio logging. Multipart parsing can use temporary
  spooled files, which close after validation; browser blobs remain only temporarily.
- README includes API behavior, privacy/format limitations, microphone permissions,
  and exact Chrome verification steps. No transcription, AI, synthesis, database,
  authentication, deployment, or unrelated application behavior added.

Verification:
- Installed updated backend requirements (network-enabled retry required after
  sandbox DNS failure).
- `backend/.venv/bin/python -m pytest -W error`: 49 passed, including 25 new audio
  cases for acceptance, unchanged session state, unknown/completed sessions, wrong
  indices, empty/unsupported/oversized uploads, boundary sizes, MIME parameters,
  malformed input, request/parser limits, temporary-file closure, and concurrent
  question advancement during transfer.
- `npm test`: 20 passed, including 18 new recording/UI cases for permissions,
  unavailable APIs, duplicate starts, state transitions, MIME fallback, successful
  upload/retry/network failure, recorder failures, empty/oversized recordings,
  duration limit, cleanup, delayed permission resolution, cancelled uploads, and
  typed-answer advancement while recording. Existing completion regressions pass.
- `npm run build` (strict TypeScript and Vite) and `npm run lint`: passed under
  Node.js 24.21.0. Initial type checking found an unused test import; fixed and reran.
- `git diff --check`: passed. Reviewed diff/status and new files; no secrets,
  generated audio, dependency directories, environments, caches, or build outputs
  are included. Existing CI scripts/workflow remain compatible and unchanged.
- User manual verification passed in Chrome on Mac: backend health, interview
  startup, real microphone permission, recording start/stop, upload to FastAPI,
  accepted UI confirmation, and typed-answer submission/question advancement.
  The user also confirmed audio is not saved or transcribed. Automated browser-media
  tests use mocks; the Playwright E2E suite was not run in this milestone.
- Validation checks declared MIME type and size, not decoded audio/speech. The
  bounded request is buffered in memory before parsing; this remains a local
  prototype with no global concurrency/rate limits or authentication.
- Prepared as three commits: backend audio API/tests, frontend recording flow/tests,
  and milestone documentation. No push or merge; work stops at this milestone.
