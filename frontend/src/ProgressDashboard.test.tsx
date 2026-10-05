// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import type { HistoryFinalizedPoint, HistoryMeasurement, HistorySummary } from './historyApi'
import type { HistoryHydrationState } from './historyHydration'
import ProgressDashboard from './ProgressDashboard'

const FIRST = '00000000-0000-4000-8000-000000000001'
const SECOND = '00000000-0000-4000-8000-000000000002'
function measurement(changes: Partial<HistoryMeasurement> = {}): HistoryMeasurement {
  return {
    measurement_version: 'speaking_metrics_v1', measurement_source: 'original_transcription',
    recognized_word_count: 10, um_count: 0, uh_count: 1, filler_unavailable_reason: null,
    timed_utterance_span_seconds: 12.123456789, estimated_words_per_minute: 49.491231198,
    timing_unavailable_reason: null, ...changes,
  }
}
function point(changes: Partial<HistoryFinalizedPoint> = {}): HistoryFinalizedPoint {
  return { question_index: 0, attempt_id: FIRST, attempt_number: 2, submitted_at: '2026-10-05T12:00:00Z', measurement: measurement(), ...changes }
}
function summary(changes: Partial<HistorySummary> = {}): HistorySummary {
  return {
    session_id: FIRST, status: 'active', created_at: '2026-10-05T10:00:00Z', completed_at: null,
    current_question_number: 2, total_questions: 5, finalized_question_count: 1,
    questions_practiced_count: 2, total_attempt_count: 4, total_retry_count: 2,
    measured_final_answer_count: 1, last_submitted_at: '2026-10-05T12:00:00Z',
    last_saved_activity_at: '2026-10-05T12:00:00Z', finalized_points: [point()], ...changes,
  }
}
function state(summaries: HistorySummary[] = [summary()], changes: Partial<HistoryHydrationState> = {}): HistoryHydrationState {
  return { status: 'complete', summaries, missingIds: [], failedChunks: [], rememberedCount: summaries.length, ...changes }
}
function setup(history: HistoryHydrationState = state()) {
  const props = { history, onRetry: vi.fn(), onReload: vi.fn(), onPractice: vi.fn() }
  return { ...render(<ProgressDashboard {...props} />), props }
}

