import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { ApiError, continueQuestion, getAttempts, getComparison, getSemanticDiagnosis, getSession, isConflictError, requestQuestionSpeech, RoleplayUnavailableError, SemanticDiagnosisError, startInterview, submitAttempt, transcribeAudio, uploadAudio, VoicePlaybackError } from './interviewApi'
import type { Attempt, DeliveryComparison, DeliveryMetricChange, InterviewSession, MetricChange, SemanticDiagnosis } from './interviewApi'
import { DELIVERY_TIMING_REASONS } from './deliveryMetrics'
import type { DeliveryMetrics } from './deliveryMetrics'
import semanticDiagnosisContract from './fixtures/semanticDiagnosis.v1.json?raw'
import { authenticateTestWorkspace } from './authTestUtils'
import { AUTH_CONTEXT_HEADER, AUTH_UNAVAILABLE_MESSAGE, getAuthState } from './auth'
import type { ScenarioType } from './scenarios'

const session: InterviewSession = {
  id: 'session-1', scenario_type: 'job_interview', interviewer_persona_id: null, question_engine: 'deterministic-v1', total_questions: 5, status: 'active', current_question_index: 2, current_question: 'Third',
  current_question_latest_attempt_number: 3, questions: ['First', 'Second', 'Third', 'Fourth', 'Fifth'], answers: ['One', 'Two'],
}
const voiceUnavailable = 'Voice playback is unavailable right now.'
function speech(signal = new AbortController().signal) {
  return requestQuestionSpeech(session.id, session.current_question_index, signal)
}
function mp3(content: BodyInit = 'MP3 audio', mediaType = 'audio/mpeg') {
  return new Response(content, { headers: { 'Content-Type': mediaType } })
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
  ['question speech', () => requestQuestionSpeech(session.id, 2, new AbortController().signal)],
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

test('speech posts only the selected persisted question identifiers and returns an owned MP3 Blob', async () => {
  const before = getAuthState()
  const timeout = vi.spyOn(AbortSignal, 'timeout')
  const response = mp3('MP3 audio', 'Audio/MPEG; charset=binary')
  const body = vi.spyOn(response, 'json')
  const fetchMock = mockResponse(response)
  const audio = await speech()
  expect(audio.size).toBe(9)
  expect(audio.type).toBe('audio/mpeg;charset=binary')
  expect(timeout).toHaveBeenCalledExactlyOnceWith(75000)
  expect(fetchMock).toHaveBeenCalledOnce()
  const [path, options] = fetchMock.mock.calls[0]
  expect(path).toBe('/api/sessions/session-1/questions/2/speech')
  expect(options).toMatchObject({ method: 'POST', credentials: 'same-origin', cache: 'no-store' })
  expect(options.body).toBeUndefined()
  const headers = new Headers(options.headers)
  expect([...headers.keys()]).toEqual(['x-rehearse-auth-context'])
  expect(headers.get(AUTH_CONTEXT_HEADER)).toBe('context-A')
  expect(body).not.toHaveBeenCalled()
  expect(getAuthState()).toEqual(before)
})

test.each([404, 409, 422, 500, 502, 503, 504])('speech HTTP %s stays voice-specific and never becomes an uncertain write', async (status) => {
  const before = getAuthState()
  const response = json({ detail: 'PRIVATE_VOICE_DETAIL' }, status)
  const jsonBody = vi.spyOn(response, 'json')
  const binaryBody = vi.spyOn(response, 'blob')
  const mock = mockResponse(response)
  const error = await speech().catch((cause: unknown) => cause)
  expect(error).toBeInstanceOf(VoicePlaybackError)
  expect(error).not.toBeInstanceOf(ApiError)
  expect(error).toMatchObject({ status, message: voiceUnavailable })
  expect(error).not.toHaveProperty('ambiguousWrite')
  expect(jsonBody).not.toHaveBeenCalled()
  expect(binaryBody).not.toHaveBeenCalled()
  expect(getAuthState()).toEqual(before)
  expect(mock).toHaveBeenCalledOnce()
})

test('speech configuration 503 retains voice-specific UX with no authentication notice', async () => {
  const before = getAuthState()
  const mock = mockResponse(json({ detail: voiceUnavailable }, 503))
  await expect(speech()).rejects.toMatchObject({ name: 'VoicePlaybackError', status: 503, message: voiceUnavailable })
  expect(getAuthState()).toEqual(before)
  expect(mock).toHaveBeenCalledOnce()
})

test('genuine authentication 503 on speech retains the shared authentication notice', async () => {
  const before = getAuthState()
  const mock = mockResponse(json({ detail: 'Authentication is temporarily unavailable.' }, 503))
  await expect(speech()).rejects.toMatchObject({ name: 'VoicePlaybackError', status: 503, message: voiceUnavailable })
  expect(getAuthState()).toEqual({ ...before, notice: AUTH_UNAVAILABLE_MESSAGE })
  expect(mock).toHaveBeenCalledOnce()
})

test.each([[401, 'signed_out'], [403, 'stale']] as const)('speech %s preserves the shared %s auth boundary', async (status, expected) => {
  const response = json({ detail: 'PRIVATE_VOICE_DETAIL' }, status)
  const body = vi.spyOn(response, 'blob')
  const mock = mockResponse(response)
  await expect(speech()).rejects.toMatchObject({ name: 'AuthBoundaryError', status })
  expect(getAuthState()).toEqual({ status: expected })
  expect(body).not.toHaveBeenCalled()
  expect(mock).toHaveBeenCalledOnce()
})

test.each([undefined, 'application/json', 'text/html', 'audio/wav', 'audio/mpeg-other'])('speech rejects non-MP3 Content-Type %s before reading bytes', async (mediaType) => {
  const response = new Response('PRIVATE_VOICE_DETAIL', { headers: mediaType ? { 'Content-Type': mediaType } : {} })
  const body = vi.spyOn(response, 'blob')
  const mock = mockResponse(response)
  await expect(speech()).rejects.toMatchObject({ name: 'VoicePlaybackError', status: 200, message: voiceUnavailable })
  expect(body).not.toHaveBeenCalled()
  expect(mock).toHaveBeenCalledOnce()
})

test('speech requires HTTP 200 even with valid audio MIME', async () => {
  const mock = mockResponse(new Response('MP3 audio', { status: 201, headers: { 'Content-Type': 'audio/mpeg' } }))
  await expect(speech()).rejects.toMatchObject({ name: 'VoicePlaybackError', status: 201, message: voiceUnavailable })
  expect(mock).toHaveBeenCalledOnce()
})

test.each([0, 2 * 1024 * 1024 + 1])('speech rejects %s actual audio bytes regardless of declared length', async (size) => {
  const response = mp3(new Uint8Array(size))
  response.headers.set('Content-Length', '9')
  const mock = mockResponse(response)
  await expect(speech()).rejects.toMatchObject({ name: 'VoicePlaybackError', status: 200, message: voiceUnavailable })
  expect(mock).toHaveBeenCalledOnce()
})

test('speech accepts the exact two MiB actual-byte boundary', async () => {
  mockResponse(mp3(new Uint8Array(2 * 1024 * 1024)))
  expect((await speech()).size).toBe(2 * 1024 * 1024)
})

test.each(['network', 'binary body'] as const)('speech %s failure discards exception text without retry or write recovery', async (phase) => {
  const mock = mockResponse(mp3())
  if (phase === 'network') mock.mockRejectedValue(new Error('PRIVATE_VOICE_EXCEPTION'))
  if (phase === 'binary body') {
    const response = mp3()
    vi.spyOn(response, 'blob').mockRejectedValue(new Error('PRIVATE_VOICE_EXCEPTION'))
    mock.mockResolvedValue(response)
  }
  const error = await speech().catch((cause: unknown) => cause)
  expect(error).toMatchObject({ name: 'VoicePlaybackError', message: voiceUnavailable })
  expect(error).not.toHaveProperty('ambiguousWrite')
  expect((error as Error).message).not.toContain('PRIVATE')
  expect(mock).toHaveBeenCalledOnce()
})

test('pre-aborted speech never enters fetch', async () => {
  const controller = new AbortController()
  controller.abort(new Error('PRIVATE_VOICE_EXCEPTION'))
  const mock = mockResponse(mp3())
  await expect(speech(controller.signal)).rejects.toMatchObject({ name: 'AbortError', message: 'Voice request cancelled.' })
  expect(mock).not.toHaveBeenCalled()
})

test('speech caller cancellation reaches fetch without retry or uncertain-write error', async () => {
  const controller = new AbortController()
  let fetchSignal: AbortSignal | undefined
  const mock = vi.fn((_url: string, options: RequestInit) => new Promise<Response>((_resolve, reject) => {
    fetchSignal = options.signal as AbortSignal
    fetchSignal.addEventListener('abort', () => reject(fetchSignal?.reason), { once: true })
  }))
  vi.stubGlobal('fetch', mock)
  const pending = speech(controller.signal).catch((cause: unknown) => cause)
  controller.abort(new Error('PRIVATE_VOICE_EXCEPTION'))
  expect(await pending).toMatchObject({ name: 'AbortError', message: 'Voice request cancelled.' })
  expect(fetchSignal?.aborted).toBe(true)
  expect(mock).toHaveBeenCalledOnce()
})

test.each(['headers', 'binary body'] as const)('speech discards late %s after caller cancellation even if the reader ignores abort', async (phase) => {
  const controller = new AbortController()
  let resolveHeaders!: (value: Response) => void
  let resolveBody!: (value: Blob) => void
  const response = mp3()
  const body = vi.spyOn(response, 'blob').mockReturnValue(new Promise((done) => { resolveBody = done }))
  const mock = vi.fn().mockReturnValue(phase === 'headers'
    ? new Promise<Response>((done) => { resolveHeaders = done }) : Promise.resolve(response))
  vi.stubGlobal('fetch', mock)
  const pending = speech(controller.signal).catch((cause: unknown) => cause)
  if (phase === 'binary body') await vi.waitFor(() => expect(body).toHaveBeenCalledOnce())
  controller.abort(new Error('PRIVATE_VOICE_EXCEPTION'))
  if (phase === 'headers') resolveHeaders(response)
  else resolveBody(new Blob(['private late audio']))
  expect(await pending).toMatchObject({ name: 'AbortError', message: 'Voice request cancelled.' })
  expect(mock).toHaveBeenCalledOnce()
})

test('speech rejects a late A binary body after account switch without affecting B', async () => {
  let resolve!: (value: Blob) => void
  const response = mp3()
  const body = vi.spyOn(response, 'blob').mockReturnValue(new Promise((done) => { resolve = done }))
  mockResponse(response)
  const pending = speech().catch((cause: unknown) => cause)
  await vi.waitFor(() => expect(body).toHaveBeenCalledOnce())
  await authenticateTestWorkspace('context-B', '144b50e1-0183-428c-943f-1850df006b66')
  resolve(new Blob(['private A audio']))
  expect(await pending).toMatchObject({ name: 'AuthBoundaryError' })
  expect(getAuthState()).toMatchObject({ status: 'authenticated', requestContext: 'context-B', notice: null })
})

test.each(['headers', 'binary body'] as const)('speech 75-second deadline rejects late %s with one request only', async (phase) => {
  const timeout = new AbortController()
  const timeoutSpy = vi.spyOn(AbortSignal, 'timeout').mockReturnValue(timeout.signal)
  let resolveHeaders!: (value: Response) => void
  let resolveBody!: (value: Blob) => void
  const response = mp3()
  const body = vi.spyOn(response, 'blob').mockReturnValue(new Promise((done) => { resolveBody = done }))
  const mock = vi.fn().mockReturnValue(phase === 'headers'
    ? new Promise<Response>((done) => { resolveHeaders = done }) : Promise.resolve(response))
  vi.stubGlobal('fetch', mock)
  const pending = speech().catch((cause: unknown) => cause)
  if (phase === 'binary body') await vi.waitFor(() => expect(body).toHaveBeenCalledOnce())
  timeout.abort(new DOMException('PRIVATE_VOICE_TIMEOUT', 'TimeoutError'))
  if (phase === 'headers') resolveHeaders(response)
  else resolveBody(new Blob(['late audio']))
  const error = await pending
  expect(error).toMatchObject({ name: 'VoicePlaybackError', status: null, message: voiceUnavailable })
  expect(error).not.toHaveProperty('ambiguousWrite')
  expect(timeoutSpy).toHaveBeenCalledExactlyOnceWith(75000)
  expect(mock).toHaveBeenCalledOnce()
})

test('starts a session with the required current-question attempt revision', async () => {
  const fresh = { ...session, interviewer_persona_id: 'recruiter' as const, current_question_latest_attempt_number: 0 }
  const fetchMock = mockResponse(json(fresh, 201))
  expect(await startInterview()).toEqual(fresh)
  expect(fetchMock.mock.calls[0][0]).toBe('/api/sessions')
  expect(fetchMock.mock.calls[0][1].method).toBe('POST')
  expect(new Headers(fetchMock.mock.calls[0][1].headers).get('Content-Type')).toBe('application/json')
  expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ scenario_type: 'job_interview', interviewer_persona_id: 'recruiter' })
})

