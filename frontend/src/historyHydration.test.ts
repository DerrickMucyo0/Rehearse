import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { getHistorySummaries, HistoryApiError } from './historyApi'
import type { HistorySummary } from './historyApi'
import { hydrateHistory, sortHistorySummaries } from './historyHydration'

vi.mock('./historyApi', async (original) => ({
  ...await original<typeof import('./historyApi')>(), getHistorySummaries: vi.fn(),
}))
const load = vi.mocked(getHistorySummaries)
const id = (index: number) => `00000000-0000-0000-0000-${index.toString(16).padStart(12, '0')}`
const ids = (count: number) => Array.from({ length: count }, (_, index) => id(index + 1))
function summary(session_id: string, date = '2026-10-01T10:00:00Z'): HistorySummary {
  return { session_id, status: 'active', created_at: date, completed_at: null, current_question_number: 1,
    total_questions: 5, finalized_question_count: 0, questions_practiced_count: 0, total_attempt_count: 0,
    total_retry_count: 0, measured_final_answer_count: 0, last_submitted_at: null, last_saved_activity_at: date, finalized_points: [] }
}
beforeEach(() => { load.mockReset() })
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })

test('empty valid registry is complete and distinct from loading or all-failed history', async () => {
  expect(await hydrateHistory([])).toEqual({ status: 'complete', summaries: [], missingIds: [], failedChunks: [], rememberedCount: 0 })
  expect(load).not.toHaveBeenCalled()
})
test.each([{ count: 1, sizes: [1] }, { count: 50, sizes: [50] }, { count: 51, sizes: [50, 1] },
  { count: 100, sizes: [50, 50] }, { count: 500, sizes: Array.from({ length: 10 }, () => 50) }])(
  'hydrates $count remembered IDs in bounded batches', async ({ count, sizes }) => {
    load.mockImplementation(async (requested) => ({ summaries: [], missing_session_ids: [...requested] }))
    const state = await hydrateHistory(ids(count))
    expect(load.mock.calls.map(([requested]) => requested.length)).toEqual(sizes)
    expect(state.status).toBe('complete')
    expect(state.rememberedCount).toBe(count)
    expect(state.missingIds).toEqual(ids(count))
    expect(state.failedChunks).toEqual([])
  },
)
test('canonicalizes duplicates and never requests malformed IDs', async () => {
  load.mockResolvedValue({ summaries: [], missing_session_ids: [id(1)] })
  const state = await hydrateHistory([id(1).toUpperCase(), 'invalid', null, {}, id(1)])
  expect(load.mock.calls[0][0]).toEqual([id(1)])
  expect(state.rememberedCount).toBe(1)
})
test('combines independently ordered batches and globally sorts activity then UUID', async () => {
  const known = ids(51)
  load.mockImplementation(async (requested) => ({ summaries: requested.map((item) => summary(item,
    item === id(51) ? '2026-10-03T10:00:00Z' : '2026-10-01T10:00:00Z')).reverse(), missing_session_ids: [] }))
  const state = await hydrateHistory(known)
  expect(state.summaries.map((item) => item.session_id)).toEqual([id(51), ...known.slice(0, 50)])
  expect(state.status).toBe('complete')
})
test('global ordering preserves server microseconds and is independent of timezone spelling', () => {
  const values = [summary(id(1), '2026-10-01T10:00:00.000001Z'), summary(id(2), '2026-10-01T10:00:00.000002Z'),
    summary(id(3), '2026-10-01T06:00:00.000002-04:00'), summary(id(4), '2026-10-01T10:00:00Z')]
  expect(sortHistorySummaries(values).map((item) => item.session_id)).toEqual([id(2), id(3), id(1), id(4)])
  expect(values.map((item) => item.session_id)).toEqual(ids(4))
})
test('retains missing IDs and failed chunks without deleting remembered capabilities', async () => {
  const known = ids(51)
  const removeItem = vi.fn()
  vi.stubGlobal('localStorage', { removeItem })
  load.mockImplementation(async (requested) => {
    if (requested.length === 1) throw new HistoryApiError('Unable to load history.')
    return { summaries: requested.slice(1).map((item) => summary(item)), missing_session_ids: [requested[0]] }
  })
  const state = await hydrateHistory(known)
  expect(state.status).toBe('partial')
  expect(state.summaries).toHaveLength(49)
  expect(state.missingIds).toEqual([id(1)])
  expect(state.failedChunks).toEqual([[id(51)]])
  expect(state.rememberedCount).toBe(51)
  expect(known).toEqual(ids(51))
  expect(removeItem).not.toHaveBeenCalled()
})
test('all chunks failed is error, while all missing is complete with remembered entries', async () => {
  load.mockRejectedValue(new HistoryApiError('Unable to load history.'))
  const failed = await hydrateHistory(ids(51))
  expect(failed).toEqual({ status: 'error', summaries: [], missingIds: [], failedChunks: [ids(50), [id(51)]], rememberedCount: 51 })
  load.mockImplementation(async (requested) => ({ summaries: [], missing_session_ids: [...requested] }))
  const missing = await hydrateHistory(ids(51))
  expect(missing.status).toBe('complete')
  expect(missing.missingIds).toHaveLength(51)
})
test('retries only failed chunks and globally merges retained successes and missing entries', async () => {
  const known = ids(51)
  load.mockImplementation(async (requested) => {
    if (requested.length === 1) throw new HistoryApiError('Unable to load history.')
    return { summaries: requested.slice(1).map((item) => summary(item)), missing_session_ids: [requested[0]] }
  })
  const previous = await hydrateHistory(known)
  const snapshot = JSON.stringify(previous)
  load.mockReset().mockResolvedValue({ summaries: [summary(id(51), '2026-10-03T10:00:00Z')], missing_session_ids: [] })
  const retried = await hydrateHistory(known, { previous, retryChunks: previous.failedChunks })
  expect(load).toHaveBeenCalledTimes(1)
  expect(load.mock.calls[0][0]).toEqual([id(51)])
  expect(retried.status).toBe('complete')
  expect(retried.summaries).toHaveLength(50)
  expect(retried.summaries[0].session_id).toBe(id(51))
  expect(retried.missingIds).toEqual([id(1)])
  expect(retried.failedChunks).toEqual([])
  expect(JSON.stringify(previous)).toBe(snapshot)
})
test('a repeated failed retry retains previous successes and the failure context', async () => {
  load.mockImplementation(async (requested) => {
    if (requested.length === 1) throw new HistoryApiError('Unable to load history.')
    return { summaries: requested.map((item) => summary(item)), missing_session_ids: [] }
  })
  const previous = await hydrateHistory(ids(51))
  const retried = await hydrateHistory(ids(51), { previous, retryChunks: previous.failedChunks })
  expect(retried.status).toBe('partial')
  expect(retried.summaries).toEqual(previous.summaries)
  expect(retried.failedChunks).toEqual(previous.failedChunks)
})
test('retry does not reintroduce removed capabilities or request an unknown supplied chunk', async () => {
  load.mockRejectedValue(new HistoryApiError('Unable to load history.'))
  const previous = await hydrateHistory(ids(51))
  load.mockReset().mockResolvedValue({ summaries: [summary(id(51))], missing_session_ids: [] })
  const result = await hydrateHistory([id(51)], { previous, retryChunks: [[id(51), id(99), 'invalid']] })
  expect(load.mock.calls[0][0]).toEqual([id(51)])
  expect(result.status).toBe('complete')
  expect(result.rememberedCount).toBe(1)
  expect(result.failedChunks).toEqual([])
})
test('bounds concurrently active batch reads to three', async () => {
  let active = 0
  let peak = 0
  load.mockImplementation(async (requested) => {
    active += 1
    peak = Math.max(peak, active)
    await Promise.resolve()
    active -= 1
    return { summaries: [], missing_session_ids: [...requested] }
  })
  await hydrateHistory(ids(500))
  expect(peak).toBe(3)
  expect(active).toBe(0)
})
test('an aborted older hydration cannot produce a late state after a newer hydration', async () => {
  const controller = new AbortController()
  let resolve: (value: { summaries: HistorySummary[]; missing_session_ids: string[] }) => void = () => { throw new Error('not started') }
  load.mockImplementationOnce(() => new Promise((done) => { resolve = done }))
    .mockResolvedValueOnce({ summaries: [summary(id(2))], missing_session_ids: [] })
  const old = hydrateHistory([id(1)], { signal: controller.signal })
  controller.abort()
  const latest = await hydrateHistory([id(2)])
  resolve({ summaries: [summary(id(1))], missing_session_ids: [] })
  await expect(old).rejects.toMatchObject({ cancelled: true })
  expect(latest.summaries.map((item) => item.session_id)).toEqual([id(2)])
})
test('pre-aborted hydration does no reads', async () => {
  const controller = new AbortController()
  controller.abort()
  await expect(hydrateHistory(ids(500), { signal: controller.signal })).rejects.toMatchObject({ cancelled: true })
  expect(load).not.toHaveBeenCalled()
})
