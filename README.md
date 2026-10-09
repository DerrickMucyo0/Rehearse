# Rehearse

Rehearse is a communication practice platform in development for interviews,
public speaking, negotiations, and presentations. The current prototype supports
interviews with five fixed questions, typed or transcribed drafts, append-only attempts,
Retry, deterministic Before/After comparison, explicit Continue to completion,
browser-local History, an objective Progress dashboard, and deterministic delivery
timing facts.

Current product loop: **Speak → Transcribe → Measure → Persist → Retry → Compare
→ History → Progress → Delivery timing facts**. Typed practice is also supported.
Transcription creates an immutable measurement; Submit Attempt saves reviewed text and optionally links
that exact measurement. Continue finalizes the question. History and Progress read
persisted facts without making provider requests. Delivery v1 adds timed word-gap
aggregates, with no AI delivery scoring or personalized diagnosis/coaching.

## Current architecture

- `frontend/`: React, TypeScript (strict mode), and Vite.
- `backend/app/`: FastAPI application served by Uvicorn; session routes call a
  separate PostgreSQL-backed session service with Pydantic request/response models.
- `tests/`: backend tests using pytest and FastAPI TestClient.
- `docs/`: reserved for future documentation.

### AI Voice v1

**Play question** speaks the current saved question; **Replay question** reuses
that question's transient browser clip. **Stop** cancels loading or playback.
There is no automatic synthesis on session load, Retry, Continue, or navigation.
Voice never submits an answer or advances the interview. Starting recording,
interview transitions, leaving Practice, or changing accounts stops playback and
discards the clip. A browser playback rejection requires another explicit click.

`POST /api/sessions/{id}/questions/{q}/speech` accepts no body. It authenticates
the existing cookie/request context and reads only an owned session's current
persisted question; historical, future, completed, foreign, and missing targets
return the same fixed 404. Database resources close before synthesis. Fresh auth
and question checks discard stale audio before release. Success is bounded binary
`audio/mpeg` with `Cache-Control: no-store` and `X-Content-Type-Options: nosniff`.

Set both `ELEVENLABS_API_KEY` and `ELEVENLABS_VOICE_ID` in the backend process
environment only. Neither is sent to the browser. The model is fixed to
`eleven_flash_v2_5` (stability `0.5`, similarity boost `0.75`) and the format to
`mp3_44100_128`; transcription uses ElevenLabs `scribe_v2`; missing configuration
returns 503 without a provider request. The separate HTTPX TTS adapter sends only
the exact saved question plus required voice/model/format settings, makes one
request with no retries or redirects, and has a 60-second total deadline with
15-second connect/write/pool limits and no independent read timeout. The browser
waits at most 75 seconds. HTTP 200, MPEG media type, plausible ID3/MPEG structure,
and a nonempty actual downloaded size of at most 2 MiB are validated
before release. Content-Length is only an early bound.

Synthesis failures use the fixed message **Voice playback is unavailable right
now.** (503 configuration, 502 provider/audio, 504 timeout), independently of
authentication errors and interview write recovery. Explicit user retry is
allowed. Rehearse stores no audio, adds no server speech cache, and logs no
provider bodies or credentials. Refresh requires another explicit Play request.
ElevenLabs processing/retention follows the configured provider account's policy;
no Rehearse persistence does not promise provider deletion. Browser cancellation
does not guarantee that already-started provider work stops. Automated tests use
synthetic audio and offline transports, with no live provider calls.

New practice sessions save one interviewer persona: University Recruiter (Polite),
Senior Manager (Formal), or HR Lead (Firm). Gemini applies the selected fixed
server-side style to follow-up questions. Existing sessions remain unassigned and
use `ELEVENLABS_VOICE_ID`. Optional backend-only settings
`ELEVENLABS_RECRUITER_VOICE_ID`, `ELEVENLABS_MANAGER_VOICE_ID`, and
`ELEVENLABS_HR_VOICE_ID` select different voices; blank values fall back to the
existing voice ID. Apply the database migration before restarting Rehearse.

React requests `GET /api/health` on page load. Vite's development proxy forwards
`/api` requests to `http://127.0.0.1:8000`, keeping browser requests on the same
origin without requiring CORS configuration. The health endpoint returns
`{"status":"ok","service":"rehearse-api"}`. The page displays “Backend connected”
on success or “Backend unavailable” on failure (including a five-second timeout).
The proxy applies to the development server only.

Click **Start Interview**, compose or transcribe an answer, then **Submit Attempt**.
Submission saves the attempt and opens review on the same question. **Retry** opens
a fresh draft; **Retry Again** supports further attempts. **Continue** finalizes the
current question and advances. Only Continue on the fifth question completes the
interview. Answers are not scored or generated by AI.

Session endpoints:

| Method | Endpoint | Behavior |
| --- | --- | --- |
| POST | `/api/sessions` | Creates a session (201), with its URL in `Location`. No body required. |
| GET | `/api/sessions/{id}` | Retrieves the session (200). |
| POST | `/api/sessions/{id}/questions/{q}/attempts` | Appends an attempt without advancing (201); returns `attempt` and `session`. |
| GET | `/api/sessions/{id}/questions/{q}/attempts` | Retrieves persisted attempts in attempt-number order (200). |
| POST | `/api/sessions/{id}/questions/{q}/continue` | Finalizes the current question and advances or completes (200); returns session state. |
| GET | `/api/sessions/{id}/questions/{q}/comparison` | Compares explicitly linked persisted measurements (200); see comparison contract below. |

The old `POST /api/sessions/{id}/answers` is retired and returns 404.
The question index `q` is zero-based and must match the current question for writes.
Attempt body: `{"expected_last_attempt_number":0,"answer":"My answer","measurement_id":null}`.
Continue body: `{"expected_last_attempt_number":1}`. Both require the exact backend
`current_question_latest_attempt_number`, never a locally incremented revision or
attempt-list length. Numbers are assigned by the server. Continue requires at least
one saved attempt. Answers must be strings with 1–10,000 characters after trimming
whitespace; embedded U+0000 is rejected before insertion. Invalid bodies or malformed
UUIDs return 422; unknown session UUIDs return 404; completed sessions, stale/future
question indices, and stale revisions return 409 without changing state.

For a transcription-derived draft, the body includes
`"measurement_id":"<UUID returned by transcription>"`. Typed-only attempts use null
(the backend also accepts omission).
The backend links only that exact measurement, never a latest recording or inferred
measurement. Malformed measurement UUIDs return 422. Unknown, mismatched-session,
mismatched-question and already-linked references all return the same 409 detail:
`Measurement cannot be attached to this answer.` Rejected links leave the attempt,
session index/status and completion timestamp unchanged.

