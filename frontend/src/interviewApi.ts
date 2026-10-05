export interface InterviewSession {
  id: string
  status: 'active' | 'completed'
  current_question_index: number
  current_question: string | null
  questions: string[]
  answers: string[]
}

async function requestSession(path: string, options?: RequestInit): Promise<InterviewSession> {
  let response: Response
  try {
    response = await fetch(path, { ...options, signal: AbortSignal.timeout(10000) })
  } catch {
    throw new Error('Unable to reach the backend. Check your connection and try again.')
  }
  if (!response.ok) {
    if (response.status === 404) throw new Error('Session not found. The backend may have restarted. Start a new interview.')
    throw new Error('Unable to update the interview. Please try again.')
  }
  return response.json() as Promise<InterviewSession>
}

export function startInterview(): Promise<InterviewSession> {
  return requestSession('/api/sessions', { method: 'POST' })
}

export async function submitAnswer(session: InterviewSession, answer: string, measurementId?: string | null): Promise<InterviewSession> {
  // Reconcile after a lost response before retrying a write. The server also
  // rejects stale indices so simultaneous requests cannot skip a question.
  const current = await requestSession(`/api/sessions/${session.id}`)
  if (current.current_question_index !== session.current_question_index) return current
  return requestSession(`/api/sessions/${session.id}/answers`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question_index: session.current_question_index, answer,
      ...(measurementId == null ? {} : { measurement_id: measurementId }) }),
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
  const body = audioForm(session, audio)
  let response: Response
  try {
    response = await fetch(`/api/sessions/${session.id}/audio`, {
      method: 'POST', body, signal: AbortSignal.any([signal, AbortSignal.timeout(30000)]),
    })
  } catch {
    throw new Error('Audio upload failed or timed out. Check your connection and try again.')
  }
  if (!response.ok) {
    const messages: Record<number, string> = {
      404: 'Session not found. Start a new interview.',
      409: 'This question is no longer current. Record an answer for the current question.',
      413: 'Recording is too large. Record a shorter answer (maximum 10 MiB).',
      415: 'This audio format is not supported. Try another browser or type your answer.',
      422: 'The recording was empty or invalid. Please record again.',
    }
    throw new Error(messages[response.status] || 'Audio upload failed. Please try again.')
  }
  const result: AudioAccepted = await response.json()
  if (result.status !== 'accepted' || result.session_id !== session.id || result.question_index !== session.current_question_index) {
    throw new Error('Unexpected upload confirmation. Please try again.')
  }
  return result
}

function audioForm(session: InterviewSession, audio: Blob): FormData {
  const extensions: Record<string, string> = { 'audio/webm': 'webm', 'audio/ogg': 'ogg', 'audio/mp4': 'm4a', 'audio/mpeg': 'mp3', 'audio/wav': 'wav', 'audio/x-wav': 'wav' }
  const extension = extensions[audio.type.split(';')[0]] || 'audio'
  const body = new FormData()
  body.append('question_index', String(session.current_question_index))
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
  timing_unavailable_reason:
    | 'missing_timings'
    | 'timing_coverage_mismatch'
    | 'invalid_timing'
    | 'invalid_timing_order'
    | 'unusable_span'
    | null
}

export interface TranscriptionResult {
  session_id: string
  question_index: number
  measurement_id: string
  text: string
  language: string | null
  words: { text: string; start: number; end: number }[]
  metrics: SpeakingMetrics
}

function validSpeakingMetrics(value: unknown): value is SpeakingMetrics {
  if (typeof value !== 'object' || value === null) return false
  const metrics = value as Record<string, unknown>
  const count = (number: unknown) => typeof number === 'number' && Number.isSafeInteger(number) && number >= 0
  const positive = (number: unknown) => typeof number === 'number' && Number.isFinite(number) && number > 0
  const timingReasons: unknown[] = ['missing_timings', 'timing_coverage_mismatch', 'invalid_timing', 'invalid_timing_order', 'unusable_span']
  return metrics.source === 'original_transcription' && count(metrics.recognized_word_count) &&
    (metrics.filler_unavailable_reason === null
      ? count(metrics.um_count) && count(metrics.uh_count)
      : metrics.filler_unavailable_reason === 'unsupported_language' && metrics.um_count === null && metrics.uh_count === null) &&
    (metrics.timing_unavailable_reason === null
      ? positive(metrics.timed_utterance_span_seconds) && positive(metrics.estimated_words_per_minute)
      : timingReasons.includes(metrics.timing_unavailable_reason) && metrics.timed_utterance_span_seconds === null && metrics.estimated_words_per_minute === null)
}

export async function transcribeAudio(session: InterviewSession, audio: Blob, signal: AbortSignal): Promise<TranscriptionResult> {
  let response: Response
  try {
    response = await fetch(`/api/sessions/${session.id}/transcriptions`, {
      method: 'POST', body: audioForm(session, audio),
      signal: AbortSignal.any([signal, AbortSignal.timeout(75000)]),
    })
  } catch {
    throw new Error('Transcription failed or timed out. Check your connection and try again.')
  }
  if (!response.ok) {
    const messages: Record<number, string> = {
      404: 'Session not found. Start a new interview.',
      409: 'This question is no longer current. Record an answer for the current question.',
      413: 'Recording is too large. Record a shorter answer (maximum 10 MiB).',
      415: 'This audio format is not supported. Try another browser or type your answer.',
      422: 'The recording was empty or invalid. Please record again.',
      502: 'Unable to transcribe this recording. Try again or type your answer.',
      503: 'Transcription is not configured on the server. You can still type your answer.',
      504: 'Transcription timed out. Please try again or type your answer.',
    }
    throw new Error(messages[response.status] || 'Transcription failed. Please try again.')
  }
  const result: TranscriptionResult = await response.json()
  if (typeof result?.text !== 'string' || !result.text.trim() || result.text.length > 10000 ||
      result.session_id !== session.id || result.question_index !== session.current_question_index ||
      typeof result.measurement_id !== 'string' ||
      !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(result.measurement_id)) {
    throw new Error('No usable transcript was returned. Please try again or type your answer.')
  }
  if (!validSpeakingMetrics(result.metrics)) {
    throw new Error('No usable speaking measurements were returned. Please try again or type your answer.')
  }
  return result
}
