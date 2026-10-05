import type { TimingUnavailableReason } from './interviewApi'
import { normalizeSessionId } from './historyStorage'

export interface HistoryMeasurement {
  measurement_version: string
  measurement_source: 'original_transcription'
  recognized_word_count: number
  um_count: number | null
  uh_count: number | null
  filler_unavailable_reason: 'unsupported_language' | null
  timed_utterance_span_seconds: number | null
  estimated_words_per_minute: number | null
  timing_unavailable_reason: TimingUnavailableReason | null
}
export interface HistoryFinalizedPoint {
  question_index: number
  attempt_id: string
  attempt_number: number
  submitted_at: string
  measurement: HistoryMeasurement | null
}
export interface HistorySummary {
  session_id: string
  status: 'active' | 'completed'
  created_at: string
  completed_at: string | null
  current_question_number: number | null
  total_questions: number
  finalized_question_count: number
  questions_practiced_count: number
  total_attempt_count: number
  total_retry_count: number
  measured_final_answer_count: number
  last_submitted_at: string | null
  last_saved_activity_at: string
  finalized_points: HistoryFinalizedPoint[]
}
export interface HistorySummaries { summaries: HistorySummary[]; missing_session_ids: string[] }
export interface HistoryQuestion {
  question_index: number
  question_text: string
  finalized: boolean
  attempt_count: number
  latest_attempt_id: string | null
  latest_attempt_number: number | null
  final_attempt_id: string | null
  final_attempt_number: number | null
}
export interface HistoryAttempt {
  attempt_id: string
  attempt_number: number
  answer_text: string
  submitted_at: string
  is_final: boolean
  measurement: HistoryMeasurement | null
}
export interface HistorySelectedQuestion {
  question_index: number
  attempts: HistoryAttempt[]
  has_more: boolean
  next_after_attempt_number: number | null
}
export interface HistoryDetail { summary: HistorySummary; questions: HistoryQuestion[]; selected_question: HistorySelectedQuestion | null }

export class HistoryApiError extends Error {
  readonly status: number | null
  readonly cancelled: boolean
  constructor(message: string, status: number | null = null, cancelled = false) {
    super(message)
    this.name = 'HistoryApiError'
    this.status = status
    this.cancelled = cancelled
  }
}
export interface HistoryReadOptions { signal?: AbortSignal }
export interface HistoryDetailOptions extends HistoryReadOptions { questionIndex?: number; afterAttemptNumber?: number; limit?: number }