Responses contain `id`, `status` (`active` or `completed`),
`current_question_index`, `current_question`, `current_question_latest_attempt_number`,
`questions`, and ordered `answers`. The revision is 0 on a fresh question or completed
session. `answers` summarizes only finalized questions, using each one's latest saved
attempt. Retrieved attempts contain `id`, `question_index`, `attempt_number`, `answer`,
`submitted_at`, and nullable `measurement_id`; earlier attempts remain unchanged.
After completion, the index equals the question count and `current_question` is null.

Sessions and submitted attempts survive backend restart/reload in PostgreSQL and
are shared across backend workers. Current Practice restoration keeps only the session
identifier in tab-scoped `sessionStorage`; drafts, recordings, and measurements are
not stored there. The separate browser-local History registry is described below.
Reload fetches session state and current-question attempts: zero attempts opens the
composer, saved attempts open review, and a completed session restores completion.
Unsaved drafts are discarded on reload; restoration requires browser tab storage.
There is no authentication, expiry, or scoring.
Anyone with a session ID can access that session. This is a local development prototype.

Retry and Cancel Retry are frontend-only transitions. Retry clears draft text,
measurement ID, speaking/delivery metrics, recording Blob, errors, and recorder
resources through a new draft generation while retaining persisted attempts. Same-draft text edits, including
delete/retype, preserve the original measurement association; new recording/transcription
and fresh drafts invalidate it. A successful voice retry gets its own measurement UUID.

409 from submission, Continue, or transcription reloads authoritative session/attempt
state, clears unsafe draft state, and never silently resubmits. An uncertain write or
failed review load blocks mutations until **Recheck saved state**. Recheck performs
reads only: it restores a saved success or permits a manual retry when the same question
and revision remain current. Pending actions and duplicate clicks are guarded.

## Session History and Progress

Navigation offers **Practice**, **History**, and **Progress**. Practice stays mounted
across safe navigation, preserving idle typed drafts, same-draft measurement
association, saved review, and retry composing state. Leaving Practice is blocked
during microphone permission, recording/finalization, upload, transcription, Attempt
submission, Continue, conflict reconciliation, and ambiguous recovery/Recheck.

### Sessions remembered on this browser

History discovers sessions only through this browser's `localStorage` registry:

```text
rehearse.history.v1:<canonical lowercase session UUID> = "1"
```

The registry stores opaque session UUID capability keys only: no answers, questions,
measurements (including delivery facts), measurement IDs, summaries, recordings,
or provider content. The current Practice ID remains separately in `sessionStorage`. Successful session
creation and verified Practice restoration register the session. Removing or clearing
remembered History removes local discovery keys; it does not delete PostgreSQL records,
clear the active Practice restoration ID, or discard its safe draft.

History is device/profile/origin-bound, not an account-owned server list. Authentication
and cross-device account history are not implemented. Anyone possessing a session UUID
can access that session under the existing prototype access model; local discovery is
not an authorization check. There is no server-wide session enumeration endpoint.

The normal registration cap is 500 remembered IDs, without automatic eviction; an
already remembered ID remains usable at capacity. This browser-local cap is a soft
limit under concurrent-tab writes. Storage failures warn without blocking Practice.
Malformed keys are ignored safely. Other-tab storage events for the History namespace
refresh its registry and invalidate cached reads. Missing server sessions stay visible
and locally removable; an empty registry says “No sessions are remembered on this
browser yet,” without implying that no server sessions exist.

### Scoped, read-only History API

| Method | Endpoint | Contract |
| --- | --- | --- |
| POST | `/api/history/summaries` | Body `{"session_ids":["<session UUID>"]}`; 1–50 explicitly supplied UUIDs per batch, with canonical deduplication after the raw-list size check. Returns `summaries` and `missing_session_ids` only for requested IDs. |
| GET | `/api/sessions/{session_id}/history-detail` | One explicitly supplied session UUID. Returns its summary and question overview; answer text is returned only for an explicitly selected question's bounded attempt page. |

There is no `GET /api/sessions` list or wildcard History read. Summaries omit answer
and question text. History/Progress measurement DTOs preserve their nine speaking
scalar/provenance/availability fields and add nullable nested `delivery_metrics`.
That object contains delivery version, source, three aggregate values and an
unavailable reason; `null` means delivery was not recorded historically. No measurement
UUID, word timings, pause events or raw provider data is exposed in these DTOs.
Detail without `question_index` returns no attempt text. Its optional zero-based
`question_index` selects one question; `limit` defaults to 10 and is bounded to 1–20.
The positive `after_attempt_number` cursor requires a selected question and returns
attempts with greater persisted numbers, ascending. `has_more` and
`next_after_attempt_number` describe the next page; numbering gaps are safe.
Malformed requests return sanitized 422 errors; unknown detail resources return 404.
Stored-integrity errors return 500 and database/configuration unavailability returns
503, without echoing supplied values, stored content, credentials, or exception text.
New History responses, including errors and method rejection, use
`Cache-Control: no-store`; browser read requests also use `cache: 'no-store'`.

History reads use an operation-local read-only, repeatable-read transaction, without
session write locks or provider inference. Summaries are ordered by persisted
`last_saved_activity_at` descending, then session UUID ascending. This timestamp is
the maximum of creation, latest submission, and completion timestamps; earlier
Continue operations have no separately stored activity timestamp. Each batch/detail
response has one database snapshot; separate hydration batches can have separate
snapshots. Browser hydration
batches remembered IDs in groups of at most 50, with at most three concurrent reads,
and applies that ordering globally across batches, preserving timestamp microseconds.
Detail shows finalized/current/upcoming questions and paginates saved attempts; only
a finalized question's final attempt is marked Final. Stale responses are suppressed.

### Final attempts and count definitions

A final attempt is the **greatest persisted `attempt_number` for a finalized
question**. Continue makes a question finalized; the current open question's latest
attempt remains provisional. Earlier finalized questions of an active session are
stable and included. `final_attempt_id` in detail is a derived response field;
no `final_attempt_id` database column exists or is needed.

`total_attempt_count` counts actual persisted attempt rows.
`questions_practiced_count` counts distinct questions with an attempt, including
the current question. **`total_retry_count = total_attempt_count -
questions_practiced_count`**. Retry counts never substitute `MAX(attempt_number)` for
row counts; a numbering gap does not invent attempts.

### Objective Progress projection

Progress means objective persisted facts. Its overview counts completed sessions,
active sessions, finalized questions, saved attempts, saved retries, and measured
final answers. Session status supplies the first two counts; the remaining counts
sum their respective summary fields. These are facts about remembered sessions,
without a judgment about retry frequency or practice quality.

One measurement row is **one final attempt of one finalized question**, supplied
only by backend `finalized_points`. Superseded retries, open-question attempts,
unsaved drafts, unlinked recordings, and measurements from earlier attempts are
excluded. If a measured attempt is superseded by a typed final, no earlier measurement
is substituted. Progress never recomputes values from edited answer text.

