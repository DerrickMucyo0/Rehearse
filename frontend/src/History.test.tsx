import { authenticateTestWorkspace } from './authTestUtils'
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
  measurement_version: 'speaking-metrics-v1', measurement_source: 'original_transcription' as const, delivery_metrics: null,
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
  return { status: 'complete', summaries, nextCursor: null, pageError: false, ...changes }
}
function props() { return { onPractice: vi.fn() } }

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}

beforeEach(async () => {
  await authenticateTestWorkspace()
  hydrate.mockReset()
  readDetail.mockReset()
  hydrate.mockResolvedValue(state())
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Unmocked network is forbidden')))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks() })

test('empty server History offers Practice and does not offer local membership deletion', async () => {
  const options = props()
  render(<History {...options} />)
  await screen.findByText('No saved sessions yet.')
  fireEvent.click(screen.getByRole('button', { name: 'Practice' }))
  expect(options.onPractice).toHaveBeenCalledOnce()
  expect(screen.queryByRole('button', { name: /Clear|Remove/ })).toBeNull()
  expect(hydrate).toHaveBeenCalledOnce()
  expect(readDetail).not.toHaveBeenCalled()
  expect(fetch).not.toHaveBeenCalled()
})
test('server-discovered active and completed rows display facts without raw UUIDs or judgments', async () => {
  hydrate.mockResolvedValue(state([summary(SECOND, 'completed'), summary(FIRST)]))
  const view = render(<History {...props()} />)
  await screen.findByRole('heading', { name: 'Active session' })
  const rows = view.container.querySelectorAll('.history-summary')
  expect(rows).toHaveLength(2)
  expect(within(rows[0] as HTMLElement).getByRole('heading').textContent).toBe('Completed session')
  const active = within(rows[1] as HTMLElement)
  expect(active.getByText('2 / 5')).toBeTruthy()
  expect(active.getByText('Attempts').nextElementSibling?.textContent).toBe('5')
  expect(active.getByText('Retries').nextElementSibling?.textContent).toBe('2')
  expect(active.getByText('Current question').nextElementSibling?.textContent).toBe('3 of 5')
  expect(view.container.textContent).not.toContain(FIRST)
  expect(view.container.textContent).not.toContain(SECOND)
  expect(view.container.textContent).not.toMatch(/\b(improved|better|worse|score|readiness|confidence)\b/i)
})
test('legacy browser registry is ignored and server sessions alone appear', async () => {
  localStorage.setItem('rehearse.history.v1', JSON.stringify([THIRD]))
  sessionStorage.setItem('rehearse.session_id', THIRD)
  hydrate.mockResolvedValue(state([summary(FIRST)]))
  const view = render(<History {...props()} />)
  await screen.findByRole('heading', { name: 'Active session' })
  expect(view.container.querySelectorAll('.history-summary')).toHaveLength(1)
  expect(hydrate.mock.calls[0][0]).not.toHaveProperty('sessionIds')
  expect(localStorage.getItem('rehearse.history.v1')).toBe(JSON.stringify([THIRD]))
  expect(sessionStorage.getItem('rehearse.session_id')).toBe(THIRD)
  localStorage.clear(); sessionStorage.clear()
})
test('pagination is explicit and keeps earlier rows until the new page loads', async () => {
  const first = state([summary()], { status: 'partial', nextCursor: 'next' })
  hydrate.mockResolvedValueOnce(first).mockResolvedValueOnce(state([summary(SECOND, 'completed'), summary()]))
  render(<History {...props()} />)
  await screen.findByRole('heading', { name: 'Active session' })
  expect(screen.queryByRole('alert')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Load more sessions' }))
  await screen.findByRole('heading', { name: 'Completed session' })
  expect(hydrate.mock.calls[1][0]?.previous).toEqual(first)
  expect(screen.queryByRole('button', { name: 'Load more sessions' })).toBeNull()
})
test('failed continuation retains rows and retries only the failed continuation', async () => {
  const failed = state([summary()], { status: 'partial', nextCursor: 'next', pageError: true })
  hydrate.mockResolvedValueOnce(failed).mockResolvedValueOnce(state([summary()]))
  render(<History {...props()} />)
  await screen.findByRole('alert')
  expect(screen.getByRole('alert').textContent).toContain('incomplete')
  expect(screen.getByRole('heading', { name: 'Active session' })).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Retry history request' }))
  await waitFor(() => expect(screen.queryByRole('alert')).toBeNull())
  expect(hydrate.mock.calls[1][0]?.previous).toEqual(failed)
})
test('initial failure differs from empty saved History', async () => {
  hydrate.mockResolvedValue(state([], { status: 'error', pageError: true }))
  render(<History {...props()} />)
  expect((await screen.findByRole('alert')).textContent).toBe('Saved sessions could not be loaded.')
  expect(screen.queryByText('No saved sessions yet.')).toBeNull()
})
test('unmount cancels hydration and cannot restore late rows', async () => {
  const old = deferred<HistoryHydrationState>()
  hydrate.mockReturnValueOnce(old.promise)
  const view = render(<History {...props()} />)
  const signal = hydrate.mock.calls[0][0]?.signal
  view.unmount()
  await act(async () => old.resolve(state([summary()])))
  expect(signal?.aborted).toBe(true)
  expect(fetch).not.toHaveBeenCalled()
})
test('opening owned detail uses memory selection and Back preserves discovery cache', async () => {
  hydrate.mockResolvedValue(state([summary()]))
  readDetail.mockResolvedValue({ summary: summary(), questions: [], selected_question: null })
  const originalUrl = window.location.href
  render(<History {...props()} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Open session' }))
  await screen.findByRole('heading', { name: 'Session detail' })
  expect(window.location.href).toBe(originalUrl)
  expect(readDetail.mock.calls[0][0]).toBe(FIRST)
  expect(screen.queryByRole('button', { name: /Remove/ })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Back to History' }))
  await screen.findByRole('heading', { name: 'Active session' })
  expect(hydrate).toHaveBeenCalledOnce()
})
