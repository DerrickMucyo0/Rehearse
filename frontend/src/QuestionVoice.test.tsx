// @vitest-environment jsdom
import { createRef } from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { authenticateTestWorkspace } from './authTestUtils'
import { getAuthState } from './auth'
import { requestQuestionSpeech, VoicePlaybackError } from './interviewApi'
import QuestionVoice from './QuestionVoice'
import type { QuestionVoiceHandle } from './QuestionVoice'

vi.mock('./interviewApi', async (original) => ({ ...await original<typeof import('./interviewApi')>(), requestQuestionSpeech: vi.fn() }))
const request = vi.mocked(requestQuestionSpeech)
const createURL = vi.fn()
const revokeURL = vi.fn()
class AudioClip {
  static instances: AudioClip[] = []
  src: string
  currentTime = 0
  onended: (() => void) | null = null
  onerror: (() => void) | null = null
  play = vi.fn().mockResolvedValue(undefined)
  pause = vi.fn()
  removeAttribute(name: string) { if (name === 'src') this.src = '' }
  constructor(url: string) { this.src = url; AudioClip.instances.push(this) }
}
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (value: unknown) => void
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}
const audio = () => new Blob(['synthetic MP3'], { type: 'audio/mpeg' })
function props() {
  const auth = getAuthState()
  if (auth.status !== 'authenticated') throw new Error('Synthetic workspace required')
  return { authGeneration: auth.generation, sessionId: 'session-1', questionIndex: 0,
    question: 'The exact current question?', active: true, disabled: false }
}
beforeEach(async () => {
  await authenticateTestWorkspace()
  request.mockReset().mockResolvedValue(audio())
  AudioClip.instances = []
  createURL.mockReset().mockImplementation(() => `blob:voice-${createURL.mock.calls.length}`)
  revokeURL.mockReset()
  const NativeURL = URL
  vi.stubGlobal('URL', class extends NativeURL { static createObjectURL = createURL; static revokeObjectURL = revokeURL })
  vi.stubGlobal('Audio', AudioClip)
})
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks() })
async function play() {
  fireEvent.click(screen.getByRole('button', { name: 'Play question' }))
  await waitFor(() => expect(AudioClip.instances).toHaveLength(1))
  await waitFor(() => expect(AudioClip.instances[0].play).toHaveBeenCalledOnce())
}

test('mount does not synthesize; explicit Play uses only session/index/signal and Replay reuses one clip', async () => {
  const input = props()
  const { rerender, unmount } = render(<QuestionVoice {...input} />)
  expect(request).not.toHaveBeenCalled()
  rerender(<QuestionVoice {...input} />)
  expect(request).not.toHaveBeenCalled()
  await play()
  expect(request.mock.calls[0]).toEqual([input.sessionId, 0, expect.any(AbortSignal)])
  act(() => AudioClip.instances[0].onended?.())
  fireEvent.click(screen.getByRole('button', { name: 'Replay question' }))
  await waitFor(() => expect(AudioClip.instances[0].play).toHaveBeenCalledTimes(2))
  expect(request).toHaveBeenCalledOnce()
  expect(createURL).toHaveBeenCalledOnce()
  fireEvent.click(screen.getByRole('button', { name: 'Stop' }))
  expect(AudioClip.instances[0].pause).toHaveBeenCalledOnce()
  expect(screen.getByRole('button', { name: 'Replay question' })).toBeDefined()
  unmount()
  expect(revokeURL).toHaveBeenCalledExactlyOnceWith('blob:voice-1')
  expect(AudioClip.instances[0].src).toBe('')
})

test('synchronous duplicate clicks enter synthesis once, and Stop discards a late response that ignores AbortSignal', async () => {
  const pending = deferred<Blob>()
  request.mockReturnValue(pending.promise)
  render(<QuestionVoice {...props()} />)
  const button = screen.getByRole('button', { name: 'Play question' })
  act(() => { button.click(); button.click() })
  expect(request).toHaveBeenCalledOnce()
  expect(screen.getByText('Preparing question audio…')).toBeDefined()
  fireEvent.click(screen.getByRole('button', { name: 'Stop' }))
  expect(request.mock.calls[0][2].aborted).toBe(true)
  await act(async () => pending.resolve(audio()))
  expect(createURL).not.toHaveBeenCalled()
  expect(AudioClip.instances).toHaveLength(0)
  expect(screen.queryByRole('alert')).toBeNull()
})

