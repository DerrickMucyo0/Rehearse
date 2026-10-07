import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { ApiError, continueQuestion, getAttempts, getComparison, getSemanticDiagnosis, getSession, isConflictError, SemanticDiagnosisError, startInterview, submitAttempt, transcribeAudio, uploadAudio } from './interviewApi'
import type { Attempt, DeliveryComparison, DeliveryMetricChange, InterviewSession, MetricChange, SemanticDiagnosis } from './interviewApi'
import { DELIVERY_TIMING_REASONS } from './deliveryMetrics'
import type { DeliveryMetrics } from './deliveryMetrics'
import semanticDiagnosisContract from './fixtures/semanticDiagnosis.v1.json?raw'
import { authenticateTestWorkspace } from './authTestUtils'
import { AUTH_CONTEXT_HEADER, AUTH_UNAVAILABLE_MESSAGE, getAuthState } from './auth'

const session: InterviewSession = {
  id: 'session-1', status: 'active', current_question_index: 2, current_question: 'Third',
  current_question_latest_attempt_number: 3, questions: ['First', 'Second', 'Third', 'Fourth'], answers: ['One', 'Two'],
}
const attempt: Attempt = {
  id: 'attempt-4', question_index: 2, attempt_number: 4, answer: 'New attempt',
  submitted_at: '2026-10-04T12:00:00Z', measurement_id: null,
}
const measurementId = 'aed74a31-ddc3-4e0a-b2aa-b98ad52f7b61'
const zeroDelivery: DeliveryMetrics = { version: 'pause-metrics-v1', source: 'original_transcription',
  pause_count: 0, total_pause_duration_seconds: 0, longest_pause_seconds: 0, unavailable_reason: null }
function legacyDelivery(): DeliveryComparison {
  const unavailable: DeliveryMetricChange = { before: null, after: null, delta: null,
    before_unavailable_reason: 'not_recorded', after_unavailable_reason: 'not_recorded', comparable: false,
    comparison_unavailable_reason: 'both_unavailable' }
  return { before_version: null, after_version: null, before_source: null, after_source: null,
    pause_count: unavailable, total_pause_duration_seconds: unavailable, longest_pause_seconds: unavailable }
}
function json(value: unknown, status = 200) { return new Response(JSON.stringify(value), { status }) }
function mockResponse(response: Response) {
  const fetchMock = vi.fn().mockResolvedValue(response)
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}
beforeEach(async () => { await authenticateTestWorkspace() })
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks() })

test.each([
  ['session', () => getSession(session.id)],
  ['start', () => startInterview()],
  ['attempt submission', () => submitAttempt(session, 'New attempt')],
  ['continue', () => continueQuestion(session)],
  ['attempt history', () => getAttempts(session)],
  ['comparison', () => getComparison(session)],
  ['audio upload', () => uploadAudio(session, new Blob(['audio']), new AbortController().signal)],
  ['transcription', () => transcribeAudio(session, new Blob(['audio']), new AbortController().signal)],
  ['diagnosis', () => getSemanticDiagnosis(session.id, 2, 4, new AbortController().signal)],
] as const)('every %s Practice API uses the shared context-bearing same-origin boundary', async (_name, invoke) => {
  const fetchMock = mockResponse(json({}))
  await invoke().catch(() => undefined)
  expect(fetchMock).toHaveBeenCalledTimes(1)
  const [path, options] = fetchMock.mock.calls[0]
  expect(path).toMatch(/^\/api\//)
  expect(options.credentials).toBe('same-origin')
  const headers = new Headers(options.headers)
  expect(headers.get(AUTH_CONTEXT_HEADER)).toBe('context-A')
  expect(headers.has('Authorization')).toBe(false)
  expect(headers.has('Cookie')).toBe(false)
  expect(headers.has('X-User-Id')).toBe(false)
})

test.each([[401, 'signed_out'], [403, 'stale']] as const)(
  'attempt mutation %s preserves auth boundary invalidation and is never replayed', async (status, expected) => {
    const fetchMock = mockResponse(json({ detail: 'private response' }, status))
    await expect(submitAttempt(session, 'New attempt')).rejects.toMatchObject({ name: 'AuthBoundaryError', status })
    expect(getAuthState()).toEqual({ status: expected })
    expect(fetchMock).toHaveBeenCalledTimes(1)
  },
)

test('a late Practice JSON body cannot cross from authenticated A into B', async () => {
  let resolve!: (value: unknown) => void
  const response = json(session)
  const body = vi.spyOn(response, 'json').mockReturnValue(new Promise((done) => { resolve = done }))
  mockResponse(response)
  const pending = getSession(session.id).catch((cause: unknown) => cause)
  await vi.waitFor(() => expect(body).toHaveBeenCalledTimes(1))
  await authenticateTestWorkspace('context-B', '144b50e1-0183-428c-943f-1850df006b66')
  resolve(session)
  expect(await pending).toMatchObject({ name: 'AuthBoundaryError' })
  expect(getAuthState()).toMatchObject({ status: 'authenticated', requestContext: 'context-B' })
})

test('reads the authoritative session without submitting an answer', async () => {
  const fetchMock = mockResponse(json(session))
  expect(await getSession(session.id)).toEqual(session)
  expect(fetchMock.mock.calls[0][0]).toBe('/api/sessions/session-1')
  expect(fetchMock.mock.calls[0][1].method).toBeUndefined()
})

test('starts a session with the required current-question attempt revision', async () => {
  const fresh = { ...session, current_question_latest_attempt_number: 0 }
  const fetchMock = mockResponse(json(fresh, 201))
  expect(await startInterview()).toEqual(fresh)
  expect(fetchMock.mock.calls[0][0]).toBe('/api/sessions')
  expect(fetchMock.mock.calls[0][1].method).toBe('POST')
})

test.each([null, measurementId])('submits an append-only attempt with exact revision and measurement %s', async (id) => {
  const saved = { attempt: { ...attempt, measurement_id: id }, session: { ...session, current_question_latest_attempt_number: 4 } }
  const fetchMock = mockResponse(json(saved, 201))
  expect(await submitAttempt(session, 'New attempt', id)).toEqual(saved)
  expect(fetchMock).toHaveBeenCalledTimes(1)
  expect(fetchMock.mock.calls[0][0]).toBe('/api/sessions/session-1/questions/2/attempts')
  expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
    answer: 'New attempt', expected_last_attempt_number: 3, measurement_id: id,
  })
})

