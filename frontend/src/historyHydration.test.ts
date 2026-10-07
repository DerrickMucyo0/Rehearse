import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { getHistorySummaries, HistoryApiError } from './historyApi'
import type { HistorySummary } from './historyApi'
import { hydrateHistory, sortHistorySummaries } from './historyHydration'
import { AuthBoundaryError } from './auth'

vi.mock('./historyApi', async (original) => ({
  ...await original<typeof import('./historyApi')>(), getHistorySummaries: vi.fn(),
}))
const load = vi.mocked(getHistorySummaries)
const id = (index: number) => `00000000-0000-0000-0000-${index.toString(16).padStart(12, '0')}`
function summary(session_id: string, date = '2026-10-01T10:00:00Z'): HistorySummary {
  return { session_id, status: 'active', created_at: date, completed_at: null, current_question_number: 1,
    total_questions: 5, finalized_question_count: 0, questions_practiced_count: 0, total_attempt_count: 0,
    total_retry_count: 0, measured_final_answer_count: 0, last_submitted_at: null, last_saved_activity_at: date, finalized_points: [] }
}
beforeEach(() => { load.mockReset() })
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })

test('empty server discovery differs from idle and failures and still performs a server read', async () => {
  load.mockResolvedValue({ items: [], next_cursor: null })
  expect(await hydrateHistory()).toEqual({ status: 'complete', summaries: [], nextCursor: null, pageError: false })
  expect(load).toHaveBeenCalledOnce()
  expect(load.mock.calls[0][0]).toEqual({ signal: undefined })
})
test('initial page retains opaque continuation without interpreting identity', async () => {
  load.mockResolvedValue({ items: [summary(id(1))], next_cursor: 'opaque-continuation' })
  expect(await hydrateHistory()).toEqual({ status: 'partial', summaries: [summary(id(1))], nextCursor: 'opaque-continuation', pageError: false })
})
test('next page deduplicates sessions, updates returned facts, and sorts exact saved activity', async () => {
  load.mockResolvedValueOnce({ items: [summary(id(1))], next_cursor: 'next' })
    .mockResolvedValueOnce({ items: [summary(id(2)), summary(id(1), '2026-10-02T10:00:00Z')], next_cursor: null })
  const previous = await hydrateHistory()
  const snapshot = JSON.stringify(previous)
  const result = await hydrateHistory({ previous })
  expect(load.mock.calls[1][0]).toEqual({ signal: undefined, cursor: 'next' })
  expect(result.summaries.map((item) => item.session_id)).toEqual([id(1), id(2)])
  expect(result.status).toBe('complete')
  expect(JSON.stringify(previous)).toBe(snapshot)
})
test('global ordering preserves server microseconds independently of timezone spelling', () => {
  const values = [summary(id(1), '2026-10-01T10:00:00.000001Z'), summary(id(2), '2026-10-01T10:00:00.000002Z'),
    summary(id(3), '2026-10-01T06:00:00.000002-04:00'), summary(id(4), '2026-10-01T10:00:00Z')]
  expect(sortHistorySummaries(values).map((item) => item.session_id)).toEqual([id(2), id(3), id(1), id(4)])
  expect(values.map((item) => item.session_id)).toEqual([id(1), id(2), id(3), id(4)])
})
test('failed next page retains successful results and retries the same continuation only on explicit demand', async () => {
  load.mockResolvedValueOnce({ items: [summary(id(1))], next_cursor: 'next' })
    .mockRejectedValueOnce(new HistoryApiError('Unable to load history.'))
    .mockResolvedValueOnce({ items: [summary(id(2))], next_cursor: null })
  const previous = await hydrateHistory()
  const failed = await hydrateHistory({ previous })
  expect(failed).toEqual({ ...previous, pageError: true })
  expect(load).toHaveBeenCalledTimes(2)
  expect(await hydrateHistory({ previous: failed })).toMatchObject({ status: 'complete', pageError: false, nextCursor: null })
  expect(load.mock.calls[2][0]?.cursor).toBe('next')
})
test('initial failure is not a successful empty history', async () => {
  load.mockRejectedValue(new HistoryApiError('Unable to load history.'))
  expect(await hydrateHistory()).toEqual({ status: 'error', summaries: [], nextCursor: null, pageError: true })
  expect(load).toHaveBeenCalledOnce()
})
test('a repeated cursor cannot trigger automatic endless discovery', async () => {
  load.mockResolvedValue({ items: [], next_cursor: 'same' })
  const previous = await hydrateHistory()
  expect(await hydrateHistory({ previous })).toMatchObject({ pageError: true, nextCursor: 'same' })
  expect(load).toHaveBeenCalledTimes(2)
})
test('completed discovery does not request another page', async () => {
  load.mockResolvedValue({ items: [summary(id(1))], next_cursor: null })
  const previous = await hydrateHistory()
  expect(await hydrateHistory({ previous })).toBe(previous)
  expect(load).toHaveBeenCalledOnce()
})
test('discovery neither reads nor writes browser membership or private data', async () => {
  const access = vi.fn(() => { throw new Error('Browser storage is forbidden') })
  vi.stubGlobal('localStorage', { getItem: access, setItem: access, removeItem: access })
  vi.stubGlobal('sessionStorage', { getItem: access, setItem: access, removeItem: access })
  load.mockResolvedValue({ items: [summary(id(9))], next_cursor: null })
  expect((await hydrateHistory()).summaries[0].session_id).toBe(id(9))
  expect(access).not.toHaveBeenCalled()
})
test('caller cancellation rejects late pages and pre-aborted reads never start', async () => {
  const controller = new AbortController()
  let resolve!: (value: { items: HistorySummary[]; next_cursor: null }) => void
  load.mockImplementationOnce(() => new Promise((done) => { resolve = done }))
  const pending = hydrateHistory({ signal: controller.signal })
  controller.abort()
  resolve({ items: [summary(id(1))], next_cursor: null })
  await expect(pending).rejects.toMatchObject({ cancelled: true })
  await expect(hydrateHistory({ signal: controller.signal })).rejects.toMatchObject({ cancelled: true })
  expect(load).toHaveBeenCalledOnce()
})
test('authentication boundary failures propagate without being recast as partial History', async () => {
  load.mockRejectedValue(new AuthBoundaryError('stale'))
  await expect(hydrateHistory()).rejects.toBeInstanceOf(AuthBoundaryError)
  expect(load).toHaveBeenCalledOnce()
})
