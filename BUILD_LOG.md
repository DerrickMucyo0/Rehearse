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

## Milestone 5 — Issue #9: Structured Nemotron interviewer reasoning

- Implemented on `feat/nemotron-interviewer`, starting with the labeling rubric and
  strict application contract. No new dependency, provider SDK or CI workflow.
- Added an immutable reasoning context/protocol, strict Decision model, a hosted
  NVIDIA/httpx adapter, and a small answer orchestrator. Validates exact actions,
  required fields, strict types, trimmed lengths, action/prompt consistency and JSON
  structure including duplicate keys. No prose extraction or repair of model output.
- The deterministic session engine remains the only state writer. Explicit submission
  reserves a turn and snapshots context under its lock; provider work runs outside
  the lock. Revalidation and answer/transition commit happen atomically afterward.
  Pending concurrent submissions receive 409. Failed, invalid, timed-out or cancelled
  reasoning leaves session state unchanged and releases the reservation.
- Added monotonic turn_revision, current_prompt, probe_count and separate turn records.
  Original planned-question answers remain in answers; follow-up/clarification/challenge
  answers remain separate in turns. Reasons and provider reasoning traces are discarded.
  The engine allows at most two extra prompts per planned question. After those, it
  advances on the next submitted answer without a provider call, records probe_limit
  as the source and no fabricated model action. Planned questions remain code-owned.
- Added submission UUIDs: committed identical retries return current authoritative
  state without re-running reasoning; changed payloads for committed UUIDs conflict.
  Stale/future question/turn checks, pending reservations, cancellation, concurrent
  sessions, replay after completion and late-result rejection have automated coverage.
- Audio upload and transcription require turn_revision and validate it after transfer;
  transcription revalidates after the provider call too. This rejects stale recordings
  from earlier prompts of the same planned question. ElevenLabs adapter unchanged.
- Frontend renders current_prompt and planned-question progress, remounts AudioAnswer
  on revision changes, guards duplicate submission, preserves drafts after errors,
  and reconciles lost responses by submission UUID/turn rather than question index.
  Existing Record/Stop/Transcribe/Review/Edit/Submit flow and microphone release remain.
- NVIDIA endpoint/model are fixed to the approved hosted Chat Completions service and
  nvidia/nemotron-3-super-120b-a12b. Configuration nemotron-super-v1 uses temperature 1.0,
  top_p 0.95 (model-card guidance), low reasoning effort, budget 256, max_tokens 1024,
  stream=false, no retries, 30-second provider/orchestration deadlines, bounded request
  and response bytes. Low/256 is a provisional baseline, not validated adequate quality.
  No assumption of hosted guided_json support and no model determinism claim.
- Error mapping: missing config 503, timeout 504, provider rejection/invalid output 502,
  state conflicts 409 and invalid client input 422. No silent MOVE_ON fallback. No
  NVIDIA logging was added. Errors contain no provider strings, bodies, headers, keys,
  candidate text, prompts, reasons or reasoning traces. Empty NVIDIA_API_KEY placeholder
  added to .env.example; credentials remain backend-only, with no automatic dotenv load.
- Added 80 synthetic human-reviewable evaluation cases: 48 development, 32 held-out.
  Labels: FOLLOW_UP 28, CLARIFY 17, CHALLENGE 16, MOVE_ON 19. Ten core categories have
  six cases each, plus ten multi-turn and ten injection cases. Labels remain drafts
  awaiting human review; no passing quality threshold is established.
- Evaluation reports all-attempt action match, per-action support/precision/recall/F1,
  macro-F1, confusion matrix (including errors), category results, invalid-output/
  provider-error/timeout rates, median/p95 latency and optional repeat agreement.
  Reports carry dataset hash/version, prompt/config versions and full inference settings,
  but no answer/prompt/reason text. Live requests require --live; normal CI only checks
  fixtures, dataset integrity and metric arithmetic. No live NVIDIA request was made.
- Updated README for turn-aware API contracts, privacy, configuration, limits, evaluation
  and pending manual verification. Added an offline Playwright server fixture so the
  pre-existing real-API completion regression can retain deterministic fake reasoning
  without paid requests or a production fallback mode. Browser test request assertions
  now include revision and submission UUID. Playwright remains outside normal CI.

Automated verification:
- Backend: 223 passed with `backend/.venv/bin/python -m pytest -W error`.
- Frontend: 40 component tests passed; lint and production TypeScript/Vite build
  passed with Node.js 24. Playwright discovery found the existing one completion test;
  the browser E2E itself was not run.
- All provider integration tests use mocked transport; NVIDIA credentials are removed
  and real async HTTP provider transport is blocked in pytest. No live quality scores
  are claimed. Existing recording/transcription/completion regressions remain covered.
- Manual Chrome adaptive-turn verification, real hosted configuration acceptance,
  prompt quality review and live development/held-out evaluation remain outstanding.
- `git diff --check` passed. Reviewed status/diff and new files. A scan of tracked,
  untracked non-ignored project files and local .env candidates found no credential
  patterns/private keys or populated credential assignments. Both .env.example values
  are empty. Exact comparison to real keys was unavailable: neither credential was
  present in the agent environment. No generated media/build/dependency artifacts are
  included. The ElevenLabs provider implementation is unchanged.
- No commits or push. Implementation stops after local verification and review.

## 2026-10-03 — Issue #9 research-freeze checkpoint

The entries above remain historical implementation checkpoints. The subsequent
research trajectory covered direct classification, assessment decomposition,
assessment-only, blocking-context, conditional two-stage, challenge_warranted,
Ultra compatibility diagnosis and matched Stage 2 model selection.

- Automatic CHALLENGE remains unresolved; no production interviewer is approved.
- Final matched classification: MODEL-SELECTION NOT SUPPORTED. Ultra was not
  selected; Super remains only the research baseline/comparator.
- Conditional two-stage architecture is retained as frozen research tooling.
- Issue #9 remains open with the original four-action contract unchanged.
- Further full-48 development evaluation is blocked for the frozen candidate;
  historical full-development results are preserved separately.
- Held-out remains sealed / not authorized. Provider experimentation is stopped.
- Existing route wiring remains unapproved feature-branch work. This preservation
  checkpoint does not change runtime behavior or authorize merge/deployment.
- Known official, diagnostic and compatibility observations are recorded in
  [the experiment ledger](evals/interviewer/EXPERIMENT_LEDGER.md). Missing historical
  reports, prompt snapshots and dependency locks remain explicitly unavailable.
- See [the freeze note](evals/interviewer/RESEARCH_FREEZE.md) and
  [provenance manifest](evals/interviewer/research_provenance.json).
