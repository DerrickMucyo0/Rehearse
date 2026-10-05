import { afterEach, expect, test, vi } from 'vitest'
import { ApiError, continueQuestion, getAttempts, getComparison, getSession, isConflictError, startInterview, submitAttempt, transcribeAudio, uploadAudio } from './interviewApi'
import type { Attempt, InterviewSession, MetricChange } from './interviewApi'

const session: InterviewSession = {
  id: 'session-1', status: 'active', current_question_index: 2, current_question: 'Third',
  current_question_latest_attempt_number: 3, questions: ['First', 'Second', 'Third', 'Fourth'], answers: ['One', 'Two'],
}
const attempt: Attempt = {
  id: 'attempt-4', question_index: 2, attempt_number: 4, answer: 'New attempt',
  submitted_at: '2026-10-04T12:00:00Z', measurement_id: null,
}
const measurementId = 'aed74a31-ddc3-4e0a-b2aa-b98ad52f7b61'
function json(value: unknown, status = 200) { return new Response(JSON.stringify(value), { status }) }
function mockResponse(response: Response) {
  const fetchMock = vi.fn().mockResolvedValue(response)
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

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
  const result = { session_id: session.id, question_index: 2, before_attempt: null, after_attempt: null, comparison: null }
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
    } }
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
  const result = { session_id: session.id, question_index: 2, measurement_id: measurementId, text: 'Hello', language: 'eng', words: [], metrics }
  const fetchMock = mockResponse(json(result))
  expect(await transcribeAudio(session, new Blob(['audio'], { type: 'audio/webm' }), new AbortController().signal)).toEqual(result)
  const options = fetchMock.mock.calls[0][1] as RequestInit
  expect(options.headers).toBeUndefined()
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
