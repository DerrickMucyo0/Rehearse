// @vitest-environment jsdom
import { cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, expect, test } from 'vitest'
import AttemptComparison from './AttemptComparison'
import type { AttemptComparison as Comparison, ComparisonMetrics, DeliveryComparison, DeliveryMetricChange, MetricChange } from './interviewApi'
import { DELIVERY_TIMING_REASONS, deliveryUnavailableText } from './deliveryMetrics'

afterEach(cleanup)

function metric(before: number, after: number): MetricChange {
  return {
    before, after, delta: after - before, before_unavailable_reason: null,
    after_unavailable_reason: null, comparable: true, comparison_unavailable_reason: null,
  }
}

function legacyDelivery(): DeliveryComparison {
  const unavailable: DeliveryMetricChange = { before: null, after: null, delta: null,
    before_unavailable_reason: 'not_recorded', after_unavailable_reason: 'not_recorded',
    comparable: false, comparison_unavailable_reason: 'both_unavailable' }
  return { before_version: null, after_version: null, before_source: null, after_source: null,
    pause_count: unavailable, total_pause_duration_seconds: unavailable, longest_pause_seconds: unavailable }
}

function recordedDelivery(): DeliveryComparison {
  const deliveryMetric = (before: number, after: number): DeliveryMetricChange => ({
    ...metric(before, after), before_unavailable_reason: null, after_unavailable_reason: null,
  })
  return { before_version: 'pause-metrics-v1', after_version: 'pause-metrics-v1',
    before_source: 'original_transcription', after_source: 'original_transcription',
    pause_count: deliveryMetric(2, 1), total_pause_duration_seconds: deliveryMetric(1.456789, 0.55555), longest_pause_seconds: deliveryMetric(0.95555, 0.55555) }
}

function speakingTable() { return screen.getByRole('table', { name: 'Speaking duration is shown in seconds.' }) }

function comparison(overrides: Partial<ComparisonMetrics> = {}): Comparison {
  return {
    session_id: 'session-id', question_index: 0,
    before_attempt: {
      id: 'before-id', attempt_number: 1, measurement_id: 'before-measurement-id',
      measurement_version: 'speaking-metrics-v1', measurement_source: 'original_transcription',
    },
    after_attempt: {
      id: 'after-id', attempt_number: 2, measurement_id: 'after-measurement-id',
      measurement_version: 'speaking-metrics-v1', measurement_source: 'original_transcription',
    },
    comparison: {
      recognized_word_count: metric(92, 108), um_count: metric(4, 1), uh_count: metric(0, 0),
      timed_utterance_span_seconds: metric(31.234, 34.789),
      estimated_words_per_minute: metric(92.123, 108.456), ...overrides,
    },
    delivery_comparison: legacyDelivery(),
  }
}

function row(label: string): HTMLElement {
  return screen.getByRole('rowheader', { name: label }).closest('tr')!
}

function values(label: string): (string | null)[] {
  return within(row(label)).getAllByRole('cell').map(cell => cell.textContent)
}

test('renders a semantic neutral comparison table with all five metric labels', () => {
  render(<AttemptComparison comparison={comparison()} />)
  expect(screen.getByRole('region', { name: 'Before / After comparison' })).toBeTruthy()
  expect(screen.getByRole('heading', { name: 'Before / After comparison' })).toBeTruthy()
  expect(within(speakingTable()).getAllByRole('columnheader').map(cell => cell.textContent)).toEqual(['Metric', 'Before', 'After', 'Change'])
  expect(within(speakingTable()).getAllByRole('rowheader').map(cell => cell.textContent)).toEqual([
    'Recognized words', 'Um', 'Uh', 'Speaking duration', 'Words per minute',
  ])
  expect(screen.getByText('Before: Attempt 1. After: Attempt 2.')).toBeTruthy()
})

test('uses persisted identity labels for Attempt 3 and later without hardcoding two attempts', () => {
  const data = comparison()
  data.after_attempt = { ...data.after_attempt!, attempt_number: 5 }
  render(<AttemptComparison comparison={data} />)
  expect(screen.getByText('Before: Attempt 1. After: Attempt 5.')).toBeTruthy()
})

