// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import AudioAnswer from './AudioAnswer'
import Interview from './Interview'
import type { Attempt, InterviewSession, SemanticDiagnosis, SpeakingMetrics } from './interviewApi'
import type { DeliveryMetrics } from './deliveryMetrics'
import { deliveryUnavailableText, TIMED_PAUSES_EXPLANATION, TIMED_PAUSES_LIMITATION } from './deliveryMetrics'

const session: InterviewSession = { id: 'session-1', status: 'active', current_question_index: 0, current_question: 'Question', current_question_latest_attempt_number: 0, questions: ['Question'], answers: [] }
const semanticDiagnosis: SemanticDiagnosis = {
  diagnosis_version: 'semantic-diagnosis-v1',
  addressed_question: 'yes', addressed_question_reason: 'The answer addresses the immediate question.',
  strengths: [], missing_information: [], structure: 'clear', structure_feedback: 'The account is easy to follow.',
  next_focus: 'maintain_strengths', next_focus_reason: 'Keep the clear account.', retry_instruction: 'Keep the clear account.',
}
let stopTrack: ReturnType<typeof vi.fn>
let getUserMedia: ReturnType<typeof vi.fn>
let media: MediaStream

class Recorder {
  static instances: Recorder[] = []
  static isTypeSupported = vi.fn((type: string) => type === 'audio/webm;codecs=opus')
  state = 'inactive'
  mimeType: string
  ondataavailable: ((event: { data: Blob }) => void) | null = null
  onstop: (() => void) | null = null
  onerror: (() => void) | null = null
  constructor(_stream: MediaStream, options?: MediaRecorderOptions) {
    this.mimeType = options?.mimeType || 'audio/mp4'
    Recorder.instances.push(this)
  }
  start() { this.state = 'recording' }
  stop() {
    this.state = 'inactive'
    // Match browser ordering: final data, then stop, asynchronously.
    queueMicrotask(() => {
      this.ondataavailable?.({ data: new Blob(['audio'], { type: this.mimeType }) })
      this.onstop?.()
    })
  }
}

beforeEach(() => {
  sessionStorage.clear()
  Recorder.instances = []
  Recorder.isTypeSupported.mockImplementation((type) => type === 'audio/webm;codecs=opus')
  stopTrack = vi.fn()
  media = { getTracks: () => [{ stop: stopTrack }] } as unknown as MediaStream
  getUserMedia = vi.fn().mockResolvedValue(media)
  vi.stubGlobal('navigator', { mediaDevices: { getUserMedia } })
  vi.stubGlobal('MediaRecorder', Recorder)
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Unmocked network is forbidden in tests')))
})
afterEach(() => { cleanup(); sessionStorage.clear(); vi.unstubAllGlobals(); vi.restoreAllMocks(); vi.useRealTimers() })

function show() { return render(<AudioAnswer session={session} disabled={false} hasAnswer={false} onTranscript={vi.fn()} onInvalidateMeasurement={vi.fn()} onTranscribing={vi.fn()} />) }
async function record() {
  fireEvent.click(screen.getByRole('button', { name: 'Record Answer' }))
  await screen.findByRole('button', { name: 'Stop Recording' })
}
async function finish() {
  fireEvent.click(screen.getByRole('button', { name: 'Stop Recording' }))
  await screen.findByText('Recording stopped. Ready to send.')
}
function accepted() {
  return new Response(JSON.stringify({ status: 'accepted', session_id: session.id, question_index: 0,
    filename: 'answer-1.webm', content_type: 'audio/webm;codecs=opus', size_bytes: 5 }))
}

