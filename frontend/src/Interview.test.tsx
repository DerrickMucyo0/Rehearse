// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import Interview from './Interview'
import type { Attempt, AttemptComparison, DeliveryMetricChange, InterviewSession, MetricChange } from './interviewApi'

const storageKey = 'rehearse.session_id'
const questions = ['Question one', 'Question two', 'Question three', 'Question four', 'Question five']
type SavedAttempt = Attempt
type Metric = MetricChange
type Comparison = AttemptComparison
const unavailable: Metric = {
  before: null, after: null, delta: null, before_unavailable_reason: 'no_measurement',
  after_unavailable_reason: 'no_measurement', comparable: false, comparison_unavailable_reason: 'both_unavailable',
}
const deliveryUnavailable: DeliveryMetricChange = {
  ...unavailable, before_unavailable_reason: 'no_measurement', after_unavailable_reason: 'no_measurement',
}
function metric(before: number, after: number): Metric {
  return { before, after, delta: after - before, before_unavailable_reason: null, after_unavailable_reason: null,
    comparable: true, comparison_unavailable_reason: null }
}
function freshSession(id = 'session-1'): InterviewSession {
  return { id, status: 'active', current_question_index: 0, current_question: questions[0], questions,
    answers: [], current_question_latest_attempt_number: 0 }
}
function response(data: unknown, status = 200) { return new Response(JSON.stringify(data), { status }) }
function mockSessionApi(initial = freshSession()) {
  let session = initial
  let creations = 0
  const attempts = new Map<number, SavedAttempt[]>()
  const comparisons = new Map<number, Comparison>()
  let intercept: ((url: string, options?: RequestInit) => Promise<Response> | Response | undefined) | undefined
  function saved(questionIndex = session.current_question_index) { return attempts.get(questionIndex) ?? [] }
  function append(answer: string, measurementId: string | null = null, attemptNumber = session.current_question_latest_attempt_number + 1) {
    const attempt: SavedAttempt = { id: `attempt-${session.current_question_index}-${attemptNumber}`,
      question_index: session.current_question_index, attempt_number: attemptNumber, answer,
      submitted_at: '2026-10-04T12:00:00Z', measurement_id: measurementId }
    attempts.set(session.current_question_index, [...saved(), attempt])
    session = { ...session, current_question_latest_attempt_number: attemptNumber }
    return attempt
  }
  function advance() {
    const latest = saved().at(-1)
    const nextIndex = session.current_question_index + 1
    session = { ...session, current_question_index: nextIndex, current_question: questions[nextIndex] ?? null,
      status: nextIndex === questions.length ? 'completed' : 'active',
      answers: [...session.answers, latest?.answer ?? 'Saved answer'], current_question_latest_attempt_number: 0 }
    return session
  }
  function comparison(questionIndex: number): Comparison {
    const list = saved(questionIndex)
    const identity = (attempt: SavedAttempt) => ({ id: attempt.id, attempt_number: attempt.attempt_number,
      measurement_id: attempt.measurement_id, measurement_version: attempt.measurement_id ? 'speaking-metrics-v1' : null,
      measurement_source: attempt.measurement_id ? 'original_transcription' : null })
    return comparisons.get(questionIndex) ?? { session_id: session.id, question_index: questionIndex,
      before_attempt: list[0] ? identity(list[0]) : null, after_attempt: list.length > 1 ? identity(list.at(-1)!) : null,
      comparison: list.length > 1 ? { recognized_word_count: { ...unavailable }, um_count: { ...unavailable },
        uh_count: { ...unavailable }, timed_utterance_span_seconds: { ...unavailable }, estimated_words_per_minute: { ...unavailable } } : null,
      delivery_comparison: list.length > 1 ? { before_version: null, after_version: null, before_source: null, after_source: null,
        pause_count: { ...deliveryUnavailable }, total_pause_duration_seconds: { ...deliveryUnavailable }, longest_pause_seconds: { ...deliveryUnavailable } } : null }
  }
  const fetchMock = vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
    const url = String(input)
    const override = intercept?.(url, options)
    if (override) return await override
    if (url === '/api/sessions' && options?.method === 'POST') {
      creations += 1
      attempts.clear()
      comparisons.clear()
      session = freshSession(`session-${creations}`)
      return response(session, 201)
    }
    const match = url.match(/\/questions\/(\d+)\/(attempts|continue|comparison)$/)
    if (match) {
      const questionIndex = Number(match[1])
      if (match[2] === 'attempts' && options?.method === 'POST') {
        const body = JSON.parse(options.body as string) as { expected_last_attempt_number: number; answer: string; measurement_id: string | null }
        expect(questionIndex).toBe(session.current_question_index)
        expect(body.expected_last_attempt_number).toBe(session.current_question_latest_attempt_number)
        const attempt = append(body.answer, body.measurement_id)
        return response({ attempt, session }, 201)
      }
      if (match[2] === 'continue' && options?.method === 'POST') {
        const body = JSON.parse(options.body as string) as { expected_last_attempt_number: number }
        expect(questionIndex).toBe(session.current_question_index)
        expect(body.expected_last_attempt_number).toBe(session.current_question_latest_attempt_number)
        expect(saved().length).toBeGreaterThan(0)
        return response(advance())
      }
      if (match[2] === 'attempts') return response(saved(questionIndex))
      if (match[2] === 'comparison') return response(comparison(questionIndex))
    }
    if (url === `/api/sessions/${session.id}` && (!options?.method || options.method === 'GET')) return response(session)
    throw new Error(`Unexpected mocked endpoint: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  return { fetchMock, append, advance, saved, comparison,
    session: () => session, creations: () => creations,
    setSession: (next: InterviewSession) => { session = next },
    setComparison: (next: Comparison) => { comparisons.set(next.question_index, next) },
    intercept: (next: typeof intercept) => { intercept = next },
    posts: (suffix: string) => fetchMock.mock.calls.filter(([url, options]) => String(url).endsWith(suffix) && options?.method === 'POST'),
    gets: (suffix: string) => fetchMock.mock.calls.filter(([url, options]) => String(url).endsWith(suffix) && options?.method !== 'POST') }
}
beforeEach(() => {
  sessionStorage.clear()
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Unmocked network is forbidden')))
})
afterEach(() => { cleanup(); sessionStorage.clear(); vi.unstubAllGlobals(); vi.restoreAllMocks() })
async function start() {
  fireEvent.click(screen.getByRole('button', { name: 'Start Interview' }))
  await screen.findByRole('textbox', { name: 'Your answer' })
}
async function submit(answer: string) {
  fireEvent.change(screen.getByRole('textbox', { name: 'Your answer' }), { target: { value: answer } })
  fireEvent.click(screen.getByRole('button', { name: 'Submit Attempt' }))
  await screen.findByRole('button', { name: 'Continue' })
  await waitFor(() => expect((screen.getByRole('button', { name: 'Continue' }) as HTMLButtonElement).disabled).toBe(false))
}
async function retry() {
  fireEvent.click(screen.getByRole('button', { name: /^Retry(?: Again)?$/ }))
  await screen.findByRole('textbox', { name: 'Your answer' })
}
async function restore(api: ReturnType<typeof mockSessionApi>) {
  sessionStorage.setItem(storageKey, api.session().id)
  render(<Interview />)
  await screen.findByText(`Question ${api.session().current_question_index + 1} of 5`)
}
function postedBody(api: ReturnType<typeof mockSessionApi>, suffix: string, index = 0) {
  return JSON.parse(api.posts(suffix)[index][1]!.body as string) as Record<string, unknown>
}
function comparisonRow(label: string) {
  const row = screen.getByRole('row', { name: new RegExp(label) })
  return within(row).getAllByRole('cell').map((cell) => cell.textContent)
}

test('a fresh question composes and stores only the session ID for reload', async () => {
  const api = mockSessionApi()
  render(<Interview />)
  await start()
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('')
  expect(screen.queryByRole('button', { name: 'Continue' })).toBeNull()
  expect(api.gets('/questions/0/attempts')).toHaveLength(0)
  expect(sessionStorage.getItem(storageKey)).toBe(api.session().id)
  expect(JSON.stringify(sessionStorage)).not.toContain('Question one')
})

test('Attempt 1 uses the new endpoint and revision zero, remains on the question, and enters saved review', async () => {
  const api = mockSessionApi()
  render(<Interview />); await start(); await submit('First saved answer')
  expect(postedBody(api, '/attempts')).toEqual({ expected_last_attempt_number: 0, answer: 'First saved answer', measurement_id: null })
  expect(screen.getByText('Question 1 of 5')).toBeTruthy()
  expect(screen.getByRole('heading', { name: 'Attempt 1' })).toBeTruthy()
  expect(screen.getByText('First saved answer', { exact: true })).toBeTruthy()
  expect(screen.queryByRole('textbox')).toBeNull()
  expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy()
  expect(api.gets('/comparison')).toHaveLength(0)
  expect(screen.queryByRole('table')).toBeNull()
  expect(api.posts('/answers')).toHaveLength(0)
})

test('Retry and Cancel Retry are local transitions preserving saved attempts and clearing unsaved text', async () => {
  const api = mockSessionApi()
  render(<Interview />); await start(); await submit('Saved baseline')
  const calls = api.fetchMock.mock.calls.length
  await retry()
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('')
  expect(screen.getByText('Saved baseline')).toBeTruthy()
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Unsaved draft' } })
  expect(screen.queryByRole('heading', { name: 'Attempt 2' })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Cancel Retry' }))
  expect(screen.queryByRole('textbox')).toBeNull()
  expect(api.fetchMock.mock.calls).toHaveLength(calls)
  await retry()
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('')
})

test('Attempt 2 and Attempt 3 use authoritative revisions, preserve history, and update default comparison', async () => {
  const api = mockSessionApi()
  render(<Interview />); await start(); await submit('Attempt one text'); await retry(); await submit('Attempt two text')
  expect(postedBody(api, '/attempts', 1)).toEqual({ expected_last_attempt_number: 1, answer: 'Attempt two text', measurement_id: null })
  expect(screen.getByRole('heading', { name: 'Attempt 1' })).toBeTruthy()
  expect(screen.getByRole('heading', { name: 'Attempt 2' })).toBeTruthy()
  await screen.findByRole('table', { name: 'Speaking duration is shown in seconds.' })
  expect(api.gets('/comparison')).toHaveLength(1)
  expect(screen.getByText(/Attempt 1.*Attempt 2/)).toBeTruthy()
  await retry(); await submit('Attempt three text')
  expect(postedBody(api, '/attempts', 2).expected_last_attempt_number).toBe(2)
  for (const value of ['Attempt one text', 'Attempt two text', 'Attempt three text']) expect(screen.getByText(value)).toBeTruthy()
  expect(screen.getByRole('heading', { name: 'Attempt 3' })).toBeTruthy()
  await waitFor(() => expect(api.gets('/comparison')).toHaveLength(2))
  expect(screen.getByText(/Attempt 1.*Attempt 3/)).toBeTruthy()
})

test('Continue sends authoritative revision and advances only after its response', async () => {
  const api = mockSessionApi()
  render(<Interview />); await start(); await submit('Saved')
  let resolve!: (value: Response) => void
  api.intercept((url, options) => url.endsWith('/continue') && options?.method === 'POST'
    ? new Promise<Response>((done) => { resolve = done }) : undefined)
  const button = screen.getByRole('button', { name: 'Continue' })
  fireEvent.click(button); fireEvent.click(button)
  expect(api.posts('/continue')).toHaveLength(1)
  expect(postedBody(api, '/continue')).toEqual({ expected_last_attempt_number: 1 })
  expect(screen.getByText('Question 1 of 5')).toBeTruthy()
  expect((screen.getByRole('button', { name: /^Retry$/ }) as HTMLButtonElement).disabled).toBe(true)
  await act(async () => resolve(response(api.advance())))
  await screen.findByText('Question 2 of 5')
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('')
  expect(screen.queryByText('Saved', { exact: true })).toBeNull()
  expect(screen.queryByRole('table')).toBeNull()
})

test('pending attempt submission prevents double writes and leaves the question active', async () => {
  const api = mockSessionApi()
  render(<Interview />); await start()
  let resolve!: (value: Response) => void
  api.intercept((url, options) => url.endsWith('/attempts') && options?.method === 'POST'
    ? new Promise<Response>((done) => { resolve = done }) : undefined)
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Answer' } })
  const button = screen.getByRole('button', { name: 'Submit Attempt' })
  fireEvent.click(button); fireEvent.click(button)
  expect(api.posts('/attempts')).toHaveLength(1)
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).disabled).toBe(true)
  const attempt = api.append('Answer')
  await act(async () => resolve(response({ attempt, session: api.session() }, 201)))
  await screen.findByRole('button', { name: 'Continue' })
  expect(screen.getByText('Question 1 of 5')).toBeTruthy()
})

test('retry and Continue use backend revision rather than the number of listed attempts', async () => {
  const api = mockSessionApi()
  api.append('Authoritative saved attempt', null, 7)
  await restore(api)
  await screen.findByRole('button', { name: 'Continue' })
  await retry(); await submit('Next draft')
  expect(postedBody(api, '/attempts').expected_last_attempt_number).toBe(7)
  fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
  await screen.findByText('Question 2 of 5')
  expect(postedBody(api, '/continue').expected_last_attempt_number).toBe(8)
})

test('the final saved attempt is reviewed before explicit Continue completes the interview', async () => {
  const api = mockSessionApi()
  render(<Interview />); await start()
  for (let index = 0; index < 5; index += 1) {
    await screen.findByText(`Question ${index + 1} of 5`)
    await submit(`Answer ${index + 1}`)
    expect(screen.queryByRole('heading', { name: 'Interview Complete' })).toBeNull()
    expect(screen.queryByRole('textbox')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
  }
  await screen.findByRole('heading', { name: 'Interview Complete' })
  expect(screen.getByText('You completed all 5 questions.')).toBeTruthy()
  expect(screen.queryByRole('button', { name: /^Retry/ })).toBeNull()
  expect(screen.queryByRole('button', { name: 'Record Answer' })).toBeNull()
  expect(api.creations()).toBe(1)
  fireEvent.click(screen.getByRole('button', { name: 'Start New Interview' }))
  await screen.findByText('Question 1 of 5')
  expect(api.creations()).toBe(2)
})

test.each([0, 1, 3])('page reload reconstructs %i saved attempts without persisting draft text', async (count) => {
  const api = mockSessionApi()
  for (let index = 0; index < count; index += 1) api.append(`Saved ${index + 1}`)
  await restore(api)
  if (count === 0) {
    await screen.findByRole('textbox', { name: 'Your answer' })
    expect(screen.queryByRole('button', { name: 'Continue' })).toBeNull()
  } else {
    await screen.findByRole('button', { name: 'Continue' })
    expect(screen.queryByRole('textbox')).toBeNull()
    for (let index = 0; index < count; index += 1) expect(screen.getByText(`Saved ${index + 1}`)).toBeTruthy()
    if (count > 1) await screen.findByRole('table', { name: 'Speaking duration is shown in seconds.' })
  }
  expect(api.creations()).toBe(0)
  expect(api.gets(`/api/sessions/${api.session().id}`)).toHaveLength(2)
  expect(api.gets('/questions/0/attempts')).toHaveLength(1)
  expect(JSON.stringify(sessionStorage)).not.toContain('Saved')
})

test('reload of a completed session shows completion without fetching active attempts', async () => {
  const api = mockSessionApi({ ...freshSession(), status: 'completed', current_question_index: 5,
    current_question: null, answers: ['one', 'two', 'three', 'four', 'five'] })
  sessionStorage.setItem(storageKey, api.session().id)
  render(<Interview />)
  await screen.findByRole('heading', { name: 'Interview Complete' })
  expect(screen.queryByRole('textbox')).toBeNull()
  expect(api.gets('/attempts')).toHaveLength(0)
  expect(screen.queryByRole('button', { name: /^Retry/ })).toBeNull()
})

test('failed restart preserves completed state', async () => {
  const api = mockSessionApi({ ...freshSession(), status: 'completed', current_question_index: 5,
    current_question: null, answers: ['one', 'two', 'three', 'four', 'five'] })
  sessionStorage.setItem(storageKey, api.session().id)
  render(<Interview />); await screen.findByRole('heading', { name: 'Interview Complete' })
  api.intercept((url, options) => url === '/api/sessions' && options?.method === 'POST'
    ? Promise.reject(new TypeError('Network unavailable')) : undefined)
  fireEvent.click(screen.getByRole('button', { name: 'Start New Interview' }))
  await screen.findByRole('alert')
  expect(screen.getByRole('heading', { name: 'Interview Complete' })).toBeTruthy()
  expect(sessionStorage.getItem(storageKey)).toBe(api.session().id)
})

test.each(['attempts', 'continue'] as const)('409 %s reconciles authoritative attempts without silently resubmitting', async (operation) => {
  const api = mockSessionApi()
  render(<Interview />); await start(); await submit('Existing answer')
  if (operation === 'attempts') {
    await retry()
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Stale draft' } })
  }
  let conflict = true
  api.intercept((url, options) => {
    if (conflict && url.endsWith(`/${operation}`) && options?.method === 'POST') {
      conflict = false
      api.append('Saved in another tab')
      return response({ detail: 'Attempt revision does not match the current question.' }, 409)
    }
    return undefined
  })
  fireEvent.click(screen.getByRole('button', { name: operation === 'attempts' ? 'Submit Attempt' : 'Continue' }))
  await screen.findByText('Saved in another tab')
  expect(screen.getByRole('alert').textContent).toMatch(/changed.*saved state/i)
  expect(screen.queryByRole('textbox')).toBeNull()
  expect(screen.queryByText('Stale draft')).toBeNull()
  expect(api.posts(`/${operation}`)).toHaveLength(operation === 'attempts' ? 2 : 1)
  await retry(); await submit('Deliberately retried new draft')
  expect(postedBody(api, '/attempts', operation === 'attempts' ? 2 : 1).expected_last_attempt_number).toBe(2)
})

test('409 Continue reconstructs the next question when another tab advanced', async () => {
  const api = mockSessionApi()
  render(<Interview />); await start(); await submit('Saved')
  api.intercept((url, options) => {
    if (url.endsWith('/continue') && options?.method === 'POST') {
      api.advance()
      return response({ detail: 'Question is not current.' }, 409)
    }
    return undefined
  })
  fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
  await screen.findByText('Question 2 of 5')
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('')
  expect(api.posts('/continue')).toHaveLength(1)
})

test.each([false, true])('ambiguous submit requires recheck before further writes; committed=%s', async (committed) => {
  const api = mockSessionApi()
  render(<Interview />); await start()
  let fail = true
  api.intercept((url, options) => {
    if (fail && url.endsWith('/attempts') && options?.method === 'POST') {
      fail = false
      if (committed) api.append('Ambiguous answer')
      return Promise.reject(new TypeError('Connection lost after sending'))
    }
    return undefined
  })
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Ambiguous answer' } })
  fireEvent.click(screen.getByRole('button', { name: 'Submit Attempt' }))
  await screen.findByRole('button', { name: 'Recheck saved state' })
  const pendingSubmit = screen.queryByRole('button', { name: 'Submit Attempt' })
  if (pendingSubmit) expect((pendingSubmit as HTMLButtonElement).disabled).toBe(true)
  expect(api.posts('/attempts')).toHaveLength(1)
  fireEvent.click(screen.getByRole('button', { name: 'Recheck saved state' }))
  if (committed) {
    await screen.findByRole('button', { name: 'Continue' })
    expect(screen.getByText('Ambiguous answer')).toBeTruthy()
    expect(screen.queryByRole('textbox')).toBeNull()
  } else {
    await waitFor(() => expect((screen.getByRole('button', { name: 'Submit Attempt' }) as HTMLButtonElement).disabled).toBe(false))
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('Ambiguous answer')
    fireEvent.click(screen.getByRole('button', { name: 'Submit Attempt' }))
    await screen.findByRole('button', { name: 'Continue' })
    expect(api.posts('/attempts')).toHaveLength(2)
  }
  expect(api.saved()).toHaveLength(1)
})

test.each([false, true])('ambiguous Continue is reconciled before a manual retry; committed=%s', async (committed) => {
  const api = mockSessionApi()
  render(<Interview />); await start(); await submit('Saved')
  let fail = true
  api.intercept((url, options) => {
    if (fail && url.endsWith('/continue') && options?.method === 'POST') {
      fail = false
      if (committed) api.advance()
      return Promise.reject(new TypeError('Connection lost after sending'))
    }
    return undefined
  })
  fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
  await screen.findByRole('button', { name: 'Recheck saved state' })
  expect((screen.getByRole('button', { name: /^Retry$/ }) as HTMLButtonElement).disabled).toBe(true)
  expect((screen.getByRole('button', { name: 'Continue' }) as HTMLButtonElement).disabled).toBe(true)
  fireEvent.click(screen.getByRole('button', { name: 'Recheck saved state' }))
  if (committed) {
    await screen.findByText('Question 2 of 5')
    expect(screen.queryByText('Saved', { exact: true })).toBeNull()
  } else {
    await waitFor(() => expect((screen.getByRole('button', { name: 'Continue' }) as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    await screen.findByText('Question 2 of 5')
  }
  expect(api.posts('/continue')).toHaveLength(committed ? 1 : 2)
})

test('a failed authority recheck keeps writes blocked', async () => {
  const api = mockSessionApi()
  render(<Interview />); await start()
  api.intercept((url, options) => {
    if (url.endsWith('/attempts') && options?.method === 'POST') return Promise.reject(new TypeError('Write response lost'))
    if (url === '/api/sessions/session-1') return Promise.reject(new TypeError('Still offline'))
    return undefined
  })
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Draft' } })
  fireEvent.click(screen.getByRole('button', { name: 'Submit Attempt' }))
  await screen.findByRole('button', { name: 'Recheck saved state' })
  fireEvent.click(screen.getByRole('button', { name: 'Recheck saved state' }))
  await screen.findByRole('alert')
  expect((screen.getByRole('button', { name: 'Submit Attempt' }) as HTMLButtonElement).disabled).toBe(true)
  expect(api.posts('/attempts')).toHaveLength(1)
})

test('neutral comparison displays backend deltas, measured zero and display-only rounding', async () => {
  const api = mockSessionApi()
  api.append('Before'); api.append('After')
  const comparison = api.comparison(0)
  comparison.comparison = { recognized_word_count: metric(8, 24), um_count: metric(3, 0), uh_count: metric(0, 0),
    timed_utterance_span_seconds: { ...metric(1.24, 2.26), delta: 1.02 },
    estimated_words_per_minute: { ...metric(92.04, 108.06), delta: 16.02 } }
  api.setComparison(comparison)
  await restore(api); await screen.findByRole('table', { name: 'Speaking duration is shown in seconds.' })
  expect(comparisonRow('Recognized words')).toEqual(['8', '24', '+16'])
  expect(comparisonRow('Um')).toEqual(['3', '0', '-3'])
  expect(comparisonRow('Uh')).toEqual(['0', '0', '0'])
  expect(comparisonRow('Speaking duration')).toEqual(['1.2', '2.3', '+1.0'])
  expect(comparisonRow('Words per minute')).toEqual(['92.0', '108.1', '+16.0'])
  const table = screen.getByRole('table', { name: 'Speaking duration is shown in seconds.' })
  for (const label of ['Before', 'After', 'Change']) expect(within(table).getByRole('columnheader', { name: label })).toBeTruthy()
  expect(table.textContent).not.toMatch(/\b(improved|better|worse|strong|weak|good|bad|score)\b/i)
})

test('typed comparisons show unavailable rather than fabricated zero', async () => {
  const api = mockSessionApi()
  api.append('Typed before'); api.append('Typed after')
  await restore(api); await screen.findByRole('table', { name: 'Speaking duration is shown in seconds.' })
  for (const label of ['Recognized words', 'Um', 'Uh', 'Speaking duration', 'Words per minute']) {
    for (const cell of comparisonRow(label)) expect(cell).toContain('Unavailable')
  }
  expect(screen.getByRole('table', { name: 'Speaking duration is shown in seconds.' }).textContent).not.toMatch(/\b0\b/)
})

test.each(['unsupported_language', 'missing_timings', 'timing_coverage_mismatch', 'invalid_timing', 'invalid_timing_order', 'unusable_span'] as const)('comparison preserves neutral unavailability for %s', async (reason) => {
  const api = mockSessionApi()
  api.append('Before'); api.append('After')
  const comparison = api.comparison(0)
  const changed = { ...unavailable, before_unavailable_reason: reason, after_unavailable_reason: reason }
  comparison.comparison = { recognized_word_count: metric(4, 4), um_count: changed, uh_count: changed,
    timed_utterance_span_seconds: changed, estimated_words_per_minute: changed }
  api.setComparison(comparison)
  await restore(api); await screen.findByRole('table', { name: 'Speaking duration is shown in seconds.' })
  const row = comparisonRow(reason === 'unsupported_language' ? 'Um' : 'Speaking duration')
  for (const cell of row) expect(cell).toContain('Unavailable')
  expect(comparisonRow('Recognized words')).toEqual(['4', '4', '0'])
})

test.each(['measurement_version_mismatch', 'measurement_source_incompatible'] as const)('%s shows measurements but never subtracts incompatible versions/sources', async (reason) => {
  const api = mockSessionApi()
  api.append('Before'); api.append('After')
  const comparison = api.comparison(0)
  const changed = { ...metric(4, 8), delta: null, comparable: false, comparison_unavailable_reason: reason }
  comparison.comparison = { recognized_word_count: changed, um_count: changed, uh_count: changed,
    timed_utterance_span_seconds: changed, estimated_words_per_minute: changed }
  api.setComparison(comparison)
  await restore(api); await screen.findByRole('table', { name: 'Speaking duration is shown in seconds.' })
  expect(comparisonRow('Recognized words').slice(0, 2)).toEqual(['4', '8'])
  expect(comparisonRow('Recognized words')[2]).toContain('Unavailable')
  expect(screen.getByRole('table', { name: 'Speaking duration is shown in seconds.' }).textContent).not.toContain('+4')
})

test.each(['attempts', 'continue'] as const)('acknowledged %s followed by a read failure remains guarded until reconciliation', async (operation) => {
  const api = mockSessionApi()
  render(<Interview />); await start()
  if (operation === 'continue') await submit('Saved')
  else fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Acknowledged answer' } })
  let acknowledged = false
  let failRead = true
  api.intercept((url, options) => {
    if (!acknowledged && url.endsWith(`/${operation}`) && options?.method === 'POST') {
      acknowledged = true
      if (operation === 'attempts') {
        const attempt = api.append('Acknowledged answer')
        return response({ attempt, session: api.session() }, 201)
      }
      return response(api.advance())
    }
    if (acknowledged && failRead && url === '/api/sessions/session-1' && options?.method !== 'POST') {
      failRead = false
      return Promise.reject(new TypeError('Read response unavailable'))
    }
    return undefined
  })
  fireEvent.click(screen.getByRole('button', { name: operation === 'attempts' ? 'Submit Attempt' : 'Continue' }))
  await screen.findByRole('button', { name: 'Recheck saved state' })
  expect(api.posts(`/${operation}`)).toHaveLength(1)
  fireEvent.click(screen.getByRole('button', { name: 'Recheck saved state' }))
  if (operation === 'attempts') {
    await screen.findByRole('button', { name: 'Continue' })
    expect(screen.getByText('Acknowledged answer')).toBeTruthy()
    expect(api.saved()).toHaveLength(1)
  } else {
    await screen.findByText('Question 2 of 5')
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('')
  }
  expect(api.posts(`/${operation}`)).toHaveLength(1)
})

test('tiny nonzero backend deltas retain their signs after display rounding', async () => {
  const api = mockSessionApi()
  api.append('Before'); api.append('After')
  const comparison = api.comparison(0)
  comparison.comparison = { recognized_word_count: metric(4, 4), um_count: metric(0, 0), uh_count: metric(0, 0),
    timed_utterance_span_seconds: { ...metric(1, 1.01), delta: 0.01 },
    estimated_words_per_minute: { ...metric(100, 99.99), delta: -0.01 } }
  api.setComparison(comparison)
  await restore(api); await screen.findByRole('table', { name: 'Speaking duration is shown in seconds.' })
  expect(comparisonRow('Speaking duration')[2]).toBe('+0.0')
  expect(comparisonRow('Words per minute')[2]).toBe('-0.0')
})

test('a null comparison from the backend never produces an empty comparison card', async () => {
  const api = mockSessionApi()
  api.append('Before'); api.append('After')
  const comparison = api.comparison(0)
  comparison.comparison = null
  comparison.delivery_comparison = null
  comparison.after_attempt = null
  api.setComparison(comparison)
  await restore(api); await screen.findByRole('button', { name: 'Continue' })
  expect(screen.queryByRole('table')).toBeNull()
  expect(screen.queryByRole('heading', { name: 'Before / After comparison' })).toBeNull()
})

test.each(['attempts', 'continue'] as const)('a server failure on %s requires recheck before another mutation', async (operation) => {
  const api = mockSessionApi()
  render(<Interview />); await start()
  if (operation === 'continue') await submit('Saved')
  else fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Draft' } })
  let fail = true
  api.intercept((url, options) => {
    if (fail && url.endsWith(`/${operation}`) && options?.method === 'POST') {
      fail = false
      return response({ detail: 'Unavailable' }, 503)
    }
    return undefined
  })
  fireEvent.click(screen.getByRole('button', { name: operation === 'attempts' ? 'Submit Attempt' : 'Continue' }))
  await screen.findByRole('button', { name: 'Recheck saved state' })
  expect(api.posts(`/${operation}`)).toHaveLength(1)
  fireEvent.click(screen.getByRole('button', { name: 'Recheck saved state' }))
  const button = screen.getByRole('button', { name: operation === 'attempts' ? 'Submit Attempt' : 'Continue' })
  await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false))
  expect(api.posts(`/${operation}`)).toHaveLength(1)
})

test('Continue in another tab during an attempt read reconstructs the next question before accepting the view', async () => {
  const api = mockSessionApi()
  api.append('Finalized in another tab')
  let advance = true
  api.intercept((url, options) => {
    if (advance && url.endsWith('/questions/0/attempts') && options?.method !== 'POST') {
      advance = false
      const oldAttempts = api.saved()
      api.advance()
      return response(oldAttempts)
    }
    return undefined
  })
  sessionStorage.setItem(storageKey, api.session().id)
  render(<Interview />)
  await screen.findByText('Question 2 of 5')
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('')
  expect(screen.queryByText('Finalized in another tab', { exact: true })).toBeNull()
  expect(screen.queryByRole('button', { name: 'Continue' })).toBeNull()
  expect(api.gets('/questions/0/attempts')).toHaveLength(1)
  expect(api.gets('/questions/1/attempts')).toHaveLength(1)
  expect(api.fetchMock.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(0)
})

test('an append during attempt retrieval causes a read-only reread with coherent history and comparison', async () => {
  const api = mockSessionApi()
  api.append('Initial saved answer')
  let append = true
  api.intercept((url, options) => {
    if (append && url.endsWith('/questions/0/attempts') && options?.method !== 'POST') {
      append = false
      const oldAttempts = api.saved()
      api.append('Concurrent second answer')
      return response(oldAttempts)
    }
    return undefined
  })
  await restore(api)
  await screen.findByRole('button', { name: 'Continue' })
  await screen.findByRole('table', { name: 'Speaking duration is shown in seconds.' })
  expect(screen.getByText('Initial saved answer')).toBeTruthy()
  expect(screen.getByText('Concurrent second answer')).toBeTruthy()
  expect(screen.getByText(/Attempt 1.*Attempt 2/)).toBeTruthy()
  expect(api.gets('/questions/0/attempts')).toHaveLength(2)
  expect(api.gets('/comparison')).toHaveLength(1)
  expect(api.fetchMock.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(0)
})

test('an append during comparison retrieval discards its stale latest identity and rereads', async () => {
  const api = mockSessionApi()
  api.append('First saved answer'); api.append('Second saved answer')
  let append = true
  api.intercept((url, options) => {
    if (append && url.endsWith('/questions/0/comparison') && options?.method !== 'POST') {
      append = false
      const oldComparison = api.comparison(0)
      api.append('Concurrent third answer')
      return response(oldComparison)
    }
    return undefined
  })
  await restore(api)
  await screen.findByRole('table', { name: 'Speaking duration is shown in seconds.' })
  expect(screen.getByText('Concurrent third answer')).toBeTruthy()
  expect(screen.getByText(/Attempt 1.*Attempt 3/)).toBeTruthy()
  expect(api.gets('/questions/0/attempts')).toHaveLength(2)
  expect(api.gets('/comparison')).toHaveLength(2)
  expect(api.fetchMock.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(0)
})

test('persistent read disagreement is bounded and requires Recheck without performing a write', async () => {
  const api = mockSessionApi()
  api.append('Initial saved answer')
  api.intercept((url, options) => {
    if (url.endsWith('/questions/0/attempts') && options?.method !== 'POST') {
      const oldAttempts = api.saved()
      api.append(`Concurrent answer ${api.saved().length + 1}`)
      return response(oldAttempts)
    }
    return undefined
  })
  sessionStorage.setItem(storageKey, api.session().id)
  render(<Interview />)
  await screen.findByRole('button', { name: 'Recheck saved state' })
  expect(screen.getByRole('alert').textContent).toContain('Recheck saved state before continuing')
  expect(screen.queryByRole('textbox')).toBeNull()
  expect(api.gets('/questions/0/attempts')).toHaveLength(2)
  expect(api.fetchMock.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(0)
  api.intercept(undefined)
  fireEvent.click(screen.getByRole('button', { name: 'Recheck saved state' }))
  await screen.findByRole('button', { name: 'Continue' })
  expect(screen.getByRole('heading', { name: 'Attempt 3' })).toBeTruthy()
  expect(api.fetchMock.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(0)
})