test('Continue sends the exact selected question revision separately from submission', async () => {
  const next = { ...session, current_question_index: 3, current_question: 'Fourth', current_question_latest_attempt_number: 0, answers: [...session.answers, 'Selected'] }
  const fetchMock = mockResponse(json(next))
  expect(await continueQuestion(session)).toEqual(next)
  expect(fetchMock.mock.calls[0][0]).toBe('/api/sessions/session-1/questions/2/continue')
  expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ expected_last_attempt_number: 3 })
})

test('reads persisted attempt history scoped to the active question', async () => {
  const history = [{ ...attempt, attempt_number: 1 }, { ...attempt, attempt_number: 3 }]
  const fetchMock = mockResponse(json(history))
  expect(await getAttempts(session)).toEqual(history)
  expect(fetchMock.mock.calls[0][0]).toBe('/api/sessions/session-1/questions/2/attempts')
})

test.each([null, [{ ...attempt, question_index: 1 }], [{ ...attempt, attempt_number: 0 }], [attempt, attempt]])(
  'rejects malformed or wrong-question attempt history without exposing content (case %#)', async (history) => {
    mockResponse(json(history))
    await expect(getAttempts(session)).rejects.toMatchObject({ name: 'ApiError', ambiguousWrite: false })
  },
)

test('reads the nullable comparison without inventing values', async () => {
  const result = { session_id: session.id, question_index: 2, before_attempt: null, after_attempt: null, comparison: null, delivery_comparison: null }
  const fetchMock = mockResponse(json(result))
  expect(await getComparison(session)).toEqual(result)
  expect(fetchMock.mock.calls[0][0]).toBe('/api/sessions/session-1/questions/2/comparison')
})

test('requests explicit comparison selectors and preserves unrounded signed delta and measured zero', async () => {
  const metric: MetricChange = {
    before: 0, after: 0.123456789, delta: 0.123456789, before_unavailable_reason: null,
    after_unavailable_reason: null, comparable: true, comparison_unavailable_reason: null,
  }
  const identity = { id: 'attempt-1', attempt_number: 1, measurement_id: measurementId, measurement_version: 'speaking-v1', measurement_source: 'original_transcription' }
  const result = { session_id: session.id, question_index: 2, before_attempt: identity,
    after_attempt: { ...identity, id: 'attempt-3', attempt_number: 3 }, comparison: {
      recognized_word_count: metric, um_count: metric, uh_count: metric,
      timed_utterance_span_seconds: metric, estimated_words_per_minute: metric,
    }, delivery_comparison: legacyDelivery() }
  const fetchMock = mockResponse(json(result))
  expect(await getComparison(session, 1, 3)).toEqual(result)
  expect(fetchMock.mock.calls[0][0]).toBe('/api/sessions/session-1/questions/2/comparison?before=1&after=3')
})

test('HTTP 409 uses typed conflict metadata and ignores arbitrary response content', async () => {
  const privateMessage = 'private provider response and credential marker'
  mockResponse(new Response(privateMessage, { status: 409 }))
  const error = await submitAttempt(session, 'New attempt').catch((cause: unknown) => cause)
  expect(isConflictError(error)).toBe(true)
  expect(error).toMatchObject({ status: 409, ambiguousWrite: false })
  expect((error as Error).message).not.toContain(privateMessage)
})

