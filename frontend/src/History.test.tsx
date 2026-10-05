// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import History from './History'
import { hydrateHistory } from './historyHydration'
import { getHistoryDetail } from './historyApi'
import type { HistorySummary } from './historyApi'
import type { HistoryHydrationState } from './historyHydration'

vi.mock('./historyHydration', () => ({ hydrateHistory: vi.fn() }))
vi.mock('./historyApi', async (importOriginal) => {
  const original = await importOriginal<typeof import('./historyApi')>()
  return { ...original, getHistoryDetail: vi.fn() }
})

const FIRST = '00000000-0000-4000-8000-000000000001'
const SECOND = '00000000-0000-4000-8000-000000000002'
const THIRD = '00000000-0000-4000-8000-000000000003'
const hydrate = vi.mocked(hydrateHistory)
const readDetail = vi.mocked(getHistoryDetail)
const measurement = {
  measurement_version: 'speaking-metrics-v1', measurement_source: 'original_transcription' as const,
  recognized_word_count: 10, um_count: 0, uh_count: 1, filler_unavailable_reason: null,
  timed_utterance_span_seconds: 12.123456789, estimated_words_per_minute: 49.491231198,
  timing_unavailable_reason: null,
}

function summary(id = FIRST, status: 'active' | 'completed' = 'active'): HistorySummary {
  const finalized = status === 'active' ? 2 : 5
  return {
    session_id: id, status, created_at: '2026-10-05T10:00:00Z',
    completed_at: status === 'completed' ? '2026-10-05T13:00:00Z' : null,
    current_question_number: status === 'active' ? 3 : null, total_questions: 5,
    finalized_question_count: finalized, questions_practiced_count: status === 'active' ? 3 : 5,
    total_attempt_count: status === 'active' ? 5 : 7, total_retry_count: 2,
    measured_final_answer_count: 2, last_submitted_at: '2026-10-05T12:00:00Z',
    last_saved_activity_at: status === 'completed' ? '2026-10-05T13:00:00Z' : '2026-10-05T12:00:00Z',
    finalized_points: Array.from({ length: finalized }, (_, question_index) => ({
      question_index, attempt_id: `00000000-0000-4000-8000-${String(question_index + 100).padStart(12, '0')}`,
      attempt_number: 1, submitted_at: '2026-10-05T12:00:00Z', measurement: question_index < 2 ? measurement : null,
    })),
  }
}

function state(summaries: HistorySummary[] = [], changes: Partial<HistoryHydrationState> = {}): HistoryHydrationState {
  return { status: 'complete', summaries, missingIds: [], failedChunks: [], rememberedCount: summaries.length, ...changes }
}

function props(ids = [FIRST]) {
  return { sessionIds: ids, storageError: null, onRemove: vi.fn(), onClear: vi.fn(), onPractice: vi.fn() }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}

