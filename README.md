# Rehearse

Rehearse is a communication practice platform in development for interviews,
public speaking, negotiations, and presentations. The current prototype supports
interviews with five planned questions, bounded adaptive interviewer prompts, typed
or transcribed draft answers, explicit submission, and a completion state.

## Current architecture

> **RESEARCH FROZEN — ISSUE #9 OPEN — NOT PRODUCTION APPROVED**
>
> Milestone 5 keeps FOLLOW_UP, CLARIFY, CHALLENGE and MOVE_ON unchanged.
> Automatic CHALLENGE remains unresolved. Conditional two-stage reasoning is
> retained as the frozen research architecture; Super is a baseline/comparator
> only, and Ultra was not selected. Further full-48 development evaluation is
> blocked for the frozen candidate. Held-out remains sealed / not authorized.
> Existing feature-branch route wiring is unapproved work, not merge/deployment
> authorization. See the [research freeze](evals/interviewer/RESEARCH_FREEZE.md)
> and [experiment ledger](evals/interviewer/EXPERIMENT_LEDGER.md).

- `frontend/`: React, TypeScript (strict mode), and Vite.
- `backend/app/`: FastAPI application served by Uvicorn; session routes call a
  separate in-memory session service with Pydantic request/response models.
- `tests/`: backend tests using pytest and FastAPI TestClient.
- `docs/`: reserved for future documentation.

React requests `GET /api/health` on page load. Vite's development proxy forwards
`/api` requests to `http://127.0.0.1:8000`, keeping browser requests on the same
origin without requiring CORS configuration. The health endpoint returns
`{"status":"ok","service":"rehearse-api"}`. The page displays “Backend connected”
on success or “Backend unavailable” on failure (including a five-second timeout).
The proxy applies to the development server only.

Click **Start Interview**, type an answer, and submit it for interviewer reasoning.
Nemotron recommends a follow-up, clarification, challenge, or moving on. Rehearse
enforces at most two extra prompts per planned question and controls completion.
No numeric scoring or candidate answer generation is implemented. Explicit submission
requires NVIDIA configuration whenever reasoning is needed; there is no silent fallback.

Session endpoints:

| Method | Endpoint | Behavior |
| --- | --- | --- |
| POST | `/api/sessions` | Creates a session (201), with its URL in `Location`. No body required. |
| GET | `/api/sessions/{id}` | Retrieves the session (200). |
| POST | `/api/sessions/{id}/answers` | Submits an answer and returns updated state (200). |

Answer body: `{"question_index":0,"turn_revision":0,"submission_id":"<UUID>","answer":"My answer"}`.
The index identifies the planned question; the monotonic revision identifies its
current interaction turn. Both must match. Reuse the submission UUID for an exact
retry; never reuse it with changed content. Answers must be strings with 1–10,000
characters after trimming whitespace. Invalid bodies or malformed UUIDs return
422; unknown session UUIDs return 404; completed sessions and stale/future question
indices return 409 without changing state.

Responses contain `id`, `status` (`active` or `completed`), `current_question_index`,
`current_question` (planned question), `current_prompt` (what to answer now),
`turn_revision`, `probe_count`, `questions`, `answers`, and `turns`. `answers` retains
the first answer to each planned question; `turns` retains every original and probe
answer separately, with its prompt, revision, submission UUID, applied action and
transition source. No synthesized final answer or provider reasoning trace is stored.
After completion, the index equals the question count and both current prompts are null.

Sessions live only in the backend process and disappear on restart/reload. Run one
Uvicorn worker: sessions are not shared between workers. Browser refresh resets
the UI; there is no session restoration, authentication, database, expiry, or scoring.
Anyone with a session ID can access that session. This is a local development prototype.

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

For the real-browser completion regression, use the offline backend fixture instead
of the normal backend (stop any other server on port 8000 first):

```sh
PYTHONPATH=backend python -m uvicorn e2e_app:app --app-dir tests --host 127.0.0.1 --port 8000
```

This fixture injects MOVE_ON decisions into the real API/session engine and never
calls NVIDIA. Do not use it for real interview practice. Keep Vite running, then
run from `frontend/`:

```sh
npx playwright install chromium
npm run test:integration
```