test.each(['job_interview', 'public_speaking', 'thesis_defense', 'salary_negotiation'] as const)(
  'Start sends only the selected canonical scenario %s', async (scenarioType) => {
    const created = { ...session, scenario_type: scenarioType, interviewer_persona_id: 'recruiter' as const, question_engine: 'deterministic-v1', current_question_latest_attempt_number: 0 }
    const fetchMock = mockResponse(json(created, 201))
    expect(await startInterview(scenarioType)).toEqual(created)
    expect(fetchMock).toHaveBeenCalledOnce()
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ scenario_type: scenarioType, interviewer_persona_id: 'recruiter' })
  },
)

test.each([null, 1, '', 'Job Interview', 'JOB_INTERVIEW', ' job_interview', 'unknown'])(
  'invalid scenario input is rejected before creating a session (case %#)', async (scenarioType) => {
    const fetchMock = mockResponse(json(session, 201))
    await expect(startInterview(scenarioType as ScenarioType)).rejects.toMatchObject({ status: 422 })
    expect(fetchMock).not.toHaveBeenCalled()
  },
)

test.each(['recruiter', 'manager', 'hr'] as const)('Start sends and validates interviewer persona %s', async (personaId) => {
  const created = { ...session, interviewer_persona_id: personaId, current_question_latest_attempt_number: 0 }
  const fetchMock = mockResponse(json(created, 201))
  expect(await startInterview('job_interview', personaId)).toEqual(created)
  expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
    scenario_type: 'job_interview', interviewer_persona_id: personaId,
  })
})