Five chronological tables show recognized words, um count, uh count, timed speech
span (seconds), and estimated WPM (words/minute), with date/time, question number,
session status, and attempt number. Rows sort by persisted `submitted_at` ascending,
then session UUID, question index, and attempt number. Raw UUIDs are not displayed in
the primary UI. Counts display as integers; span and WPM use up to one decimal place
for presentation. Exact stored floats remain unchanged internally.

Measured rows form separate cohorts for each exact
`(measurement_version, measurement_source)` tuple. The current version is
`speaking-metrics-v1`, with source `original_transcription` (displayed as Original
transcription). Different versions/sources never form one continuous series or
cross-cohort numeric comparison. Typed finals have `measurement: null`; they remain
finalized answers and appear in a separate no-measurement group as
**Unavailable — No measurement** for every speech metric.

**0 is real measured zero; null is unavailable.** Stored filler/timing reasons map
to factual explanations: Unsupported language, Missing timings, Timing coverage
mismatch, Invalid timing, Invalid timing order, or Unusable span. Unknown future
reasons display Unavailable without inventing a cause. Recognized words use their
stored count independently of filler/timing availability. Each metric reports
coverage as available values out of all finalized points in that cohort, including
unavailable points; zero is available. The separate no-measurement group reports
zero available out of its typed/unmeasured final points.

Delivery adds three chronological tables: **Pause count**, **Total pause time
(seconds)** and **Longest pause (seconds)**. These use separate cohorts for exact
`(delivery_measurement_version, measurement_source)` pairs, projected as nested
delivery `version`/`source`; speaking provenance does not determine delivery
compatibility. The current delivery version is `pause-metrics-v1`. Only the exact
linked final attempt supplies delivery facts, including finalized earlier questions
in active sessions. Open questions, superseded retries, typed finals and historical
measurements without delivery facts do not enter delivery cohorts. Recorded
unavailable points count in their cohort's coverage denominator, and measured zero
counts as available. The existing measured-final-answer count still counts linked
speaking measurements, including historical ones without delivery. Chronology and
all five speaking tables retain their existing semantics.

There are no averages, medians, trend slopes, charts, communication/confidence/
readiness/answer-quality scores, semantic improvement claims, or better/worse
judgments. Metric magnitude has no quality color coding. Tables have captions,
column headers, explicit units, and readable provenance/unavailable text.

### Shared hydration and completeness

History and Progress share one memory-only hydration owner; switching between them
does not duplicate current summary reads. Complete hydration permits overview
totals for available remembered sessions, with an explicit caveat for missing server
sessions. Partial, loading, or error hydration hides totals; partial results say
“Progress totals are unavailable until all remembered sessions load.” Loaded points
remain visible as loaded-session facts. Errors offer retry, and failed-chunk retry
retains successes and requests only failed remembered chunks. Reload history reads
all remembered IDs again.

Session creation, successful Attempt/Continue, authoritative recovery, registration,
removal, clearing, relevant storage events, and explicit reload invalidate the cache.
Reads resume on safe History/Progress navigation, without polling or background
timers. Unlinked transcription alone changes no History DTO and does not invalidate
it. Writes elsewhere that emit no History storage event require explicit Reload
history. Clearing discards in-memory reads even if local storage fails; retained IDs
then reload safely. The History/Progress read-model milestone required no schema
change. The delivery extension uses migration `0002_pause_delivery_metrics` described
below; its reads add no provider requests or provider persistence. Hydrated delivery
facts stay in memory and are never written to the browser-local registry.

## Local frontend setup

Use Node.js 24 LTS with npm (also used by CI). From the project root:

```sh
cd frontend
npm ci
npm run dev
```

Open `http://localhost:5173`. Run the backend in a separate terminal.
To type-check and build: `npm run build`. To lint: `npm run lint`.
To run frontend regression tests: `npm test`.

For the real-browser integration suite, keep local frontend and backend services
running against an isolated disposable PostgreSQL database, with provider credentials
unset, then run from `frontend/`:

```sh
npx playwright install chromium
npm run test:integration
```

Alternatively, set `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH` to an installed Chrome
executable. Four tests make real localhost API requests and create/read sessions in
the configured isolated database. They cover retry/completion/reload, browser-local
History and paginated detail, typed-final Progress with preserved Practice state,
and an exact linked measured-final fixture. The fifth uses mocked API responses and
synthetic microphone data to cover delivery transcription, retry comparison, History
and Progress. External page requests are blocked; none of the five tests calls an
external provider.
Failure traces are written to ignored `test-results/`.

The measured-final fixture test requires `REHEARSE_E2E_MEASURED_SESSION_ID`: an opaque UUID
of a provider-free fixture in that same isolated database. It expects active Question 2,
two saved attempts (finalized Question 1 plus open Question 2), and Question 1's exact
linked `speaking-metrics-v1` / `original_transcription` values: 12 recognized words,
um 0, uh 1, 12.5 seconds, and 57.6 WPM. It is skipped when the fixture ID is absent.
The release audit supplies this fixture through the existing persistence service and
an execution-local test harness using the validated dedicated test database/role;
the backend never falls back from `DATABASE_URL` to `TEST_DATABASE_URL`. No provider,
schema, or application configuration change is needed.

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
Before using session endpoints, configure `DATABASE_URL` in the backend process and
apply the migration as described below. The health endpoint does not require a database.

## Run backend tests

From the project root with the backend virtual environment activated:

```sh
REHEARSE_REQUIRE_POSTGRES_TESTS=1 python -m pytest -W error
```

Configure the isolated `TEST_DATABASE_URL` first using the instructions below.
Session, audio and transcription API regression tests use real PostgreSQL; pure
speaking-metrics and configuration tests remain database-independent.

## PostgreSQL persistence (Issue #11)

PostgreSQL stores interview sessions and submitted answers, using synchronous
SQLAlchemy 2.x, psycopg 3 and Alembic. Importing the application creates no engine or
connection. Session routes resolve `DATABASE_URL` on first use and cache the service's
engine/session factory. Each service operation creates and closes its own ORM session.
`create_database_engine()` creates a lazy engine;
`create_session_factory()` returns a factory, not a shared session. Callers
own their sessions and transactions. There is no startup `create_all()`, automatic
migration, storage fallback, or additional provider integration.

`DATABASE_URL` is required for session endpoints and migrations.
`TEST_DATABASE_URL` is exclusively for destructive
PostgreSQL tests and never falls back to `DATABASE_URL`. Standard PostgreSQL URLs
are normalized to `postgresql+psycopg`. Destructive tests require both database and
role to be named `rehearse_test`, reject connection-query overrides and any configured
application database of the same name, and verify the actual connected database/role
before DDL. The test target must contain only this test schema. Configuration errors
omit URL values; engine SQL echo is disabled and bound parameters are hidden in
SQLAlchemy errors. Do not enable SQL logging or log database exceptions/URLs containing
submitted text or credentials. No automatic `.env` loading is added.