// Deterministic mocked backend: saving appends, while Continue alone advances.
function interviewFetch({ active = session, transcriptions = [], submissionError = null }: {
  active?: InterviewSession
  transcriptions?: (Response | Promise<Response>)[]
  submissionError?: number | null
} = {}) {
  let current = active
  const histories = new Map<number, Attempt[]>()
  const persistedMetrics = new Map<string, SpeakingMetrics>()
  const persistedDelivery = new Map<string, DeliveryMetrics>()
  const fetchMock = vi.fn(async (url: string, options?: RequestInit) => {
    const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status })
    if (url === '/api/sessions') {
      current = active
      histories.clear()
      return json(current, 201)
    }
    if (url === `/api/sessions/${active.id}`) return json(current)
    if (url === `/api/sessions/${active.id}/transcriptions`) {
      const response = await transcriptions.shift()
      if (!response) throw new Error('No mocked transcription remains')
      const result = await response.clone().json().catch(() => null)
      if (typeof result?.measurement_id === 'string' && result.metrics) persistedMetrics.set(result.measurement_id, result.metrics)
      if (typeof result?.measurement_id === 'string' && result.delivery_metrics) persistedDelivery.set(result.measurement_id, result.delivery_metrics)
      return response
    }
    const diagnosisMatch = url.match(/^\/api\/sessions\/([^/]+)\/questions\/(\d+)\/attempts\/(\d+)\/diagnosis$/)
    if (diagnosisMatch) {
      expect(diagnosisMatch[1]).toBe(active.id)
      expect(options?.method).toBe('POST')
      expect(options?.body).toBeUndefined()
      expect(histories.get(Number(diagnosisMatch[2]))?.some((attempt) => attempt.attempt_number === Number(diagnosisMatch[3]))).toBe(true)
      return json(semanticDiagnosis)
    }
    const path = `/api/sessions/${active.id}/questions/${current.current_question_index}`
    if (url === `${path}/attempts`) {
      const history = histories.get(current.current_question_index) || []
      if (options?.method !== 'POST') return json(history)
      if (submissionError !== null) return json({}, submissionError)
      const body = JSON.parse(options.body as string)
      expect(body.expected_last_attempt_number).toBe(current.current_question_latest_attempt_number)
      const attempt: Attempt = {
        id: `attempt-${current.current_question_index}-${history.length + 1}`,
        question_index: current.current_question_index, attempt_number: history.length + 1,
        answer: body.answer, measurement_id: body.measurement_id ?? null, submitted_at: '2026-10-04T12:00:00Z',
      }
      histories.set(current.current_question_index, [...history, attempt])
      current = { ...current, current_question_latest_attempt_number: attempt.attempt_number }
      return json({ attempt, session: current }, 201)
    }
    if (url === `${path}/continue`) {
      const body = JSON.parse(options?.body as string)
      expect(body.expected_last_attempt_number).toBe(current.current_question_latest_attempt_number)
      const latest = histories.get(current.current_question_index)?.at(-1)
      if (!latest) return json({}, 409)
      const index = current.current_question_index + 1
      current = { ...current, current_question_index: index,
        current_question: current.questions[index] ?? null,
        status: index === current.questions.length ? 'completed' : 'active',
        current_question_latest_attempt_number: 0, answers: [...current.answers, latest.answer] }
      return json(current)
    }
    if (url === `${path}/comparison`) {
      const history = histories.get(current.current_question_index) || []
      const identity = (attempt: Attempt) => ({ id: attempt.id, attempt_number: attempt.attempt_number,
        measurement_id: attempt.measurement_id, measurement_version: attempt.measurement_id ? 'speaking-v1' : null,
        measurement_source: attempt.measurement_id ? 'original_transcription' : null })
      const metric = (name: 'recognized_word_count' | 'um_count' | 'uh_count' | 'timed_utterance_span_seconds' | 'estimated_words_per_minute') => {
        const selected = [history[0], history.at(-1)!].map((attempt) => attempt.measurement_id ? persistedMetrics.get(attempt.measurement_id) : undefined)
        const values = selected.map((metrics) => metrics?.[name] ?? null)
        const reasons = selected.map((metrics) => !metrics ? 'no_measurement' : name === 'recognized_word_count' ? null
          : name === 'um_count' || name === 'uh_count' ? metrics.filler_unavailable_reason : metrics.timing_unavailable_reason)
        const comparable = values[0] !== null && values[1] !== null
        return { before: values[0], after: values[1], delta: comparable ? values[1]! - values[0]! : null,
          before_unavailable_reason: reasons[0], after_unavailable_reason: reasons[1], comparable,
          comparison_unavailable_reason: comparable ? null : values[0] === null && values[1] === null ? 'both_unavailable'
            : values[0] === null ? 'before_unavailable' : 'after_unavailable' }
      }
      const selected = [history[0], history.at(-1)!].map((attempt) => attempt.measurement_id ? persistedDelivery.get(attempt.measurement_id) : undefined)
      const deliveryMetric = (name: 'pause_count' | 'total_pause_duration_seconds' | 'longest_pause_seconds') => {
        const values = selected.map((facts) => facts?.[name] ?? null)
        const reasons = selected.map((facts) => facts ? facts.unavailable_reason : 'no_measurement')
        const comparable = values[0] !== null && values[1] !== null
        return { before: values[0], after: values[1], delta: comparable ? values[1]! - values[0]! : null,
          before_unavailable_reason: reasons[0], after_unavailable_reason: reasons[1], comparable,
          comparison_unavailable_reason: comparable ? null : values[0] === null && values[1] === null ? 'both_unavailable'
            : values[0] === null ? 'before_unavailable' : 'after_unavailable' }
      }
      return json({ session_id: current.id, question_index: current.current_question_index,
        before_attempt: identity(history[0]), after_attempt: identity(history.at(-1)!), comparison: {
          recognized_word_count: metric('recognized_word_count'), um_count: metric('um_count'), uh_count: metric('uh_count'),
          timed_utterance_span_seconds: metric('timed_utterance_span_seconds'), estimated_words_per_minute: metric('estimated_words_per_minute'),
        }, delivery_comparison: {
          before_version: selected[0]?.version ?? null, after_version: selected[1]?.version ?? null,
          before_source: selected[0]?.source ?? null, after_source: selected[1]?.source ?? null,
          pause_count: deliveryMetric('pause_count'), total_pause_duration_seconds: deliveryMetric('total_pause_duration_seconds'),
          longest_pause_seconds: deliveryMetric('longest_pause_seconds'),
        } })
    }
    throw new Error('Unmocked route is forbidden')
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function attemptWrites(fetchMock: ReturnType<typeof vi.fn>) {
  return fetchMock.mock.calls.filter(([url, options]) => String(url).endsWith('/attempts') && options?.method === 'POST')
}

async function submitThenContinue() {
  fireEvent.click(screen.getByRole('button', { name: 'Submit Attempt' }))
  const button = await screen.findByRole('button', { name: 'Continue' })
  await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false))
  fireEvent.click(button)
}

test('requests permission only on click, prevents duplicate starts, stops tracks, and uploads actual MIME', async () => {
  let resolve!: (response: Response) => void
  const fetchMock = vi.fn(() => new Promise<Response>((done) => { resolve = done }))
  vi.stubGlobal('fetch', fetchMock)
  show()
  expect(screen.getByRole('button', { name: 'Record Answer' })).toBeTruthy()
  expect(getUserMedia).not.toHaveBeenCalled()
  await record()
  fireEvent.click(screen.getByRole('button', { name: 'Record Answer' }))
  expect(getUserMedia).toHaveBeenCalledTimes(1)
  expect(getUserMedia).toHaveBeenCalledWith({ audio: true })
  await finish()
  expect(stopTrack).toHaveBeenCalledTimes(1)
  fireEvent.click(screen.getByRole('button', { name: 'Send Recording' }))
  expect(screen.getByText('Uploading recording…')).toBeTruthy()
  const [url, options] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
  expect(url).toBe('/api/sessions/session-1/audio')
  expect(options.headers).toBeUndefined()
  const body = options.body as FormData
  expect(body.get('question_index')).toBe('0')
  expect((body.get('audio') as File).type).toBe('audio/webm;codecs=opus')
  await act(async () => resolve(accepted()))
  expect(screen.getByText(/Recording accepted/)).toBeTruthy()
})

test('shows permission denial', async () => {
  getUserMedia.mockRejectedValue(new DOMException('Denied', 'NotAllowedError'))
  show()
  fireEvent.click(screen.getByRole('button', { name: 'Record Answer' }))
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', expect.stringContaining('permission denied'))
})

test.each(['media', 'recorder'])('handles unavailable %s APIs without requesting permission', async (api) => {
  if (api === 'media') vi.stubGlobal('navigator', {})
  else vi.stubGlobal('MediaRecorder', undefined)
  show()
  fireEvent.click(screen.getByRole('button', { name: 'Record Answer' }))
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', expect.stringContaining('unavailable'))
  expect(getUserMedia).not.toHaveBeenCalled()
})

test('upload failure retains recording for retry', async () => {
  const fetchMock = vi.fn().mockResolvedValueOnce(new Response('{}', { status: 413 })).mockResolvedValueOnce(accepted())
  vi.stubGlobal('fetch', fetchMock)
  show(); await record(); await finish()
  fireEvent.click(screen.getByRole('button', { name: 'Send Recording' }))
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', expect.stringContaining('too large'))
  fireEvent.click(screen.getByRole('button', { name: 'Send Recording' }))
  expect(await screen.findByText(/Recording accepted/)).toBeTruthy()
  expect(getUserMedia).toHaveBeenCalledTimes(1)
})

test('recorder errors release microphone and allow retry', async () => {
  show(); await record()
  act(() => Recorder.instances[0].onerror?.())
  expect(stopTrack).toHaveBeenCalledTimes(1)
  expect(screen.getByRole('alert').textContent).toContain('Recording failed')
  await record()
  expect(getUserMedia).toHaveBeenCalledTimes(2)
})

test('unmount while recording stops recorder and microphone', async () => {
  const view = show(); await record(); view.unmount()
  expect(stopTrack).toHaveBeenCalledTimes(1)
  expect(Recorder.instances[0].state).toBe('inactive')
})