test('invalid interviewer persona is rejected before session creation', async () => {
  const fetchMock = mockResponse(json(session, 201))
  await expect(startInterview('job_interview', 'unknown' as never)).rejects.toMatchObject({ status: 422 })
  expect(fetchMock).not.toHaveBeenCalled()
})

test.each([undefined, null, 1, '', 'Job Interview', 'JOB_INTERVIEW', ' job_interview', 'unknown'])(
  'session reads reject missing or noncanonical scenario responses (case %#)', async (scenarioType) => {
    mockResponse(json({ ...session, scenario_type: scenarioType }))
    await expect(getSession(session.id)).rejects.toMatchObject({ name: 'ApiError', ambiguousWrite: false })
  },
)

test('Start rejects a malformed scenario response as an uncertain creation', async () => {
  mockResponse(json({ ...session, scenario_type: 'unknown' }, 201))
  await expect(startInterview('thesis_defense')).rejects.toMatchObject({ name: 'ApiError', ambiguousWrite: true })
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


const adaptive: InterviewSession = {
  ...session, interviewer_persona_id: 'recruiter', question_engine: 'live-ai-roleplay-v1', questions: session.questions.slice(0, 3),
}
const adaptiveNext: InterviewSession = {
  ...adaptive, current_question_index: 3, current_question: 'Adaptive follow-up',
  current_question_latest_attempt_number: 0, questions: [...adaptive.questions, 'Adaptive follow-up'],
  answers: [...adaptive.answers, 'Selected'],
}
const roleplayFailure = {
  detail: 'Interviewer is unavailable right now. Try Continue again.',
  code: 'roleplay_generation_unavailable', write_outcome: 'not_applied',
}

test.each(['job_interview', 'public_speaking', 'thesis_defense', 'salary_negotiation'] as const)(
  'new adaptive %s session preserves the scenario and fixed first question with a five-question plan', async (scenarioType) => {
    const created: InterviewSession = { ...adaptive, scenario_type: scenarioType, interviewer_persona_id: 'recruiter', current_question_index: 0,
      current_question: 'Fixed first question', current_question_latest_attempt_number: 0,
      questions: ['Fixed first question'], answers: [] }
    const fetchMock = mockResponse(json(created, 201))
    expect(await startInterview(scenarioType)).toEqual(created)
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ scenario_type: scenarioType, interviewer_persona_id: 'recruiter' })
    expect(fetchMock).toHaveBeenCalledOnce()
  },
)

