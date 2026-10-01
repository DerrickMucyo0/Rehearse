// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import AudioAnswer from './AudioAnswer'
import Interview from './Interview'
import type { InterviewSession } from './interviewApi'

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
})
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); vi.useRealTimers() })

function show() { return render(<AudioAnswer session={session} disabled={false} />) }
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
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Typed' } })
  fireEvent.click(screen.getByRole('button', { name: 'Submit Answer' }))
  await screen.findByText('Question 2 of 2')
  expect(stopTrack).toHaveBeenCalledTimes(1)
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
