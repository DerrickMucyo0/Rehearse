// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import AudioAnswer from './AudioAnswer'
import Interview from './Interview'
import type { InterviewSession, SpeakingMetrics } from './interviewApi'

const session: InterviewSession = { id: 'session-1', status: 'active', current_question_index: 0, current_question: 'Question', questions: ['Question'], answers: [] }
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
  Recorder.instances = []
  Recorder.isTypeSupported.mockImplementation((type) => type === 'audio/webm;codecs=opus')
  stopTrack = vi.fn()
  media = { getTracks: () => [{ stop: stopTrack }] } as unknown as MediaStream
  getUserMedia = vi.fn().mockResolvedValue(media)
  vi.stubGlobal('navigator', { mediaDevices: { getUserMedia } })
  vi.stubGlobal('MediaRecorder', Recorder)
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Unmocked network is forbidden in tests')))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); vi.useRealTimers() })

function show() { return render(<AudioAnswer session={session} disabled={false} hasAnswer={false} onTranscript={vi.fn()} onTranscribing={vi.fn()} />) }
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

test('typed answer advances and releases a recording for the old question', async () => {
  const active = { ...session, questions: ['First', 'Second'] }
  const next = { ...active, current_question_index: 1, current_question: 'Second', answers: ['Typed'] }
  vi.stubGlobal('fetch', vi.fn()
    .mockResolvedValueOnce(new Response(JSON.stringify(active)))
    .mockResolvedValueOnce(new Response(JSON.stringify(active)))
    .mockResolvedValueOnce(new Response(JSON.stringify(next))))
  render(<Interview />)
  fireEvent.click(screen.getByRole('button', { name: 'Start Interview' }))
  await screen.findByRole('button', { name: 'Record Answer' })
  await record()
  const oldRecorder = Recorder.instances[0]
  expect(oldRecorder.state).toBe('recording')
  expect(stopTrack).not.toHaveBeenCalled()
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Typed' } })
  fireEvent.click(screen.getByRole('button', { name: 'Submit Answer' }))
  await screen.findByText('Question 2 of 2')
  // The new DOM can appear before the old recorder's passive unmount cleanup.
  await waitFor(() => expect(stopTrack).toHaveBeenCalledTimes(1))
  expect(oldRecorder.state).toBe('inactive')
  expect(screen.queryByRole('button', { name: 'Stop Recording' })).toBeNull()
  expect(screen.queryByRole('button', { name: 'Send Recording' })).toBeNull()
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

function transcriptResponse(text = 'Hello from my recording', metrics: SpeakingMetrics = originalMetrics) {
  return new Response(JSON.stringify({ session_id: session.id, question_index: 0, text,
    language: 'eng', words: [
      { text: 'Hello', start: 10, end: 10.5 }, { text: 'from', start: 10.5, end: 11 },
      { text: 'my', start: 11, end: 11.5 }, { text: 'recording', start: 11.5, end: 12.46 },
    ], metrics }))
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

test('transcribes once, fills editable answer, and advances only after explicit submission', async () => {
  let resolve!: (response: Response) => void
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(new Response(JSON.stringify(session)))
    .mockImplementationOnce(() => new Promise<Response>((done) => { resolve = done }))
    .mockResolvedValueOnce(new Response(JSON.stringify(session)))
    .mockResolvedValueOnce(new Response(JSON.stringify({ ...session, status: 'completed',
      current_question_index: 1, current_question: null, answers: ['Edited answer'] })))
  vi.stubGlobal('fetch', fetchMock)
  await interviewRecording()
  const transcribe = screen.getByRole('button', { name: 'Transcribe Recording' })
  fireEvent.click(transcribe)
  fireEvent.click(transcribe)
  expect(fetchMock).toHaveBeenCalledTimes(2)
  expect(screen.getByText('Transcribing recording…')).toBeTruthy()
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).disabled).toBe(true)
  expect((screen.getByRole('button', { name: 'Submit Answer' }) as HTMLButtonElement).disabled).toBe(true)
  const [url, options] = fetchMock.mock.calls[1]
  expect(url).toBe('/api/sessions/session-1/transcriptions')
  expect(options.headers).toBeUndefined()
  expect(options.body.get('question_index')).toBe('0')
  expect(options.body.get('audio').type).toBe('audio/webm;codecs=opus')
  await act(async () => resolve(transcriptResponse()))
  const answer = screen.getByRole('textbox') as HTMLTextAreaElement
  expect(answer.value).toBe('Hello from my recording')
  expect(answer.disabled).toBe(false)
  expect(screen.getByText('Question 1 of 1')).toBeTruthy()
  expect(fetchMock).toHaveBeenCalledTimes(2)
  expect(screen.getByText(/Transcript ready/)).toBeTruthy()
  expect(screen.getByRole('heading', { name: 'Speaking measurements' })).toBeTruthy()
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
  fireEvent.click(screen.getByRole('button', { name: 'Submit Answer' }))
  await screen.findByRole('heading', { name: 'Interview Complete' })
  expect(JSON.parse(fetchMock.mock.calls[3][1].body)).toEqual({ question_index: 0, answer: 'Edited answer' })
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
    onTranscript={onTranscript} onTranscribing={vi.fn()} />)
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
    session_id: session.id, question_index: 0, text: 'Um, UH my recording', language: 'eng',
    words: [
      { text: 'Um,', start: 10, end: 10.5 }, { text: 'UH', start: 10.5, end: 11 },
      { text: 'my', start: 11, end: 11.5 }, { text: 'recording', start: 11.5, end: 12.46 },
    ], metrics,
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
    expect(panel.textContent).not.toMatch(/NaN|undefined|0 sec|0 WPM/)
    expect(panel.textContent).not.toContain(reason)
    expect(measurement('Um')).toBe('0')
    expect(measurement('Uh')).toBe('0')
  },
)

