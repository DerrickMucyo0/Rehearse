import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { getHistoryDetail, getHistorySummaries, HistoryApiError } from './historyApi'
import type { HistoryDetail, HistoryMeasurement, HistorySummary } from './historyApi'
import type { DeliveryMetrics } from './deliveryMetrics'
import { authenticateTestWorkspace } from './authTestUtils'
import { bootstrapAuth, getAuthState } from './auth'

const id = 'aabbccdd-0011-2233-4455-66778899aabb'
const other = 'aabbccdd-0011-2233-4455-66778899aabc'
const attemptId = '00000000-0000-0000-0000-000000000001'
const created = '2026-10-01T10:00:00.000001Z'
const submitted = '2026-10-01T10:00:01.000002Z'
function summary(): HistorySummary {
  return { session_id: id, scenario_type: 'job_interview', question_engine: 'deterministic-v1', status: 'active', created_at: created, completed_at: null, current_question_number: 1,
    total_questions: 5, finalized_question_count: 0, questions_practiced_count: 0, total_attempt_count: 0,
    total_retry_count: 0, measured_final_answer_count: 0, last_submitted_at: null, last_saved_activity_at: created, finalized_points: [] }
}
function measured(): HistoryMeasurement {
  return { measurement_version: 'speaking-metrics-v1', measurement_source: 'original_transcription', recognized_word_count: 4,
    um_count: 0, uh_count: 0, filler_unavailable_reason: null, timed_utterance_span_seconds: 1.234567890123,
    estimated_words_per_minute: 194.40000174967392, timing_unavailable_reason: null, delivery_metrics: null }
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
beforeEach(async () => { await authenticateTestWorkspace() })
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

test('GET discovery is a same-origin authenticated no-store read without browser IDs or body', async () => {
  const value = { items: [summary()], next_cursor: null }
  const calls = mock(value)
  expect(await getHistorySummaries()).toEqual(value)
  expect(calls.mock.calls[0][0]).toBe('/api/history/summaries')
  expect(calls.mock.calls[0][1]).toMatchObject({ method: 'GET', cache: 'no-store' })
  const headers = new Headers(calls.mock.calls[0][1].headers)
  expect(headers.get('X-Rehearse-Auth-Context')).toBe('context-A')
  expect(headers.has('Authorization')).toBe(false)
  expect(headers.has('Cookie')).toBe(false)
  expect(calls.mock.calls[0][1].body).toBeUndefined()
})
test('empty server page is valid and exact', async () => {
  mock({ items: [], next_cursor: null })
  expect(await getHistorySummaries()).toEqual({ items: [], next_cursor: null })
})
test.each(['job_interview', 'public_speaking', 'thesis_defense', 'salary_negotiation'] as const)(
  'discovery preserves canonical saved scenario %s', async (scenarioType) => {
    const item = { ...summary(), scenario_type: scenarioType }
    mock({ items: [item], next_cursor: null })
    expect((await getHistorySummaries()).items[0].scenario_type).toBe(scenarioType)
  },
)
test.each([undefined, null, 1, '', 'Job Interview', 'JOB_INTERVIEW', ' job_interview', 'unknown'])(
  'discovery rejects missing or noncanonical saved scenario (case %#)', async (scenarioType) => {
    mock({ items: [{ ...summary(), scenario_type: scenarioType }], next_cursor: null })
    await expect(getHistorySummaries()).rejects.toMatchObject({ name: 'HistoryApiError', status: 200 })
  },
)
test('detail rejects a noncanonical saved scenario before displaying history', async () => {
  const value = detail()
  Object.assign(value.summary, { scenario_type: 'unknown' })
  mock(value)
  await expect(getHistoryDetail(id)).rejects.toMatchObject({ name: 'HistoryApiError', status: 200 })
})
test('passes an opaque cursor only as a continuation and bounded limit', async () => {
  const calls = mock({ items: [summary()], next_cursor: 'next-page' })
  await getHistorySummaries({ cursor: 'opaque+/=', limit: 20 })
  expect(calls.mock.calls[0][0]).toBe('/api/history/summaries?limit=20&cursor=opaque%2B%2F%3D')
})
test.each([{ limit: 0 }, { limit: 21 }, { limit: 1.5 }, { cursor: '' }, { cursor: 'a'.repeat(513) }])(
  'invalid discovery option never reaches fetch (case %#)', async (options) => {
    const calls = mock({})
    await expect(getHistorySummaries(options)).rejects.toMatchObject({ status: 422, cancelled: false })
    expect(calls).not.toHaveBeenCalled()
  },
)
test.each([
  { summaries: [summary()], missing_session_ids: [] },
  { items: [summary(), summary()], next_cursor: null },
  { items: [summary()], next_cursor: null, private: 'metadata' },
  { items: [{ ...summary(), total_retry_count: 1 }], next_cursor: null },
  { items: [], next_cursor: '' }, { items: [], next_cursor: 123 },
  { items: Array.from({ length: 11 }, (_, index) => ({ ...summary(), session_id: `00000000-0000-0000-0000-${index.toString(16).padStart(12, '0')}` })), next_cursor: null },
])('rejects obsolete, duplicate, extra, or malformed page responses (case %#)', async (value) => {
  mock(value)
  await expect(getHistorySummaries()).rejects.toMatchObject({ name: 'HistoryApiError', status: 200, cancelled: false })
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
  const error = await getHistorySummaries().catch((cause: unknown) => cause)
  expect(error).toBeInstanceOf(HistoryApiError)
  expect(error).toMatchObject({ status, cancelled: false })
  expect((error as Error).message).not.toContain('PRIVATE')
  expect(error).not.toHaveProperty('ambiguousWrite')
})
test('network failure remains a retryable read without uncertain-write metadata', async () => {
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('private transport failure')))
  const error = await getHistorySummaries().catch((cause: unknown) => cause)
  expect(error).toMatchObject({ status: null, cancelled: false })
  expect(error).not.toHaveProperty('ambiguousWrite')
  expect((error as Error).message).not.toContain('private')
})
test('invalid JSON produces a fixed safe response error', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('PRIVATE-MALFORMED')))
  await expect(getHistorySummaries()).rejects.toMatchObject({ status: 200, message: 'Unexpected history response. Please try again.' })
})
test('aborted reads are distinct and late responses cannot be accepted', async () => {
  const controller = new AbortController()
  let resolve: (value: Response) => void = () => { throw new Error('not started') }
  const fetchMock = vi.fn().mockImplementation(() => new Promise<Response>((done) => { resolve = done }))
  vi.stubGlobal('fetch', fetchMock)
  const request = getHistorySummaries({ signal: controller.signal })
  controller.abort()
  resolve(json({ items: [summary()], next_cursor: null }))
  await expect(request).rejects.toMatchObject({ status: null, cancelled: true })
  expect(fetchMock.mock.calls[0][1].signal.aborted).toBe(true)
  await expect(getHistoryDetail(id, { signal: controller.signal })).rejects.toMatchObject({ cancelled: true })
  expect(fetchMock).toHaveBeenCalledTimes(1)
})

