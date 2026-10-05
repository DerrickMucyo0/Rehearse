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

## Issue #7 — Speech-to-text transcription

- Confirmed clean `feat/speech-transcription` and inspected the existing audio/session
  service, multipart validation, recording hook/UI, API helpers, tests, and dependencies.
- Added a small `TranscriptionService` protocol with dependency injection and a separate
  `ElevenLabsTranscriptionService`. Routes and application result models do not expose
  SDK objects. Shared multipart/audio validation now serves upload and transcription.
- Installed and inspected official ElevenLabs Python SDK 2.70.0 signatures and response
  models. Uses `AsyncElevenLabs.speech_to_text.convert`, `model_id="scribe_v2"`, and
  `timestamps_granularity="word"`, with event tagging/diarization disabled. An explicit
  httpx dependency owns the async transport lifetime; no unrelated upgrades requested.
- `POST /api/sessions/{session_id}/transcriptions` accepts audio/question_index, validates
  the current session/question before provider work and again afterward, and returns
  text, optional language, and useful word intervals. Never submits or advances a session.
- Word timings are retained for later deterministic pause/pacing/filler analysis;
  no measurements, AI follow-ups, Nemotron, TTS, realtime transcription, database,
  authentication, or deployment changes implemented.
- Reads `ELEVENLABS_API_KEY` from the backend environment only at transcription time.
  Empty `.env.example` fits the existing ignore rules; no dotenv or frontend key added.
- Missing configuration returns 503, provider/network deadline timeout 504, and provider
  failures or malformed/empty/overlong transcripts 502. Errors are sanitized. The SDK
  has retries disabled and a 60-second total deadline; browser requests allow 75 seconds.
- Extended AudioAnswer with Transcribe Recording while retaining upload-only Send Recording.
  Transcripts fill the existing textarea for review/edit and explicit Submit Answer.
  Any existing textarea content disables transcription; typing/submission are locked
  during the request. Duplicate requests are guarded and unmount aborts/ignores results.
  The MediaRecorder hook and cleanup behavior remain unchanged.
- Rehearse retains no permanent audio and closes temporary files on request exit.
  Transcription deliberately sends audio to ElevenLabs; provider retention follows the
  account's policies. Browser cancellation cannot guarantee cancellation at the provider.

Verification:
- Backend suite with warnings as errors: 108 passed, including 59 new transcription
  cases. Existing 49 audio/session/health cases remain passing. Tests cover session
  and upload validation, non-advancement, stale results, file closure, missing config,
  provider failure/timeouts, malformed output, optional metadata, word timing mapping,
  and the installed SDK's multipart request shape with a mocked HTTP transport.
- Frontend component suite: 30 passed, including 10 new transcription cases covering
  loading/duplicate prevention, editable draft population, explicit submission, draft
  protection, errors/retry, unusable results, and abort/late-result handling. Existing
  typed completion and microphone-cleanup tests remain passing.
- Strict TypeScript/Vite build, Oxlint, and `git diff --check` passed. Existing CI commands
  and workflow remain unchanged. Final diff/status reviewed for milestone-only scope.
- Tests use fake transcribers or mocked SDK transport and explicitly block real provider
  transport in transcription tests. No real API key or real provider call used.
- No secrets, recorded audio, .env files with values, dependencies, environments, caches,
  or build artifacts included. README documents hidden environment entry, privacy,
  API behavior/statuses, and exact manual checks with a real key.
- User manual end-to-end verification passed in Chrome with a fresh interview and
  real microphone recording: ElevenLabs Scribe v2 returned the transcript, the UI
  displayed the review/edit confirmation, and the textarea contained editable text.
  Transcription did not advance the question; explicit Submit Answer advanced from
  Question 1 to Question 2. Existing-text replacement protection also worked.
- The earlier 401 authentication issue was resolved by replacing/rotating the provider
  key. It was not an application-code defect. No real key is included in the repository.
- Removed temporary execution breadcrumbs, their flag/helper/call sites, and tests
  specific to those breadcrumbs. Retained default-dependency route regression coverage.
- Retained opt-in, sanitized failure diagnostics (`REHEARSE_TRANSCRIPTION_DEBUG=1`,
  off by default): one stderr line containing a fixed stage/category and numeric
  provider status if available. No exception text, headers, body, transcript, audio,
  or credentials are emitted. HTTP 503/504/502 behavior is unchanged.
- No new product features, frontend key exposure, commit, or push.

## Deterministic Speaking Metrics v1

- Branch: `feat/speaking-metrics`, independent of Issue #9 and its frozen research.
- Backend slice (`9b06301`): adds deterministic measurements to the transcription
  response while preserving existing fields and session behavior. Measures recognized
  words, conservative standalone English `um`/`uh` counts, timed lexical utterance
  span, and estimated WPM. Complete valid timing coverage is required; missing data
  is not interpolated. Unavailable values use `null`, never substitute zero.