test('replacement clears original metrics immediately, and its new transcription replaces them', async () => {
  const replacement: SpeakingMetrics = { ...originalMetrics, recognized_word_count: 1,
    timed_utterance_span_seconds: 2, estimated_words_per_minute: 30 }
  const nextResponse = new Response(JSON.stringify({ session_id: session.id, question_index: 0,
    text: 'Replacement', language: 'eng', words: [{ text: 'Replacement', start: 0, end: 2 }],
    metrics: replacement }))
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response(JSON.stringify(session)))
    .mockResolvedValueOnce(transcriptResponse()).mockResolvedValueOnce(nextResponse))
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

test('replacement microphone failure cannot leave old metrics visible', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response(JSON.stringify(session)))
    .mockResolvedValueOnce(transcriptResponse()))
  await interviewRecording()
  fireEvent.click(screen.getByRole('button', { name: 'Transcribe Recording' }))
  await screen.findByRole('region', { name: 'Speaking measurements' })
  getUserMedia.mockRejectedValueOnce(new DOMException('Denied', 'NotAllowedError'))
  fireEvent.click(screen.getByRole('button', { name: 'Record Answer' }))
  await screen.findByRole('alert')
  expect(screen.queryByRole('region', { name: 'Speaking measurements' })).toBeNull()
})

test('question advancement clears measurements rather than carrying them to the next question', async () => {
  const active = { ...session, questions: ['First', 'Second'] }
  const next = { ...active, current_question_index: 1, current_question: 'Second', answers: ['Hello from my recording'] }
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response(JSON.stringify(active)))
    .mockResolvedValueOnce(transcriptResponse()).mockResolvedValueOnce(new Response(JSON.stringify(active)))
    .mockResolvedValueOnce(new Response(JSON.stringify(next))))
  await interviewRecording()
  fireEvent.click(screen.getByRole('button', { name: 'Transcribe Recording' }))
  await screen.findByRole('region', { name: 'Speaking measurements' })
  fireEvent.click(screen.getByRole('button', { name: 'Submit Answer' }))
  await screen.findByText('Question 2 of 2')
  expect(screen.queryByRole('region', { name: 'Speaking measurements' })).toBeNull()
})

test('failed submission preserves metrics and explicit session restart clears them', async () => {
  const restarted = { ...session, id: 'session-2' }
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response(JSON.stringify(session)))
    .mockResolvedValueOnce(transcriptResponse()).mockResolvedValueOnce(new Response(JSON.stringify(session)))
    .mockResolvedValueOnce(new Response('{}', { status: 500 }))
    .mockResolvedValueOnce(new Response(JSON.stringify(restarted))))
  await interviewRecording()
  fireEvent.click(screen.getByRole('button', { name: 'Transcribe Recording' }))
  await screen.findByRole('region', { name: 'Speaking measurements' })
  fireEvent.click(screen.getByRole('button', { name: 'Submit Answer' }))
  await screen.findByRole('alert')
  expect(measurement('Words')).toBe('4')
  fireEvent.click(screen.getByRole('button', { name: 'Start New Interview' }))
  await waitFor(() => expect(screen.queryByRole('region', { name: 'Speaking measurements' })).toBeNull())
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('')
})

test('a typed-only answer never creates speaking measurements or requests the microphone', async () => {
  const completed = { ...session, status: 'completed', current_question_index: 1,
    current_question: null, answers: ['Typed answer'] }
  const fetchMock = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify(session)))
    .mockResolvedValueOnce(new Response(JSON.stringify(session)))
    .mockResolvedValueOnce(new Response(JSON.stringify(completed)))
  vi.stubGlobal('fetch', fetchMock)
  render(<Interview />)
  fireEvent.click(screen.getByRole('button', { name: 'Start Interview' }))
  await screen.findByRole('textbox')
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Typed answer' } })
  expect(screen.queryByRole('region', { name: 'Speaking measurements' })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Submit Answer' }))
  await screen.findByRole('heading', { name: 'Interview Complete' })
  expect(screen.queryByRole('region', { name: 'Speaking measurements' })).toBeNull()
  expect(getUserMedia).not.toHaveBeenCalled()
  expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
    '/api/sessions', '/api/sessions/session-1', '/api/sessions/session-1/answers',
  ])
})

test.each([
  undefined, { ...originalMetrics, source: 'edited_answer' },
  { ...originalMetrics, recognized_word_count: '4' },
  { ...originalMetrics, um_count: null },
  { ...originalMetrics, estimated_words_per_minute: null },
  { ...originalMetrics, timing_unavailable_reason: 'private-provider-message' },
])('rejects missing or malformed required measurements without displaying fake values (case %#)', async (metrics) => {
  const response = new Response(JSON.stringify({ session_id: session.id, question_index: 0,
    text: 'Hello', language: 'eng', words: [], metrics }))
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response(JSON.stringify(session)))
    .mockResolvedValueOnce(response))
  await interviewRecording()
  fireEvent.click(screen.getByRole('button', { name: 'Transcribe Recording' }))
  const alert = await screen.findByRole('alert')
  expect(alert.textContent).toBe('No usable speaking measurements were returned. Please try again or type your answer.')
  expect(alert.textContent).not.toContain('private-provider-message')
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('')
  expect(screen.queryByRole('region', { name: 'Speaking measurements' })).toBeNull()
})