function delivery(changes: Partial<DeliveryMetrics> = {}): DeliveryMetrics {
  return { version: 'pause-metrics-v1', source: 'original_transcription', pause_count: 2,
    total_pause_duration_seconds: 1.100000000009, longest_pause_seconds: 0.600000000006,
    unavailable_reason: null, ...changes }
}
function summaryWithMeasurement(measurement: HistoryMeasurement): HistorySummary {
  return { ...finalDetail().summary, finalized_points: [{ ...finalDetail().summary.finalized_points[0], measurement }] }
}

test.each([
  null,
  delivery(),
  delivery({ pause_count: 0, total_pause_duration_seconds: 0, longest_pause_seconds: 0 }),
  ...(['missing_timings', 'timing_coverage_mismatch', 'invalid_timing', 'invalid_timing_order', 'unusable_span'] as const)
    .map((unavailable_reason) => delivery({ pause_count: null, total_pause_duration_seconds: null, longest_pause_seconds: null, unavailable_reason })),
])('history preserves exact legacy, available, zero and unavailable delivery state (case %#)', async (delivery_metrics) => {
  const value = { items: [summaryWithMeasurement({ ...measured(), delivery_metrics })], next_cursor: null }
  const calls = mock(value)
  const result = await getHistorySummaries()
  expect(result).toEqual(value)
  expect(result.items[0].finalized_points[0].measurement!.delivery_metrics).toEqual(delivery_metrics)
  expect(calls).toHaveBeenCalledOnce()
  expect(calls.mock.calls[0][1].cache).toBe('no-store')
})

test('selected persisted attempt retains exact delivery facts without exposing a measurement UUID', async () => {
  const value = finalDetail()
  value.selected_question!.attempts[0].measurement!.delivery_metrics = delivery()
  value.summary.finalized_points[0].measurement!.delivery_metrics = delivery()
  mock(value)
  const returned = await getHistoryDetail(id, { questionIndex: 0 })
  expect(returned.selected_question!.attempts[0].measurement!.delivery_metrics).toEqual(delivery())
  expect(returned.selected_question!.attempts[0].measurement).not.toHaveProperty('measurement_id')
})

test.each([
  { version: '' }, { version: ' \t\n ' }, { source: '' }, { source: 'future_source' },
  { pause_count: -1 }, { pause_count: 0.5 }, { pause_count: null },
  { total_pause_duration_seconds: null }, { longest_pause_seconds: null },
  { total_pause_duration_seconds: -1 }, { longest_pause_seconds: -1 },
  { total_pause_duration_seconds: '1.1' }, { longest_pause_seconds: '0.6' },
  { pause_count: 0 }, { total_pause_duration_seconds: 0 }, { longest_pause_seconds: 0 },
  { longest_pause_seconds: 1.2 }, { pause_count: 4 },
  { unavailable_reason: 'missing_timings' }, { unavailable_reason: 'delivery_error' },
  { measurement_id: other }, { words: [] }, { pause_events: [] }, { provider_payload: 'PRIVATE-MARKER' },
])('malformed delivery values and private extra fields reject the entire safe history response (case %#)', async (changes) => {
  const invalid = { ...delivery(), ...changes }
  mock({ items: [summaryWithMeasurement({ ...measured(), delivery_metrics: invalid as DeliveryMetrics })], next_cursor: null })
  const error = await getHistorySummaries().catch((cause: unknown) => cause)
  expect(error).toMatchObject({ status: 200, message: 'Unexpected history response. Please try again.' })
  expect((error as Error).message).not.toContain('PRIVATE')
})

