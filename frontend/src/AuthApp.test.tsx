// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import App from './App'
import * as auth from './auth'
import { authenticateTestWorkspace } from './authTestUtils'

const userA = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
const userB = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
const sessionId = '11111111-1111-4111-8111-111111111111'
const session = { id: sessionId, scenario_type: 'job_interview', interviewer_persona_id: 'recruiter', question_engine: 'deterministic-v1', total_questions: 5, status: 'active', current_question_index: 0,
  current_question: 'Immediate practice question', current_question_latest_attempt_number: 0,
  questions: ['Immediate practice question', 'Two', 'Three', 'Four', 'Five'], answers: [] }
const summary = { session_id: sessionId, scenario_type: 'job_interview', question_engine: 'deterministic-v1', status: 'active', created_at: '2026-10-06T12:00:00Z',
  completed_at: null, current_question_number: 1, total_questions: 5, finalized_question_count: 0,
  questions_practiced_count: 0, total_attempt_count: 0, total_retry_count: 0,
  measured_final_answer_count: 0, last_submitted_at: null, last_saved_activity_at: '2026-10-06T12:00:00Z', finalized_points: [] }
function json(value: unknown, status = 200) { return new Response(JSON.stringify(value), { status }) }
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}
function api(override?: (path: string, options?: RequestInit) => Response | Promise<Response> | undefined) {
  const mock = vi.fn(async (path: RequestInfo | URL, options?: RequestInit): Promise<Response> => {
    const url = String(path)
    const replacement = override?.(url, options)
    if (replacement) return await replacement
    if (url === '/api/auth/me') return json({ user_id: userA, request_context: 'context-A' })
    if (url === '/api/health') return json({ status: 'ok', service: 'rehearse-api' })
    if (url === '/api/auth/logout') return new Response(null, { status: 204 })
    if (url === '/api/sessions') return json(session, 201)
    if (url === '/api/history/summaries') return json({ items: [summary], next_cursor: null })
    throw new Error('Unexpected synthetic application request.')
  })
  vi.stubGlobal('fetch', mock)
  return mock
}
beforeEach(() => {
  localStorage.clear(); sessionStorage.clear()
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Unmocked requests are forbidden.')))
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); localStorage.clear(); sessionStorage.clear() })
async function workspace() { await screen.findByRole('button', { name: 'Start Interview' }) }
async function start() {
  await workspace()
  fireEvent.click(screen.getByRole('button', { name: 'Start Interview' }))
  await screen.findByRole('textbox', { name: 'Your answer' })
}
function calls(mock: ReturnType<typeof api>, path: string) { return mock.mock.calls.filter(([url]) => String(url) === path) }

test('initial bootstrap has loading UI, no sign-in flash, and no protected request', async () => {
  const pending = deferred<Response>()
  const mock = api((path) => path === '/api/auth/me' ? pending.promise : undefined)
  render(<App />)
  expect(screen.getByText('Checking sign-in…')).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Sign in' })).toBeNull()
  expect(screen.queryByRole('button', { name: 'Start Interview' })).toBeNull()
  expect(calls(mock, '/api/auth/me')).toHaveLength(1)
  expect(new Headers(calls(mock, '/api/auth/me')[0][1]?.headers).has(auth.AUTH_CONTEXT_HEADER)).toBe(false)
  await act(async () => { pending.resolve(json({ user_id: userA, request_context: 'context-A' })) })
  await workspace()
  expect(calls(mock, '/api/sessions')).toHaveLength(0)
})

test('401 bootstrap offers explicit navigation to login without fetching it', async () => {
  const mock = api((path) => path === '/api/auth/me' ? json({}, 401) : undefined)
  const navigate = vi.spyOn(auth, 'signIn').mockImplementation(() => {})
  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: 'Sign in' }))
  expect(navigate).toHaveBeenCalledOnce()
  expect(calls(mock, '/api/auth/login')).toHaveLength(0)
  expect(screen.queryByRole('navigation')).toBeNull()
})

test.each([json({}, 503), json({ user_id: userA }), json({ user_id: userA, request_context: 'context-A', extra: true })])(
  'unavailable or malformed bootstrap fails closed without signed-out workspace', async (result) => {
    api((path) => path === '/api/auth/me' ? result : undefined)
    render(<App />)
    await screen.findByRole('region', { name: 'Authentication unavailable' })
    expect(screen.queryByRole('button', { name: 'Sign in' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Start Interview' })).toBeNull()
  },
)

test.each([401, 403])('protected mutation %s clears private workspace without bootstrap or replay', async (status) => {
  const reload = vi.spyOn(auth, 'reloadSignIn').mockImplementation(() => {})
  const path = `/api/sessions/${sessionId}/questions/0/attempts`
  const mock = api((url) => url === path ? json({ detail: 'PRIVATE_FAILURE_SENTINEL' }, status) : undefined)
  render(<App />); await start()
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'PRIVATE_DRAFT_A' } })
  fireEvent.click(screen.getByRole('button', { name: 'Submit Attempt' }))
  await screen.findByRole('button', { name: status === 401 ? 'Sign in' : 'Reload current sign-in' })
  expect(calls(mock, path)).toHaveLength(1)
  expect(calls(mock, '/api/auth/me')).toHaveLength(1)
  expect(document.body.textContent).not.toContain('PRIVATE_DRAFT_A')
  expect(document.body.textContent).not.toContain('PRIVATE_FAILURE_SENTINEL')
  expect(sessionStorage.getItem(`rehearse.session_id:${userA}`)).toBeNull()
  if (status === 403) {
    fireEvent.click(screen.getByRole('button', { name: 'Reload current sign-in' }))
    expect(reload).toHaveBeenCalledOnce()
    expect(calls(mock, '/api/auth/me')).toHaveLength(1)
  }
})