test('permission granted after unmount immediately releases tracks', async () => {
  let resolve!: (stream: MediaStream) => void
  getUserMedia.mockImplementation(() => new Promise<MediaStream>((done) => { resolve = done }))
  const view = show()
  fireEvent.click(screen.getByRole('button', { name: 'Record Answer' }))
  expect(screen.getByText('Waiting for microphone permission…')).toBeTruthy()
  view.unmount()
  await act(async () => resolve(media))
  expect(stopTrack).toHaveBeenCalledTimes(1)
  expect(Recorder.instances).toHaveLength(0)
})

test('falls back to browser default MIME when preferred formats are unsupported', async () => {
  Recorder.isTypeSupported.mockReturnValue(false)
  const fetchMock = vi.fn().mockResolvedValue(accepted())
  vi.stubGlobal('fetch', fetchMock)
  show(); await record(); await finish()
  fireEvent.click(screen.getByRole('button', { name: 'Send Recording' }))
  const audio = (fetchMock.mock.calls[0][1].body as FormData).get('audio') as File
  expect(audio.type).toBe('audio/mp4')
  expect(audio.name).toBe('answer-1.m4a')
  await screen.findByText(/Recording accepted/)
})

test('constructor failure releases acquired tracks', async () => {
  vi.stubGlobal('MediaRecorder', class {
    static isTypeSupported() { return false }
    constructor() { throw new Error('No encoder') }
  })
  show()
  fireEvent.click(screen.getByRole('button', { name: 'Record Answer' }))
  await screen.findByRole('alert')
  expect(stopTrack).toHaveBeenCalledTimes(1)
})

test('unmount during upload aborts the request', async () => {
  const fetchMock = vi.fn().mockImplementation(() => new Promise(() => {}))
  vi.stubGlobal('fetch', fetchMock)
  const view = show(); await record(); await finish()
  fireEvent.click(screen.getByRole('button', { name: 'Send Recording' }))
  const signal = fetchMock.mock.calls[0][1].signal as AbortSignal
  view.unmount()
  expect(signal.aborted).toBe(true)
})

test('recording byte limit releases microphone and prevents upload', async () => {
  show(); await record()
  act(() => Recorder.instances[0].ondataavailable?.({ data: new Blob([new Uint8Array(10 * 1024 * 1024 + 1)]) }))
  expect(screen.getByRole('alert').textContent).toContain('exceeds 10 MiB')
  expect(stopTrack).toHaveBeenCalledTimes(1)
  expect(screen.queryByRole('button', { name: 'Send Recording' })).toBeNull()
})

test('empty recording is rejected and microphone released', async () => {
  vi.spyOn(Recorder.prototype, 'stop').mockImplementation(function (this: Recorder) {
    this.state = 'inactive'
    queueMicrotask(() => this.onstop?.())
  })
  show(); await record()
  fireEvent.click(screen.getByRole('button', { name: 'Stop Recording' }))
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', expect.stringContaining('No usable audio'))
  expect(stopTrack).toHaveBeenCalledTimes(1)
})

test.each(['start', 'stop'] as const)('%s failure releases microphone', async (method) => {
  vi.spyOn(Recorder.prototype, method).mockImplementation(() => { throw new Error('Recorder failure') })
  show()
  if (method === 'stop') {
    await record()
    fireEvent.click(screen.getByRole('button', { name: 'Stop Recording' }))
  } else fireEvent.click(screen.getByRole('button', { name: 'Record Answer' }))
  await screen.findByRole('alert')
  expect(stopTrack).toHaveBeenCalledTimes(1)
})

test('automatically stops after five minutes', async () => {
  vi.useFakeTimers()
  show()
  await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Record Answer' })))
  await act(async () => vi.advanceTimersByTime(5 * 60 * 1000))
  expect(stopTrack).toHaveBeenCalledTimes(1)
  expect(screen.getByText('Recording stopped. Ready to send.')).toBeTruthy()
})

test('saving a typed attempt releases the old recording and Continue alone advances', async () => {
  const active = { ...session, questions: ['First', 'Second'] }
  interviewFetch({ active })
  render(<Interview />)
  fireEvent.click(screen.getByRole('button', { name: 'Start Interview' }))
  await screen.findByRole('button', { name: 'Record Answer' })
  await record()
  const oldRecorder = Recorder.instances[0]
  expect(oldRecorder.state).toBe('recording')
  expect(stopTrack).not.toHaveBeenCalled()
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Typed' } })
  fireEvent.click(screen.getByRole('button', { name: 'Submit Attempt' }))
  await screen.findByRole('button', { name: 'Continue' })
  expect(screen.getByText('Question 1 of 2')).toBeTruthy()
  await waitFor(() => expect(stopTrack).toHaveBeenCalledTimes(1))
  expect(oldRecorder.state).toBe('inactive')
  expect(screen.queryByRole('button', { name: 'Stop Recording' })).toBeNull()
  expect(screen.queryByRole('button', { name: 'Send Recording' })).toBeNull()
  const button = screen.getByRole('button', { name: 'Continue' }) as HTMLButtonElement
  await waitFor(() => expect(button.disabled).toBe(false))
  fireEvent.click(button)
  await screen.findByText('Question 2 of 2')
})

test('network failure is shown and allows retry', async () => {
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Network failed')))
  show(); await record(); await finish()
  fireEvent.click(screen.getByRole('button', { name: 'Send Recording' }))
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', expect.stringContaining('Check your connection'))
  expect((screen.getByRole('button', { name: 'Send Recording' }) as HTMLButtonElement).disabled).toBe(false)
})

const originalMetrics: SpeakingMetrics = {
  source: 'original_transcription', recognized_word_count: 4, um_count: 0, uh_count: 0,
  filler_unavailable_reason: null, timed_utterance_span_seconds: 2.46,
  estimated_words_per_minute: 97.5609756097561, timing_unavailable_reason: null,
}
const provenance = 'Based on your original recording. Editing the transcript won’t change these measurements.'
const measurementId = 'aed74a31-ddc3-4e0a-b2aa-b98ad52f7b61'
const replacementMeasurementId = '90b9d4a5-4158-4bb3-ae17-33e83a1e9ccf'
const zeroDelivery: DeliveryMetrics = {
  version: 'pause-metrics-v1', source: 'original_transcription', pause_count: 0,
  total_pause_duration_seconds: 0, longest_pause_seconds: 0, unavailable_reason: null,
}