test('also honors an explicitly selected before attempt identity', () => {
  const data = comparison()
  data.before_attempt = { ...data.before_attempt!, attempt_number: 2 }
  data.after_attempt = { ...data.after_attempt!, attempt_number: 3 }
  render(<AttemptComparison comparison={data} />)
  expect(screen.getByText('Before: Attempt 2. After: Attempt 3.')).toBeTruthy()
})

test('formats counts as integers, positive and negative changes with signs, and measured zero as zero', () => {
  render(<AttemptComparison comparison={comparison()} />)
  expect(values('Recognized words')).toEqual(['92', '108', '+16'])
  expect(values('Um')).toEqual(['4', '1', '-3'])
  expect(values('Uh')).toEqual(['0', '0', '0'])
})

test('formats duration and pace to one decimal using the exact backend delta', () => {
  render(<AttemptComparison comparison={comparison()} />)
  expect(values('Speaking duration')).toEqual(['31.2', '34.8', '+3.6'])
  expect(values('Words per minute')).toEqual(['92.1', '108.5', '+16.3'])
})

test('preserves signs for nonzero decimal changes that display as zero', () => {
  render(<AttemptComparison comparison={comparison({
    timed_utterance_span_seconds: metric(1.14, 1.15),
    estimated_words_per_minute: metric(10.04, 10.03),
  })} />)
  expect(values('Speaking duration')).toEqual(['1.1', '1.1', '+0.0'])
  expect(values('Words per minute')).toEqual(['10.0', '10.0', '-0.0'])
})

test('displays an exactly zero decimal delta as zero', () => {
  render(<AttemptComparison comparison={comparison({ estimated_words_per_minute: metric(90, 90) })} />)
  expect(values('Words per minute')).toEqual(['90.0', '90.0', '0'])
})

test('hides the entire card when the backend comparison is null', () => {
  const data = comparison()
  data.comparison = null
  data.delivery_comparison = null
  data.after_attempt = null
  const { container } = render(<AttemptComparison comparison={data} />)
  expect(container.textContent).toBe('')
  expect(screen.queryByRole('table')).toBeNull()
})

test('keeps a non-null comparison visible even when both attempts have no measurement', () => {
  const unavailable: MetricChange = {
    before: null, after: null, delta: null,
    before_unavailable_reason: 'no_measurement', after_unavailable_reason: 'no_measurement',
    comparable: false, comparison_unavailable_reason: 'both_unavailable',
  }
  const data = comparison({
    recognized_word_count: unavailable, um_count: unavailable, uh_count: unavailable,
    timed_utterance_span_seconds: unavailable, estimated_words_per_minute: unavailable,
  })
  data.before_attempt = { ...data.before_attempt!, measurement_id: null, measurement_version: null, measurement_source: null }
  data.after_attempt = { ...data.after_attempt!, measurement_id: null, measurement_version: null, measurement_source: null }
  const unmeasured = { ...legacyDelivery().pause_count, before_unavailable_reason: 'no_measurement', after_unavailable_reason: 'no_measurement' } as const
  data.delivery_comparison = { ...legacyDelivery(), pause_count: unmeasured, total_pause_duration_seconds: unmeasured, longest_pause_seconds: unmeasured }
  render(<AttemptComparison comparison={data} />)
  expect(speakingTable()).toBeTruthy()
  for (const cell of within(speakingTable()).getAllByRole('cell')) {
    expect(cell.textContent?.startsWith('Unavailable')).toBe(true)
    expect(cell.textContent).not.toMatch(/^0$/)
  }
  expect(screen.getAllByText('No linked speaking measurement.')).toHaveLength(10)
  expect(within(speakingTable()).getAllByText('Both measurements are unavailable.')).toHaveLength(5)
})

test('unavailable filler counts remain null while recognized words remain available', () => {
  const unavailable: MetricChange = {
    ...metric(0, 2), before: null, delta: null, before_unavailable_reason: 'unsupported_language',
    comparable: false, comparison_unavailable_reason: 'before_unavailable',
  }
  render(<AttemptComparison comparison={comparison({ um_count: unavailable, uh_count: unavailable })} />)
  expect(values('Recognized words')).toEqual(['92', '108', '+16'])
  expect(values('Um')).toEqual([
    'UnavailableFiller counts are unavailable for this language.', '2',
    'UnavailableThe before measurement is unavailable.',
  ])
})

