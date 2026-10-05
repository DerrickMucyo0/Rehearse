import { useEffect, useRef, useState } from 'react'
import { getHistoryDetail, HistoryApiError } from './historyApi'
import type { HistoryAttempt, HistoryDetail, HistorySummary } from './historyApi'

interface Props {
  sessionId: string
  onBack: () => void
  onRemove: (id: string) => void
}

interface AttemptPage {
  questionIndex: number
  attempts: HistoryAttempt[]
  hasMore: boolean
  nextAfter: number | null
}

interface PageFailure {
  questionIndex: number
  after: number | undefined
}

function timestamp(value: string): string {
  return new Date(value).toLocaleString()
}

export function SessionFacts({ summary }: { summary: HistorySummary }) {
  return <dl className="session-facts">
    <dt>Created</dt><dd><time dateTime={summary.created_at}>{timestamp(summary.created_at)}</time></dd>
    {summary.completed_at !== null && <>
      <dt>Completed</dt><dd><time dateTime={summary.completed_at}>{timestamp(summary.completed_at)}</time></dd>
    </>}
    <dt>Last saved activity</dt><dd>
      <time dateTime={summary.last_saved_activity_at}>{timestamp(summary.last_saved_activity_at)}</time>
    </dd>
    <dt>Finalized questions</dt><dd>{summary.finalized_question_count} / {summary.total_questions}</dd>
    <dt>Attempts</dt><dd>{summary.total_attempt_count}</dd>
    <dt>Retries</dt><dd>{summary.total_retry_count}</dd>
    <dt>Measured final answers</dt><dd>{summary.measured_final_answer_count}</dd>
    {summary.status === 'active' && summary.current_question_number !== null && <>
      <dt>Current question</dt><dd>{summary.current_question_number} of {summary.total_questions}</dd>
    </>}
  </dl>
}

function mergeAttempts(previous: HistoryAttempt[], incoming: HistoryAttempt[]): HistoryAttempt[] {
  const attempts = new Map(previous.map((attempt) => [attempt.attempt_id, attempt]))
  for (const attempt of incoming) attempts.set(attempt.attempt_id, attempt)
  return [...attempts.values()].sort((left, right) => left.attempt_number - right.attempt_number)
}

