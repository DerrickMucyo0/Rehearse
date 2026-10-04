import { useRef, useState } from 'react'
import AudioAnswer from './AudioAnswer'
import type { FormEvent } from 'react'
import { startInterview, submitAnswer } from './interviewApi'
import type { InterviewSession } from './interviewApi'

export default function Interview() {
  const [session, setSession] = useState<InterviewSession | null>(null)
  const [answer, setAnswer] = useState('')
  const [busy, setBusy] = useState(false)
  const [transcribing, setTranscribing] = useState(false)
  const [error, setError] = useState('')
  const submitting = useRef(false)
  const attempt = useRef<{ sessionId: string; revision: number; answer: string; id: string } | null>(null)

  async function start() {
    setBusy(true)
    setError('')
    try {
      setSession(await startInterview())
      attempt.current = null
      setAnswer('')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to start interview.')
    } finally {
      setBusy(false)
    }
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!session || submitting.current || busy || transcribing || !answer.trim()) return
    submitting.current = true
    const text = answer.trim()
    if (!attempt.current || attempt.current.sessionId !== session.id ||
        attempt.current.revision !== session.turn_revision || attempt.current.answer !== text) {
      attempt.current = { sessionId: session.id, revision: session.turn_revision, answer: text, id: crypto.randomUUID() }
    }
    setBusy(true)
    setError('')
    try {
      setSession(await submitAnswer(session, text, attempt.current.id))
      attempt.current = null
      setAnswer('')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to submit answer.')
    } finally {
      submitting.current = false
      setBusy(false)
    }
  }

  return (
    <section className="interview" aria-label="Interview practice" aria-busy={busy}>
      {!session && (
        <button type="button" onClick={() => void start()} disabled={busy || transcribing}>
          {busy ? 'Starting…' : 'Start Interview'}
        </button>
      )}
      {session?.status === 'active' && (
        <form onSubmit={(event) => void submit(event)}>
          <p aria-live="polite">Question {session.current_question_index + 1} of {session.questions.length}</p>
          <h2 id="current-question" aria-live="polite">{session.current_prompt}</h2>
          <AudioAnswer key={`${session.id}:${session.turn_revision}`} session={session} disabled={busy || transcribing}
            hasAnswer={answer.length > 0} onTranscribing={setTranscribing}
            onTranscript={(text) => setAnswer((current) => current === '' ? text : current)} />
          <p>Submit Answer sends your text to NVIDIA for interviewer reasoning.</p>
          {busy && <p role="status">Waiting for the interviewer…</p>}
          <label htmlFor="answer">Your answer</label>
          <textarea
            id="answer"
            aria-describedby="current-question"
            value={answer}
            onChange={(event) => setAnswer(event.target.value)}
            rows={6}
            maxLength={10000}
            required
            disabled={busy || transcribing}
          />
          <button type="submit" disabled={busy || transcribing || !answer.trim()}>
            {busy ? 'Reviewing answer…' : 'Submit Answer'}
          </button>
        </form>
      )}
      {session?.status === 'completed' && (
        <div role="status">
          <h2>Interview Complete</h2>
          <p>You completed all {session.questions.length} questions.</p>
          <button type="button" onClick={() => void start()} disabled={busy || transcribing}>Start New Interview</button>
        </div>
      )}
      {error && (
        <div>
          <p role="alert">{error}</p>
          {session?.status === 'active' && (
            <button type="button" onClick={() => void start()} disabled={busy || transcribing}>Start New Interview</button>
          )}
        </div>
      )}
    </section>
  )
}