beforeEach(() => { vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Provider and other unmocked requests forbidden'))) })
afterEach(() => { cleanup(); expect(fetch).not.toHaveBeenCalled(); vi.unstubAllGlobals() })

test('loading remembered entries has a readable status and hides complete totals', () => {
  setup(state([], { status: 'loading', rememberedCount: 1 }))
  expect(screen.getByRole('status').textContent).toBe('Loading remembered sessions…')
  expect(screen.queryByRole('region', { name: 'Progress overview' })).toBeNull()
  expect(screen.getByText('Progress totals are unavailable until all remembered sessions load.')).toBeTruthy()
})
test('idle hydration is presented as loading rather than an empty server', () => {
  setup(state([], { status: 'idle', rememberedCount: 1 }))
  expect(screen.getByRole('status')).toBeTruthy()
  expect(screen.queryByText('No sessions are remembered on this browser yet.')).toBeNull()
})
test('empty remembered history explicitly identifies browser scope', () => {
  const { props } = setup(state([]))
  expect(screen.getByText('No sessions are remembered on this browser yet.')).toBeTruthy()
  expect(screen.queryByRole('region', { name: 'Progress overview' })).toBeNull()
  expect(screen.queryByRole('table')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Practice' }))
  expect(props.onPractice).toHaveBeenCalledOnce()
})
test('complete hydration renders factual overview and five individual metric tables', () => {
  setup()
  expect(screen.getByRole('region', { name: 'Progress overview' })).toBeTruthy()
  expect(screen.getAllByRole('table')).toHaveLength(5)
  expect(screen.queryByRole('alert')).toBeNull()
})
test('partial hydration hides totals while preserving loaded chronological facts', () => {
  const { props } = setup(state([summary()], { status: 'partial', rememberedCount: 2, failedChunks: [[SECOND]] }))
  expect(screen.getByRole('alert').textContent).toBe('Some remembered sessions could not be loaded.')
  expect(screen.queryByRole('region', { name: 'Progress overview' })).toBeNull()
  expect(screen.getByText('Showing finalized answers from loaded sessions only.')).toBeTruthy()
  expect(screen.getAllByRole('table')).toHaveLength(5)
  fireEvent.click(screen.getByRole('button', { name: 'Retry failed history requests' }))
  expect(props.onRetry).toHaveBeenCalledOnce()
})
test('failed hydration is retryable and distinct from an empty registry', () => {
  const { props } = setup(state([], { status: 'error', rememberedCount: 1, failedChunks: [[FIRST]] }))
  expect(screen.getByRole('alert').textContent).toBe('Remembered sessions could not be loaded.')
  expect(screen.queryByText('No sessions are remembered on this browser yet.')).toBeNull()
  expect(screen.queryByRole('region', { name: 'Progress overview' })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Retry failed history requests' }))
  expect(props.onRetry).toHaveBeenCalledOnce()
})
test('reload delegates to shared history owner without fetching itself', () => {
  const { props } = setup()
  fireEvent.click(screen.getByRole('button', { name: 'Reload history' }))
  expect(props.onReload).toHaveBeenCalledOnce()
})
test('storage errors remain nonblocking and clearly readable', () => {
  render(<ProgressDashboard history={state()} onRetry={vi.fn()} onReload={vi.fn()} storageError="History could not be saved in this browser." />)
  expect(screen.getByRole('status').textContent).toBe('History could not be saved in this browser.')
  expect(screen.getByRole('region', { name: 'Progress overview' })).toBeTruthy()
})

test.each([
  ['Completed sessions', '1'], ['Active sessions', '1'], ['Finalized questions', '6'],
  ['Saved attempts', '12'], ['Saved retries', '5'], ['Measured final answers', '5'],
])('overview card %s displays exact persisted count %s', (label, value) => {
  setup(state([summary(), summary({ session_id: SECOND, status: 'completed', finalized_question_count: 5, total_attempt_count: 8, total_retry_count: 3, measured_final_answer_count: 4 })]))
  const overview = screen.getByRole('region', { name: 'Progress overview' })
  expect(within(overview).getByText(label).nextElementSibling?.textContent).toBe(value)
})
test.each([
  ['Estimated WPM', 'words/minute', '49.5'], ['Um count', 'count', '0'], ['Uh count', 'count', '1'],
  ['Timed speech span', 'seconds', '12.1'], ['Recognized words', 'words', '10'],
])('%s table exposes units, column headers, and stored display value', (name, unit, value) => {
  setup()
  const region = screen.getByRole('region', { name })
  const table = within(region).getByRole('table', { name: `${name} (${unit})` })
  expect(within(table).getAllByRole('columnheader')).toHaveLength(5)
  expect(within(table).getByRole('columnheader', { name: `Value (${unit})` })).toBeTruthy()
  expect(within(table).getAllByRole('row')[1].lastElementChild?.textContent).toBe(value)
})
test('typed final is retained in every metric table as explicit no measurement', () => {
  setup(state([summary({ finalized_points: [point({ measurement: null })], measured_final_answer_count: 0 })]))
  expect(screen.getAllByText('Unavailable — No measurement')).toHaveLength(5)
  expect(screen.getAllByText('Finalized answers without a measurement')).toHaveLength(5)
  expect(screen.getByText('Estimated WPM available for 0 of 1 finalized answers in this no-measurement group.')).toBeTruthy()
  expect(screen.queryByText(/Measurement version:/)).toBeNull()
})
test('zero filler is literal zero and available, not an unavailable placeholder', () => {
  setup(state([summary({ finalized_points: [point({ measurement: measurement({ um_count: 0, uh_count: 0 }) })] })]))
  for (const name of ['Um count', 'Uh count']) {
    const region = screen.getByRole('region', { name })
    expect(within(region).getByText('0')).toBeTruthy()
    expect(region.textContent).toContain('available for 1 of 1')
    expect(region.textContent).not.toContain('Unavailable')
  }
})
test('unavailable filler and timing render distinct factual reasons without changing words', () => {
  setup(state([summary({ finalized_points: [point({ measurement: measurement({
    um_count: null, uh_count: null, filler_unavailable_reason: 'unsupported_language',
    timed_utterance_span_seconds: null, estimated_words_per_minute: null, timing_unavailable_reason: 'missing_timings',
  }) })] })]))
  expect(screen.getAllByText('Unavailable — Unsupported language')).toHaveLength(2)
  expect(screen.getAllByText('Unavailable — Missing timings')).toHaveLength(2)
  expect(screen.getByRole('region', { name: 'Recognized words' }).textContent).toContain('available for 1 of 1')
})
test.each(['timing_coverage_mismatch', 'invalid_timing', 'invalid_timing_order', 'unusable_span'] as const)('timing cause %s remains factual unavailable text', (reason) => {
  setup(state([summary({ finalized_points: [point({ measurement: measurement({ timed_utterance_span_seconds: null, estimated_words_per_minute: null, timing_unavailable_reason: reason }) })] })]))
  const region = screen.getByRole('region', { name: 'Estimated WPM' })
  expect(region.textContent).toContain(`Unavailable — ${reason.split('_').map((word, index) => index === 0 ? word[0].toUpperCase() + word.slice(1) : word).join(' ')}`)
})
test('unknown future unavailable cause shows generic unavailable without echoing machine data', () => {
  const marker = 'future_reason_marker'
  setup(state([summary({ finalized_points: [point({ measurement: measurement({
    um_count: null, uh_count: null, filler_unavailable_reason: marker as HistoryMeasurement['filler_unavailable_reason'],
  }) })] })]))
  expect(screen.getAllByText('Unavailable')).toHaveLength(2)
  expect(document.body.textContent).not.toContain(marker)
})
test('version and original source are visibly identified for every metric', () => {
  setup()
  expect(screen.getAllByText('Measurement version: speaking_metrics_v1')).toHaveLength(5)
  expect(screen.getAllByText('Source: Original transcription')).toHaveLength(5)
})
test('second version has distinct tables and coverage denominator', () => {
  setup(state([summary({ finalized_points: [point(), point({ question_index: 1, measurement: measurement({ measurement_version: 'speaking_metrics_v2' }) })] })]))
  const region = screen.getByRole('region', { name: 'Estimated WPM' })
  expect(within(region).getAllByRole('table')).toHaveLength(2)
  expect(within(region).getByText('Measurement version: speaking_metrics_v1')).toBeTruthy()
  expect(within(region).getByText('Measurement version: speaking_metrics_v2')).toBeTruthy()
  expect(within(region).getAllByText('Estimated WPM available for 1 of 1 finalized answers in this provenance group.')).toHaveLength(2)
})
test('future unknown source is literal text in its own group rather than merged', () => {
  setup(state([summary({ finalized_points: [point(), point({ measurement: measurement({ measurement_source: 'future_source' as HistoryMeasurement['measurement_source'] }) })] })]))
  expect(screen.getAllByText('Source: future_source')).toHaveLength(5)
  expect(screen.getAllByRole('table')).toHaveLength(10)
})
test('coverage does not omit an unavailable measured member', () => {
  setup(state([summary({ finalized_points: [point(), point({ measurement: measurement({ um_count: null, uh_count: null, filler_unavailable_reason: 'unsupported_language' }) })] })]))
  expect(screen.getByText('Um count available for 1 of 2 finalized answers in this provenance group.')).toBeTruthy()
  expect(within(screen.getByRole('region', { name: 'Um count' })).getAllByRole('row')).toHaveLength(3)
})
test('chronological table order is independent of hydration summary order', () => {
  setup(state([
    summary({ finalized_points: [point({ submitted_at: '2026-10-06T12:00:00Z', question_index: 1, attempt_number: 4 })] }),
    summary({ session_id: SECOND, finalized_points: [point({ submitted_at: '2026-10-04T12:00:00Z', question_index: 0, attempt_number: 2 })] }),
  ]))
  const rows = within(screen.getByRole('region', { name: 'Recognized words' })).getAllByRole('row').slice(1)
  expect(rows.map((row) => row.querySelector('time')?.dateTime)).toEqual(['2026-10-04T12:00:00Z', '2026-10-06T12:00:00Z'])
  expect(rows[0].textContent).toContain('Question 1')
  expect(rows[1].textContent).toContain('Question 2')
  expect(rows[0].children[3].textContent).toBe('2')
  expect(rows[1].children[3].textContent).toBe('4')
})
test('persisted date/time values are used in accessible time elements', () => {
  const { container } = setup()
  expect([...container.querySelectorAll('time')].every((element) => element.dateTime === '2026-10-05T12:00:00Z' && element.textContent!.length > 0)).toBe(true)
})
test('active and completed row contexts are displayed without raw UUIDs', () => {
  const { container } = setup(state([summary(), summary({ session_id: SECOND, status: 'completed' })]))
  expect(screen.getAllByText('Active')).toHaveLength(5)
  expect(screen.getAllByText('Completed')).toHaveLength(5)
  expect(container.textContent).not.toContain(FIRST)
  expect(container.textContent).not.toContain(SECOND)
})
test('unavailable remembered server entries are described without claiming their totals', () => {
  setup(state([summary()], { missingIds: [SECOND], rememberedCount: 2 }))
  expect(screen.getByText('1 remembered session is unavailable. These totals describe the available persisted sessions.')).toBeTruthy()
})
test('loaded sessions without finalized answers show factual no-points state', () => {
  setup(state([summary({ finalized_points: [], finalized_question_count: 0, measured_final_answer_count: 0 })]))
  expect(screen.getByText('No finalized question answers are available in the loaded sessions.')).toBeTruthy()
  expect(screen.queryByRole('table')).toBeNull()
})
test('UI contains no scores, quality judgments, directions, or aggregate metric statistics', () => {
  const { container } = setup()
  expect(container.textContent).not.toMatch(/\b(improved|improvement|better|worse|good|bad|score|performance|readiness|confidence|quality|strong|weak|average|mean|median|trend)\b/i)
  expect(container.querySelector('svg,canvas')).toBeNull()
})
test('future provenance is React plaintext and cannot inject markup', () => {
  const marker = '<img src=x onerror=alert(1)>'
  const { container } = setup(state([summary({ finalized_points: [point({ measurement: measurement({ measurement_version: marker }) })] })]))
  expect(screen.getAllByText(`Measurement version: ${marker}`)).toHaveLength(5)
  expect(container.querySelector('img')).toBeNull()
})
