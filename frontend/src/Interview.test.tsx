// @vitest-environment jsdom
import { authenticateTestWorkspace } from './authTestUtils'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import Interview from './Interview'
import * as historyApi from './historyApi'
import type { HistoryDetail } from './historyApi'
import { personalizedDrillForFocus } from './personalizedDrills'
import type { Attempt, AttemptComparison, DeliveryMetricChange, InterviewSession, MetricChange, SemanticDiagnosis } from './interviewApi'
import type { ScenarioType } from './scenarios'

const storageKey = 'rehearse.session_id:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
const questions = ['Question one', 'Question two', 'Question three', 'Question four', 'Question five']
const summarySessionId = '11111111-1111-4111-8111-111111111111'
const nextSummarySessionId = '22222222-2222-4222-8222-222222222222'
const readHistoryDetail = historyApi.getHistoryDetail
const summaryAttemptId = (questionIndex: number, number: number) =>
  `00000000-0000-4000-8000-${String((questionIndex + 1) * 100 + number).padStart(12, '0')}`

function completedHistoryDetail(id = summarySessionId, counts = [1, 1, 1, 1, 1], numbers = counts): HistoryDetail {
  const totalAttempts = counts.reduce((total, count) => total + count, 0)
  return {
    summary: { session_id: id, scenario_type: 'job_interview', question_engine: 'deterministic-v1', status: 'completed', created_at: '2026-10-04T11:00:00Z',
      completed_at: '2026-10-04T12:00:01Z', current_question_number: null, total_questions: 5,
      finalized_question_count: 5, questions_practiced_count: 5, total_attempt_count: totalAttempts,
      total_retry_count: totalAttempts - 5, measured_final_answer_count: 0,
      last_submitted_at: '2026-10-04T12:00:00Z', last_saved_activity_at: '2026-10-04T12:00:01Z',
      finalized_points: counts.map((_, question_index) => ({ question_index,
        attempt_id: summaryAttemptId(question_index, numbers[question_index]), attempt_number: numbers[question_index],
        submitted_at: '2026-10-04T12:00:00Z', measurement: null })) },
    questions: counts.map((attempt_count, question_index) => ({ question_index, question_text: questions[question_index],
      finalized: true, attempt_count, latest_attempt_id: summaryAttemptId(question_index, numbers[question_index]),
      latest_attempt_number: numbers[question_index], final_attempt_id: summaryAttemptId(question_index, numbers[question_index]),
      final_attempt_number: numbers[question_index] })),
    selected_question: null,
  }
}
const diagnosis: SemanticDiagnosis = {
  diagnosis_version: 'semantic-diagnosis-v1',
  addressed_question: 'partially', addressed_question_reason: 'The example addresses part of the question.',
  strengths: ['The personal contribution is concrete.'], missing_information: ['Explain the result of the work.'],
  structure: 'mixed', structure_feedback: 'Connect the action to its result.', next_focus: 'supporting_detail',
  next_focus_reason: 'Support the account with a concrete outcome.', retry_instruction: 'Add the result to your next attempt.',
}
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
function freshSession(id = 'session-1', scenarioType: ScenarioType = 'job_interview'): InterviewSession {
  return { id, scenario_type: scenarioType, question_engine: 'deterministic-v1', total_questions: 5, status: 'active', current_question_index: 0, current_question: questions[0], questions,
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
    const attempt: SavedAttempt = { id: session.id.startsWith('session-')
      ? `attempt-${session.current_question_index}-${attemptNumber}` : summaryAttemptId(session.current_question_index, attemptNumber),
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
      questions: session.question_engine === 'live-ai-roleplay-v1' ? questions.slice(0, Math.min(nextIndex + 1, 5)) : session.questions,
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
      const body = JSON.parse(options.body as string) as { scenario_type: ScenarioType }
      creations += 1
      attempts.clear()
      comparisons.clear()
      session = freshSession(`session-${creations}`, body.scenario_type)
      if (initial.question_engine === 'live-ai-roleplay-v1') session = { ...session, question_engine: initial.question_engine, questions: questions.slice(0, 1) }
      return response(session, 201)
    }
    const diagnosisMatch = url.match(/^\/api\/sessions\/([^/]+)\/questions\/(\d+)\/attempts\/(\d+)\/diagnosis$/)
    if (diagnosisMatch && options?.method === 'POST') {
      expect(diagnosisMatch[1]).toBe(session.id)
      expect(saved(Number(diagnosisMatch[2])).some((attempt) => attempt.attempt_number === Number(diagnosisMatch[3]))).toBe(true)
      expect(options.body).toBeUndefined()
      return response(diagnosis)
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
    if (url === `/api/sessions/${session.id}/history-detail` && (!options?.method || options.method === 'GET')) {
      expect(session.status).toBe('completed')
      const counts = questions.map((_, index) => Math.max(1, saved(index).length))
      const numbers = questions.map((_, index) => saved(index).at(-1)?.attempt_number ?? 1)
      const detail = completedHistoryDetail(session.id, counts, numbers)
      detail.summary.question_engine = session.question_engine
      return response(detail)
    }
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
beforeEach(async () => {
  await authenticateTestWorkspace()
  sessionStorage.clear()
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Unmocked network is forbidden')))
  // Existing Practice fixtures use opaque toy IDs. Keep that legacy exception at
  // the History helper boundary; canonical summary tests use the real validator.
  vi.spyOn(historyApi, 'getHistoryDetail').mockImplementation((id, options) => id.startsWith('session-')
    ? Promise.resolve(completedHistoryDetail(id)) : readHistoryDetail(id, options))
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
function deferredResponse() {
  let resolve!: (value: Response) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<Response>((accept, fail) => { resolve = accept; reject = fail })
  return { promise, resolve, reject }
}
function pendingDiagnoses(api: ReturnType<typeof mockSessionApi>) {
  const pending: Array<ReturnType<typeof deferredResponse> & { signal: AbortSignal }> = []
  api.intercept((url, options) => {
    if (url.endsWith('/diagnosis') && options?.method === 'POST') {
      const request = { ...deferredResponse(), signal: options.signal as AbortSignal }
      pending.push(request)
      // Deliberately ignore abort here so tests also prove generation/target guards.
      return request.promise
    }
    return undefined
  })
  return pending
}
function feedback() { return screen.getByRole('region', { name: 'Answer feedback' }) }
function expectNoDrill() { expect(screen.queryByRole('region', { name: 'Practice drill' })).toBeNull() }
function expectDrillForFocus(focus: SemanticDiagnosis['next_focus']) {
  const drill = personalizedDrillForFocus(focus)
  const regions = screen.getAllByRole('region', { name: 'Practice drill' })
  expect(regions).toHaveLength(1)
  const region = regions[0]
  expect(within(feedback()).getByRole('region', { name: 'Practice drill' })).toBe(region)
  expect(within(region).getByRole('heading', { name: drill.title, level: 4 })).toBeTruthy()
  expect(within(region).getByText(drill.goal, { exact: true })).toBeTruthy()
  expect(within(region).getByRole('list').tagName).toBe('OL')
  expect(within(region).getAllByRole('listitem').map((item) => item.textContent)).toEqual(drill.steps)
  expect(region.textContent).toBe(drill.title + drill.goal + drill.steps.join(''))
  expect(within(region).queryByText(drill.drill_version, { exact: true })).toBeNull()
  expect(within(region).queryByText(focus, { exact: true })).toBeNull()
  return region
}
function expectReviewActionsEnabled() {
  expect((screen.getByRole('button', { name: /^Retry(?: Again)?$/ }) as HTMLButtonElement).disabled).toBe(false)
  expect((screen.getByRole('button', { name: 'Continue' }) as HTMLButtonElement).disabled).toBe(false)
}

test('setup shows four described scenarios and changes selection without creating a session', () => {
  const api = mockSessionApi()
  render(<Interview />)
  const choices = within(screen.getByRole('group', { name: 'Practice scenario' })).getAllByRole('radio')
  expect(choices).toHaveLength(4)
  expect((choices[0] as HTMLInputElement).checked).toBe(true)
  for (const choice of choices) {
    expect(choice.closest('label')?.querySelector('.scenario-description')?.textContent).toBeTruthy()
    fireEvent.click(choice)
  }
  expect((choices[3] as HTMLInputElement).checked).toBe(true)
  expect(api.fetchMock).not.toHaveBeenCalled()
  expect(sessionStorage.getItem(storageKey)).toBeNull()
})

test.each([
  ['job_interview', 'Job Interview'], ['public_speaking', 'Public Speaking'],
  ['thesis_defense', 'Thesis Defense'], ['salary_negotiation', 'Salary Negotiation'],
] as const)('explicit Start creates %s and displays its persisted scenario', async (scenarioType, label) => {
  const api = mockSessionApi()
  render(<Interview />)
  fireEvent.click(screen.getByRole('radio', { name: new RegExp(`^${label}`) }))
  expect(api.creations()).toBe(0)
  await start()
  expect(postedBody(api, '/api/sessions')).toEqual({ scenario_type: scenarioType })
  expect(api.creations()).toBe(1)
  expect(screen.getByText(`Scenario: ${label}`)).toBeTruthy()
  expect(screen.queryByRole('group', { name: 'Practice scenario' })).toBeNull()
})

test('creation locks the scenario choices and repeated Start sends only the chosen scenario once', async () => {
  const api = mockSessionApi()
  const pending = deferredResponse()
  api.intercept((url, options) => url === '/api/sessions' && options?.method === 'POST' ? pending.promise : undefined)
  render(<Interview />)
  fireEvent.click(screen.getByRole('radio', { name: /^Thesis Defense/ }))
  const button = screen.getByRole('button', { name: 'Start Interview' })
  fireEvent.click(button); fireEvent.click(button)
  const choices = screen.getAllByRole('radio')
  expect(choices.every((choice) => choice.matches(':disabled'))).toBe(true)
  expect(api.posts('/api/sessions')).toHaveLength(1)
  expect(postedBody(api, '/api/sessions')).toEqual({ scenario_type: 'thesis_defense' })
  await act(async () => { pending.resolve(response(freshSession('session-1', 'thesis_defense'), 201)) })
  await screen.findByText('Scenario: Thesis Defense')
})

test('restored Practice labels the canonical saved scenario without creating another session', async () => {
  const api = mockSessionApi(freshSession('existing-session', 'salary_negotiation'))
  await restore(api)
  expect(screen.getByText('Scenario: Salary Negotiation')).toBeTruthy()
  expect(api.creations()).toBe(0)
  expect(screen.queryByRole('group', { name: 'Practice scenario' })).toBeNull()
})

test('choosing a new scenario on completion leaves the saved label and session unchanged until Start', async () => {
  const api = mockSessionApi({ ...freshSession('session-1', 'thesis_defense'), status: 'completed',
    current_question_index: 5, current_question: null, answers: questions.map(() => 'Saved answer') })
  sessionStorage.setItem(storageKey, api.session().id)
  render(<Interview />)
  await screen.findByRole('heading', { name: 'Interview Complete' })
  fireEvent.click(screen.getByRole('radio', { name: /^Public Speaking/ }))
  expect(screen.getByText('Scenario: Thesis Defense')).toBeTruthy()
  expect(api.creations()).toBe(0)
  expect(sessionStorage.getItem(storageKey)).toBe('session-1')
  fireEvent.click(screen.getByRole('button', { name: 'Start New Interview' }))
  await screen.findByText('Scenario: Public Speaking')
  expect(postedBody(api, '/api/sessions')).toEqual({ scenario_type: 'public_speaking' })
})

test('the existing recovery restart offers scenarios and creates only after explicit Start', async () => {
  const api = mockSessionApi()
  render(<Interview />); await start()
  api.intercept((url, options) => url.endsWith('/attempts') && options?.method === 'POST'
    ? Promise.reject(new TypeError('Response lost')) : undefined)
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Unsaved answer' } })
  fireEvent.click(screen.getByRole('button', { name: 'Submit Attempt' }))
  await screen.findByRole('alert')
  fireEvent.click(screen.getByRole('radio', { name: 'Salary Negotiation' }))
  expect(api.creations()).toBe(1)
  expect(screen.getByText('Scenario: Job Interview')).toBeTruthy()
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('Unsaved answer')
  fireEvent.click(screen.getByRole('button', { name: 'Start New Interview' }))
  await screen.findByText('Scenario: Salary Negotiation')
  expect(api.creations()).toBe(2)
  expect(postedBody(api, '/api/sessions', 1)).toEqual({ scenario_type: 'salary_negotiation' })
})

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
  await screen.findByRole('region', { name: 'Practice drill' })
  expectDrillForFocus(diagnosis.next_focus)
  const calls = api.fetchMock.mock.calls.length
  await retry()
  expectNoDrill()
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('')
  expect(screen.getByText('Saved baseline')).toBeTruthy()
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Unsaved draft' } })
  expect(screen.queryByRole('heading', { name: 'Attempt 2' })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Cancel Retry' }))
  expect(screen.queryByRole('textbox')).toBeNull()
  expectNoDrill()
  expect(api.fetchMock.mock.calls).toHaveLength(calls)
  expect(api.posts('/diagnosis')).toHaveLength(1)
  expect(api.posts('/attempts')).toHaveLength(1)
  expect(api.posts('/continue')).toHaveLength(0)
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
  await screen.findByRole('region', { name: 'Practice drill' })
  expectDrillForFocus(diagnosis.next_focus)
  let resolve!: (value: Response) => void
  api.intercept((url, options) => url.endsWith('/continue') && options?.method === 'POST'
    ? new Promise<Response>((done) => { resolve = done }) : undefined)
  const button = screen.getByRole('button', { name: 'Continue' })
  fireEvent.click(button); fireEvent.click(button)
  expectNoDrill()
  expect(api.posts('/continue')).toHaveLength(1)
  expect(postedBody(api, '/continue')).toEqual({ expected_last_attempt_number: 1 })
  expect(screen.getByText('Question 1 of 5')).toBeTruthy()
  expect((screen.getByRole('button', { name: /^Retry$/ }) as HTMLButtonElement).disabled).toBe(true)
  await act(async () => resolve(response(api.advance())))
  await screen.findByText('Question 2 of 5')
  expectNoDrill()
  expect(api.posts('/diagnosis')).toHaveLength(1)
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
    await screen.findByRole('region', { name: 'Practice drill' })
    expect(screen.queryByRole('heading', { name: 'Interview Complete' })).toBeNull()
    expect(screen.queryByRole('textbox')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
  }
  await screen.findByRole('heading', { name: 'Interview Complete' })
  expectNoDrill()
  expect(screen.getByText('You completed all 5 questions.')).toBeTruthy()
  expect(screen.queryByRole('button', { name: /^Retry/ })).toBeNull()
  expect(screen.queryByRole('button', { name: 'Record Answer' })).toBeNull()
  expect(api.creations()).toBe(1)
  fireEvent.click(screen.getByRole('button', { name: 'Start New Interview' }))
  await screen.findByText('Question 1 of 5')
  expect(api.creations()).toBe(2)
  expectNoDrill()
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
  expectNoDrill()
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

test.each([
  'answer_the_question', 'specificity', 'supporting_detail', 'structure', 'completeness', 'conciseness', 'maintain_strengths',
] as const)('owned diagnosis focus %s renders exactly its drill without extra requests, writes or rerender side effects', async (focus) => {
  const api = mockSessionApi()
  const pending = pendingDiagnoses(api)
  const factsChanged = vi.fn()
  const component = render(<Interview onHistoryFactsChange={factsChanged} />)
  await start(); await submit('Saved drill answer')
  expect(pending).toHaveLength(1)
  expectNoDrill()
  const calls = api.fetchMock.mock.calls.length
  const savedBefore = JSON.stringify(api.saved())
  const sessionBefore = JSON.stringify(api.session())
  const localStorageBefore = JSON.stringify(localStorage)
  const sessionStorageBefore = JSON.stringify(sessionStorage)
  const factsBefore = factsChanged.mock.calls.length
  const storageWrites = (['setItem', 'removeItem', 'clear'] as const).map((method) => vi.spyOn(Storage.prototype, method))
  await act(async () => pending[0].resolve(response({ ...diagnosis, next_focus: focus })))
  await screen.findByRole('region', { name: 'Practice drill' })
  const region = expectDrillForFocus(focus)
  const retryInstruction = within(feedback()).getByText(diagnosis.retry_instruction, { exact: true })
  expect(retryInstruction.compareDocumentPosition(region) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0)
  expect(region.compareDocumentPosition(screen.getByRole('button', { name: 'Retry' })) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0)
  expect(region.compareDocumentPosition(screen.getByRole('button', { name: 'Continue' })) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0)
  component.rerender(<Interview onHistoryFactsChange={factsChanged} />)
  expect(expectDrillForFocus(focus)).toBe(region)
  expectReviewActionsEnabled()
  expect(api.fetchMock.mock.calls).toHaveLength(calls)
  expect(api.posts('/diagnosis')).toHaveLength(1)
  expect(api.posts('/attempts')).toHaveLength(1)
  expect(api.posts('/continue')).toHaveLength(0)
  expect(JSON.stringify(api.saved())).toBe(savedBefore)
  expect(JSON.stringify(api.session())).toBe(sessionBefore)
  expect(JSON.stringify(localStorage)).toBe(localStorageBefore)
  expect(JSON.stringify(sessionStorage)).toBe(sessionStorageBefore)
  expect(factsChanged).toHaveBeenCalledTimes(factsBefore)
  for (const write of storageWrites) expect(write).not.toHaveBeenCalled()
})

test('diagnosis waits for coherent saved review, then renders semantic feedback separately for the acknowledged attempt', async () => {
  const api = mockSessionApi()
  const localStorageBefore = JSON.stringify(localStorage)
  const refresh = deferredResponse()
  let acknowledged = false
  let waitingRead = false
  api.intercept((url, options) => {
    if (url.endsWith('/attempts') && options?.method === 'POST') {
      const body = JSON.parse(options.body as string) as { answer: string }
      const attempt = api.append(body.answer)
      acknowledged = true
      return response({ attempt, session: api.session() }, 201)
    }
    if (acknowledged && !waitingRead && url === '/api/sessions/session-1' && options?.method !== 'POST') {
      waitingRead = true
      return refresh.promise
    }
    return undefined
  })
  const factsChanged = vi.fn()
  render(<Interview onHistoryFactsChange={factsChanged} />); await start()
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Persisted answer' } })
  fireEvent.click(screen.getByRole('button', { name: 'Submit Attempt' }))
  await screen.findByText('Persisted answer', { exact: true })
  await waitFor(() => expect(waitingRead).toBe(true))
  expect(api.posts('/diagnosis')).toHaveLength(0)
  expect(screen.queryByRole('region', { name: 'Answer feedback' })).toBeNull()
  expectNoDrill()
  await act(async () => refresh.resolve(response(api.session())))
  await screen.findByRole('region', { name: 'Answer feedback' })
  await within(feedback()).findByText(diagnosis.addressed_question_reason)
  expectDrillForFocus(diagnosis.next_focus)
  expect(api.posts('/diagnosis')).toHaveLength(1)
  const [url, options] = api.posts('/diagnosis')[0]
  expect(url).toBe('/api/sessions/session-1/questions/0/attempts/1/diagnosis')
  expect(options?.method).toBe('POST')
  expect(options?.body).toBeUndefined()
  for (const label of ['Question addressed', 'Strengths', 'Missing information', 'Structure', 'Next focus', 'Retry instruction']) {
    expect(within(feedback()).getByText(label, { exact: true })).toBeTruthy()
  }
  for (const text of ['Partially', 'Mixed', 'Supporting detail', diagnosis.strengths[0], diagnosis.missing_information[0],
    diagnosis.structure_feedback, diagnosis.next_focus_reason, diagnosis.retry_instruction]) {
    expect(within(feedback()).getByText(text, { exact: true })).toBeTruthy()
  }
  expect(screen.queryByText(/diagnosis_version|semantic-diagnosis-v1/)).toBeNull()
  expect(within(feedback()).queryByText('Persisted answer', { exact: true })).toBeNull()
  expect(within(feedback()).queryByText(/Words per minute|Recognized words|Timed pauses/)).toBeNull()
  expect(api.posts('/attempts')).toHaveLength(1)
  expect(api.posts('/continue')).toHaveLength(0)
  expect(api.saved()).toHaveLength(1)
  expect(factsChanged).toHaveBeenCalledTimes(2)
  expectReviewActionsEnabled()
  expect(JSON.stringify(sessionStorage)).not.toContain(diagnosis.retry_instruction)
  expect(JSON.stringify(localStorage)).toBe(localStorageBefore)
})

test('diagnosis targets the acknowledged sparse attempt number, question, session and persisted identity', async () => {
  const api = mockSessionApi({ ...freshSession('existing-session'), current_question_index: 2, current_question: questions[2], answers: ['First final', 'Second final'] })
  api.append('Earlier authoritative answer', null, 7)
  await restore(api)
  await screen.findByRole('button', { name: 'Continue' })
  expect(api.posts('/diagnosis')).toHaveLength(0)
  api.intercept((url, options) => {
    if (url.endsWith('/attempts') && options?.method === 'POST') {
      const body = JSON.parse(options.body as string) as { answer: string }
      const attempt = api.append(body.answer)
      attempt.id = 'server-persisted-exact-identity'
      return response({ attempt, session: api.session() }, 201)
    }
    return undefined
  })
  await retry(); await submit('Acknowledged eighth attempt')
  await within(feedback()).findByText(diagnosis.retry_instruction)
  expectDrillForFocus(diagnosis.next_focus)
  expect(api.posts('/diagnosis').map(([url]) => url)).toEqual([
    '/api/sessions/existing-session/questions/2/attempts/8/diagnosis',
  ])
  expect(api.saved().at(-1)?.id).toBe('server-persisted-exact-identity')
  expect(api.saved()).toHaveLength(2)
  expect(screen.getByRole('heading', { name: 'Attempt 8' })).toBeTruthy()
})

test.each(['newer attempt', 'different identity', 'advanced question'] as const)(
  'no diagnosis is requested when coherent refresh displays a %s instead of the acknowledged target', async (replacement) => {
    const api = mockSessionApi()
    let acknowledged = false
    let replaced = false
    api.intercept((url, options) => {
      if (url.endsWith('/attempts') && options?.method === 'POST') {
        const attempt = api.append('Acknowledged answer')
        acknowledged = true
        return response({ attempt, session: api.session() }, 201)
      }
      if (acknowledged && !replaced && url === '/api/sessions/session-1' && options?.method !== 'POST') {
        replaced = true
        if (replacement === 'newer attempt') api.append('Newer answer from another tab')
        else if (replacement === 'different identity') api.saved().at(-1)!.id = 'different-persisted-identity'
        else api.advance()
      }
      return undefined
    })
    render(<Interview />); await start()
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Acknowledged answer' } })
    fireEvent.click(screen.getByRole('button', { name: 'Submit Attempt' }))
    await waitFor(() => expect(replaced).toBe(true))
    if (replacement === 'advanced question') {
      await screen.findByText('Question 2 of 5')
      await waitFor(() => expect((screen.getByRole('button', { name: 'Record Answer' }) as HTMLButtonElement).disabled).toBe(false))
    } else await waitFor(expectReviewActionsEnabled)
    expect(api.posts('/diagnosis')).toHaveLength(0)
    expect(screen.queryByRole('region', { name: 'Answer feedback' })).toBeNull()
    expectNoDrill()
  },
)

test('pending semantic feedback leaves Retry, Continue and interview navigation independently enabled', async () => {
  const api = mockSessionApi()
  const pending = pendingDiagnoses(api)
  const navigationBusy = vi.fn()
  render(<Interview onNavigationBusyChange={navigationBusy} />); await start(); await submit('Saved answer')
  expect(pending).toHaveLength(1)
  const region = feedback()
  expect(region.getAttribute('aria-busy')).toBe('true')
  expectNoDrill()
  expect(within(region).getByRole('status').textContent).toBe('Generating answer feedback…')
  expect(screen.getByRole('region', { name: 'Interview practice' }).getAttribute('aria-busy')).toBe('false')
  expect(navigationBusy.mock.calls.at(-1)).toEqual([false])
  expectReviewActionsEnabled()
  expect(api.posts('/attempts')).toHaveLength(1)
  expect(api.posts('/continue')).toHaveLength(0)
})

test.each([
  [404, 'Feedback is no longer available for this attempt.'],
  [502, 'Unable to generate feedback right now. You can still retry or continue.'],
  [503, 'Feedback is unavailable right now. You can still retry or continue.'],
  [504, 'Feedback took too long. You can still retry or continue.'],
] as const)('diagnosis HTTP %s stays local to feedback and does not enter mutation recovery', async (status, message) => {
  const api = mockSessionApi()
  const pending = pendingDiagnoses(api)
  const factsChanged = vi.fn()
  render(<Interview onHistoryFactsChange={factsChanged} />); await start(); await submit('Saved answer')
  const calls = api.fetchMock.mock.calls.length
  await act(async () => pending[0].resolve(response({ detail: 'PRIVATE_BACKEND_DIAGNOSTIC_TEXT' }, status)))
  await within(feedback()).findByText(message)
  expectNoDrill()
  expectReviewActionsEnabled()
  expect(screen.getByText('Saved answer', { exact: true })).toBeTruthy()
  expect(screen.getByRole('heading', { name: 'Attempt 1' })).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Recheck saved state' })).toBeNull()
  expect(screen.queryByText(/result is unknown|review could not be loaded|PRIVATE_BACKEND/)).toBeNull()
  expect(api.fetchMock.mock.calls).toHaveLength(calls)
  expect(api.posts('/diagnosis')).toHaveLength(1)
  expect(api.posts('/attempts')).toHaveLength(1)
  expect(api.session().current_question_latest_attempt_number).toBe(1)
  expect(factsChanged).toHaveBeenCalledTimes(2)
})

test.each(['network', 'malformed'] as const)('a %s diagnosis failure preserves successful attempt review and controls', async (failure) => {
  const api = mockSessionApi()
  const pending = pendingDiagnoses(api)
  render(<Interview />); await start(); await submit('Saved answer')
  const calls = api.fetchMock.mock.calls.length
  await act(async () => {
    if (failure === 'network') pending[0].reject(new TypeError('PRIVATE_EXCEPTION_TEXT'))
    else pending[0].resolve(response({ ...diagnosis, next_focus: 'PRIVATE_INVALID_ENUM' }))
  })
  const expected = failure === 'network'
    ? 'Unable to load feedback right now. You can still retry or continue.'
    : 'Unable to generate feedback right now. You can still retry or continue.'
  await within(feedback()).findByText(expected)
  expectNoDrill()
  expectReviewActionsEnabled()
  expect(screen.getByText('Saved answer', { exact: true })).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Recheck saved state' })).toBeNull()
  expect(document.body.textContent).not.toContain('PRIVATE_')
  expect(api.fetchMock.mock.calls).toHaveLength(calls)
})

test('semantic completion does not change the authoritative speaking/delivery comparison or history facts', async () => {
  const api = mockSessionApi()
  api.append('Before')
  const speaking = { recognized_word_count: metric(12, 20), um_count: metric(0, 0), uh_count: metric(1, 0),
    timed_utterance_span_seconds: { ...metric(2.2, 3.1), delta: 0.9000000000000001 },
    estimated_words_per_minute: { ...metric(150.02, 148.99), delta: -1.0300000000000011 } }
  const zeroPause: DeliveryMetricChange = { ...metric(0, 0), before_unavailable_reason: null, after_unavailable_reason: null }
  const delivery = { before_version: 'delivery-metrics-v1', after_version: 'delivery-metrics-v1',
    before_source: 'original_transcription', after_source: 'original_transcription',
    pause_count: zeroPause, total_pause_duration_seconds: zeroPause, longest_pause_seconds: zeroPause }
  const pending = pendingDiagnoses(api)
  const factsChanged = vi.fn()
  sessionStorage.setItem(storageKey, api.session().id)
  render(<Interview onHistoryFactsChange={factsChanged} />)
  await screen.findByRole('button', { name: 'Continue' })
  api.setComparison({ session_id: api.session().id, question_index: 0,
    before_attempt: { id: api.saved()[0].id, attempt_number: 1, measurement_id: null, measurement_version: null, measurement_source: null },
    after_attempt: { id: 'attempt-0-2', attempt_number: 2, measurement_id: null, measurement_version: null, measurement_source: null },
    comparison: speaking, delivery_comparison: delivery })
  await retry(); await submit('After')
  await screen.findByRole('table', { name: 'Speaking duration is shown in seconds.' })
  const tableBefore = screen.getAllByRole('table').map((table) => table.textContent)
  const storedBefore = JSON.stringify(api.comparison(0))
  const calls = api.fetchMock.mock.calls.length
  const historyChanges = factsChanged.mock.calls.length
  await act(async () => pending[0].resolve(response(diagnosis)))
  await within(feedback()).findByText(diagnosis.retry_instruction)
  expectDrillForFocus(diagnosis.next_focus)
  expect(screen.getAllByRole('table').map((table) => table.textContent)).toEqual(tableBefore)
  expect(JSON.stringify(api.comparison(0))).toBe(storedBefore)
  expect(comparisonRow('Recognized words')).toEqual(['12', '20', '+8'])
  expect(comparisonRow('Words per minute')).toEqual(['150.0', '149.0', '-1.0'])
  expect(api.fetchMock.mock.calls).toHaveLength(calls)
  expect(factsChanged).toHaveBeenCalledTimes(historyChanges)
})

test('empty semantic lists are not fabricated and returned text is rendered as text', async () => {
  const api = mockSessionApi()
  const text = '<img src=x onerror=alert(1)>'
  api.intercept((url) => url.endsWith('/diagnosis') ? response({ ...diagnosis, addressed_question: 'no',
    strengths: [], missing_information: [], structure: 'insufficient_content', next_focus: 'answer_the_question',
    retry_instruction: text }) : undefined)
  render(<Interview />); await start(); await submit('Saved answer')
  await within(feedback()).findByText(text, { exact: true })
  expect(within(feedback()).queryByText('Strengths', { exact: true })).toBeNull()
  expect(within(feedback()).queryByText('Missing information', { exact: true })).toBeNull()
  expect(within(feedback()).getByText('No', { exact: true })).toBeTruthy()
  expect(within(feedback()).getByText('Not enough content', { exact: true })).toBeTruthy()
  expect(within(feedback()).getByText('Answer the question', { exact: true })).toBeTruthy()
  expect(within(feedback()).queryByRole('img')).toBeNull()
})

test.each(['rejected', 'conflict', 'network', 'malformed acknowledgement', 'review failure'] as const)(
  '%s submission and its recovery reads do not request diagnosis', async (failure) => {
    const api = mockSessionApi()
    let failWrite = true
    let failRead = failure === 'review failure'
    api.intercept((url, options) => {
      if (failWrite && url.endsWith('/attempts') && options?.method === 'POST') {
        failWrite = false
        if (failure === 'rejected') return response({ detail: 'PRIVATE_VALIDATION_MESSAGE' }, 422)
        if (failure === 'conflict') {
          api.append('Saved in another tab')
          return response({ detail: 'PRIVATE_CONFLICT_MESSAGE' }, 409)
        }
        if (failure === 'network') { api.append('Saved answer'); return Promise.reject(new TypeError('Private transport text')) }
        if (failure === 'malformed acknowledgement') { api.append('Saved answer'); return response({ unexpected: true }, 201) }
        const attempt = api.append('Saved answer')
        return response({ attempt, session: api.session() }, 201)
      }
      if (failRead && url === '/api/sessions/session-1' && options?.method !== 'POST') {
        failRead = false
        return Promise.reject(new TypeError('Private review failure'))
      }
      return undefined
    })
    render(<Interview />); await start()
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Saved answer' } })
    fireEvent.click(screen.getByRole('button', { name: 'Submit Attempt' }))
    await screen.findByRole('alert')
    expect(api.posts('/diagnosis')).toHaveLength(0)
    const recheck = screen.queryByRole('button', { name: 'Recheck saved state' })
    if (recheck) {
      fireEvent.click(recheck)
      await waitFor(expectReviewActionsEnabled)
      expect(api.posts('/diagnosis')).toHaveLength(0)
    }
    expect(api.posts('/attempts')).toHaveLength(1)
    expect(screen.queryByRole('region', { name: 'Answer feedback' })).toBeNull()
    expectNoDrill()
  },
)

test('restored review, ordinary rerender and Cancel Retry never trigger diagnosis', async () => {
  const api = mockSessionApi()
  api.append('Restored persisted answer')
  sessionStorage.setItem(storageKey, api.session().id)
  const component = render(<Interview />)
  await screen.findByRole('button', { name: 'Continue' })
  await waitFor(expectReviewActionsEnabled)
  expectNoDrill()
  component.rerender(<Interview onHistoryFactsChange={vi.fn()} />)
  expectNoDrill()
  await retry()
  fireEvent.click(screen.getByRole('button', { name: 'Cancel Retry' }))
  await waitFor(expectReviewActionsEnabled)
  expect(api.posts('/diagnosis')).toHaveLength(0)
  expect(api.posts('/attempts')).toHaveLength(0)
  expect(api.posts('/continue')).toHaveLength(0)
  expect(screen.queryByRole('region', { name: 'Answer feedback' })).toBeNull()
  expectNoDrill()
})

test.each(['success', 'error'] as const)('late diagnosis %s stays invalidated through Retry and Cancel Retry', async (settlement) => {
  const api = mockSessionApi()
  const pending = pendingDiagnoses(api)
  render(<Interview />); await start(); await submit('Saved answer')
  const calls = api.fetchMock.mock.calls.length
  await retry()
  expect(pending[0].signal.aborted).toBe(true)
  expect(screen.queryByRole('region', { name: 'Answer feedback' })).toBeNull()
  expectNoDrill()
  fireEvent.click(screen.getByRole('button', { name: 'Cancel Retry' }))
  await waitFor(expectReviewActionsEnabled)
  await act(async () => {
    if (settlement === 'success') pending[0].resolve(response({ ...diagnosis, retry_instruction: 'STALE_RETRY_FEEDBACK' }))
    else pending[0].reject(new TypeError('STALE_RETRY_FAILURE'))
  })
  expect(screen.queryByRole('region', { name: 'Answer feedback' })).toBeNull()
  expect(screen.queryByRole('alert')).toBeNull()
  expectNoDrill()
  expect(api.fetchMock.mock.calls).toHaveLength(calls)
  expect(api.posts('/diagnosis')).toHaveLength(1)
  expect(api.posts('/attempts')).toHaveLength(1)
  expect(api.posts('/continue')).toHaveLength(0)
})

test.each([
  ['success', 'pending Continue'], ['error', 'pending Continue'],
  ['success', 'changed question'], ['error', 'changed question'],
] as const)('late diagnosis %s is ignored with %s', async (settlement, stage) => {
  const api = mockSessionApi()
  const diagnosisResponse = deferredResponse()
  const continueResponse = deferredResponse()
  let diagnosisSignal!: AbortSignal
  api.intercept((url, options) => {
    if (url.endsWith('/diagnosis')) { diagnosisSignal = options?.signal as AbortSignal; return diagnosisResponse.promise }
    if (url.endsWith('/continue') && options?.method === 'POST') return continueResponse.promise
    return undefined
  })
  render(<Interview />); await start(); await submit('Saved answer')
  fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
  expect(diagnosisSignal.aborted).toBe(true)
  expect(api.posts('/continue')).toHaveLength(1)
  expect(screen.getByText('Question 1 of 5')).toBeTruthy()
  expect(screen.queryByRole('region', { name: 'Answer feedback' })).toBeNull()
  expectNoDrill()
  if (stage === 'changed question') {
    await act(async () => continueResponse.resolve(response(api.advance())))
    await screen.findByText('Question 2 of 5')
  }
  await act(async () => {
    if (settlement === 'success') diagnosisResponse.resolve(response({ ...diagnosis, retry_instruction: 'STALE_CONTINUE_FEEDBACK' }))
    else diagnosisResponse.reject(new TypeError('STALE_CONTINUE_FAILURE'))
  })
  expect(screen.queryByRole('region', { name: 'Answer feedback' })).toBeNull()
  expect(screen.queryByRole('alert')).toBeNull()
  expectNoDrill()
  if (stage === 'pending Continue') {
    await act(async () => continueResponse.resolve(response(api.advance())))
    await screen.findByText('Question 2 of 5')
  }
  expect(screen.queryByRole('region', { name: 'Answer feedback' })).toBeNull()
  expect(api.posts('/diagnosis')).toHaveLength(1)
  expectNoDrill()
})

test.each(['success', 'error'] as const)('old diagnosis %s/final completion cannot replace a newer request or clear its loading state', async (settlement) => {
  const api = mockSessionApi()
  const pending = pendingDiagnoses(api)
  render(<Interview />); await start(); await submit('First saved answer')
  await retry(); await submit('Second saved answer')
  expect(pending).toHaveLength(2)
  expect(pending[0].signal.aborted).toBe(true)
  expect(pending[1].signal.aborted).toBe(false)
  await act(async () => {
    if (settlement === 'success') pending[0].resolve(response({ ...diagnosis, next_focus: 'specificity', retry_instruction: 'STALE_OLDER_FEEDBACK' }))
    else pending[0].reject(new TypeError('STALE_OLDER_FAILURE'))
  })
  expect(within(feedback()).getByRole('status').textContent).toBe('Generating answer feedback…')
  expect(feedback().getAttribute('aria-busy')).toBe('true')
  expectNoDrill()
  expect(screen.queryByRole('alert')).toBeNull()
  expect(document.body.textContent).not.toContain('STALE_OLDER')
  await act(async () => pending[1].resolve(response({ ...diagnosis, next_focus: 'conciseness', retry_instruction: 'CURRENT_SECOND_FEEDBACK' })))
  await within(feedback()).findByText('CURRENT_SECOND_FEEDBACK')
  expectDrillForFocus('conciseness')
  expect(screen.queryByRole('heading', { name: personalizedDrillForFocus('specificity').title })).toBeNull()
  expect(screen.queryByText('Generating answer feedback…')).toBeNull()
  expect(api.posts('/diagnosis').map(([url]) => url)).toEqual([
    '/api/sessions/session-1/questions/0/attempts/1/diagnosis',
    '/api/sessions/session-1/questions/0/attempts/2/diagnosis',
  ])
  expect(api.posts('/attempts')).toHaveLength(2)
  expectReviewActionsEnabled()
})

test.each(['success', 'error'] as const)('an old diagnosis %s cannot replace completed newer feedback', async (settlement) => {
  const api = mockSessionApi()
  const pending = pendingDiagnoses(api)
  render(<Interview />); await start(); await submit('First saved answer')
  await retry(); await submit('Second saved answer')
  await act(async () => pending[1].resolve(response({ ...diagnosis, next_focus: 'conciseness', retry_instruction: 'CURRENT_COMPLETED_FEEDBACK' })))
  await within(feedback()).findByText('CURRENT_COMPLETED_FEEDBACK')
  const currentDrill = expectDrillForFocus('conciseness')
  await act(async () => {
    if (settlement === 'success') pending[0].resolve(response({ ...diagnosis, next_focus: 'specificity', retry_instruction: 'STALE_COMPLETED_FEEDBACK' }))
    else pending[0].reject(new TypeError('STALE_COMPLETED_FAILURE'))
  })
  expect(within(feedback()).getByText('CURRENT_COMPLETED_FEEDBACK')).toBeTruthy()
  expect(expectDrillForFocus('conciseness')).toBe(currentDrill)
  expect(screen.queryByRole('heading', { name: personalizedDrillForFocus('specificity').title })).toBeNull()
  expect(screen.queryByRole('alert')).toBeNull()
  expect(document.body.textContent).not.toContain('STALE_COMPLETED')
})

test.each(['success', 'error'] as const)('late diagnosis %s from a previous session is ignored after the reachable restart flow', async (settlement) => {
  const api = mockSessionApi()
  const diagnosisResponse = deferredResponse()
  let diagnosisSignal!: AbortSignal
  api.intercept((url, options) => {
    if (url.endsWith('/diagnosis')) { diagnosisSignal = options?.signal as AbortSignal; return diagnosisResponse.promise }
    if (url.endsWith('/continue') && options?.method === 'POST') return response({ detail: 'Private unavailable text' }, 503)
    return undefined
  })
  render(<Interview />); await start(); await submit('Previous session answer')
  // Start New Interview is offered by the existing recovery UI after a failed Continue.
  fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
  await screen.findByRole('button', { name: 'Start New Interview' })
  fireEvent.click(screen.getByRole('button', { name: 'Start New Interview' }))
  await screen.findByRole('textbox', { name: 'Your answer' })
  await waitFor(() => expect((screen.getByRole('button', { name: 'Submit Attempt' }) as HTMLButtonElement).disabled).toBe(true))
  expect(api.session().id).toBe('session-2')
  expect(diagnosisSignal.aborted).toBe(true)
  await act(async () => {
    if (settlement === 'success') diagnosisResponse.resolve(response({ ...diagnosis, retry_instruction: 'STALE_SESSION_FEEDBACK' }))
    else diagnosisResponse.reject(new TypeError('STALE_SESSION_FAILURE'))
  })
  expect(screen.queryByRole('region', { name: 'Answer feedback' })).toBeNull()
  expect(screen.queryByRole('alert')).toBeNull()
  expect(screen.queryByText('Previous session answer', { exact: true })).toBeNull()
  expectNoDrill()
  expect(api.posts('/diagnosis')).toHaveLength(1)
  expect(api.creations()).toBe(2)
  expect(api.posts('/attempts')).toHaveLength(1)
})

test.each(['success', 'error'] as const)('late diagnosis %s is ignored after unmount and a fresh mounted session', async (settlement) => {
  const api = mockSessionApi()
  const pending = pendingDiagnoses(api)
  const original = render(<Interview />); await start(); await submit('Saved before unmount')
  original.unmount()
  expect(pending[0].signal.aborted).toBe(true)
  sessionStorage.clear()
  render(<Interview />); await start()
  await act(async () => {
    if (settlement === 'success') pending[0].resolve(response({ ...diagnosis, retry_instruction: 'STALE_UNMOUNT_FEEDBACK' }))
    else pending[0].reject(new TypeError('STALE_UNMOUNT_FAILURE'))
  })
  expect(screen.queryByRole('region', { name: 'Answer feedback' })).toBeNull()
  expect(screen.queryByRole('alert')).toBeNull()
  expect(document.body.textContent).not.toContain('STALE_UNMOUNT')
  expectNoDrill()
  expect(api.posts('/diagnosis')).toHaveLength(1)
  expect(api.creations()).toBe(2)
})

function finalQuestionSession(): InterviewSession {
  return { ...freshSession(summarySessionId), current_question_index: 4, current_question: questions[4],
    answers: ['Saved one', 'Saved two', 'Saved three', 'Saved four'] }
}

function completedSummarySession(): InterviewSession {
  return { ...freshSession(summarySessionId), status: 'completed', current_question_index: 5, current_question: null,
    answers: ['Saved one', 'Saved two', 'Saved three', 'Saved four', 'Saved five'] }
}

function summaryFact(label: string) {
  const summary = screen.getByRole('region', { name: 'Interview summary' })
  return within(summary).getByText(label, { selector: 'dt', exact: true }).nextElementSibling?.textContent
}

async function restoreFinalReview(api: ReturnType<typeof mockSessionApi>) {
  await restore(api)
  await waitFor(() => expect((screen.getByRole('button', { name: 'Continue' }) as HTMLButtonElement).disabled).toBe(false))
}

test('an incomplete restored interview never requests or displays a completed summary', async () => {
  const api = mockSessionApi(freshSession(summarySessionId))
  await restore(api)
  await screen.findByRole('textbox', { name: 'Your answer' })
  expect(screen.queryByRole('region', { name: 'Interview summary' })).toBeNull()
  expect(historyApi.getHistoryDetail).not.toHaveBeenCalled()
  expect(api.gets('/history-detail')).toHaveLength(0)
  expect(api.posts('/attempts')).toHaveLength(0)
  expect(api.posts('/continue')).toHaveLength(0)
})

test('a newly submitted fifth answer remains provisional without any summary read until Continue', async () => {
  const api = mockSessionApi(finalQuestionSession())
  await restore(api)
  await submit('Provisional final answer')
  await screen.findByRole('region', { name: 'Answer feedback' })
  expect(api.session().status).toBe('active')
  expect(screen.queryByRole('heading', { name: 'Interview Complete' })).toBeNull()
  expect(screen.queryByRole('region', { name: 'Interview summary' })).toBeNull()
  expect(historyApi.getHistoryDetail).not.toHaveBeenCalled()
  expect(api.gets('/history-detail')).toHaveLength(0)
  expect(api.posts('/attempts')).toHaveLength(1)
  expect(api.posts('/continue')).toHaveLength(0)
})

test.each([
  { saved: 1, finalNumber: 1, retries: 0 },
  { saved: 3, finalNumber: 12, retries: 2 },
])('successful final Continue loads one persisted summary with $retries row-count retries', async ({ saved, finalNumber, retries }) => {
  const api = mockSessionApi(finalQuestionSession())
  for (let index = 0; index < saved; index += 1) {
    api.append(`Saved final attempt ${index + 1}`, null, index === saved - 1 ? finalNumber : index + 1)
  }
  await restoreFinalReview(api)
  expect(historyApi.getHistoryDetail).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
  await screen.findByRole('heading', { name: 'Interview Complete' })
  await waitFor(() => expect(summaryFact('Questions completed')).toBe('5 / 5'))
  expect(summaryFact('Total attempts')).toBe(String(5 + retries))
  expect(summaryFact('Total retries')).toBe(String(retries))
  const summary = screen.getByRole('region', { name: 'Interview summary' })
  expect(within(summary).getAllByRole('heading', { level: 4 }).map((heading) => heading.textContent))
    .toEqual(['Question 1', 'Question 2', 'Question 3', 'Question 4', 'Question 5'])
  const finalRow = within(summary).getByRole('article', { name: 'Summary for Question 5' })
  expect(within(finalRow).getByText('Final attempt', { selector: 'dt' }).nextElementSibling?.textContent).toBe(String(finalNumber))
  expect(within(finalRow).getByText('Retries', { selector: 'dt' }).nextElementSibling?.textContent).toBe(String(retries))
  expect(within(summary).getAllByText('Speaking measurements: Unavailable — No measurement')).toHaveLength(5)
  expect(historyApi.getHistoryDetail).toHaveBeenCalledTimes(1)
  expect(api.gets('/history-detail')).toHaveLength(1)
  const [url, options] = api.gets('/history-detail')[0]
  expect(url).toBe(`/api/sessions/${summarySessionId}/history-detail`)
  expect(options?.body).toBeUndefined()
  expect(options?.cache).toBe('no-store')
  expect(api.posts('/continue')).toHaveLength(1)
  expect(postedBody(api, '/continue').expected_last_attempt_number).toBe(finalNumber)
  expect(api.posts('/attempts')).toHaveLength(0)
  expect(api.posts('/diagnosis')).toHaveLength(0)
  expectNoDrill()
  expect(summary.textContent).not.toContain('Saved final attempt')
})

test('restoring a completed interview loads one read-only summary and rerender does not refetch it', async () => {
  const api = mockSessionApi(completedSummarySession())
  sessionStorage.setItem(storageKey, summarySessionId)
  const sessionWrites = vi.spyOn(Storage.prototype, 'setItem')
  const view = render(<Interview />)
  await screen.findByRole('heading', { name: 'Interview Complete' })
  await waitFor(() => expect(summaryFact('Total attempts')).toBe('5'))
  view.rerender(<Interview />)
  expect(historyApi.getHistoryDetail).toHaveBeenCalledTimes(1)
  expect(api.gets('/history-detail')).toHaveLength(1)
  expect(api.gets('/attempts')).toHaveLength(0)
  expect(api.gets('/comparison')).toHaveLength(0)
  expect(api.fetchMock.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(0)
  for (const [key, value] of sessionWrites.mock.calls) {
    expect(key).toBe(storageKey)
    expect(value).toBe(summarySessionId)
  }
  expect(sessionStorage.getItem(storageKey)).toBe(summarySessionId)
  expect(JSON.stringify(sessionStorage)).not.toContain('Questions completed')
  expectNoDrill()
})

test.each([400, 503])('failed final Continue (%i) never starts a summary read', async (status) => {
  const api = mockSessionApi(finalQuestionSession())
  api.append('Saved final answer')
  await restoreFinalReview(api)
  api.intercept((url, options) => url.endsWith('/continue') && options?.method === 'POST'
    ? response({ detail: 'PRIVATE_CONTINUE_ERROR' }, status) : undefined)
  fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
  await screen.findByRole('alert')
  expect(api.session().status).toBe('active')
  expect(screen.queryByRole('heading', { name: 'Interview Complete' })).toBeNull()
  expect(screen.queryByRole('region', { name: 'Interview summary' })).toBeNull()
  expect(historyApi.getHistoryDetail).not.toHaveBeenCalled()
  expect(api.gets('/history-detail')).toHaveLength(0)
  expect(api.posts('/continue')).toHaveLength(1)
  expect(api.posts('/attempts')).toHaveLength(0)
})

test('an acknowledged final Continue waits for coherent recovery before reading the summary', async () => {
  const api = mockSessionApi(finalQuestionSession())
  api.append('Saved final answer')
  await restoreFinalReview(api)
  let acknowledged = false
  let failRead = true
  api.intercept((url, options) => {
    if (!acknowledged && url.endsWith('/continue') && options?.method === 'POST') {
      acknowledged = true
      return response(api.advance())
    }
    if (acknowledged && failRead && url === `/api/sessions/${summarySessionId}` && options?.method !== 'POST') {
      failRead = false
      return Promise.reject(new TypeError('Reconciliation read unavailable'))
    }
    return undefined
  })
  fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
  await screen.findByRole('button', { name: 'Recheck saved state' })
  expect(screen.getByRole('heading', { name: 'Interview Complete' })).toBeTruthy()
  expect(screen.queryByRole('region', { name: 'Interview summary' })).toBeNull()
  expect(historyApi.getHistoryDetail).not.toHaveBeenCalled()
  expect(api.gets('/history-detail')).toHaveLength(0)
  fireEvent.click(screen.getByRole('button', { name: 'Recheck saved state' }))
  await waitFor(() => expect(summaryFact('Questions completed')).toBe('5 / 5'))
  expect(api.gets('/history-detail')).toHaveLength(1)
  expect(api.posts('/continue')).toHaveLength(1)
  expect(api.posts('/attempts')).toHaveLength(0)
})

test('a failed completed-session refresh performs no summary read until saved state is rechecked', async () => {
  const api = mockSessionApi(completedSummarySession())
  api.intercept((url) => url === `/api/sessions/${summarySessionId}`
    ? Promise.reject(new TypeError('Restore read unavailable')) : undefined)
  sessionStorage.setItem(storageKey, summarySessionId)
  render(<Interview />)
  await screen.findByRole('button', { name: 'Recheck saved state' })
  expect(screen.queryByRole('region', { name: 'Interview summary' })).toBeNull()
  expect(historyApi.getHistoryDetail).not.toHaveBeenCalled()
  expect(api.gets('/history-detail')).toHaveLength(0)
  api.intercept(undefined)
  fireEvent.click(screen.getByRole('button', { name: 'Recheck saved state' }))
  await waitFor(() => expect(summaryFact('Questions completed')).toBe('5 / 5'))
  expect(api.gets('/history-detail')).toHaveLength(1)
  expect(api.fetchMock.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(0)
})

test('summary read failure preserves completion, never replays writes, and leaves restart available', async () => {
  const api = mockSessionApi(finalQuestionSession())
  api.append('Saved final answer')
  await restoreFinalReview(api)
  api.intercept((url) => url.endsWith('/history-detail')
    ? response({ detail: 'PRIVATE_SUMMARY_BACKEND_BODY' }, 503) : undefined)
  fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
  const summary = await screen.findByRole('region', { name: 'Interview summary' })
  expect((await within(summary).findByRole('alert')).textContent).toBe('Interview summary is unavailable.')
  expect(document.body.textContent).not.toContain('PRIVATE_SUMMARY_BACKEND_BODY')
  expect(screen.getByRole('heading', { name: 'Interview Complete' })).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Recheck saved state' })).toBeNull()
  expect((screen.getByRole('button', { name: 'Start New Interview' }) as HTMLButtonElement).disabled).toBe(false)
  expect(api.posts('/continue')).toHaveLength(1)
  expect(api.posts('/attempts')).toHaveLength(0)
  expect(api.posts('/diagnosis')).toHaveLength(0)
  expect(api.gets('/history-detail')).toHaveLength(1)
  expect(sessionStorage.getItem(storageKey)).toBe(summarySessionId)
  fireEvent.click(screen.getByRole('button', { name: 'Start New Interview' }))
  await screen.findByText('Question 1 of 5')
  expect(screen.queryByRole('region', { name: 'Interview summary' })).toBeNull()
  expect(screen.queryByText('Interview summary is unavailable.')).toBeNull()
  expect(api.posts('/continue')).toHaveLength(1)
})

test.each(['success', 'error'] as const)('restart immediately aborts and ignores old summary %s while session creation is pending', async (settlement) => {
  const api = mockSessionApi(completedSummarySession())
  const pending = deferredResponse()
  const creation = deferredResponse()
  let summarySignal!: AbortSignal
  api.intercept((url, options) => {
    if (url.endsWith('/history-detail')) {
      summarySignal = options!.signal as AbortSignal
      // Deliberately settle after abort to exercise ownership as well as transport cancellation.
      return pending.promise
    }
    if (url === '/api/sessions' && options?.method === 'POST') return creation.promise
    return undefined
  })
  sessionStorage.setItem(storageKey, summarySessionId)
  const view = render(<Interview />)
  await screen.findByText('Loading interview summary…')
  expect((screen.getByRole('button', { name: 'Start New Interview' }) as HTMLButtonElement).disabled).toBe(false)
  view.rerender(<Interview />)
  expect(api.gets('/history-detail')).toHaveLength(1)
  fireEvent.click(screen.getByRole('button', { name: 'Start New Interview' }))
  expect(summarySignal.aborted).toBe(true)
  expect(screen.queryByRole('region', { name: 'Interview summary' })).toBeNull()
  expect((screen.getByRole('button', { name: 'Start New Interview' }) as HTMLButtonElement).disabled).toBe(true)
  const oldDetail = completedHistoryDetail()
  oldDetail.questions[0].question_text = 'STALE_SUMMARY_SUCCESS'
  await act(async () => {
    if (settlement === 'success') pending.resolve(response(oldDetail))
    else pending.reject(new TypeError('STALE_SUMMARY_FAILURE'))
  })
  expect(document.body.textContent).not.toContain('STALE_SUMMARY')
  expect(screen.queryByText('Interview summary is unavailable.')).toBeNull()
  expect((screen.getByRole('button', { name: 'Start New Interview' }) as HTMLButtonElement).disabled).toBe(true)
  const next = freshSession(nextSummarySessionId)
  api.setSession(next)
  await act(async () => creation.resolve(response(next, 201)))
  await screen.findByText('Question 1 of 5')
  expect(sessionStorage.getItem(storageKey)).toBe(nextSummarySessionId)
  expect(screen.queryByRole('region', { name: 'Interview summary' })).toBeNull()
  expect(document.body.textContent).not.toContain('STALE_SUMMARY')
  expect(api.gets('/history-detail')).toHaveLength(1)
  expect(api.posts('/attempts')).toHaveLength(0)
  expect(api.posts('/continue')).toHaveLength(0)
})

test.each(['success', 'error'] as const)('unmount aborts the summary and ignores its late %s in a fresh Practice mount', async (settlement) => {
  const api = mockSessionApi(completedSummarySession())
  const pending = deferredResponse()
  let summarySignal!: AbortSignal
  api.intercept((url, options) => {
    if (!url.endsWith('/history-detail')) return undefined
    summarySignal = options!.signal as AbortSignal
    return pending.promise
  })
  sessionStorage.setItem(storageKey, summarySessionId)
  const original = render(<Interview />)
  await screen.findByText('Loading interview summary…')
  original.unmount()
  expect(summarySignal.aborted).toBe(true)
  sessionStorage.clear()
  render(<Interview />)
  await screen.findByRole('button', { name: 'Start Interview' })
  await act(async () => {
    if (settlement === 'success') pending.resolve(response(completedHistoryDetail()))
    else pending.reject(new TypeError('STALE_UNMOUNT_SUMMARY'))
  })
  expect(screen.queryByRole('region', { name: 'Interview summary' })).toBeNull()
  expect(screen.queryByRole('alert')).toBeNull()
  expect(document.body.textContent).not.toContain('STALE_UNMOUNT_SUMMARY')
  expect(api.gets('/history-detail')).toHaveLength(1)
  expect(api.fetchMock.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(0)
})


const roleplayUnavailable = {
  detail: 'Interviewer is unavailable right now. Try Continue again.',
  code: 'roleplay_generation_unavailable', write_outcome: 'not_applied',
}
function adaptiveSession(): InterviewSession {
  return { ...freshSession(), question_engine: 'live-ai-roleplay-v1', questions: questions.slice(0, 1) }
}

test('adaptive questions are prepared only by Continue and remain a persisted prefix through completion', async () => {
  const api = mockSessionApi(adaptiveSession())
  render(<Interview />); await start()
  expect(screen.getByText('Question 1 of 5')).toBeTruthy()
  expect(api.session().questions).toEqual(questions.slice(0, 1))
  await submit('First attempt'); await retry(); await submit('Selected final answer')
  expect(api.posts('/continue')).toHaveLength(0)
  const pending = deferredResponse()
  api.intercept((url, options) => url.endsWith('/continue') && options?.method === 'POST' ? pending.promise : undefined)
  const button = screen.getByRole('button', { name: 'Continue' })
  fireEvent.click(button); fireEvent.click(button)
  expect(screen.getByText('Preparing next question…')).toBeTruthy()
  expect((screen.getByRole('button', { name: 'Retry Again' }) as HTMLButtonElement).disabled).toBe(true)
  expect((button as HTMLButtonElement).disabled).toBe(true)
  expect(api.posts('/continue')).toHaveLength(1)
  expect(screen.getByText('Question 1 of 5')).toBeTruthy()
  api.intercept(() => undefined)
  await act(async () => pending.resolve(response(api.advance())))
  await screen.findByText('Question 2 of 5')
  expect(api.session().questions).toEqual(questions.slice(0, 2))
  for (let index = 1; index < 5; index += 1) {
    await submit(`Answer ${index + 1}`)
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    if (index < 4) await screen.findByText(`Question ${index + 2} of 5`)
  }
  await screen.findByRole('heading', { name: 'Interview Complete' })
  expect(api.session().questions).toEqual(questions)
  expect(api.posts('/continue')).toHaveLength(5)
  expect(api.posts('/diagnosis')).toHaveLength(6)
})

test('known no-commit roleplay failure preserves saved review and allows an explicit Continue retry', async () => {
  const api = mockSessionApi(adaptiveSession())
  render(<Interview />); await start(); await submit('Saved answer')
  api.intercept((url, options) => url.endsWith('/continue') && options?.method === 'POST' ? response(roleplayUnavailable, 503) : undefined)
  fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
  await screen.findByText(roleplayUnavailable.detail)
  expect(screen.getByText('Saved answer')).toBeTruthy()
  expect(screen.getByText('Question 1 of 5')).toBeTruthy()
  expect(api.session().questions).toHaveLength(1)
  expect(screen.queryByRole('button', { name: 'Recheck saved state' })).toBeNull()
  expect((screen.getByRole('button', { name: 'Continue' }) as HTMLButtonElement).disabled).toBe(false)
  expect((screen.getByRole('button', { name: 'Retry' }) as HTMLButtonElement).disabled).toBe(false)
  expect(api.posts('/continue')).toHaveLength(1)
  await retry()
  fireEvent.click(screen.getByRole('button', { name: 'Cancel Retry' }))
  api.intercept(() => undefined)
  fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
  await screen.findByText('Question 2 of 5')
  expect(api.posts('/continue')).toHaveLength(2)
})

test.each([false, true])('adaptive lost Continue response reconciles without replay; committed=%s', async (committed) => {
  const api = mockSessionApi(adaptiveSession())
  render(<Interview />); await start(); await submit('Saved answer')
  api.intercept((url, options) => {
    if (url.endsWith('/continue') && options?.method === 'POST') {
      if (committed) api.advance()
      return Promise.reject(new TypeError('Response lost'))
    }
  })
  fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
  await screen.findByRole('button', { name: 'Recheck saved state' })
  expect((screen.getByRole('button', { name: 'Continue' }) as HTMLButtonElement).disabled).toBe(true)
  fireEvent.click(screen.getByRole('button', { name: 'Recheck saved state' }))
  await screen.findByText(committed ? 'Question 2 of 5' : 'Saved state rechecked. Choose your next action; no request was resubmitted.')
  expect(api.posts('/continue')).toHaveLength(1)
  expect(api.session().questions).toHaveLength(committed ? 2 : 1)
})

test('unmount cancels pending adaptive Continue and ignores its late result', async () => {
  const api = mockSessionApi(adaptiveSession())
  const mounted = render(<Interview />); await start(); await submit('Saved answer')
  const pending = deferredResponse()
  api.intercept((url, options) => url.endsWith('/continue') && options?.method === 'POST' ? pending.promise : undefined)
  fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
  const signal = api.posts('/continue')[0][1]?.signal
  expect(signal?.aborted).toBe(false)
  mounted.unmount()
  expect(signal?.aborted).toBe(true)
  await act(async () => pending.resolve(response(api.advance())))
  expect(screen.queryByText('Question 2 of 5')).toBeNull()
})

test('restoring an adaptive session reads its generated prefix without continuing or requesting diagnosis', async () => {
  const api = mockSessionApi(adaptiveSession())
  api.append('Finalized first'); api.advance(); api.append('Saved current')
  await restore(api)
  await screen.findByText('Question 2 of 5')
  expect(screen.getByText('Saved current')).toBeTruthy()
  expect(screen.queryByText(questions[2])).toBeNull()
  expect(api.posts('/continue')).toHaveLength(0)
  expect(api.posts('/diagnosis')).toHaveLength(0)
})

class QuestionAudio {
  static instances: QuestionAudio[] = []
  src: string
  currentTime = 0
  onended: (() => void) | null = null
  onerror: (() => void) | null = null
  play = vi.fn().mockResolvedValue(undefined)
  pause = vi.fn()
  removeAttribute(name: string) { if (name === 'src') this.src = '' }
  constructor(url: string) { this.src = url; QuestionAudio.instances.push(this) }
}
function stubQuestionAudio() {
  QuestionAudio.instances = []
  const createURL = vi.fn().mockReturnValue('blob:question')
  const revokeURL = vi.fn()
  const NativeURL = URL
  vi.stubGlobal('URL', class extends NativeURL { static createObjectURL = createURL; static revokeObjectURL = revokeURL })
  vi.stubGlobal('Audio', QuestionAudio)
  return { createURL, revokeURL }
}
function speechResponse() { return new Response(new Uint8Array([0xff, 0xfb, 0x90, 0x64]), { headers: { 'Content-Type': 'audio/mpeg' } }) }
async function playQuestion() {
  fireEvent.click(screen.getByRole('button', { name: 'Play question' }))
  await waitFor(() => expect(QuestionAudio.instances.at(-1)?.play).toHaveBeenCalledOnce())
}

test.each(['deterministic-v1', 'live-ai-roleplay-v1'] as const)(
  '%s synthesis is explicit and Replay creates no attempt, diagnosis, advancement, or storage write', async (engine) => {
    const api = mockSessionApi(engine === 'live-ai-roleplay-v1' ? adaptiveSession() : freshSession())
    const { revokeURL } = stubQuestionAudio()
    api.intercept((url, options) => {
      if (!url.endsWith('/speech')) return
      expect(url).toBe('/api/sessions/session-1/questions/0/speech')
      expect(options?.body).toBeUndefined()
      return speechResponse()
    })
    const { unmount } = render(<Interview />); await start()
    expect(api.posts('/speech')).toHaveLength(0)
    const writes = api.fetchMock.mock.calls.filter(([, options]) => options?.method === 'POST').length
    const storage = vi.spyOn(Storage.prototype, 'setItem')
    await playQuestion()
    act(() => QuestionAudio.instances[0].onended?.())
    fireEvent.click(screen.getByRole('button', { name: 'Replay question' }))
    await waitFor(() => expect(QuestionAudio.instances[0].play).toHaveBeenCalledTimes(2))
    expect(api.posts('/speech')).toHaveLength(1)
    expect(api.fetchMock.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(writes + 1)
    expect(api.posts('/attempts')).toHaveLength(0)
    expect(api.posts('/continue')).toHaveLength(0)
    expect(api.posts('/diagnosis')).toHaveLength(0)
    expect(storage).not.toHaveBeenCalled()
    expect(api.session().current_question_index).toBe(0)
    expect(api.session().current_question_latest_attempt_number).toBe(0)
    unmount()
    expect(revokeURL).toHaveBeenCalledExactlyOnceWith('blob:question')
  },
)

test.each(['Submit Attempt', 'Retry', 'Cancel Retry', 'Continue'])(
  '%s immediately disposes voice through its existing interview lifecycle without automatic synthesis', async (action) => {
    const api = mockSessionApi()
    const { revokeURL } = stubQuestionAudio()
    api.intercept((url) => url.endsWith('/speech') ? speechResponse() : undefined)
    render(<Interview />); await start()
    if (action !== 'Submit Attempt') await submit('Original saved answer')
    if (action === 'Cancel Retry') await retry()
    if (action === 'Submit Attempt') fireEvent.change(screen.getByRole('textbox', { name: 'Your answer' }), { target: { value: 'Saved answer' } })
    await playQuestion()
    fireEvent.click(screen.getByRole('button', { name: action }))
    // Disposal occurs in the click handler before persistence/reconciliation settles.
    expect(QuestionAudio.instances[0].pause).toHaveBeenCalledOnce()
    expect(revokeURL).toHaveBeenCalledExactlyOnceWith('blob:question')
    if (action === 'Submit Attempt') await screen.findByRole('button', { name: 'Continue' })
    if (action === 'Retry') await screen.findByRole('textbox', { name: 'Your answer' })
    if (action === 'Cancel Retry') await screen.findByRole('button', { name: 'Continue' })
    if (action === 'Continue') await screen.findByText('Question 2 of 5')
    expect(api.posts('/speech')).toHaveLength(1)
    expect(api.posts('/attempts')).toHaveLength(1)
    expect(api.posts('/continue')).toHaveLength(action === 'Continue' ? 1 : 0)
  },
)

test('Continue aborts pending speech immediately; an ignored-abort response cannot play the previous question', async () => {
  const api = mockSessionApi(adaptiveSession())
  const { createURL } = stubQuestionAudio()
  const pending = deferredResponse()
  api.intercept((url) => url.endsWith('/speech') ? pending.promise : undefined)
  render(<Interview />); await start(); await submit('Saved answer')
  fireEvent.click(screen.getByRole('button', { name: 'Play question' }))
  const signal = api.posts('/speech')[0][1]?.signal
  expect((screen.getByRole('button', { name: 'Continue' }) as HTMLButtonElement).disabled).toBe(false)
  fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
  expect(signal?.aborted).toBe(true)
  await screen.findByText('Question 2 of 5')
  await act(async () => pending.resolve(speechResponse()))
  expect(createURL).not.toHaveBeenCalled()
  expect(QuestionAudio.instances).toHaveLength(0)
  expect(api.posts('/speech')).toHaveLength(1)
  expect(api.posts('/continue')).toHaveLength(1)
})

test('Cancel Retry never restores an old voice clip and does not synthesize a replacement', async () => {
  const api = mockSessionApi()
  const { revokeURL } = stubQuestionAudio()
  api.intercept((url) => url.endsWith('/speech') ? speechResponse() : undefined)
  render(<Interview />); await start(); await submit('Saved answer'); await playQuestion()
  await retry()
  fireEvent.click(screen.getByRole('button', { name: 'Cancel Retry' }))
  expect(screen.getByRole('button', { name: 'Play question' })).toBeDefined()
  expect(screen.queryByRole('button', { name: 'Replay question' })).toBeNull()
  expect(revokeURL).toHaveBeenCalledOnce()
  expect(api.posts('/speech')).toHaveLength(1)
  expect(api.posts('/attempts')).toHaveLength(1)
  expect(api.posts('/diagnosis')).toHaveLength(1)
})

test.each([502, 503, 504])('voice %i is a non-mutation failure; Retry and Continue remain usable', async (status) => {
  const api = mockSessionApi()
  api.intercept((url) => url.endsWith('/speech') ? response({ detail: 'Voice playback is unavailable right now.' }, status) : undefined)
  render(<Interview />); await start(); await submit('Saved answer')
  fireEvent.click(screen.getByRole('button', { name: 'Play question' }))
  await screen.findByText('Voice playback is unavailable right now.')
  expect(screen.queryByText('Authentication is temporarily unavailable. Please try again.')).toBeNull()
  expect(screen.queryByRole('button', { name: 'Recheck saved state' })).toBeNull()
  expect((screen.getByRole('button', { name: 'Retry' }) as HTMLButtonElement).disabled).toBe(false)
  expect((screen.getByRole('button', { name: 'Continue' }) as HTMLButtonElement).disabled).toBe(false)
  expect(api.posts('/speech')).toHaveLength(1)
  expect(api.posts('/attempts')).toHaveLength(1)
  expect(api.posts('/continue')).toHaveLength(0)
})

test('restored saved review exposes Play without synthesizing or requesting diagnosis', async () => {
  const api = mockSessionApi()
  api.append('Saved current answer')
  await restore(api)
  await screen.findByRole('button', { name: 'Play question' })
  expect(api.posts('/speech')).toHaveLength(0)
  expect(api.posts('/diagnosis')).toHaveLength(0)
})

test('session restart disposes old voice immediately without synthesizing for the new session', async () => {
  const api = mockSessionApi()
  const { revokeURL } = stubQuestionAudio()
  api.intercept((url, options) => {
    if (url.endsWith('/speech')) return speechResponse()
    if (url.endsWith('/attempts') && options?.method === 'POST') return response({ detail: 'Cannot submit this answer.' }, 400)
  })
  render(<Interview />); await start()
  fireEvent.change(screen.getByRole('textbox', { name: 'Your answer' }), { target: { value: 'Unaccepted answer' } })
  fireEvent.click(screen.getByRole('button', { name: 'Submit Attempt' }))
  const restart = await screen.findByRole('button', { name: 'Start New Interview' })
  await playQuestion()
  fireEvent.click(restart)
  expect(QuestionAudio.instances[0].pause).toHaveBeenCalledOnce()
  expect(revokeURL).toHaveBeenCalledOnce()
  await waitFor(() => expect(api.creations()).toBe(2))
  await screen.findByRole('textbox', { name: 'Your answer' })
  expect(api.posts('/speech')).toHaveLength(1)
  expect(api.session().id).toBe('session-2')
  expect(screen.queryByRole('button', { name: 'Replay question' })).toBeNull()
})