Alternatively, set `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH` to an installed Chrome
executable. This test uses real API requests and creates disposable in-memory
sessions. It checks all five answers, completion without a remount/navigation,
and an explicit restart. Failure traces are written to ignored `test-results/`.

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
python -m pytest -W error
```

## Continuous integration

GitHub Actions runs `.github/workflows/ci.yml` on pushes to `main` and pull
requests targeting `main`, with separate backend and frontend jobs on Ubuntu:

- Python 3.13: install runtime and test requirements, then run the complete pytest
  suite with warnings treated as errors.
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
10 MiB. Microphone tracks are released on stop, errors, turn changes, and unmount,
including permission requests that resolve after leaving the turn. Upload requests
are cancelled when leaving the turn and time out after 30 seconds.

The browser chooses a supported WebM/Opus, Ogg/Opus, or MP4 recording format, falling
back to its default encoder. The actual recorded MIME type is preserved in multipart
uploads. An unsupported default format receives an actionable backend error.

Audio acceptance **does not advance the question** or add a text answer. Continue with
a typed or reviewed transcript answer to advance through the existing five-question
interview. The upload-only action does not transcribe; use Transcribe Recording for
speech-to-text. Adaptive prompts occur only after explicit text submission; voice
synthesis and permanent audio storage remain absent.
Recordings remain temporarily in browser memory; replacing the recording or leaving the
question releases the reference. The backend discards uploads after validation. Multipart
parsing may spool larger files to temporary storage; those files are closed and removed
at the end of the request. Raw audio is not logged or included in the response.

### Audio API

`POST /api/sessions/{session_id}/audio` accepts multipart fields:

- `question_index`: zero-based non-negative integer for the current planned question.
- `turn_revision`: non-negative revision for the current interaction turn.
- `audio`: nonempty file, at most 10 MiB, with an allowed audio content type.

Allowed base MIME types: `audio/webm`, `audio/ogg`, `audio/mp4`, `audio/mpeg`,
`audio/wav`, and `audio/x-wav`; codec parameters are preserved. Validation checks
content-type metadata, not decoded audio content or speech quality. The total request
body is bounded to 10 MiB plus 64 KiB multipart overhead, including streamed requests
without Content-Length. The parser permits only one file and two fields.

Successful response (HTTP 200):

```json
{
  "session_id": "<existing session UUID>",
  "question_index": 0,
  "turn_revision": 0,
  "filename": "answer-1.webm",
  "content_type": "audio/webm;codecs=opus",
  "size_bytes": 12345,
  "status": "accepted"
}
```

The filename is generated by the server; client filenames and filesystem paths are
not returned. Errors: 404 for unknown sessions, 409 for completed sessions or stale/future
questions/turns, 413 for excessive size, 415 for unsupported media types, and 422 for missing,
invalid, or empty inputs. Multipart parser limits/malformed form data can return 400.
The current-turn check runs after transfer to reject concurrent typed-answer changes.
No authentication or persistence is added; existing local-prototype limitations apply.

### Manual Chrome verification

1. From the project root, activate `backend/.venv` and install the updated dependencies:
   `python -m pip install -r backend/requirements-dev.txt`.
2. Configure NVIDIA for explicit answer submission (see Issue #9 below). Start FastAPI: `python -m uvicorn app.main:app --app-dir backend --reload --host 127.0.0.1 --port 8000`.
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
9. Confirm the question has not advanced. Type an answer and click **Submit Answer**;
   confirm a probe or the next planned question appears with fresh recording controls.
   Answer any probes and complete all five planned questions and verify the completion screen and explicit restart still work.
10. Reset microphone permission and repeat while choosing **Block**; confirm a useful
    permission error and that typed answers still work. Also stop the backend after
    recording, send, verify the upload error, restart it, and start a new interview.

Automated coverage uses mocked browser media APIs; real permission prompts, microphone
hardware, encoded audio, and Chrome's active-microphone indicator require this manual check.
The existing CI commands remain unchanged: `python -m pytest -W error`, `npm test`,
`npm run build`, and `npm run lint` (frontend commands run in `frontend/`).

Historical voice-foundation verification passed in Chrome on Mac: microphone permission, recording start/stop,
audio upload and acceptance, and typed-answer submission/question advancement.


## Speech-to-text transcription (Issue #7)

**Record Answer → Stop Recording → Transcribe Recording → review/edit → Submit Answer**.
ElevenLabs Scribe v2 currently transcribes recordings through the official Python SDK.
The backend adapter maps results to application-owned text, optional language, and
word timestamps (`text`, `start`, `end`, in seconds). Word timings are preserved for
future deterministic pause/pacing/filler analysis; no such analysis is implemented.
Spacing/audio-event entries and words without timing are omitted from the timing list.

Transcription never submits an answer or advances the interview. The existing textarea
receives the transcript and remains editable after the request finishes. To protect
user work, Transcribe Recording is disabled whenever the textarea contains any text
(including whitespace). Clear it explicitly before transcribing. The textarea and
Submit Answer are disabled while transcription is pending. Failed requests keep the
recording for retry and unlock typing. Duplicate requests are blocked; leaving the
component aborts the browser request and ignores late results.

Send Recording remains the upload-only validation action and needs no provider key.
Transcribe Recording is a separate operation; sending first is not required.

### Server configuration

Install the updated requirements in the backend virtual environment:

```sh
python -m pip install -r backend/requirements-dev.txt
```

Set `ELEVENLABS_API_KEY` in the **backend process environment only**, then start/restart
Uvicorn. For the project's macOS zsh terminal, enter the key without echoing it or putting
its value into shell history:

```zsh
read -rs "ELEVENLABS_API_KEY?ElevenLabs API key: "
echo
export ELEVENLABS_API_KEY
python -m uvicorn app.main:app --app-dir backend --reload --host 127.0.0.1 --port 8000
```

`.env.example` contains only the empty variable name. `.env` files are ignored, but the
application does **not** automatically load them; no dotenv dependency is needed.
Never use a `VITE_` variable for this key, include it in frontend configuration, or
commit a real value. Recording and upload-only acceptance work without the ElevenLabs key;
text submission uses the separate NVIDIA configuration described below. A transcription request without configuration returns a controlled 503.
CI/tests require no real key and use fake transcribers or an SDK mock HTTP transport.

### Transcription API and errors

`POST /api/sessions/{session_id}/transcriptions` accepts the same multipart `audio`,
`question_index` and `turn_revision` fields and limits as `/audio`. Shared validation rejects invalid
uploads before contacting the provider. The session/question/turn is checked again after
transcription to reject an answer that became stale while the provider was running.
HTTP 200 returns only application metadata, for example:

```json
{
  "session_id": "<existing session UUID>",
  "question_index": 0,
  "turn_revision": 0,
  "text": "Hello there.",
  "language": "eng",
  "words": [{"text": "Hello", "start": 0.0, "end": 0.5}]
}
```

Existing validation statuses remain: 400 malformed multipart/parser limits, 404 unknown
session, 409 completed/stale question or turn, 413 oversized upload, 415 unsupported media,
and 422 invalid/empty input. Transcription adds:

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
browser; explicitly submitted text is sent to NVIDIA for interviewer reasoning and,
on successful acceptance, uses the existing process-local session storage.
ElevenLabs processing/retention is governed by your provider account and policies;
Rehearse's lack of permanent audio storage is not a promise of provider-side deletion.
Aborting a browser request does not guarantee cancellation of provider work already
started. The backend deadline bounds how long Rehearse waits.

This remains a local prototype without authentication or rate limits. Keep the
key-enabled backend local. There is no realtime transcription, TTS, database,
authentication, or additional scoring/measurements. Issue #9 adds bounded Nemotron
interviewer reasoning after explicit answer submission.

### Manual verification with a real key

1. Install backend requirements and configure both provider keys in the backend terminal.
   The recording/transcription steps require ElevenLabs; submission requires NVIDIA.
   Start Uvicorn. In another terminal with Node.js 24, run `cd frontend`, `npm ci`,
   and `npm run dev`. Open `http://localhost:5173` in Chrome.