- Frontend slice (`1b889a4`): shows Words, Um, Uh, Timed speech span, and Estimated WPM
  during transcript review. Provenance explicitly identifies the original recording;
  editing the transcript does not recalculate measurements. Replacement recording,
  question advancement, and session lifecycle clear stale measurements; typed-only
  answers show no panel. Unavailable measurements have an explanation.
- Measurements are ephemeral response/frontend state, with no metrics persistence,
  semantic scoring, judgments, coaching, readiness/confidence assessment, or adaptive
  interviewing. Timed span is not full recording duration; pause diagnosis and
  contextual filler detection are not implemented. No provider configuration,
  dependency, CI, or Issue #9 research changes were introduced.
- Final documentation slice updates the current transcription contract, measurement
  rules, original-recording provenance, unavailable states, and limitations in README.
  Earlier build-log entries retain their historical scope and results.

Previously completed milestone-wide verification from committed implementation:
- Full backend suite with warnings as errors: 169 passed.
- Frontend suite: 49 passed.
- TypeScript/Vite build: passed.
- Oxlint: passed.
- Existing local Playwright interview completion/restart flow: 1 passed against
  local FastAPI/Vite with provider credentials unset.
- `git diff --check main...HEAD`: passed.
- Zero live NVIDIA calls and zero live ElevenLabs calls during this verification;
  provider integration was not exercised live. Tests used mocks/local services.
- Issue #9 and held-out data remained untouched. No push or PR created.

## Issue #11 — PostgreSQL foundation, first slice

- Branch: `feat/postgres-persistence`, starting from the merged Speaking Metrics v1
  baseline. Adds synchronous SQLAlchemy 2.x, psycopg 3 and Alembic within the existing
  ranged requirements style. Hosting remains provider-neutral.
- Adds opt-in environment configuration, lazy engines and caller-owned ORM sessions.
  Application `DATABASE_URL` and destructive `TEST_DATABASE_URL` are separate; tests
  require the dedicated `rehearse_test` database/role and verify the connected target
  before DDL. No automatic dotenv loading, migrations, SQL echo or storage fallback.
- Adds the three-table schema: immutable five-question session snapshots, numbered
  submitted-answer attempts and immutable original-transcription measurements.
  Named checks cover status/completion, counts, finite timing values and unavailable
  states. Unique/composite foreign keys enforce one attachment and matching context.
  Parent session deletion cascades; a linked measurement cannot be deleted alone.
- Initial revision `0001_database_foundation` contains self-contained reviewed DDL,
  immutability triggers and a child-first downgrade. No startup `create_all()`.
  Domain validation trims answers and rejects NUL without repair. A pure helper
  models 24-hour unlinked deletion eligibility; no cleanup worker is introduced.
- Adds development Compose PostgreSQL `18.6-bookworm`, a loopback port, named volume,
  healthcheck and environment-supplied local credentials. Backend CI uses the same
  image with isolated disposable test credentials and requires integration tests;
  frontend CI is unchanged. README documents setup, migrations, destructive-test
  isolation, security and the future transactional service enforcement boundary.
- Runtime session/API/transcription behavior remains in memory. Measurement-ID
  association, atomic submission, HTTP 422 NUL handling and cleanup integration are
  deferred. No audio, original-transcript copy or word timing persistence, accounts,
  history/retry UI, semantic scoring, or production hosting integration.

Verification:
- Configuration/domain/offline migration tests with warnings as errors: 44 passed.
  PostgreSQL DDL renders for upgrade and downgrade; one initial revision is present.
- Targeted real PostgreSQL tests: 57 skipped, classified **BLOCKED_BY_LOCAL_DB_ENV**.
  Docker and `psql` are unavailable and `TEST_DATABASE_URL` is absent. No SQLite
  substitution was used. Migration execution and constraint acceptance on a real
  server remain unverified locally; this slice is not production-verified.
- Full backend suite with warnings as errors: 213 passed, 57 skipped. All 169 existing
  API/audio/transcription/speaking-metrics regressions passed.
- `git diff --check` passed. Fingerprints of all pre-existing runtime, frontend and
  test files remained unchanged. No Issue #9 research or held-out inspection, no live
  provider calls, commit, push or PR.

## Issue #11 — PostgreSQL session runtime, second slice

- Continues `feat/postgres-persistence` from foundation commit `20613fa`, starting
  with a clean working tree. Replaces process-local session/answer storage with the
  existing synchronous SQLAlchemy schema; no new migration or dependency changes.
- Session routes lazily resolve `DATABASE_URL` and cache an engine/session factory,
  never an ORM Session. Each service operation owns and closes its database session
  and transaction. There is no in-memory or `TEST_DATABASE_URL` fallback.
- Creation persists the immutable five-question snapshot and initial state. Retrieval
  reads state and ordered attempt-1 answers in a single SQL statement for consistent
  snapshots. UUIDs, public response fields, Location, trimming, 404/409/422 behavior,
  and fifth-answer completion are preserved.