function transcriptResponse(text = 'Hello from my recording', metrics: SpeakingMetrics = originalMetrics,
  delivery: DeliveryMetrics = metrics.timing_unavailable_reason === null ? zeroDelivery : {
    ...zeroDelivery, pause_count: null, total_pause_duration_seconds: null, longest_pause_seconds: null,
    unavailable_reason: metrics.timing_unavailable_reason,
  }) {
  return new Response(JSON.stringify({ session_id: session.id, question_index: 0, measurement_id: measurementId, text,
    language: 'eng', words: [
      { text: 'Hello', start: 10, end: 10.5 }, { text: 'from', start: 10.5, end: 11 },
      { text: 'my', start: 11, end: 11.5 }, { text: 'recording', start: 11.5, end: 12.46 },
    ], metrics, delivery_metrics: delivery }))
}

function measurement(label: string) {
  const panel = screen.getByRole('region', { name: 'Speaking measurements' })
  return within(panel).getByText(label, { selector: 'dt', exact: true }).nextElementSibling?.textContent
}

async function interviewRecording() {
  const view = render(<Interview />)
  fireEvent.click(screen.getByRole('button', { name: 'Start Interview' }))
  await screen.findByRole('button', { name: 'Record Answer' })
  await record()
  await finish()
  return view
}

test('transcribes once, preserves original measurements on edits, and advances only after Continue', async () => {
  let resolve!: (response: Response) => void
  const pending = new Promise<Response>((done) => { resolve = done })
  const fetchMock = interviewFetch({ transcriptions: [pending] })
  await interviewRecording()
  const transcribe = screen.getByRole('button', { name: 'Transcribe Recording' })
  fireEvent.click(transcribe)
  fireEvent.click(transcribe)
  expect(fetchMock).toHaveBeenCalledTimes(2)
  expect(screen.getByText('Transcribing recording…')).toBeTruthy()
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).disabled).toBe(true)
  expect((screen.getByRole('button', { name: 'Submit Attempt' }) as HTMLButtonElement).disabled).toBe(true)
  const [url, options] = fetchMock.mock.calls[1]
  expect(url).toBe('/api/sessions/session-1/transcriptions')
  expect(options?.headers).toBeUndefined()
  const body = options?.body as FormData
  expect(body.get('question_index')).toBe('0')
  expect(body.get('expected_last_attempt_number')).toBe('0')
  expect((body.get('audio') as File).type).toBe('audio/webm;codecs=opus')
  await act(async () => resolve(transcriptResponse()))
  const answer = screen.getByRole('textbox') as HTMLTextAreaElement
  expect(answer.value).toBe('Hello from my recording')
  expect(answer.disabled).toBe(false)
  expect(screen.getByText('Question 1 of 1')).toBeTruthy()
  expect(fetchMock).toHaveBeenCalledTimes(2)
  expect(screen.getByText(/Transcript ready/)).toBeTruthy()
  expect(measurement('Words')).toBe('4')
  expect(measurement('Um')).toBe('0')
  expect(measurement('Uh')).toBe('0')
  expect(measurement('Timed speech span')).toBe('2.5 sec')
  expect(measurement('Estimated WPM')).toBe('98 WPM')
  expect(screen.getByText(provenance)).toBeTruthy()
  fireEvent.change(answer, { target: { value: 'Edited answer' } })
  expect(measurement('Words')).toBe('4')
  expect(measurement('Timed speech span')).toBe('2.5 sec')
  expect(measurement('Estimated WPM')).toBe('98 WPM')
  expect(screen.getByText(provenance)).toBeTruthy()
  await submitThenContinue()
  await screen.findByRole('heading', { name: 'Interview Complete' })
  const [, write] = attemptWrites(fetchMock)[0]
  expect(JSON.parse(write.body)).toEqual({
    expected_last_attempt_number: 0, answer: 'Edited answer', measurement_id: measurementId,
  })
  expect(stopTrack).toHaveBeenCalledTimes(1)
  expect(screen.queryByRole('region', { name: 'Speaking measurements' })).toBeNull()
})

test.each(['My draft', ' '])('protects existing answer %j from replacement', async (text) => {
  const fetchMock = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify(session)))
  vi.stubGlobal('fetch', fetchMock)
  await interviewRecording()
  fireEvent.change(screen.getByRole('textbox'), { target: { value: text } })
  const transcribe = screen.getByRole('button', { name: 'Transcribe Recording' }) as HTMLButtonElement
  expect(transcribe.disabled).toBe(true)
  fireEvent.click(transcribe)
  expect(fetchMock).toHaveBeenCalledTimes(1)
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe(text)
  expect(screen.queryByRole('region', { name: 'Speaking measurements' })).toBeNull()
  expect(screen.getByText(/Clear your answer before transcribing/)).toBeTruthy()
  fireEvent.change(screen.getByRole('textbox'), { target: { value: '' } })
  expect(transcribe.disabled).toBe(false)
})

test.each([502, 503, 504])('transcription failure %s unlocks typing and permits retry', async (status) => {
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(new Response(JSON.stringify(session)))
    .mockResolvedValueOnce(new Response('{}', { status }))
    .mockResolvedValueOnce(transcriptResponse())
  vi.stubGlobal('fetch', fetchMock)
  await interviewRecording()
  fireEvent.click(screen.getByRole('button', { name: 'Transcribe Recording' }))
  await screen.findByRole('alert')
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).disabled).toBe(false)
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('')
  expect(screen.queryByRole('region', { name: 'Speaking measurements' })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Transcribe Recording' }))
  await screen.findByText(/Transcript ready/)
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('Hello from my recording')
  expect(getUserMedia).toHaveBeenCalledTimes(1)
})

test('unmount cancels transcription and ignores a late result', async () => {
  let resolve!: (response: Response) => void
  const fetchMock = vi.fn().mockImplementation(() => new Promise<Response>((done) => { resolve = done }))
  const onTranscript = vi.fn()
  vi.stubGlobal('fetch', fetchMock)
  const view = render(<AudioAnswer session={session} disabled={false} hasAnswer={false}
    onTranscript={onTranscript} onInvalidateMeasurement={vi.fn()} onTranscribing={vi.fn()} />)
  await record(); await finish()
  fireEvent.click(screen.getByRole('button', { name: 'Transcribe Recording' }))
  const signal = fetchMock.mock.calls[0][1].signal as AbortSignal
  view.unmount()
  expect(signal.aborted).toBe(true)
  await act(async () => resolve(transcriptResponse()))
  expect(onTranscript).not.toHaveBeenCalled()
})

test.each(['', '   ', 'x'.repeat(10001)])('unusable transcript does not populate answer (case %#)', async (text) => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response(JSON.stringify(session)))
    .mockResolvedValueOnce(transcriptResponse(text)))
  await interviewRecording()
  fireEvent.click(screen.getByRole('button', { name: 'Transcribe Recording' }))
  await screen.findByRole('alert')
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('')
})

