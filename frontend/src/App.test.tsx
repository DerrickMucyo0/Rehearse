// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import App from './App'
import { AUTH_UNAVAILABLE_MESSAGE, getAuthState } from './auth'
import type { Attempt, InterviewSession, SemanticDiagnosis, SpeakingMetrics } from './interviewApi'
import { personalizedDrillForFocus } from './personalizedDrills'

const SESSION_ID = '11111111-1111-4111-8111-111111111111'
const OTHER_ID = '22222222-2222-4222-8222-222222222222'
const MEASUREMENT_ID = '33333333-3333-4333-8333-333333333333'
const HISTORY_PREFIX = 'rehearse.history.v1:'
const USER_ID = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
const PRACTICE_KEY = `rehearse.session_id:${USER_ID}`
const questions = ['First interview question', 'Second interview question', 'Third interview question', 'Fourth interview question', 'Fifth interview question']
const submittedAt = '2026-10-05T12:00:00Z'
const metrics: SpeakingMetrics = { source: 'original_transcription', recognized_word_count: 3, um_count: 0, uh_count: 0,
  filler_unavailable_reason: null, timed_utterance_span_seconds: 1.5, estimated_words_per_minute: 120, timing_unavailable_reason: null }
const deliveryMetrics = { version: 'pause-metrics-v1', source: 'original_transcription', pause_count: 2,
  total_pause_duration_seconds: 1.5, longest_pause_seconds: 0.8, unavailable_reason: null }
const semanticDiagnosis: SemanticDiagnosis = {
  diagnosis_version: 'semantic-diagnosis-v1',
  addressed_question: 'yes', addressed_question_reason: 'The answer addresses the immediate question.',
  strengths: ['The example is concrete.'], missing_information: [], structure: 'clear',
  structure_feedback: 'The actions and result are easy to follow.', next_focus: 'maintain_strengths',
  next_focus_reason: 'Keep the concrete example.', retry_instruction: 'Keep the clear account in your next attempt.',
}
function response(value: unknown, status = 200) { return new Response(JSON.stringify(value), { status }) }
function initialSession(id = SESSION_ID): InterviewSession {
  return { id, status: 'active', current_question_index: 0, current_question: questions[0],
    current_question_latest_attempt_number: 0, questions, answers: [] }
}
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}

class Recorder {
  static instances: Recorder[] = []
  static manualStop = false
  static isTypeSupported(type: string) { return type === 'audio/webm;codecs=opus' }
  state = 'inactive'
  mimeType: string
  ondataavailable: ((event: { data: Blob }) => void) | null = null
  onstop: (() => void) | null = null
  onerror: (() => void) | null = null
  constructor(_stream: MediaStream, options?: MediaRecorderOptions) {
    this.mimeType = options?.mimeType ?? 'audio/webm'
    Recorder.instances.push(this)
  }
  start() { this.state = 'recording' }
  stop() {
    this.state = 'inactive'
    if (!Recorder.manualStop) queueMicrotask(() => this.finish())
  }
  finish() {
    this.ondataavailable?.({ data: new Blob(['local synthetic audio'], { type: this.mimeType }) })
    this.onstop?.()
  }
}
let getUserMedia: ReturnType<typeof vi.fn>
let stopTrack: ReturnType<typeof vi.fn>
let media: MediaStream
beforeEach(() => {
  localStorage.clear(); sessionStorage.clear()
  Recorder.instances = []; Recorder.manualStop = false
  stopTrack = vi.fn()
  media = { getTracks: () => [{ stop: stopTrack }] } as unknown as MediaStream
  getUserMedia = vi.fn().mockResolvedValue(media)
  vi.stubGlobal('navigator', { mediaDevices: { getUserMedia } })
  vi.stubGlobal('MediaRecorder', Recorder)
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Unmocked network is forbidden')))
})
afterEach(() => {
  cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals()
  localStorage.clear(); sessionStorage.clear()
})

