// @vitest-environment jsdom
import { cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, expect, test } from 'vitest'
import DeliveryFacts from './DeliveryFacts'
import { DELIVERY_TIMING_REASONS, deliveryUnavailableText, TIMED_PAUSES_EXPLANATION, TIMED_PAUSES_LIMITATION } from './deliveryMetrics'
import type { DeliveryMetrics } from './deliveryMetrics'

afterEach(cleanup)
const zero: DeliveryMetrics = { version: 'pause-metrics-v1', source: 'original_transcription', pause_count: 0,
  total_pause_duration_seconds: 0, longest_pause_seconds: 0, unavailable_reason: null }
function value(label: string) {
  return within(screen.getByRole('region', { name: 'Timed pauses' })).getByText(label, { selector: 'dt', exact: true }).nextElementSibling?.textContent
}

test('measured zero is visible as zero for every delivery fact, with original timing explanation', () => {
  render(<DeliveryFacts metrics={zero} />)
  expect(value('Pause count')).toBe('0')
  expect(value('Total pause time')).toBe('0.0 s')
  expect(value('Longest pause')).toBe('0.0 s')
  expect(screen.getByText(TIMED_PAUSES_EXPLANATION)).toBeTruthy()
  expect(screen.getByText(TIMED_PAUSES_LIMITATION)).toBeTruthy()
  expect(screen.getByText('Source: Original transcription')).toBeTruthy()
})

test('positive facts round only for display without modifying the supplied snapshot or browser storage', () => {
  const metrics = { ...zero, pause_count: 2, total_pause_duration_seconds: 1.34567891, longest_pause_seconds: 0.84567891 }
  const original = structuredClone(metrics)
  const before = Object.entries(localStorage)
  const { container } = render(<DeliveryFacts metrics={metrics} />)
  expect(value('Pause count')).toBe('2')
  expect(value('Total pause time')).toBe('1.3 s')
  expect(value('Longest pause')).toBe('0.8 s')
  expect(metrics).toEqual(original)
  expect(Object.entries(localStorage)).toEqual(before)
  expect(container.textContent).not.toMatch(/\b(better|worse|improved|quality|score|confidence|fluency|ideal)\b/i)
})

test('legacy delivery is Not recorded rather than a fabricated version, measured zero or unavailable analysis', () => {
  const { container } = render(<DeliveryFacts metrics={null} />)
  expect(value('Pause count')).toBe('Not recorded')
  expect(value('Total pause time')).toBe('Not recorded')
  expect(value('Longest pause')).toBe('Not recorded')
  expect(container.textContent).not.toMatch(/Unavailable|pause-metrics-v1/)
})

test.each(DELIVERY_TIMING_REASONS)('measured unavailable %s uses factual mapped text and no internal code or zero', (reason) => {
  const metrics = { ...zero, pause_count: null, total_pause_duration_seconds: null, longest_pause_seconds: null, unavailable_reason: reason }
  const { container } = render(<DeliveryFacts metrics={metrics} />)
  expect(value('Pause count')).toBe('Unavailable')
  expect(value('Total pause time')).toBe('Unavailable')
  expect(value('Longest pause')).toBe('Unavailable')
  expect(screen.getByText(`Unavailable — ${deliveryUnavailableText(reason)}`)).toBeTruthy()
  expect(container.textContent).not.toContain(reason)
  expect(container.textContent).not.toMatch(/Not recorded|0.0 s/)
})

test('selected-attempt use may keep the surrounding heading hierarchy', () => {
  render(<DeliveryFacts metrics={zero} headingLevel={6} />)
  expect(screen.getByRole('heading', { name: 'Timed pauses', level: 6 })).toBeTruthy()
})