test('displays returned filler counts and formats numbers without mutating API values', async () => {
  const metrics = { ...originalMetrics, um_count: 1, uh_count: 1 }
  const result = {
    session_id: session.id, question_index: 0, measurement_id: measurementId, text: 'Um, UH my recording', language: 'eng',
    words: [
      { text: 'Um,', start: 10, end: 10.5 }, { text: 'UH', start: 10.5, end: 11 },
      { text: 'my', start: 11, end: 11.5 }, { text: 'recording', start: 11.5, end: 12.46 },
    ], metrics, delivery_metrics: zeroDelivery,
  }
  const response = new Response()
  vi.spyOn(response, 'json').mockResolvedValue(result)
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response(JSON.stringify(session)))
    .mockResolvedValueOnce(response))
  await interviewRecording()
  fireEvent.click(screen.getByRole('button', { name: 'Transcribe Recording' }))
  await screen.findByRole('region', { name: 'Speaking measurements' })
  expect(measurement('Um')).toBe('1')
  expect(measurement('Uh')).toBe('1')
  expect(measurement('Timed speech span')).toBe('2.5 sec')
  expect(measurement('Estimated WPM')).toBe('98 WPM')
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Edited without fillers' } })
  expect(measurement('Um')).toBe('1')
  expect(measurement('Uh')).toBe('1')
  expect(metrics).toEqual({ ...originalMetrics, um_count: 1, uh_count: 1 })
})

test('unsupported filler language is unavailable with an explanation, while timing remains visible', async () => {
  const response = transcriptResponse('Hello from my recording', {
    ...originalMetrics, um_count: null, uh_count: null, filler_unavailable_reason: 'unsupported_language',
  })
  const body = await response.json()
  body.language = null
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response(JSON.stringify(session)))
    .mockResolvedValueOnce(new Response(JSON.stringify(body))))
  await interviewRecording()
  fireEvent.click(screen.getByRole('button', { name: 'Transcribe Recording' }))
  await screen.findByRole('region', { name: 'Speaking measurements' })
  expect(measurement('Um')).toBe('Unavailable')
  expect(measurement('Uh')).toBe('Unavailable')
  expect(screen.getByText('Um/uh counts unavailable for this language.')).toBeTruthy()
  expect(measurement('Estimated WPM')).toBe('98 WPM')
})

test.each(['missing_timings', 'timing_coverage_mismatch', 'invalid_timing', 'invalid_timing_order', 'unusable_span'] as const)(
  'explains unavailable timing (%s) without fake numeric values or internal codes', async (reason) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response(JSON.stringify(session)))
      .mockResolvedValueOnce(transcriptResponse('Hello from my recording', {
        ...originalMetrics, timed_utterance_span_seconds: null, estimated_words_per_minute: null,
        timing_unavailable_reason: reason,
      })))
    await interviewRecording()
    fireEvent.click(screen.getByRole('button', { name: 'Transcribe Recording' }))
    const panel = await screen.findByRole('region', { name: 'Speaking measurements' })
    expect(measurement('Timed speech span')).toBe('Unavailable')
    expect(measurement('Estimated WPM')).toBe('Unavailable')
    expect(within(panel).getByText(reason === 'timing_coverage_mismatch'
      ? 'Timing measurements aren’t available because complete word timing wasn’t available.'
      : 'Timing measurements aren’t available for this transcription.')).toBeTruthy()
    expect(panel.querySelector('dl')?.textContent).not.toMatch(/NaN|undefined|0 sec|0 WPM/)
    expect(panel.textContent).not.toContain(reason)
    expect(measurement('Um')).toBe('0')
    expect(measurement('Uh')).toBe('0')
  },
)

test('replacement clears original metrics immediately, and its new transcription supplies the linked measurement', async () => {
  const replacement: SpeakingMetrics = { ...originalMetrics, recognized_word_count: 1,
    timed_utterance_span_seconds: 2, estimated_words_per_minute: 30 }
  const nextResponse = new Response(JSON.stringify({ session_id: session.id, question_index: 0, measurement_id: replacementMeasurementId,
    text: 'Replacement', language: 'eng', words: [{ text: 'Replacement', start: 0, end: 2 }], metrics: replacement,
    delivery_metrics: zeroDelivery }))
  const fetchMock = interviewFetch({ transcriptions: [transcriptResponse(), nextResponse] })
  await interviewRecording()
  fireEvent.click(screen.getByRole('button', { name: 'Transcribe Recording' }))
  await screen.findByRole('region', { name: 'Speaking measurements' })
  fireEvent.change(screen.getByRole('textbox'), { target: { value: '' } })
  expect(measurement('Words')).toBe('4')
  let grant!: (stream: MediaStream) => void
  getUserMedia.mockImplementationOnce(() => new Promise<MediaStream>((done) => { grant = done }))
  fireEvent.click(screen.getByRole('button', { name: 'Record Answer' }))
  expect(screen.queryByRole('region', { name: 'Speaking measurements' })).toBeNull()
  await act(async () => grant(media))
  await finish()
  fireEvent.click(screen.getByRole('button', { name: 'Transcribe Recording' }))
  await screen.findByRole('region', { name: 'Speaking measurements' })
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('Replacement')
  expect(measurement('Words')).toBe('1')
  expect(measurement('Timed speech span')).toBe('2 sec')
  expect(measurement('Estimated WPM')).toBe('30 WPM')
  await submitThenContinue()
  await screen.findByRole('heading', { name: 'Interview Complete' })
  expect(JSON.parse(attemptWrites(fetchMock)[0][1].body)).toEqual({
    expected_last_attempt_number: 0, answer: 'Replacement', measurement_id: replacementMeasurementId,
  })
})

test('failed same-recording transcription retains metrics; failed replacement creates none', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response(JSON.stringify(session)))
    .mockResolvedValueOnce(transcriptResponse()).mockResolvedValueOnce(new Response('{}', { status: 502 }))
    .mockResolvedValueOnce(new Response('{}', { status: 504 })))
  await interviewRecording()
  fireEvent.click(screen.getByRole('button', { name: 'Transcribe Recording' }))
  await screen.findByRole('region', { name: 'Speaking measurements' })
  fireEvent.change(screen.getByRole('textbox'), { target: { value: '' } })
  fireEvent.click(screen.getByRole('button', { name: 'Transcribe Recording' }))
  await screen.findByRole('alert')
  expect(measurement('Words')).toBe('4')
  expect(screen.getByText(provenance)).toBeTruthy()
  await record(); await finish()
  expect(screen.queryByRole('region', { name: 'Speaking measurements' })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Transcribe Recording' }))
  await screen.findByRole('alert')
  expect(screen.queryByRole('region', { name: 'Speaking measurements' })).toBeNull()
})

test('replacement microphone failure cannot leave old metrics or measurement ID attached', async () => {
  const fetchMock = interviewFetch({ transcriptions: [transcriptResponse()] })
  await interviewRecording()
  fireEvent.click(screen.getByRole('button', { name: 'Transcribe Recording' }))
  await screen.findByRole('region', { name: 'Speaking measurements' })
  getUserMedia.mockRejectedValueOnce(new DOMException('Denied', 'NotAllowedError'))
  fireEvent.click(screen.getByRole('button', { name: 'Record Answer' }))
  await screen.findByRole('alert')
  expect(screen.queryByRole('region', { name: 'Speaking measurements' })).toBeNull()
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('Hello from my recording')
  await submitThenContinue()
  await screen.findByRole('heading', { name: 'Interview Complete' })
  expect(JSON.parse(attemptWrites(fetchMock)[0][1].body)).toEqual({
    expected_last_attempt_number: 0, answer: 'Hello from my recording', measurement_id: null,
  })
})

