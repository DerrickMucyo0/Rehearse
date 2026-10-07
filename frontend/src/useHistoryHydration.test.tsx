import { authenticateTestWorkspace } from './authTestUtils'
// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { hydrateHistory } from './historyHydration'
import type { HistoryHydrationState } from './historyHydration'
import { useHistoryHydration } from './useHistoryHydration'

vi.mock('./historyHydration', () => ({ hydrateHistory: vi.fn() }))
const hydrate = vi.mocked(hydrateHistory)
function state(changes: Partial<HistoryHydrationState> = {}): HistoryHydrationState {
  return { status: 'complete', summaries: [], nextCursor: null, pageError: false, ...changes }
}
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}
beforeEach(async () => {
  await authenticateTestWorkspace(); hydrate.mockReset().mockResolvedValue(state()) })
afterEach(() => { cleanup(); vi.restoreAllMocks() })

test('a memory cache survives Practice and returns without another discovery request', async () => {
  const view = renderHook(({ enabled }) => useHistoryHydration(0, enabled), { initialProps: { enabled: true } })
  await waitFor(() => expect(view.result.current.history.status).toBe('complete'))
  view.rerender({ enabled: false })
  view.rerender({ enabled: true })
  expect(hydrate).toHaveBeenCalledOnce()
})
test('persisted-write invalidation waits for safe read navigation', async () => {
  const view = renderHook(({ version, enabled }) => useHistoryHydration(version, enabled), { initialProps: { version: 0, enabled: false } })
  view.rerender({ version: 1, enabled: false })
  expect(hydrate).not.toHaveBeenCalled()
  view.rerender({ version: 1, enabled: true })
  await waitFor(() => expect(view.result.current.history.status).toBe('complete'))
  expect(hydrate).toHaveBeenCalledOnce()
})
test('generation invalidation resets cursor immediately and ignores earlier response', async () => {
  const old = deferred<HistoryHydrationState>()
  hydrate.mockReturnValueOnce(old.promise).mockResolvedValueOnce(state({ nextCursor: 'new', status: 'partial' }))
  const view = renderHook(({ version }) => useHistoryHydration(version, true), { initialProps: { version: 0 } })
  const oldSignal = hydrate.mock.calls[0][0]?.signal
  view.rerender({ version: 1 })
  expect(view.result.current.history.nextCursor).toBeNull()
  await waitFor(() => expect(view.result.current.history.nextCursor).toBe('new'))
  expect(oldSignal?.aborted).toBe(true)
  await act(async () => old.resolve(state({ nextCursor: 'old', status: 'partial' })))
  expect(view.result.current.history.nextCursor).toBe('new')
  expect(hydrate.mock.calls[1][0]?.previous).toBeUndefined()
})
test('load more passes only current server continuation and double-click does not duplicate reads', async () => {
  const previous = state({ status: 'partial', nextCursor: 'next' })
  const next = deferred<HistoryHydrationState>()
  hydrate.mockResolvedValueOnce(previous).mockReturnValueOnce(next.promise)
  const view = renderHook(() => useHistoryHydration(0, true))
  await waitFor(() => expect(view.result.current.canLoadMore).toBe(true))
  act(() => { view.result.current.loadMore(); view.result.current.loadMore() })
  await waitFor(() => expect(hydrate).toHaveBeenCalledTimes(2))
  expect(view.result.current.canLoadMore).toBe(false)
  expect(hydrate.mock.calls[1][0]?.previous).toEqual(previous)
  await act(async () => next.resolve(state()))
  expect(view.result.current.history.status).toBe('complete')
})
test('manual retry retains page continuation; reload resets all pages instead', async () => {
  const previous = state({ status: 'partial', nextCursor: 'failed-page', pageError: true })
  hydrate.mockResolvedValueOnce(previous).mockResolvedValueOnce(previous).mockResolvedValueOnce(state())
  const view = renderHook(() => useHistoryHydration(0, true))
  await waitFor(() => expect(view.result.current.history.pageError).toBe(true))
  act(() => view.result.current.retry())
  await waitFor(() => expect(hydrate).toHaveBeenCalledTimes(2))
  expect(hydrate.mock.calls[1][0]?.previous).toEqual(previous)
  act(() => view.result.current.reload())
  await waitFor(() => expect(hydrate).toHaveBeenCalledTimes(3))
  expect(hydrate.mock.calls[2][0]?.previous).toBeUndefined()
})
test('disabled pending reads cancel and cannot restore old rows on re-entry', async () => {
  const old = deferred<HistoryHydrationState>()
  hydrate.mockReturnValueOnce(old.promise)
  const view = renderHook(({ enabled }) => useHistoryHydration(0, enabled), { initialProps: { enabled: true } })
  const oldSignal = hydrate.mock.calls[0][0]?.signal
  view.rerender({ enabled: false })
  expect(oldSignal?.aborted).toBe(true)
  view.rerender({ enabled: true })
  await waitFor(() => expect(view.result.current.history.status).toBe('complete'))
  await act(async () => old.resolve(state({ status: 'error', pageError: true })))
  expect(view.result.current.history.status).toBe('complete')
  expect(hydrate).toHaveBeenCalledTimes(2)
})
test('unmount cancels pending pages without issuing more requests', async () => {
  const old = deferred<HistoryHydrationState>()
  hydrate.mockReturnValueOnce(old.promise)
  const view = renderHook(() => useHistoryHydration(0, true))
  const signal = hydrate.mock.calls[0][0]?.signal
  view.unmount()
  await act(async () => old.resolve(state()))
  expect(signal?.aborted).toBe(true)
  expect(hydrate).toHaveBeenCalledOnce()
})

test('account workspace replacement resets memory and cursors even if the hook stays mounted', async () => {
  hydrate.mockResolvedValueOnce(state({ status: 'partial', nextCursor: 'A-cursor' })).mockResolvedValueOnce(state())
  const view = renderHook(({ enabled }) => useHistoryHydration(0, enabled), { initialProps: { enabled: true } })
  await waitFor(() => expect(view.result.current.history.nextCursor).toBe('A-cursor'))
  await act(async () => { await authenticateTestWorkspace('context-B', 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb') })
  view.rerender({ enabled: true })
  expect(view.result.current.history.nextCursor).toBeNull()
  await waitFor(() => expect(view.result.current.history.status).toBe('complete'))
  expect(hydrate).toHaveBeenCalledTimes(2)
  expect(hydrate.mock.calls[1][0]?.previous).toBeUndefined()
})
test('an old workspace promise cannot publish even before its component rerenders or unmounts', async () => {
  const old = deferred<HistoryHydrationState>()
  hydrate.mockReturnValueOnce(old.promise)
  const view = renderHook(() => useHistoryHydration(0, true))
  await act(async () => { await authenticateTestWorkspace('context-B', 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb') })
  await act(async () => old.resolve(state({ status: 'partial', nextCursor: 'A-cursor' })))
  expect(view.result.current.history.nextCursor).toBeNull()
  expect(view.result.current.history.status).toBe('loading')
})