const timingReasons: readonly unknown[] = ['missing_timings', 'timing_coverage_mismatch', 'invalid_timing', 'invalid_timing_order', 'unusable_span']
const measurementKeys = ['measurement_version', 'measurement_source', 'recognized_word_count', 'um_count', 'uh_count', 'filler_unavailable_reason', 'timed_utterance_span_seconds', 'estimated_words_per_minute', 'timing_unavailable_reason']
const pointKeys = ['question_index', 'attempt_id', 'attempt_number', 'submitted_at', 'measurement']
const summaryKeys = ['session_id', 'status', 'created_at', 'completed_at', 'current_question_number', 'total_questions', 'finalized_question_count', 'questions_practiced_count', 'total_attempt_count', 'total_retry_count', 'measured_final_answer_count', 'last_submitted_at', 'last_saved_activity_at', 'finalized_points']
const questionKeys = ['question_index', 'question_text', 'finalized', 'attempt_count', 'latest_attempt_id', 'latest_attempt_number', 'final_attempt_id', 'final_attempt_number']
const attemptKeys = ['attempt_id', 'attempt_number', 'answer_text', 'submitted_at', 'is_final', 'measurement']
function object(value: unknown): value is Record<string, unknown> { return typeof value === 'object' && value !== null && !Array.isArray(value) }
function shape(value: unknown, keys: string[]): value is Record<string, unknown> {
  return object(value) && Object.keys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key))
}
function integer(value: unknown): value is number { return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 }
function positive(value: unknown): value is number { return integer(value) && value > 0 }
function positiveFloat(value: unknown): value is number { return typeof value === 'number' && Number.isFinite(value) && value > 0 }
function uuid(value: unknown): value is string { return typeof value === 'string' && normalizeSessionId(value) === value }
function timestamp(value: unknown): value is string {
  return typeof value === 'string' && /(?:Z|[+-]\d{2}:\d{2})$/.test(value) && Number.isFinite(Date.parse(value))
}
function measurement(value: unknown): value is HistoryMeasurement | null {
  if (value === null) return true
  return shape(value, measurementKeys) && typeof value.measurement_version === 'string' && value.measurement_version.trim().length > 0 &&
    value.measurement_source === 'original_transcription' && integer(value.recognized_word_count) &&
    (value.filler_unavailable_reason === null
      ? integer(value.um_count) && integer(value.uh_count)
      : value.filler_unavailable_reason === 'unsupported_language' && value.um_count === null && value.uh_count === null) &&
    (value.timing_unavailable_reason === null
      ? positiveFloat(value.timed_utterance_span_seconds) && positiveFloat(value.estimated_words_per_minute)
      : timingReasons.includes(value.timing_unavailable_reason) && value.timed_utterance_span_seconds === null && value.estimated_words_per_minute === null)
}
function point(value: unknown): value is HistoryFinalizedPoint {
  return shape(value, pointKeys) && integer(value.question_index) && uuid(value.attempt_id) && positive(value.attempt_number) && timestamp(value.submitted_at) && measurement(value.measurement)
}
function summary(value: unknown): value is HistorySummary {
  if (!shape(value, summaryKeys) || !uuid(value.session_id) || !timestamp(value.created_at) || !timestamp(value.last_saved_activity_at) ||
      !(value.completed_at === null || timestamp(value.completed_at)) || !(value.last_submitted_at === null || timestamp(value.last_submitted_at)) ||
      value.total_questions !== 5 || !integer(value.finalized_question_count) || value.finalized_question_count > 5 ||
      !integer(value.questions_practiced_count) || value.questions_practiced_count < value.finalized_question_count || value.questions_practiced_count > 5 ||
      !integer(value.total_attempt_count) || value.total_attempt_count < value.questions_practiced_count ||
      !integer(value.total_retry_count) || value.total_retry_count !== value.total_attempt_count - value.questions_practiced_count ||
      !integer(value.measured_final_answer_count) || !Array.isArray(value.finalized_points) || !value.finalized_points.every(point) ||
      value.finalized_points.length !== value.finalized_question_count) return false
  if (value.status === 'active') {
    if (value.finalized_question_count >= 5 || value.completed_at !== null || value.current_question_number !== value.finalized_question_count + 1 || value.questions_practiced_count > value.finalized_question_count + 1) return false
  } else if (value.status !== 'completed' || value.finalized_question_count !== 5 || value.completed_at === null || value.current_question_number !== null) return false
  const points = value.finalized_points as HistoryFinalizedPoint[]
  const dates = [value.created_at, ...(value.last_submitted_at ? [value.last_submitted_at] : []), ...(value.completed_at ? [value.completed_at] : [])]
  return points.every((item, index) => item.question_index === index) && new Set(points.map((item) => item.attempt_id)).size === points.length &&
    value.measured_final_answer_count === points.filter((item) => item.measurement !== null).length &&
    Date.parse(value.last_saved_activity_at) === Math.max(...dates.map((date) => Date.parse(date))) &&
    (value.completed_at === null || Date.parse(value.completed_at) >= Date.parse(value.created_at))
}
function validSummaries(value: unknown, requested: string[]): value is HistorySummaries {
  if (!shape(value, ['summaries', 'missing_session_ids']) || !Array.isArray(value.summaries) || !value.summaries.every(summary) ||
      !Array.isArray(value.missing_session_ids) || !value.missing_session_ids.every(uuid)) return false
  const returned = [...value.summaries.map((item) => (item as HistorySummary).session_id), ...value.missing_session_ids]
  return returned.length === requested.length && new Set(returned).size === returned.length && returned.every((id) => requested.includes(id))
}
function question(value: unknown): value is HistoryQuestion {
  if (!shape(value, questionKeys) || !integer(value.question_index) || typeof value.question_text !== 'string' || !value.question_text.trim() ||
      typeof value.finalized !== 'boolean' || !integer(value.attempt_count)) return false
  if (value.attempt_count === 0) return !value.finalized && value.latest_attempt_id === null && value.latest_attempt_number === null && value.final_attempt_id === null && value.final_attempt_number === null
  return uuid(value.latest_attempt_id) && positive(value.latest_attempt_number) && value.latest_attempt_number >= value.attempt_count &&
    (value.finalized
      ? value.final_attempt_id === value.latest_attempt_id && value.final_attempt_number === value.latest_attempt_number
      : value.final_attempt_id === null && value.final_attempt_number === null)
}
function attempt(value: unknown): value is HistoryAttempt {
  return shape(value, attemptKeys) && uuid(value.attempt_id) && positive(value.attempt_number) && typeof value.answer_text === 'string' &&
    value.answer_text.trim().length > 0 && !value.answer_text.includes('\0') && [...value.answer_text].length <= 10000 && timestamp(value.submitted_at) &&
    typeof value.is_final === 'boolean' && measurement(value.measurement)
}
function validDetail(value: unknown, id: string, options: HistoryDetailOptions): value is HistoryDetail {
  if (!shape(value, ['summary', 'questions', 'selected_question']) || !summary(value.summary) || value.summary.session_id !== id ||
      !Array.isArray(value.questions) || !value.questions.every(question) || value.questions.length !== 5) return false
  const questions = value.questions as HistoryQuestion[]
  const session = value.summary
  if (!questions.every((item, index) => item.question_index === index && item.finalized === (index < session.finalized_question_count) &&
      (!item.finalized || item.final_attempt_id === session.finalized_points[index].attempt_id)) ||
      questions.reduce((count, item) => count + item.attempt_count, 0) !== session.total_attempt_count ||
      questions.filter((item) => item.attempt_count > 0).length !== session.questions_practiced_count) return false
  if (options.questionIndex === undefined) return value.selected_question === null
  const page = value.selected_question
  if (!shape(page, ['question_index', 'attempts', 'has_more', 'next_after_attempt_number']) || page.question_index !== options.questionIndex ||
      !Array.isArray(page.attempts) || !page.attempts.every(attempt) || page.attempts.length > (options.limit ?? 10) || typeof page.has_more !== 'boolean') return false
  const attempts = page.attempts as HistoryAttempt[]
  const overview = questions[options.questionIndex]
  if (!overview || new Set(attempts.map((item) => item.attempt_id)).size !== attempts.length ||
      !attempts.every((item, index) => item.attempt_number > (index === 0 ? options.afterAttemptNumber ?? 0 : attempts[index - 1].attempt_number) &&
        item.attempt_number <= (overview.latest_attempt_number ?? 0) && item.is_final === (item.attempt_id === overview.final_attempt_id))) return false
  return page.has_more
    ? attempts.length === (options.limit ?? 10) && page.next_after_attempt_number === attempts.at(-1)?.attempt_number
    : page.next_after_attempt_number === null
}

