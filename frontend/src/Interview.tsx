import { useState } from 'react'
import AudioAnswer from './AudioAnswer'
import type { FormEvent } from 'react'
import { startInterview, submitAnswer } from './interviewApi'
import type { InterviewSession } from './interviewApi'

export default function Interview() {
  const [session, setSession] = useState<InterviewSession | null>(null)
  const [draft, setDraft] = useState<{ text: string; measurementId: string | null }>({ text: '', measurementId: null })
  const answer = draft.text
  const [busy, setBusy] = useState(false)
  const [transcribing, setTranscribing] = useState(false)
  const [error, setError] = useState('')

  async function start() {
    setBusy(true)
    setError('')
    try {
      setSession(await startInterview())
      setDraft({ text: '', measurementId: null })
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to start interview.')
    } finally {
      setBusy(false)
    }
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!session || busy || transcribing || !answer.trim()) return
    setBusy(true)
    setError('')
    try {
      setSession(await submitAnswer(session, answer.trim(), draft.measurementId))
      setDraft({ text: '', measurementId: null })
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to submit answer.')
    } finally {
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
          <h2 id="current-question" aria-live="polite">{session.current_question}</h2>
          <AudioAnswer key={`${session.id}:${session.current_question_index}`} session={session} disabled={busy || transcribing}
            hasAnswer={answer.length > 0} onTranscribing={setTranscribing}
            onTranscript={(text, measurementId) => setDraft((current) => current.text === ''
              ? { text, measurementId } : current)}
            onInvalidateMeasurement={() => setDraft((current) => ({ ...current, measurementId: null }))} />
          <label htmlFor="answer">Your answer</label>
          <textarea
            id="answer"
            aria-describedby="current-question"
            value={answer}
            onChange={(event) => {
              const text = event.target.value
              setDraft((current) => ({ text, measurementId: text.trim() ? current.measurementId : null }))
            }}
            rows={6}
            maxLength={10000}
            required
            disabled={busy || transcribing}
          />
          <button type="submit" disabled={busy || transcribing || !answer.trim()}>
            {busy ? 'Submitting…' : 'Submit Answer'}
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
