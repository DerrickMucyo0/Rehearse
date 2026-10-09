import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { AUTH_CONTEXT_HEADER, AUTH_UNAVAILABLE_MESSAGE, AuthBoundaryError, assertProtectedResponseCurrent,
  bootstrapAuth, getAuthState, logoutAuth, protectedFetch, readProtectedBlob, readProtectedJson, reloadSignIn, signIn, subscribeAuth } from './auth'

const userA = '4b12df9e-1dbe-418e-b5c1-82b720211d82'
const userB = '144b50e1-0183-428c-943f-1850df006b66'
const bootstrapA = { user_id: userA, request_context: 'context-A' }
function json(value: unknown, status = 200) { return new Response(JSON.stringify(value), { status }) }
function mockFetch(response: Response) {
  const mock = vi.fn().mockResolvedValue(response)
  vi.stubGlobal('fetch', mock)
  return mock
}
async function authenticate(value = bootstrapA) {
  const mock = mockFetch(json(value))
  await bootstrapAuth()
  mock.mockClear()
  return mock
}
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}
beforeEach(async () => {
  mockFetch(json({}, 401))
  await bootstrapAuth()
})
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

test('bootstrap stays loading until /me finishes and publishes authenticated immutable memory state', async () => {
  const result = deferred<Response>()
  const mock = vi.fn().mockReturnValue(result.promise)
  vi.stubGlobal('fetch', mock)
  const notices: string[] = []
  const unsubscribe = subscribeAuth(() => { notices.push(getAuthState().status) })
  const pending = bootstrapAuth()
  expect(getAuthState()).toEqual({ status: 'loading' })
  result.resolve(json(bootstrapA))
  await pending
  expect(getAuthState()).toMatchObject({ status: 'authenticated', userId: userA, requestContext: 'context-A', notice: null })
  expect(Object.isFrozen(getAuthState())).toBe(true)
  expect(notices).toEqual(['loading', 'authenticated'])
  unsubscribe()
  const [path, options] = mock.mock.calls[0]
  expect(path).toBe('/api/auth/me')
  expect(options).toMatchObject({ method: 'GET', credentials: 'same-origin', cache: 'no-store' })
  expect(options.headers).toBeUndefined()
  expect(options.signal).toBeInstanceOf(AbortSignal)
})

test.each([[401, 'signed_out'], [503, 'unavailable'], [403, 'unavailable'], [500, 'unavailable']] as const)(
  '/me %s enters %s without redirecting or fetching again', async (status, expected) => {
    const mock = mockFetch(json({ detail: 'private detail' }, status))
    await bootstrapAuth()
    expect(getAuthState()).toEqual({ status: expected })
    expect(mock).toHaveBeenCalledTimes(1)
  },
)

test.each([
  null, [], {}, { ...bootstrapA, extra: 'not allowed' }, { user_id: userA },
  { request_context: 'context-A' }, { ...bootstrapA, user_id: 'not-a-uuid' },
  { ...bootstrapA, request_context: '' }, { ...bootstrapA, request_context: ' ' },
  { ...bootstrapA, request_context: 'context\r\nInjected: value' },
  { ...bootstrapA, request_context: '\x00' }, { ...bootstrapA, request_context: 1 },
  { ...bootstrapA, request_context: ' context-A ' }, { ...bootstrapA, request_context: '\u0100' },
])('malformed successful bootstrap fails closed without exposing payload (case %#)', async (value) => {
  const mock = mockFetch(json(value))
  await bootstrapAuth()
  expect(getAuthState()).toEqual({ status: 'unavailable' })
  expect(mock).toHaveBeenCalledTimes(1)
})

test.each(['invalid JSON', 'transport failure'])('bootstrap %s is sanitized unavailable without retry', async (failure) => {
  const mock = vi.fn()
  if (failure === 'invalid JSON') mock.mockResolvedValue(new Response('sensitive raw content'))
  else mock.mockRejectedValue(new Error('sensitive transport detail'))
  vi.stubGlobal('fetch', mock)
  await bootstrapAuth()
  expect(getAuthState()).toEqual({ status: 'unavailable' })
  expect(mock).toHaveBeenCalledTimes(1)
})