### Local PostgreSQL and explicit migrations

Install Docker separately if needed. Development and CI use
`postgres:18.6-bookworm`. Compose binds only `127.0.0.1:5432`, uses the named
`postgres_development` volume and a `pg_isready` healthcheck. Credentials are required
environment inputs for local development only; never reuse production credentials.
For macOS zsh, from the repository root:

```zsh
export POSTGRES_USER=rehearse_dev
read -rs "POSTGRES_PASSWORD?Local PostgreSQL password: "
echo
export POSTGRES_PASSWORD
docker compose up -d postgres
docker compose ps
```

Once healthy, enter an application URL privately. Its shape is
`postgresql+psycopg://rehearse_dev:<URL-encoded-password>@127.0.0.1:5432/rehearse_dev`.
Use percent encoding for special characters in credentials. These commands put
neither the entered value nor password into shell history:

```zsh
read -rs "DATABASE_URL?Local application database URL: "
echo
export DATABASE_URL
PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python -m alembic upgrade head
PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python -m alembic current
```

Migration `0001_database_foundation` creates the three tables below, their constraints,
the measurement creation-time index and two immutability triggers. Its DDL is
self-contained. Downgrade removes attempts, measurements, sessions, then trigger
functions; it destroys stored data and must only be run intentionally on an appropriate
database. Migration `0001_database_foundation` remains unchanged. The current head,
`0002_pause_delivery_metrics`, extends the measurement table with five nullable scalar
columns and eight checks; it performs no backfill and leaves historical delivery
fields all-null. It adds no table or delivery-source column. Its downgrade removes
only those checks and columns, retaining the foundation schema and existing
measurements, IDs, links and immutability trigger. Changing initial Compose
credentials does not change an existing volume's
database roles/passwords. `docker compose down` retains the named volume.

### Schema and enforcement boundary

All IDs are application-generated UUID primary keys. Required timestamps are
`TIMESTAMPTZ` with server `now()` defaults. There is no `user_id` or account schema.

| Table | Columns |
| --- | --- |
| `interview_sessions` | `id UUID`, `questions JSONB NOT NULL`, `current_question_index INTEGER NOT NULL` (default 0), `status TEXT NOT NULL` (default active), `created_at TIMESTAMPTZ NOT NULL`, `completed_at TIMESTAMPTZ NULL` |
| `question_attempts` | `id UUID`, `session_id UUID NOT NULL`, `question_index INTEGER NOT NULL`, `attempt_number INTEGER NOT NULL` (ORM default 1), `answer_text TEXT NOT NULL`, `submitted_at TIMESTAMPTZ NOT NULL`, `measurement_id UUID NULL` |
| `transcription_measurements` | `id UUID`, `session_id UUID NOT NULL`, `question_index INTEGER NOT NULL`, `created_at TIMESTAMPTZ NOT NULL`, `measurement_version TEXT NOT NULL`, `measurement_source TEXT NOT NULL`, `recognized_word_count INTEGER NOT NULL`, `um_count INTEGER NULL`, `uh_count INTEGER NULL`, `filler_unavailable_reason TEXT NULL`, `timed_utterance_span_seconds DOUBLE PRECISION NULL`, `estimated_words_per_minute DOUBLE PRECISION NULL`, `timing_unavailable_reason TEXT NULL`; migration 0002 adds nullable `delivery_measurement_version TEXT`, `pause_count INTEGER`, `total_pause_duration_seconds DOUBLE PRECISION`, `longest_pause_seconds DOUBLE PRECISION`, `pause_unavailable_reason TEXT` |

PostgreSQL enforces:

- Exactly five nonempty text questions; snapshot changes are rejected by a trigger.
  Active sessions have index 0–4 and no completion timestamp. Completed sessions have
  index 5 and a completion timestamp no earlier than creation. Only `active` and
  `completed` statuses are allowed.
- Nonnegative question indices and positive attempt numbers. Attempts 2, 3 and beyond
  are permitted by the schema, with a unique `(session_id, question_index, attempt_number)`.
  Answer text length is 1–10,000 characters.
- Measurement source exactly `original_transcription`, nonblank version and
  nonnegative word/filler counts. Available filler counts are both non-null with no
  reason; unavailable counts are both null with `unsupported_language`.
- Available timing values are both positive and finite, including rejection of NaN
  and infinity, with no unavailable reason. Unavailable values are both null with one
  of `missing_timings`, `timing_coverage_mismatch`, `invalid_timing`,
  `invalid_timing_order`, or `unusable_span`. Double-precision values are not rounded.
  A trigger rejects updates to the whole persisted measurement snapshot, including
  the delivery extension.
- Delivery is either historical all-null, available with version/three numeric
  values/no reason, or unavailable with version/null numeric values/a timing reason.
  Counts are nonnegative and bounded by recognized words minus one; durations are
  finite and nonnegative. Zero pauses requires zero total/longest duration; positive
  counts require positive durations with longest no greater than total. The existing
  `measurement_source` supplies provenance for both independently versioned families.
- Foreign keys from attempts/measurements to their session. A composite foreign key
  makes a linked measurement match the attempt's session and question. Unique
  `measurement_id` permits only one attachment; null permits typed-only attempts.
  Session deletion cascades to both child tables; deleting a linked measurement alone
  is rejected. No deletion endpoint is introduced.

The request and persistence validators trim submitted answers and reject embedded
U+0000 before insertion with HTTP 422; they do not strip/replace NUL.
Attempt submission opens a transaction, selects the session row `FOR UPDATE`, validates
existence/status/current index and the revision against `MAX(attempt_number)`, then
appends `MAX + 1` without advancing or completing. Continue uses the same lock/revision
guard, requires a saved attempt, and advances exactly once; final Continue sets
`completed` plus a UTC completion timestamp. It inserts no attempt. Flush/commit
failure rolls back the whole operation. Competing writes to one session serialize;
different sessions use independent row locks. There is no global Python lock or global
ORM session. Retrieval joins state and attempts in one statement, deriving finalized
latest answers and the current revision from that read snapshot.
Async audio/transcription routes perform session checks in the thread pool and close
their database operation before awaiting upload/provider work.

Successful transcription creates an immutable measurement in its own transaction after
the provider call and both deterministic speaking/delivery calculations. These run
before insertion into one measurement row with one UUID, without another provider
request. Provider or calculation failure creates no measurement. The service reopens
a database operation, locks/revalidates the authoritative current session/question and stores only
the scalar metrics, version, source and context. Transcription checks the expected
revision before provider inference and again under that lock before persistence.
Advancement or a new attempt on the same question rejects a stale result without
creating a measurement. No database session/lock is held during provider inference.