test.each([
  { question_engine: undefined }, { question_engine: 'future-engine' }, { total_questions: undefined }, { total_questions: 4 },
  { questions: ['First', 'Second'] }, { questions: [...adaptive.questions, 'Premature future question'] },
  { current_question: 'Wrong current question' }, { answers: [] }, { current_question_index: 5 },
])('adaptive session rejects corrupt plan or generated prefix (case %#)', async (changes) => {
  mockResponse(json({ ...adaptive, ...changes }))
  await expect(getSession(adaptive.id)).rejects.toMatchObject({ name: 'ApiError', ambiguousWrite: false })
})

test('adaptive Continue appends the validated next question and sends only the selected attempt revision', async () => {
  const timeout = vi.spyOn(AbortSignal, 'timeout')
  const fetchMock = mockResponse(json(adaptiveNext))
  expect(await continueQuestion(adaptive)).toEqual(adaptiveNext)
  expect(timeout).toHaveBeenCalledWith(135_000)
  expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ expected_last_attempt_number: 3 })
  expect(fetchMock).toHaveBeenCalledOnce()
})

test.each([
  { questions: ['Changed first', 'Second', 'Third', 'Adaptive follow-up'] },
  { answers: ['Changed final answer', 'Two', 'Selected'] },
  { question_engine: 'deterministic-v1', questions: session.questions },
  { scenario_type: 'thesis_defense' }, { current_question_latest_attempt_number: 1 },
])('adaptive malformed successful Continue remains an uncertain write (case %#)', async (changes) => {
  const mock = mockResponse(json({ ...adaptiveNext, ...changes }))
  await expect(continueQuestion(adaptive)).rejects.toMatchObject({ name: 'ApiError', ambiguousWrite: true })
  expect(mock).toHaveBeenCalledOnce()
})