test.each(['network', 'invalid-json', 'bad-contract'] as const)('marks %s attempt response as uncertain without retrying', async (failure) => {
  const fetchMock = vi.fn()
  if (failure === 'network') fetchMock.mockRejectedValue(new Error('private low-level failure'))
  else fetchMock.mockResolvedValue(failure === 'invalid-json' ? new Response('private malformed body') : json({ private: 'provider text' }))
  vi.stubGlobal('fetch', fetchMock)
  const error = await submitAttempt(session, 'New attempt').catch((cause: unknown) => cause)
  expect(error).toBeInstanceOf(ApiError)
  expect(error).toMatchObject({ ambiguousWrite: true })
  expect((error as Error).message).not.toMatch(/private|provider|low-level/)
  expect(fetchMock).toHaveBeenCalledTimes(1)
})

test('a malformed successful Continue confirmation is uncertain', async () => {
  mockResponse(json(session))
  await expect(continueQuestion(session)).rejects.toMatchObject({ ambiguousWrite: true })
})

test('a read network failure remains read-only and sanitized', async () => {
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('private failure')))
  await expect(getSession(session.id)).rejects.toMatchObject({ status: null, ambiguousWrite: false })
})

const metrics = {
  source: 'original_transcription', recognized_word_count: 1, um_count: 0, uh_count: 0,
  filler_unavailable_reason: null, timed_utterance_span_seconds: 2, estimated_words_per_minute: 30, timing_unavailable_reason: null,
}
test('transcription sends the exact attempt revision without JSON headers', async () => {
  const result = { session_id: session.id, question_index: 2, measurement_id: measurementId, text: 'Hello', language: 'eng', words: [], metrics, delivery_metrics: zeroDelivery }
  const fetchMock = mockResponse(json(result))
  expect(await transcribeAudio(session, new Blob(['audio'], { type: 'audio/webm' }), new AbortController().signal)).toEqual(result)
  const options = fetchMock.mock.calls[0][1] as RequestInit
  expect(new Headers(options.headers).get('Content-Type')).toBeNull()
  expect(new Headers(options.headers).get(AUTH_CONTEXT_HEADER)).toBe('context-A')
  const body = options.body as FormData
  expect(body.get('expected_last_attempt_number')).toBe('3')
  expect(body.get('question_index')).toBe('2')
})

test('audio acceptance keeps its existing non-persisting multipart contract', async () => {
  const fetchMock = mockResponse(json({ session_id: session.id, question_index: 2, status: 'accepted' }))
  await uploadAudio(session, new Blob(['audio'], { type: 'audio/webm' }), new AbortController().signal)
  const body = fetchMock.mock.calls[0][1].body as FormData
  expect(body.get('expected_last_attempt_number')).toBeNull()
  expect([...body.keys()].sort()).toEqual(['audio', 'question_index'])
})

test('lost transcription response is uncertain while HTTP errors retain their status', async () => {
  const fetchMock = vi.fn().mockRejectedValueOnce(new Error('private transport information'))
    .mockResolvedValueOnce(json({ private: 'do not display' }, 409))
  vi.stubGlobal('fetch', fetchMock)
  const audio = new Blob(['audio'], { type: 'audio/webm' })
  await expect(transcribeAudio(session, audio, new AbortController().signal)).rejects.toMatchObject({ ambiguousWrite: true, status: null })
  await expect(transcribeAudio(session, audio, new AbortController().signal)).rejects.toMatchObject({ ambiguousWrite: false, status: 409 })
})

function transcription(delivery: unknown = zeroDelivery) {
  return { session_id: session.id, question_index: 2, measurement_id: measurementId,
    text: 'Hello', language: 'eng', words: [{ text: 'Hello', start: 0, end: 2 }], metrics, delivery_metrics: delivery }
}
function transcribe() {
  return transcribeAudio(session, new Blob(['audio'], { type: 'audio/webm' }), new AbortController().signal)
}

test('accepts the unchanged words contract and preserves unrounded delivery values', async () => {
  const result = { ...transcription({ ...zeroDelivery, pause_count: 1, total_pause_duration_seconds: 0.56789123, longest_pause_seconds: 0.56789123 }),
    text: 'Hello there', words: [{ text: 'Hello', start: 0, end: 0.2 }, { text: 'there', start: 0.76789123, end: 2 }],
    metrics: { ...metrics, recognized_word_count: 2 } }
  mockResponse(json(result))
  expect(await transcribe()).toEqual(result)
})

test.each(DELIVERY_TIMING_REASONS)('accepts recorded unavailable delivery (%s) without invented numeric values', async (reason) => {
  const delivery = { ...zeroDelivery, pause_count: null, total_pause_duration_seconds: null, longest_pause_seconds: null, unavailable_reason: reason }
  const result = transcription(delivery)
  mockResponse(json(result))
  expect(await transcribe()).toEqual(result)
})

