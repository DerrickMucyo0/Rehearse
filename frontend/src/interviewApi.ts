import { assertProtectedResponseCurrent, isAuthBoundaryError, protectedFetch, readProtectedBlob, readProtectedJson } from './auth'
import { DELIVERY_TIMING_REASONS, validLiveDeliveryMetrics } from './deliveryMetrics'
import type { DeliveryMetrics, DeliveryTimingReason } from './deliveryMetrics'
import { isScenarioType } from './scenarios'
import type { ScenarioType } from './scenarios'
import { isInterviewerPersonaId } from './interviewerPersonas'
import type { InterviewerPersonaId } from './interviewerPersonas'

export interface InterviewSession {
  id: string
  scenario_type: ScenarioType
  interviewer_persona_id?: InterviewerPersonaId | null
  question_engine: QuestionEngine
  total_questions: 5
  status: 'active' | 'completed'
  current_question_index: number
  current_question: string | null
  current_question_latest_attempt_number: number
  questions: string[]
  answers: string[]
}

export type QuestionEngine = 'deterministic-v1' | 'live-ai-roleplay-v1'
export function isQuestionEngine(value: unknown): value is QuestionEngine {
  return value === 'deterministic-v1' || value === 'live-ai-roleplay-v1'
}
export function preparesNextQuestion(session: InterviewSession): boolean {
  return session.question_engine === 'live-ai-roleplay-v1' && session.status === 'active' &&
    session.current_question_index < session.total_questions - 1
}

export interface Attempt {
  id: string
  question_index: number
  attempt_number: number
  answer: string
  submitted_at: string
  measurement_id: string | null
}

export interface AttemptSubmission {
  attempt: Attempt
  session: InterviewSession
}

export interface SemanticDiagnosis {
  diagnosis_version: 'semantic-diagnosis-v1'
  addressed_question: 'yes' | 'partially' | 'no'
  addressed_question_reason: string
  strengths: string[]
  missing_information: string[]
  structure: 'clear' | 'mixed' | 'unclear' | 'insufficient_content'
  structure_feedback: string
  next_focus:
    | 'answer_the_question'
    | 'specificity'
    | 'supporting_detail'
    | 'structure'
    | 'completeness'
    | 'conciseness'
    | 'maintain_strengths'
  next_focus_reason: string
  retry_instruction: string
}

export type TimingUnavailableReason = 'missing_timings' | 'timing_coverage_mismatch' | 'invalid_timing' | 'invalid_timing_order' | 'unusable_span'
export type MetricUnavailableReason = 'no_measurement' | 'unsupported_language' | TimingUnavailableReason
export type ComparisonUnavailableReason = 'measurement_version_mismatch' | 'measurement_source_incompatible' | 'before_unavailable' | 'after_unavailable' | 'both_unavailable'

export interface MetricChange {
  before: number | null
  after: number | null
  delta: number | null
  before_unavailable_reason: MetricUnavailableReason | null
  after_unavailable_reason: MetricUnavailableReason | null
  comparable: boolean
  comparison_unavailable_reason: ComparisonUnavailableReason | null
}

export interface ComparisonMetrics {
  recognized_word_count: MetricChange
  um_count: MetricChange
  uh_count: MetricChange
  timed_utterance_span_seconds: MetricChange
  estimated_words_per_minute: MetricChange
}

export type DeliverySideReason = 'no_measurement' | 'not_recorded' | DeliveryTimingReason
export interface DeliveryMetricChange extends Omit<MetricChange, 'before_unavailable_reason' | 'after_unavailable_reason'> {
  before_unavailable_reason: DeliverySideReason | null
  after_unavailable_reason: DeliverySideReason | null
}
export interface DeliveryComparison {
  before_version: string | null
  after_version: string | null
  before_source: string | null
  after_source: string | null
  pause_count: DeliveryMetricChange
  total_pause_duration_seconds: DeliveryMetricChange
  longest_pause_seconds: DeliveryMetricChange
}

export interface ComparedAttempt {
  id: string
  attempt_number: number
  measurement_id: string | null
  measurement_version: string | null
  measurement_source: string | null
}

export interface AttemptComparison {
  session_id: string
  question_index: number
  before_attempt: ComparedAttempt | null
  after_attempt: ComparedAttempt | null
  comparison: ComparisonMetrics | null
  delivery_comparison: DeliveryComparison | null
}

