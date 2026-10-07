// @vitest-environment jsdom
import { authenticateTestWorkspace } from './authTestUtils'
import { StrictMode } from 'react'
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import InterviewSummary from './InterviewSummary.tsx'
import type { HistoryDetail, HistoryMeasurement } from './historyApi'
import { describeUnavailableReason, formatProgressValue } from './progress'
import { deliveryUnavailableText, formatDeliveryDuration } from './deliveryMetrics'

const firstId = '11111111-1111-4111-8111-111111111111'
const secondId = '22222222-2222-4222-8222-222222222222'
const createdAt = '2026-10-06T10:00:00.000001Z'
const completedAt = '2026-10-06T10:00:10.000001Z'
const privateMarker = 'PRIVATE_ANSWER_PROVIDER_BACKEND_ERROR'
const attemptId = (index: number) => `00000000-0000-4000-8000-${String(index + 1).padStart(12, '0')}`
const submittedAt = (index: number) => `2026-10-06T10:00:0${index + 1}.000002Z`

function completedDetail(id = firstId, counts = [1, 1, 1, 1, 1], numbers = counts): HistoryDetail {
  const totalAttempts = counts.reduce((total, count) => total + count, 0)
  return {
    summary: { session_id: id, scenario_type: 'job_interview', question_engine: 'deterministic-v1', status: 'completed', created_at: createdAt, completed_at: completedAt,
      current_question_number: null, total_questions: 5, finalized_question_count: 5, questions_practiced_count: 5,
      total_attempt_count: totalAttempts, total_retry_count: totalAttempts - 5, measured_final_answer_count: 0,
      last_submitted_at: submittedAt(4), last_saved_activity_at: completedAt,
      finalized_points: counts.map((_, question_index) => ({ question_index, attempt_id: attemptId(question_index),
        attempt_number: numbers[question_index], submitted_at: submittedAt(question_index), measurement: null })) },
    questions: counts.map((attempt_count, question_index) => ({ question_index, question_text: `Persisted question ${question_index + 1}`,
      finalized: true, attempt_count, latest_attempt_id: attemptId(question_index), latest_attempt_number: numbers[question_index],
      final_attempt_id: attemptId(question_index), final_attempt_number: numbers[question_index] })),
    selected_question: null,
  }
}

function provisionalDetail(): HistoryDetail {
  const detail = completedDetail()
  detail.summary.status = 'active'
  detail.summary.completed_at = null
  detail.summary.current_question_number = 5
  detail.summary.finalized_question_count = 4
  detail.summary.finalized_points.pop()
  detail.summary.last_saved_activity_at = submittedAt(4)
  detail.questions[4].finalized = false
  detail.questions[4].final_attempt_id = null
  detail.questions[4].final_attempt_number = null
  return detail
}

function activeDetail(): HistoryDetail {
  const detail = completedDetail()
  detail.summary = { ...detail.summary, status: 'active', completed_at: null, current_question_number: 1,
    finalized_question_count: 0, questions_practiced_count: 0, total_attempt_count: 0, total_retry_count: 0,
    last_submitted_at: null, last_saved_activity_at: createdAt, finalized_points: [] }
  detail.questions = detail.questions.map((question) => ({ ...question, finalized: false, attempt_count: 0,
    latest_attempt_id: null, latest_attempt_number: null, final_attempt_id: null, final_attempt_number: null }))
  return detail
}

function measured(changes: Partial<HistoryMeasurement> = {}): HistoryMeasurement {
  return { measurement_version: 'speaking-metrics-v1', measurement_source: 'original_transcription', recognized_word_count: 4,
    um_count: 0, uh_count: 0, filler_unavailable_reason: null, timed_utterance_span_seconds: 1.234567890123,
    estimated_words_per_minute: 194.40000174967392, timing_unavailable_reason: null, delivery_metrics: null, ...changes }
}

