// @vitest-environment jsdom
import { cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, expect, test } from 'vitest'
import AttemptComparison from './AttemptComparison'
import type { AttemptComparison as Comparison, ComparisonMetrics, MetricChange } from './interviewApi'

afterEach(cleanup)

function metric(before: number, after: number): MetricChange {
  return {
    before, after, delta: after - before, before_unavailable_reason: null,
    after_unavailable_reason: null, comparable: true, comparison_unavailable_reason: null,
  }
}

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
  expect(screen.getAllByRole('columnheader').map(cell => cell.textContent)).toEqual(['Metric', 'Before', 'After', 'Change'])
  expect(screen.getAllByRole('rowheader').map(cell => cell.textContent)).toEqual([
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
  render(<AttemptComparison comparison={data} />)
  expect(screen.getByRole('table')).toBeTruthy()
  for (const cell of screen.getAllByRole('cell')) {
    expect(cell.textContent?.startsWith('Unavailable')).toBe(true)
    expect(cell.textContent).not.toMatch(/^0$/)
  }
  expect(screen.getAllByText('No linked speaking measurement.')).toHaveLength(10)
  expect(screen.getAllByText('Both measurements are unavailable.')).toHaveLength(5)
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
