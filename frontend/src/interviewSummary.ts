import type { HistoryDetail, HistoryMeasurement } from './historyApi'

export interface InterviewSummary {
  readonly summary_version: 'interview-summary-v1'
  readonly session_id: string
  readonly status: 'completed'
  readonly questions_completed: number
  readonly question_count: number
  readonly total_attempts: number
  readonly total_retries: number
  readonly questions: readonly {
    readonly question_index: number
    readonly question_text: string
    readonly final_attempt_number: number
    readonly retry_count: number
    readonly measurement: HistoryMeasurement | null
  }[]
}

const INVALID_SUMMARY = 'Completed interview summary is unavailable.'

function nonnegativeInteger(value: number): boolean {
  return Number.isSafeInteger(value) && value >= 0
}

function immutableMeasurement(measurement: HistoryMeasurement | null): HistoryMeasurement | null {
  if (measurement === null) return null
  // Copy both DTO levels: freezing or sharing mutable source facts would change
  // the caller's objects or allow an output mutation to affect a later projection.
  return Object.freeze({
    ...measurement,
    delivery_metrics: measurement.delivery_metrics === null
      ? null : Object.freeze({ ...measurement.delivery_metrics }),
  })
}

/** Project already validated History facts; never select attempts or recalculate metrics. */
export function buildInterviewSummary(detail: HistoryDetail): InterviewSummary {
  const { summary, questions } = detail
  const count = summary.total_questions
  if (summary.status !== 'completed' || summary.completed_at === null ||
      summary.current_question_number !== null || !nonnegativeInteger(count) || count === 0 ||
      summary.finalized_question_count !== count || summary.questions_practiced_count !== count ||
      questions.length !== count || summary.finalized_points.length !== count ||
      !nonnegativeInteger(summary.total_attempt_count) || summary.total_attempt_count < count ||
      !nonnegativeInteger(summary.total_retry_count) ||
      summary.total_retry_count !== summary.total_attempt_count - summary.questions_practiced_count) {
    throw new Error(INVALID_SUMMARY)
  }

  const finalIds = new Set<string>()
  let attempts = 0
  const rows = questions.map((question, index) => {
    const point = summary.finalized_points[index]
    if (question.question_index !== index || point.question_index !== index || question.finalized !== true ||
        !nonnegativeInteger(question.attempt_count) || question.attempt_count === 0 ||
        question.final_attempt_id === null || question.final_attempt_number === null ||
        !nonnegativeInteger(question.final_attempt_number) || question.final_attempt_number < question.attempt_count ||
        question.latest_attempt_id !== question.final_attempt_id ||
        question.latest_attempt_number !== question.final_attempt_number ||
        point.attempt_id !== question.final_attempt_id || point.attempt_number !== question.final_attempt_number ||
        finalIds.has(point.attempt_id)) {
      throw new Error(INVALID_SUMMARY)
    }
    finalIds.add(point.attempt_id)
    attempts += question.attempt_count
    return Object.freeze({
      question_index: question.question_index,
      question_text: question.question_text,
      final_attempt_number: question.final_attempt_number,
      retry_count: question.attempt_count - 1,
      measurement: immutableMeasurement(point.measurement),
    })
  })
  if (attempts !== summary.total_attempt_count) throw new Error(INVALID_SUMMARY)

  return Object.freeze({
    summary_version: 'interview-summary-v1',
    session_id: summary.session_id,
    status: 'completed',
    questions_completed: summary.finalized_question_count,
    question_count: count,
    total_attempts: summary.total_attempt_count,
    total_retries: summary.total_retry_count,
    questions: Object.freeze(rows),
  })
}