test('question advancement clears measurements rather than carrying them to the next question', async () => {
  const active = { ...session, questions: ['First', 'Second'] }
  const fetchMock = interviewFetch({ active, transcriptions: [transcriptResponse()] })
  await interviewRecording()
  fireEvent.click(screen.getByRole('button', { name: 'Transcribe Recording' }))
  await screen.findByRole('region', { name: 'Speaking measurements' })
  await submitThenContinue()
  await screen.findByText('Question 2 of 2')
  expect(screen.queryByRole('region', { name: 'Speaking measurements' })).toBeNull()
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Second typed answer' } })
  await submitThenContinue()
  await screen.findByRole('heading', { name: 'Interview Complete' })
  expect(attemptWrites(fetchMock).map(([, options]) => JSON.parse(options.body))).toEqual([
    { expected_last_attempt_number: 0, answer: 'Hello from my recording', measurement_id: measurementId },
    { expected_last_attempt_number: 0, answer: 'Second typed answer', measurement_id: null },
  ])
})

test('failed submission preserves metrics and explicit session restart clears them', async () => {
  const fetchMock = interviewFetch({ transcriptions: [transcriptResponse()], submissionError: 500 })
  await interviewRecording()
  fireEvent.click(screen.getByRole('button', { name: 'Transcribe Recording' }))
  await screen.findByRole('region', { name: 'Speaking measurements' })
  fireEvent.click(screen.getByRole('button', { name: 'Submit Attempt' }))
  await screen.findByRole('alert')
  expect(measurement('Words')).toBe('4')
  expect(JSON.parse(attemptWrites(fetchMock)[0][1].body)).toEqual({
    expected_last_attempt_number: 0, answer: 'Hello from my recording', measurement_id: measurementId,
  })
  fireEvent.click(screen.getByRole('button', { name: 'Start New Interview' }))
  await waitFor(() => expect(screen.queryByRole('region', { name: 'Speaking measurements' })).toBeNull())
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('')
})

test('a typed-only attempt never creates speaking measurements or requests the microphone', async () => {
  const fetchMock = interviewFetch()
  render(<Interview />)
  fireEvent.click(screen.getByRole('button', { name: 'Start Interview' }))
  await screen.findByRole('textbox')
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Typed answer' } })
  expect(screen.queryByRole('region', { name: 'Speaking measurements' })).toBeNull()
  await submitThenContinue()
  await screen.findByRole('heading', { name: 'Interview Complete' })
  expect(screen.queryByRole('region', { name: 'Speaking measurements' })).toBeNull()
  expect(getUserMedia).not.toHaveBeenCalled()
  expect(fetchMock.mock.calls.every(([url]) => !String(url).endsWith('/answers'))).toBe(true)
  expect(JSON.parse(attemptWrites(fetchMock)[0][1].body)).toEqual({ expected_last_attempt_number: 0, answer: 'Typed answer', measurement_id: null })
})

test.each([
  undefined, { ...originalMetrics, source: 'edited_answer' },
  { ...originalMetrics, recognized_word_count: '4' },
  { ...originalMetrics, um_count: null },
  { ...originalMetrics, estimated_words_per_minute: null },
  { ...originalMetrics, timing_unavailable_reason: 'private-provider-message' },
])('rejects missing or malformed required measurements without displaying fake values (case %#)', async (metrics) => {
  const response = new Response(JSON.stringify({ session_id: session.id, question_index: 0, measurement_id: measurementId,
    text: 'Hello', language: 'eng', words: [], metrics, delivery_metrics: zeroDelivery }))
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response(JSON.stringify(session)))
    .mockResolvedValueOnce(response))
  await interviewRecording()
  fireEvent.click(screen.getByRole('button', { name: 'Transcribe Recording' }))
  const alert = await screen.findByRole('alert')
  expect(alert.textContent).toBe('The transcription result is unknown. Recheck saved state before continuing.')
  expect(alert.textContent).not.toContain('private-provider-message')
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('')
  expect(screen.queryByRole('region', { name: 'Speaking measurements' })).toBeNull()
})

test.each(['', '   '])('deleting and retyping transcript text through %j preserves the same draft measurement association', async (empty) => {
  const fetchMock = interviewFetch({ transcriptions: [transcriptResponse()] })
  await interviewRecording()
  fireEvent.click(screen.getByRole('button', { name: 'Transcribe Recording' }))
  await screen.findByRole('region', { name: 'Speaking measurements' })
  fireEvent.change(screen.getByRole('textbox'), { target: { value: empty } })
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Typed replacement' } })
  await submitThenContinue()
  await screen.findByRole('heading', { name: 'Interview Complete' })
  expect(JSON.parse(attemptWrites(fetchMock)[0][1].body)).toEqual({ expected_last_attempt_number: 0, answer: 'Typed replacement', measurement_id: measurementId })
})

test('failed replacement transcription leaves no old measurement ID on a later typed attempt', async () => {
  const fetchMock = interviewFetch({ transcriptions: [transcriptResponse(), new Response('{}', { status: 502 })] })
  await interviewRecording()
  fireEvent.click(screen.getByRole('button', { name: 'Transcribe Recording' }))
  await screen.findByRole('region', { name: 'Speaking measurements' })
  fireEvent.change(screen.getByRole('textbox'), { target: { value: '' } })
  await record(); await finish()
  fireEvent.click(screen.getByRole('button', { name: 'Transcribe Recording' }))
  await screen.findByRole('alert')
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Typed after failure' } })
  await submitThenContinue()
  await screen.findByRole('heading', { name: 'Interview Complete' })
  expect(JSON.parse(attemptWrites(fetchMock)[0][1].body)).toEqual({ expected_last_attempt_number: 0, answer: 'Typed after failure', measurement_id: null })
})

test.each([undefined, null, '', 'not-a-uuid', 123, `${measurementId}-extra`])(
  'rejects a transcription without a usable opaque measurement UUID (case %#)', async (id) => {
    const response = await transcriptResponse().json()
    response.measurement_id = id
    const fetchMock = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify(session)))
      .mockResolvedValueOnce(new Response(JSON.stringify(response)))
    vi.stubGlobal('fetch', fetchMock)
    await interviewRecording()
    fireEvent.click(screen.getByRole('button', { name: 'Transcribe Recording' }))
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toBe('The transcription result is unknown. Recheck saved state before continuing.')
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('')
    expect(screen.queryByRole('region', { name: 'Speaking measurements' })).toBeNull()
    expect(fetchMock).toHaveBeenCalledTimes(2)
  },
)