function delivery(changes: Partial<NonNullable<HistoryMeasurement['delivery_metrics']>> = {}) {
  return { version: 'pause-metrics-v1', source: 'original_transcription', pause_count: 2,
    total_pause_duration_seconds: 1.234567890789, longest_pause_seconds: 0.734567890789,
    unavailable_reason: null, ...changes } satisfies NonNullable<HistoryMeasurement['delivery_metrics']>
}

function setMeasurement(detail: HistoryDetail, index: number, measurement: HistoryMeasurement) {
  detail.summary.finalized_points[index].measurement = measurement
  detail.summary.measured_final_answer_count = detail.summary.finalized_points.filter((point) => point.measurement !== null).length
}

function response(value: unknown, status = 200) { return new Response(JSON.stringify(value), { status }) }
function mockRead(value: unknown = completedDetail(), status = 200) {
  const fetchMock = vi.fn<(url: string, options?: RequestInit) => Promise<Response>>().mockResolvedValue(response(value, status))
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}
function deferredResponse() {
  let resolve!: (value: Response) => void
  let reject!: (cause: unknown) => void
  const promise = new Promise<Response>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}
function summary() { return screen.getByRole('region', { name: 'Interview summary' }) }
function question(number: number) { return screen.getByRole('article', { name: `Summary for Question ${number}` }) }
function fact(region: HTMLElement, label: string) {
  return within(region).getByText(label, { selector: 'dt', exact: true }).nextElementSibling?.textContent
}
function expectReadRequests(calls: readonly [string, RequestInit?][], ids: readonly string[]) {
  expect(calls).toHaveLength(ids.length)
  calls.forEach(([url, options], index) => {
    expect(url).toBe(`/api/sessions/${ids[index]}/history-detail`)
    expect(options?.method ?? 'GET').toBe('GET')
    expect(options?.body).toBeUndefined()
    expect(options?.cache).toBe('no-store')
    expect(options?.signal).toBeInstanceOf(AbortSignal)
  })
}