test.each([
  undefined, null, {}, { ...zeroDelivery, version: '' }, { ...zeroDelivery, version: 'pause-metrics-v2' },
  { ...zeroDelivery, source: 'edited_answer' }, { ...zeroDelivery, extra: 'private marker' },
  { ...zeroDelivery, pause_count: null }, { ...zeroDelivery, pause_count: false },
  { ...zeroDelivery, pause_count: -1 }, { ...zeroDelivery, pause_count: 0.5 },
  { ...zeroDelivery, pause_count: Number.MAX_SAFE_INTEGER + 1 },
  { ...zeroDelivery, total_pause_duration_seconds: -0.5 }, { ...zeroDelivery, longest_pause_seconds: NaN },
  { ...zeroDelivery, total_pause_duration_seconds: Infinity },
  { ...zeroDelivery, pause_count: 1, total_pause_duration_seconds: 0.6, longest_pause_seconds: 0.6 },
  { ...zeroDelivery, pause_count: 0, total_pause_duration_seconds: 0.6, longest_pause_seconds: 0.6 },
  { ...zeroDelivery, unavailable_reason: 'provider-private-marker' },
  { ...zeroDelivery, unavailable_reason: 'missing_timings' },
])('rejects malformed live delivery without leaking values or accepting legacy form (case %#)', async (delivery) => {
  const result = transcription(delivery)
  result.delivery_metrics = delivery
  const fetchMock = mockResponse(json(result))
  const error = await transcribe().catch((cause: unknown) => cause)
  expect(error).toBeInstanceOf(ApiError)
  expect(error).toMatchObject({ ambiguousWrite: true })
  expect((error as Error).message).not.toMatch(/private|pause_count|Infinity|NaN/)
  expect(fetchMock).toHaveBeenCalledTimes(1)
})

function deliveryComparison(): DeliveryComparison {
  const change = (before: number, after: number): DeliveryMetricChange => ({ before, after, delta: after - before,
    before_unavailable_reason: null, after_unavailable_reason: null, comparable: true, comparison_unavailable_reason: null })
  return { before_version: 'pause-metrics-v1', after_version: 'pause-metrics-v1',
    before_source: 'original_transcription', after_source: 'original_transcription',
    pause_count: change(1, 2), total_pause_duration_seconds: change(0.57891234, 1.57891234), longest_pause_seconds: change(0.57891234, 1) }
}
function pairedComparison(delivery: unknown = deliveryComparison()) {
  const metric: MetricChange = { before: 4, after: 6, delta: 2, before_unavailable_reason: null,
    after_unavailable_reason: null, comparable: true, comparison_unavailable_reason: null }
  const identity = { id: 'before', attempt_number: 1, measurement_id: measurementId,
    measurement_version: 'speaking-v1', measurement_source: 'original_transcription' }
  return { session_id: session.id, question_index: 2, before_attempt: identity,
    after_attempt: { ...identity, id: 'after', attempt_number: 2 }, comparison: {
      recognized_word_count: metric, um_count: metric, uh_count: metric,
      timed_utterance_span_seconds: metric, estimated_words_per_minute: metric,
    }, delivery_comparison: delivery }
}

test('validates delivery comparison independently and retains unrounded deltas', async () => {
  const result = pairedComparison()
  mockResponse(json(result))
  expect(await getComparison(session)).toEqual(result)
})

test.each(['measurement_version_mismatch', 'measurement_source_incompatible'] as const)(
  'accepts %s delivery without disabling compatible speaking values', async (reason) => {
    const delivery = deliveryComparison()
    if (reason === 'measurement_version_mismatch') delivery.after_version = 'pause-metrics-v2'
    else delivery.after_source = 'another_transcription_source'
    for (const key of ['pause_count', 'total_pause_duration_seconds', 'longest_pause_seconds'] as const) {
      delivery[key] = { ...delivery[key], delta: null, comparable: false, comparison_unavailable_reason: reason }
    }
    const result = pairedComparison(delivery)
    mockResponse(json(result))
    expect(await getComparison(session)).toEqual(result)
    expect(result.comparison.recognized_word_count.comparable).toBe(true)
  },
)

test('compatible delivery remains independent of a speaking version mismatch', async () => {
  const result = pairedComparison()
  result.after_attempt.measurement_version = 'speaking-v2'
  for (const key of Object.keys(result.comparison) as (keyof typeof result.comparison)[]) {
    result.comparison[key] = { ...result.comparison[key], delta: null, comparable: false,
      comparison_unavailable_reason: 'measurement_version_mismatch' }
  }
  mockResponse(json(result))
  expect(await getComparison(session)).toEqual(result)
})

test.each(['not_recorded', 'no_measurement', ...DELIVERY_TIMING_REASONS] as const)(
  'accepts a factual unavailable delivery side (%s) separately from numeric speaking comparison', async (reason) => {
    const delivery = deliveryComparison()
    if (reason === 'not_recorded' || reason === 'no_measurement') { delivery.before_version = null; delivery.before_source = null }
    for (const key of ['pause_count', 'total_pause_duration_seconds', 'longest_pause_seconds'] as const) {
      delivery[key] = { ...delivery[key], before: null, before_unavailable_reason: reason, delta: null,
        comparable: false, comparison_unavailable_reason: 'before_unavailable' }
    }
    const result = pairedComparison(delivery)
    mockResponse(json(result))
    expect(await getComparison(session)).toEqual(result)
  },
)