test('switching auth generation clears A draft and scoped restore ID without reading it as B', async () => {
  const mock = api()
  render(<App />); await start()
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'PRIVATE_A_DRAFT' } })
  expect(sessionStorage.getItem(`rehearse.session_id:${userA}`)).toBe(sessionId)
  await act(async () => { await authenticateTestWorkspace('context-B', userB) })
  await workspace()
  expect(sessionStorage.getItem(`rehearse.session_id:${userA}`)).toBeNull()
  expect(sessionStorage.getItem(`rehearse.session_id:${userB}`)).toBeNull()
  expect(screen.queryByRole('textbox')).toBeNull()
  expect(document.body.textContent).not.toContain('PRIVATE_A_DRAFT')
  expect(calls(mock, `/api/sessions/${sessionId}`)).toHaveLength(0)
})

test('authentication 503 on a protected operation retains workspace and never automatically repeats the mutation', async () => {
  const mock = api((path) => path === '/api/sessions' ? json({ detail: 'Authentication is temporarily unavailable.' }, 503) : undefined)
  render(<App />); await workspace()
  fireEvent.click(screen.getByRole('button', { name: 'Start Interview' }))
  await screen.findByText(auth.AUTH_UNAVAILABLE_MESSAGE)
  expect(screen.getByRole('button', { name: 'Log out' })).toBeTruthy()
  expect(calls(mock, '/api/sessions')).toHaveLength(1)
  expect(calls(mock, '/api/auth/me')).toHaveLength(1)
})

test('404 resource failure does not invalidate authentication', async () => {
  api((path) => path === '/api/sessions' ? json({}, 404) : undefined)
  render(<App />); await workspace()
  fireEvent.click(screen.getByRole('button', { name: 'Start Interview' }))
  await waitFor(() => expect(auth.getAuthState().status).toBe('authenticated'))
  expect(screen.getByRole('button', { name: 'Log out' })).toBeTruthy()
})

test('logout clears Practice and History; a late request cannot refill either', async () => {
  const pending = deferred<Response>()
  const mock = api((path) => path === '/api/sessions' ? pending.promise : undefined)
  render(<App />); await workspace()
  fireEvent.click(screen.getByRole('button', { name: 'History' }))
  await screen.findByRole('heading', { name: 'Active session' })
  fireEvent.click(screen.getByRole('button', { name: /^Practice$/ }))
  fireEvent.click(screen.getByRole('button', { name: 'Start Interview' }))
  fireEvent.click(screen.getByRole('button', { name: 'Log out' }))
  await screen.findByRole('button', { name: 'Sign in' })
  await act(async () => { pending.resolve(json(session, 201)) })
  expect(screen.queryByText('Immediate practice question')).toBeNull()
  expect(screen.queryByRole('heading', { name: 'Active session' })).toBeNull()
  expect(calls(mock, '/api/auth/logout')).toHaveLength(1)
  expect(new Headers(calls(mock, '/api/auth/logout')[0][1]?.headers).get(auth.AUTH_CONTEXT_HEADER)).toBe('context-A')
})

test.each([401, 403, 503])('logout %s uses fixed state transition and does not replay', async (status) => {
  const mock = api((path) => path === '/api/auth/logout' ? json({ detail: 'PRIVATE_LOGOUT_SENTINEL' }, status) : undefined)
  render(<App />); await start()
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Keep the current draft' } })
  fireEvent.click(screen.getByRole('button', { name: 'Log out' }))
  if (status === 503) {
    await screen.findByText('Unable to sign out right now. Please try again.')
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('Keep the current draft')
  } else await screen.findByRole('button', { name: status === 401 ? 'Sign in' : 'Reload current sign-in' })
  expect(calls(mock, '/api/auth/logout')).toHaveLength(1)
  expect(calls(mock, '/api/auth/me')).toHaveLength(1)
  expect(document.body.textContent).not.toContain('PRIVATE_LOGOUT_SENTINEL')
})

