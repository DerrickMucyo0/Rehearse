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

export async function submitAnswer(session: InterviewSession, answer: string): Promise<InterviewSession> {
  // Reconcile after a lost response before retrying a write. The server also
  // rejects stale indices so simultaneous requests cannot skip a question.
  const current = await requestSession(`/api/sessions/${session.id}`)
  if (current.current_question_index !== session.current_question_index) return current
  return requestSession(`/api/sessions/${session.id}/answers`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question_index: session.current_question_index, answer }),
  })
}