Answer submission optionally loads and locks the exact context-matching measurement,
checks that it is unattached, and writes its ID with the new attempt atomically.
The existing foreign keys and unique constraint remain a final safety layer.
A failed link rolls back all changes; no measurement is
inferred from answer text. Typed-only attempts retain null `measurement_id`.

Unlinked measurements become deletion-eligible after 24 hours; linked measurements
remain associated with their attempt. A pure timestamp/linkage helper models this
policy. It introduces neither automatic expiry of attachment rights nor a cleanup
worker; future cleanup must recheck linkage transactionally. Replaced/unsubmitted
measurements remain unlinked; the frontend does not synchronously delete them.

Every saved trimmed attempt is durable text. No audio, second
original-transcription text copy or word timings are stored. There are no user
accounts or semantic scoring. The browser-local History and objective Progress reads
described above reuse this schema. Hosting remains provider-neutral;
no Supabase-specific APIs are used. Issue #9 remains open and frozen.

### Deterministic Before/After comparison

After two or more saved attempts, review shows neutral Before / After / Change
values for Attempt 1 versus the latest attempt. Attempt 1 alone has no comparison
card. Counts display as integers; duration and WPM use one decimal place; nonzero
changes retain their sign. Unavailable values display as Unavailable, never zero.
The comparison endpoint is read-only:

`GET /api/sessions/{session_id}/questions/{question_index}/comparison`

Optional positive integer selectors `before` and `after` choose attempt numbers for
that exact session/question. `before` defaults to 1; `after` defaults to the latest
persisted attempt, including attempts 3 and beyond. Both selectors omitted with
fewer than two attempts returns 200 with `comparison: null`,
`delivery_comparison: null`, `after_attempt: null`, and Attempt 1 as `before_attempt`
if it exists. Supplying either selector requires
both selected resources to exist: an unknown session, question or selected attempt
returns 404. Invalid selector shapes return 422. Existing selections must satisfy
`before < after`; equal or reversed numbers return 422. Partial selectors use the
same defaults; for example `before=2` compares Attempt 2 with the latest attempt.

The response has `session_id`, `question_index`, `before_attempt`, `after_attempt`
and `comparison`, plus additive `delivery_comparison`. Each selected attempt exposes
only `id`, `attempt_number`, `measurement_id`, `measurement_version` and
`measurement_source`. The comparison
contains `recognized_word_count`, `um_count`, `uh_count`,
`timed_utterance_span_seconds` and `estimated_words_per_minute`. Each metric has
`before`, `after`, `delta`, `before_unavailable_reason`, `after_unavailable_reason`,
`comparable` and `comparison_unavailable_reason`.

Only each attempt's exact linked immutable measurement supplies values. A single
SQL read keeps selection and measurement provenance consistent without row locks.
Unlinked measurements and edited answer text have no effect. Compatible, available
pairs use the unrounded persisted values for `delta = after - before`; measured
zero remains zero. Typed attempts have null values with `no_measurement`. Stored
filler/timing unavailable reasons remain unchanged, with null deltas.

Both measurements must have the same version and source `original_transcription`.
Unequal versions yield `measurement_version_mismatch`; otherwise unsupported
sources yield `measurement_source_incompatible`. These take precedence over
`before_unavailable`, `after_unavailable` or `both_unavailable`, while per-side
reasons remain visible. Comparisons report neutral facts and never score quality
or label a change as improvement.

The separate `delivery_comparison` preserves speaking comparison behavior. It
contains `before_version`, `after_version`, `before_source`, `after_source` and
changes for `pause_count`, `total_pause_duration_seconds` and `longest_pause_seconds`.
Each change has the same seven before/after/delta/availability fields described
above. Delivery requires exact matching delivery versions and sources independently
of speaking compatibility; incompatible or unavailable sides never yield a numeric
delta. Typed sides use `no_measurement`; historical measured sides use `not_recorded`.
Recorded unavailable sides retain their timing reason and delivery provenance.
Compatible available sides use unrounded persisted `after - before`. The UI presents
neutral Before / After / Change facts, with **Not recorded**, **Unavailable**, and
visible measured zero distinguished.

### Real PostgreSQL verification

Create a dedicated disposable test database and role once, on the local Compose
server. The interactive password command does not expose its value:

```zsh
docker compose exec postgres psql -U "$POSTGRES_USER" -d rehearse_dev -c 'CREATE ROLE rehearse_test LOGIN'
docker compose exec postgres psql -U "$POSTGRES_USER" -d rehearse_dev -c '\password rehearse_test'
docker compose exec postgres createdb -U "$POSTGRES_USER" -O rehearse_test rehearse_test
read -rs "TEST_DATABASE_URL?Isolated rehearse_test database URL: "
echo
export TEST_DATABASE_URL
REHEARSE_REQUIRE_POSTGRES_TESTS=1 PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python -m pytest -W error tests/test_sessions.py tests/test_session_persistence.py tests/test_measurement_persistence.py tests/test_postgres_schema.py
REHEARSE_REQUIRE_POSTGRES_TESTS=1 PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python -m pytest -W error
git diff --check
```

The test URL must use `rehearse_test` for both username and database, with the privately
entered password and local host/port. Integration tests downgrade/upgrade this schema,
including an upgrade from an empty schema; **never use a database containing valuable
data**. They verify schema/ORM parity, UUID/JSONB/timestamp persistence, constraints,
immutability, foreign keys, delete policy and persistence across engine reconstruction.
Session integration tests also prove transactional rollback, completion persistence,
HTTP NUL rejection, and actual PostgreSQL row-lock blocking with independent-session
progress. Existing audio/transcription API tests use database-backed session storage.
They use real PostgreSQL, never SQLite. Without explicit local configuration they skip
as `BLOCKED_BY_LOCAL_DB_ENV`; this is not PostgreSQL acceptance. Setting
`REHEARSE_REQUIRE_POSTGRES_TESTS=1` makes missing configuration fail. Database-independent
configuration/domain/offline migration tests and existing API regressions remain runnable.

## Continuous integration

GitHub Actions runs `.github/workflows/ci.yml` on pushes to `main` and pull
requests targeting `main`, with separate backend and frontend jobs on Ubuntu:

- Python 3.13: install runtime and test requirements, then run the complete pytest
  suite with warnings treated as errors against an isolated PostgreSQL 18.6 service.
  Its runner-only test credentials are disposable; PostgreSQL tests are required.
- Node.js 24 LTS: install locked npm dependencies, run Vitest component tests,
  type-check and build with TypeScript/Vite, and lint with Oxlint.

The workflow caches pip and npm downloads and uses read-only repository permissions.
Playwright remains a local check: its current configuration requires separately
started Vite and FastAPI servers plus a browser installation.

Run the same checks locally from the project root, with the backend virtual
environment activated as described above:

```sh
python -m pip install -r backend/requirements-dev.txt
python -m pytest -W error
cd frontend
npm ci
npm test
npm run build
npm run lint
```