test.each([
  undefined, null, {}, { ...deliveryComparison(), extra: [] }, { ...deliveryComparison(), before_version: ' ' },
  { ...deliveryComparison(), before_source: null }, { ...deliveryComparison(), after_version: 'pause-metrics-v2' },
  { ...deliveryComparison(), pause_count: { ...deliveryComparison().pause_count, before: 0.5 } },
  { ...deliveryComparison(), pause_count: { ...deliveryComparison().pause_count, extra: 'private-value' } },
  { ...deliveryComparison(), longest_pause_seconds: { ...deliveryComparison().longest_pause_seconds, before: 2 } },
  { ...deliveryComparison(), pause_count: { ...deliveryComparison().pause_count, before_unavailable_reason: 'private-value' } },
  { ...deliveryComparison(), pause_count: { ...deliveryComparison().pause_count, comparable: false, delta: null, comparison_unavailable_reason: 'both_unavailable' } },
])('rejects malformed delivery comparisons while preserving sanitized read errors (case %#)', async (delivery) => {
  const result = pairedComparison(delivery)
  result.delivery_comparison = delivery
  mockResponse(json(result))
  const error = await getComparison(session).catch((cause: unknown) => cause)
  expect(error).toMatchObject({ name: 'ApiError', ambiguousWrite: false })
  expect((error as Error).message).not.toContain('private-value')
})

const diagnosis: SemanticDiagnosis = {
  diagnosis_version: 'semantic-diagnosis-v1',
  addressed_question: 'partially', addressed_question_reason: 'The answer covers the actions but not the result.',
  strengths: ['The actions are concrete.'], missing_information: ['Explain the result.'],
  structure: 'mixed', structure_feedback: 'State the result after the actions.', next_focus: 'completeness',
  next_focus_reason: 'The result completes the account.', retry_instruction: 'Keep the actions and add the result.',
}
const privateDiagnosisMarker = 'PRIVATE_DIAGNOSIS_DETAIL_API_KEY_TRACEBACK'
const malformedDiagnosisMessage = 'Unable to generate feedback right now. You can still retry or continue.'
function diagnose(signal = new AbortController().signal) {
  return getSemanticDiagnosis(session.id, 2, 4, signal)
}

test('requests exactly one bodyless semantic diagnosis POST for the specified persisted attempt', async () => {
  const fetchMock = mockResponse(json(diagnosis))
  const result = await diagnose()
  expect(result).toEqual(diagnosis)
  expect(result.diagnosis_version).toBe('semantic-diagnosis-v1')
  expect(Object.keys(result).sort()).toEqual(Object.keys(diagnosis).sort())
  expect(fetchMock).toHaveBeenCalledTimes(1)
  expect(fetchMock.mock.calls[0][0]).toBe('/api/sessions/session-1/questions/2/attempts/4/diagnosis')
  const options = fetchMock.mock.calls[0][1] as RequestInit
  expect(options.method).toBe('POST')
  expect(Object.keys(options).sort()).toEqual(['cache', 'credentials', 'headers', 'method', 'signal'])
  expect(options.body).toBeUndefined()
  expect(new Headers(options.headers).get('Content-Type')).toBeNull()
  expect(new Headers(options.headers).get(AUTH_CONTEXT_HEADER)).toBe('context-A')
  expect(JSON.stringify(options)).not.toContain(session.current_question)
  expect(JSON.stringify(options)).not.toContain(attempt.answer)
})

test('accepts the shared backend SemanticDiagnosis contract fixture unchanged', async () => {
  const fetchMock = mockResponse(new Response(semanticDiagnosisContract))
  const result = await diagnose()
  expect(result).toEqual(JSON.parse(semanticDiagnosisContract))
  expect(result.diagnosis_version).toBe('semantic-diagnosis-v1')
  expect(fetchMock).toHaveBeenCalledTimes(1)
})

test.each(['semantic-diagnosis-v2', 'SEMANTIC-DIAGNOSIS-V1', 'semantic-diagnosis-v1 ', '', null, false, 1, {}, []])(
  'rejects an incorrect diagnosis version without coercion or replay (case %#)', async (version) => {
    const fetchMock = mockResponse(json({ ...diagnosis, diagnosis_version: version }))
    await expect(diagnose()).rejects.toMatchObject({
      name: 'SemanticDiagnosisError', status: 200, message: malformedDiagnosisMessage,
    })
    expect(fetchMock).toHaveBeenCalledTimes(1)
  },
)

test('accepts empty semantic lists without inventing strengths or missing information', async () => {
  const result = { ...diagnosis, strengths: [], missing_information: [] }
  mockResponse(json(result))
  expect(await diagnose()).toEqual(result)
})

test.each(['yes', 'partially', 'no'] as const)('accepts the addressed-question literal %s unchanged', async (value) => {
  const result = { ...diagnosis, addressed_question: value }
  mockResponse(json(result))
  expect(await diagnose()).toEqual(result)
})