export class ApiError extends Error {
  readonly status: number | null
  readonly ambiguousWrite: boolean

  constructor(message: string, status: number | null = null, ambiguousWrite = false) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.ambiguousWrite = ambiguousWrite
  }
}

export class SemanticDiagnosisError extends Error {
  readonly status: number | null

  constructor(message: string, status: number | null = null) {
    super(message)
    this.name = 'SemanticDiagnosisError'
    this.status = status
  }
}

export class VoicePlaybackError extends Error {
  readonly status: number | null

  constructor(status: number | null = null) {
    super('Voice playback is unavailable right now.')
    this.name = 'VoicePlaybackError'
    this.status = status
  }
}

export class RoleplayUnavailableError extends ApiError {
  constructor() {
    super('Interviewer is unavailable right now. Try Continue again.', 503)
    this.name = 'RoleplayUnavailableError'
  }
}

export function isConflictError(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409
}

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}
function nonnegativeInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
}
function nullableString(value: unknown): value is string | null {
  return value === null || typeof value === 'string'
}
function validSession(value: unknown): value is InterviewSession {
  if (!(object(value) && typeof value.id === 'string' &&
    isScenarioType(value.scenario_type) && isQuestionEngine(value.question_engine) && value.total_questions === 5 &&
    (!Object.hasOwn(value, 'interviewer_persona_id') || value.interviewer_persona_id === null || isInterviewerPersonaId(value.interviewer_persona_id)) &&
    (value.status === 'active' || value.status === 'completed') &&
    nonnegativeInteger(value.current_question_index) && nullableString(value.current_question) &&
    nonnegativeInteger(value.current_question_latest_attempt_number) &&
    Array.isArray(value.questions) && value.questions.every((question) => typeof question === 'string' && !!question.trim()) &&
    Array.isArray(value.answers) && value.answers.every((answer) => typeof answer === 'string') &&
    value.answers.length === value.current_question_index)) return false
  if (value.status === 'completed') return value.current_question_index === 5 && value.current_question === null &&
    value.current_question_latest_attempt_number === 0 && value.questions.length === 5
  return value.current_question_index < 5 && value.current_question === value.questions[value.current_question_index] &&
    value.questions.length === (value.question_engine === 'live-ai-roleplay-v1' ? value.current_question_index + 1 : 5)
}
function validAttempt(value: unknown): value is Attempt {
  return object(value) && typeof value.id === 'string' && nonnegativeInteger(value.question_index) &&
    nonnegativeInteger(value.attempt_number) && value.attempt_number > 0 &&
    typeof value.answer === 'string' && typeof value.submitted_at === 'string' && nullableString(value.measurement_id)
}

async function request<T>(path: string, valid: (value: unknown) => value is T, options?: RequestInit,
  behavior: { timeoutMs?: number; roleplayFailure?: boolean } = {}): Promise<T> {
  const write = options?.method === 'POST'
  const timeout = AbortSignal.timeout(behavior.timeoutMs ?? 10_000)
  const signal = options?.signal ? AbortSignal.any([options.signal, timeout]) : timeout
  let response: Response
  try {
    response = await protectedFetch(path, { ...options, signal })
  } catch (error) {
    if (isAuthBoundaryError(error)) throw error
    throw new ApiError('Unable to reach the backend. Check your connection and recheck the interview.', null, write)
  }
  assertProtectedResponseCurrent(response)
  if (!response.ok) {
    if (behavior.roleplayFailure && response.status === 503) {
      let failure: unknown
      try { failure = await readProtectedJson(response) } catch (error) {
        if (isAuthBoundaryError(error)) throw error
        throw new ApiError('The request result is unknown. Recheck saved state before trying again.', response.status, write)
      }
      if (object(failure) && Object.keys(failure).length === 3 &&
          failure.detail === 'Interviewer is unavailable right now. Try Continue again.' &&
          failure.code === 'roleplay_generation_unavailable' && failure.write_outcome === 'not_applied') {
        throw new RoleplayUnavailableError()
      }
    }
    const messages: Record<number, string> = {
      404: 'Session or attempt not found. Recheck the interview or start a new one.',
      409: 'The interview changed. Recheck it before continuing.',
      422: 'The request was not accepted. Recheck the interview and try again.',
    }
    throw new ApiError(messages[response.status] || 'Unable to update the interview. Please try again.', response.status,
      write && response.status >= 500)
  }
  let result: unknown
  try { result = await readProtectedJson(response) } catch (error) {
    if (isAuthBoundaryError(error)) throw error
    throw new ApiError('Unexpected backend response. Recheck the interview before continuing.', response.status, write)
  }
  if (!valid(result)) {
    throw new ApiError('Unexpected backend response. Recheck the interview before continuing.', response.status, write)
  }
  return result
}

