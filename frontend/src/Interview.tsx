import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import AudioAnswer from './AudioAnswer'
import Comparison from './AttemptComparison'
import {
  ApiError, continueQuestion, getAttempts, getComparison, getSession,
  isConflictError, startInterview, submitAttempt,
} from './interviewApi'
import type { Attempt, AttemptComparison, InterviewSession } from './interviewApi'

const SESSION_KEY = 'rehearse.session_id'
type Mode = 'composing' | 'review'
interface SavedView {
  session: InterviewSession
  attempts: Attempt[]
  comparison: AttemptComparison | null
  mode: Mode
}
interface Recovery {
  sessionId: string
  questionIndex?: number
  revision?: number
  kind: 'submit' | 'continue' | 'transcription' | 'conflict' | 'restore' | 'review'
}

function storedSessionId(): string | null {
  try { return sessionStorage.getItem(SESSION_KEY) } catch { return null }
}
function rememberSession(id: string | null) {
  // Restore only an opaque session ID in this tab, never drafts or recordings.
  try {
    if (id === null) sessionStorage.removeItem(SESSION_KEY)
    else sessionStorage.setItem(SESSION_KEY, id)
  } catch { /* The current interview works if browser storage is disabled. */ }
}
function recoveryFor(view: SavedView, kind: Recovery['kind']): Recovery {
  return {
    sessionId: view.session.id, questionIndex: view.session.current_question_index,
    revision: view.session.current_question_latest_attempt_number, kind,
  }
}
async function readSavedView(id: string): Promise<SavedView> {
  // Recheck reads if a concurrent append changes facts between endpoints.
  // This never repeats a mutation or treats list length as a revision.
  for (let read = 0; read < 2; read += 1) {
    const session = await getSession(id)
    if (session.status === 'completed') return { session, attempts: [], comparison: null, mode: 'review' }
    const attempts = await getAttempts(session)
    const latest = attempts.at(-1)
    if ((latest?.attempt_number ?? 0) !== session.current_question_latest_attempt_number) continue
    const comparison = attempts.length >= 2 ? await getComparison(session) : null
    if (comparison !== null && comparison.comparison !== null &&
        (comparison.after_attempt?.id !== latest?.id || comparison.before_attempt?.id !== attempts[0]?.id)) continue
    const confirmed = await getSession(id)
    if (confirmed.status !== session.status || confirmed.current_question_index !== session.current_question_index ||
        confirmed.current_question_latest_attempt_number !== session.current_question_latest_attempt_number) continue
    return { session: confirmed, attempts, comparison, mode: latest ? 'review' : 'composing' }
  }
  throw new ApiError('The saved interview changed while loading. Recheck saved state.', 409)
}

interface Props {
  onSessionAccess?: (sessionId: string) => void
  onNavigationBusyChange?: (busy: boolean) => void
  onHistoryFactsChange?: () => void
}