test.each(['session', 'index', 'question', 'hidden', 'busy'])(
  '%s change aborts synthesis and discards a stale response without playback', async (change) => {
    const pending = deferred<Blob>()
    request.mockReturnValue(pending.promise)
    const input = props()
    const { rerender } = render(<QuestionVoice {...input} />)
    fireEvent.click(screen.getByRole('button', { name: 'Play question' }))
    const next = { ...input }
    if (change === 'session') next.sessionId = 'session-2'
    if (change === 'index') next.questionIndex = 1
    if (change === 'question') next.question = 'A replacement question?'
    if (change === 'hidden') next.active = false
    if (change === 'busy') next.disabled = true
    rerender(<QuestionVoice {...next} />)
    expect(request.mock.calls[0][2].aborted).toBe(true)
    await act(async () => pending.resolve(audio()))
    expect(createURL).not.toHaveBeenCalled()
    expect(request).toHaveBeenCalledOnce()
    if (change === 'hidden' || change === 'busy') expect((screen.getByRole('button', { name: 'Play question' }) as HTMLButtonElement).disabled).toBe(true)
  },
)

test.each(['session', 'index', 'question', 'hidden', 'busy'])(
  '%s change stops and revokes the previous clip without synthesizing another', async (change) => {
    const input = props()
    const { rerender, unmount } = render(<QuestionVoice {...input} />)
    await play()
    const next = { ...input }
    if (change === 'session') next.sessionId = 'session-2'
    if (change === 'index') next.questionIndex = 1
    if (change === 'question') next.question = 'A replacement question?'
    if (change === 'hidden') next.active = false
    if (change === 'busy') next.disabled = true
    rerender(<QuestionVoice {...next} />)
    expect(AudioClip.instances[0].pause).toHaveBeenCalledOnce()
    expect(revokeURL).toHaveBeenCalledExactlyOnceWith('blob:voice-1')
    expect(request).toHaveBeenCalledOnce()
    unmount()
    expect(revokeURL).toHaveBeenCalledOnce()
  },
)

