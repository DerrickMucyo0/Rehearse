// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { hydrateHistory } from './historyHydration'
import type { HistoryHydrationState } from './historyHydration'
import { useHistoryHydration } from './useHistoryHydration'

vi.mock('./historyHydration', () => ({ hydrateHistory: vi.fn() }))
const hydrate = vi.mocked(hydrateHistory)
const FIRST = '00000000-0000-4000-8000-000000000001'
const SECOND = '00000000-0000-4000-8000-000000000002'
function state(changes: Partial<HistoryHydrationState> = {}): HistoryHydrationState {
  return { status: 'complete', summaries: [], missingIds: [FIRST], failedChunks: [], rememberedCount: 1, ...changes }
}
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}
beforeEach(() => { hydrate.mockReset().mockResolvedValue(state()) })
afterEach(() => { cleanup(); vi.restoreAllMocks() })

test('a valid memory cache survives disabled Practice and returns without another request', async () => {
  const view = renderHook(({ enabled }) => useHistoryHydration([FIRST], 0, enabled), { initialProps: { enabled: true } })
  await waitFor(() => expect(view.result.current.history.status).toBe('complete'))
  view.rerender({ enabled: false })
  view.rerender({ enabled: true })
  expect(hydrate).toHaveBeenCalledOnce()
})
test('an invalidation while Practice is busy waits for read navigation', async () => {
  const view = renderHook(({ version, enabled }) => useHistoryHydration([FIRST], version, enabled),
    { initialProps: { version: 0, enabled: false } })
  expect(hydrate).not.toHaveBeenCalled()
  view.rerender({ version: 1, enabled: false })
  expect(hydrate).not.toHaveBeenCalled()
  view.rerender({ version: 1, enabled: true })
  await waitFor(() => expect(view.result.current.history.status).toBe('complete'))
  expect(hydrate).toHaveBeenCalledOnce()
})
test('membership invalidation hides stale data and rejects the older response', async () => {
  const old = deferred<HistoryHydrationState>()
  hydrate.mockReturnValueOnce(old.promise).mockResolvedValueOnce(state({ missingIds: [SECOND] }))
  const view = renderHook(({ ids }) => useHistoryHydration(ids, 0, true), { initialProps: { ids: [FIRST] } })
  const oldSignal = hydrate.mock.calls[0][1]?.signal
  view.rerender({ ids: [SECOND] })
  expect(view.result.current.history.missingIds).toEqual([])
  await waitFor(() => expect(view.result.current.history.missingIds).toEqual([SECOND]))
  expect(oldSignal?.aborted).toBe(true)
  await act(async () => old.resolve(state()))
  expect(view.result.current.history.missingIds).toEqual([SECOND])
})
test('manual retry retains successful chunks and supplies only failed chunks', async () => {
  const previous = state({ status: 'partial', failedChunks: [[SECOND]], rememberedCount: 2 })
  hydrate.mockResolvedValueOnce(previous).mockResolvedValueOnce(state({ missingIds: [FIRST, SECOND], rememberedCount: 2 }))
  const view = renderHook(() => useHistoryHydration([FIRST, SECOND], 0, true))
  await waitFor(() => expect(view.result.current.history.status).toBe('partial'))
  act(() => view.result.current.retry())
  await waitFor(() => expect(view.result.current.history.status).toBe('complete'))
  expect(hydrate.mock.calls[1][1]?.previous).toEqual(previous)
  expect(hydrate.mock.calls[1][1]?.retryChunks).toEqual([[SECOND]])
})
test('explicit reload reads every remembered ID even after a partial result', async () => {
  hydrate.mockResolvedValueOnce(state({ status: 'partial', failedChunks: [[SECOND]], rememberedCount: 2 }))
  const view = renderHook(() => useHistoryHydration([FIRST, SECOND], 0, true))
  await waitFor(() => expect(view.result.current.history.status).toBe('partial'))
  act(() => view.result.current.reload())
  await waitFor(() => expect(hydrate).toHaveBeenCalledTimes(2))
  expect(hydrate.mock.calls[1][0]).toEqual([FIRST, SECOND])
  expect(hydrate.mock.calls[1][1]?.retryChunks).toBeUndefined()
})
test('leaving pending reads cancels them and re-entering makes a fresh safe request', async () => {
  const old = deferred<HistoryHydrationState>()
  hydrate.mockReturnValueOnce(old.promise)
  const view = renderHook(({ enabled }) => useHistoryHydration([FIRST], 0, enabled), { initialProps: { enabled: true } })
  const oldSignal = hydrate.mock.calls[0][1]?.signal
  view.rerender({ enabled: false })
  expect(oldSignal?.aborted).toBe(true)
  view.rerender({ enabled: true })
  await waitFor(() => expect(view.result.current.history.status).toBe('complete'))
  await act(async () => old.resolve(state({ status: 'error' })))
  expect(view.result.current.history.status).toBe('complete')
  expect(hydrate).toHaveBeenCalledTimes(2)
})
test('clear immediately drops the cache and suppresses pending results', async () => {
  const old = deferred<HistoryHydrationState>()
  hydrate.mockReturnValueOnce(old.promise)
  const view = renderHook(() => useHistoryHydration([FIRST], 0, true))
  act(() => view.result.current.discard())
  await act(async () => old.resolve(state()))
  expect(view.result.current.history).toEqual(state({ rememberedCount: 0, missingIds: [] }))
  expect(hydrate.mock.calls[0][1]?.signal?.aborted).toBe(true)
})
test('unmount rejects late responses without starting additional requests', async () => {
  const old = deferred<HistoryHydrationState>()
  hydrate.mockReturnValueOnce(old.promise)
  const view = renderHook(() => useHistoryHydration([FIRST], 0, true))
  const signal = hydrate.mock.calls[0][1]?.signal
  view.unmount()
  await act(async () => old.resolve(state()))
  expect(signal?.aborted).toBe(true)
  expect(hydrate).toHaveBeenCalledOnce()
})
