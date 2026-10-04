export interface InterviewTurn {
  question_index: number
  turn_revision: number
  submission_id: string
  prompt: string
  answer: string
  action: 'FOLLOW_UP' | 'CLARIFY' | 'CHALLENGE' | 'MOVE_ON' | null
  transition_source: 'nemotron' | 'probe_limit'
}

export interface InterviewSession {
  id: string
  status: 'active' | 'completed'
  current_question_index: number
  current_question: string | null
  current_prompt: string | null
  turn_revision: number
  probe_count: number
  turns: InterviewTurn[]
  questions: string[]
  answers: string[]
}

async function requestSession(path: string, options?: RequestInit, timeout = 10000): Promise<InterviewSession> {
  let response: Response
  try {
    response = await fetch(path, { ...options, signal: AbortSignal.timeout(timeout) })
  } catch {
    throw new Error('Unable to reach the backend. Check your connection and try again.')
  }
  if (!response.ok) {
    if (response.status === 404) throw new Error('Session not found. The backend may have restarted. Start a new interview.')
    const messages: Record<number, string> = {
      409: 'This turn changed or an answer is still being processed. Retry to reconcile, or start a new interview.',
      502: 'Unable to evaluate this answer. Your draft is preserved. Please retry.',
      503: 'Interviewer reasoning is not configured on the server. Your draft is preserved.',
      504: 'Interviewer reasoning timed out. Your draft is preserved. Please retry.',
    }
    throw new Error(messages[response.status] || 'Unable to update the interview. Please try again.')
  }
  return response.json() as Promise<InterviewSession>
}

export function startInterview(): Promise<InterviewSession> {
  return requestSession('/api/sessions', { method: 'POST' })
}

export async function submitAnswer(session: InterviewSession, answer: string, submissionId: string): Promise<InterviewSession> {
  // Only reconcile our exact accepted submission, not an unrelated same-question turn.
  const current = await requestSession(`/api/sessions/${session.id}`)
  const accepted = current.turns.find((turn) => turn.submission_id === submissionId)
  if (accepted && accepted.turn_revision === session.turn_revision &&
      accepted.question_index === session.current_question_index && accepted.answer === answer) return current
  if (current.turn_revision !== session.turn_revision || current.current_question_index !== session.current_question_index) {
    throw new Error('This turn changed in another request. Your draft is preserved. Start a new interview to continue.')
  }
  return requestSession(`/api/sessions/${session.id}/answers`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question_index: session.current_question_index, turn_revision: session.turn_revision,
      submission_id: submissionId, answer }),
  }, 45000)
}

export interface AudioAccepted {
  session_id: string
  question_index: number
  turn_revision: number
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
      409: 'This turn is no longer current. Record an answer for the current prompt.',
      413: 'Recording is too large. Record a shorter answer (maximum 10 MiB).',
      415: 'This audio format is not supported. Try another browser or type your answer.',
      422: 'The recording was empty or invalid. Please record again.',
    }
    throw new Error(messages[response.status] || 'Audio upload failed. Please try again.')
  }
  const result: AudioAccepted = await response.json()
  if (result.status !== 'accepted' || result.session_id !== session.id || result.question_index !== session.current_question_index || result.turn_revision !== session.turn_revision) {
    throw new Error('Unexpected upload confirmation. Please try again.')
  }
  return result
}

function audioForm(session: InterviewSession, audio: Blob): FormData {
  const extensions: Record<string, string> = { 'audio/webm': 'webm', 'audio/ogg': 'ogg', 'audio/mp4': 'm4a', 'audio/mpeg': 'mp3', 'audio/wav': 'wav', 'audio/x-wav': 'wav' }
  const extension = extensions[audio.type.split(';')[0]] || 'audio'
  const body = new FormData()
  body.append('question_index', String(session.current_question_index))
  body.append('turn_revision', String(session.turn_revision))
  body.append('audio', audio, `answer-${session.current_question_index + 1}.${extension}`)
  return body
}

export interface TranscriptionResult {
  session_id: string
  question_index: number
  turn_revision: number
  text: string
  language: string | null
  words: { text: string; start: number; end: number }[]
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
      409: 'This turn is no longer current. Record an answer for the current prompt.',
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
      result.session_id !== session.id || result.question_index !== session.current_question_index || result.turn_revision !== session.turn_revision) {
    throw new Error('No usable transcript was returned. Please try again or type your answer.')
  }
  return result
}