function questionPath(session: InterviewSession): string {
  return `/api/sessions/${session.id}/questions/${session.current_question_index}`
}
function jsonBody(body: unknown): RequestInit {
  return { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
}

export function getSession(id: string): Promise<InterviewSession> {
  return request(`/api/sessions/${id}`, (value): value is InterviewSession => validSession(value) && value.id === id)
}

export function startInterview(
  scenarioType: ScenarioType = 'job_interview', interviewerPersonaId: InterviewerPersonaId = 'recruiter',
): Promise<InterviewSession> {
  if (!isScenarioType(scenarioType)) return Promise.reject(new ApiError('The request was not accepted. Choose a practice scenario.', 422))
  if (!isInterviewerPersonaId(interviewerPersonaId)) return Promise.reject(new ApiError('Choose a valid interviewer.', 422))
  return request('/api/sessions', (value): value is InterviewSession =>
    validSession(value) && value.interviewer_persona_id === interviewerPersonaId,
  jsonBody({ scenario_type: scenarioType, interviewer_persona_id: interviewerPersonaId }))
}

export function submitAttempt(session: InterviewSession, answer: string, measurementId: string | null = null): Promise<AttemptSubmission> {
  return request(`${questionPath(session)}/attempts`, (value): value is AttemptSubmission =>
    object(value) && validAttempt(value.attempt) && validSession(value.session) &&
    value.session.id === session.id && value.session.current_question_index === session.current_question_index &&
    value.session.question_engine === session.question_engine && value.session.scenario_type === session.scenario_type &&
    value.session.interviewer_persona_id === session.interviewer_persona_id &&
    value.session.questions.length === session.questions.length && value.session.questions.every((text, index) => text === session.questions[index]) &&
    value.session.answers.length === session.answers.length && value.session.answers.every((text, index) => text === session.answers[index]) &&
    value.attempt.question_index === session.current_question_index &&
    value.attempt.attempt_number === session.current_question_latest_attempt_number + 1 &&
    value.session.current_question_latest_attempt_number === value.attempt.attempt_number &&
    value.attempt.measurement_id === measurementId,
  jsonBody({ answer, expected_last_attempt_number: session.current_question_latest_attempt_number, measurement_id: measurementId }))
}

export function continueQuestion(session: InterviewSession, signal?: AbortSignal): Promise<InterviewSession> {
  const generates = preparesNextQuestion(session)
  return request(`${questionPath(session)}/continue`, (value): value is InterviewSession =>
    validSession(value) && value.id === session.id && value.current_question_index === session.current_question_index + 1 &&
    value.question_engine === session.question_engine && value.scenario_type === session.scenario_type &&
    value.interviewer_persona_id === session.interviewer_persona_id &&
    value.current_question_latest_attempt_number === 0 &&
    value.questions.length === session.questions.length + Number(generates) &&
    session.questions.every((text, index) => value.questions[index] === text) &&
    value.answers.length === session.answers.length + 1 && session.answers.every((text, index) => value.answers[index] === text),
  { ...jsonBody({ expected_last_attempt_number: session.current_question_latest_attempt_number }), signal },
  { timeoutMs: generates ? 135_000 : 10_000, roleplayFailure: generates })
}

function validSemanticDiagnosis(value: unknown): value is SemanticDiagnosis {
  const keys = ['diagnosis_version', 'addressed_question', 'addressed_question_reason', 'strengths', 'missing_information',
    'structure', 'structure_feedback', 'next_focus', 'next_focus_reason', 'retry_instruction']
  const text = (field: unknown): field is string => typeof field === 'string' && field.trim().length > 0
  const list = (field: unknown): field is string[] => Array.isArray(field) && field.every(text)
  return object(value) && Object.keys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key)) &&
    value.diagnosis_version === 'semantic-diagnosis-v1' &&
    ['yes', 'partially', 'no'].includes(value.addressed_question as string) &&
    text(value.addressed_question_reason) && list(value.strengths) && list(value.missing_information) &&
    ['clear', 'mixed', 'unclear', 'insufficient_content'].includes(value.structure as string) &&
    text(value.structure_feedback) &&
    ['answer_the_question', 'specificity', 'supporting_detail', 'structure', 'completeness', 'conciseness', 'maintain_strengths']
      .includes(value.next_focus as string) && text(value.next_focus_reason) && text(value.retry_instruction)
}