function mockAppApi(initial = initialSession()) {
  let session = initial
  let creations = 0
  const attempts = new Map<number, Attempt[]>()
  const known = new Map<string, InterviewSession>()
  let intercept: ((url: string, options?: RequestInit) => Response | Promise<Response> | undefined) | undefined
  const saved = (questionIndex = session.current_question_index) => attempts.get(questionIndex) ?? []
  function append(answer: string, measurementId: string | null = null) {
    const attempt: Attempt = { id: `44444444-4444-4444-8444-${String(session.current_question_index * 100 + session.current_question_latest_attempt_number + 1).padStart(12, '0')}`,
      question_index: session.current_question_index, attempt_number: session.current_question_latest_attempt_number + 1,
      answer, submitted_at: submittedAt, measurement_id: measurementId }
    attempts.set(session.current_question_index, [...saved(), attempt])
    session = { ...session, current_question_latest_attempt_number: attempt.attempt_number }
    known.set(session.id, session)
    return attempt
  }
  function advance() {
    const answer = saved().at(-1)?.answer ?? ''
    const next = session.current_question_index + 1
    session = { ...session, current_question_index: next, current_question: questions[next] ?? null,
      current_question_latest_attempt_number: 0, answers: [...session.answers, answer],
      status: next === questions.length ? 'completed' : 'active' }
    known.set(session.id, session)
    return session
  }
  function summary(forSession: InterviewSession) {
    const list = forSession.id === session.id ? [...attempts.values()].flat() : []
    const points = Array.from({ length: forSession.current_question_index }, (_, questionIndex) => {
      const attempt = saved(questionIndex).at(-1)
      return { question_index: questionIndex, attempt_id: attempt?.id ?? '55555555-5555-4555-8555-555555555555',
        attempt_number: attempt?.attempt_number ?? 1, submitted_at: submittedAt, measurement: null }
    })
    return { session_id: forSession.id, status: forSession.status, created_at: submittedAt,
      completed_at: forSession.status === 'completed' ? submittedAt : null,
      current_question_number: forSession.status === 'active' ? forSession.current_question_index + 1 : null,
      total_questions: questions.length, finalized_question_count: forSession.current_question_index,
      questions_practiced_count: new Set(list.map((attempt) => attempt.question_index)).size,
      total_attempt_count: list.length, total_retry_count: list.length - new Set(list.map((attempt) => attempt.question_index)).size,
      measured_final_answer_count: 0, last_submitted_at: list.length ? submittedAt : null,
      last_saved_activity_at: submittedAt, finalized_points: points }
  }
  function detail(questionIndex: number | null) {
    return { summary: summary(session), questions: questions.map((question_text, index) => {
      const latest = saved(index).at(-1)
      const finalized = index < session.current_question_index
      return { question_index: index, question_text, finalized, attempt_count: saved(index).length,
        latest_attempt_id: latest?.id ?? null, latest_attempt_number: latest?.attempt_number ?? null,
        final_attempt_id: finalized ? latest?.id ?? null : null, final_attempt_number: finalized ? latest?.attempt_number ?? null : null }
    }), selected_question: questionIndex === null ? null : { question_index: questionIndex,
      attempts: saved(questionIndex).map((attempt) => ({ attempt_id: attempt.id, attempt_number: attempt.attempt_number,
        answer_text: attempt.answer, submitted_at: attempt.submitted_at,
        is_final: questionIndex < session.current_question_index && attempt === saved(questionIndex).at(-1), measurement: null })),
      has_more: false, next_after_attempt_number: null } }
  }
  const fetchMock = vi.fn(async (input: RequestInfo | URL, options?: RequestInit): Promise<Response> => {
    const url = String(input)
    if (url !== '/api/health' && url !== '/api/auth/me') {
      expect(new Headers(options?.headers).get('X-Rehearse-Auth-Context')).toBe('context-A')
    }
    const override = intercept?.(url, options)
    if (override) return await override
    if (url === '/api/health') return response({ status: 'ok', service: 'rehearse-api' })
    if (url === '/api/auth/me') return response({ user_id: USER_ID, request_context: 'context-A' })
    if (url === '/api/auth/logout') return new Response(null, { status: 204 })
    if (url === '/api/sessions' && options?.method === 'POST') {
      creations += 1
      session = initialSession()
      known.set(session.id, session)
      return response(session, 201)
    }
    if (url === '/api/history/summaries') {
      expect(options?.method).toBe('GET')
      expect(options?.body).toBeUndefined()
      return response({ items: [...known.values()].map(summary), next_cursor: null })
    }
    if (url.startsWith(`/api/sessions/${session.id}/history-detail`)) {
      const query = new URL(url, 'http://localhost').searchParams
      return response(detail(query.has('question_index') ? Number(query.get('question_index')) : null))
    }
    const diagnosisMatch = url.match(/^\/api\/sessions\/([^/]+)\/questions\/(\d+)\/attempts\/(\d+)\/diagnosis$/)
    if (diagnosisMatch) {
      expect(diagnosisMatch[1]).toBe(session.id)
      expect(options?.method).toBe('POST')
      expect(options?.body).toBeUndefined()
      expect(saved(Number(diagnosisMatch[2])).some((attempt) => attempt.attempt_number === Number(diagnosisMatch[3]))).toBe(true)
      return response(semanticDiagnosis)
    }
    const match = url.match(/\/questions\/(\d+)\/(attempts|continue|comparison)$/)
    if (match) {
      const index = Number(match[1])
      if (match[2] === 'attempts' && options?.method === 'POST') {
        const body = JSON.parse(options.body as string) as { expected_last_attempt_number: number; answer: string; measurement_id: string | null }
        expect(body.expected_last_attempt_number).toBe(session.current_question_latest_attempt_number)
        const attempt = append(body.answer, body.measurement_id)
        return response({ attempt, session }, 201)
      }
      if (match[2] === 'continue' && options?.method === 'POST') {
        const body = JSON.parse(options.body as string) as { expected_last_attempt_number: number }
        expect(body.expected_last_attempt_number).toBe(session.current_question_latest_attempt_number)
        return response(advance())
      }
      if (match[2] === 'attempts') return response(saved(index))
      if (match[2] === 'comparison') {
        const list = saved(index)
        const identity = (attempt: Attempt) => ({ id: attempt.id, attempt_number: attempt.attempt_number,
          measurement_id: attempt.measurement_id, measurement_version: null, measurement_source: null })
        const unavailable = { before: null, after: null, delta: null, before_unavailable_reason: 'no_measurement',
          after_unavailable_reason: 'no_measurement', comparable: false, comparison_unavailable_reason: 'both_unavailable' }
        return response({ session_id: session.id, question_index: index, before_attempt: list[0] ? identity(list[0]) : null,
          after_attempt: list.length > 1 ? identity(list.at(-1)!) : null, comparison: list.length > 1 ? {
            recognized_word_count: unavailable, um_count: unavailable, uh_count: unavailable,
            timed_utterance_span_seconds: unavailable, estimated_words_per_minute: unavailable } : null,
          delivery_comparison: list.length > 1 ? { before_version: null, after_version: null, before_source: null, after_source: null,
            pause_count: unavailable, total_pause_duration_seconds: unavailable, longest_pause_seconds: unavailable } : null })
      }
    }
    if (url.endsWith('/transcriptions') && options?.method === 'POST') {
      return response({ session_id: session.id, question_index: session.current_question_index,
        measurement_id: MEASUREMENT_ID, text: 'Original recorded words', language: 'eng', words: [], metrics, delivery_metrics: deliveryMetrics })
    }
    if (url.endsWith('/audio') && options?.method === 'POST') {
      return response({ session_id: session.id, question_index: session.current_question_index,
        status: 'accepted', filename: 'answer.webm', content_type: 'audio/webm;codecs=opus', size_bytes: 21 })
    }
    if (url === `/api/sessions/${session.id}`) return response(session)
    throw new Error(`Unexpected mocked endpoint ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  return { fetchMock, append, advance, saved, detail, session: () => session, creations: () => creations,
    addServerSession: (other: InterviewSession) => known.set(other.id, other),
    intercept: (next: typeof intercept) => { intercept = next },
    posts: (suffix: string) => fetchMock.mock.calls.filter(([url, options]) => String(url).endsWith(suffix) && options?.method === 'POST') }
}
function nav(name: 'Practice' | 'History' | 'Progress') {
  return within(screen.getByRole('navigation')).getByRole('button', { name })
}
function go(name: 'Practice' | 'History' | 'Progress') { fireEvent.click(nav(name)) }
function assertNavigationBlocked() {
  expect((nav('History') as HTMLButtonElement).disabled).toBe(true)
  expect((nav('Progress') as HTMLButtonElement).disabled).toBe(true)
  fireEvent.click(nav('History'))
  expect(nav('Practice').getAttribute('aria-current')).toBe('page')
}
async function start() {
  fireEvent.click(await screen.findByRole('button', { name: 'Start Interview' }))
  await screen.findByRole('textbox', { name: 'Your answer' })
}
async function submit(text: string) {
  fireEvent.change(screen.getByRole('textbox', { name: 'Your answer' }), { target: { value: text } })
  fireEvent.click(screen.getByRole('button', { name: 'Submit Attempt' }))
  await screen.findByRole('button', { name: 'Continue' })
  await waitFor(() => expect((screen.getByRole('button', { name: 'Continue' }) as HTMLButtonElement).disabled).toBe(false))
}
async function record() {
  fireEvent.click(screen.getByRole('button', { name: 'Record Answer' }))
  await screen.findByRole('button', { name: 'Stop Recording' })
}
async function finishRecording() {
  fireEvent.click(screen.getByRole('button', { name: 'Stop Recording' }))
  await screen.findByText('Recording stopped. Ready to send.')
}
function rememberedKeys() { return Object.keys(localStorage).filter((key) => key.startsWith(HISTORY_PREFIX)).sort() }

test('unconfigured diagnosis renders only feedback unavailability and preserves authenticated review', async () => {
  const api = mockAppApi()
  api.intercept((url) => url.endsWith('/diagnosis')
    ? response({ detail: 'Semantic diagnosis is not configured.' }, 503) : undefined)
  render(<App />); await start(); await submit('Saved first attempt')
  const feedback = await screen.findByRole('region', { name: 'Answer feedback' })
  await within(feedback).findByText('Feedback is unavailable right now. You can still retry or continue.')
  expect(screen.queryByText(AUTH_UNAVAILABLE_MESSAGE)).toBeNull()
  expect(document.body.textContent).not.toContain('Semantic diagnosis is not configured.')
  expect(screen.getByRole('button', { name: 'Logout' })).toBeTruthy()
  expect((screen.getByRole('button', { name: 'Retry' }) as HTMLButtonElement).disabled).toBe(false)
  expect((screen.getByRole('button', { name: 'Continue' }) as HTMLButtonElement).disabled).toBe(false)
  expect(getAuthState()).toMatchObject({ status: 'authenticated', notice: null })
  expect(api.posts('/diagnosis')).toHaveLength(1)
  expect(api.posts('/attempts')).toHaveLength(1)
  expect(api.posts('/continue')).toHaveLength(0)
})

test('navigation preserves the same idle typed editor while displaying objective Progress', async () => {
  mockAppApi(); render(<App />); await start()
  const editor = screen.getByRole('textbox')
  fireEvent.change(editor, { target: { value: 'Safe unsaved answer' } })
  go('History')
  await screen.findByRole('heading', { name: 'History' })
  expect(screen.queryByRole('textbox')).toBeNull()
  go('Progress')
  expect(await screen.findByText('Objective practice history from your saved sessions.')).toBeTruthy()
  expect(nav('Progress').getAttribute('aria-current')).toBe('page')
  go('Practice')
  expect(screen.getByRole('textbox')).toBe(editor)
  expect((editor as HTMLTextAreaElement).value).toBe('Safe unsaved answer')
  expect(nav('Practice').getAttribute('aria-current')).toBe('page')
})

test('History navigation preserves review and retry draft without creating or resubmitting attempts', async () => {
  const api = mockAppApi(); render(<App />); await start(); await submit('Saved first attempt')
  go('History'); await screen.findByRole('heading', { name: 'History' }); go('Practice')
  expect(screen.getByRole('heading', { name: 'Attempt 1' })).toBeTruthy()
  expect(screen.queryByRole('textbox')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Safe retry draft' } })
  const retryEditor = screen.getByRole('textbox')
  go('History'); await screen.findByRole('heading', { name: 'History' }); go('Practice')
  expect(screen.getByRole('textbox')).toBe(retryEditor)
  expect((retryEditor as HTMLTextAreaElement).value).toBe('Safe retry draft')
  expect(screen.getByText('Saved first attempt')).toBeTruthy()
  expect(api.posts('/attempts')).toHaveLength(1)
  expect(api.creations()).toBe(1)
})

test.each([
  ['success', 'History'], ['error', 'History'],
  ['success', 'Progress'], ['error', 'Progress'],
] as const)('pending and %s semantic feedback preserve navigation, mounted review, and existing mutations while hidden in %s', async (result, hiddenView) => {
  const api = mockAppApi()
  const pending = deferred<Response>()
  const expectedDrill = personalizedDrillForFocus(semanticDiagnosis.next_focus)
  api.intercept((url) => url === `/api/sessions/${SESSION_ID}/questions/0/attempts/1/diagnosis` ? pending.promise : undefined)
  const view = render(<App />); await start(); await submit('Persisted feedback navigation answer')
  await screen.findByText('Generating answer feedback…')
  expect(screen.queryByRole('region', { name: 'Practice drill' })).toBeNull()
  const attemptHeading = screen.getByRole('heading', { name: 'Attempt 1' })
  const signal = api.posts('/diagnosis')[0][1]?.signal
  const setItem = vi.spyOn(Storage.prototype, 'setItem')
  const removeItem = vi.spyOn(Storage.prototype, 'removeItem')
  const clear = vi.spyOn(Storage.prototype, 'clear')
  expect((nav('History') as HTMLButtonElement).disabled).toBe(false)
  expect((nav('Progress') as HTMLButtonElement).disabled).toBe(false)
  go('History'); await screen.findByRole('heading', { name: 'History' })
  go('Progress'); await screen.findByText('Objective practice history from your saved sessions.')
  await within(screen.getByRole('region', { name: 'Progress' })).findByText('Saved attempts', { exact: true })
  if (hiddenView === 'History') { go('History'); await screen.findByRole('heading', { name: 'History' }) }
  const hiddenPanel = screen.getByRole('region', { name: hiddenView === 'History' ? 'Session history' : 'Progress' })
  const beforePanel = hiddenPanel.innerHTML
  const beforeRequests = api.fetchMock.mock.calls.length
  const beforeFacts = JSON.stringify(api.detail(0))
  const beforeLocalStorage = JSON.stringify(Object.entries(localStorage))
  const beforeSessionStorage = JSON.stringify(Object.entries(sessionStorage))
  expect(screen.queryByRole('region', { name: 'Practice drill' })).toBeNull()
  expect(signal?.aborted).toBe(false)
  expect(api.posts('/attempts')).toHaveLength(1)
  expect(api.posts('/continue')).toHaveLength(0)
  expect(api.posts('/diagnosis')).toHaveLength(1)
  await act(async () => pending.resolve(result === 'success'
    ? response(semanticDiagnosis) : response({ detail: 'PRIVATE_FEEDBACK_SERVER_DETAIL' }, 503)))
  expect(hiddenPanel.innerHTML).toBe(beforePanel)
  expect(screen.queryByRole('region', { name: 'Practice drill' })).toBeNull()
  expect(api.fetchMock.mock.calls).toHaveLength(beforeRequests)
  expect(JSON.stringify(api.detail(0))).toBe(beforeFacts)
  go('Practice')
  expect(screen.getByRole('heading', { name: 'Attempt 1' })).toBe(attemptHeading)
  expect(screen.getByText('Persisted feedback navigation answer')).toBeTruthy()
  const feedback = screen.getByRole('region', { name: 'Answer feedback' })
  if (result === 'success') {
    await within(feedback).findByText(semanticDiagnosis.addressed_question_reason)
    expect(screen.getAllByRole('region', { name: 'Practice drill' })).toHaveLength(1)
    const drill = within(feedback).getByRole('region', { name: 'Practice drill' })
    expect(within(drill).getByRole('heading', { name: expectedDrill.title, level: 4 })).toBeTruthy()
    expect(within(drill).getByText(expectedDrill.goal, { exact: true })).toBeTruthy()
    const steps = within(drill).getByRole('list')
    expect(steps.tagName).toBe('OL')
    expect(within(steps).getAllByRole('listitem').map((step) => step.textContent)).toEqual(expectedDrill.steps)
    expect(drill.textContent).not.toContain(expectedDrill.drill_version)
    expect(drill.textContent).not.toContain(expectedDrill.focus)
  } else {
    expect((await within(feedback).findByRole('alert')).textContent).toBe('Feedback is unavailable right now. You can still retry or continue.')
    expect(screen.queryByRole('region', { name: 'Practice drill' })).toBeNull()
  }
  view.rerender(<App />)
  expect(api.fetchMock.mock.calls).toHaveLength(beforeRequests)
  expect(JSON.stringify(api.detail(0))).toBe(beforeFacts)
  expect(JSON.stringify(Object.entries(localStorage))).toBe(beforeLocalStorage)
  expect(JSON.stringify(Object.entries(sessionStorage))).toBe(beforeSessionStorage)
  expect(setItem).not.toHaveBeenCalled()
  expect(removeItem).not.toHaveBeenCalled()
  expect(clear).not.toHaveBeenCalled()
  expect(document.body.textContent).not.toContain('PRIVATE_FEEDBACK_SERVER_DETAIL')
  expect(screen.queryByRole('button', { name: 'Recheck saved state' })).toBeNull()
  expect((screen.getByRole('button', { name: 'Retry' }) as HTMLButtonElement).disabled).toBe(false)
  expect((screen.getByRole('button', { name: 'Continue' }) as HTMLButtonElement).disabled).toBe(false)
  expect((nav('History') as HTMLButtonElement).disabled).toBe(false)
  expect((nav('Progress') as HTMLButtonElement).disabled).toBe(false)
  go('History'); await screen.findByRole('heading', { name: 'History' })
  go('Progress'); await screen.findByText('Objective practice history from your saved sessions.')
  go('Practice')
  expect(screen.getByRole('heading', { name: 'Attempt 1' })).toBe(attemptHeading)
  expect(api.fetchMock.mock.calls).toHaveLength(beforeRequests)
  expect(screen.queryAllByRole('region', { name: 'Practice drill' })).toHaveLength(result === 'success' ? 1 : 0)
  expect(setItem).not.toHaveBeenCalled()
  expect(removeItem).not.toHaveBeenCalled()
  expect(clear).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
  await screen.findByText('Question 2 of 5')
  expect(screen.queryByRole('region', { name: 'Practice drill' })).toBeNull()
  await waitFor(() => expect((nav('History') as HTMLButtonElement).disabled).toBe(false))
  go('History'); await screen.findByRole('heading', { name: 'History' }); go('Practice')
  expect(api.posts('/attempts')).toHaveLength(1)
  expect(api.posts('/continue')).toHaveLength(1)
  expect(api.posts('/diagnosis')).toHaveLength(1)
  expect(api.creations()).toBe(1)
})

test.each([
  ['success', 'History'], ['error', 'History'],
  ['success', 'Progress'], ['error', 'Progress'],
] as const)('completed summary %s preserves mounted Practice and existing facts while hidden in %s', async (result, hiddenView) => {
  const api = mockAppApi()
  const pending = deferred<Response>()
  const summaryPath = `/api/sessions/${SESSION_ID}/history-detail`
  const summaryReads = () => api.fetchMock.mock.calls.filter(([url]) => String(url).includes('/history-detail'))
  api.intercept((url) => url === summaryPath ? pending.promise : undefined)
  const view = render(<App />); await start()
  expect(screen.queryByRole('region', { name: 'Interview summary' })).toBeNull()
  for (let index = 0; index < questions.length; index += 1) {
    await submit(`PRIVATE_SUMMARY_ANSWER_${index}`)
    if (index === 0) {
      for (const answer of ['PRIVATE_SUMMARY_RETRY_ONE', 'PRIVATE_SUMMARY_RETRY_TWO']) {
        fireEvent.click(screen.getByRole('button', { name: /^Retry(?: Again)?$/ }))
        await submit(answer)
      }
    }
    expect(screen.queryByRole('region', { name: 'Interview summary' })).toBeNull()
    expect(summaryReads()).toHaveLength(0)
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    if (index + 1 < questions.length) {
      await screen.findByText(`Question ${index + 2} of 5`)
      await waitFor(() => expect((nav('History') as HTMLButtonElement).disabled).toBe(false))
    }
  }
  await screen.findByRole('heading', { name: 'Interview Complete' })
  await waitFor(() => expect(summaryReads()).toHaveLength(1))
  const [path, options] = summaryReads()[0]
  expect(path).toBe(summaryPath)
  expect(options?.method).toBeUndefined()
  expect(options?.body).toBeUndefined()
  expect(options?.cache).toBe('no-store')
  const signal = options?.signal
  const summary = screen.getByRole('region', { name: 'Interview summary' })
  expect(summary.getAttribute('aria-busy')).toBe('true')
  expect(within(summary).getByRole('status')).toBeTruthy()
  expect((screen.getByRole('button', { name: 'Start New Interview' }) as HTMLButtonElement).disabled).toBe(false)
  await waitFor(() => expect((nav('History') as HTMLButtonElement).disabled).toBe(false))
  go('History'); await screen.findByRole('button', { name: 'Open session' })
  go('Progress')
  await within(screen.getByRole('region', { name: 'Progress' })).findByText('Saved attempts', { exact: true })
  if (hiddenView === 'History') { go('History'); await screen.findByRole('heading', { name: 'History' }) }
  const visiblePanel = screen.getByRole('region', { name: hiddenView === 'History' ? 'Session history' : 'Progress' })
  const beforePanel = visiblePanel.innerHTML
  const beforeFacts = JSON.stringify(api.detail(null))
  const beforeAttempts = JSON.stringify(questions.map((_, index) => api.saved(index)))
  const beforeRequests = api.fetchMock.mock.calls.length
  const beforeDiagnosis = api.posts('/diagnosis').length
  const beforeLocalStorage = JSON.stringify(Object.entries(localStorage))
  const beforeSessionStorage = JSON.stringify(Object.entries(sessionStorage))
  const setItem = vi.spyOn(Storage.prototype, 'setItem')
  const removeItem = vi.spyOn(Storage.prototype, 'removeItem')
  const clear = vi.spyOn(Storage.prototype, 'clear')
  expect(screen.queryByRole('region', { name: 'Interview summary' })).toBeNull()
  expect(signal?.aborted).toBe(false)

  await act(async () => pending.resolve(result === 'success'
    ? response(api.detail(null)) : response({ detail: 'PRIVATE_SUMMARY_BACKEND_DETAIL' }, 503)))
  view.rerender(<App />)
  expect(visiblePanel.innerHTML).toBe(beforePanel)
  expect(screen.queryByRole('region', { name: 'Interview summary' })).toBeNull()
  expect(api.fetchMock.mock.calls).toHaveLength(beforeRequests)
  go('Practice')
  expect(screen.getByRole('heading', { name: 'Interview Complete' })).toBeTruthy()
  expect(screen.getByRole('region', { name: 'Interview summary' })).toBe(summary)
  expect(summary.getAttribute('aria-busy')).toBe('false')
  if (result === 'success') {
    expect(within(summary).queryByRole('alert')).toBeNull()
    expect(within(summary).getByText('Questions completed', { exact: true }).nextElementSibling?.textContent).toBe('5 / 5')
    expect(within(summary).getByText('Total attempts', { exact: true }).nextElementSibling?.textContent).toBe('7')
    expect(within(summary).getByText('Total retries', { exact: true }).nextElementSibling?.textContent).toBe('2')
    const rows = within(summary).getAllByRole('article')
    expect(rows.map((row) => within(row).getByRole('heading', { level: 4 }).textContent)).toEqual([
      'Question 1', 'Question 2', 'Question 3', 'Question 4', 'Question 5',
    ])
    rows.forEach((row, index) => {
      expect(within(row).getByText(questions[index], { exact: true })).toBeTruthy()
      expect(within(row).getByText('Final attempt', { exact: true }).nextElementSibling?.textContent).toBe(index === 0 ? '3' : '1')
      expect(within(row).getByText('Retries', { exact: true }).nextElementSibling?.textContent).toBe(index === 0 ? '2' : '0')
      expect(within(row).getByText('Speaking measurements: Unavailable — No measurement')).toBeTruthy()
      expect(within(row).getByText('Timed pauses: Unavailable — No measurement')).toBeTruthy()
    })
  } else {
    expect(within(summary).getByRole('alert').textContent).toBe('Interview summary is unavailable.')
    expect(within(summary).queryByText(questions[0], { exact: true })).toBeNull()
  }
  expect(summary.textContent).not.toMatch(/PRIVATE_SUMMARY|interview-summary-v1|summary_version|score|confidence|NVIDIA|Nemotron|ElevenLabs/i)
  expect(summary.textContent).not.toContain(SESSION_ID)
  for (const attempt of questions.flatMap((_, index) => api.saved(index))) {
    expect(summary.textContent).not.toContain(attempt.id)
  }
  expect(summary.textContent).not.toContain(semanticDiagnosis.addressed_question_reason)
  expect(summary.textContent).not.toContain(personalizedDrillForFocus(semanticDiagnosis.next_focus).title)
  expect(within(summary).queryByRole('region', { name: 'Answer feedback' })).toBeNull()
  expect(within(summary).queryByRole('region', { name: 'Practice drill' })).toBeNull()
  expect((screen.getByRole('button', { name: 'Start New Interview' }) as HTMLButtonElement).disabled).toBe(false)
  go('History'); await screen.findByRole('heading', { name: 'History' })
  go('Progress'); await screen.findByText('Objective practice history from your saved sessions.')
  go('Practice')
  view.rerender(<App />)
  expect(screen.getByRole('region', { name: 'Interview summary' })).toBe(summary)
  expect(signal?.aborted).toBe(false)
  expect(summaryReads()).toHaveLength(1)
  expect(api.fetchMock.mock.calls).toHaveLength(beforeRequests)
  expect(api.posts('/attempts')).toHaveLength(7)
  expect(api.posts('/continue')).toHaveLength(5)
  expect(api.posts('/diagnosis')).toHaveLength(beforeDiagnosis)
  expect(api.posts('/transcriptions')).toHaveLength(0)
  expect(api.creations()).toBe(1)
  expect(JSON.stringify(api.detail(null))).toBe(beforeFacts)
  expect(JSON.stringify(questions.map((_, index) => api.saved(index)))).toBe(beforeAttempts)
  expect(JSON.stringify(Object.entries(localStorage))).toBe(beforeLocalStorage)
  expect(JSON.stringify(Object.entries(sessionStorage))).toBe(beforeSessionStorage)
  expect(setItem).not.toHaveBeenCalled()
  expect(removeItem).not.toHaveBeenCalled()
  expect(clear).not.toHaveBeenCalled()
})

test('same-draft transcript edits retain measurement linkage across navigation', async () => {
  const api = mockAppApi(); render(<App />); await start(); await record(); await finishRecording()
  fireEvent.click(screen.getByRole('button', { name: 'Transcribe Recording' }))
  await screen.findByRole('region', { name: 'Speaking measurements' })
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Edited recorded answer' } })
  go('History'); await screen.findByRole('heading', { name: 'History' }); go('Practice')
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('Edited recorded answer')
  expect(screen.getByRole('region', { name: 'Speaking measurements' })).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Submit Attempt' }))
  await screen.findByRole('button', { name: 'Continue' })
  const body = JSON.parse(api.posts('/attempts')[0][1]!.body as string) as { measurement_id: string; answer: string }
  expect(body).toMatchObject({ measurement_id: MEASUREMENT_ID, answer: 'Edited recorded answer' })
  expect(rememberedKeys()).toEqual([])
  expect(localStorage.getItem(HISTORY_PREFIX + SESSION_ID)).toBeNull()
})

test('successful creation scopes only its canonical restore ID to the authenticated user and stores no Practice content', async () => {
  mockAppApi(); render(<App />); await start()
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Private unsaved draft' } })
  expect(rememberedKeys()).toEqual([])
  expect(localStorage.getItem(HISTORY_PREFIX + SESSION_ID)).toBeNull()
  expect(sessionStorage.getItem(PRACTICE_KEY)).toBe(SESSION_ID)
  expect(JSON.stringify(localStorage)).not.toMatch(/Private unsaved draft|First interview question|recognized_word_count|measurement_id/)
})

test('successful verified Practice restore uses the authenticated user key without registering History membership', async () => {
  const api = mockAppApi(); api.append('Previously saved answer')
  sessionStorage.setItem(PRACTICE_KEY, SESSION_ID)
  render(<App />)
  await screen.findByRole('button', { name: 'Continue' })
  expect(rememberedKeys()).toEqual([])
  expect(api.creations()).toBe(0)
})

test('legacy unscoped and another user restore IDs are ignored by the authenticated workspace', async () => {
  const api = mockAppApi()
  sessionStorage.setItem('rehearse.session_id', SESSION_ID)
  sessionStorage.setItem('rehearse.session_id:bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', OTHER_ID)
  render(<App />)
  await screen.findByRole('button', { name: 'Start Interview' })
  expect(api.fetchMock.mock.calls.some(([url]) => String(url).startsWith('/api/sessions/'))).toBe(false)
  expect(api.creations()).toBe(0)
  expect(sessionStorage.getItem(PRACTICE_KEY)).toBeNull()
  await start()
  expect(sessionStorage.getItem(PRACTICE_KEY)).toBe(SESSION_ID)
  expect(sessionStorage.getItem('request_context')).toBeNull()
})

test.each(['create_failure', 'malformed_creation_id', 'unknown_restore'] as const)('%s is never registered as History', async (scenario) => {
  const api = mockAppApi()
  if (scenario === 'unknown_restore') sessionStorage.setItem(PRACTICE_KEY, SESSION_ID)
  api.intercept((url, options) => {
    if (scenario === 'unknown_restore' && url === `/api/sessions/${SESSION_ID}`) return response({}, 404)
    if (url === '/api/sessions' && options?.method === 'POST') {
      if (scenario === 'create_failure') return response({}, 503)
      if (scenario === 'malformed_creation_id') return response(initialSession('not-a-valid-session-id'), 201)
    }
    return undefined
  })
  render(<App />)
  if (scenario !== 'unknown_restore') fireEvent.click(await screen.findByRole('button', { name: 'Start Interview' }))
  if (scenario !== 'malformed_creation_id') await screen.findAllByRole('alert')
  else await screen.findByRole('textbox')
  expect(rememberedKeys()).toEqual([])
})

test('legacy History storage is never written and a storage write policy cannot block Practice', async () => {
  const api = mockAppApi()
  const original = Storage.prototype.setItem
  const writes = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key, value) {
    if (this === localStorage) throw new DOMException('private quota details', 'QuotaExceededError')
    original.call(this, key, value)
  })
  render(<App />); await start()
  expect(screen.queryByText('History could not be saved in this browser.')).toBeNull()
  expect(document.body.textContent).not.toContain('private quota details')
  await submit('Still usable')
  expect(sessionStorage.getItem(PRACTICE_KEY)).toBe(SESSION_ID)
  expect(writes.mock.instances).not.toContain(localStorage)
  expect(api.posts('/attempts')).toHaveLength(1)
})

test('500 legacy browser IDs do not influence authenticated server History or block Practice', async () => {
  for (let index = 1; index <= 500; index += 1) {
    const id = `00000000-0000-4000-8000-${index.toString(16).padStart(12, '0')}`
    localStorage.setItem(HISTORY_PREFIX + id, '1')
  }
  const api = mockAppApi(); render(<App />); await start()
  expect(rememberedKeys()).toHaveLength(500)
  expect(localStorage.getItem(HISTORY_PREFIX + SESSION_ID)).toBeNull()
  expect(screen.queryByText(/History storage is full/)).toBeNull()
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Practice remains available' } })
  expect((screen.getByRole('button', { name: 'Submit Attempt' }) as HTMLButtonElement).disabled).toBe(false)
  go('History')
  await screen.findByRole('button', { name: 'Open session' })
  expect(screen.getAllByRole('button', { name: 'Open session' })).toHaveLength(1)
  expect(api.posts('/api/history/summaries')).toHaveLength(0)
})

test('reloading server History preserves the live draft and restoration ID without deleting anything', async () => {
  const api = mockAppApi(); render(<App />); await start()
  const editor = screen.getByRole('textbox')
  fireEvent.change(editor, { target: { value: 'Keep my current answer' } })
  localStorage.setItem('unrelated.preference', 'keep')
  go('History'); await screen.findByRole('button', { name: 'Open session' })
  expect(screen.queryByRole('button', { name: 'Clear remembered history' })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Reload history' }))
  await screen.findByRole('button', { name: 'Open session' })
  expect(rememberedKeys()).toEqual([])
  expect(localStorage.getItem('unrelated.preference')).toBe('keep')
  expect(sessionStorage.getItem(PRACTICE_KEY)).toBe(SESSION_ID)
  go('Practice')
  expect(screen.getByRole('textbox')).toBe(editor)
  expect((editor as HTMLTextAreaElement).value).toBe('Keep my current answer')
  expect(api.posts('/attempts')).toHaveLength(0)
  expect(api.posts('/continue')).toHaveLength(0)
  expect(api.fetchMock.mock.calls.some(([, options]) => options?.method === 'DELETE')).toBe(false)
})

test('other-tab legacy add/remove/clear storage events cannot change server History or Practice', async () => {
  const api = mockAppApi(); api.addServerSession(initialSession(OTHER_ID))
  render(<App />); await start(); go('History')
  await waitFor(() => expect(screen.getAllByRole('button', { name: 'Open session' })).toHaveLength(2))
  const reads = api.fetchMock.mock.calls.length
  localStorage.setItem(HISTORY_PREFIX + OTHER_ID, '1')
  act(() => window.dispatchEvent(new StorageEvent('storage', { key: HISTORY_PREFIX + OTHER_ID, newValue: '1', storageArea: localStorage })))
  localStorage.removeItem(HISTORY_PREFIX + OTHER_ID)
  act(() => window.dispatchEvent(new StorageEvent('storage', { key: HISTORY_PREFIX + OTHER_ID, newValue: null, storageArea: localStorage })))
  act(() => window.dispatchEvent(new StorageEvent('storage', { key: null, storageArea: localStorage })))
  expect(screen.getAllByRole('button', { name: 'Open session' })).toHaveLength(2)
  expect(api.fetchMock.mock.calls).toHaveLength(reads)
  go('Practice')
  expect(screen.getByRole('textbox')).toBeTruthy()
  expect(sessionStorage.getItem(PRACTICE_KEY)).toBe(SESSION_ID)
})

test('server History reload does not access legacy browser removal even when storage policy rejects it', async () => {
  const api = mockAppApi(); render(<App />); await start()
  const editor = screen.getByRole('textbox')
  fireEvent.change(editor, { target: { value: 'Keep this draft after a storage failure' } })
  go('History'); await screen.findByRole('button', { name: 'Open session' })
  const original = Storage.prototype.removeItem
  vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(function (this: Storage, key) {
    if (this === localStorage) throw new DOMException('private storage details', 'SecurityError')
    original.call(this, key)
  })
  fireEvent.click(screen.getByRole('button', { name: 'Reload history' }))
  await screen.findByRole('button', { name: 'Open session' })
  expect(document.body.textContent).not.toContain('private storage details')
  expect(rememberedKeys()).toEqual([])
  expect(sessionStorage.getItem(PRACTICE_KEY)).toBe(SESSION_ID)
  expect(api.posts('/api/history/summaries')).toHaveLength(0)
  go('Practice')
  expect(screen.getByRole('textbox')).toBe(editor)
  expect((editor as HTMLTextAreaElement).value).toBe('Keep this draft after a storage failure')
})

test('a pending History read can be left safely without hiding an active Practice mutation', async () => {
  const api = mockAppApi(); render(<App />); await start()
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Idle draft' } })
  api.intercept((url) => url === '/api/history/summaries' ? new Promise<Response>(() => {}) : undefined)
  go('History')
  expect((nav('Practice') as HTMLButtonElement).disabled).toBe(false)
  go('Practice')
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('Idle draft')
})

test('navigation is blocked while session creation is pending', async () => {
  const api = mockAppApi(); const pending = deferred<Response>()
  api.intercept((url) => url === '/api/sessions' ? pending.promise : undefined)
  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: 'Start Interview' }))
  assertNavigationBlocked()
  await act(async () => pending.resolve(response(initialSession(), 201)))
  await screen.findByRole('textbox')
  expect((nav('History') as HTMLButtonElement).disabled).toBe(false)
})

test('navigation is blocked during microphone permission, recording and finalization, then released safely', async () => {
  const pendingMedia = deferred<MediaStream>()
  getUserMedia.mockReturnValue(pendingMedia.promise)
  mockAppApi(); render(<App />); await start()
  fireEvent.click(screen.getByRole('button', { name: 'Record Answer' }))
  await screen.findByText('Waiting for microphone permission…')
  assertNavigationBlocked()
  await act(async () => pendingMedia.resolve(media))
  await screen.findByRole('button', { name: 'Stop Recording' })
  assertNavigationBlocked()
  Recorder.manualStop = true
  fireEvent.click(screen.getByRole('button', { name: 'Stop Recording' }))
  expect(screen.getByText('Finishing recording…')).toBeTruthy()
  assertNavigationBlocked()
  act(() => Recorder.instances[0].finish())
  await screen.findByText('Recording stopped. Ready to send.')
  expect((nav('History') as HTMLButtonElement).disabled).toBe(false)
  expect(stopTrack).toHaveBeenCalledTimes(1)
})

test.each(['audio', 'transcriptions'] as const)('navigation is blocked while %s is pending', async (operation) => {
  const api = mockAppApi(); render(<App />); await start(); await record(); await finishRecording()
  const pending = deferred<Response>()
  api.intercept((url) => url.endsWith(`/${operation}`) ? pending.promise : undefined)
  fireEvent.click(screen.getByRole('button', { name: operation === 'audio' ? 'Send Recording' : 'Transcribe Recording' }))
  assertNavigationBlocked()
  const result = operation === 'audio' ? { session_id: SESSION_ID, question_index: 0, status: 'accepted',
    filename: 'answer.webm', content_type: 'audio/webm;codecs=opus', size_bytes: 21 }
    : { session_id: SESSION_ID, question_index: 0, measurement_id: MEASUREMENT_ID,
      text: 'Original recorded words', language: 'eng', words: [], metrics, delivery_metrics: deliveryMetrics }
  await act(async () => pending.resolve(response(result)))
  await waitFor(() => expect((nav('History') as HTMLButtonElement).disabled).toBe(false))
})

test.each(['attempts', 'continue'] as const)('navigation is blocked during pending %s mutation', async (operation) => {
  const api = mockAppApi(); render(<App />); await start()
  if (operation === 'continue') await submit('Saved answer')
  else fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Draft answer' } })
  const pending = deferred<Response>()
  api.intercept((url, options) => url.endsWith(`/${operation}`) && options?.method === 'POST' ? pending.promise : undefined)
  fireEvent.click(screen.getByRole('button', { name: operation === 'attempts' ? 'Submit Attempt' : 'Continue' }))
  assertNavigationBlocked()
  const result = operation === 'attempts' ? { attempt: api.append('Draft answer'), session: api.session() } : api.advance()
  api.intercept(undefined)
  await act(async () => pending.resolve(response(result, operation === 'attempts' ? 201 : 200)))
  await waitFor(() => expect((nav('History') as HTMLButtonElement).disabled).toBe(false))
})

test('navigation remains blocked throughout conflict reconciliation', async () => {
  const api = mockAppApi(); render(<App />); await start()
  const read = deferred<Response>()
  let failed = false
  api.intercept((url, options) => {
    if (url.endsWith('/attempts') && options?.method === 'POST') {
      failed = true; api.append('Saved elsewhere')
      return response({}, 409)
    }
    if (failed && url === `/api/sessions/${SESSION_ID}`) return read.promise
    return undefined
  })
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Stale draft' } })
  fireEvent.click(screen.getByRole('button', { name: 'Submit Attempt' }))
  await waitFor(() => expect(api.fetchMock.mock.calls.some(([url, options]) =>
    String(url) === `/api/sessions/${SESSION_ID}` && options?.method !== 'POST')).toBe(true))
  assertNavigationBlocked()
  api.intercept(undefined)
  await act(async () => read.resolve(response(api.session())))
  await screen.findByText('Saved elsewhere')
  expect((nav('History') as HTMLButtonElement).disabled).toBe(false)
  expect(api.posts('/attempts')).toHaveLength(1)
})

test('ambiguous mutation and pending Recheck both prevent navigation until authoritative recovery', async () => {
  const api = mockAppApi(); render(<App />); await start()
  let unknown = true
  api.intercept((url, options) => {
    if (unknown && url.endsWith('/attempts') && options?.method === 'POST') {
      unknown = false
      return Promise.reject(new TypeError('Response lost'))
    }
    return undefined
  })
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Draft answer' } })
  fireEvent.click(screen.getByRole('button', { name: 'Submit Attempt' }))
  await screen.findByRole('button', { name: 'Recheck saved state' })
  assertNavigationBlocked()
  const read = deferred<Response>()
  api.intercept((url) => url === `/api/sessions/${SESSION_ID}` ? read.promise : undefined)
  fireEvent.click(screen.getByRole('button', { name: 'Recheck saved state' }))
  assertNavigationBlocked()
  api.intercept(undefined)
  await act(async () => read.resolve(response(api.session())))
  await waitFor(() => expect((nav('History') as HTMLButtonElement).disabled).toBe(false))
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('Draft answer')
  expect(api.posts('/attempts')).toHaveLength(1)
})

test('unavailable localStorage cannot prevent current-tab Practice creation', async () => {
  mockAppApi()
  vi.spyOn(window, 'localStorage', 'get').mockImplementation(() => {
    throw new DOMException('private browser policy details', 'SecurityError')
  })
  render(<App />); await start()
  expect(screen.queryByText('History could not be saved in this browser.')).toBeNull()
  expect(sessionStorage.getItem(PRACTICE_KEY)).toBe(SESSION_ID)
  expect(document.body.textContent).not.toContain('private browser policy details')
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).disabled).toBe(false)
})

test('History has no browser removal control and preserves the safe Practice draft and restoration ID', async () => {
  const api = mockAppApi(); render(<App />); await start()
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Keep this draft after viewing History' } })
  go('History'); await screen.findByRole('button', { name: 'Open session' })
  expect(screen.queryByRole('button', { name: 'Remove from this browser' })).toBeNull()
  expect(screen.queryByRole('button', { name: 'Clear remembered history' })).toBeNull()
  expect(localStorage.getItem(HISTORY_PREFIX + SESSION_ID)).toBeNull()
  expect(sessionStorage.getItem(PRACTICE_KEY)).toBe(SESSION_ID)
  go('Practice')
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('Keep this draft after viewing History')
  expect(rememberedKeys()).toEqual([])
  expect(api.posts('/attempts')).toHaveLength(0)
  expect(api.posts('/continue')).toHaveLength(0)
})

test('an ambiguous transcription keeps away-navigation blocked until an authoritative Recheck', async () => {
  const api = mockAppApi(); render(<App />); await start(); await record(); await finishRecording()
  api.intercept((url) => url.endsWith('/transcriptions') ? Promise.reject(new TypeError('Response lost')) : undefined)
  fireEvent.click(screen.getByRole('button', { name: 'Transcribe Recording' }))
  await screen.findByRole('button', { name: 'Recheck saved state' })
  assertNavigationBlocked()
  api.intercept(undefined)
  fireEvent.click(screen.getByRole('button', { name: 'Recheck saved state' }))
  await waitFor(() => expect((nav('History') as HTMLButtonElement).disabled).toBe(false))
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('')
  expect(api.posts('/transcriptions')).toHaveLength(1)
})

test('opening persisted finalized answers in History preserves the next-question Practice draft', async () => {
  const api = mockAppApi(); render(<App />); await start(); await submit('Persisted finalized answer')
  fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
  await screen.findByText('Question 2 of 5')
  await waitFor(() => expect((nav('History') as HTMLButtonElement).disabled).toBe(false))
  const editor = screen.getByRole('textbox')
  fireEvent.change(editor, { target: { value: 'Keep the next-question draft' } })
  const overview = deferred<Response>()
  api.intercept((url) => url === `/api/sessions/${SESSION_ID}/history-detail` ? overview.promise : undefined)
  go('History'); await screen.findByRole('button', { name: 'Open session' })
  fireEvent.click(screen.getByRole('button', { name: 'Open session' }))
  await screen.findByRole('heading', { name: 'Session detail' })
  expect(screen.getByRole('region', { name: 'Session detail' }).getAttribute('aria-busy')).toBe('true')
  expect(screen.queryByRole('button', { name: 'Question 1' })).toBeNull()
  api.intercept(undefined)
  await act(async () => overview.resolve(response(api.detail(null))))
  fireEvent.click(await screen.findByRole('button', { name: 'Question 1' }))
  await screen.findByText('Persisted finalized answer')
  expect(screen.getByText('Final', { exact: true })).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Question 1' }).getAttribute('aria-expanded')).toBe('true')
  go('Practice')
  expect(screen.getByRole('textbox')).toBe(editor)
  expect((editor as HTMLTextAreaElement).value).toBe('Keep the next-question draft')
  expect(api.posts('/attempts')).toHaveLength(1)
  expect(api.posts('/continue')).toHaveLength(1)
  expect(window.location.pathname).toBe('/')
  expect(window.location.search).toBe('')
})