export default function Interview({ onSessionAccess, onNavigationBusyChange, onHistoryFactsChange }: Props = {}) {
  const [restoreId] = useState(storedSessionId)
  const [view, setView] = useState<SavedView | null>(null)
  const [draft, setDraft] = useState<{ text: string; measurementId: string | null }>({ text: '', measurementId: null })
  const [draftGeneration, setDraftGeneration] = useState(0)
  const [operation, setOperation] = useState<string | null>(restoreId ? 'Restoring interview…' : null)
  const [transcribing, setTranscribing] = useState(false)
  const [audioBusy, setAudioBusy] = useState(false)
  const [error, setError] = useState('')
  const [recovery, setRecovery] = useState<Recovery | null>(null)
  const locked = useRef(Boolean(restoreId))
  const mounted = useRef(true)
  const accessCallback = useRef(onSessionAccess)
  useLayoutEffect(() => { accessCallback.current = onSessionAccess }, [onSessionAccess])
  const factsCallback = useRef(onHistoryFactsChange)
  useLayoutEffect(() => { factsCallback.current = onHistoryFactsChange }, [onHistoryFactsChange])
  const session = view?.session
  const blocked = operation !== null || transcribing || recovery !== null
  const navigationBlocked = blocked || audioBusy

  useLayoutEffect(() => {
    onNavigationBusyChange?.(navigationBlocked)
  }, [navigationBlocked, onNavigationBusyChange])

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])
  useEffect(() => {
    if (!restoreId) return
    let active = true
    void readSavedView(restoreId).then((saved) => {
      if (active) {
        setView(saved)
        accessCallback.current?.(saved.session.id)
      }
    }).catch((cause: unknown) => {
      if (!active) return
      if (cause instanceof ApiError && cause.status === 404) {
        rememberSession(null)
        setError('The saved interview was not found. Start a new interview.')
      } else {
        setRecovery({ sessionId: restoreId, kind: 'restore' })
        setError('Unable to restore the interview. Recheck saved state before continuing.')
      }
    }).finally(() => {
      if (active) { locked.current = false; setOperation(null) }
    })
    return () => { active = false }
  }, [restoreId])

  function clearDraft() {
    setDraft({ text: '', measurementId: null })
    setDraftGeneration((current) => current + 1)
    setTranscribing(false)
  }
  function install(saved: SavedView) {
    setView(saved)
    rememberSession(saved.session.id)
    clearDraft()
  }
  async function start() {
    if (locked.current || transcribing) return
    locked.current = true
    setOperation('Starting…')
    setError('')
    try {
      const created = await startInterview()
      if (!mounted.current) return
      install({ session: created, attempts: [], comparison: null, mode: 'composing' })
      accessCallback.current?.(created.id)
      factsCallback.current?.()
      setRecovery(null)
    } catch (cause) {
      if (mounted.current) setError(cause instanceof Error ? cause.message : 'Unable to start interview.')
    } finally {
      if (mounted.current) { locked.current = false; setOperation(null) }
    }
  }
  async function reconcileConflict(context: Recovery) {
    clearDraft()
    try {
      const saved = await readSavedView(context.sessionId)
      if (!mounted.current) return
      install(saved)
      factsCallback.current?.()
      setRecovery(null)
      setError('The interview changed. Reloaded the saved state; review it before continuing.')
    } catch {
      if (!mounted.current) return
      setRecovery({ ...context, kind: 'conflict' })
      setError('The interview changed. Unable to reload it; recheck saved state before continuing.')
    }
  }
  async function mutate(kind: 'submit' | 'continue') {
    if (!view || locked.current || blocked || session?.status !== 'active') return
    if (kind === 'submit' && (view.mode !== 'composing' || !draft.text.trim())) return
    if (kind === 'continue' && (view.mode !== 'review' || !view.attempts.length)) return
    const context = recoveryFor(view, kind)
    locked.current = true
    setOperation(kind === 'submit' ? 'Submitting…' : 'Continuing…')
    setError('')
    let acknowledged = false
    try {
      if (kind === 'submit') {
        const result = await submitAttempt(view.session, draft.text.trim(), draft.measurementId)
        acknowledged = true
        if (!mounted.current) return
        install({ session: result.session, attempts: [...view.attempts, result.attempt], comparison: null, mode: 'review' })
        factsCallback.current?.()
      } else {
        const updated = await continueQuestion(view.session)
        acknowledged = true
        if (!mounted.current) return
        install({ session: updated, attempts: [], comparison: null, mode: 'composing' })
        factsCallback.current?.()
      }
      const saved = await readSavedView(view.session.id)
      if (mounted.current) { install(saved); setRecovery(null) }
    } catch (cause) {
      if (!mounted.current) return
      if (!acknowledged && isConflictError(cause)) {
        await reconcileConflict(context)
      } else if (acknowledged || !(cause instanceof ApiError) || cause.ambiguousWrite ||
                 (cause.status !== null && cause.status >= 500)) {
        setRecovery({ ...context, kind: acknowledged ? 'review' : kind })
        setError(acknowledged
          ? 'Your change was saved, but the review could not be loaded. Recheck saved state.'
          : 'The request result is unknown. Recheck saved state before trying again.')
      } else {
        setError(cause.message)
      }
    } finally {
      if (mounted.current) { locked.current = false; setOperation(null) }
    }
  }
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    void mutate('submit')
  }
  function retry() {
    if (!view || locked.current || blocked) return
    clearDraft()
    setError('')
    setView({ ...view, mode: 'composing' })
  }
  function cancelRetry() {
    if (!view || locked.current || blocked) return
    clearDraft()
    setError('')
    setView({ ...view, mode: 'review' })
  }
  async function recheck() {
    if (!recovery || locked.current) return
    locked.current = true
    setOperation('Rechecking saved state…')
    try {
      const saved = await readSavedView(recovery.sessionId)
      if (!mounted.current) return
      const unchangedDraft = (recovery.kind === 'submit' || recovery.kind === 'transcription') &&
        saved.session.status === 'active' && saved.session.current_question_index === recovery.questionIndex &&
        saved.session.current_question_latest_attempt_number === recovery.revision
      if (unchangedDraft) setView({ ...saved, mode: 'composing' })
      else install(saved)
      if (recovery.kind === 'restore') accessCallback.current?.(saved.session.id)
      // Recovery reads may reveal a committed write whose response was lost.
      // A standalone unlinked transcription does not change history summaries.
      if (recovery.kind !== 'transcription' || !unchangedDraft) factsCallback.current?.()
      setRecovery(null)
      setError('Saved state rechecked. Choose your next action; no request was resubmitted.')
    } catch {
      if (mounted.current) setError('Unable to recheck saved state. Check your connection and recheck before continuing.')
    } finally {
      if (mounted.current) { locked.current = false; setOperation(null) }
    }
  }
  async function audioConflict() {
    if (!view || locked.current) return
    locked.current = true
    setOperation('Reloading saved state…')
    try { await reconcileConflict(recoveryFor(view, 'conflict')) }
    finally { if (mounted.current) { locked.current = false; setOperation(null) } }
  }
  function uncertainTranscription() {
    if (!view) return
    clearDraft()
    setRecovery(recoveryFor(view, 'transcription'))
    setError('The transcription result is unknown. Recheck saved state before continuing.')
  }

  return (
    <section className="interview" aria-label="Interview practice" aria-busy={operation !== null}>
      {operation && <p role="status">{operation}</p>}
      {!session && (
        <button type="button" onClick={() => void start()} disabled={operation !== null || transcribing}>Start Interview</button>
      )}
      {session?.status === 'active' && view && (
        <>
          <p aria-live="polite">Question {session.current_question_index + 1} of {session.questions.length}</p>
          <h2 id="current-question" aria-live="polite">{session.current_question}</h2>
          {view.mode === 'composing' && (
            <form onSubmit={submit}>
              <AudioAnswer key={`${session.id}:${session.current_question_index}:${draftGeneration}`}
                session={session} disabled={blocked} hasAnswer={draft.text.length > 0}
                onBusyChange={setAudioBusy}
                onTranscribing={setTranscribing} onConflict={() => void audioConflict()}
                onUncertainTranscription={uncertainTranscription}
                onTranscript={(text, measurementId) => setDraft((current) => current.text === '' ? { text, measurementId } : current)}
                onInvalidateMeasurement={() => setDraft((current) => ({ ...current, measurementId: null }))} />
              <label htmlFor="answer">Your answer</label>
              <textarea id="answer" aria-describedby="current-question" value={draft.text}
                onChange={(event) => {
                  const text = event.target.value
                  setDraft((current) => ({ text, measurementId: current.measurementId }))
                }} rows={6} maxLength={10000} required disabled={blocked} />
              <div className="attempt-actions">
                <button type="submit" disabled={blocked || !draft.text.trim()}>Submit Attempt</button>
                {view.attempts.length > 0 && <button type="button" onClick={cancelRetry} disabled={blocked}>Cancel Retry</button>}
              </div>
            </form>
          )}
          {view.attempts.length > 0 && (
            <section className="saved-attempts" aria-label="Saved attempts">
              <h3>Saved attempts</h3>
              {view.attempts.map((attempt) => (
                <article key={attempt.id}>
                  <h4>Attempt {attempt.attempt_number}</h4>
                  <p className="saved-answer">{attempt.answer}</p>
                </article>
              ))}
            </section>
          )}
          {view.mode === 'review' && (
            <>
              {view.comparison && <Comparison comparison={view.comparison} />}
              <div className="attempt-actions">
                <button type="button" onClick={retry} disabled={blocked}>{view.attempts.length > 1 ? 'Retry Again' : 'Retry'}</button>
                <button type="button" onClick={() => void mutate('continue')} disabled={blocked || view.attempts.length === 0}>Continue</button>
              </div>
            </>
          )}
        </>
      )}
      {session?.status === 'completed' && (
        <div role="status">
          <h2>Interview Complete</h2>
          <p>You completed all {session.questions.length} questions.</p>
          <button type="button" onClick={() => void start()} disabled={operation !== null || transcribing}>Start New Interview</button>
        </div>
      )}
      {error && (
        <div>
          <p role="alert">{error}</p>
          {recovery && <button type="button" onClick={() => void recheck()} disabled={operation !== null}>Recheck saved state</button>}
          {session?.status === 'active' && (
            <button type="button" onClick={() => void start()} disabled={operation !== null || transcribing}>Start New Interview</button>
          )}
        </div>
      )}
    </section>
  )
}