test('A response finishing after explicit B bootstrap cannot populate B or reuse A restore ID', async () => {
  const pending = deferred<Response>()
  const mock = api((path) => path === '/api/sessions' ? pending.promise : undefined)
  sessionStorage.setItem('rehearse.session_id', sessionId)
  render(<App />); await workspace()
  expect(calls(mock, `/api/sessions/${sessionId}`)).toHaveLength(0)
  fireEvent.click(screen.getByRole('button', { name: 'Start Interview' }))
  await act(async () => { await authenticateTestWorkspace('context-B', userB) })
  await workspace()
  await act(async () => { pending.resolve(json(session, 201)) })
  expect(screen.queryByText('Immediate practice question')).toBeNull()
  expect(sessionStorage.getItem(`rehearse.session_id:${userB}`)).toBeNull()
  expect(auth.getAuthState()).toMatchObject({ userId: userB, requestContext: 'context-B' })
})

test('browser history keys and storage events cannot choose server membership or cause a request', async () => {
  const mock = api()
  localStorage.setItem('rehearse.history.v1:99999999-9999-4999-8999-999999999999', '1')
  render(<App />); await workspace()
  fireEvent.click(screen.getByRole('button', { name: 'History' }))
  await screen.findByRole('heading', { name: 'Active session' })
  expect(calls(mock, '/api/history/summaries')).toHaveLength(1)
  expect(calls(mock, '/api/history/summaries')[0][1]?.method).toBe('GET')
  expect(calls(mock, '/api/history/summaries')[0][1]?.body).toBeUndefined()
  fireEvent(window, new StorageEvent('storage', { key: 'rehearse.history.v1:other' }))
  expect(calls(mock, '/api/history/summaries')).toHaveLength(1)
  expect(screen.queryByRole('button', { name: 'Clear remembered history' })).toBeNull()
  expect(mock.mock.calls.some(([path]) => String(path).includes('99999999'))).toBe(false)
})


function adaptivePracticeApi(continueResponse: () => Response | Promise<Response>) {
  let current = { ...session, question_engine: 'live-ai-roleplay-v1', questions: session.questions.slice(0, 1) }
  const attempt = { id: '44444444-4444-4444-8444-444444444444', question_index: 0, attempt_number: 1,
    answer: 'Saved adaptive answer', submitted_at: '2026-10-06T12:00:00Z', measurement_id: null }
  return api((path, options) => {
    if (path === '/api/sessions') return json(current, 201)
    if (path === `/api/sessions/${sessionId}`) return json(current)
    if (path.endsWith('/questions/0/attempts')) {
      if (options?.method === 'POST') {
        current = { ...current, current_question_latest_attempt_number: 1 }
        return json({ attempt, session: current }, 201)
      }
      return json(current.current_question_latest_attempt_number === 1 ? [attempt] : [])
    }
    if (path.endsWith('/diagnosis')) return json({ detail: 'Semantic diagnosis is not configured.' }, 503)
    if (path.endsWith('/questions/0/continue')) return continueResponse()
  })
}
async function adaptiveReview() {
  await start()
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Saved adaptive answer' } })
  fireEvent.click(screen.getByRole('button', { name: 'Submit Attempt' }))
  await screen.findByRole('button', { name: 'Continue' })
  await waitFor(() => expect((screen.getByRole('button', { name: 'Continue' }) as HTMLButtonElement).disabled).toBe(false))
}

test.each([401, 403])('adaptive Continue %s clears the private workspace and never replays', async (status) => {
  const mock = adaptivePracticeApi(() => json({ detail: 'PRIVATE' }, status))
  render(<App />); await adaptiveReview()
  fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
  await screen.findByRole('button', { name: status === 401 ? 'Sign in' : 'Reload current sign-in' })
  expect(document.body.textContent).not.toContain('Saved adaptive answer')
  expect(document.body.textContent).not.toContain('PRIVATE')
  expect(calls(mock, `/api/sessions/${sessionId}/questions/0/continue`)).toHaveLength(1)
  expect(calls(mock, '/api/auth/me')).toHaveLength(1)
})

test('auth generation change aborts adaptive Continue and its late success cannot refill the new account', async () => {
  const pending = deferred<Response>()
  const mock = adaptivePracticeApi(() => pending.promise)
  render(<App />); await adaptiveReview()
  fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
  const signal = calls(mock, `/api/sessions/${sessionId}/questions/0/continue`)[0][1]?.signal
  expect(signal?.aborted).toBe(false)
  await act(async () => { await authenticateTestWorkspace('context-B', userB) })
  await workspace()
  expect(signal?.aborted).toBe(true)
  await act(async () => pending.resolve(json({ ...session, question_engine: 'live-ai-roleplay-v1',
    current_question_index: 1, current_question: 'PRIVATE_LATE_QUESTION', questions: [session.questions[0], 'PRIVATE_LATE_QUESTION'],
    answers: ['Saved adaptive answer'] })))
  expect(document.body.textContent).not.toContain('PRIVATE_LATE_QUESTION')
  expect(document.body.textContent).not.toContain('Saved adaptive answer')
  expect(auth.getAuthState()).toMatchObject({ requestContext: 'context-B' })
  expect(calls(mock, `/api/sessions/${sessionId}/questions/0/continue`)).toHaveLength(1)
})