## Voice recording foundation

Each active question also offers **Record Answer → Stop Recording → Send Recording**.
The browser requests microphone access only when you click Record Answer. Use Chrome
(or another browser with MediaRecorder) on `http://localhost:5173` or HTTPS; microphone
access may be unavailable on insecure non-localhost origins. You can deny permission
and continue using typed answers.

The control shows permission, recording, stopped, uploading, accepted, and error
states. Failed uploads can be retried; Record Answer replaces the previous recording.
Recording stops after five minutes or when you stop it manually. Audio is limited to
10 MiB. Microphone tracks are released on stop, errors, question changes, and unmount,
including permission requests that resolve after leaving the question. Upload requests
are cancelled when leaving the question and time out after 30 seconds.

The browser chooses a supported WebM/Opus, Ogg/Opus, or MP4 recording format, falling
back to its default encoder. The actual recorded MIME type is preserved in multipart
uploads. An unsupported default format receives an actionable backend error.

Audio acceptance **does not advance the question** or save an attempt. Submit a typed
or reviewed transcript attempt, review it, then use explicit Continue to advance.
The upload-only action does not transcribe; use Transcribe Recording for
speech-to-text. AI follow-ups, voice synthesis, and permanent audio storage remain absent.
Recordings remain temporarily in browser memory; replacing the recording or leaving the
question releases the reference. The backend discards uploads after validation. Multipart
parsing may spool larger files to temporary storage; those files are closed and removed
at the end of the request. Raw audio is not logged or included in the response.

### Audio API

`POST /api/sessions/{session_id}/audio` accepts multipart fields:

- `question_index`: zero-based non-negative integer for the current question.
- `audio`: nonempty file, at most 10 MiB, with an allowed audio content type.

Allowed base MIME types: `audio/webm`, `audio/ogg`, `audio/mp4`, `audio/mpeg`,
`audio/wav`, and `audio/x-wav`; codec parameters are preserved. Validation checks
content-type metadata, not decoded audio content or speech quality. The total request
body is bounded to 10 MiB plus 64 KiB multipart overhead, including streamed requests
without Content-Length. The parser permits only one file and one field.

Successful response (HTTP 200):

```json
{
  "session_id": "<existing session UUID>",
  "question_index": 0,
  "filename": "answer-1.webm",
  "content_type": "audio/webm;codecs=opus",
  "size_bytes": 12345,
  "status": "accepted"
}
```

The filename is generated by the server; client filenames and filesystem paths are
not returned. Errors: 404 for unknown sessions, 409 for completed sessions or stale/future
questions, 413 for excessive size, 415 for unsupported media types, and 422 for missing,
invalid, or empty inputs. Multipart parser limits/malformed form data can return 400.
The current-question check runs after transfer to reject concurrent typed-answer changes.
This upload-only action persists no audio and creates no measurement; existing
local-prototype authentication limitations apply.

### Manual Chrome verification

1. From the project root, activate `backend/.venv` and install the updated dependencies:
   `python -m pip install -r backend/requirements-dev.txt`.
2. Start FastAPI: `python -m uvicorn app.main:app --app-dir backend --reload --host 127.0.0.1 --port 8000`.
3. In another terminal, run `cd frontend`, `npm ci`, then `npm run dev` using Node.js 24.
4. Open `http://localhost:5173` in Chrome and confirm “Backend connected”. If microphone
   permission was previously saved, reset it using the site controls beside the address
   bar, then reload so this test includes the permission prompt.
5. Click **Start Interview**, then **Record Answer**. Confirm Chrome requests microphone
   permission only after that click. Choose **Allow**.