test('sign-in is an exact browser navigation and stale re-entry explicitly reloads the page', () => {
  const assign = vi.fn()
  const reload = vi.fn()
  vi.stubGlobal('window', { location: { assign, reload } })
  signIn()
  reloadSignIn()
  expect(assign).toHaveBeenCalledExactlyOnceWith('/api/auth/login')
  expect(reload).toHaveBeenCalledExactlyOnceWith()
})

test('protected request carries exact context and same-origin cookies without identity or credential headers', async () => {
  const mock = await authenticate()
  mock.mockResolvedValue(json({ result: 1 }))
  const response = await protectedFetch('/api/sessions', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
  expect(await readProtectedJson(response)).toEqual({ result: 1 })
  const [path, options] = mock.mock.calls[0]
  expect(path).toBe('/api/sessions')
  expect(options).toMatchObject({ method: 'POST', credentials: 'same-origin', cache: 'no-store', body: '{}' })
  const headers = new Headers(options.headers)
  expect(headers.get(AUTH_CONTEXT_HEADER)).toBe('context-A')
  expect(headers.get('Content-Type')).toBe('application/json')
  expect(headers.has('Authorization')).toBe(false)
  expect(headers.has('Cookie')).toBe(false)
  expect(headers.has('X-User-Id')).toBe(false)
  expect(path).not.toContain(userA)
  expect(path).not.toContain('context-A')
})

test.each(['https://example.test/api/sessions', '//example.test/api/sessions', '/outside', '/api/\\external', '/api/sessions\r\n',
  '/api/../outside', '/api/sessions#fragment', '/api/auth/me', '/api/auth/login', '/api/auth/callback'])(
  'protected transport rejects non-application path %s before fetch', async (path) => {
    const mock = await authenticate()
    await expect(protectedFetch(path)).rejects.toBeInstanceOf(AuthBoundaryError)
    expect(mock).not.toHaveBeenCalled()
  },
)

test.each(['Authorization', 'Cookie', 'X-User-Id'])('protected transport rejects caller %s authorization before fetch', async (header) => {
  const mock = await authenticate()
  await expect(protectedFetch('/api/sessions', { headers: { [header]: 'caller-controlled' } })).rejects.toBeInstanceOf(AuthBoundaryError)
  expect(mock).not.toHaveBeenCalled()
})

test('protected transport overrides caller context with the authoritative bootstrap value', async () => {
  const mock = await authenticate()
  mock.mockResolvedValue(json({}))
  await protectedFetch('/api/sessions', { headers: { [AUTH_CONTEXT_HEADER]: 'other-context' } })
  expect(new Headers(mock.mock.calls[0][1].headers).get(AUTH_CONTEXT_HEADER)).toBe('context-A')
})

test.each([[401, 'signed_out'], [403, 'stale']] as const)(
  'protected mutation %s invalidates workspace to %s with no bootstrap or replay', async (status, expected) => {
    const mock = await authenticate()
    const response = json({ detail: 'sensitive private detail' }, status)
    const body = vi.spyOn(response, 'json')
    mock.mockResolvedValue(response)
    const error = await protectedFetch('/api/sessions', { method: 'POST' }).catch((cause: unknown) => cause)
    expect(error).toMatchObject({ name: 'AuthBoundaryError', status })
    expect((error as Error).message).not.toContain('sensitive')
    expect(getAuthState()).toEqual({ status: expected })
    expect(mock).toHaveBeenCalledTimes(1)
    expect(mock.mock.calls[0][0]).toBe('/api/sessions')
    expect(body).not.toHaveBeenCalled()
    await expect(protectedFetch('/api/sessions')).rejects.toBeInstanceOf(AuthBoundaryError)
    expect(mock).toHaveBeenCalledTimes(1)
  },
)

test.each(['/api/sessions', '/api/sessions/A/questions/0/attempts/1/diagnosis', '/api/sessions/A/questions/0/speech', '/api/history/summaries'])(
  'authentication 503 on %s preserves exact auth generation and reports unavailability without replay', async (path) => {
    const mock = await authenticate()
    const before = getAuthState()
    const body = { detail: 'Authentication is temporarily unavailable.' }
    mock.mockResolvedValue(json(body, 503))
    const response = await protectedFetch(path, { method: 'POST' })
    expect(response.status).toBe(503)
    expect(getAuthState()).toEqual({ ...before, notice: AUTH_UNAVAILABLE_MESSAGE })
    expect(await readProtectedJson(response)).toEqual(body)
    expect(mock).toHaveBeenCalledTimes(1)
  },
)

test.each([
  { detail: 'Semantic diagnosis is not configured.' },
  { detail: 'PRIVATE_PROVIDER_DETAIL' },
  {}, null, [],
  { detail: 'Authentication is temporarily unavailable.', extra: 'PRIVATE_PROVIDER_DETAIL' },
  { detail: 'Authentication is temporarily unavailable. ' },
])('non-authentication 503 keeps the workspace and original response unchanged (case %#)', async (body) => {
  const mock = await authenticate()
  const before = getAuthState()
  mock.mockResolvedValue(json(body, 503))
  const response = await protectedFetch('/api/sessions/A/questions/0/attempts/1/diagnosis', { method: 'POST' })
  expect(response.status).toBe(503)
  expect(getAuthState()).toEqual(before)
  expect(await readProtectedJson(response)).toEqual(body)
  expect(mock).toHaveBeenCalledTimes(1)
})

test('non-JSON 503 stays local and leaves the original response readable', async () => {
  const mock = await authenticate()
  const before = getAuthState()
  mock.mockResolvedValue(new Response('PRIVATE_NON_JSON_ERROR', { status: 503 }))
  const response = await protectedFetch('/api/sessions/A/questions/0/attempts/1/diagnosis')
  expect(getAuthState()).toEqual(before)
  expect(await response.text()).toBe('PRIVATE_NON_JSON_ERROR')
  expect(mock).toHaveBeenCalledTimes(1)
})

test('a feature 503 does not clear an existing authentication-unavailable notice', async () => {
  const mock = await authenticate()
  mock.mockResolvedValueOnce(json({ detail: 'Authentication is temporarily unavailable.' }, 503))
  await protectedFetch('/api/sessions')
  const before = getAuthState()
  mock.mockResolvedValueOnce(json({ detail: 'Semantic diagnosis is not configured.' }, 503))
  await protectedFetch('/api/sessions/A/questions/0/attempts/1/diagnosis')
  expect(getAuthState()).toEqual(before)
  expect(mock).toHaveBeenCalledTimes(2)
})

test.each(['auth detail', 'parse failure'] as const)('late 503 %s classification cannot update authenticated B', async (outcome) => {
  const mock = await authenticate()
  const body = deferred<unknown>()
  const response = json({}, 503)
  const clone = json({}, 503)
  const read = vi.spyOn(clone, 'json').mockReturnValue(body.promise)
  vi.spyOn(response, 'clone').mockReturnValue(clone)
  mock.mockResolvedValueOnce(response).mockResolvedValueOnce(json({ user_id: userB, request_context: 'context-B' }))
  const pending = protectedFetch('/api/sessions/A/questions/0/attempts/1/diagnosis').catch((cause: unknown) => cause)
  await vi.waitFor(() => expect(read).toHaveBeenCalledOnce())
  await bootstrapAuth()
  const newer = getAuthState()
  if (outcome === 'auth detail') body.resolve({ detail: 'Authentication is temporarily unavailable.' })
  else body.reject(new SyntaxError('PRIVATE_PARSER_DETAIL'))
  expect(await pending).toBeInstanceOf(AuthBoundaryError)
  expect(getAuthState()).toEqual(newer)
  expect(mock).toHaveBeenCalledTimes(2)
})

test.each(['auth detail', 'parse failure'] as const)('caller cancellation during 503 %s classification propagates', async (outcome) => {
  const mock = await authenticate()
  const before = getAuthState()
  const body = deferred<unknown>()
  const response = json({}, 503)
  const clone = json({}, 503)
  const read = vi.spyOn(clone, 'json').mockReturnValue(body.promise)
  vi.spyOn(response, 'clone').mockReturnValue(clone)
  mock.mockResolvedValue(response)
  const controller = new AbortController()
  const pending = protectedFetch('/api/sessions/A/questions/0/attempts/1/diagnosis', { signal: controller.signal })
    .catch((cause: unknown) => cause)
  await vi.waitFor(() => expect(read).toHaveBeenCalledOnce())
  controller.abort()
  if (outcome === 'auth detail') body.resolve({ detail: 'Authentication is temporarily unavailable.' })
  else body.reject(new SyntaxError('PRIVATE_PARSER_DETAIL'))
  expect(await pending).toMatchObject({ name: 'AbortError' })
  expect(getAuthState()).toEqual(before)
  expect(mock).toHaveBeenCalledTimes(1)
})

test('protected 404 remains resource behavior and does not invalidate auth', async () => {
  const mock = await authenticate()
  const before = getAuthState()
  mock.mockResolvedValue(json({}, 404))
  expect((await protectedFetch('/api/sessions/missing')).status).toBe(404)
  expect(getAuthState()).toEqual(before)
})

test('logout uses exact authenticated context and 204 clears workspace without provider logout', async () => {
  const mock = await authenticate()
  mock.mockResolvedValue(new Response(null, { status: 204 }))
  await logoutAuth()
  expect(getAuthState()).toEqual({ status: 'signed_out' })
  expect(mock).toHaveBeenCalledTimes(1)
  expect(mock.mock.calls[0][0]).toBe('/api/auth/logout')
  const options = mock.mock.calls[0][1]
  expect(options.method).toBe('POST')
  expect(new Headers(options.headers).get(AUTH_CONTEXT_HEADER)).toBe('context-A')
  expect(options.body).toBeUndefined()
})

test.each([[401, 'signed_out'], [403, 'stale']] as const)('logout %s enters %s without retry', async (status, expected) => {
  const mock = await authenticate()
  mock.mockResolvedValue(json({ detail: 'not exposed' }, status))
  await expect(logoutAuth()).rejects.toMatchObject({ status })
  expect(getAuthState()).toEqual({ status: expected })
  expect(mock).toHaveBeenCalledTimes(1)
})

test('logout 503 retains authenticated workspace and fixed retryable failure', async () => {
  const mock = await authenticate()
  const before = getAuthState()
  mock.mockResolvedValue(json({ detail: 'not exposed' }, 503))
  await expect(logoutAuth()).rejects.toMatchObject({ status: 503, message: 'Unable to sign out right now. Please try again.' })
  expect(getAuthState()).toEqual({ ...before, notice: AUTH_UNAVAILABLE_MESSAGE })
  expect(mock).toHaveBeenCalledTimes(1)
})

test('logout network failure retains workspace and discards arbitrary transport messages without retry', async () => {
  const mock = await authenticate()
  const before = getAuthState()
  mock.mockRejectedValue(new Error('private low-level transport content'))
  await expect(logoutAuth()).rejects.toMatchObject({ name: 'AuthBoundaryError', status: null,
    message: 'Unable to sign out right now. Please try again.' })
  expect(getAuthState()).toEqual(before)
  expect(mock).toHaveBeenCalledTimes(1)
})

test.each(['headers', 'JSON body'] as const)('bootstrap deadline rejects late %s even when fetch ignores abort', async (phase) => {
  const timeout = new AbortController()
  const timeoutSpy = vi.spyOn(AbortSignal, 'timeout').mockReturnValue(timeout.signal)
  const lateResponse = deferred<Response>()
  const lateBody = deferred<unknown>()
  const response = json(bootstrapA)
  const bodySpy = vi.spyOn(response, 'json').mockReturnValue(lateBody.promise)
  const mock = vi.fn().mockReturnValue(phase === 'headers' ? lateResponse.promise : Promise.resolve(response))
  vi.stubGlobal('fetch', mock)
  const pending = bootstrapAuth()
  if (phase === 'JSON body') await vi.waitFor(() => expect(bodySpy).toHaveBeenCalledTimes(1))
  timeout.abort(new DOMException('private timeout detail', 'TimeoutError'))
  if (phase === 'headers') lateResponse.resolve(response)
  else lateBody.resolve(bootstrapA)
  await pending
  expect(getAuthState()).toEqual({ status: 'unavailable' })
  expect(timeoutSpy).toHaveBeenCalledExactlyOnceWith(10_000)
  expect(mock).toHaveBeenCalledTimes(1)
})

test('late A response after logout cannot return data or restore the old workspace', async () => {
  const mock = await authenticate()
  const late = deferred<Response>()
  mock.mockReturnValueOnce(late.promise).mockResolvedValueOnce(new Response(null, { status: 204 }))
  const pending = protectedFetch('/api/sessions/A').catch((cause: unknown) => cause)
  const oldSignal = mock.mock.calls[0][1].signal as AbortSignal
  await logoutAuth()
  expect(oldSignal.aborted).toBe(true)
  late.resolve(json({ private_A: 'must not populate UI' }))
  expect(await pending).toBeInstanceOf(AuthBoundaryError)
  expect(getAuthState()).toEqual({ status: 'signed_out' })
  expect(mock).toHaveBeenCalledTimes(2)
})

test.each(['success', '401', '403', '503', 'transport rejection'] as const)(
  'late A %s cannot populate or invalidate authenticated B', async (outcome) => {
    const mock = await authenticate()
    const late = deferred<Response>()
    mock.mockReturnValueOnce(late.promise).mockResolvedValueOnce(json({ user_id: userB, request_context: 'context-B' }))
    const pending = protectedFetch('/api/sessions/A').catch((cause: unknown) => cause)
    await bootstrapAuth()
    const newer = getAuthState()
    if (outcome === 'transport rejection') late.reject(new Error('private network failure'))
    else late.resolve(json({ private_A: 'must not populate UI' }, outcome === 'success' ? 200 : Number(outcome)))
    expect(await pending).toBeInstanceOf(AuthBoundaryError)
    expect(getAuthState()).toEqual(newer)
    expect(newer).toMatchObject({ status: 'authenticated', userId: userB, requestContext: 'context-B' })
    expect(mock).toHaveBeenCalledTimes(2)
  },
)

test.each(['success', 'parse failure'] as const)('late A body %s cannot populate B after headers resolved', async (outcome) => {
  const mock = await authenticate()
  const body = deferred<unknown>()
  const response = json({})
  vi.spyOn(response, 'json').mockReturnValue(body.promise)
  mock.mockResolvedValueOnce(response).mockResolvedValueOnce(json({ user_id: userB, request_context: 'context-B' }))
  const owned = await protectedFetch('/api/sessions/A')
  const pending = readProtectedJson(owned).catch((cause: unknown) => cause)
  await bootstrapAuth()
  const newer = getAuthState()
  if (outcome === 'success') body.resolve({ private_A: 'must not populate UI' })
  else body.reject(new SyntaxError('sensitive parser content'))
  expect(await pending).toBeInstanceOf(AuthBoundaryError)
  expect(getAuthState()).toEqual(newer)
  expect(() => assertProtectedResponseCurrent(owned)).toThrow(AuthBoundaryError)
})

test('protected binary reading retains exact response ownership without another request', async () => {
  const mock = await authenticate()
  const response = new Response('MP3 audio', { headers: { 'Content-Type': 'audio/mpeg' } })
  mock.mockResolvedValue(response)
  const owned = await protectedFetch('/api/sessions/A/questions/0/speech', { method: 'POST' })
  const audio = await readProtectedBlob(owned)
  expect(audio.size).toBe(9)
  expect(audio.type).toBe('audio/mpeg')
  expect(mock).toHaveBeenCalledTimes(1)
  expect(mock.mock.calls[0][1]).toMatchObject({ credentials: 'same-origin', cache: 'no-store', method: 'POST' })
  expect(new Headers(mock.mock.calls[0][1].headers).get(AUTH_CONTEXT_HEADER)).toBe('context-A')
  await expect(readProtectedBlob(new Response('unowned'))).rejects.toBeInstanceOf(AuthBoundaryError)
})

test.each(['success', 'body failure'] as const)('late A binary %s cannot cross into B', async (outcome) => {
  const mock = await authenticate()
  const body = deferred<Blob>()
  const response = new Response('MP3 audio', { headers: { 'Content-Type': 'audio/mpeg' } })
  vi.spyOn(response, 'blob').mockReturnValue(body.promise)
  mock.mockResolvedValueOnce(response).mockResolvedValueOnce(json({ user_id: userB, request_context: 'context-B' }))
  const owned = await protectedFetch('/api/sessions/A/questions/0/speech', { method: 'POST' })
  const pending = readProtectedBlob(owned).catch((cause: unknown) => cause)
  await bootstrapAuth()
  const newer = getAuthState()
  if (outcome === 'success') body.resolve(new Blob(['private A audio']))
  else body.reject(new Error('private binary transport detail'))
  expect(await pending).toBeInstanceOf(AuthBoundaryError)
  expect(getAuthState()).toEqual(newer)
  expect(mock).toHaveBeenCalledTimes(2)
})

test.each(['success', 'body failure'] as const)('cancelled binary %s is discarded after headers resolve', async (outcome) => {
  const mock = await authenticate()
  const controller = new AbortController()
  const body = deferred<Blob>()
  const response = new Response('MP3 audio', { headers: { 'Content-Type': 'audio/mpeg' } })
  vi.spyOn(response, 'blob').mockReturnValue(body.promise)
  mock.mockResolvedValue(response)
  const owned = await protectedFetch('/api/sessions/A/questions/0/speech', { method: 'POST', signal: controller.signal })
  const pending = readProtectedBlob(owned).catch((cause: unknown) => cause)
  controller.abort()
  if (outcome === 'success') body.resolve(new Blob(['late audio']))
  else body.reject(new Error('private binary transport detail'))
  expect(await pending).toMatchObject({ name: 'AbortError' })
  expect(getAuthState().status).toBe('authenticated')
  expect(mock).toHaveBeenCalledTimes(1)
})

test.each(['success', 'failure'] as const)('older bootstrap %s never overwrites newer identity', async (outcome) => {
  const late = deferred<Response>()
  const mock = vi.fn().mockReturnValueOnce(late.promise).mockResolvedValueOnce(json({ user_id: userB, request_context: 'context-B' }))
  vi.stubGlobal('fetch', mock)
  const first = bootstrapAuth()
  const firstSignal = mock.mock.calls[0][1].signal as AbortSignal
  await bootstrapAuth()
  const newer = getAuthState()
  expect(firstSignal.aborted).toBe(true)
  if (outcome === 'success') late.resolve(json(bootstrapA))
  else late.reject(new Error('private low-level detail'))
  await first
  expect(getAuthState()).toEqual(newer)
})

test('caller cancellation combines with workspace signal and makes no pre-aborted request', async () => {
  const mock = await authenticate()
  const cancelled = new AbortController()
  cancelled.abort()
  await expect(protectedFetch('/api/sessions', { signal: cancelled.signal })).rejects.toMatchObject({ name: 'AbortError' })
  expect(mock).not.toHaveBeenCalled()
  const later = new AbortController()
  const pendingResponse = deferred<Response>()
  mock.mockReturnValue(pendingResponse.promise)
  const pending = protectedFetch('/api/sessions', { signal: later.signal }).catch((cause: unknown) => cause)
  const signal = mock.mock.calls[0][1].signal as AbortSignal
  later.abort()
  expect(signal.aborted).toBe(true)
  pendingResponse.resolve(json({}))
  expect(await pending).toMatchObject({ name: 'AbortError' })
  expect(getAuthState().status).toBe('authenticated')
})

test('bootstrap, protected requests and logout never persist auth or consult browser storage/query identity', async () => {
  const local = { getItem: vi.fn(), setItem: vi.fn(), removeItem: vi.fn() }
  const session = { getItem: vi.fn(), setItem: vi.fn(), removeItem: vi.fn() }
  vi.stubGlobal('localStorage', local)
  vi.stubGlobal('sessionStorage', session)
  const mock = await authenticate()
  mock.mockResolvedValueOnce(json({})).mockResolvedValueOnce(new Response(null, { status: 204 }))
  await readProtectedJson(await protectedFetch('/api/sessions'))
  await logoutAuth()
  for (const storage of [local, session]) {
    expect(storage.getItem).not.toHaveBeenCalled()
    expect(storage.setItem).not.toHaveBeenCalled()
    expect(storage.removeItem).not.toHaveBeenCalled()
  }
})
