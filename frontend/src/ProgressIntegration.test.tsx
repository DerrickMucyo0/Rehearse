// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import App from './App'
import type { Attempt, InterviewSession, SemanticDiagnosis, SpeakingMetrics } from './interviewApi'

const SESSION_ID = '11111111-1111-4111-8111-111111111111'
const OTHER_ID = '22222222-2222-4222-8222-222222222222'
const MEASUREMENT_ID = '33333333-3333-4333-8333-333333333333'
const HISTORY_PREFIX = 'rehearse.history.v1:'
const PRACTICE_KEY = 'rehearse.session_id'
const questions = ['First interview question', 'Second interview question', 'Third interview question', 'Fourth interview question', 'Fifth interview question']
const submittedAt = '2026-10-05T12:00:00Z'
const metrics: SpeakingMetrics = { source: 'original_transcription', recognized_word_count: 3, um_count: 0, uh_count: 0,
  filler_unavailable_reason: null, timed_utterance_span_seconds: 1.5, estimated_words_per_minute: 120, timing_unavailable_reason: null }
const deliveryMetrics = { version: 'pause-metrics-v1', source: 'original_transcription', pause_count: 2,
  total_pause_duration_seconds: 1.5, longest_pause_seconds: 0.8, unavailable_reason: null }
const semanticDiagnosis: SemanticDiagnosis = {
  addressed_question: 'yes', addressed_question_reason: 'Semantic-only question feedback.',
  strengths: ['Semantic-only strength.'], missing_information: [], structure: 'clear',
  structure_feedback: 'Semantic-only structure feedback.', next_focus: 'maintain_strengths',
  next_focus_reason: 'Semantic-only focus reason.', retry_instruction: 'Semantic-only retry instruction.',
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
  known.set(initial.id, initial)
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
  function linkedMeasurement(attempt: Attempt | undefined) {
    return attempt?.measurement_id ? { measurement_version: 'speaking-metrics-v1', measurement_source: metrics.source,
      recognized_word_count: metrics.recognized_word_count, um_count: metrics.um_count, uh_count: metrics.uh_count,
      filler_unavailable_reason: metrics.filler_unavailable_reason, timed_utterance_span_seconds: metrics.timed_utterance_span_seconds,
      estimated_words_per_minute: metrics.estimated_words_per_minute, timing_unavailable_reason: metrics.timing_unavailable_reason,
      delivery_metrics: deliveryMetrics } : null
  }
  function summary(forSession: InterviewSession) {
    const list = forSession.id === session.id ? [...attempts.values()].flat() : []
    const points = Array.from({ length: forSession.current_question_index }, (_, questionIndex) => {
      const attempt = saved(questionIndex).at(-1)
      return { question_index: questionIndex, attempt_id: attempt?.id ?? '55555555-5555-4555-8555-555555555555',
        attempt_number: attempt?.attempt_number ?? 1, submitted_at: submittedAt, measurement: linkedMeasurement(attempt) }
    })
    return { session_id: forSession.id, status: forSession.status, created_at: submittedAt,
      completed_at: forSession.status === 'completed' ? submittedAt : null,
      current_question_number: forSession.status === 'active' ? forSession.current_question_index + 1 : null,
      total_questions: questions.length, finalized_question_count: forSession.current_question_index,
      questions_practiced_count: new Set(list.map((attempt) => attempt.question_index)).size,
      total_attempt_count: list.length, total_retry_count: list.length - new Set(list.map((attempt) => attempt.question_index)).size,
      measured_final_answer_count: points.filter((point) => point.measurement !== null).length, last_submitted_at: list.length ? submittedAt : null,
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
        is_final: questionIndex < session.current_question_index && attempt === saved(questionIndex).at(-1), measurement: linkedMeasurement(attempt) })),
      has_more: false, next_after_attempt_number: null } }
  }
  const fetchMock = vi.fn(async (input: RequestInfo | URL, options?: RequestInit): Promise<Response> => {
    const url = String(input)
    const override = intercept?.(url, options)
    if (override) return await override
    if (url === '/api/health') return response({ status: 'ok', service: 'rehearse-api' })
    if (url === '/api/sessions' && options?.method === 'POST') {
      creations += 1
      session = initialSession()
      known.set(session.id, session)
      return response(session, 201)
    }
    if (url === '/api/history/summaries' && options?.method === 'POST') {
      const body = JSON.parse(options.body as string) as { session_ids: string[] }
      return response({ summaries: body.session_ids.filter((id) => known.has(id)).map((id) => summary(known.get(id)!)),
        missing_session_ids: body.session_ids.filter((id) => !known.has(id)) })
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
    rememberKnown: (other: InterviewSession) => known.set(other.id, other),
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
  fireEvent.click(screen.getByRole('button', { name: 'Start Interview' }))
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

function progress() { return screen.getByRole('region', { name: 'Progress' }) }
function overviewValue(label: string) {
  const name = within(progress()).getByText(label, { exact: true })
  return name.parentElement!
}
function remember(id: string) { localStorage.setItem(HISTORY_PREFIX + id, '1') }
function summariesReadCount(api: ReturnType<typeof mockAppApi>) { return api.posts('/api/history/summaries').length }
async function openLoadedHistory() {
  go('History')
  await screen.findByRole('button', { name: 'Open session' })
}
async function openLoadedProgress() {
  go('Progress')
  await waitFor(() => expect(within(progress()).getByText('Saved attempts', { exact: true })).toBeTruthy())
}

test('History → Progress → History reuses one current hydration and preserves the Practice editor', async () => {
  const api = mockAppApi(); render(<App />); await start()
  const editor = screen.getByRole('textbox')
  fireEvent.change(editor, { target: { value: 'A safe unsaved Practice draft' } })
  await openLoadedHistory()
  expect(summariesReadCount(api)).toBe(1)
  await openLoadedProgress()
  expect(summariesReadCount(api)).toBe(1)
  expect(overviewValue('Active sessions').textContent).toContain('1')
  await openLoadedHistory()
  expect(summariesReadCount(api)).toBe(1)
  go('Practice')
  expect(screen.getByRole('textbox')).toBe(editor)
  expect((editor as HTMLTextAreaElement).value).toBe('A safe unsaved Practice draft')
  expect(api.posts('/attempts')).toHaveLength(0)
})

test('Progress → History → Progress reuses its current hydration without detail reads', async () => {
  const api = mockAppApi(); render(<App />); await start()
  await openLoadedProgress()
  expect(summariesReadCount(api)).toBe(1)
  await openLoadedHistory()
  await openLoadedProgress()
  expect(summariesReadCount(api)).toBe(1)
  expect(api.fetchMock.mock.calls.filter(([url]) => String(url).includes('history-detail'))).toHaveLength(0)
})

test('History → Progress reuses an in-flight hydration without cancelling or duplicating it', async () => {
  const api = mockAppApi(); render(<App />); await start()
  const pending = deferred<Response>()
  api.intercept((url) => url === '/api/history/summaries' ? pending.promise : undefined)
  go('History')
  await waitFor(() => expect(summariesReadCount(api)).toBe(1))
  const signal = api.posts('/api/history/summaries')[0][1]?.signal
  go('Progress')
  expect(summariesReadCount(api)).toBe(1)
  expect(signal?.aborted).toBe(false)
  await act(async () => pending.resolve(response({ summaries: [], missing_session_ids: [SESSION_ID] })))
  await waitFor(() => expect(overviewValue('Active sessions').textContent).toContain('0'))
  expect(summariesReadCount(api)).toBe(1)
})

test('successful session creation invalidates a previously loaded remembered history', async () => {
  const api = mockAppApi(); api.rememberKnown(initialSession(OTHER_ID)); remember(OTHER_ID)
  render(<App />); await openLoadedHistory()
  expect(summariesReadCount(api)).toBe(1)
  go('Practice'); await start()
  // Writes invalidate only; Practice does not launch a hidden history read.
  expect(summariesReadCount(api)).toBe(1)
  await openLoadedProgress()
  expect(summariesReadCount(api)).toBe(2)
  expect(overviewValue('Active sessions').textContent).toContain('2')
  expect(rememberedKeys()).toEqual([HISTORY_PREFIX + SESSION_ID, HISTORY_PREFIX + OTHER_ID])
})

test('successful Attempt submission invalidates counts without promoting the open question to a metric point', async () => {
  const api = mockAppApi(); render(<App />); await start(); await openLoadedHistory()
  go('Practice'); await submit('The current question is not yet finalized')
  expect(summariesReadCount(api)).toBe(1)
  await openLoadedProgress()
  expect(summariesReadCount(api)).toBe(2)
  expect(overviewValue('Saved attempts').textContent).toContain('1')
  expect(overviewValue('Finalized questions').textContent).toContain('0')
  expect(overviewValue('Measured final answers').textContent).toContain('0')
  go('Practice')
  expect(screen.getByRole('heading', { name: 'Attempt 1' })).toBeTruthy()
  expect(screen.getByText('The current question is not yet finalized')).toBeTruthy()
  expect(api.posts('/attempts')).toHaveLength(1)
})

test('semantic diagnosis completion preserves finalized measurements, attempt facts, and shared objective history', async () => {
  const api = mockAppApi(); render(<App />); await start(); await record(); await finishRecording()
  fireEvent.click(screen.getByRole('button', { name: 'Transcribe Recording' }))
  await screen.findByRole('region', { name: 'Speaking measurements' })
  await submit('Recorded first final answer')
  fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
  await screen.findByText('Question 2 of 5')
  await waitFor(() => expect((nav('Progress') as HTMLButtonElement).disabled).toBe(false))
  const pending = deferred<Response>()
  api.intercept((url) => url === `/api/sessions/${SESSION_ID}/questions/1/attempts/1/diagnosis` ? pending.promise : undefined)
  await submit('Second typed review answer')
  await screen.findByText('Generating answer feedback…')
  const beforeFacts = JSON.stringify(api.detail(1))
  const beforeAttempts = JSON.stringify(api.saved(1))
  await openLoadedProgress()
  expect(overviewValue('Finalized questions').querySelector('dd')?.textContent).toBe('1')
  expect(overviewValue('Saved attempts').querySelector('dd')?.textContent).toBe('2')
  expect(overviewValue('Measured final answers').querySelector('dd')?.textContent).toBe('1')
  const pauseCount = within(progress()).getByRole('region', { name: 'Pause count' })
  expect(pauseCount.textContent).toContain('available for 1 of 1')
  expect(within(pauseCount).getByRole('cell', { name: '2' })).toBeTruthy()
  const beforeProgress = progress().innerHTML
  const reads = summariesReadCount(api)
  await act(async () => pending.resolve(response(semanticDiagnosis)))
  expect(progress().innerHTML).toBe(beforeProgress)
  expect(JSON.stringify(api.detail(1))).toBe(beforeFacts)
  expect(JSON.stringify(api.saved(1))).toBe(beforeAttempts)
  expect(summariesReadCount(api)).toBe(reads)
  expect(within(progress()).queryByText(semanticDiagnosis.addressed_question_reason)).toBeNull()
  expect(within(progress()).queryByRole('region', { name: 'Answer feedback' })).toBeNull()
  await openLoadedHistory()
  expect(summariesReadCount(api)).toBe(reads)
  const history = screen.getByRole('region', { name: 'Remembered session history' })
  expect(within(history).getByText('Attempts', { exact: true }).nextElementSibling?.textContent).toBe('2')
  expect(within(history).queryByText(semanticDiagnosis.addressed_question_reason)).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Open session' }))
  await screen.findByRole('heading', { name: 'Session detail' })
  fireEvent.click(screen.getByRole('button', { name: 'Question 2' }))
  await screen.findByText('Second typed review answer')
  expect(within(screen.getByRole('region', { name: 'Session detail' })).queryByText(semanticDiagnosis.retry_instruction)).toBeNull()
  go('Practice')
  await within(screen.getByRole('region', { name: 'Answer feedback' })).findByText(semanticDiagnosis.addressed_question_reason)
  expect(api.posts('/attempts')).toHaveLength(2)
  expect(api.posts('/continue')).toHaveLength(1)
  expect(api.posts('/transcriptions')).toHaveLength(1)
  expect(api.posts('/diagnosis')).toHaveLength(2)
  expect(JSON.stringify(Object.entries(localStorage))).not.toContain('Semantic-only')
  expect(JSON.stringify(Object.entries(sessionStorage))).not.toContain('Semantic-only')
})

test('successful Continue invalidates final points while the next-question editor remains mounted', async () => {
  const api = mockAppApi(); render(<App />); await start(); await submit('A typed finalized answer')
  await openLoadedProgress()
  expect(overviewValue('Finalized questions').textContent).toContain('0')
  go('Practice')
  fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
  await screen.findByText('Question 2 of 5')
  await waitFor(() => expect((nav('Progress') as HTMLButtonElement).disabled).toBe(false))
  const nextEditor = screen.getByRole('textbox')
  fireEvent.change(nextEditor, { target: { value: 'The next question remains usable' } })
  expect(summariesReadCount(api)).toBe(1)
  await openLoadedProgress()
  expect(summariesReadCount(api)).toBe(2)
  expect(overviewValue('Finalized questions').textContent).toContain('1')
  expect(overviewValue('Saved attempts').textContent).toContain('1')
  expect(overviewValue('Measured final answers').textContent).toContain('0')
  expect(within(progress()).getAllByText(/Unavailable/).length).toBeGreaterThan(0)
  go('Practice')
  expect(screen.getByRole('textbox')).toBe(nextEditor)
  expect((nextEditor as HTMLTextAreaElement).value).toBe('The next question remains usable')
  expect(api.posts('/continue')).toHaveLength(1)
})

test('a same-membership history storage event invalidates facts without losing Practice state', async () => {
  const api = mockAppApi(); render(<App />); await start(); await openLoadedProgress()
  expect(overviewValue('Saved attempts').textContent).toContain('0')
  api.append('Saved in another browser tab')
  act(() => window.dispatchEvent(new StorageEvent('storage', {
    key: HISTORY_PREFIX + SESSION_ID, oldValue: '1', newValue: '1', storageArea: localStorage,
  })))
  await waitFor(() => expect(summariesReadCount(api)).toBe(2))
  await waitFor(() => expect(overviewValue('Saved attempts').textContent).toContain('1'))
  expect(rememberedKeys()).toEqual([HISTORY_PREFIX + SESSION_ID])
  go('History')
  await screen.findByRole('button', { name: 'Open session' })
  expect(summariesReadCount(api)).toBe(2)
  go('Practice')
  expect(screen.getByRole('textbox')).toBeTruthy()
})

test('a history event while Practice is active invalidates lazily and ignores unrelated storage events', async () => {
  const api = mockAppApi(); render(<App />); await start(); await openLoadedProgress(); go('Practice')
  act(() => window.dispatchEvent(new StorageEvent('storage', {
    key: 'unrelated.preference', newValue: '1', storageArea: localStorage,
  })))
  await openLoadedProgress()
  expect(summariesReadCount(api)).toBe(1)
  go('Practice')
  api.append('A saved fact from another tab')
  act(() => window.dispatchEvent(new StorageEvent('storage', {
    key: HISTORY_PREFIX + SESSION_ID, oldValue: '1', newValue: '1', storageArea: localStorage,
  })))
  expect(summariesReadCount(api)).toBe(1)
  await openLoadedProgress()
  expect(summariesReadCount(api)).toBe(2)
  expect(overviewValue('Saved attempts').textContent).toContain('1')
})

test('removing a remembered ID invalidates both views and preserves the current Practice session', async () => {
  const api = mockAppApi(); api.rememberKnown(initialSession(OTHER_ID)); remember(OTHER_ID)
  render(<App />); await start(); await openLoadedProgress()
  expect(overviewValue('Active sessions').textContent).toContain('2')
  go('History')
  await waitFor(() => expect(screen.getAllByRole('button', { name: 'Open session' })).toHaveLength(2))
  const first = screen.getAllByRole('button', { name: 'Remove from this browser' })[0]
  fireEvent.click(first)
  await waitFor(() => expect(screen.getAllByRole('button', { name: 'Open session' })).toHaveLength(1))
  expect(summariesReadCount(api)).toBe(2)
  await openLoadedProgress()
  expect(overviewValue('Active sessions').textContent).toContain('1')
  expect(summariesReadCount(api)).toBe(2)
  go('Practice')
  expect(screen.getByRole('textbox')).toBeTruthy()
  expect(sessionStorage.getItem(PRACTICE_KEY)).toBe(SESSION_ID)
})

test('clearing remembered history clears shared Progress facts and no server delete occurs', async () => {
  const api = mockAppApi(); render(<App />); await start(); await openLoadedProgress()
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  go('History'); await screen.findByRole('button', { name: 'Open session' })
  fireEvent.click(screen.getByRole('button', { name: 'Clear remembered history' }))
  await screen.findByText('No sessions are remembered on this browser yet.')
  go('Progress')
  await within(progress()).findByText('No sessions are remembered on this browser yet.')
  expect(within(progress()).queryByText('Saved attempts', { exact: true })).toBeNull()
  expect(summariesReadCount(api)).toBe(1)
  expect(api.fetchMock.mock.calls.some(([, options]) => options?.method === 'DELETE')).toBe(false)
  go('Practice')
  expect(screen.getByRole('textbox')).toBeTruthy()
  expect(sessionStorage.getItem(PRACTICE_KEY)).toBe(SESSION_ID)
})

test('clearing history cancels a pending shared read and prevents its late result from restoring rows', async () => {
  const api = mockAppApi(); render(<App />); await start()
  const pending = deferred<Response>()
  api.intercept((url) => url === '/api/history/summaries' ? pending.promise : undefined)
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  go('History')
  await waitFor(() => expect(summariesReadCount(api)).toBe(1))
  fireEvent.click(screen.getByRole('button', { name: 'Clear remembered history' }))
  await screen.findByText('No sessions are remembered on this browser yet.')
  go('Progress')
  await within(progress()).findByText('No sessions are remembered on this browser yet.')
  expect(api.posts('/api/history/summaries')[0][1]?.signal?.aborted).toBe(true)
  await act(async () => pending.resolve(response({ summaries: [], missing_session_ids: [SESSION_ID] })))
  expect(within(progress()).queryByText('Active sessions', { exact: true })).toBeNull()
  go('History')
  expect(screen.queryByRole('button', { name: 'Open session' })).toBeNull()
  expect(rememberedKeys()).toEqual([])
  expect(summariesReadCount(api)).toBe(1)
})

test('partial hydration persists across navigation and manual retry requests only the failed chunk', async () => {
  const api = mockAppApi()
  const ids = Array.from({ length: 51 }, (_, index) => `00000000-0000-4000-8000-${(index + 1).toString(16).padStart(12, '0')}`)
  ids.forEach((id) => { remember(id); api.rememberKnown(initialSession(id)) })
  let failLast = true
  api.intercept((url, options) => {
    if (url !== '/api/history/summaries') return undefined
    const requested = (JSON.parse(options!.body as string) as { session_ids: string[] }).session_ids
    if (requested.includes(ids[50]) && failLast) return response({}, 503)
    return undefined
  })
  render(<App />); go('Progress')
  await within(progress()).findByText('Progress totals are unavailable until all remembered sessions load.')
  expect(within(progress()).queryByText('Saved attempts', { exact: true })).toBeNull()
  expect(summariesReadCount(api)).toBe(2)
  go('History')
  await waitFor(() => expect(screen.getAllByRole('button', { name: 'Open session' })).toHaveLength(50))
  expect(screen.getByText(/These results are incomplete/)).toBeTruthy()
  go('Progress')
  expect(within(progress()).getByText('Progress totals are unavailable until all remembered sessions load.')).toBeTruthy()
  expect(summariesReadCount(api)).toBe(2)
  failLast = false
  fireEvent.click(within(progress()).getByRole('button', { name: /Retry.*history/i }))
  await waitFor(() => expect(overviewValue('Active sessions').textContent).toContain('51'))
  expect(summariesReadCount(api)).toBe(3)
  const retryBody = JSON.parse(api.posts('/api/history/summaries')[2][1]!.body as string) as { session_ids: string[] }
  expect(retryBody.session_ids).toEqual([ids[50]])
  go('History')
  await waitFor(() => expect(screen.getAllByRole('button', { name: 'Open session' })).toHaveLength(51))
  expect(summariesReadCount(api)).toBe(3)
})

test('a stale history response cannot overwrite a newer successful Practice invalidation', async () => {
  const api = mockAppApi(); render(<App />); await start()
  const pending = deferred<Response>()
  const initialSummary = { session_id: SESSION_ID, status: 'active', created_at: submittedAt, completed_at: null,
    current_question_number: 1, total_questions: 5, finalized_question_count: 0, questions_practiced_count: 0,
    total_attempt_count: 0, total_retry_count: 0, measured_final_answer_count: 0, last_submitted_at: null,
    last_saved_activity_at: submittedAt, finalized_points: [] }
  api.intercept((url) => url === '/api/history/summaries' ? pending.promise : undefined)
  go('History')
  await waitFor(() => expect(summariesReadCount(api)).toBe(1))
  go('Practice')
  api.intercept(undefined)
  await submit('A newer saved attempt')
  await openLoadedProgress()
  expect(summariesReadCount(api)).toBe(2)
  expect(overviewValue('Saved attempts').textContent).toContain('1')
  await act(async () => pending.resolve(response({ summaries: [initialSummary], missing_session_ids: [] })))
  expect(overviewValue('Saved attempts').textContent).toContain('1')
  await openLoadedHistory()
  expect(summariesReadCount(api)).toBe(2)
  const facts = within(screen.getByRole('region', { name: 'Remembered session history' }))
  expect(facts.getByText('Attempts', { exact: true }).nextElementSibling?.textContent).toBe('1')
})

test('explicit reload refreshes current facts once for both shared history views', async () => {
  const api = mockAppApi(); render(<App />); await start(); await openLoadedProgress()
  api.append('Another tab saved an attempt without a registry membership change')
  fireEvent.click(within(progress()).getByRole('button', { name: /Reload.*history/i }))
  await waitFor(() => expect(overviewValue('Saved attempts').textContent).toContain('1'))
  expect(summariesReadCount(api)).toBe(2)
  await openLoadedHistory()
  expect(summariesReadCount(api)).toBe(2)
})

test('safe Progress navigation preserves measured draft identity without transcription-only invalidation', async () => {
  const api = mockAppApi(); render(<App />); await start(); await openLoadedProgress()
  go('Practice'); await record(); await finishRecording()
  fireEvent.click(screen.getByRole('button', { name: 'Transcribe Recording' }))
  await screen.findByRole('region', { name: 'Speaking measurements' })
  expect(screen.getByRole('region', { name: 'Timed pauses' })).toBeTruthy()
  expect(screen.getByText('Editing the transcript won’t change these measurements.', { exact: false })).toBeTruthy()
  const editor = screen.getByRole('textbox')
  fireEvent.change(editor, { target: { value: 'Edited recorded draft' } })
  await openLoadedProgress()
  expect(summariesReadCount(api)).toBe(1)
  await openLoadedHistory()
  expect(summariesReadCount(api)).toBe(1)
  go('Practice')
  expect(screen.getByRole('textbox')).toBe(editor)
  expect((editor as HTMLTextAreaElement).value).toBe('Edited recorded draft')
  expect(screen.getByRole('region', { name: 'Speaking measurements' })).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Submit Attempt' }))
  await screen.findByRole('button', { name: 'Continue' })
  const body = JSON.parse(api.posts('/attempts')[0][1]!.body as string) as { measurement_id: string }
  expect(body.measurement_id).toBe(MEASUREMENT_ID)
  expect(api.posts('/transcriptions')).toHaveLength(1)
})

test('review and retry-draft identity survive both shared read-only history views', async () => {
  const api = mockAppApi(); render(<App />); await start(); await submit('A saved first attempt')
  await openLoadedProgress(); await openLoadedHistory(); go('Practice')
  expect(screen.getByRole('heading', { name: 'Attempt 1' })).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  const editor = screen.getByRole('textbox')
  fireEvent.change(editor, { target: { value: 'A safe retry draft' } })
  await openLoadedProgress(); await openLoadedHistory(); go('Practice')
  expect(screen.getByRole('textbox')).toBe(editor)
  expect((editor as HTMLTextAreaElement).value).toBe('A safe retry draft')
  expect(screen.getByText('A saved first attempt')).toBeTruthy()
  expect(api.posts('/attempts')).toHaveLength(1)
})

test('linked delivery reaches History and Progress only after finalization without storing measurement facts', async () => {
  const api = mockAppApi(); render(<App />); await start(); await record(); await finishRecording()
  fireEvent.click(screen.getByRole('button', { name: 'Transcribe Recording' }))
  await screen.findByRole('region', { name: 'Timed pauses' })
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Edited delivery review answer' } })
  fireEvent.click(screen.getByRole('button', { name: 'Submit Attempt' }))
  await screen.findByRole('button', { name: 'Continue' })
  await openLoadedProgress()
  expect(overviewValue('Finalized questions').querySelector('dd')?.textContent).toBe('0')
  expect(within(progress()).queryByRole('region', { name: 'Pause count' })).toBeNull()
  go('Practice')
  fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
  await screen.findByText('Question 2 of 5')
  await openLoadedProgress()
  expect(overviewValue('Measured final answers').querySelector('dd')?.textContent).toBe('1')
  const pauseCount = within(progress()).getByRole('region', { name: 'Pause count' })
  expect(within(pauseCount).getByRole('cell', { name: '2' })).toBeTruthy()
  expect(pauseCount.textContent).toContain('available for 1 of 1')
  await openLoadedHistory()
  fireEvent.click(screen.getByRole('button', { name: 'Open session' }))
  await screen.findByRole('heading', { name: 'Session detail' })
  fireEvent.click(screen.getByRole('button', { name: 'Question 1' }))
  await screen.findByText('Edited delivery review answer')
  expect(screen.getByRole('region', { name: 'Timed pauses' }).textContent).toContain('Pause count')
  expect(api.posts('/transcriptions')).toHaveLength(1)
  expect(JSON.stringify(Object.entries(localStorage))).not.toMatch(/pause_count|pause-metrics|Edited delivery|total_pause/)
})

test('a typed final retry excludes superseded voice delivery from longitudinal groups', async () => {
  const api = mockAppApi(); render(<App />); await start(); await record(); await finishRecording()
  fireEvent.click(screen.getByRole('button', { name: 'Transcribe Recording' }))
  await screen.findByRole('region', { name: 'Timed pauses' })
  fireEvent.click(screen.getByRole('button', { name: 'Submit Attempt' }))
  await screen.findByRole('button', { name: 'Retry' })
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  expect(screen.queryByRole('region', { name: 'Timed pauses' })).toBeNull()
  await submit('Typed final retry')
  fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
  await screen.findByText('Question 2 of 5')
  await openLoadedProgress()
  expect(overviewValue('Measured final answers').querySelector('dd')?.textContent).toBe('0')
  expect(within(progress()).queryByRole('region', { name: 'Pause count' })).toBeNull()
  const bodies = api.posts('/attempts').map((call) => JSON.parse(call[1]!.body as string) as { measurement_id: string | null })
  expect(bodies.map((body) => body.measurement_id)).toEqual([MEASUREMENT_ID, null])
  expect(api.posts('/transcriptions')).toHaveLength(1)
})

test('Progress navigation remains blocked during microphone permission, recording and finalization', async () => {
  const pending = deferred<MediaStream>(); getUserMedia.mockReturnValue(pending.promise)
  mockAppApi(); render(<App />); await start()
  fireEvent.click(screen.getByRole('button', { name: 'Record Answer' }))
  await screen.findByText('Waiting for microphone permission…'); assertNavigationBlocked()
  await act(async () => pending.resolve(media))
  await screen.findByRole('button', { name: 'Stop Recording' }); assertNavigationBlocked()
  Recorder.manualStop = true
  fireEvent.click(screen.getByRole('button', { name: 'Stop Recording' }))
  expect(screen.getByText('Finishing recording…')).toBeTruthy(); assertNavigationBlocked()
  act(() => Recorder.instances[0].finish())
  await screen.findByText('Recording stopped. Ready to send.')
  expect((nav('Progress') as HTMLButtonElement).disabled).toBe(false)
})

test.each(['audio', 'transcriptions'] as const)('Progress navigation remains blocked while %s is pending', async (operation) => {
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
  await waitFor(() => expect((nav('Progress') as HTMLButtonElement).disabled).toBe(false))
})

test.each(['attempts', 'continue'] as const)('Progress navigation remains blocked during pending %s mutation', async (operation) => {
  const api = mockAppApi(); render(<App />); await start()
  if (operation === 'continue') await submit('A saved attempt')
  else fireEvent.change(screen.getByRole('textbox'), { target: { value: 'An idle draft' } })
  const pending = deferred<Response>()
  api.intercept((url, options) => url.endsWith(`/${operation}`) && options?.method === 'POST' ? pending.promise : undefined)
  fireEvent.click(screen.getByRole('button', { name: operation === 'attempts' ? 'Submit Attempt' : 'Continue' }))
  assertNavigationBlocked()
  const result = operation === 'attempts' ? { attempt: api.append('An idle draft'), session: api.session() } : api.advance()
  api.intercept(undefined)
  await act(async () => pending.resolve(response(result, operation === 'attempts' ? 201 : 200)))
  await waitFor(() => expect((nav('Progress') as HTMLButtonElement).disabled).toBe(false))
})

test('ambiguous submission and pending authoritative recovery keep Progress navigation blocked', async () => {
  const api = mockAppApi(); render(<App />); await start()
  api.intercept((url, options) => url.endsWith('/attempts') && options?.method === 'POST'
    ? Promise.reject(new TypeError('Synthetic lost response')) : undefined)
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'An unresolved local draft' } })
  fireEvent.click(screen.getByRole('button', { name: 'Submit Attempt' }))
  await screen.findByRole('button', { name: 'Recheck saved state' }); assertNavigationBlocked()
  const pending = deferred<Response>()
  api.intercept((url) => url === `/api/sessions/${SESSION_ID}` ? pending.promise : undefined)
  fireEvent.click(screen.getByRole('button', { name: 'Recheck saved state' })); assertNavigationBlocked()
  api.intercept(undefined)
  await act(async () => pending.resolve(response(api.session())))
  await waitFor(() => expect((nav('Progress') as HTMLButtonElement).disabled).toBe(false))
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('An unresolved local draft')
  expect(api.posts('/attempts')).toHaveLength(1)
})

test('authoritative recovery invalidates history after a successful write whose response was lost', async () => {
  const api = mockAppApi(); render(<App />); await start(); await openLoadedProgress(); go('Practice')
  api.intercept((url, options) => {
    if (!url.endsWith('/attempts') || options?.method !== 'POST') return undefined
    api.append('A persisted answer whose response was lost')
    return Promise.reject(new TypeError('Synthetic lost response'))
  })
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'A persisted answer whose response was lost' } })
  fireEvent.click(screen.getByRole('button', { name: 'Submit Attempt' }))
  await screen.findByRole('button', { name: 'Recheck saved state' }); assertNavigationBlocked()
  expect(summariesReadCount(api)).toBe(1)
  api.intercept(undefined)
  fireEvent.click(screen.getByRole('button', { name: 'Recheck saved state' }))
  await screen.findByText('A persisted answer whose response was lost')
  await waitFor(() => expect((nav('Progress') as HTMLButtonElement).disabled).toBe(false))
  await openLoadedProgress()
  expect(overviewValue('Saved attempts').textContent).toContain('1')
  expect(summariesReadCount(api)).toBe(2)
  expect(api.posts('/attempts')).toHaveLength(1)
})
