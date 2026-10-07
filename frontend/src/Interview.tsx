import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import AudioAnswer from './AudioAnswer'
import Comparison from './AttemptComparison'
import InterviewSummary from './InterviewSummary.tsx'
import QuestionVoice from './QuestionVoice'
import type { QuestionVoiceHandle } from './QuestionVoice'
import {
  ApiError, continueQuestion, getAttempts, getComparison, getSession,
  getSemanticDiagnosis, isConflictError, preparesNextQuestion, RoleplayUnavailableError, SemanticDiagnosisError, startInterview, submitAttempt,
} from './interviewApi'
import type { Attempt, AttemptComparison, InterviewSession, SemanticDiagnosis } from './interviewApi'
import { personalizedDrillForFocus } from './personalizedDrills'
import { getAuthState, isAuthWorkspaceCurrent } from './auth'
import { SCENARIOS, scenarioLabel } from './scenarios'
import type { ScenarioType } from './scenarios'

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

type DiagnosisTarget = {
  sessionId: string
  questionIndex: number
  attemptNumber: number
  attemptId: string
}
type DiagnosisState =
  | { status: 'idle' }
  | { status: 'loading'; target: DiagnosisTarget }
  | { status: 'success'; target: DiagnosisTarget; diagnosis: SemanticDiagnosis }
  | { status: 'error'; target: DiagnosisTarget; message: string }

const addressedLabels: Record<SemanticDiagnosis['addressed_question'], string> = {
  yes: 'Yes', partially: 'Partially', no: 'No',
}
const structureLabels: Record<SemanticDiagnosis['structure'], string> = {
  clear: 'Clear', mixed: 'Mixed', unclear: 'Unclear', insufficient_content: 'Not enough content',
}
const focusLabels: Record<SemanticDiagnosis['next_focus'], string> = {
  answer_the_question: 'Answer the question', specificity: 'Specificity', supporting_detail: 'Supporting detail',
  structure: 'Structure', completeness: 'Completeness', conciseness: 'Conciseness', maintain_strengths: 'Maintain strengths',
}

function ownsDiagnosis(view: SavedView | null, target: DiagnosisTarget): boolean {
  const latest = view?.attempts.at(-1)
  return view?.mode === 'review' && view.session.status === 'active' &&
    view.session.id === target.sessionId && view.session.current_question_index === target.questionIndex &&
    view.session.current_question_latest_attempt_number === target.attemptNumber &&
    latest?.id === target.attemptId && latest.question_index === target.questionIndex &&
    latest.attempt_number === target.attemptNumber
}