const SEMANTIC_DIAGNOSIS_TIMEOUT_MS = 135_000

const QUESTION_SPEECH_TIMEOUT_MS = 75_000
const MAX_QUESTION_SPEECH_BYTES = 2 * 1024 * 1024

export async function requestQuestionSpeech(
  sessionId: string,
  questionIndex: number,
  signal: AbortSignal,
): Promise<Blob> {
  const timeout = AbortSignal.timeout(QUESTION_SPEECH_TIMEOUT_MS)
  const checkCancellation = () => {
    if (signal.aborted) throw new DOMException('Voice request cancelled.', 'AbortError')
    if (timeout.aborted) throw new VoicePlaybackError()
  }
  checkCancellation()
  let response: Response
  try {
    // This POST reads synthesized speech; it is not a persisted interview write.
    response = await protectedFetch(`/api/sessions/${sessionId}/questions/${questionIndex}/speech`, {
      method: 'POST', signal: AbortSignal.any([signal, timeout]),
    })
  } catch (error) {
    if (isAuthBoundaryError(error)) throw error
    checkCancellation()
    throw new VoicePlaybackError()
  }
  checkCancellation()
  assertProtectedResponseCurrent(response)
  const mediaType = response.headers.get('Content-Type')?.split(';')[0].trim().toLowerCase()
  if (response.status !== 200 || mediaType !== 'audio/mpeg') throw new VoicePlaybackError(response.status)
  let audio: Blob
  try { audio = await readProtectedBlob(response) } catch (error) {
    if (isAuthBoundaryError(error)) throw error
    checkCancellation()
    throw new VoicePlaybackError(response.status)
  }
  checkCancellation()
  if (audio.size === 0 || audio.size > MAX_QUESTION_SPEECH_BYTES) throw new VoicePlaybackError(response.status)
  return audio
}

export async function getSemanticDiagnosis(
  sessionId: string,
  questionIndex: number,
  attemptNumber: number,
  signal: AbortSignal,
): Promise<SemanticDiagnosis> {
  const timeoutMessage = 'Feedback took too long. You can still retry or continue.'
  const malformedMessage = 'Unable to generate feedback right now. You can still retry or continue.'
  const timeout = AbortSignal.timeout(SEMANTIC_DIAGNOSIS_TIMEOUT_MS)
  const checkCancellation = () => {
    if (signal.aborted) throw new DOMException('Feedback request cancelled.', 'AbortError')
    if (timeout.aborted) throw new SemanticDiagnosisError(timeoutMessage)
  }
  checkCancellation()
  let response: Response
  try {
    response = await protectedFetch(`/api/sessions/${sessionId}/questions/${questionIndex}/attempts/${attemptNumber}/diagnosis`, {
      method: 'POST', signal: AbortSignal.any([signal, timeout]),
    })
  } catch (error) {
    if (isAuthBoundaryError(error)) throw error
    checkCancellation()
    throw new SemanticDiagnosisError('Unable to load feedback right now. You can still retry or continue.')
  }
  checkCancellation()
  assertProtectedResponseCurrent(response)
  if (!response.ok) {
    const messages: Record<number, string> = {
      404: 'Feedback is no longer available for this attempt.',
      502: malformedMessage,
      503: 'Feedback is unavailable right now. You can still retry or continue.',
      504: timeoutMessage,
    }
    throw new SemanticDiagnosisError(messages[response.status] ?? malformedMessage, response.status)
  }
  let result: unknown
  try { result = await readProtectedJson(response) } catch (error) {
    if (isAuthBoundaryError(error)) throw error
    checkCancellation()
    throw new SemanticDiagnosisError(malformedMessage, response.status)
  }
  checkCancellation()
  if (!validSemanticDiagnosis(result)) throw new SemanticDiagnosisError(malformedMessage, response.status)
  return result
}

export function getAttempts(session: InterviewSession): Promise<Attempt[]> {
  return request(`${questionPath(session)}/attempts`, (value): value is Attempt[] =>
    Array.isArray(value) && value.every((attempt, index) => validAttempt(attempt) &&
      attempt.question_index === session.current_question_index &&
      (index === 0 || attempt.attempt_number > value[index - 1].attempt_number)))
}