test('hands the successful transcript and its exact opaque ID to the draft owner together', async () => {
  const onTranscript = vi.fn()
  const onInvalidateMeasurement = vi.fn()
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(transcriptResponse()))
  render(<AudioAnswer session={session} disabled={false} hasAnswer={false}
    onTranscript={onTranscript} onInvalidateMeasurement={onInvalidateMeasurement} onTranscribing={vi.fn()} />)
  await record(); await finish()
  fireEvent.click(screen.getByRole('button', { name: 'Transcribe Recording' }))
  await screen.findByText(/Transcript ready/)
  expect(onTranscript).toHaveBeenCalledExactlyOnceWith('Hello from my recording', measurementId)
  expect(onInvalidateMeasurement).toHaveBeenCalledTimes(2)
})

test('live positive delivery displays scalar original-transcription facts and survives transcript edits without storage persistence', async () => {
  const delivery: DeliveryMetrics = { ...zeroDelivery, pause_count: 2, total_pause_duration_seconds: 1.123456789, longest_pause_seconds: 0.623456789 }
  const before = structuredClone(delivery)
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response(JSON.stringify(session)))
    .mockResolvedValueOnce(transcriptResponse('Hello from my recording', originalMetrics, delivery)))
  await interviewRecording()
  const storageBefore = Object.entries(localStorage)
  fireEvent.click(screen.getByRole('button', { name: 'Transcribe Recording' }))
  const panel = await screen.findByRole('region', { name: 'Timed pauses' })
  const value = (label: string) => within(panel).getByText(label, { selector: 'dt', exact: true }).nextElementSibling?.textContent
  expect(value('Pause count')).toBe('2')
  expect(value('Total pause time')).toBe('1.1 s')
  expect(value('Longest pause')).toBe('0.6 s')
  expect(within(panel).getByText(TIMED_PAUSES_EXPLANATION)).toBeTruthy()
  expect(within(panel).getByText(TIMED_PAUSES_LIMITATION)).toBeTruthy()
  expect(screen.getByText(provenance)).toBeTruthy()
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Edited answer with different words' } })
  expect(value('Pause count')).toBe('2')
  expect(value('Total pause time')).toBe('1.1 s')
  expect(screen.getByText(provenance)).toBeTruthy()
  expect(delivery).toEqual(before)
  expect(Object.entries(localStorage)).toEqual(storageBefore)
  expect(panel.textContent).not.toMatch(/\b(improved|better|worse|score|quality|confidence|fluency|ideal)\b/i)
})

test('live zero delivery renders 0 and 0.0 s instead of unavailable states', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(transcriptResponse()))
  show(); await record(); await finish()
  fireEvent.click(screen.getByRole('button', { name: 'Transcribe Recording' }))
  const panel = await screen.findByRole('region', { name: 'Timed pauses' })
  expect(within(panel).getByText('Pause count', { selector: 'dt' }).nextElementSibling?.textContent).toBe('0')
  expect(within(panel).getByText('Total pause time', { selector: 'dt' }).nextElementSibling?.textContent).toBe('0.0 s')
  expect(within(panel).getByText('Longest pause', { selector: 'dt' }).nextElementSibling?.textContent).toBe('0.0 s')
  expect(panel.textContent).not.toMatch(/Unavailable|Not recorded/)
})

test.each(['missing_timings', 'timing_coverage_mismatch', 'invalid_timing', 'invalid_timing_order', 'unusable_span'] as const)(
  'live unavailable delivery (%s) renders only factual mapped reason and null facts', async (reason) => {
    const metrics: SpeakingMetrics = { ...originalMetrics, timed_utterance_span_seconds: null, estimated_words_per_minute: null, timing_unavailable_reason: reason }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(transcriptResponse('Hello from my recording', metrics)))
    show(); await record(); await finish()
    fireEvent.click(screen.getByRole('button', { name: 'Transcribe Recording' }))
    const panel = await screen.findByRole('region', { name: 'Timed pauses' })
    expect(within(panel).getByText('Pause count', { selector: 'dt' }).nextElementSibling?.textContent).toBe('Unavailable')
    expect(within(panel).getByText(`Unavailable — ${deliveryUnavailableText(reason)}`)).toBeTruthy()
    expect(panel.textContent).not.toContain(reason)
    expect(panel.textContent).not.toMatch(/Not recorded|0.0 s/)
  },
)

test('Retry clears the recorded draft and a typed retry explicitly carries no original measurement ID', async () => {
  const fetchMock = interviewFetch({ transcriptions: [transcriptResponse()] })
  await interviewRecording()
  fireEvent.click(screen.getByRole('button', { name: 'Transcribe Recording' }))
  await screen.findByRole('region', { name: 'Speaking measurements' })
  fireEvent.click(screen.getByRole('button', { name: 'Submit Attempt' }))
  const retry = await screen.findByRole('button', { name: 'Retry' })
  await waitFor(() => expect((retry as HTMLButtonElement).disabled).toBe(false))
  fireEvent.click(retry)
  const textbox = await screen.findByRole('textbox') as HTMLTextAreaElement
  expect(textbox.value).toBe('')
  expect(screen.queryByRole('region', { name: 'Speaking measurements' })).toBeNull()
  expect(screen.queryByRole('button', { name: 'Send Recording' })).toBeNull()
  expect(screen.queryByRole('button', { name: 'Transcribe Recording' })).toBeNull()
  expect(screen.queryByRole('alert')).toBeNull()
  fireEvent.change(textbox, { target: { value: 'Typed retry' } })
  fireEvent.click(screen.getByRole('button', { name: 'Submit Attempt' }))
  await screen.findByRole('button', { name: 'Retry Again' })
  expect(attemptWrites(fetchMock).map(([, options]) => JSON.parse(options.body))).toEqual([
    { answer: 'Hello from my recording', expected_last_attempt_number: 0, measurement_id: measurementId },
    { answer: 'Typed retry', expected_last_attempt_number: 1, measurement_id: null },
  ])
  expect(getUserMedia).toHaveBeenCalledTimes(1)
  expect(stopTrack).toHaveBeenCalledTimes(1)
})