test.each([
  ['missing_timings', 'Word timings were not available.'],
  ['timing_coverage_mismatch', 'Word timings do not cover the recognized words.'],
  ['invalid_timing', 'Word timings were not usable.'],
  ['invalid_timing_order', 'Word timings were not in a usable order.'],
  ['unusable_span', 'A speaking duration could not be calculated from the word timings.'],
] as const)('translates the %s timing reason neutrally without inventing a zero', (reason, message) => {
  const unavailable: MetricChange = {
    ...metric(30, 40), before: null, delta: null, before_unavailable_reason: reason,
    comparable: false, comparison_unavailable_reason: 'before_unavailable',
  }
  render(<AttemptComparison comparison={comparison({
    timed_utterance_span_seconds: unavailable, estimated_words_per_minute: unavailable,
  })} />)
  expect(values('Speaking duration')).toEqual([
    `Unavailable${message}`, '40.0', 'UnavailableThe before measurement is unavailable.',
  ])
  expect(screen.getAllByText(message)).toHaveLength(2)
})

test.each([
  ['measurement_version_mismatch', 'Measurement versions differ.'],
  ['measurement_source_incompatible', 'Measurement sources are incompatible.'],
] as const)('shows non-comparable %s without subtracting available numbers', (reason, message) => {
  const nonComparable = { ...metric(92, 108), comparable: false, delta: null, comparison_unavailable_reason: reason }
  render(<AttemptComparison comparison={comparison({ recognized_word_count: nonComparable })} />)
  expect(values('Recognized words')).toEqual(['92', '108', `Unavailable${message}`])
  expect(within(row('Recognized words')).queryByText('+16')).toBeNull()
})

test('translates after-unavailable and preserves the available before value', () => {
  render(<AttemptComparison comparison={comparison({ recognized_word_count: {
    ...metric(92, 0), after: null, delta: null, after_unavailable_reason: 'no_measurement',
    comparable: false, comparison_unavailable_reason: 'after_unavailable',
  } })} />)
  expect(values('Recognized words')).toEqual([
    '92', 'UnavailableNo linked speaking measurement.', 'UnavailableThe after measurement is unavailable.',
  ])
})

test('contains no quality judgments, score, arrows, or raw measurement identities', () => {
  const { container } = render(<AttemptComparison comparison={comparison()} />)
  expect(container.textContent).not.toMatch(/\b(improved|better|worse|strong|weak|good|bad|score)\b/i)
  expect(container.textContent).not.toMatch(/[↑↓↗↘]/)
  expect(container.textContent).not.toContain('measurement-id')
  expect(container.textContent).not.toContain('speaking-metrics-v1')
  expect(container.querySelectorAll('[style]')).toHaveLength(0)
})

test('renders a separate factual delivery Before/After/Change table with backend deltas and display-only rounding', () => {
  const data = comparison()
  const delivery = recordedDelivery()
  const original = structuredClone(delivery)
  data.delivery_comparison = delivery
  render(<AttemptComparison comparison={data} />)
  const table = screen.getByRole('table', { name: 'Timed pauses: durations are shown in seconds.' })
  expect(within(table).getAllByRole('columnheader').map(cell => cell.textContent)).toEqual(['Metric', 'Before', 'After', 'Change'])
  expect(within(table).getAllByRole('rowheader').map(cell => cell.textContent)).toEqual(['Pause count', 'Total pause time', 'Longest pause'])
  expect(values('Pause count')).toEqual(['2', '1', '-1'])
  expect(values('Total pause time')).toEqual(['1.5 s', '0.6 s', '-0.9 s'])
  expect(values('Longest pause')).toEqual(['1.0 s', '0.6 s', '-0.4 s'])
  expect(values('Recognized words')).toEqual(['92', '108', '+16'])
  expect(delivery).toEqual(original)
  expect(screen.getByText(/gaps of at least 0.50 seconds/)).toBeTruthy()
  expect(screen.getByText('These gaps are not necessarily acoustic silence.')).toBeTruthy()
})