const metricReasons: readonly unknown[] = ['no_measurement', 'unsupported_language', 'missing_timings', 'timing_coverage_mismatch', 'invalid_timing', 'invalid_timing_order', 'unusable_span']
const comparisonReasons: readonly unknown[] = ['measurement_version_mismatch', 'measurement_source_incompatible', 'before_unavailable', 'after_unavailable', 'both_unavailable']
function validMetricChange(value: unknown): value is MetricChange {
  if (!object(value)) return false
  const numeric = (number: unknown) => number === null || (typeof number === 'number' && Number.isFinite(number))
  return numeric(value.before) && numeric(value.after) && numeric(value.delta) &&
    (value.before_unavailable_reason === null || metricReasons.includes(value.before_unavailable_reason)) &&
    (value.after_unavailable_reason === null || metricReasons.includes(value.after_unavailable_reason)) &&
    typeof value.comparable === 'boolean' &&
    (value.comparison_unavailable_reason === null || comparisonReasons.includes(value.comparison_unavailable_reason)) &&
    (value.comparable
      ? value.before !== null && value.after !== null && value.delta !== null &&
        value.before_unavailable_reason === null && value.after_unavailable_reason === null && value.comparison_unavailable_reason === null
      : value.delta === null && value.comparison_unavailable_reason !== null)
}
function validComparedAttempt(value: unknown): value is ComparedAttempt {
  return object(value) && typeof value.id === 'string' && nonnegativeInteger(value.attempt_number) && value.attempt_number > 0 &&
    nullableString(value.measurement_id) && nullableString(value.measurement_version) && nullableString(value.measurement_source)
}

function validDeliveryComparison(value: unknown): value is DeliveryComparison {
  if (!object(value)) return false
  const names = ['pause_count', 'total_pause_duration_seconds', 'longest_pause_seconds'] as const
  const keys = ['before_version', 'after_version', 'before_source', 'after_source', ...names]
  const nullableNonblank = (item: unknown) => item === null || (typeof item === 'string' && !!item.trim())
  if (Object.keys(value).length !== keys.length || !keys.every((key) => Object.hasOwn(value, key)) ||
      !['before_version', 'after_version', 'before_source', 'after_source'].every((key) => nullableNonblank(value[key]))) return false
  const reason = (item: unknown) => item === null || item === 'no_measurement' || item === 'not_recorded' || DELIVERY_TIMING_REASONS.includes(item as DeliveryTimingReason)
  const numeric = (item: unknown) => item === null || (typeof item === 'number' && Number.isFinite(item))
  const changes: DeliveryMetricChange[] = []
  for (const name of names) {
    const metric = value[name]
    const metricKeys = ['before', 'after', 'delta', 'before_unavailable_reason', 'after_unavailable_reason', 'comparable', 'comparison_unavailable_reason']
    if (!object(metric) || Object.keys(metric).length !== metricKeys.length || !metricKeys.every((key) => Object.hasOwn(metric, key)) ||
        !numeric(metric.before) || !numeric(metric.after) || !numeric(metric.delta) ||
        !reason(metric.before_unavailable_reason) || !reason(metric.after_unavailable_reason) ||
        (metric.before === null) !== (metric.before_unavailable_reason !== null) ||
        (metric.after === null) !== (metric.after_unavailable_reason !== null) ||
        typeof metric.comparable !== 'boolean' ||
        !(metric.comparison_unavailable_reason === null || comparisonReasons.includes(metric.comparison_unavailable_reason))) return false
    if ([metric.before, metric.after].some((item) => item !== null && (item as number) < 0)) return false
    if (name === 'pause_count' && [metric.before, metric.after, metric.delta].some((item) => item !== null && !Number.isSafeInteger(item))) return false
    if (metric.comparable
      ? metric.before === null || metric.after === null || metric.delta === null || metric.comparison_unavailable_reason !== null
      : metric.delta !== null || metric.comparison_unavailable_reason === null) return false
    changes.push(metric as unknown as DeliveryMetricChange)
  }
  for (const side of ['before', 'after'] as const) {
    const version = value[`${side}_version`]
    const source = value[`${side}_source`]
    const sideReason = changes[0][`${side}_unavailable_reason`]
    if (!changes.every((metric) => metric[`${side}_unavailable_reason`] === sideReason)) return false
    const [count, total, longest] = changes.map((metric) => metric[side])
    if (sideReason === null) {
      if (version === null || source === null || count === null || total === null || longest === null ||
          (count === 0 ? total !== 0 || longest !== 0 : total <= 0 || longest <= 0 || longest > total)) return false
    } else if (sideReason === 'no_measurement') {
      if (version !== null || source !== null) return false
    } else if (sideReason === 'not_recorded') {
      if (version !== null || source !== null) return false
    } else if (version === null || source === null) return false
  }
  const unavailable = value.before_version !== null && value.after_version !== null && value.before_version !== value.after_version
    ? 'measurement_version_mismatch'
    : value.before_source !== null && value.after_source !== null && value.before_source !== value.after_source
      ? 'measurement_source_incompatible'
      : changes[0].before === null && changes[0].after === null ? 'both_unavailable'
        : changes[0].before === null ? 'before_unavailable' : changes[0].after === null ? 'after_unavailable' : null
  return changes.every((metric) => metric.comparable === (unavailable === null) && metric.comparison_unavailable_reason === unavailable)
}