beforeEach(async () => {
  await authenticateTestWorkspace()
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Unmocked network is forbidden.'))) })
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

test.each([
  { name: 'zero retries', counts: [1, 1, 1, 1, 1], numbers: [1, 1, 1, 1, 1], attempts: 5, retries: 0 },
  { name: 'multiple retries', counts: [3, 2, 1, 4, 2], numbers: [3, 2, 1, 4, 2], attempts: 12, retries: 7 },
  { name: 'sparse numbering', counts: [2, 1, 3, 1, 2], numbers: [7, 4, 12, 1, 5], attempts: 9, retries: 4 },
])('renders completed $name counts, final attempts and questions in persisted index order', async ({ counts, numbers, attempts, retries }) => {
  const detail = completedDetail(firstId, counts, numbers)
  const fetchMock = mockRead(detail)
  render(<InterviewSummary sessionId={firstId} />)
  await screen.findByRole('article', { name: 'Summary for Question 1' })
  expect(fact(summary(), 'Questions completed')).toBe('5 / 5')
  expect(fact(summary(), 'Total attempts')).toBe(String(attempts))
  expect(fact(summary(), 'Total retries')).toBe(String(retries))
  const articles = within(summary()).getAllByRole('article')
  expect(articles).toHaveLength(5)
  articles.forEach((article, index) => {
    expect(within(article).getByRole('heading', { name: `Question ${index + 1}`, level: 4 })).toBeTruthy()
    expect(within(article).getByText(detail.questions[index].question_text, { exact: true })).toBeTruthy()
    expect(fact(article, 'Final attempt')).toBe(String(numbers[index]))
    expect(fact(article, 'Retries')).toBe(String(counts[index] - 1))
  })
  expectReadRequests(fetchMock.mock.calls, [firstId])
  for (const metadata of [firstId, 'interview-summary-v1', 'summary_version', attemptId(0), completedAt]) {
    expect(summary().textContent).not.toContain(metadata)
  }
  expect(within(summary()).queryByRole('region', { name: 'Answer feedback' })).toBeNull()
  expect(within(summary()).queryByRole('region', { name: 'Practice drill' })).toBeNull()
  expect(within(summary()).queryByRole('table')).toBeNull()
})

test('a final answer without a measurement displays neutral unavailability for speaking and delivery', async () => {
  mockRead()
  render(<InterviewSummary sessionId={firstId} />)
  await screen.findByRole('article', { name: 'Summary for Question 1' })
  const row = question(1)
  expect(within(row).getByText('Speaking measurements: Unavailable — No measurement', { exact: true })).toBeTruthy()
  expect(within(row).getByText('Timed pauses: Unavailable — No measurement', { exact: true })).toBeTruthy()
  expect(within(row).queryByRole('region', { name: 'Speaking measurements' })).toBeNull()
  expect(within(row).queryByRole('region', { name: 'Timed pauses' })).toBeNull()
})

test('measured zeros and precise final speaking/delivery facts use the existing display formatters', async () => {
  const detail = completedDetail()
  const precise = measured({ delivery_metrics: delivery() })
  setMeasurement(detail, 0, precise)
  setMeasurement(detail, 1, measured({ delivery_metrics: delivery({ pause_count: 0, total_pause_duration_seconds: 0, longest_pause_seconds: 0 }) }))
  const before = structuredClone(detail)
  mockRead(detail)
  render(<InterviewSummary sessionId={firstId} />)
  await screen.findByRole('article', { name: 'Summary for Question 1' })
  const row = question(1)
  expect(fact(row, 'Estimated WPM')).toBe(formatProgressValue(precise.estimated_words_per_minute!, 'estimated_words_per_minute'))
  expect(fact(row, 'Timed speech span')).toBe(formatProgressValue(precise.timed_utterance_span_seconds!, 'timed_utterance_span_seconds'))
  expect(fact(row, 'Recognized words')).toBe('4')
  expect(fact(row, 'Um count')).toBe('0')
  expect(fact(row, 'Uh count')).toBe('0')
  const pauses = within(row).getByRole('region', { name: 'Timed pauses' })
  expect(within(pauses).getByRole('heading', { name: 'Timed pauses', level: 6 })).toBeTruthy()
  expect(fact(pauses, 'Pause count')).toBe('2')
  expect(fact(pauses, 'Total pause time')).toBe(formatDeliveryDuration(precise.delivery_metrics!.total_pause_duration_seconds!))
  expect(fact(pauses, 'Longest pause')).toBe(formatDeliveryDuration(precise.delivery_metrics!.longest_pause_seconds!))
  const zeroPauses = within(question(2)).getByRole('region', { name: 'Timed pauses' })
  expect(fact(zeroPauses, 'Pause count')).toBe('0')
  expect(fact(zeroPauses, 'Total pause time')).toBe('0.0 s')
  expect(fact(zeroPauses, 'Longest pause')).toBe('0.0 s')
  expect(detail).toEqual(before)
})

test('unsupported-language fillers stay unavailable while recognized words and legacy delivery remain distinct', async () => {
  const detail = completedDetail()
  setMeasurement(detail, 0, measured({ um_count: null, uh_count: null, filler_unavailable_reason: 'unsupported_language' }))
  mockRead(detail)
  render(<InterviewSummary sessionId={firstId} />)
  await screen.findByRole('article', { name: 'Summary for Question 1' })
  for (const label of ['Um count', 'Uh count']) expect(fact(question(1), label)).toBe('Unavailable — Unsupported language')
  expect(fact(question(1), 'Recognized words')).toBe('4')
  expect(fact(within(question(1)).getByRole('region', { name: 'Timed pauses' }), 'Pause count')).toBe('Not recorded')
})

test('zero recognized words remain zero while missing timing values remain unavailable', async () => {
  const detail = completedDetail()
  setMeasurement(detail, 0, measured({ recognized_word_count: 0, timed_utterance_span_seconds: null,
    estimated_words_per_minute: null, timing_unavailable_reason: 'missing_timings' }))
  mockRead(detail)
  render(<InterviewSummary sessionId={firstId} />)
  await screen.findByRole('article', { name: 'Summary for Question 1' })
  expect(fact(question(1), 'Recognized words')).toBe('0')
  expect(fact(question(1), 'Estimated WPM')).toBe('Unavailable — Missing timings')
})

test.each(['missing_timings', 'timing_coverage_mismatch', 'invalid_timing', 'invalid_timing_order', 'unusable_span'] as const)(
  'unavailable timing %s preserves the existing reason instead of displaying zero', async (reason) => {
    const detail = completedDetail()
    setMeasurement(detail, 0, measured({ timed_utterance_span_seconds: null, estimated_words_per_minute: null, timing_unavailable_reason: reason }))
    mockRead(detail)
    render(<InterviewSummary sessionId={firstId} />)
    await screen.findByRole('article', { name: 'Summary for Question 1' })
    for (const label of ['Estimated WPM', 'Timed speech span']) expect(fact(question(1), label)).toBe(`Unavailable — ${describeUnavailableReason(reason)}`)
  },
)

test.each(['missing_timings', 'timing_coverage_mismatch', 'invalid_timing', 'invalid_timing_order', 'unusable_span'] as const)(
  'recorded-but-unavailable delivery %s keeps its reason and differs from unrecorded delivery', async (reason) => {
    const detail = completedDetail()
    setMeasurement(detail, 0, measured({ delivery_metrics: delivery({ pause_count: null, total_pause_duration_seconds: null,
      longest_pause_seconds: null, unavailable_reason: reason }) }))
    mockRead(detail)
    render(<InterviewSummary sessionId={firstId} />)
    await screen.findByRole('article', { name: 'Summary for Question 1' })
    const pauses = within(question(1)).getByRole('region', { name: 'Timed pauses' })
    for (const label of ['Pause count', 'Total pause time', 'Longest pause']) expect(fact(pauses, label)).toBe('Unavailable')
    expect(within(pauses).getByText(`Unavailable — ${deliveryUnavailableText(reason)}`, { exact: true })).toBeTruthy()
    expect(within(pauses).queryByText('Not recorded')).toBeNull()
  },
)

test('independent historical speaking and delivery provenance remain visible on the correct final question', async () => {
  const detail = completedDetail()
  setMeasurement(detail, 0, measured({ measurement_version: 'speaking-historical', delivery_metrics: delivery({ version: 'delivery-historical' }) }))
  mockRead(detail)
  render(<InterviewSummary sessionId={firstId} />)
  await screen.findByRole('article', { name: 'Summary for Question 1' })
  expect(within(question(1)).getByText('Measurement version: speaking-historical', { exact: true })).toBeTruthy()
  expect(within(question(1)).getByText('Measurement version: delivery-historical', { exact: true })).toBeTruthy()
  expect(within(question(1)).getAllByText('Source: Original transcription', { exact: true })).toHaveLength(2)
  expect(question(2).textContent).not.toContain('historical')
})

test('pending and successful rerenders use one read and render no storage or interview writes', async () => {
  const pending = deferredResponse()
  const fetchMock = vi.fn<(url: string, options?: RequestInit) => Promise<Response>>().mockReturnValue(pending.promise)
  vi.stubGlobal('fetch', fetchMock)
  const storageWrites = (['setItem', 'removeItem', 'clear'] as const).map((method) => vi.spyOn(Storage.prototype, method))
  const localBefore = JSON.stringify(localStorage)
  const sessionBefore = JSON.stringify(sessionStorage)
  const component = render(<InterviewSummary sessionId={firstId} />)
  expect(within(summary()).getByText('Loading interview summary…', { exact: true })).toBeTruthy()
  expect(summary().getAttribute('aria-busy')).toBe('true')
  expect(within(summary()).queryByRole('article')).toBeNull()
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
  component.rerender(<InterviewSummary sessionId={firstId} />)
  expect(fetchMock.mock.calls[0][1]?.signal?.aborted).toBe(false)
  await act(async () => pending.resolve(response(completedDetail())))
  await screen.findByRole('article', { name: 'Summary for Question 1' })
  const firstQuestion = question(1)
  component.rerender(<InterviewSummary sessionId={firstId} />)
  expect(question(1)).toBe(firstQuestion)
  expect(summary().getAttribute('aria-busy')).toBe('false')
  expectReadRequests(fetchMock.mock.calls, [firstId])
  expect(JSON.stringify(localStorage)).toBe(localBefore)
  expect(JSON.stringify(sessionStorage)).toBe(sessionBefore)
  for (const write of storageWrites) expect(write).not.toHaveBeenCalled()
})

test('StrictMode effect replay starts one owned read, preserves rerenders and aborts on unmount without writes', async () => {
  const pending = deferredResponse()
  const fetchMock = vi.fn<(url: string, options?: RequestInit) => Promise<Response>>().mockReturnValue(pending.promise)
  vi.stubGlobal('fetch', fetchMock)
  const storageWrites = (['setItem', 'removeItem', 'clear'] as const).map((method) => vi.spyOn(Storage.prototype, method))
  const localBefore = JSON.stringify(localStorage)
  const sessionBefore = JSON.stringify(sessionStorage)
  const component = render(<StrictMode><InterviewSummary sessionId={firstId} /></StrictMode>)
  expect(within(summary()).getByText('Loading interview summary…', { exact: true })).toBeTruthy()
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
  const signal = fetchMock.mock.calls[0][1]?.signal
  expect(signal?.aborted).toBe(false)
  component.rerender(<StrictMode><InterviewSummary sessionId={firstId} /></StrictMode>)
  expect(signal?.aborted).toBe(false)
  await act(async () => pending.resolve(response(completedDetail())))
  await screen.findByRole('article', { name: 'Summary for Question 1' })
  const firstQuestion = question(1)
  component.rerender(<StrictMode><InterviewSummary sessionId={firstId} /></StrictMode>)
  expect(question(1)).toBe(firstQuestion)
  expectReadRequests(fetchMock.mock.calls, [firstId])
  component.unmount()
  expect(signal?.aborted).toBe(true)
  expect(JSON.stringify(localStorage)).toBe(localBefore)
  expect(JSON.stringify(sessionStorage)).toBe(sessionBefore)
  for (const write of storageWrites) expect(write).not.toHaveBeenCalled()
})

test.each([
  { name: 'HTTP 404', result: () => Promise.resolve(response({ detail: privateMarker }, 404)) },
  { name: 'HTTP 500', result: () => Promise.resolve(response({ detail: privateMarker }, 500)) },
  { name: 'HTTP 503', result: () => Promise.resolve(response({ detail: privateMarker }, 503)) },
  { name: 'network failure', result: () => Promise.reject(new TypeError(privateMarker)) },
  { name: 'malformed JSON', result: () => Promise.resolve(new Response(privateMarker, { status: 200 })) },
  { name: 'unexpected source fields', result: () => Promise.resolve(response({ ...completedDetail(), provider: privateMarker })) },
  { name: 'active incomplete session', result: () => Promise.resolve(response(activeDetail())) },
  { name: 'provisional fifth question', result: () => Promise.resolve(response(provisionalDetail())) },
  { name: 'contradictory final attempt number', result: () => {
    const detail = completedDetail(); detail.summary.finalized_points[0].attempt_number = 7
    return Promise.resolve(response(detail))
  } },
  { name: 'wrong session identity', result: () => Promise.resolve(response(completedDetail(secondId))) },
])('$name displays only the fixed summary failure without replaying requests', async ({ result }) => {
  const fetchMock = vi.fn<(url: string, options?: RequestInit) => Promise<Response>>().mockImplementation(result)
  vi.stubGlobal('fetch', fetchMock)
  render(<InterviewSummary sessionId={firstId} />)
  await within(summary()).findByText('Interview summary is unavailable.', { exact: true })
  expect(summary().getAttribute('aria-busy')).toBe('false')
  expect(within(summary()).queryByRole('article')).toBeNull()
  expect(document.body.textContent).not.toContain(privateMarker)
  expectReadRequests(fetchMock.mock.calls, [firstId])
})

test.each([
  ['success', 'pending newer read'], ['error', 'pending newer read'],
  ['success', 'completed newer read'], ['error', 'completed newer read'],
] as const)('late old-session %s is ignored with a %s', async (settlement, stage) => {
  const oldRead = deferredResponse()
  const newRead = deferredResponse()
  const fetchMock = vi.fn<(url: string, options?: RequestInit) => Promise<Response>>()
    .mockReturnValueOnce(oldRead.promise).mockReturnValueOnce(newRead.promise)
  vi.stubGlobal('fetch', fetchMock)
  const component = render(<InterviewSummary sessionId={firstId} />)
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
  const oldSignal = fetchMock.mock.calls[0][1]?.signal
  component.rerender(<InterviewSummary sessionId={secondId} />)
  expect(oldSignal?.aborted).toBe(true)
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
  const newer = completedDetail(secondId, [2, 1, 3, 1, 2], [7, 4, 12, 1, 5])
  newer.questions[0].question_text = 'Current second-session question'
  if (stage === 'completed newer read') {
    await act(async () => newRead.resolve(response(newer)))
    await screen.findByText('Current second-session question')
  }
  await act(async () => {
    if (settlement === 'success') {
      const older = completedDetail(); older.questions[0].question_text = 'STALE first-session question'
      oldRead.resolve(response(older))
    } else oldRead.reject(new TypeError(privateMarker))
  })
  expect(document.body.textContent).not.toContain('STALE first-session question')
  expect(document.body.textContent).not.toContain(privateMarker)
  expect(within(summary()).queryByText('Interview summary is unavailable.')).toBeNull()
  if (stage === 'pending newer read') {
    expect(within(summary()).getByText('Loading interview summary…')).toBeTruthy()
    await act(async () => newRead.resolve(response(newer)))
    await screen.findByText('Current second-session question')
  }
  expect(fact(summary(), 'Total attempts')).toBe('9')
  expect(fact(question(1), 'Final attempt')).toBe('7')
  expectReadRequests(fetchMock.mock.calls, [firstId, secondId])
})

test('changing owner clears a completed old summary while the new summary is loading', async () => {
  const pending = deferredResponse()
  const older = completedDetail(); older.questions[0].question_text = 'Old completed session question'
  const fetchMock = vi.fn<(url: string, options?: RequestInit) => Promise<Response>>()
    .mockResolvedValueOnce(response(older)).mockReturnValueOnce(pending.promise)
  vi.stubGlobal('fetch', fetchMock)
  const component = render(<InterviewSummary sessionId={firstId} />)
  await screen.findByText('Old completed session question')
  component.rerender(<InterviewSummary sessionId={secondId} />)
  expect(screen.queryByText('Old completed session question')).toBeNull()
  expect(within(summary()).getByText('Loading interview summary…')).toBeTruthy()
  expect(within(summary()).queryByRole('article')).toBeNull()
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
  await act(async () => pending.resolve(response(completedDetail(secondId))))
  await screen.findByRole('article', { name: 'Summary for Question 1' })
  expectReadRequests(fetchMock.mock.calls, [firstId, secondId])
})

test.each(['success', 'error'] as const)('returning to the first owner cannot resurrect its prior %s during a new read', async (previous) => {
  const oldFirst = completedDetail(); oldFirst.questions[0].question_text = 'Old first-session summary'
  const secondRead = deferredResponse()
  const latestFirstRead = deferredResponse()
  const fetchMock = vi.fn<(url: string, options?: RequestInit) => Promise<Response>>()
    .mockImplementationOnce(() => previous === 'success'
      ? Promise.resolve(response(oldFirst)) : Promise.reject(new TypeError(privateMarker)))
    .mockReturnValueOnce(secondRead.promise).mockReturnValueOnce(latestFirstRead.promise)
  vi.stubGlobal('fetch', fetchMock)
  const component = render(<InterviewSummary sessionId={firstId} />)
  if (previous === 'success') await screen.findByText('Old first-session summary')
  else await within(summary()).findByText('Interview summary is unavailable.', { exact: true })
  const expectPendingSummary = () => {
    expect(within(summary()).getByText('Loading interview summary…', { exact: true })).toBeTruthy()
    expect(within(summary()).queryByRole('article')).toBeNull()
    expect(within(summary()).queryByRole('alert')).toBeNull()
    expect(document.body.textContent).not.toContain('Old first-session summary')
    expect(document.body.textContent).not.toContain(privateMarker)
  }
  component.rerender(<InterviewSummary sessionId={secondId} />)
  expectPendingSummary()
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
  const secondSignal = fetchMock.mock.calls[1][1]?.signal
  component.rerender(<InterviewSummary sessionId={firstId} />)
  expect(secondSignal?.aborted).toBe(true)
  expectPendingSummary()
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3))
  const latestFirstSignal = fetchMock.mock.calls[2][1]?.signal
  component.rerender(<InterviewSummary sessionId={firstId} />)
  expectPendingSummary()
  expect(latestFirstSignal?.aborted).toBe(false)
  const obsoleteSecond = completedDetail(secondId); obsoleteSecond.questions[0].question_text = 'STALE second-session summary'
  await act(async () => secondRead.resolve(response(obsoleteSecond)))
  component.rerender(<InterviewSummary sessionId={firstId} />)
  expectPendingSummary()
  expect(document.body.textContent).not.toContain('STALE second-session summary')
  const currentFirst = completedDetail(firstId, [2, 1, 3, 1, 2], [7, 4, 12, 1, 5])
  currentFirst.questions[0].question_text = 'Current reselected first-session summary'
  await act(async () => latestFirstRead.resolve(response(currentFirst)))
  await screen.findByText('Current reselected first-session summary')
  expect(fact(summary(), 'Total attempts')).toBe('9')
  expect(fact(summary(), 'Total retries')).toBe('4')
  expect(fact(question(1), 'Final attempt')).toBe('7')
  expect(within(summary()).queryByRole('alert')).toBeNull()
  expect(document.body.textContent).not.toContain('Old first-session summary')
  expect(document.body.textContent).not.toContain('STALE second-session summary')
  expectReadRequests(fetchMock.mock.calls, [firstId, secondId, firstId])
})