test.each(['clear', 'mixed', 'unclear', 'insufficient_content'] as const)('accepts the structure literal %s unchanged', async (value) => {
  const result = { ...diagnosis, structure: value }
  mockResponse(json(result))
  expect(await diagnose()).toEqual(result)
})

test.each(['answer_the_question', 'specificity', 'supporting_detail', 'structure', 'completeness', 'conciseness', 'maintain_strengths'] as const)(
  'accepts the next-focus literal %s unchanged', async (value) => {
    const result = { ...diagnosis, next_focus: value }
    mockResponse(json(result))
    expect(await diagnose()).toEqual(result)
  },
)

test.each(Object.keys(diagnosis))('rejects a missing semantic diagnosis field %s without exposing payload content', async (field) => {
  const result: Record<string, unknown> = { ...diagnosis, addressed_question_reason: privateDiagnosisMarker }
  delete result[field]
  const fetchMock = mockResponse(json(result))
  const error = await diagnose().catch((cause: unknown) => cause)
  expect(error).toBeInstanceOf(SemanticDiagnosisError)
  expect(error).toMatchObject({ status: 200, message: malformedDiagnosisMessage })
  expect((error as Error).message).not.toContain(privateDiagnosisMarker)
  expect(error).not.toHaveProperty('ambiguousWrite')
  expect(fetchMock).toHaveBeenCalledTimes(1)
})

test.each([
  null, [], 'semantic text', {},
  { ...diagnosis, provider: privateDiagnosisMarker },
  { ...diagnosis, addressed_question: true }, { ...diagnosis, addressed_question: 'sometimes' },
  { ...diagnosis, structure: 'excellent' }, { ...diagnosis, structure: 1 },
  { ...diagnosis, next_focus: 'confidence' }, { ...diagnosis, next_focus: null },
  { ...diagnosis, strengths: 'Strong answer' }, { ...diagnosis, strengths: [1] },
  { ...diagnosis, strengths: [null] }, { ...diagnosis, strengths: [' '] },
  { ...diagnosis, missing_information: null }, { ...diagnosis, missing_information: [false] },
  { ...diagnosis, missing_information: [{}] }, { ...diagnosis, missing_information: [''] },
  { ...diagnosis, addressed_question_reason: 1 }, { ...diagnosis, addressed_question_reason: '' },
  { ...diagnosis, structure_feedback: null }, { ...diagnosis, structure_feedback: ' ' },
  { ...diagnosis, next_focus_reason: [] }, { ...diagnosis, next_focus_reason: '' },
  { ...diagnosis, retry_instruction: {} }, { ...diagnosis, retry_instruction: ' ' },
])('rejects malformed semantic diagnosis types/enums without coercion (case %#)', async (result) => {
  const fetchMock = mockResponse(json(result))
  const error = await diagnose().catch((cause: unknown) => cause)
  expect(error).toBeInstanceOf(SemanticDiagnosisError)
  expect(error).toMatchObject({ status: 200, message: malformedDiagnosisMessage })
  expect((error as Error).message).not.toContain(privateDiagnosisMarker)
  expect(error).not.toHaveProperty('ambiguousWrite')
  expect(fetchMock).toHaveBeenCalledTimes(1)
})

test('a malformed JSON diagnosis response gets a fixed error without raw text or replay', async () => {
  const fetchMock = mockResponse(new Response(privateDiagnosisMarker))
  const error = await diagnose().catch((cause: unknown) => cause)
  expect(error).toMatchObject({ name: 'SemanticDiagnosisError', status: 200, message: malformedDiagnosisMessage })
  expect((error as Error).message).not.toContain(privateDiagnosisMarker)
  expect(fetchMock).toHaveBeenCalledTimes(1)
})

test.each([
  [404, 'Feedback is no longer available for this attempt.'],
  [502, malformedDiagnosisMessage],
  [503, 'Feedback is unavailable right now. You can still retry or continue.'],
  [504, 'Feedback took too long. You can still retry or continue.'],
  [422, malformedDiagnosisMessage], [500, malformedDiagnosisMessage],
] as const)('semantic HTTP %s uses fixed feedback text without consuming or exposing the original error body', async (status, message) => {
  const before = getAuthState()
  const response = json({ detail: privateDiagnosisMarker }, status)
  const jsonSpy = vi.spyOn(response, 'json')
  const fetchMock = mockResponse(response)
  const error = await diagnose().catch((cause: unknown) => cause)
  expect(error).toBeInstanceOf(SemanticDiagnosisError)
  expect(error).toMatchObject({ status, message })
  expect((error as Error).message).not.toContain(privateDiagnosisMarker)
  expect(error).not.toHaveProperty('ambiguousWrite')
  expect(jsonSpy).not.toHaveBeenCalled()
  expect(getAuthState()).toEqual(before)
  expect(fetchMock).toHaveBeenCalledTimes(1)
})