export function getComparison(session: InterviewSession, before?: number, after?: number): Promise<AttemptComparison> {
  const selectors = new URLSearchParams()
  if (before !== undefined) selectors.set('before', String(before))
  if (after !== undefined) selectors.set('after', String(after))
  const suffix = selectors.size ? `?${selectors}` : ''
  return request(`${questionPath(session)}/comparison${suffix}`, (value): value is AttemptComparison => {
    if (!object(value) || value.session_id !== session.id || value.question_index !== session.current_question_index ||
        (value.before_attempt !== null && !validComparedAttempt(value.before_attempt)) ||
        (value.after_attempt !== null && !validComparedAttempt(value.after_attempt))) return false
    if (value.comparison === null) return value.after_attempt === null && value.delivery_comparison === null
    const metrics = value.comparison
    return validComparedAttempt(value.before_attempt) && validComparedAttempt(value.after_attempt) && object(metrics) &&
      ['recognized_word_count', 'um_count', 'uh_count', 'timed_utterance_span_seconds', 'estimated_words_per_minute']
        .every((name) => validMetricChange(metrics[name])) && validDeliveryComparison(value.delivery_comparison)
  })
}

export interface AudioAccepted {
  session_id: string
  question_index: number
  filename: string
  content_type: string
  size_bytes: number
  status: 'accepted'
}

export async function uploadAudio(session: InterviewSession, audio: Blob, signal: AbortSignal): Promise<AudioAccepted> {
  let response: Response
  try {
    response = await protectedFetch(`/api/sessions/${session.id}/audio`, {
      method: 'POST', body: audioForm(session, audio), signal: AbortSignal.any([signal, AbortSignal.timeout(30000)]),
    })
  } catch (error) {
    if (isAuthBoundaryError(error)) throw error
    throw new ApiError('Audio upload failed or timed out. Check your connection and try again.')
  }
  assertProtectedResponseCurrent(response)
  if (!response.ok) {
    const messages: Record<number, string> = {
      404: 'Session not found. Start a new interview.',
      409: 'This question is no longer current. Recheck the interview.',
      413: 'Recording is too large. Record a shorter answer (maximum 10 MiB).',
      415: 'This audio format is not supported. Try another browser or type your answer.',
      422: 'The recording was empty or invalid. Please record again.',
    }
    throw new ApiError(messages[response.status] || 'Audio upload failed. Please try again.', response.status)
  }
  let result: unknown
  try { result = await readProtectedJson(response) } catch (error) {
    if (isAuthBoundaryError(error)) throw error
    throw new ApiError('Unexpected upload confirmation. Please try again.', response.status)
  }
  if (!object(result) || result.status !== 'accepted' || result.session_id !== session.id || result.question_index !== session.current_question_index) {
    throw new ApiError('Unexpected upload confirmation. Please try again.', response.status)
  }
  return result as unknown as AudioAccepted
}

function audioForm(session: InterviewSession, audio: Blob, withRevision = false): FormData {
  const extensions: Record<string, string> = { 'audio/webm': 'webm', 'audio/ogg': 'ogg', 'audio/mp4': 'm4a', 'audio/mpeg': 'mp3', 'audio/wav': 'wav', 'audio/x-wav': 'wav' }
  const extension = extensions[audio.type.split(';')[0]] || 'audio'
  const body = new FormData()
  body.append('question_index', String(session.current_question_index))
  if (withRevision) body.append('expected_last_attempt_number', String(session.current_question_latest_attempt_number))
  body.append('audio', audio, `answer-${session.current_question_index + 1}.${extension}`)
  return body
}

