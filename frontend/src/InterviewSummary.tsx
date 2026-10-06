import { useEffect, useRef, useState } from 'react'
import DeliveryFacts from './DeliveryFacts'
import { getHistoryDetail } from './historyApi'
import type { HistoryMeasurement } from './historyApi'
import { buildInterviewSummary } from './interviewSummary'
import type { InterviewSummary as Summary } from './interviewSummary'
import { describeUnavailableReason, formatProgressValue, PROGRESS_METRICS } from './progress'
import type { ProgressMetricId } from './progress'

type Snapshot =
  | { sessionId: string; status: 'error' }
  | { sessionId: string; status: 'success'; summary: Summary }

function unavailableReason(measurement: HistoryMeasurement, metric: ProgressMetricId): string | null {
  if (metric === 'um_count' || metric === 'uh_count') return measurement.filler_unavailable_reason
  if (metric === 'estimated_words_per_minute' || metric === 'timed_utterance_span_seconds') return measurement.timing_unavailable_reason
  return null
}

function OwnedInterviewSummary({ sessionId }: { sessionId: string }) {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null)
  const generation = useRef(0)
  const owner = useRef<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    const read = ++generation.current
    owner.current = sessionId
    const current = () => !controller.signal.aborted && generation.current === read && owner.current === sessionId
    // A discarded mount (including development StrictMode replay) owns no read.
    void Promise.resolve().then(() => {
      if (current()) return getHistoryDetail(sessionId, { signal: controller.signal })
    }).then((detail) => {
      if (!detail || !current()) return
      if (detail.summary.session_id !== sessionId) throw new Error('Interview summary is unavailable.')
      const summary = buildInterviewSummary(detail)
      if (current()) setSnapshot({ sessionId, status: 'success', summary })
    }).catch(() => {
      if (current()) setSnapshot({ sessionId, status: 'error' })
    })
    return () => {
      controller.abort()
      generation.current += 1
      owner.current = null
    }
  }, [sessionId])

  const current = snapshot?.sessionId === sessionId ? snapshot : null
  const loading = current === null

  return <section aria-label="Interview summary" aria-busy={loading}>
    <h3>Interview summary</h3>
    {loading && <p role="status">Loading interview summary…</p>}
    {current?.status === 'error' && <p role="alert">Interview summary is unavailable.</p>}
    {current?.status === 'success' && <>
      <dl>
        <dt>Questions completed</dt><dd>{current.summary.questions_completed} / {current.summary.question_count}</dd>
        <dt>Total attempts</dt><dd>{current.summary.total_attempts}</dd>
        <dt>Total retries</dt><dd>{current.summary.total_retries}</dd>
      </dl>
      {current.summary.questions.map((question) => <article key={question.question_index}
        aria-label={`Summary for Question ${question.question_index + 1}`}>
        <h4>Question {question.question_index + 1}</h4>
        <p>{question.question_text}</p>
        <dl>
          <dt>Final attempt</dt><dd>{question.final_attempt_number}</dd>
          <dt>Retries</dt><dd>{question.retry_count}</dd>
        </dl>
        <section aria-label={`Final-attempt measurements for Question ${question.question_index + 1}`}>
          <h5>Final-attempt measurements</h5>
          {question.measurement === null ? <>
            <p>Speaking measurements: Unavailable — No measurement</p>
            <p>Timed pauses: Unavailable — No measurement</p>
          </> : <>
            <section aria-label="Speaking measurements">
              <h6>Speaking measurements</h6>
              <p>Measurement version: {question.measurement.measurement_version}</p>
              <p>Source: Original transcription</p>
              <dl>{PROGRESS_METRICS.map((metric) => {
                const value = question.measurement![metric.id]
                const reason = unavailableReason(question.measurement!, metric.id)
                return <div key={metric.id}>
                  <dt>{metric.label}</dt>
                  <dd>{value === null
                    ? <>Unavailable{reason !== null && <> — {describeUnavailableReason(reason)}</>}</>
                    : formatProgressValue(value, metric.id)}</dd>
                </div>
              })}</dl>
            </section>
            <DeliveryFacts metrics={question.measurement.delivery_metrics} headingLevel={6} />
          </>}
        </section>
      </article>)}
    </>}
  </section>
}

export default function InterviewSummary({ sessionId }: { sessionId: string }) {
  // Ownership changes discard the previous snapshot and abort its read.
  return <OwnedInterviewSummary key={sessionId} sessionId={sessionId} />
}