test('unconfigured diagnosis 503 uses feedback-unavailable text without an authentication warning', async () => {
  const before = getAuthState()
  const fetchMock = mockResponse(json({ detail: 'Semantic diagnosis is not configured.' }, 503))
  await expect(diagnose()).rejects.toMatchObject({ name: 'SemanticDiagnosisError', status: 503,
    message: 'Feedback is unavailable right now. You can still retry or continue.' })
  expect(getAuthState()).toEqual(before)
  expect(fetchMock).toHaveBeenCalledTimes(1)
})

test('authentication 503 on a diagnosis request retains the genuine auth warning', async () => {
  const before = getAuthState()
  const fetchMock = mockResponse(json({ detail: 'Authentication is temporarily unavailable.' }, 503))
  await expect(diagnose()).rejects.toMatchObject({ name: 'SemanticDiagnosisError', status: 503,
    message: 'Feedback is unavailable right now. You can still retry or continue.' })
  expect(getAuthState()).toEqual({ ...before, notice: AUTH_UNAVAILABLE_MESSAGE })
  expect(fetchMock).toHaveBeenCalledTimes(1)
})

test.each([[401, 'signed_out'], [403, 'stale']] as const)(
  'diagnosis %s preserves the %s auth transition without reading the body or replaying', async (status, expected) => {
    const response = json({ detail: privateDiagnosisMarker }, status)
    const clone = vi.spyOn(response, 'clone')
    const body = vi.spyOn(response, 'json')
    const fetchMock = mockResponse(response)
    await expect(diagnose()).rejects.toMatchObject({ name: 'AuthBoundaryError', status })
    expect(getAuthState()).toEqual({ status: expected })
    expect(clone).not.toHaveBeenCalled()
    expect(body).not.toHaveBeenCalled()
    expect(fetchMock).toHaveBeenCalledTimes(1)
  },
)

test('semantic network failure discards private exception text without retrying or marking an uncertain write', async () => {
  const fetchMock = vi.fn().mockRejectedValue(new Error(privateDiagnosisMarker))
  vi.stubGlobal('fetch', fetchMock)
  const error = await diagnose().catch((cause: unknown) => cause)
  expect(error).toMatchObject({ name: 'SemanticDiagnosisError', status: null,
    message: 'Unable to load feedback right now. You can still retry or continue.' })
  expect((error as Error).message).not.toContain(privateDiagnosisMarker)
  expect(error).not.toHaveProperty('ambiguousWrite')
  expect(fetchMock).toHaveBeenCalledTimes(1)
})

test('semantic diagnosis honors a pre-aborted caller without any fetch or generic error', async () => {
  const controller = new AbortController()
  controller.abort(new Error(privateDiagnosisMarker))
  const fetchMock = vi.fn()
  vi.stubGlobal('fetch', fetchMock)
  const error = await diagnose(controller.signal).catch((cause: unknown) => cause)
  expect(error).toMatchObject({ name: 'AbortError', message: 'Feedback request cancelled.' })
  expect(error).not.toBeInstanceOf(SemanticDiagnosisError)
  expect((error as Error).message).not.toContain(privateDiagnosisMarker)
  expect(fetchMock).not.toHaveBeenCalled()
})

test('caller cancellation reaches the semantic fetch signal and remains an AbortError', async () => {
  const controller = new AbortController()
  let fetchSignal: AbortSignal | undefined
  const fetchMock = vi.fn((_url: string, options: RequestInit) => new Promise<Response>((_resolve, reject) => {
    fetchSignal = options.signal as AbortSignal
    fetchSignal.addEventListener('abort', () => reject(fetchSignal?.reason), { once: true })
  }))
  vi.stubGlobal('fetch', fetchMock)
  const pending = diagnose(controller.signal).catch((cause: unknown) => cause)
  expect(fetchSignal?.aborted).toBe(false)
  controller.abort(new Error(privateDiagnosisMarker))
  const error = await pending
  expect(fetchSignal?.aborted).toBe(true)
  expect(error).toMatchObject({ name: 'AbortError', message: 'Feedback request cancelled.' })
  expect(error).not.toBeInstanceOf(SemanticDiagnosisError)
  expect((error as Error).message).not.toContain(privateDiagnosisMarker)
  expect(fetchMock).toHaveBeenCalledTimes(1)
})

test('cancellation rejects a late semantic response even when a fetch mock ignores abort', async () => {
  const controller = new AbortController()
  let resolve!: (value: Response) => void
  const fetchMock = vi.fn(() => new Promise<Response>((done) => { resolve = done }))
  vi.stubGlobal('fetch', fetchMock)
  const pending = diagnose(controller.signal).catch((cause: unknown) => cause)
  controller.abort(new Error(privateDiagnosisMarker))
  resolve(json(diagnosis))
  expect(await pending).toMatchObject({ name: 'AbortError', message: 'Feedback request cancelled.' })
  expect(fetchMock).toHaveBeenCalledTimes(1)
})