function SessionDetailView({ sessionId, onBack, onRemove }: Props) {
  const [overview, setOverview] = useState<HistoryDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<{ sessionId: string; missing: boolean } | null>(null)
  const [reload, setReload] = useState(0)
  const [selected, setSelected] = useState<number | null>(null)
  const [page, setPage] = useState<AttemptPage | null>(null)
  const [pageLoading, setPageLoading] = useState(false)
  const [pageFailure, setPageFailure] = useState<PageFailure | null>(null)
  const sessionGeneration = useRef(0)
  const pageGeneration = useRef(0)
  const overviewController = useRef<AbortController | null>(null)
  const pageController = useRef<AbortController | null>(null)
  const pageBusy = useRef(false)
  const current = overview?.summary.session_id === sessionId ? overview : null
  const currentError = error?.sessionId === sessionId ? error : null

  useEffect(() => {
    const generation = ++sessionGeneration.current
    const controller = new AbortController()
    overviewController.current = controller
    void getHistoryDetail(sessionId, { signal: controller.signal }).then((detail) => {
      if (controller.signal.aborted || sessionGeneration.current !== generation) return
      setOverview({ ...detail, selected_question: null })
    }).catch((cause: unknown) => {
      if (controller.signal.aborted || sessionGeneration.current !== generation) return
      if (cause instanceof HistoryApiError && cause.cancelled) return
      setError({ sessionId, missing: cause instanceof HistoryApiError && cause.status === 404 })
    }).finally(() => {
      if (!controller.signal.aborted && sessionGeneration.current === generation) setLoading(false)
    })
    return () => {
      controller.abort()
      pageController.current?.abort()
      sessionGeneration.current += 1
      pageGeneration.current += 1
      pageBusy.current = false
    }
  }, [sessionId, reload])

  function retryOverview() {
    overviewController.current?.abort()
    pageController.current?.abort()
    sessionGeneration.current += 1
    pageGeneration.current += 1
    pageBusy.current = false
    setOverview(null)
    setError(null)
    setSelected(null)
    setPage(null)
    setPageFailure(null)
    setPageLoading(false)
    setLoading(true)
    setReload((value) => value + 1)
  }

  function requestPage(questionIndex: number, after?: number) {
    if (!current || pageBusy.current) return
    const sessionRead = sessionGeneration.current
    const generation = ++pageGeneration.current
    const controller = new AbortController()
    pageController.current?.abort()
    pageController.current = controller
    pageBusy.current = true
    setPageLoading(true)
    setPageFailure(null)
    void getHistoryDetail(sessionId, {
      questionIndex, afterAttemptNumber: after, limit: 10, signal: controller.signal,
    }).then((detail) => {
      if (controller.signal.aborted || sessionGeneration.current !== sessionRead ||
          pageGeneration.current !== generation) return
      const result = detail.selected_question
      if (detail.summary.session_id !== sessionId || result === null || result.question_index !== questionIndex) {
        throw new Error('Unexpected saved question response.')
      }
      setOverview({ ...detail, selected_question: null })
      setPage((previous) => ({
        questionIndex,
        attempts: mergeAttempts(after !== undefined && previous?.questionIndex === questionIndex ? previous.attempts : [], result.attempts),
        hasMore: result.has_more,
        nextAfter: result.next_after_attempt_number,
      }))
    }).catch((cause: unknown) => {
      if (controller.signal.aborted || sessionGeneration.current !== sessionRead ||
          pageGeneration.current !== generation) return
      if (cause instanceof HistoryApiError && cause.cancelled) return
      if (cause instanceof HistoryApiError && cause.status === 404) {
        setOverview(null)
        setPage(null)
        setSelected(null)
        setError({ sessionId, missing: true })
      } else {
        setPageFailure({ questionIndex, after })
      }
    }).finally(() => {
      if (!controller.signal.aborted && sessionGeneration.current === sessionRead &&
          pageGeneration.current === generation) {
        pageBusy.current = false
        pageController.current = null
        setPageLoading(false)
      }
    })
  }

  function chooseQuestion(questionIndex: number) {
    pageController.current?.abort()
    pageGeneration.current += 1
    pageBusy.current = false
    setPage(null)
    setPageFailure(null)
    setPageLoading(false)
    if (selected === questionIndex) {
      setSelected(null)
      return
    }
    setSelected(questionIndex)
    requestPage(questionIndex)
  }

  function loadMore() {
    if (selected === null || page?.questionIndex !== selected || !page.hasMore ||
        page.nextAfter === null || pageBusy.current) return
    requestPage(selected, page.nextAfter)
  }

  return <section className="history-detail" aria-label="Session detail" aria-busy={loading}>
    <button type="button" onClick={onBack}>Back to History</button>
    <h2>Session detail</h2>
    {loading && <p role="status">Loading session history…</p>}
    {currentError && <div>
      <p role="alert">{currentError.missing ? 'This session is unavailable.' : 'Session history could not be loaded.'}</p>
      <button type="button" onClick={retryOverview}>Retry session request</button>
      {currentError.missing && <button type="button" onClick={() => onRemove(sessionId)}>Remove from this browser</button>}
    </div>}
    {current && <>
      <p>{current.summary.status === 'active' ? 'Active' : 'Completed'}</p>
      <SessionFacts summary={current.summary} />
      <section aria-label="Saved questions">
        <h3>Questions</h3>
        {current.questions.map((question) => {
          const expanded = selected === question.question_index
          const isCurrent = current.summary.status === 'active' &&
            current.summary.current_question_number === question.question_index + 1
          const selectedPage = expanded && page?.questionIndex === question.question_index ? page : null
          return <article className="question-history" key={question.question_index}>
            <h4><button type="button" aria-expanded={expanded} onClick={() => chooseQuestion(question.question_index)}>
              Question {question.question_index + 1}
            </button></h4>
            <p>{question.question_text}</p>
            <p>{question.finalized ? 'Finalized' : isCurrent ? 'Current' : 'Upcoming'}</p>
            <p>Attempts: {question.attempt_count}</p>
            {question.finalized && question.final_attempt_number !== null && <p>Final attempt: {question.final_attempt_number}</p>}
            {!question.finalized && isCurrent && question.latest_attempt_number !== null && <p>Latest attempt: {question.latest_attempt_number}</p>}
            {expanded && <section aria-label={`Saved attempts for Question ${question.question_index + 1}`} aria-busy={pageLoading}>
              {pageLoading && <p role="status">Loading saved attempts…</p>}
              {pageFailure?.questionIndex === question.question_index && <div>
                <p role="alert">Saved attempts could not be loaded.</p>
                <button type="button" disabled={pageLoading} onClick={() => requestPage(question.question_index, pageFailure.after)}>
                  Retry saved attempts
                </button>
              </div>}
              {selectedPage && <>
                {selectedPage.attempts.length === 0 && <p>No saved attempts in this page.</p>}
                {selectedPage.attempts.map((attempt) => <article key={attempt.attempt_id}>
                  <h5>Attempt {attempt.attempt_number}</h5>
                  {attempt.is_final && <p className="final-badge">Final</p>}
                  <p>Submitted <time dateTime={attempt.submitted_at}>{timestamp(attempt.submitted_at)}</time></p>
                  <p className="saved-answer">{attempt.answer_text}</p>
                </article>)}
                {selectedPage.hasMore && <button type="button" disabled={pageLoading} onClick={loadMore}>Load more</button>}
              </>}
            </section>}
          </article>
        })}
      </section>
    </>}
  </section>
}

export default function SessionDetail(props: Props) {
  return <SessionDetailView key={props.sessionId} {...props} />
}