2. Start an interview. Leave the answer empty, click Record Answer, allow microphone
   access, speak a short sentence, and Stop Recording. Check microphone release.
3. Click **Transcribe Recording** directly. Confirm the loading state and disabled
   transcription/textarea/submit controls while waiting.
4. Confirm the transcript appears in the existing textarea and the question has not
   advanced. In DevTools Network, inspect the transcription response for text,
   language, and word timing entries. No API key should appear in browser requests.
5. Edit the transcript, then click **Submit Answer**. Confirm only this creates a
   follow-up turn or advances to the next planned question. Continue to completion and verify explicit restart.
6. Type a draft before requesting transcription. Confirm Transcribe Recording is
   disabled and the draft stays intact. Clear the textarea to enable transcription.
7. Stop Uvicorn, run `unset ELEVENLABS_API_KEY`, and restart it. Start a new interview
   (sessions reset on restart), record, and transcribe. Confirm the controlled
   configuration error and that typed answers and Send Recording still work.
8. Restore the key using hidden input and restart for further manual tests. To test
   browser network failure, record first, stop the backend, and attempt transcription;
   confirm an error and that the textarea becomes editable again.

Automated tests verify SDK request shape/mapping with mocked transport, not real
provider credentials, billing, audio recognition quality, or live provider latency.
Historical Issue #7 end-to-end verification passed in Chrome with a real spoken answer and
ElevenLabs Scribe v2: the transcript appeared in the textarea, could be reviewed/edited,
and did not advance the question until Submit Answer was clicked. Existing-text
protection also worked. An earlier 401 authentication failure was resolved by rotating
the API key; it was not an application-code defect.