test.each(['success', 'error'] as const)('unmount aborts the old request and late %s cannot populate a fresh summary', async (settlement) => {
  const pending = deferredResponse()
  const fresh = completedDetail(secondId); fresh.questions[0].question_text = 'Fresh mounted session question'
  const fetchMock = vi.fn<(url: string, options?: RequestInit) => Promise<Response>>()
    .mockReturnValueOnce(pending.promise).mockResolvedValueOnce(response(fresh))
  vi.stubGlobal('fetch', fetchMock)
  const component = render(<InterviewSummary sessionId={firstId} />)
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
  const signal = fetchMock.mock.calls[0][1]?.signal
  component.unmount()
  expect(signal?.aborted).toBe(true)
  render(<InterviewSummary sessionId={secondId} />)
  await screen.findByText('Fresh mounted session question')
  await act(async () => {
    if (settlement === 'success') {
      const older = completedDetail(); older.questions[0].question_text = 'STALE unmounted session question'
      pending.resolve(response(older))
    } else pending.reject(new TypeError(privateMarker))
  })
  expect(screen.getByText('Fresh mounted session question')).toBeTruthy()
  expect(document.body.textContent).not.toContain('STALE unmounted session question')
  expect(document.body.textContent).not.toContain(privateMarker)
  expect(within(summary()).queryByText('Interview summary is unavailable.')).toBeNull()
  expectReadRequests(fetchMock.mock.calls, [firstId, secondId])
})