beforeEach(() => {
  hydrate.mockReset()
  readDetail.mockReset()
  hydrate.mockResolvedValue(state())
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Unmocked network is forbidden')))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks() })

test('empty history describes this browser and offers Practice without a backend request', async () => {
  const options = props([])
  render(<History {...options} />)
  expect(screen.getByText('No sessions are remembered on this browser yet.')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Practice' }))
  expect(options.onPractice).toHaveBeenCalledOnce()
  expect(screen.queryByRole('button', { name: 'Clear remembered history' })).toBeNull()
  await waitFor(() => expect(hydrate).toHaveBeenCalledOnce())
  expect(readDetail).not.toHaveBeenCalled()
  expect(fetch).not.toHaveBeenCalled()
})

test('active and completed rows display objective counts and no raw UUIDs or quality judgments', async () => {
  hydrate.mockResolvedValue(state([summary(SECOND, 'completed'), summary(FIRST)]))
  const view = render(<History {...props([FIRST, SECOND])} />)
  await screen.findByRole('heading', { name: 'Active session' })
  const rows = view.container.querySelectorAll('.history-summary')
  expect(rows).toHaveLength(2)
  expect(within(rows[0] as HTMLElement).getByRole('heading').textContent).toBe('Completed session')
  expect(within(rows[1] as HTMLElement).getByRole('heading').textContent).toBe('Active session')
  const active = within(rows[1] as HTMLElement)
  expect(active.getByText('2 / 5')).toBeTruthy()
  expect(active.getByText('Attempts').nextElementSibling?.textContent).toBe('5')
  expect(active.getByText('Retries').nextElementSibling?.textContent).toBe('2')
  expect(active.getByText('Measured final answers').nextElementSibling?.textContent).toBe('2')
  expect(active.getByText('Current question').nextElementSibling?.textContent).toBe('3 of 5')
  expect(within(rows[0] as HTMLElement).queryByText('Current question')).toBeNull()
  expect(view.container.textContent).not.toContain(FIRST)
  expect(view.container.textContent).not.toContain(SECOND)
  expect(view.container.textContent).not.toMatch(/\b(improved|better|worse|good|bad|score|performance|readiness|confidence)\b/i)
})

test('known and missing entries offer local removal without silently removing missing IDs', async () => {
  const options = props([FIRST, SECOND])
  hydrate.mockResolvedValue(state([summary()], { missingIds: [SECOND], rememberedCount: 2 }))
  render(<History {...options} />)
  await screen.findByRole('heading', { name: 'Active session' })
  expect(options.onRemove).not.toHaveBeenCalled()
  const missing = screen.getByRole('region', { name: 'Unavailable remembered sessions' })
  fireEvent.click(within(missing).getByRole('button', { name: 'Remove from this browser' }))
  expect(options.onRemove).toHaveBeenLastCalledWith(SECOND)
  fireEvent.click(screen.getAllByRole('button', { name: 'Remove from this browser' })[0])
  expect(options.onRemove).toHaveBeenLastCalledWith(FIRST)
  expect(fetch).not.toHaveBeenCalled()
})

test('clear requires confirmation and removes only remembered history through its callback', async () => {
  const options = props()
  hydrate.mockResolvedValue(state([summary()]))
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
  const view = render(<History {...options} />)
  await screen.findByRole('heading', { name: 'Active session' })
  sessionStorage.setItem('rehearse.session_id', FIRST)
  fireEvent.click(screen.getByRole('button', { name: 'Clear remembered history' }))
  expect(confirm).toHaveBeenCalledWith('Clear sessions remembered on this browser?\n\nThis removes the local history list. It does not delete sessions stored on the server.')
  expect(options.onClear).not.toHaveBeenCalled()
  confirm.mockReturnValue(true)
  fireEvent.click(screen.getByRole('button', { name: 'Clear remembered history' }))
  expect(options.onClear).toHaveBeenCalledOnce()
  view.rerender(<History {...options} sessionIds={[]} />)
  expect(screen.getByText('No sessions are remembered on this browser yet.')).toBeTruthy()
  expect(screen.queryByRole('heading', { name: 'Active session' })).toBeNull()
  expect(sessionStorage.getItem('rehearse.session_id')).toBe(FIRST)
  sessionStorage.removeItem('rehearse.session_id')
})

test('partial results stay visible and retry only failed chunks with previous results', async () => {
  const partial = state([summary()], { status: 'partial', failedChunks: [[SECOND]], rememberedCount: 2 })
  hydrate.mockResolvedValueOnce(partial).mockResolvedValueOnce(state([summary(SECOND, 'completed'), summary()], { rememberedCount: 2 }))
  render(<History {...props([FIRST, SECOND])} />)
  await screen.findByRole('heading', { name: 'Active session' })
  expect(screen.getByRole('alert').textContent).toContain('incomplete')
  fireEvent.click(screen.getByRole('button', { name: 'Retry failed history requests' }))
  await screen.findByRole('heading', { name: 'Completed session' })
  expect(hydrate.mock.calls[1][1]?.previous).toEqual(partial)
  expect(hydrate.mock.calls[1][1]?.retryChunks).toEqual([[SECOND]])
  expect(screen.queryByRole('alert')).toBeNull()
})

test('all failed requests differ from an empty registry and retain remembered entries', async () => {
  const options = props()
  hydrate.mockResolvedValue(state([], { status: 'error', failedChunks: [[FIRST]], rememberedCount: 1 }))
  render(<History {...options} />)
  await screen.findByRole('alert')
  expect(screen.getByRole('alert').textContent).toBe('Remembered sessions could not be loaded.')
  expect(screen.queryByText('No sessions are remembered on this browser yet.')).toBeNull()
  expect(screen.getByRole('button', { name: 'Retry failed history requests' })).toBeTruthy()
  expect(options.onRemove).not.toHaveBeenCalled()
  expect(options.onClear).not.toHaveBeenCalled()
})

test('a storage warning keeps the empty-state Practice action usable', () => {
  const options = props([])
  render(<History {...options} storageError="History could not be saved in this browser." />)
  expect(screen.getByRole('status').textContent).toContain('History could not be saved')
  fireEvent.click(screen.getByRole('button', { name: 'Practice' }))
  expect(options.onPractice).toHaveBeenCalledOnce()
})

test('a registry change aborts old hydration and excludes its late results', async () => {
  const old = deferred<HistoryHydrationState>()
  hydrate.mockReturnValueOnce(old.promise).mockResolvedValueOnce(state([summary(SECOND, 'completed')]))
  const options = props()
  const view = render(<History {...options} />)
  const oldSignal = hydrate.mock.calls[0][1]?.signal
  view.rerender(<History {...options} sessionIds={[SECOND]} />)
  await screen.findByRole('heading', { name: 'Completed session' })
  expect(oldSignal?.aborted).toBe(true)
  await act(async () => old.resolve(state([summary()])))
  expect(screen.queryByRole('heading', { name: 'Active session' })).toBeNull()
})

test('clearing or unmounting cancels hydration and cannot restore late rows', async () => {
  const old = deferred<HistoryHydrationState>()
  hydrate.mockReturnValueOnce(old.promise)
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  const options = props()
  const view = render(<History {...options} />)
  const signal = hydrate.mock.calls[0][1]?.signal
  fireEvent.click(screen.getByRole('button', { name: 'Clear remembered history' }))
  expect(signal?.aborted).toBe(true)
  await act(async () => old.resolve(state([summary()])))
  expect(screen.queryByRole('heading', { name: 'Active session' })).toBeNull()
  view.unmount()
  expect(fetch).not.toHaveBeenCalled()
})

test('opening detail uses app state and removal closes it without leaving stale sensitive content', async () => {
  hydrate.mockResolvedValue(state([summary()]))
  readDetail.mockResolvedValue({ summary: summary(), questions: [], selected_question: null })
  const options = props()
  const originalUrl = window.location.href
  const view = render(<History {...options} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Open session' }))
  await screen.findByRole('heading', { name: 'Session detail' })
  expect(window.location.href).toBe(originalUrl)
  expect(readDetail.mock.calls[0][0]).toBe(FIRST)
  view.rerender(<History {...options} sessionIds={[THIRD]} />)
  await waitFor(() => expect(screen.queryByRole('heading', { name: 'Session detail' })).toBeNull())
  view.rerender(<History {...options} sessionIds={[FIRST]} />)
  expect(screen.queryByRole('heading', { name: 'Session detail' })).toBeNull()
})