export interface SpeakingMetrics {
  source: 'original_transcription'
  recognized_word_count: number
  um_count: number | null
  uh_count: number | null
  filler_unavailable_reason: 'unsupported_language' | null
  timed_utterance_span_seconds: number | null
  estimated_words_per_minute: number | null
  timing_unavailable_reason: TimingUnavailableReason | null
}

export interface TranscriptionResult {
  session_id: string
  question_index: number
  measurement_id: string
  text: string
  language: string | null
  words: { text: string; start: number; end: number }[]
  metrics: SpeakingMetrics
  delivery_metrics: DeliveryMetrics
}

function validSpeakingMetrics(value: unknown): value is SpeakingMetrics {
  if (!object(value)) return false
  const positive = (number: unknown) => typeof number === 'number' && Number.isFinite(number) && number > 0
  const timingReasons: unknown[] = ['missing_timings', 'timing_coverage_mismatch', 'invalid_timing', 'invalid_timing_order', 'unusable_span']
  return value.source === 'original_transcription' && nonnegativeInteger(value.recognized_word_count) &&
    (value.filler_unavailable_reason === null
      ? nonnegativeInteger(value.um_count) && nonnegativeInteger(value.uh_count)
      : value.filler_unavailable_reason === 'unsupported_language' && value.um_count === null && value.uh_count === null) &&
    (value.timing_unavailable_reason === null
      ? positive(value.timed_utterance_span_seconds) && positive(value.estimated_words_per_minute)
      : timingReasons.includes(value.timing_unavailable_reason) && value.timed_utterance_span_seconds === null && value.estimated_words_per_minute === null)
}

export async function transcribeAudio(session: InterviewSession, audio: Blob, signal: AbortSignal): Promise<TranscriptionResult> {
  let response: Response
  try {
    response = await protectedFetch(`/api/sessions/${session.id}/transcriptions`, {
      method: 'POST', body: audioForm(session, audio, true),
      signal: AbortSignal.any([signal, AbortSignal.timeout(75000)]),
    })
  } catch (error) {
    if (isAuthBoundaryError(error)) throw error
    throw new ApiError('Transcription failed or timed out. Check your connection and recheck the interview.', null, true)
  }
  assertProtectedResponseCurrent(response)
  if (!response.ok) {
    const messages: Record<number, string> = {
      404: 'Session not found. Start a new interview.',
      409: 'The interview changed. Recheck it before recording another answer.',
      413: 'Recording is too large. Record a shorter answer (maximum 10 MiB).',
      415: 'This audio format is not supported. Try another browser or type your answer.',
      422: 'The recording was empty or invalid. Please record again.',
      502: 'Unable to transcribe this recording. Try again or type your answer.',
      503: 'Transcription is not configured on the server. You can still type your answer.',
      504: 'Transcription timed out. Please try again or type your answer.',
    }
    throw new ApiError(messages[response.status] || 'Transcription failed. Please try again.', response.status)
  }
  let result: unknown
  try { result = await readProtectedJson(response) } catch (error) {
    if (isAuthBoundaryError(error)) throw error
    throw new ApiError('No usable transcript was returned. Recheck the interview before continuing.', response.status, true)
  }
  if (!object(result) || typeof result.text !== 'string' || !result.text.trim() || result.text.length > 10000 ||
      result.session_id !== session.id || result.question_index !== session.current_question_index ||
      typeof result.measurement_id !== 'string' ||
      !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(result.measurement_id)) {
    throw new ApiError('No usable transcript was returned. Please try again or type your answer.', response.status, true)
  }
  if (!validSpeakingMetrics(result.metrics)) {
    throw new ApiError('No usable speaking measurements were returned. Please try again or type your answer.', response.status, true)
  }
  if (!validLiveDeliveryMetrics(result.delivery_metrics) || (result.delivery_metrics.pause_count !== null &&
      (result.metrics.recognized_word_count < 1 || result.delivery_metrics.pause_count > result.metrics.recognized_word_count - 1))) {
    throw new ApiError('No usable delivery measurements were returned. Please try again or type your answer.', response.status, true)
  }
  return result as unknown as TranscriptionResult
}