function storedSessionId(key: string | null): string | null {
  if (key === null) return null
  try { return sessionStorage.getItem(key) } catch { return null }
}
function rememberSession(key: string | null, id: string | null) {
  if (key === null) return
  // Only an opaque ID is retained in this tab, scoped to the server-bootstrap
  // user. It never establishes membership/ownership; every restore is secured.
  try {
    if (id === null) sessionStorage.removeItem(key)
    else sessionStorage.setItem(key, id)
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
  active?: boolean
  onSessionAccess?: (sessionId: string) => void
  onNavigationBusyChange?: (busy: boolean) => void
  onHistoryFactsChange?: () => void
}

export default function Interview({ active = true, onSessionAccess, onNavigationBusyChange, onHistoryFactsChange }: Props = {}) {
  const [workspace] = useState(() => {
    const auth = getAuthState()
    return auth.status === 'authenticated' ? { generation: auth.generation, storageKey: `${SESSION_KEY}:${auth.userId}` } : null
  })
  const [restoreId] = useState(() => storedSessionId(workspace?.storageKey ?? null))
  const [view, setView] = useState<SavedView | null>(null)
  const [selectedScenario, setSelectedScenario] = useState<ScenarioType>('job_interview')
  const [draft, setDraft] = useState<{ text: string; measurementId: string | null }>({ text: '', measurementId: null })
  const [draftGeneration, setDraftGeneration] = useState(0)
  const [operation, setOperation] = useState<string | null>(restoreId ? 'Restoring interview…' : null)
  const [transcribing, setTranscribing] = useState(false)
  const [audioBusy, setAudioBusy] = useState(false)
  const [error, setError] = useState('')
  const [recovery, setRecovery] = useState<Recovery | null>(null)
  const [diagnosis, setDiagnosis] = useState<DiagnosisState>({ status: 'idle' })
  const currentView = useRef<SavedView | null>(null)
  const voice = useRef<QuestionVoiceHandle | null>(null)
  const diagnosisController = useRef<AbortController | null>(null)
  const continueController = useRef<AbortController | null>(null)
  const diagnosisGeneration = useRef(0)
  const diagnosisOwner = useRef<DiagnosisTarget | null>(null)
  const locked = useRef(Boolean(restoreId))
  const mounted = useRef(true)
  const accessCallback = useRef(onSessionAccess)
  useLayoutEffect(() => { accessCallback.current = onSessionAccess }, [onSessionAccess])
  const factsCallback = useRef(onHistoryFactsChange)
  useLayoutEffect(() => { factsCallback.current = onHistoryFactsChange }, [onHistoryFactsChange])
  const currentWorkspace = useCallback(() => mounted.current && workspace !== null &&
    isAuthWorkspaceCurrent(workspace.generation), [workspace])
  const session = view?.session
  const blocked = operation !== null || transcribing || recovery !== null
  const navigationBlocked = blocked || audioBusy
  const feedback = diagnosis.status !== 'idle' && ownsDiagnosis(view, diagnosis.target) ? diagnosis : null
  const drill = feedback?.status === 'success' ? personalizedDrillForFocus(feedback.diagnosis.next_focus) : null

  useLayoutEffect(() => {
    onNavigationBusyChange?.(navigationBlocked)
  }, [navigationBlocked, onNavigationBusyChange])

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      continueController.current?.abort()
      invalidateDiagnosis()
      if (workspace && !isAuthWorkspaceCurrent(workspace.generation)) rememberSession(workspace.storageKey, null)
    }
  }, [workspace])
  useEffect(() => {
    if (!restoreId) return
    let active = true
    void readSavedView(restoreId).then((saved) => {
      if (active && currentWorkspace()) {
        showView(saved)
        accessCallback.current?.(saved.session.id)
      }
    }).catch((cause: unknown) => {
      if (!active || !currentWorkspace()) return
      if (cause instanceof ApiError && cause.status === 404) {
        rememberSession(workspace?.storageKey ?? null, null)
        setError('The saved interview was not found. Start a new interview.')
      } else {
        setRecovery({ sessionId: restoreId, kind: 'restore' })
        setError('Unable to restore the interview. Recheck saved state before continuing.')
      }
    }).finally(() => {
      if (active && currentWorkspace()) { locked.current = false; setOperation(null) }
    })
    return () => { active = false }
  }, [restoreId, workspace, currentWorkspace])

  function invalidateDiagnosis() {
    diagnosisGeneration.current += 1
    const pending = diagnosisController.current
    diagnosisController.current = null
    diagnosisOwner.current = null
    pending?.abort()
    if (mounted.current) setDiagnosis({ status: 'idle' })
  }
  function showView(saved: SavedView) {
    const previous = currentView.current?.session
    if (previous?.id !== saved.session.id || previous?.current_question_index !== saved.session.current_question_index ||
        previous?.current_question !== saved.session.current_question || saved.session.status !== 'active') voice.current?.invalidate()
    currentView.current = saved
    if (diagnosisOwner.current && !ownsDiagnosis(saved, diagnosisOwner.current)) invalidateDiagnosis()
    setView(saved)
  }
  function requestDiagnosis(target: DiagnosisTarget) {
    if (!currentWorkspace() || !ownsDiagnosis(currentView.current, target)) return
    invalidateDiagnosis()
    const pending = new AbortController()
    const generation = diagnosisGeneration.current
    diagnosisController.current = pending
    diagnosisOwner.current = target
    setDiagnosis({ status: 'loading', target })
    const current = () => currentWorkspace() && !pending.signal.aborted &&
      diagnosisGeneration.current === generation && diagnosisOwner.current === target &&
      ownsDiagnosis(currentView.current, target)
    void getSemanticDiagnosis(target.sessionId, target.questionIndex, target.attemptNumber, pending.signal)
      .then((result) => {
        if (current()) setDiagnosis({ status: 'success', target, diagnosis: result })
      }).catch((cause: unknown) => {
        if (!current() || (cause instanceof DOMException && cause.name === 'AbortError')) return
        setDiagnosis({ status: 'error', target, message: cause instanceof SemanticDiagnosisError
          ? cause.message : 'Unable to load feedback right now. You can still retry or continue.' })
      }).finally(() => {
        if (current() && diagnosisController.current === pending) diagnosisController.current = null
      })
  }

  function clearDraft() {
    setDraft({ text: '', measurementId: null })
    setDraftGeneration((current) => current + 1)
    setTranscribing(false)
  }
  function install(saved: SavedView) {
    showView(saved)
    rememberSession(workspace?.storageKey ?? null, saved.session.id)
    clearDraft()
  }
  async function start() {
    if (locked.current || transcribing) return
    voice.current?.invalidate()
    invalidateDiagnosis()
    locked.current = true
    setOperation('Starting…')
    setError('')
    try {
      const created = await startInterview(selectedScenario)
      if (!currentWorkspace()) return
      install({ session: created, attempts: [], comparison: null, mode: 'composing' })
      accessCallback.current?.(created.id)
      factsCallback.current?.()
      setRecovery(null)
    } catch (cause) {
      if (currentWorkspace()) setError(cause instanceof Error ? cause.message : 'Unable to start interview.')
    } finally {
      if (currentWorkspace()) { locked.current = false; setOperation(null) }
    }
  }
  async function reconcileConflict(context: Recovery) {
    voice.current?.invalidate()
    invalidateDiagnosis()
    clearDraft()
    try {
      const saved = await readSavedView(context.sessionId)
      if (!currentWorkspace()) return
      install(saved)
      factsCallback.current?.()
      setRecovery(null)
      setError('The interview changed. Reloaded the saved state; review it before continuing.')
    } catch {
      if (!currentWorkspace()) return
      setRecovery({ ...context, kind: 'conflict' })
      setError('The interview changed. Unable to reload it; recheck saved state before continuing.')
    }
  }
  async function mutate(kind: 'submit' | 'continue') {
    if (!view || locked.current || blocked || session?.status !== 'active') return
    if (kind === 'submit' && (view.mode !== 'composing' || !draft.text.trim())) return
    if (kind === 'continue' && (view.mode !== 'review' || !view.attempts.length)) return
    voice.current?.invalidate()
    invalidateDiagnosis()
    const context = recoveryFor(view, kind)
    const controller = kind === 'continue' ? new AbortController() : null
    if (controller) continueController.current = controller
    locked.current = true
    setOperation(kind === 'submit' ? 'Submitting…' : preparesNextQuestion(view.session) ? 'Preparing next question…' : 'Continuing…')
    setError('')
    let acknowledged = false
    let submittedTarget: DiagnosisTarget | null = null
    let diagnosisAfterSubmit: DiagnosisTarget | null = null
    try {
      if (kind === 'submit') {
        const result = await submitAttempt(view.session, draft.text.trim(), draft.measurementId)
        acknowledged = true
        if (!currentWorkspace()) return
        submittedTarget = { sessionId: result.session.id, questionIndex: result.attempt.question_index,
          attemptNumber: result.attempt.attempt_number, attemptId: result.attempt.id }
        install({ session: result.session, attempts: [...view.attempts, result.attempt], comparison: null, mode: 'review' })
        factsCallback.current?.()
      } else {
        const updated = await continueQuestion(view.session, controller?.signal)
        acknowledged = true
        if (!currentWorkspace()) return
        install({ session: updated, attempts: [], comparison: null, mode: 'composing' })
        factsCallback.current?.()
      }
      const saved = await readSavedView(view.session.id)
      if (currentWorkspace()) {
        install(saved)
        setRecovery(null)
        if (submittedTarget && ownsDiagnosis(saved, submittedTarget)) diagnosisAfterSubmit = submittedTarget
      }
    } catch (cause) {
      if (!currentWorkspace()) return
      if (!acknowledged && kind === 'continue' && cause instanceof RoleplayUnavailableError) {
        setError(cause.message)
      } else if (!acknowledged && isConflictError(cause)) {
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
      if (continueController.current === controller) continueController.current = null
      if (currentWorkspace()) { locked.current = false; setOperation(null) }
    }
    // Feedback has its own lifecycle; its failures never classify a persisted write.
    if (diagnosisAfterSubmit) requestDiagnosis(diagnosisAfterSubmit)
  }
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    void mutate('submit')
  }
  function retry() {
    if (!view || locked.current || blocked) return
    voice.current?.invalidate()
    invalidateDiagnosis()
    clearDraft()
    setError('')
    showView({ ...view, mode: 'composing' })
  }
  function cancelRetry() {
    if (!view || locked.current || blocked) return
    voice.current?.invalidate()
    clearDraft()
    setError('')
    showView({ ...view, mode: 'review' })
  }
  async function recheck() {
    if (!recovery || locked.current) return
    voice.current?.invalidate()
    invalidateDiagnosis()
    locked.current = true
    setOperation('Rechecking saved state…')
    try {
      const saved = await readSavedView(recovery.sessionId)
      if (!currentWorkspace()) return
      const unchangedDraft = (recovery.kind === 'submit' || recovery.kind === 'transcription') &&
        saved.session.status === 'active' && saved.session.current_question_index === recovery.questionIndex &&
        saved.session.current_question_latest_attempt_number === recovery.revision
      if (unchangedDraft) showView({ ...saved, mode: 'composing' })
      else install(saved)
      if (recovery.kind === 'restore') accessCallback.current?.(saved.session.id)
      // Recovery reads may reveal a committed write whose response was lost.
      // A standalone unlinked transcription does not change history summaries.
      if (recovery.kind !== 'transcription' || !unchangedDraft) factsCallback.current?.()
      setRecovery(null)
      setError('Saved state rechecked. Choose your next action; no request was resubmitted.')
    } catch {
      if (currentWorkspace()) setError('Unable to recheck saved state. Check your connection and recheck before continuing.')
    } finally {
      if (currentWorkspace()) { locked.current = false; setOperation(null) }
    }
  }
  async function audioConflict() {
    if (!view || locked.current) return
    locked.current = true
    setOperation('Reloading saved state…')
    try { await reconcileConflict(recoveryFor(view, 'conflict')) }
    finally { if (currentWorkspace()) { locked.current = false; setOperation(null) } }
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
      {session && <p>Scenario: {scenarioLabel(session.scenario_type)}</p>}
      {(!session || session.status === 'completed' || error) && <fieldset className="scenario-setup"
        disabled={operation !== null || transcribing}>
        <legend>Practice scenario</legend>
        {SCENARIOS.map((scenario) => <label className="scenario-choice" key={scenario.type}>
          <input type="radio" name="practice-scenario" value={scenario.type}
            aria-labelledby={`scenario-label-${scenario.type}`} aria-describedby={`scenario-description-${scenario.type}`}
            checked={selectedScenario === scenario.type} onChange={() => setSelectedScenario(scenario.type)} />
          <span><strong id={`scenario-label-${scenario.type}`}>{scenario.label}</strong>
            <span id={`scenario-description-${scenario.type}`} className="scenario-description">{scenario.description}</span></span>
        </label>)}
      </fieldset>}
      {!session && (
        <button type="button" onClick={() => void start()} disabled={operation !== null || transcribing}>Start Interview</button>
      )}
      {session?.status === 'active' && view && (
        <>
          <p aria-live="polite">Question {session.current_question_index + 1} of {session.total_questions}</p>
          <h2 id="current-question" aria-live="polite">{session.current_question}</h2>
          {workspace && <QuestionVoice ref={voice} authGeneration={workspace.generation} sessionId={session.id}
            questionIndex={session.current_question_index} question={session.current_question!}
            active={active} disabled={blocked || audioBusy} />}
          {view.mode === 'composing' && (
            <form onSubmit={submit}>
              <AudioAnswer key={`${session.id}:${session.current_question_index}:${draftGeneration}`}
                session={session} disabled={blocked} hasAnswer={draft.text.length > 0}
                onBusyChange={setAudioBusy}
                onBeforeRecording={() => voice.current?.invalidate()}
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
              {feedback && <section aria-label="Answer feedback" aria-busy={feedback.status === 'loading'}>
                <h3>Answer feedback</h3>
                {feedback.status === 'loading' && <p role="status">Generating answer feedback…</p>}
                {feedback.status === 'error' && <p role="alert">{feedback.message}</p>}
                {feedback.status === 'success' && <>
                  <dl><dt>Question addressed</dt><dd>{addressedLabels[feedback.diagnosis.addressed_question]}</dd></dl>
                  <p>{feedback.diagnosis.addressed_question_reason}</p>
                  {feedback.diagnosis.strengths.length > 0 && <>
                    <h4>Strengths</h4>
                    <ul>{feedback.diagnosis.strengths.map((item, index) => <li key={index}>{item}</li>)}</ul>
                  </>}
                  {feedback.diagnosis.missing_information.length > 0 && <>
                    <h4>Missing information</h4>
                    <ul>{feedback.diagnosis.missing_information.map((item, index) => <li key={index}>{item}</li>)}</ul>
                  </>}
                  <dl><dt>Structure</dt><dd>{structureLabels[feedback.diagnosis.structure]}</dd></dl>
                  <p>{feedback.diagnosis.structure_feedback}</p>
                  <dl><dt>Next focus</dt><dd>{focusLabels[feedback.diagnosis.next_focus]}</dd></dl>
                  <p>{feedback.diagnosis.next_focus_reason}</p>
                  <h4>Retry instruction</h4>
                  <p>{feedback.diagnosis.retry_instruction}</p>
                  {drill && <section aria-label="Practice drill">
                    <h4>{drill.title}</h4>
                    <p>{drill.goal}</p>
                    <ol>{drill.steps.map((step, index) => <li key={index}>{step}</li>)}</ol>
                  </section>}
                </>}
              </section>}
              <div className="attempt-actions">
                <button type="button" onClick={retry} disabled={blocked}>{view.attempts.length > 1 ? 'Retry Again' : 'Retry'}</button>
                <button type="button" onClick={() => void mutate('continue')} disabled={blocked || view.attempts.length === 0}>Continue</button>
              </div>
            </>
          )}
        </>
      )}
      {session?.status === 'completed' && (
        <div>
          <div role="status">
            <h2>Interview Complete</h2>
            <p>You completed all {session.total_questions} questions.</p>
          </div>
          {operation === null && recovery === null && <InterviewSummary key={session.id} sessionId={session.id} />}
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