test('only exact closed no-commit roleplay 503 is safe to retry directly', async () => {
  const before = getAuthState()
  const mock = mockResponse(json(roleplayFailure, 503))
  const error = await continueQuestion(adaptive).catch((cause: unknown) => cause)
  expect(error).toBeInstanceOf(RoleplayUnavailableError)
  expect(error).toMatchObject({ status: 503, ambiguousWrite: false, message: roleplayFailure.detail })
  expect(getAuthState()).toEqual(before)
  expect(mock).toHaveBeenCalledOnce()
})

test.each([
  { ...roleplayFailure, extra: 'PRIVATE' }, { ...roleplayFailure, code: 'unknown' },
  { ...roleplayFailure, write_outcome: 'unknown' }, { ...roleplayFailure, detail: 'PRIVATE' },
  { detail: roleplayFailure.detail }, null,
])('unrecognized roleplay 503 remains uncertain and hides arbitrary body text (case %#)', async (failure) => {
  const mock = mockResponse(json(failure, 503))
  const error = await continueQuestion(adaptive).catch((cause: unknown) => cause)
  expect(error).toBeInstanceOf(ApiError)
  expect(error).not.toBeInstanceOf(RoleplayUnavailableError)
  expect(error).toMatchObject({ status: 503, ambiguousWrite: true })
  expect((error as Error).message).not.toContain('PRIVATE')
  expect(mock).toHaveBeenCalledOnce()
})

test('malformed roleplay failure JSON remains uncertain', async () => {
  mockResponse(new Response('PRIVATE', { status: 503 }))
  await expect(continueQuestion(adaptive)).rejects.toMatchObject({ name: 'ApiError', ambiguousWrite: true })
})

test('legacy Continue never classifies a roleplay 503 as a safe generation retry', async () => {
  const timeout = vi.spyOn(AbortSignal, 'timeout')
  mockResponse(json(roleplayFailure, 503))
  await expect(continueQuestion(session)).rejects.toMatchObject({ name: 'ApiError', ambiguousWrite: true })
  expect(timeout).toHaveBeenCalledWith(10_000)
})

test('final adaptive Continue uses ten seconds and completes without appending a question', async () => {
  const final: InterviewSession = { ...adaptive, current_question_index: 4, current_question: 'Fifth',
    questions: session.questions, answers: ['One', 'Two', 'Three', 'Four'] }
  const completed: InterviewSession = { ...final, status: 'completed', current_question_index: 5,
    current_question: null, current_question_latest_attempt_number: 0, answers: [...final.answers, 'Five'] }
  const timeout = vi.spyOn(AbortSignal, 'timeout')
  const mock = mockResponse(json(completed))
  expect(await continueQuestion(final)).toEqual(completed)
  expect(timeout).toHaveBeenCalledWith(10_000)
  expect(mock).toHaveBeenCalledOnce()
})

test.each([[401, 'signed_out'], [403, 'stale']] as const)(
  'adaptive Continue %s preserves the auth boundary without replay', async (status, expected) => {
    const mock = mockResponse(json(roleplayFailure, status))
    await expect(continueQuestion(adaptive)).rejects.toMatchObject({ name: 'AuthBoundaryError', status })
    expect(getAuthState()).toEqual({ status: expected })
    expect(mock).toHaveBeenCalledOnce()
  },
)