test('a newly recorded retry transcribes with revision one and links only its new measurement', async () => {
  const replacement = await transcriptResponse('Recorded retry').json()
  replacement.measurement_id = replacementMeasurementId
  const fetchMock = interviewFetch({ transcriptions: [transcriptResponse(), new Response(JSON.stringify(replacement))] })
  await interviewRecording()
  fireEvent.click(screen.getByRole('button', { name: 'Transcribe Recording' }))
  await screen.findByRole('region', { name: 'Speaking measurements' })
  fireEvent.click(screen.getByRole('button', { name: 'Submit Attempt' }))
  const retry = await screen.findByRole('button', { name: 'Retry' })
  await waitFor(() => expect((retry as HTMLButtonElement).disabled).toBe(false))
  fireEvent.click(retry)
  await record(); await finish()
  fireEvent.click(screen.getByRole('button', { name: 'Transcribe Recording' }))
  await screen.findByRole('region', { name: 'Speaking measurements' })
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('Recorded retry')
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Edited recorded retry' } })
  fireEvent.click(screen.getByRole('button', { name: 'Submit Attempt' }))
  await screen.findByRole('button', { name: 'Retry Again' })
  const transcriptions = fetchMock.mock.calls.filter(([url]) => url.endsWith('/transcriptions'))
  expect(transcriptions.map(([, options]) => {
    if (!options || !(options.body instanceof FormData)) throw new Error('Expected mocked transcription multipart body')
    return options.body.get('expected_last_attempt_number')
  })).toEqual(['0', '1'])
  expect(JSON.parse(attemptWrites(fetchMock)[1][1].body)).toEqual({
    answer: 'Edited recorded retry', expected_last_attempt_number: 1, measurement_id: replacementMeasurementId,
  })
  expect(getUserMedia).toHaveBeenCalledTimes(2)
  expect(stopTrack).toHaveBeenCalledTimes(2)
})

test('cancelling Retry stops its live microphone and discards the unsaved recording and text', async () => {
  interviewFetch()
  render(<Interview />)
  fireEvent.click(screen.getByRole('button', { name: 'Start Interview' }))
  await screen.findByRole('textbox')
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Saved typed answer' } })
  fireEvent.click(screen.getByRole('button', { name: 'Submit Attempt' }))
  const retry = await screen.findByRole('button', { name: 'Retry' })
  await waitFor(() => expect((retry as HTMLButtonElement).disabled).toBe(false))
  fireEvent.click(retry)
  await record()
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Discard this retry' } })
  fireEvent.click(screen.getByRole('button', { name: 'Cancel Retry' }))
  expect(stopTrack).toHaveBeenCalledTimes(1)
  expect(Recorder.instances[0].state).toBe('inactive')
  expect(screen.queryByRole('textbox')).toBeNull()
  expect(screen.getByText('Saved typed answer')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('')
  expect(screen.queryByRole('button', { name: 'Send Recording' })).toBeNull()
})

test('transcription 409 clears stale recording state and reloads the current server question without replay', async () => {
  const active = { ...session, questions: ['First', 'Second'] }
  const changed: InterviewSession = { ...active, current_question_index: 1, current_question: 'Second', answers: ['Other tab answer'] }
  const fetchMock = vi.fn(async (url: string) => {
    if (url === '/api/sessions') return new Response(JSON.stringify(active))
    if (url.endsWith('/transcriptions')) return new Response('private conflict details', { status: 409 })
    if (url === '/api/sessions/session-1') return new Response(JSON.stringify(changed))
    if (url === '/api/sessions/session-1/questions/1/attempts') return new Response('[]')
    throw new Error('Unmocked route')
  })
  vi.stubGlobal('fetch', fetchMock)
  await interviewRecording()
  fireEvent.click(screen.getByRole('button', { name: 'Transcribe Recording' }))
  await screen.findByText('Question 2 of 2')
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('')
  expect(screen.queryByRole('button', { name: 'Send Recording' })).toBeNull()
  expect(screen.queryByRole('region', { name: 'Speaking measurements' })).toBeNull()
  expect(screen.getByRole('alert').textContent).not.toContain('private conflict details')
  expect(fetchMock.mock.calls.filter(([url]) => url.endsWith('/transcriptions'))).toHaveLength(1)
  expect(fetchMock.mock.calls.some(([url]) => url === '/api/sessions/session-1/questions/1/attempts')).toBe(true)
})

test('an uncertain transcription blocks the draft until explicit read-only recheck and never replays audio', async () => {
  const fetchMock = vi.fn(async (url: string) => {
    if (url === '/api/sessions' || url === '/api/sessions/session-1') return new Response(JSON.stringify(session))
    if (url.endsWith('/transcriptions')) throw new Error('private network error')
    if (url.endsWith('/attempts')) return new Response('[]')
    throw new Error('Unmocked route')
  })
  vi.stubGlobal('fetch', fetchMock)
  await interviewRecording()
  fireEvent.click(screen.getByRole('button', { name: 'Transcribe Recording' }))
  const recheck = await screen.findByRole('button', { name: 'Recheck saved state' })
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).disabled).toBe(true)
  expect(screen.queryByRole('button', { name: 'Send Recording' })).toBeNull()
  expect(screen.getByRole('alert').textContent).not.toContain('private network error')
  fireEvent.click(recheck)
  await waitFor(() => expect((screen.getByRole('textbox') as HTMLTextAreaElement).disabled).toBe(false))
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('')
  expect(fetchMock.mock.calls.filter(([url]) => url.endsWith('/transcriptions'))).toHaveLength(1)
  expect(fetchMock.mock.calls.filter(([url]) => url === '/api/sessions/session-1')).toHaveLength(2)
})

test('an audio generation remount cancels pending transcription and excludes late original measurements', async () => {
  let resolve!: (response: Response) => void
  const fetchMock = vi.fn().mockImplementationOnce(() => new Promise<Response>((done) => { resolve = done }))
    .mockResolvedValueOnce(new Response('{}', { status: 502 }))
  vi.stubGlobal('fetch', fetchMock)
  const onTranscript = vi.fn()
  const props = { disabled: false, hasAnswer: false, onTranscript, onInvalidateMeasurement: vi.fn(), onTranscribing: vi.fn() }
  const view = render(<AudioAnswer key="draft-0" session={session} {...props} />)
  await record(); await finish()
  fireEvent.click(screen.getByRole('button', { name: 'Transcribe Recording' }))
  const signal = fetchMock.mock.calls[0][1].signal as AbortSignal
  view.rerender(<AudioAnswer key="draft-1" session={{ ...session, current_question_latest_attempt_number: 1 }} {...props} />)
  expect(signal.aborted).toBe(true)
  expect(screen.queryByRole('button', { name: 'Send Recording' })).toBeNull()
  expect(screen.queryByRole('region', { name: 'Speaking measurements' })).toBeNull()
  expect(screen.queryByRole('alert')).toBeNull()
  await act(async () => resolve(transcriptResponse()))
  expect(onTranscript).not.toHaveBeenCalled()
  await record(); await finish()
  fireEvent.click(screen.getByRole('button', { name: 'Transcribe Recording' }))
  await screen.findByRole('alert')
  expect((fetchMock.mock.calls[1][1].body as FormData).get('expected_last_attempt_number')).toBe('1')
  view.rerender(<AudioAnswer key="draft-2" session={{ ...session, current_question_latest_attempt_number: 1 }} {...props} />)
  expect(screen.queryByRole('alert')).toBeNull()
  expect(screen.queryByRole('button', { name: 'Send Recording' })).toBeNull()
})