test.each(['version', 'source', 'pause_count', 'total_pause_duration_seconds', 'longest_pause_seconds', 'unavailable_reason'] as const)(
  'delivery key %s is required, not interpreted as historical absence', async (field) => {
    const invalid: Record<string, unknown> = { ...delivery() }
    delete invalid[field]
    mock({ items: [summaryWithMeasurement({ ...measured(), delivery_metrics: invalid as unknown as DeliveryMetrics })], next_cursor: null })
    await expect(getHistorySummaries()).rejects.toMatchObject({ status: 200 })
  },
)

test('missing delivery_metrics key is rejected while an explicit historical null remains valid', async () => {
  const invalid: Record<string, unknown> = { ...measured() }
  delete invalid.delivery_metrics
  mock({ items: [summaryWithMeasurement(invalid as unknown as HistoryMeasurement)], next_cursor: null })
  await expect(getHistorySummaries()).rejects.toMatchObject({ status: 200 })
})

test('available pause count cannot exceed the recorded lexical word prerequisite', async () => {
  const zero = delivery({ pause_count: 0, total_pause_duration_seconds: 0, longest_pause_seconds: 0 })
  mock({ items: [summaryWithMeasurement({ ...measured(), recognized_word_count: 0, delivery_metrics: zero })], next_cursor: null })
  await expect(getHistorySummaries()).rejects.toMatchObject({ status: 200 })
})

test.each([401, 403])('protected discovery %s invalidates auth without replay or body exposure', async (status) => {
  const calls = mock({ detail: 'PRIVATE' }, status)
  await expect(getHistorySummaries()).rejects.toMatchObject({ name: 'AuthBoundaryError' })
  expect(calls).toHaveBeenCalledOnce()
  expect(getAuthState().status).toBe(status === 401 ? 'signed_out' : 'stale')
})
test('an A detail body completing after B bootstrap cannot escape the authenticated boundary', async () => {
  let resolveBody!: (value: unknown) => void
  const response = { ok: true, status: 200, json: () => new Promise((done) => { resolveBody = done }) } as Response
  const calls = vi.fn().mockResolvedValue(response)
  vi.stubGlobal('fetch', calls)
  const pending = getHistoryDetail(id)
  await vi.waitFor(() => expect(resolveBody).toBeTypeOf('function'))
  calls.mockResolvedValue(json({ user_id: other, request_context: 'context-B' }))
  await bootstrapAuth()
  resolveBody(detail())
  await expect(pending).rejects.toMatchObject({ name: 'AuthBoundaryError' })
  expect(getAuthState().status).toBe('authenticated')
})


test('adaptive history preserves the generated prefix and stable planned total', async () => {
  const value = detail()
  value.summary.question_engine = 'live-ai-roleplay-v1'
  value.questions = value.questions.slice(0, 1)
  mock(value)
  expect(await getHistoryDetail(id)).toEqual(value)
  expect(value.summary.total_questions).toBe(5)
})

test('adaptive selected history retains finalized measurements and saved question text', async () => {
  const value = finalDetail()
  value.summary.question_engine = 'live-ai-roleplay-v1'
  value.questions = value.questions.slice(0, 2)
  mock(value)
  expect(await getHistoryDetail(id, { questionIndex: 0 })).toEqual(value)
})

test.each([0, 2, 5])('adaptive active history rejects a prefix of the wrong length %s', async (count) => {
  const value = detail()
  value.summary.question_engine = 'live-ai-roleplay-v1'
  value.questions = value.questions.slice(0, count)
  mock(value)
  await expect(getHistoryDetail(id)).rejects.toMatchObject({ name: 'HistoryApiError', status: 200 })
})

test('deterministic history still requires all five preselected question overviews', async () => {
  const value = detail()
  value.questions = value.questions.slice(0, 1)
  mock(value)
  await expect(getHistoryDetail(id)).rejects.toMatchObject({ name: 'HistoryApiError', status: 200 })
})

test.each([undefined, null, 'unknown', 'live-ai-roleplay-v2'])('history rejects missing or unknown engines (case %#)', async (engine) => {
  mock({ items: [{ ...summary(), question_engine: engine }], next_cursor: null })
  await expect(getHistorySummaries()).rejects.toMatchObject({ name: 'HistoryApiError', status: 200 })
})

test('mixed adaptive and deterministic discovery preserves canonical engines without detail generation', async () => {
  const items = [summary(), { ...summary(), session_id: other, question_engine: 'live-ai-roleplay-v1' }]
  const calls = mock({ items, next_cursor: null })
  expect((await getHistorySummaries()).items).toEqual(items)
  expect(calls).toHaveBeenCalledOnce()
})