test('measured delivery zeros stay numeric and signed tiny duration changes keep their backend sign', () => {
  const data = comparison()
  const delivery = recordedDelivery()
  delivery.pause_count = { ...delivery.pause_count, before: 0, after: 0, delta: 0 }
  delivery.total_pause_duration_seconds = { ...delivery.total_pause_duration_seconds, before: 0, after: 0, delta: 0 }
  delivery.longest_pause_seconds = { ...delivery.longest_pause_seconds, before: 1.14, after: 1.15, delta: 0.01 }
  data.delivery_comparison = delivery
  render(<AttemptComparison comparison={data} />)
  expect(values('Pause count')).toEqual(['0', '0', '0'])
  expect(values('Total pause time')).toEqual(['0.0 s', '0.0 s', '0.0 s'])
  expect(values('Longest pause')).toEqual(['1.1 s', '1.1 s', '+0.0 s'])
})

test('legacy delivery is Not recorded and does not receive numeric zeros or fabricated provenance', () => {
  render(<AttemptComparison comparison={comparison()} />)
  expect(values('Pause count')).toEqual(['Not recorded', 'Not recorded', 'UnavailableBoth measurements are unavailable.'])
  expect(values('Total pause time')).toEqual(['Not recorded', 'Not recorded', 'UnavailableBoth measurements are unavailable.'])
  expect(screen.queryByText('pause-metrics-v1')).toBeNull()
})

test.each(DELIVERY_TIMING_REASONS)('delivery unavailable (%s) remains factual without erasing speaking comparisons', (reason) => {
  const data = comparison()
  const delivery = recordedDelivery()
  for (const key of ['pause_count', 'total_pause_duration_seconds', 'longest_pause_seconds'] as const) {
    delivery[key] = { ...delivery[key], before: null, before_unavailable_reason: reason,
      delta: null, comparable: false, comparison_unavailable_reason: 'before_unavailable' }
  }
  data.delivery_comparison = delivery
  render(<AttemptComparison comparison={data} />)
  expect(values('Pause count')).toEqual([`Unavailable${deliveryUnavailableText(reason)}`, '1', 'UnavailableThe before measurement is unavailable.'])
  expect(values('Recognized words')).toEqual(['92', '108', '+16'])
  expect(screen.getByRole('region', { name: 'Timed pauses comparison' }).textContent).not.toContain(reason)
})

test.each([
  ['measurement_version_mismatch', 'Measurement versions differ.'],
  ['measurement_source_incompatible', 'Measurement sources are incompatible.'],
] as const)('independent delivery %s suppresses only delivery deltas', (reason, message) => {
  const data = comparison()
  const delivery = recordedDelivery()
  if (reason === 'measurement_version_mismatch') delivery.after_version = 'pause-metrics-v2'
  else delivery.after_source = 'other_source'
  for (const key of ['pause_count', 'total_pause_duration_seconds', 'longest_pause_seconds'] as const) {
    delivery[key] = { ...delivery[key], delta: null, comparable: false, comparison_unavailable_reason: reason }
  }
  data.delivery_comparison = delivery
  render(<AttemptComparison comparison={data} />)
  expect(values('Pause count')).toEqual(['2', '1', `Unavailable${message}`])
  expect(values('Recognized words')).toEqual(['92', '108', '+16'])
  expect(within(row('Pause count')).queryByText('-1')).toBeNull()
})

test('independent available delivery survives unavailable speaking comparisons without a quality interpretation', () => {
  const data = comparison()
  for (const key of Object.keys(data.comparison!) as (keyof ComparisonMetrics)[]) {
    data.comparison![key] = { ...data.comparison![key], delta: null, comparable: false,
      comparison_unavailable_reason: 'measurement_version_mismatch' }
  }
  data.delivery_comparison = recordedDelivery()
  const { container } = render(<AttemptComparison comparison={data} />)
  expect(values('Pause count')).toEqual(['2', '1', '-1'])
  expect(values('Recognized words')).toEqual(['92', '108', 'UnavailableMeasurement versions differ.'])
  expect(container.textContent).not.toMatch(/\b(improved|better|worse|score|quality|confidence|fluency|ideal)\b/i)
})