test('cancellation while reading semantic JSON remains an AbortError rather than malformed feedback', async () => {
  const controller = new AbortController()
  let reject!: (cause: unknown) => void
  const response = json(diagnosis)
  const jsonSpy = vi.spyOn(response, 'json').mockImplementation(() => new Promise((_resolve, fail) => { reject = fail }))
  mockResponse(response)
  const pending = diagnose(controller.signal).catch((cause: unknown) => cause)
  await vi.waitFor(() => expect(jsonSpy).toHaveBeenCalledTimes(1))
  controller.abort(new Error(privateDiagnosisMarker))
  reject(new Error(privateDiagnosisMarker))
  expect(await pending).toMatchObject({ name: 'AbortError', message: 'Feedback request cancelled.' })
})

test('semantic timeout combines with caller cancellation and reports a fixed timeout without replay', async () => {
  const timeout = new AbortController()
  const timeoutSpy = vi.spyOn(AbortSignal, 'timeout').mockReturnValue(timeout.signal)
  const controller = new AbortController()
  let fetchSignal: AbortSignal | undefined
  const fetchMock = vi.fn((_url: string, options: RequestInit) => new Promise<Response>((_resolve, reject) => {
    fetchSignal = options.signal as AbortSignal
    fetchSignal.addEventListener('abort', () => reject(fetchSignal?.reason), { once: true })
  }))
  vi.stubGlobal('fetch', fetchMock)
  const pending = diagnose(controller.signal).catch((cause: unknown) => cause)
  timeout.abort(new DOMException(privateDiagnosisMarker, 'TimeoutError'))
  const error = await pending
  expect(timeoutSpy).toHaveBeenCalledWith(135000)
  expect(fetchSignal?.aborted).toBe(true)
  expect(controller.signal.aborted).toBe(false)
  expect(error).toMatchObject({ name: 'SemanticDiagnosisError', status: null,
    message: 'Feedback took too long. You can still retry or continue.' })
  expect((error as Error).message).not.toContain(privateDiagnosisMarker)
  expect(fetchMock).toHaveBeenCalledTimes(1)
})

test('semantic diagnosis stays pending through 75 seconds and times out at exactly 135 seconds without replay', async () => {
  vi.useFakeTimers()
  const timeoutSpy = vi.spyOn(AbortSignal, 'timeout').mockImplementation((milliseconds) => {
    const timeout = new AbortController()
    setTimeout(() => timeout.abort(new DOMException(privateDiagnosisMarker, 'TimeoutError')), milliseconds)
    return timeout.signal
  })
  let fetchSignal: AbortSignal | undefined
  const fetchMock = vi.fn((_url: string, options: RequestInit) => new Promise<Response>((_resolve, reject) => {
    fetchSignal = options.signal as AbortSignal
    fetchSignal.addEventListener('abort', () => reject(fetchSignal?.reason), { once: true })
  }))
  vi.stubGlobal('fetch', fetchMock)
  let settled = false
  const pending = diagnose().catch((cause: unknown) => { settled = true; return cause })

  await vi.advanceTimersByTimeAsync(75000)
  expect(settled).toBe(false)
  expect(fetchSignal?.aborted).toBe(false)
  await vi.advanceTimersByTimeAsync(59999)
  expect(settled).toBe(false)
  expect(fetchSignal?.aborted).toBe(false)
  await vi.advanceTimersByTimeAsync(1)

  expect(await pending).toMatchObject({ name: 'SemanticDiagnosisError', status: null,
    message: 'Feedback took too long. You can still retry or continue.' })
  expect(timeoutSpy).toHaveBeenCalledExactlyOnceWith(135000)
  expect(fetchSignal?.aborted).toBe(true)
  expect(fetchMock).toHaveBeenCalledTimes(1)
})

test.each(['caller first', 'timeout first'] as const)(
  'caller cancellation wins when both semantic signals abort before fetch settles (%s)', async (order) => {
    const timeout = new AbortController()
    vi.spyOn(AbortSignal, 'timeout').mockReturnValue(timeout.signal)
    const controller = new AbortController()
    let fetchSignal: AbortSignal | undefined
    const fetchMock = vi.fn((_url: string, options: RequestInit) => new Promise<Response>((_resolve, reject) => {
      fetchSignal = options.signal as AbortSignal
      fetchSignal.addEventListener('abort', () => reject(fetchSignal?.reason), { once: true })
    }))
    vi.stubGlobal('fetch', fetchMock)
    const pending = diagnose(controller.signal).catch((cause: unknown) => cause)
    const abortCaller = () => controller.abort(new Error(privateDiagnosisMarker))
    const abortTimeout = () => timeout.abort(new DOMException(privateDiagnosisMarker, 'TimeoutError'))
    if (order === 'caller first') { abortCaller(); abortTimeout() }
    else { abortTimeout(); abortCaller() }

    const error = await pending
    expect(error).toMatchObject({ name: 'AbortError', message: 'Feedback request cancelled.' })
    expect(error).not.toBeInstanceOf(SemanticDiagnosisError)
    expect((error as Error).message).not.toContain(privateDiagnosisMarker)
    expect(fetchSignal?.aborted).toBe(true)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  },
)