test('account replacement immediately disposes audio owned by the previous workspace', async () => {
  render(<QuestionVoice {...props()} />)
  await play()
  await act(async () => authenticateTestWorkspace('context-B', 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'))
  expect(AudioClip.instances[0].pause).toHaveBeenCalledOnce()
  expect(revokeURL).toHaveBeenCalledOnce()
  expect((screen.getByRole('button', { name: 'Play question' }) as HTMLButtonElement).disabled).toBe(true)
  expect(request).toHaveBeenCalledOnce()
})

test('imperative invalidation synchronously aborts pending synthesis before another lifecycle action', async () => {
  const pending = deferred<Blob>()
  request.mockReturnValue(pending.promise)
  const handle = createRef<QuestionVoiceHandle>()
  render(<QuestionVoice {...props()} ref={handle} />)
  fireEvent.click(screen.getByRole('button', { name: 'Play question' }))
  act(() => handle.current!.invalidate())
  expect(request.mock.calls[0][2].aborted).toBe(true)
  await act(async () => pending.resolve(audio()))
  expect(createURL).not.toHaveBeenCalled()
})

test.each([502, 503, 504])('synthesis HTTP %i has fixed voice-specific UI and explicit retry only', async (status) => {
  request.mockRejectedValue(new VoicePlaybackError(status))
  render(<QuestionVoice {...props()} />)
  fireEvent.click(screen.getByRole('button', { name: 'Play question' }))
  expect((await screen.findByRole('alert')).textContent).toBe('Voice playback is unavailable right now.')
  expect(screen.queryByText('Authentication is temporarily unavailable. Please try again.')).toBeNull()
  expect(screen.queryByText('sensitive provider details')).toBeNull()
  expect(request).toHaveBeenCalledOnce()
  request.mockResolvedValue(audio())
  await play()
  expect(request).toHaveBeenCalledTimes(2)
})

test('browser voice fallback reads the current question without retrying ElevenLabs', async () => {
  class Utterance {
    onend: (() => void) | null = null
    onerror: (() => void) | null = null
    readonly text: string
    constructor(text: string) { this.text = text }
  }
  const speech = { cancel: vi.fn(), speak: vi.fn() }
  vi.stubGlobal('SpeechSynthesisUtterance', Utterance)
  vi.stubGlobal('speechSynthesis', speech)
  request.mockRejectedValue(new VoicePlaybackError(502))
  render(<QuestionVoice {...props()} />)
  fireEvent.click(screen.getByRole('button', { name: 'Play question' }))
  await screen.findByRole('alert')
  fireEvent.click(screen.getByRole('button', { name: 'Use browser voice' }))
  expect(speech.speak).toHaveBeenCalledOnce()
  const utterance = speech.speak.mock.calls[0][0] as Utterance
  expect(utterance.text).toBe('The exact current question?')
  expect(screen.getByText('Reading the question with your browser voice…')).toBeDefined()
  act(() => utterance.onend?.())
  expect(request).toHaveBeenCalledOnce()
  expect(screen.queryByRole('alert')).toBeNull()
  expect(screen.queryByRole('button', { name: 'Use browser voice' })).toBeNull()
  expect(screen.getByRole('button', { name: 'Play question' })).toBeDefined()
})

test('Audio.play rejection does not retry synthesis; an explicit Replay can use the same clip', async () => {
  render(<QuestionVoice {...props()} />)
  // Install the rejection at construction rather than depending on browser policy.
  vi.stubGlobal('Audio', class extends AudioClip {
    constructor(url: string) { super(url); this.play.mockRejectedValueOnce(new DOMException('Blocked', 'NotAllowedError')) }
  })
  fireEvent.click(screen.getByRole('button', { name: 'Play question' }))
  expect((await screen.findByRole('alert')).textContent).toBe('Voice playback is unavailable right now.')
  expect(request).toHaveBeenCalledOnce()
  AudioClip.instances[0].play.mockResolvedValue(undefined)
  fireEvent.click(screen.getByRole('button', { name: 'Replay question' }))
  await waitFor(() => expect(AudioClip.instances[0].play).toHaveBeenCalledTimes(2))
  expect(request).toHaveBeenCalledOnce()
})

test('a late rejected play promise cannot replace a newer request state', async () => {
  const oldPlay = deferred<void>()
  vi.stubGlobal('Audio', class extends AudioClip {
    constructor(url: string) { super(url); this.play.mockReturnValueOnce(oldPlay.promise) }
  })
  const handle = createRef<QuestionVoiceHandle>()
  render(<QuestionVoice {...props()} ref={handle} />)
  await play()
  act(() => handle.current!.invalidate())
  const pending = deferred<Blob>()
  request.mockReturnValue(pending.promise)
  fireEvent.click(screen.getByRole('button', { name: 'Play question' }))
  await act(async () => oldPlay.reject(new DOMException('Old playback', 'AbortError')))
  expect(screen.getByText('Preparing question audio…')).toBeDefined()
  expect(screen.queryByRole('alert')).toBeNull()
})

test('Stop followed by Replay ignores the old rejected play promise and reuses the same clip', async () => {
  const firstPlay = deferred<void>()
  vi.stubGlobal('Audio', class extends AudioClip {
    constructor(url: string) { super(url); this.play.mockReturnValueOnce(firstPlay.promise) }
  })
  render(<QuestionVoice {...props()} />)
  await play()
  fireEvent.click(screen.getByRole('button', { name: 'Stop' }))
  fireEvent.click(screen.getByRole('button', { name: 'Replay question' }))
  await waitFor(() => expect(AudioClip.instances[0].play).toHaveBeenCalledTimes(2))
  await act(async () => firstPlay.reject(new DOMException('Old activation result', 'NotAllowedError')))
  expect(screen.getByText('Playing question…')).toBeDefined()
  expect(screen.queryByRole('alert')).toBeNull()
  expect(request).toHaveBeenCalledOnce()
  act(() => AudioClip.instances[0].onended?.())
  expect((screen.getByRole('button', { name: 'Replay question' }) as HTMLButtonElement).disabled).toBe(false)
})

test('unmount aborts pending synthesis and a later response never allocates an object URL', async () => {
  const pending = deferred<Blob>()
  request.mockReturnValue(pending.promise)
  const { unmount } = render(<QuestionVoice {...props()} />)
  fireEvent.click(screen.getByRole('button', { name: 'Play question' }))
  unmount()
  expect(request.mock.calls[0][2].aborted).toBe(true)
  await act(async () => pending.resolve(audio()))
  expect(createURL).not.toHaveBeenCalled()
  expect(revokeURL).not.toHaveBeenCalled()
})

test('account change during synthesis discards a late result without an audio allocation', async () => {
  const pending = deferred<Blob>()
  request.mockReturnValue(pending.promise)
  render(<QuestionVoice {...props()} />)
  fireEvent.click(screen.getByRole('button', { name: 'Play question' }))
  await act(async () => authenticateTestWorkspace('context-B', 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'))
  expect(request.mock.calls[0][2].aborted).toBe(true)
  await act(async () => pending.resolve(audio()))
  expect(createURL).not.toHaveBeenCalled()
  expect(AudioClip.instances).toHaveLength(0)
})
