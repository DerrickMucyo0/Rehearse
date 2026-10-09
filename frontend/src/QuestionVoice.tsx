import { forwardRef, useCallback, useImperativeHandle, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react'
import { getAuthState, isAuthWorkspaceCurrent, subscribeAuth } from './auth'
import { requestQuestionSpeech } from './interviewApi'

export interface QuestionVoiceHandle { invalidate: () => void }
interface Props {
  authGeneration: number
  sessionId: string
  questionIndex: number
  question: string
  active: boolean
  disabled: boolean
}
type Phase = 'idle' | 'loading' | 'playing' | 'fallback-playing' | 'ready' | 'error'
const UNAVAILABLE = 'Voice playback is unavailable right now.'

// The current question's clip is transient. It never participates in attempts,
// diagnosis, navigation locks, or persisted-write recovery.
const QuestionVoice = forwardRef<QuestionVoiceHandle, Props>(function QuestionVoice(
  { authGeneration, sessionId, questionIndex, question, active, disabled }, ref,
) {
  const auth = useSyncExternalStore(subscribeAuth, getAuthState)
  const authIdentity = auth.status === 'authenticated' ? auth.generation : auth.status
  const [phase, setPhase] = useState<Phase>('idle')
  const [hasClip, setHasClip] = useState(false)
  const generation = useRef(0)
  const pending = useRef<AbortController | null>(null)
  const clip = useRef<{ url: string; audio: HTMLAudioElement } | null>(null)
  const browserUtterance = useRef<SpeechSynthesisUtterance | null>(null)
  const locked = useRef(false)
  const mounted = useRef(false)
  const owner = useRef({ authGeneration, sessionId, questionIndex, question, active, disabled })

  const invalidate = useCallback(() => {
    generation.current += 1
    locked.current = false
    pending.current?.abort()
    pending.current = null
    if (browserUtterance.current) {
      window.speechSynthesis?.cancel()
      browserUtterance.current.onend = null
      browserUtterance.current.onerror = null
      browserUtterance.current = null
    }
    const previous = clip.current
    clip.current = null
    if (previous) {
      previous.audio.onended = null
      previous.audio.onerror = null
      previous.audio.pause()
      previous.audio.removeAttribute('src')
      URL.revokeObjectURL(previous.url)
    }
    if (mounted.current) { setPhase('idle'); setHasClip(false) }
  }, [])
  useImperativeHandle(ref, () => ({ invalidate }), [invalidate])
  useLayoutEffect(() => {
    mounted.current = true
    return () => { mounted.current = false; invalidate() }
  }, [invalidate])
  useLayoutEffect(() => {
    // Publish ownership before any event or asynchronous response can act on it.
    owner.current = { authGeneration, sessionId, questionIndex, question, active, disabled }
    invalidate()
  }, [authGeneration, sessionId, questionIndex, question, active, disabled, authIdentity, invalidate])

  async function play() {
    const target = owner.current
    if (locked.current || !target.active || target.disabled || !isAuthWorkspaceCurrent(target.authGeneration)) return
    locked.current = true
    const revision = generation.current
    const current = () => mounted.current && revision === generation.current &&
      owner.current.authGeneration === target.authGeneration && owner.current.sessionId === target.sessionId &&
      owner.current.questionIndex === target.questionIndex && owner.current.question === target.question &&
      owner.current.active && !owner.current.disabled && isAuthWorkspaceCurrent(target.authGeneration)
    try {
      if (!clip.current) {
        const controller = new AbortController()
        pending.current = controller
        setPhase('loading')
        const blob = await requestQuestionSpeech(target.sessionId, target.questionIndex, controller.signal)
        if (!current() || controller.signal.aborted) return
        pending.current = null
        const url = URL.createObjectURL(blob)
        try { clip.current = { url, audio: new Audio(url) }; setHasClip(true) }
        catch { URL.revokeObjectURL(url); throw new Error(UNAVAILABLE) }
      }
      if (!current()) return
      const audio = clip.current!.audio
      audio.currentTime = 0
      audio.onended = () => { if (current() && clip.current?.audio === audio) { locked.current = false; setPhase('ready') } }
      audio.onerror = () => { if (current() && clip.current?.audio === audio) { locked.current = false; setPhase('error') } }
      setPhase('playing')
      await audio.play()
      // An old play promise may settle after a new playback; never install its state.
      if (!current()) return
    } catch {
      if (current()) { pending.current = null; locked.current = false; setPhase('error') }
    }
  }
  function playWithBrowserVoice() {
    const target = owner.current
    if (locked.current || !target.active || target.disabled || !isAuthWorkspaceCurrent(target.authGeneration)) return
    if (typeof window === 'undefined' || !window.speechSynthesis || typeof SpeechSynthesisUtterance === 'undefined') return

    locked.current = true
    const revision = generation.current
    const current = () => mounted.current && revision === generation.current &&
      owner.current.authGeneration === target.authGeneration && owner.current.sessionId === target.sessionId &&
      owner.current.questionIndex === target.questionIndex && owner.current.question === target.question &&
      owner.current.active && !owner.current.disabled && isAuthWorkspaceCurrent(target.authGeneration)
    try {
      const utterance = new SpeechSynthesisUtterance(target.question)
      browserUtterance.current = utterance
      utterance.onend = () => {
        if (current() && browserUtterance.current === utterance) {
          browserUtterance.current = null
          locked.current = false
          setPhase('idle')
        }
      }
      utterance.onerror = () => {
        if (current() && browserUtterance.current === utterance) {
          browserUtterance.current = null
          locked.current = false
          setPhase('error')
        }
      }
      setPhase('fallback-playing')
      window.speechSynthesis.speak(utterance)
    } catch {
      browserUtterance.current = null
      locked.current = false
      setPhase('error')
    }
  }
  function stop() {
    generation.current += 1
    pending.current?.abort()
    pending.current = null
    const stoppedBrowserVoice = browserUtterance.current !== null
    if (browserUtterance.current) {
      window.speechSynthesis?.cancel()
      browserUtterance.current.onend = null
      browserUtterance.current.onerror = null
      browserUtterance.current = null
    }
    locked.current = false
    if (clip.current) {
      clip.current.audio.pause()
      clip.current.audio.onended = null
      clip.current.audio.onerror = null
    }
    setPhase(clip.current ? 'ready' : stoppedBrowserVoice ? 'error' : 'idle')
  }
  const unavailable = !active || disabled || !isAuthWorkspaceCurrent(authGeneration)
  const canUseBrowserVoice = typeof window !== 'undefined' &&
    'speechSynthesis' in window && typeof SpeechSynthesisUtterance !== 'undefined'
  return <section aria-label="Question voice" aria-busy={phase === 'loading' || phase === 'fallback-playing'}>
    <button type="button" disabled={unavailable || phase === 'loading' || phase === 'playing'} onClick={() => void play()}>
      {hasClip ? 'Replay question' : 'Play question'}
    </button>
    {(phase === 'loading' || phase === 'playing' || phase === 'fallback-playing') && <button type="button" onClick={stop}>Stop</button>}
    {phase === 'loading' && <p role="status">Preparing question audio…</p>}
    {phase === 'playing' && <p role="status">Playing question…</p>}
    {phase === 'fallback-playing' && <p role="status">Reading the question with your browser voice…</p>}
    {phase === 'error' && <p role="alert">{UNAVAILABLE}</p>}
    {phase === 'error' && canUseBrowserVoice && <button type="button" disabled={unavailable} onClick={playWithBrowserVoice}>Use browser voice</button>}
  </section>
})
export default QuestionVoice
