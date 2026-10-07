export type AuthState =
  | { status: 'loading' }
  | { status: 'signed_out' }
  | { status: 'stale' }
  | { status: 'unavailable' }
  | { status: 'authenticated'; userId: string; requestContext: string; generation: number; notice: string | null }

export const AUTH_UNAVAILABLE_MESSAGE = 'Authentication is temporarily unavailable. Please try again.'
export const AUTH_CONTEXT_HEADER = 'X-Rehearse-Auth-Context'

export class AuthBoundaryError extends Error {
  readonly status: number | null

  constructor(message: string, status: number | null = null) {
    super(message)
    this.name = 'AuthBoundaryError'
    this.status = status
  }
}

export function isAuthBoundaryError(error: unknown): error is AuthBoundaryError {
  return error instanceof AuthBoundaryError
}

interface Workspace {
  generation: number
  requestContext: string
  controller: AbortController
}

let state: AuthState = Object.freeze({ status: 'loading' })
let generation = 0
let workspace: Workspace | null = null
let bootstrap: { generation: number; controller: AbortController } | null = null
const listeners = new Set<() => void>()
const responseWorkspaces = new WeakMap<Response, { workspace: Workspace; signal: AbortSignal }>()

export function getAuthState(): AuthState { return state }
export function isAuthWorkspaceCurrent(currentGeneration: number): boolean {
  return state.status === 'authenticated' && state.generation === currentGeneration &&
    workspace?.generation === currentGeneration && !workspace.controller.signal.aborted
}
export function subscribeAuth(listener: () => void): () => void {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

function publish(next: AuthState): void {
  state = Object.freeze(next)
  for (const listener of listeners) listener()
}

function replaceWorkspace(next: Exclude<AuthState, { status: 'authenticated' }>): void {
  workspace?.controller.abort()
  workspace = null
  bootstrap?.controller.abort()
  bootstrap = null
  generation += 1
  publish(next)
}

function assertWorkspace(current: Workspace): void {
  if (workspace !== current || current.controller.signal.aborted || state.status !== 'authenticated') {
    throw new AuthBoundaryError('This sign-in workspace is no longer current.')
  }
}

function validBootstrap(value: unknown): value is { user_id: string; request_context: string } {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  const fields = value as Record<string, unknown>
  return Object.keys(fields).length === 2 && Object.hasOwn(fields, 'user_id') && Object.hasOwn(fields, 'request_context') &&
    typeof fields.user_id === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(fields.user_id) &&
    typeof fields.request_context === 'string' && !!fields.request_context.trim() &&
    fields.request_context === fields.request_context.trim() && /^[\x20-\x7e]+$/.test(fields.request_context)
}

// Identity and request context exist only in this tab's memory. Reloading always
// bootstraps from the backend cookie; no cookie or provider token is read here.
export async function bootstrapAuth(): Promise<AuthState> {
  replaceWorkspace({ status: 'loading' })
  const current = { generation, controller: new AbortController() }
  bootstrap = current
  const isCurrent = () => bootstrap === current && generation === current.generation
  const signal = AbortSignal.any([current.controller.signal, AbortSignal.timeout(10_000)])
  try {
    const response = await fetch('/api/auth/me', {
      method: 'GET', credentials: 'same-origin', cache: 'no-store',
      signal,
    })
    if (!isCurrent()) return state
    if (signal.aborted) {
      replaceWorkspace({ status: 'unavailable' })
      return state
    }
    if (response.status === 401) {
      replaceWorkspace({ status: 'signed_out' })
      return state
    }
    if (!response.ok) {
      replaceWorkspace({ status: 'unavailable' })
      return state
    }
    const value: unknown = await response.json()
    if (!isCurrent()) return state
    if (signal.aborted) {
      replaceWorkspace({ status: 'unavailable' })
      return state
    }
    if (!validBootstrap(value)) {
      replaceWorkspace({ status: 'unavailable' })
      return state
    }
    bootstrap = null
    workspace = { generation: current.generation, requestContext: value.request_context, controller: new AbortController() }
    publish({ status: 'authenticated', userId: value.user_id, requestContext: value.request_context,
      generation: current.generation, notice: null })
  } catch {
    if (isCurrent()) replaceWorkspace({ status: 'unavailable' })
  }
  return state
}

export function signIn(): void { window.location.assign('/api/auth/login') }
export function reloadSignIn(): void { window.location.reload() }

function protectedApiPath(path: string): boolean {
  if (!path.startsWith('/api/') || path.includes('\\') || [...path].some((character) => character.charCodeAt(0) <= 0x20)) return false
  const url = new URL(path, 'https://rehearse.invalid')
  return url.origin === 'https://rehearse.invalid' && url.pathname.startsWith('/api/') && !url.hash &&
    !['/api/auth/me', '/api/auth/login', '/api/auth/callback'].includes(url.pathname)
}

export async function protectedFetch(path: string, options: RequestInit = {}): Promise<Response> {
  const current = workspace
  if (current === null || state.status !== 'authenticated') throw new AuthBoundaryError('Sign in to continue.')
  if (!protectedApiPath(path)) throw new AuthBoundaryError('Invalid application request.')
  const headers = new Headers(options.headers)
  if (['Authorization', 'Cookie', 'X-User-Id'].some((header) => headers.has(header))) {
    throw new AuthBoundaryError('Invalid application request.')
  }
  headers.set(AUTH_CONTEXT_HEADER, current.requestContext)
  const signal = options.signal
    ? AbortSignal.any([options.signal, current.controller.signal])
    : current.controller.signal
  if (signal.aborted) throw new DOMException('Request cancelled.', 'AbortError')
  let response: Response
  try {
    response = await fetch(path, { ...options, headers, credentials: 'same-origin', cache: 'no-store', signal })
  } catch (error) {
    assertWorkspace(current)
    throw error
  }
  assertWorkspace(current)
  if (signal.aborted) throw new DOMException('Request cancelled.', 'AbortError')
  if (response.status === 401 || response.status === 403) {
    const status = response.status
    replaceWorkspace({ status: status === 401 ? 'signed_out' : 'stale' })
    throw new AuthBoundaryError(status === 401 ? 'Sign in to continue.' : 'Your sign-in changed. Reload current sign-in.', status)
  }
  if (response.status === 503 && state.status === 'authenticated') {
    publish({ ...state, notice: AUTH_UNAVAILABLE_MESSAGE })
  }
  responseWorkspaces.set(response, { workspace: current, signal })
  return response
}

export function assertProtectedResponseCurrent(response: Response): void {
  const ownership = responseWorkspaces.get(response)
  if (!ownership) throw new AuthBoundaryError('Invalid application response.')
  assertWorkspace(ownership.workspace)
  if (ownership.signal.aborted) throw new DOMException('Request cancelled.', 'AbortError')
}

export async function readProtectedJson(response: Response): Promise<unknown> {
  assertProtectedResponseCurrent(response)
  try {
    const value: unknown = await response.json()
    assertProtectedResponseCurrent(response)
    return value
  } catch (error) {
    assertProtectedResponseCurrent(response)
    throw error
  }
}

export async function logoutAuth(): Promise<void> {
  try {
    const response = await protectedFetch('/api/auth/logout', { method: 'POST', signal: AbortSignal.timeout(10_000) })
    assertProtectedResponseCurrent(response)
    if (response.status !== 204) throw new AuthBoundaryError('Unable to sign out right now. Please try again.', response.status)
    replaceWorkspace({ status: 'signed_out' })
  } catch (error) {
    if (isAuthBoundaryError(error)) throw error
    throw new AuthBoundaryError('Unable to sign out right now. Please try again.')
  }
}
