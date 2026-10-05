import { afterEach, expect, test, vi } from 'vitest'
import { getHistoryDetail, getHistorySummaries, HistoryApiError } from './historyApi'
import type { HistoryDetail, HistoryMeasurement, HistorySummary } from './historyApi'

const id = 'aabbccdd-0011-2233-4455-66778899aabb'
const other = 'aabbccdd-0011-2233-4455-66778899aabc'
const attemptId = '00000000-0000-0000-0000-000000000001'
const created = '2026-10-01T10:00:00.000001Z'
const submitted = '2026-10-01T10:00:01.000002Z'
function summary(): HistorySummary {
  return { session_id: id, status: 'active', created_at: created, completed_at: null, current_question_number: 1,
    total_questions: 5, finalized_question_count: 0, questions_practiced_count: 0, total_attempt_count: 0,
    total_retry_count: 0, measured_final_answer_count: 0, last_submitted_at: null, last_saved_activity_at: created, finalized_points: [] }
}
function measured(): HistoryMeasurement {
  return { measurement_version: 'speaking-metrics-v1', measurement_source: 'original_transcription', recognized_word_count: 4,
    um_count: 0, uh_count: 0, filler_unavailable_reason: null, timed_utterance_span_seconds: 1.234567890123,
    estimated_words_per_minute: 194.40000174967392, timing_unavailable_reason: null }
}
function detail(): HistoryDetail {
  return { summary: summary(), questions: Array.from({ length: 5 }, (_, question_index) => ({ question_index,
    question_text: `Question ${question_index + 1}`, finalized: false, attempt_count: 0, latest_attempt_id: null,
    latest_attempt_number: null, final_attempt_id: null, final_attempt_number: null })), selected_question: null }
}
function finalDetail(): HistoryDetail {
  const result = detail()
  const measurement = measured()
  result.summary = { ...summary(), current_question_number: 2, finalized_question_count: 1, questions_practiced_count: 1,
    total_attempt_count: 1, measured_final_answer_count: 1, last_submitted_at: submitted, last_saved_activity_at: submitted,
    finalized_points: [{ question_index: 0, attempt_id: attemptId, attempt_number: 3, submitted_at: submitted, measurement }] }
  result.questions[0] = { question_index: 0, question_text: 'Question 1', finalized: true, attempt_count: 1,
    latest_attempt_id: attemptId, latest_attempt_number: 3, final_attempt_id: attemptId, final_attempt_number: 3 }
  result.selected_question = { question_index: 0, attempts: [{ attempt_id: attemptId, attempt_number: 3,
    answer_text: '<script>Plain submitted text</script>', submitted_at: submitted, is_final: true, measurement }],
  has_more: false, next_after_attempt_number: null }
  return result
}
function json(value: unknown, status = 200) { return new Response(JSON.stringify(value), { status }) }
function mock(value: unknown, status = 200) {
  const fetchMock = vi.fn().mockResolvedValue(json(value, status))
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

test('POST summaries is a scoped no-store read and canonicalizes duplicate IDs', async () => {
  const value = { summaries: [summary()], missing_session_ids: [] }
  const fetchMock = mock(value)
  expect(await getHistorySummaries([id.toUpperCase(), id])).toEqual(value)
  expect(fetchMock.mock.calls[0][0]).toBe('/api/history/summaries')
  expect(fetchMock.mock.calls[0][1]).toMatchObject({ method: 'POST', cache: 'no-store', headers: { 'Content-Type': 'application/json' } })
  expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ session_ids: [id] })
})
test('accepts an exact all-missing response and retains only requested IDs', async () => {
  mock({ summaries: [], missing_session_ids: [other, id] })
  expect(await getHistorySummaries([other, id])).toEqual({ summaries: [], missing_session_ids: [other, id] })
})
test.each([[], Array.from({ length: 51 }, () => id), ['invalid'], [id, 'invalid']].map((ids) => ({ ids })))('invalid batch never reaches fetch (case %#)', async ({ ids }) => {
  const fetchMock = mock({})
  await expect(getHistorySummaries(ids)).rejects.toMatchObject({ status: 422, cancelled: false })
  expect(fetchMock).not.toHaveBeenCalled()
})
test.each([
  { summaries: [{ ...summary(), session_id: other }], missing_session_ids: [] },
  { summaries: [summary()], missing_session_ids: [id] },
  { summaries: [], missing_session_ids: [] },
  { summaries: [summary(), summary()], missing_session_ids: [] },
  { summaries: [summary()], missing_session_ids: [], private: 'metadata' },
  { summaries: [{ ...summary(), total_retry_count: 1 }], missing_session_ids: [] },
])('rejects unrelated, duplicate, incomplete, or malformed responses (case %#)', async (value) => {
  mock(value)
  await expect(getHistorySummaries([id])).rejects.toMatchObject({ name: 'HistoryApiError', status: 200, cancelled: false })
})
test('overview uses only the scoped detail endpoint with no body or mutation request', async () => {
  const value = detail()
  const fetchMock = mock(value)
  expect(await getHistoryDetail(id.toUpperCase())).toEqual(value)
  expect(fetchMock.mock.calls[0][0]).toBe(`/api/sessions/${id}/history-detail`)
  expect(fetchMock.mock.calls[0][1].method).toBeUndefined()
  expect(fetchMock.mock.calls[0][1].cache).toBe('no-store')
})
test('selected detail preserves numbering gaps, answer text, final marker, zero, and unrounded stored values', async () => {
  const value = finalDetail()
  const fetchMock = mock(value)
  expect(await getHistoryDetail(id, { questionIndex: 0, afterAttemptNumber: 1 })).toEqual(value)
  expect(fetchMock.mock.calls[0][0]).toBe(`/api/sessions/${id}/history-detail?question_index=0&after_attempt_number=1&limit=10`)
})
test.each([
  { afterAttemptNumber: 1 }, { questionIndex: -1 }, { questionIndex: 0, afterAttemptNumber: 0 },
  { questionIndex: 0, limit: 21 }, { limit: 0 }, { questionIndex: 1.5 },
])('rejects invalid page options without fetching (case %#)', async (options) => {
  const fetchMock = mock({})
  await expect(getHistoryDetail(id, options)).rejects.toMatchObject({ status: 422 })
  expect(fetchMock).not.toHaveBeenCalled()
})
test.each(['scope', 'question', 'final-marker', 'measurement-id', 'availability', 'cursor'] as const)('rejects corrupt detail %s', async (failure) => {
  const value = finalDetail()
  if (failure === 'scope') value.summary.session_id = other
  if (failure === 'question') value.selected_question!.question_index = 1
  if (failure === 'final-marker') value.selected_question!.attempts[0].is_final = false
  if (failure === 'measurement-id') Object.assign(value.selected_question!.attempts[0].measurement!, { measurement_id: other })
  if (failure === 'availability') value.selected_question!.attempts[0].measurement!.um_count = null
  if (failure === 'cursor') value.selected_question!.next_after_attempt_number = 3
  mock(value)
  await expect(getHistoryDetail(id, { questionIndex: 0 })).rejects.toMatchObject({ status: 200 })
})
test('null unavailable metrics remain null with their persisted reasons', async () => {
  const value = finalDetail()
  const unavailable: HistoryMeasurement = { ...measured(), um_count: null, uh_count: null, filler_unavailable_reason: 'unsupported_language',
    timed_utterance_span_seconds: null, estimated_words_per_minute: null, timing_unavailable_reason: 'missing_timings' }
  value.selected_question!.attempts[0].measurement = unavailable
  value.summary.finalized_points[0].measurement = unavailable
  mock(value)
  expect((await getHistoryDetail(id, { questionIndex: 0 })).selected_question!.attempts[0].measurement).toEqual(unavailable)
})
test.each([404, 422, 500, 503])('HTTP %s errors are typed and hide arbitrary server text', async (status) => {
  mock({ detail: 'PRIVATE-SESSION-CREDENTIAL-PROVIDER-TEXT' }, status)
  const error = await getHistorySummaries([id]).catch((cause: unknown) => cause)
  expect(error).toBeInstanceOf(HistoryApiError)
  expect(error).toMatchObject({ status, cancelled: false })
  expect((error as Error).message).not.toContain('PRIVATE')
  expect(error).not.toHaveProperty('ambiguousWrite')
})
test('network failure remains a retryable read without uncertain-write metadata', async () => {
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('private transport failure')))
  const error = await getHistorySummaries([id]).catch((cause: unknown) => cause)
  expect(error).toMatchObject({ status: null, cancelled: false })
  expect(error).not.toHaveProperty('ambiguousWrite')
  expect((error as Error).message).not.toContain('private')
})
test('invalid JSON produces a fixed safe response error', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('PRIVATE-MALFORMED')))
  await expect(getHistorySummaries([id])).rejects.toMatchObject({ status: 200, message: 'Unexpected history response. Please try again.' })
})
test('aborted reads are distinct and late responses cannot be accepted', async () => {
  const controller = new AbortController()
  let resolve: (value: Response) => void = () => { throw new Error('not started') }
  const fetchMock = vi.fn().mockImplementation(() => new Promise<Response>((done) => { resolve = done }))
  vi.stubGlobal('fetch', fetchMock)
  const request = getHistorySummaries([id], { signal: controller.signal })
  controller.abort()
  resolve(json({ summaries: [summary()], missing_session_ids: [] }))
  await expect(request).rejects.toMatchObject({ status: null, cancelled: true })
  expect(fetchMock.mock.calls[0][1].signal.aborted).toBe(true)
  await expect(getHistoryDetail(id, { signal: controller.signal })).rejects.toMatchObject({ cancelled: true })
  expect(fetchMock).toHaveBeenCalledTimes(1)
})