test('auth 503 on adaptive Continue retains auth notice and uncertain-write recovery', async () => {
  const mock = mockResponse(json({ detail: 'Authentication is temporarily unavailable.' }, 503))
  await expect(continueQuestion(adaptive)).rejects.toMatchObject({ name: 'ApiError', ambiguousWrite: true })
  expect(getAuthState()).toMatchObject({ status: 'authenticated', notice: AUTH_UNAVAILABLE_MESSAGE })
  expect(mock).toHaveBeenCalledOnce()
})

test.each(['caller', 'deadline'] as const)('adaptive Continue %s cancellation remains uncertain and is never replayed', async (source) => {
  const caller = new AbortController()
  const deadline = new AbortController()
  const timeout = vi.spyOn(AbortSignal, 'timeout').mockReturnValue(deadline.signal)
  const mock = vi.fn((_url: string, options: RequestInit) => new Promise<Response>((_resolve, reject) => {
    options.signal?.addEventListener('abort', () => reject(options.signal?.reason), { once: true })
  }))
  vi.stubGlobal('fetch', mock)
  const pending = continueQuestion(adaptive, caller.signal).catch((cause: unknown) => cause)
  ;(source === 'caller' ? caller : deadline).abort(new DOMException('PRIVATE', 'AbortError'))
  expect(await pending).toMatchObject({ name: 'ApiError', ambiguousWrite: true })
  expect(timeout).toHaveBeenCalledWith(135_000)
  expect(mock).toHaveBeenCalledOnce()
})

test('adaptive Continue stays pending until its bounded 135-second deadline', async () => {
  vi.useFakeTimers()
  vi.spyOn(AbortSignal, 'timeout').mockImplementation((milliseconds) => {
    const controller = new AbortController()
    setTimeout(() => controller.abort(new DOMException('Timeout', 'TimeoutError')), milliseconds)
    return controller.signal
  })
  const mock = vi.fn((_url: string, options: RequestInit) => new Promise<Response>((_resolve, reject) => {
    options.signal?.addEventListener('abort', () => reject(options.signal?.reason), { once: true })
  }))
  vi.stubGlobal('fetch', mock)
  let settled = false
  const pending = continueQuestion(adaptive).catch((cause: unknown) => { settled = true; return cause })
  await vi.advanceTimersByTimeAsync(134_999)
  expect(settled).toBe(false)
  await vi.advanceTimersByTimeAsync(1)
  expect(await pending).toMatchObject({ name: 'ApiError', ambiguousWrite: true })
  expect(mock).toHaveBeenCalledOnce()
})

test.each(['success', 'roleplay failure'] as const)('late adaptive Continue %s body cannot cross auth workspaces', async (kind) => {
  let resolve!: (value: unknown) => void
  const value = kind === 'success' ? adaptiveNext : roleplayFailure
  const response = json(value, kind === 'success' ? 200 : 503)
  const body = vi.spyOn(response, 'json').mockReturnValue(new Promise((done) => { resolve = done }))
  mockResponse(response)
  const pending = continueQuestion(adaptive).catch((cause: unknown) => cause)
  await vi.waitFor(() => expect(body).toHaveBeenCalledOnce())
  await authenticateTestWorkspace('context-B', '144b50e1-0183-428c-943f-1850df006b66')
  resolve(value)
  expect(await pending).toMatchObject({ name: 'AuthBoundaryError' })
  expect(getAuthState()).toMatchObject({ requestContext: 'context-B', notice: null })
})


test.each(['success', 'roleplay failure'] as const)('adaptive Continue caller cancellation during %s JSON remains uncertain', async (kind) => {
  const caller = new AbortController()
  let resolve!: (value: unknown) => void
  const value = kind === 'success' ? adaptiveNext : roleplayFailure
  const response = json(value, kind === 'success' ? 200 : 503)
  const body = vi.spyOn(response, 'json').mockReturnValue(new Promise((done) => { resolve = done }))
  mockResponse(response)
  const pending = continueQuestion(adaptive, caller.signal).catch((cause: unknown) => cause)
  await vi.waitFor(() => expect(body).toHaveBeenCalledOnce())
  caller.abort()
  resolve(value)
  expect(await pending).toMatchObject({ name: 'ApiError', ambiguousWrite: true })
})