- Submission locks the owning session with `SELECT ... FOR UPDATE`, checks
  existence/status/current index, rejects NUL before insertion, inserts attempt 1,
  advances state and sets UTC completion time on the fifth answer. Flush and commit
  failures roll back the attempt, index, status and completion timestamp together.
  The row lock serializes competing submissions without a global Python lock.
- Async audio/transcription endpoints run synchronous session checks in the thread
  pool; no database operation stays open during upload/provider awaits. Existing
  temporary-file cleanup, transcription configuration and speaking metrics remain
  unchanged. Measurement creation/linking is deferred; submitted attempts have no
  measurement association. No frontend, auth, retry UI or provider changes.
- Shared PostgreSQL fixtures preserve explicit destructive-test isolation. Existing
  session/audio/transcription test assertions remain intact; their old in-memory
  setup now injects a real database factory. README reflects active persistence and
  required application/test configuration.

Verification against local PostgreSQL 18.6 (Postgres.app):
- Focused session/persistence/schema suite with warnings as errors and PostgreSQL
  required: 102 passed, 0 failed, 0 skipped (23 existing API, 22 new persistence,
  57 schema cases).
- Full backend suite with warnings as errors and PostgreSQL required: 292 passed,
  0 failed, 0 skipped. No SQLite substitution.
- Tests prove session/answer/completion persistence across engine reconstruction,
  timestamps and attempt 1, explicit configuration, pool connection return,
  transactional rollback after flush/before commit, uniqueness protection, and
  HTTP 422 for NUL without mutation. The default HTTP dependency is tested without
  service overrides and retains answers after reconstruction.
- Concurrency tests observe the actual PostgreSQL blocker for duplicate submissions,
  require only one accepted answer, and advance a different session while that lock
  remains held. A concurrent read retains a consistent state/answer snapshot even
  when an answer commits before the reader returns.
- `git diff --check` passed. Schema definitions/migration bytes, frontend, provider
  implementations/configuration and speaking-metrics calculation files remain
  unchanged. Issue #9/held-out data untouched; zero live provider calls. No commit,
  push, PR or merge.

## Issue #11 — Original measurement persistence and explicit association

- Continues `feat/postgres-persistence` from session-runtime commit `da01b77`, with
  a clean working tree. Uses the existing schema; no migration, dependency, CI,
  provider configuration or speaking-metrics formula changes.
- Successful transcription calculates the original deterministic metrics, then
  starts a separate database operation. The service locks/revalidates the active
  persisted session/current question, saves immutable scalar metrics with
  `speaking-metrics-v1`, original-transcription source and UTC creation time, and
  returns an opaque UUID `measurement_id`. Audio, transcript copies and word timing
  arrays are not persisted. No database operation remains open during provider work.
- Answer requests optionally include the exact measurement UUID. In the existing
  session-row-locked transaction, the service locks a context-matching measurement,
  verifies it is unattached, and links it with attempt 1, session advancement and
  completion atomically. Unknown/wrong-context/already-linked IDs share HTTP 409
  `Measurement cannot be attached to this answer.` Malformed UUIDs return 422.
  Failures leave attempts, progress and completion unchanged; existing constraints
  remain a final safety layer. Typed answers omit the ID; no latest lookup or
  recalculation from edited text is introduced.
- The frontend pairs the transcript draft with its returned ID. Nonempty edits keep
  that association. Clearing the draft, replacing the recording, failed replacement
  transcription, successful advancement and session restart clear stale IDs; a new
  successful transcription supplies its own ID. Typed-only requests omit the field.
  Original metrics display/provenance, reconciliation, and visual layout are preserved.
- Replaced/unsubmitted rows remain unlinked; no synchronous deletion or background
  cleanup worker is added. The existing 24-hour unlinked deletion-eligibility policy
  and linked retention remain unchanged. No authentication, retry behavior, history
  UI or semantic scoring is added.

Verification against local PostgreSQL 18.6 (Postgres.app):
- Focused measurement/transcription/session suite with warnings as errors and
  PostgreSQL required: 149 passed, 0 failed, 0 skipped, including 42 new measurement
  cases. Covers exact unrounded and unavailable values, reconstruction, original-ID
  rather than latest association, edited text, malformed/invalid references,
  fifth-answer completion, rollback after flush/before commit, no provider-held DB
  resources, post-calculation races and actual PostgreSQL row-lock revalidation.
- Full backend with warnings as errors and PostgreSQL required: 334 passed,
  0 failed, 0 skipped. Existing session concurrency tests remain passing.
- Frontend `npm test`: 59 passed (49 existing plus 10 new cases). `npm run build`
  and `npm run lint` passed. Tests use mocked fetch/media APIs.
- `git diff --check` passed. Schema definitions and migration bytes remain unchanged;
  speaking-metrics formulas and provider implementations/configuration are unchanged.
  Zero live provider calls; Issue #9/held-out data untouched. No commit, push, PR or merge.