function cancelled(): HistoryApiError { return new HistoryApiError('History request cancelled.', null, true) }
async function read<T>(path: string, valid: (value: unknown) => value is T, options: RequestInit, signal?: AbortSignal): Promise<T> {
  if (signal?.aborted) throw cancelled()
  let response: Response
  try {
    const timeout = AbortSignal.timeout(10000)
    response = await fetch(path, { ...options, cache: 'no-store', signal: signal ? AbortSignal.any([signal, timeout]) : timeout })
  } catch {
    if (signal?.aborted) throw cancelled()
    throw new HistoryApiError('Unable to load history. Check your connection and try again.')
  }
  if (signal?.aborted) throw cancelled()
  if (!response.ok) {
    const messages: Record<number, string> = {
      404: 'This session is unavailable.', 422: 'The history request was not accepted.',
      500: 'Stored session history is unavailable.', 503: 'History is temporarily unavailable. Please try again.',
    }
    throw new HistoryApiError(messages[response.status] ?? 'Unable to load history. Please try again.', response.status)
  }
  let result: unknown
  try { result = await response.json() } catch {
    if (signal?.aborted) throw cancelled()
    throw new HistoryApiError('Unexpected history response. Please try again.', response.status)
  }
  if (signal?.aborted) throw cancelled()
  if (!valid(result)) throw new HistoryApiError('Unexpected history response. Please try again.', response.status)
  return result
}

export async function getHistorySummaries(ids: readonly string[], options: HistoryReadOptions = {}): Promise<HistorySummaries> {
  if (!Array.isArray(ids) || ids.length < 1 || ids.length > 50) throw new HistoryApiError('The history request was not accepted.', 422)
  const normalized = ids.map(normalizeSessionId)
  if (normalized.some((id) => id === null)) throw new HistoryApiError('The history request was not accepted.', 422)
  const requested = [...new Set(normalized as string[])]
  return read('/api/history/summaries', (value): value is HistorySummaries => validSummaries(value, requested), {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session_ids: requested }),
  }, options.signal)
}

export async function getHistoryDetail(value: string, options: HistoryDetailOptions = {}): Promise<HistoryDetail> {
  const id = normalizeSessionId(value)
  if (id === null || (options.questionIndex !== undefined && !integer(options.questionIndex)) ||
      (options.afterAttemptNumber !== undefined && (!positive(options.afterAttemptNumber) || options.questionIndex === undefined)) ||
      (options.limit !== undefined && (!positive(options.limit) || options.limit > 20))) throw new HistoryApiError('The history request was not accepted.', 422)
  const query = new URLSearchParams()
  if (options.questionIndex !== undefined) query.set('question_index', String(options.questionIndex))
  if (options.afterAttemptNumber !== undefined) query.set('after_attempt_number', String(options.afterAttemptNumber))
  if (options.questionIndex !== undefined || options.limit !== undefined) query.set('limit', String(options.limit ?? 10))
  return read(`/api/sessions/${id}/history-detail${query.size ? `?${query}` : ''}`,
    (result): result is HistoryDetail => validDetail(result, id, options), {}, options.signal)
}
