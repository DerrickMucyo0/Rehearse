// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import type { HistoryFinalizedPoint, HistoryMeasurement, HistorySummary } from './historyApi'
import type { HistoryHydrationState } from './historyHydration'
import ProgressDashboard from './ProgressDashboard'
import type { DeliveryMetrics } from './deliveryMetrics'

const FIRST = '00000000-0000-4000-8000-000000000001'
const SECOND = '00000000-0000-4000-8000-000000000002'
function measurement(changes: Partial<HistoryMeasurement> = {}): HistoryMeasurement {
  return {
    measurement_version: 'speaking_metrics_v1', measurement_source: 'original_transcription',
    recognized_word_count: 10, um_count: 0, uh_count: 1, filler_unavailable_reason: null,
    timed_utterance_span_seconds: 12.123456789, estimated_words_per_minute: 49.491231198,
    timing_unavailable_reason: null, delivery_metrics: null, ...changes,
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
  return { status: 'complete', summaries, nextCursor: null, pageError: false, ...changes }
}
function setup(history: HistoryHydrationState = state()) {
  const props = { history, onRetry: vi.fn(), onReload: vi.fn(), onPractice: vi.fn() }
  return { ...render(<ProgressDashboard {...props} />), props }
}

beforeEach(() => { vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Provider and other unmocked requests forbidden'))) })
afterEach(() => { cleanup(); expect(fetch).not.toHaveBeenCalled(); vi.unstubAllGlobals() })

test('loading server History has a readable status and hides complete totals', () => {
  setup(state([], { status: 'loading' }))
  expect(screen.getByRole('status').textContent).toBe('Loading saved sessions…')
  expect(screen.queryByRole('region', { name: 'Progress overview' })).toBeNull()
  expect(screen.getByText('Progress totals are unavailable until all saved sessions load.')).toBeTruthy()
})
test('idle hydration is presented as loading rather than an empty server', () => {
  setup(state([], { status: 'idle' }))
  expect(screen.getByRole('status')).toBeTruthy()
  expect(screen.queryByText('No saved sessions yet.')).toBeNull()
})
test('empty server History explicitly offers Practice', () => {
  const { props } = setup(state([]))
  expect(screen.getByText('No saved sessions yet.')).toBeTruthy()
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
  const { props } = setup(state([summary()], { status: 'partial', nextCursor: 'next', pageError: true }))
  expect(screen.getByRole('alert').textContent).toBe('More saved sessions could not be loaded.')
  expect(screen.queryByRole('region', { name: 'Progress overview' })).toBeNull()
  expect(screen.getByText('Showing finalized answers from loaded sessions only.')).toBeTruthy()
  expect(screen.getAllByRole('table')).toHaveLength(5)
  fireEvent.click(screen.getByRole('button', { name: 'Retry history request' }))
  expect(props.onRetry).toHaveBeenCalledOnce()
})
test('failed hydration is retryable and distinct from an empty registry', () => {
  const { props } = setup(state([], { status: 'error', pageError: true }))
  expect(screen.getByRole('alert').textContent).toBe('Saved sessions could not be loaded.')
  expect(screen.queryByText('No saved sessions yet.')).toBeNull()
  expect(screen.queryByRole('region', { name: 'Progress overview' })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Retry history request' }))
  expect(props.onRetry).toHaveBeenCalledOnce()
})
test('reload delegates to shared history owner without fetching itself', () => {
  const { props } = setup()
  fireEvent.click(screen.getByRole('button', { name: 'Reload history' }))
  expect(props.onReload).toHaveBeenCalledOnce()
})
test('more server pages keep totals incomplete and provide an explicit load-more action', () => {
  const onLoadMore = vi.fn()
  render(<ProgressDashboard history={state([summary()], { status: 'partial', nextCursor: 'next' })} onRetry={vi.fn()} onReload={vi.fn()} onLoadMore={onLoadMore} />)
  expect(screen.queryByRole('alert')).toBeNull()
  expect(screen.queryByRole('region', { name: 'Progress overview' })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Load more sessions' }))
  expect(onLoadMore).toHaveBeenCalledOnce()
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

function delivery(changes: Partial<DeliveryMetrics> = {}): DeliveryMetrics {
  return { version: 'pause-metrics-v1', source: 'original_transcription', pause_count: 2,
    total_pause_duration_seconds: 1.234567890123, longest_pause_seconds: 0.765432109876,
    unavailable_reason: null, ...changes }
}
function recordedPoint(delivery_metrics: DeliveryMetrics | null = delivery(), changes: Partial<HistoryFinalizedPoint> = {}): HistoryFinalizedPoint {
  return point({ measurement: measurement({ delivery_metrics }), ...changes })
}

test.each([
  ['Pause count', 'count', '2'], ['Total pause time', 'seconds', '1.2 s'], ['Longest pause', 'seconds', '0.8 s'],
])('recorded %s has an accessible factual table and clear units', (name, unit, value) => {
  const original = delivery()
  setup(state([summary({ finalized_points: [recordedPoint(original)] })]))
  const section = screen.getByRole('region', { name })
  const table = within(section).getByRole('table', { name: `${name} (${unit})` })
  expect(within(table).getAllByRole('columnheader')).toHaveLength(5)
  expect(within(table).getByRole('columnheader', { name: `Value (${unit})` })).toBeTruthy()
  expect(within(table).getAllByRole('row')[1].lastElementChild?.textContent).toBe(value)
  expect(section.textContent).toContain('Delivery measurement version: pause-metrics-v1')
  expect(section.textContent).toContain('Source: Original transcription')
  expect(original.total_pause_duration_seconds).toBe(1.234567890123)
  expect(original.longest_pause_seconds).toBe(0.765432109876)
})

test('timed pauses explains lexical gaps and acoustic limitation without delivery judgments', () => {
  setup(state([summary({ finalized_points: [recordedPoint()] })]))
  const section = screen.getByRole('region', { name: 'Timed pauses' })
  expect(within(section).getByText('Timed pauses are gaps of at least 0.50 seconds between consecutive recognized words in the original transcription.')).toBeTruthy()
  expect(within(section).getByText('These gaps are not necessarily acoustic silence.')).toBeTruthy()
  expect(section.textContent).not.toMatch(/\b(better|worse|improved|improvement|regressed|confidence|fluent|fluency|weak|strong|ideal|score|average|median|trend)\b/i)
})

test('available delivery zeros show count0 and fixed-decimal duration0.0, not unavailability', () => {
  setup(state([summary({ finalized_points: [recordedPoint(delivery({ pause_count: 0, total_pause_duration_seconds: 0, longest_pause_seconds: 0 }))] })]))
  const section = screen.getByRole('region', { name: 'Timed pauses' })
  expect(within(section).getByText('0')).toBeTruthy()
  expect(within(section).getAllByText('0.0 s')).toHaveLength(2)
  expect(section.textContent).not.toContain('Unavailable')
  expect(section.textContent).not.toContain('Not recorded')
  expect(section.textContent).toContain('available for 1 of 1')
})

test.each(['missing_timings', 'timing_coverage_mismatch', 'invalid_timing', 'invalid_timing_order', 'unusable_span'] as const)(
  'recorded delivery %s stays unavailable with factual explanation and denominator', (unavailable_reason) => {
    setup(state([summary({ finalized_points: [recordedPoint(delivery({
      pause_count: null, total_pause_duration_seconds: null, longest_pause_seconds: null, unavailable_reason,
    }))] })]))
    const section = screen.getByRole('region', { name: 'Timed pauses' })
    expect(within(section).getAllByText(/^Unavailable — /)).toHaveLength(3)
    expect(section.textContent).toContain('available for 0 of 1')
    expect(section.textContent).not.toContain(unavailable_reason)
    expect(section.textContent).not.toContain('Not recorded')
    expect(section.textContent).not.toContain('0.0 s')
  },
)

test('legacy finalized measurements display Not recorded without a fabricated pause version or delivery cohort', () => {
  setup(state([summary({ finalized_points: [recordedPoint(null)] })]))
  const section = screen.getByRole('region', { name: 'Timed pauses' })
  expect(within(section).getByText('Timed pause analysis: Not recorded for 1 finalized measured answer.')).toBeTruthy()
  expect(within(section).queryByRole('table')).toBeNull()
  expect(section.textContent).not.toContain('pause-metrics-v1')
  expect(screen.getAllByRole('table')).toHaveLength(5)
})

test('typed finals keep established no-measurement meaning without becoming historical delivery rows', () => {
  setup(state([summary({ finalized_points: [point({ measurement: null })], measured_final_answer_count: 0 })]))
  expect(screen.getAllByText('Unavailable — No measurement')).toHaveLength(5)
  expect(screen.queryByText(/Timed pause analysis: Not recorded/)).toBeNull()
  expect(within(screen.getByRole('region', { name: 'Timed pauses' })).queryByRole('table')).toBeNull()
})

test('delivery coverage counts recorded-unavailable but excludes legacy and typed finals', () => {
  const unavailable = delivery({ pause_count: null, total_pause_duration_seconds: null, longest_pause_seconds: null, unavailable_reason: 'missing_timings' })
  setup(state([summary({ finalized_points: [recordedPoint(), recordedPoint(unavailable), recordedPoint(null), point({ measurement: null })], measured_final_answer_count: 3 })]))
  expect(screen.getByText('Pause count available for 1 of 2 finalized answers in this delivery provenance group.')).toBeTruthy()
  expect(screen.getByText('Timed pause analysis: Not recorded for 1 finalized measured answer.')).toBeTruthy()
  expect(screen.getByText('Measured final answers').nextElementSibling?.textContent).toBe('3')
  expect(within(screen.getByRole('region', { name: 'Pause count' })).getAllByRole('row')).toHaveLength(3)
})

test('delivery versions separate tables even when speaking measurement version matches', () => {
  setup(state([summary({ finalized_points: [recordedPoint(), recordedPoint(delivery({ version: 'pause-metrics-v2' }))] })]))
  const region = screen.getByRole('region', { name: 'Pause count' })
  expect(within(region).getAllByRole('table')).toHaveLength(2)
  expect(within(region).getByText('Delivery measurement version: pause-metrics-v1')).toBeTruthy()
  expect(within(region).getByText('Delivery measurement version: pause-metrics-v2')).toBeTruthy()
  expect(within(region).getAllByText('Pause count available for 1 of 1 finalized answers in this delivery provenance group.')).toHaveLength(2)
  expect(within(screen.getByRole('region', { name: 'Estimated WPM' })).getAllByRole('table')).toHaveLength(1)
})

test('compatible delivery cohort stays unified when speaking versions differ', () => {
  const second = recordedPoint(delivery(), { measurement: measurement({ measurement_version: 'speaking-metrics-v2', delivery_metrics: delivery() }) })
  setup(state([summary({ finalized_points: [recordedPoint(), second] })]))
  expect(within(screen.getByRole('region', { name: 'Pause count' })).getAllByRole('table')).toHaveLength(1)
  expect(screen.getByText('Pause count available for 2 of 2 finalized answers in this delivery provenance group.')).toBeTruthy()
  expect(within(screen.getByRole('region', { name: 'Estimated WPM' })).getAllByRole('table')).toHaveLength(2)
})

test('future delivery sources remain explicit separate groups in pure projected UI', () => {
  const second = recordedPoint(delivery({ source: 'future_source' }), { measurement: measurement({
    measurement_source: 'future_source' as HistoryMeasurement['measurement_source'], delivery_metrics: delivery({ source: 'future_source' }),
  }) })
  setup(state([summary({ finalized_points: [recordedPoint(), second] })]))
  const region = screen.getByRole('region', { name: 'Pause count' })
  expect(within(region).getAllByRole('table')).toHaveLength(2)
  expect(within(region).getByText('Source: future_source')).toBeTruthy()
  expect(within(region).getByText('Source: Original transcription')).toBeTruthy()
})

test('delivery rows retain chronological order and microseconds rather than supplied summary order', () => {
  setup(state([
    summary({ finalized_points: [recordedPoint(delivery(), { submitted_at: '2026-10-05T12:00:00.000009Z', question_index: 1 })] }),
    summary({ session_id: SECOND, finalized_points: [recordedPoint(delivery(), { submitted_at: '2026-10-05T12:00:00.000001Z' })] }),
  ]))
  const rows = within(screen.getByRole('region', { name: 'Pause count' })).getAllByRole('row').slice(1)
  expect(rows.map((row) => row.querySelector('time')!.dateTime)).toEqual(['2026-10-05T12:00:00.000001Z', '2026-10-05T12:00:00.000009Z'])
  expect(rows[0].textContent).toContain('Question 1')
  expect(rows[1].textContent).toContain('Question 2')
  expect(document.body.textContent).not.toContain(FIRST)
  expect(document.body.textContent).not.toContain(SECOND)
})

test('partial hydration shows loaded delivery facts while suppressing all overview totals', () => {
  const { props } = setup(state([summary({ finalized_points: [recordedPoint()] })], { status: 'partial', nextCursor: 'next', pageError: true }))
  expect(screen.queryByRole('region', { name: 'Progress overview' })).toBeNull()
  expect(screen.getByText('Progress totals are unavailable until all saved sessions load.')).toBeTruthy()
  expect(screen.getByText('Showing finalized answers from loaded sessions only.')).toBeTruthy()
  expect(within(screen.getByRole('region', { name: 'Timed pauses' })).getAllByRole('table')).toHaveLength(3)
  fireEvent.click(screen.getByRole('button', { name: 'Retry history request' }))
  expect(props.onRetry).toHaveBeenCalledOnce()
})

test('delivery projection and presentation do not mutate exact persisted values or fetch new detail', () => {
  const original = delivery()
  const source = summary({ finalized_points: [recordedPoint(original)] })
  const before = JSON.stringify(source)
  setup(state([source]))
  expect(JSON.stringify(source)).toBe(before)
  expect(screen.getAllByRole('table')).toHaveLength(8)
  expect(fetch).not.toHaveBeenCalled()
})