6. Speak for several seconds. Confirm “Recording… Microphone is active.” and **Stop Recording**.
7. Click **Stop Recording**. Confirm “Recording stopped. Ready to send.” and that Chrome's
   active-microphone indicator turns off (the site's permission may remain allowed).
8. Click **Send Recording**. Confirm “Uploading recording…” then “Recording accepted…”.
   In DevTools Network, inspect `POST /api/sessions/{id}/audio`: HTTP 200 and metadata
   matching the current question, MIME type, and a positive byte count; no raw audio response.
9. Confirm the question has not advanced. Type an answer and click **Submit Attempt**;
   confirm saved review remains on that question. Retry opens a fresh draft on the
   same question; Continue opens the next question with fresh recording controls.
   Submit then Continue on all five questions and verify completion and restart.
10. Reset microphone permission and repeat while choosing **Block**; confirm a useful
    permission error and that typed answers still work. Also stop the backend after
    recording, send, verify the upload error, restart it, and start a new interview.

Automated coverage uses mocked browser media APIs; real permission prompts, microphone
hardware, encoded audio, and Chrome's active-microphone indicator require this manual check.
The existing CI commands remain unchanged: `python -m pytest -W error`, `npm test`,
`npm run build`, and `npm run lint` (frontend commands run in `frontend/`).

Historical pre-Retry manual verification passed in Chrome on Mac for microphone
permission, recording, and audio acceptance. Current retry progression is verified
by automated lifecycle checks; these earlier manual results do not claim a new live
provider or microphone test of this milestone.


## Speech-to-text transcription (Issue #7)

**Record Answer → Stop Recording → Transcribe Recording → review/edit → Submit Attempt
→ saved review → Retry or Continue**.
ElevenLabs Scribe v2 currently transcribes recordings through the official Python SDK.
The backend adapter maps results to application-owned text, optional language, and
word timestamps (`text`, `start`, `end`, in seconds). The original transcript and
valid word timings now support deterministic speaking measurements described below.
Spacing/audio-event entries and words without timing are omitted from the timing list.

Transcription never submits an answer or advances the interview. The existing textarea
receives the transcript and remains editable after the request finishes. To protect
user work, Transcribe Recording is disabled whenever the textarea contains any text
(including whitespace). Clear it explicitly before transcribing. The textarea and
Submit Attempt are disabled while transcription is pending. Definite transcription
failures allow manual retry or typing. Conflicts reload saved state; uncertain results
require Recheck saved state before further mutations and clear unsafe audio/measurement
state. Duplicate requests are blocked; leaving the component aborts the browser request
and ignores late results.

Send Recording remains the upload-only validation action and needs no provider key.
Transcribe Recording is a separate operation; sending first is not required.

### Deterministic Speaking Metrics v1

After successful transcription, the draft editor shows **Words**, **Um**, **Uh**,
**Timed speech span**, and **Estimated WPM**. These are deterministic measurements,
not scores, quality judgments, coaching, readiness or confidence assessments, or
semantic feedback.

Word count uses the same deterministic lexical tokenization for every language:
alphanumeric runs count as words, with internal straight/curly apostrophes and ASCII
hyphens keeping contractions and hyphenated words together; punctuation alone does not count.
Only standalone `um` and `uh` tokens are counted as fillers, ignoring letter case,
and only when the transcription language is exactly `eng` (the supported English
representation). Missing or unsupported language makes filler counts unavailable,
rather than zero. Contextual fillers such as `like` are not detected.

Timing measurements require complete word timing coverage matching the transcript,
valid ordered non-overlapping intervals, and a usable positive span. Missing,
incomplete, or unusable timing makes both timing measurements unavailable; Rehearse
does not interpolate missing data. Timed speech span runs from the first timed
lexical word's start to the last one's end. It includes intervening time but excludes
leading/trailing recording silence and is not full recording duration. Estimated
WPM is recognized word count × 60 / timed speech span, calculated only when that
timing evidence is valid. The UI rounds span to one decimal place and WPM to a whole
number in the original-recording panel; the comparison table displays both span and
WPM to one decimal place. Response values retain their precision. These speaking
calculations, filler rules and timing-reason precedence remain unchanged by delivery v1.

The panel states: “Based on your original recording. Editing the transcript won’t
change these measurements.” Measurements describe the original transcribed recording;
editing the draft does not recalculate them. The backend stores their exact unrounded
values in an immutable `transcription_measurements` record tagged `speaking-metrics-v1`.
History and Progress read only measurements explicitly linked to saved attempts;
Progress uses finalized questions only. The response supplies an opaque `measurement_id`
that the frontend keeps beside the draft and preserves through same-draft edits,
including delete/retype. A replacement recording clears them immediately, a new
successful transcription replaces them, and leaving the question/session clears them.
Typed-only answers show no measurements panel and submit a null measurement ID.
Retry clears the text,
ID, metrics and audio through a fresh draft generation; its new transcription receives
a new UUID. A typed retry therefore cannot inherit a previous attempt's measurement.
Explicit submission of an edited transcript links the original measurement without
recalculation. The same lifecycle applies to the separate delivery facts on that
measurement.

### Deterministic Delivery Metrics v1

The **Timed pauses** section shows **Pause count**, **Total pause time** and
**Longest pause**, tagged `pause-metrics-v1` with source `original_transcription`.
Speaking and delivery retain separate versions and compatibility checks, sharing
the source and UUID of one immutable original-recording measurement.

For consecutive validated lexical words, the engine computes:

```python
gap = Decimal(str(next_word.start)) - Decimal(str(previous_word.end))
qualifying_pause = gap >= Decimal("0.50")
```

The 0.50-second boundary is inclusive. There is no epsilon or rounding before
classification: the full qualifying gap contributes to the total and longest
duration. An isolated Decimal context preserves this arithmetic independently of
the caller's context. The linear-time scan uses the same lexical eligibility,
coverage, interval/order checks and timing-reason precedence as speaking metrics.
One valid timed word with a usable positive span yields measured zero pauses.
No pause-event array is created or persisted. Duration display rounding happens
only after calculation; persisted scalar values stay unrounded.

The three approved aggregate fields are `pause_count`,
`total_pause_duration_seconds` and `longest_pause_seconds`. Estimated WPM remains
the existing pacing fact; no additional pacing metric is calculated. Timed gaps
between recognized words are **not necessarily acoustic silence**. They do not
establish hesitation, confidence, fluency, answer quality, emotion, pronunciation,
pitch, loudness, vocal variety or energy. There is no AI delivery score, pause
diagnosis or personalized coaching.

Three states remain distinct across Practice, comparison, History and Progress:

- **Measured zero:** count `0`, total `0.0`, longest `0.0`, reason `null`.
- **Recorded unavailable:** delivery version/source are present, all three numeric
  values are `null`, and the timing reason explains the unavailable evidence.
- **Not recorded:** historical rows have all five delivery columns `null`; History
  exposes `delivery_metrics: null`. This is different from recorded unavailable
  and from a typed attempt with no measurement at all.

Practice explains that editing the transcript does not change original-recording
measurements. Retry clears both metric families; a successful new transcription
produces one new measurement UUID. History includes delivery on selected saved
attempts, while Progress uses only exact linked final attempts, as described above.

### Server configuration

Install the updated requirements in the backend virtual environment:

```sh
python -m pip install -r backend/requirements-dev.txt
```

Adaptive interviewer follow-up questions use Gemini Interactions API. Set
`GEMINI_API_KEY` in the backend process environment only. Rehearse sends the bounded
question-and-answer history for each generated follow-up and sets `store=false`; it
does not use Gemini's server-side conversation history. The default model is
`gemini-3.5-flash`; `GEMINI_MODEL` can override it. The candidate's first question
continues to come from Rehearse's existing scenario catalog. Gemini only proposes
follow-up text; Rehearse validates it and commits it through the existing session
transaction. Answer diagnosis remains a separate provider path.

For the project's macOS zsh terminal, enter the Gemini API key without echoing it or
putting its value into shell history, then restart Uvicorn:

```zsh
read -rs "GEMINI_API_KEY?Gemini API key: "
echo
export GEMINI_API_KEY
python -m uvicorn app.main:app --app-dir backend --reload --host 127.0.0.1 --port 8000
```

Get the key in [Google AI Studio](https://aistudio.google.com/app/apikey). Never use a
`VITE_` variable for this key, include it in frontend configuration, or commit a real
value. Without it, session creation and deterministic opening questions still work;
requesting an adaptive follow-up returns a controlled unavailable response.

Set `ELEVENLABS_API_KEY` in the **backend process environment only**, then start/restart
Uvicorn. For the project's macOS zsh terminal, enter the key without echoing it or putting
its value into shell history:

```zsh
read -rs "ELEVENLABS_API_KEY?ElevenLabs API key: "
echo
export ELEVENLABS_API_KEY
python -m uvicorn app.main:app --app-dir backend --reload --host 127.0.0.1 --port 8000
```

`.env.example` contains empty provider/database configuration placeholders. `.env` files are ignored, but the
application does **not** automatically load them; no dotenv dependency is needed.
Never use a `VITE_` variable for this key, include it in frontend configuration, or
commit a real value. The application and all non-transcription endpoints work without
the key. A transcription request without configuration returns a controlled 503.
CI/tests require no real key and use fake transcribers or an SDK mock HTTP transport.

### Transcription API and errors

`POST /api/sessions/{session_id}/transcriptions` requires multipart `audio`,
`question_index`, and `expected_last_attempt_number` (a non-negative integer matching
the authoritative current revision). Audio limits are the same as `/audio`, which
does not accept the revision field. Shared validation rejects invalid uploads before
contacting the provider. Session/question/revision are checked before inference and
again under a new short database row lock before storing a measurement. This rejects
a stale result even if a newer attempt stayed on the same question.
HTTP 200 returns only application metadata, for example:

```json
{
  "session_id": "<existing session UUID>",
  "question_index": 0,
  "measurement_id": "<new opaque measurement UUID>",
  "text": "Hello there.",
  "language": "eng",
  "words": [
    {"text": "Hello", "start": 0.0, "end": 0.5},
    {"text": "there.", "start": 0.5, "end": 1.0}
  ],
  "metrics": {
    "source": "original_transcription",
    "recognized_word_count": 2,
    "um_count": 0,
    "uh_count": 0,
    "filler_unavailable_reason": null,
    "timed_utterance_span_seconds": 1.0,
    "estimated_words_per_minute": 120.0,
    "timing_unavailable_reason": null
  },
  "delivery_metrics": {
    "version": "pause-metrics-v1",
    "source": "original_transcription",
    "pause_count": 0,
    "total_pause_duration_seconds": 0.0,
    "longest_pause_seconds": 0.0,
    "unavailable_reason": null
  }
}
```

The existing `text`, `language`, `words`, `metrics` and UUID `measurement_id`
fields remain unchanged; `delivery_metrics` is an additive, required scalar-only
sibling. The same UUID identifies both metric families. Word timing entries remain
transient HTTP data and are not persisted. Unavailable numeric measurements use `null`;
zero remains a real word/filler count when applicable. Available measurements have
a `null` unavailable reason. `filler_unavailable_reason` is `unsupported_language`
when both filler counts are unavailable. `timing_unavailable_reason` identifies
`missing_timings`, `timing_coverage_mismatch`, `invalid_timing`,
`invalid_timing_order`, or `unusable_span` when both timing values are unavailable.
A successful transcription can still return HTTP 200 with unavailable measurements;
the UI shows “Unavailable” with an explanation. Existing transcription errors below
remain unchanged. Recorded unavailable delivery uses the same five timing reasons
in `delivery_metrics.unavailable_reason`, with all three numeric values `null`;
available delivery uses a null reason. Neither this object nor any delivery UI
causes an extra provider request.

Existing validation statuses remain: 400 malformed multipart/parser limits, 404 unknown
session, 409 completed/stale question or attempt revision, 413 oversized upload,
415 unsupported media, and 422 invalid/empty input. Transcription adds:

- 503: server API key is missing/blank.
- 504: provider network timeout or the 60-second total provider deadline expires.
- 502: provider failure (including rejected credentials/quota), malformed or empty
  result, invalid timings, or transcript over the existing 10,000-character answer limit.

Errors are generic and do not expose provider exceptions, credentials, or raw responses.
The SDK does not automatically retry; the user may retry explicitly. Browser requests
have a 75-second timeout to allow for upload and the backend provider deadline.

### Privacy and limitations

Transcribe Recording sends the recording to ElevenLabs. Rehearse does not permanently
store audio, log raw audio/keys/provider responses, or persist transcripts separately.
Temporary upload files close after success or failure. Draft transcripts stay in the
browser; explicitly submitted text and its optional original measurement association
are stored in PostgreSQL. No separate original-transcript copy, word timing arrays
or pause-event arrays are stored; raw timings are used transiently for deterministic
calculations. The delivery extension persists only five scalar/version/reason
columns on the existing immutable measurement. There is no new behavioral-content
logging, and delivery facts are never written to localStorage; the History registry
retains opaque IDs only.
ElevenLabs processing/retention is governed by your provider account and policies;
Rehearse's lack of permanent audio storage is not a promise of provider-side deletion.
Aborting a browser request does not guarantee cancellation of provider work already
started. The backend deadline bounds how long Rehearse waits.

This remains a local prototype without authentication or rate limits. Keep the
key-enabled backend local. Speaking Metrics v1 adds no semantic scoring, coaching
judgments, adaptive interviewing, or pause diagnosis. Delivery v1 adds timed-gap
aggregates, with no acoustic-silence or qualitative interpretation. Browser-local
History and Progress now display persisted facts over time without semantic interpretation.
Immutable measurement snapshots and all saved attempts are persistent. Timing remains unavailable
when evidence is insufficient. Realtime transcription, Nemotron, AI follow-ups, TTS,
and authentication remain absent.

### Manual verification with a real key (outside the offline release audit)

1. Install backend requirements and configure the key in the backend terminal as above.
   Start Uvicorn. In another terminal with Node.js 24, run `cd frontend`, `npm ci`,
   and `npm run dev`. Open `http://localhost:5173` in Chrome.
2. Start an interview. Leave the answer empty, click Record Answer, allow microphone
   access, speak a short sentence, and Stop Recording. Check microphone release.
3. Click **Transcribe Recording** directly. Confirm the loading state and disabled
   transcription/textarea/submit controls while waiting.
4. Confirm the transcript appears in the existing textarea and the question has not
   advanced. In DevTools Network, inspect the transcription response for text,
   language, and word timing entries. No API key should appear in browser requests.
5. Edit the transcript, then click **Submit Attempt**. Confirm saved review stays on
   the same question. Retry should clear the draft/recording/measurement; a new voice
   attempt uses its own UUID. Only Continue advances. Submit then Continue through
   all questions and verify explicit restart.
6. Type a draft before requesting transcription. Confirm Transcribe Recording is
   disabled and the draft stays intact. Clear the textarea to enable transcription.
7. Stop Uvicorn, run `unset ELEVENLABS_API_KEY`, and restart it. Start a new interview
   (persisted sessions survive restart), record, and transcribe. Confirm the controlled
   configuration error and that typed answers and Send Recording still work.
8. Restore the key using hidden input and restart for further manual tests. To test
   browser network failure, record first, stop the backend, and attempt transcription;
   confirm an uncertain-result message blocks mutations until the backend returns
   and Recheck saved state succeeds. No request should be silently replayed.

Automated tests verify SDK request shape/mapping with mocked transport, not real
provider credentials, billing, audio recognition quality, or live provider latency.
Historical pre-Retry manual end-to-end verification passed in Chrome with a spoken
answer and ElevenLabs Scribe v2: the transcript appeared in the textarea, could be reviewed/edited,
and did not advance until the then-current Submit Answer action. The current lifecycle
requires explicit Continue; no live provider verification was performed for this audit.
Existing-text protection also worked. An earlier 401 authentication failure was resolved by rotating
the API key; it was not an application-code defect.

For local development diagnostics only, set `REHEARSE_TRANSCRIPTION_DEBUG=1` in the
backend terminal before starting/restarting Uvicorn. It defaults to off; only the exact
value `1` enables it. During a failed transcription, one flushed line is written directly to backend stderr
(independent of Python/Uvicorn logger handlers). Look for `transcription_failure`
with `stage=provider_request` or `stage=result_mapping`, `provider_status` (numeric or
`unavailable`), and a fixed error category. No exception text, headers, body, audio,
transcript, or key is logged by this diagnostic. Client errors remain generic.
Use `unset REHEARSE_TRANSCRIPTION_DEBUG` and restart the backend to disable it.