For local development diagnostics only, set `REHEARSE_TRANSCRIPTION_DEBUG=1` in the
backend terminal before starting/restarting Uvicorn. It defaults to off; only the exact
value `1` enables it. During a failed transcription, one flushed line is written directly to backend stderr
(independent of Python/Uvicorn logger handlers). Look for `transcription_failure`
with `stage=provider_request` or `stage=result_mapping`, `provider_status` (numeric or
`unavailable`), and a fixed error category. No exception text, headers, body, audio,
transcript, or key is logged by this diagnostic. Client errors remain generic.
Use `unset REHEARSE_TRANSCRIPTION_DEBUG` and restart the backend to disable it.

## Structured interviewer reasoning (Issue #9 / Milestone 5)

Code enforces measurable limits and session transitions. Nemotron judges what to
ask next. ElevenLabs handles transcription. Rehearse controls the interview.

### Decisions and state

The provider returns exactly `action`, `reason`, and `next_prompt`. Actions are:

- FOLLOW_UP: relevant, understandable answer missing useful detail.
- CLARIFY: ambiguity prevents understanding, or the response is irrelevant.
- CHALLENGE: probe the reasoning, evidence, assumptions, decisions or tradeoffs.
- MOVE_ON: sufficiently complete/useful; `next_prompt` must be null.

The first three actions require a nonempty next prompt (1–500 trimmed characters).
The explanation is 1–300 trimmed characters. Strict validation rejects unknown
fields/actions, missing fields, coercion, duplicate JSON keys, malformed JSON,
prose-wrapped JSON and inconsistent action/prompt combinations. No JSON repair is
attempted. Reasons are short application explanations, not private chain-of-thought;
they and provider reasoning traces are neither logged nor persisted/returned.

Only Submit Answer invokes reasoning. The backend takes an immutable snapshot,
reserves the current turn, releases its lock, awaits advice, validates it, then
rechecks and commits under the lock. The provider has no session service or tools.
For probes the planned question stays fixed and the turn revision increases.
MOVE_ON advances exactly one planned question. After two generated probes, the
engine accepts the next answer and advances without another provider request;
that turn has `transition_source="probe_limit"` and `action=null`, not a fabricated
model decision. Five questions therefore take between five and fifteen submissions.

Completed retries with the same submission UUID and payload return current session
state without another provider call. Reusing a committed UUID with different content
returns 409. Pending concurrent submissions return 409; retry explicitly after the
first finishes. Failed/cancelled calls release the reservation without adding an
answer. The browser keeps a submission UUID for an unchanged draft/turn, reconciles
accepted retries by that UUID, and preserves drafts on errors or unrelated stale
turns. These guarantees are process-local and last only for the session lifetime.

Audio upload and transcription require the exact turn revision, including on
follow-ups to the same planned question. Late transcription responses are rejected
by the server and ignored by the browser after remount. The recorder remounts on
session/revision changes and releases its microphone. No ElevenLabs adapter behavior
was changed. Transcription still only fills an editable draft.

### NVIDIA configuration

Set `NVIDIA_API_KEY` in the backend environment, then start/restart Uvicorn. In zsh:

```zsh
read -rs "NVIDIA_API_KEY?NVIDIA API key: "
echo
export NVIDIA_API_KEY
python -m uvicorn app.main:app --app-dir backend --reload --host 127.0.0.1 --port 8000
```

`.env.example` contains empty placeholders only. The application does not load
.env files automatically. Never use a VITE_ variable or put either key in browser
configuration. No new dependency is required: the adapter uses existing httpx.

The versioned `nemotron-super-v1` configuration in `backend/app/nemotron.py` uses:

| Setting | Value |
| --- | --- |
| Endpoint | `https://integrate.api.nvidia.com/v1/chat/completions` |
| Model | `nvidia/nemotron-3-super-120b-a12b` |
| Prompt version in current application source | `interviewer-v5` |
| Streaming / retries | false / none |
| Temperature / top_p | 1.0 / 0.95 |
| Reasoning effort / budget | low / 256 tokens |
| Maximum generated tokens | 1024 |
| Provider and orchestration deadline | 30 seconds |
| Browser answer POST timeout | 45 seconds |
| Encoded request / response limits | 150,000 / 65,536 bytes |

Sampling follows the [NVIDIA model card](https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-BF16).
The [hosted API reference](https://docs.api.nvidia.com/nim/reference/nvidia-nemotron-3-super-120b-a12b-infer)
documents reasoning controls. Low effort and a small budget are an initial economical
configuration, not a proven minimum adequate setting. The evaluation CLI exposes
sampling and reasoning overrides for comparison. Output is not deterministic.
No self-hosted NIM `guided_json` capability is assumed for the hosted endpoint;
application validation is mandatory and is the implemented constraint.

### Errors and privacy

Missing configuration returns 503; provider/network deadlines return 504; provider
rejection or malformed/invalid decisions return 502. Invalid client input returns
422, missing sessions 404, and stale/conflicting turns 409. These failures never
silently advance or commit the answer. The draft stays editable for retry. Startup,
health, session creation, audio upload and ElevenLabs transcription need no NVIDIA
key, but answer submission requires it whenever reasoning is needed.

Explicit submission sends the edited/typed answer and bounded prior turns for that
planned question to NVIDIA. No raw audio, session UUID, other-question history,
credentials in prompts, or evaluation labels are sent. Accepted text/prompt history
is held in process memory; model explanations and reasoning traces are discarded.
There is no new provider diagnostic logging, no response body/header/exception
logging, and no frontend key. NVIDIA processing/retention follows your provider
account terms; local non-persistence does not promise provider-side deletion.
Cancellation cannot guarantee already-started provider work is cancelled or unbilled.
The prototype remains single-process, without authentication, global rate limits,
persistence or deployment changes. Keep the configured server local.

### Evaluation and verification

See [the rubric and evaluation instructions](evals/interviewer/README.md). The dataset
has 80 synthetic cases (48 development, 32 held-out); dataset label review was
completed, and intermediate development annotations received separate owner review.
This does not establish a validated benchmark or authorize held-out inspection.
Reports include overall action match, per-action support/precision/recall/F1,
macro-F1, confusion matrix, category results, failure rates, median/p95 latency and
optional repeat agreement. Every attempted case stays in the denominator.

Normal CI tests schema/state/provider boundaries, dataset integrity and metric
arithmetic using fake services/mock transport, with real provider transport blocked.
No live evaluation, provider credential or new workflow is required. Run:

```sh
python -m pytest -W error
cd frontend
npm test
npm run lint
npm run build
```

Historical initial implementation checkpoint: no live NVIDIA evaluation or quality
threshold had yet been established. Subsequent development experiments and frozen
gates are recorded in the [experiment ledger](evals/interviewer/EXPERIMENT_LEDGER.md);
none established production approval. Milestone 5 remains open and research frozen.
Further full-development evaluation is blocked and held-out remains sealed / not
authorized. Historical configuration/manual commands are not authorization for
provider inference.

Historical manual acceptance checklist, not authorization to execute:
with both keys configured, verify Record → Stop → Transcribe → Review/Edit →
Submit; transcription alone must not advance, while submission may generate a probe.
Confirm fresh recording controls on same-question probes, the two-probe limit,
completion/restart, and preservation of a draft on a reasoning error. The offline
Playwright server above remains available for the deterministic completion regression.
