import { expect, test } from 'vitest'
import { DELIVERY_TIMING_REASONS, formatDeliveryDuration, validDeliveryMetrics, validLiveDeliveryMetrics } from './deliveryMetrics'

const zero = { version: 'pause-metrics-v1', source: 'original_transcription', pause_count: 0,
  total_pause_duration_seconds: 0, longest_pause_seconds: 0, unavailable_reason: null }
const positive = { ...zero, pause_count: 2, total_pause_duration_seconds: 1.34567891, longest_pause_seconds: 0.84567891 }

test('validates all scalar measured zeros and unrounded positive facts without mutation', () => {
  const before = structuredClone(positive)
  expect(validDeliveryMetrics(zero)).toBe(true)
  expect(validDeliveryMetrics(positive)).toBe(true)
  expect(validLiveDeliveryMetrics(positive)).toBe(true)
  expect(positive).toEqual(before)
})

test('history provenance remains versioned independently while live provenance is fixed', () => {
  const historical = { ...positive, version: 'pause-metrics-v2', source: 'another_source' }
  expect(validDeliveryMetrics(historical)).toBe(true)
  expect(validLiveDeliveryMetrics(historical)).toBe(false)
})

test.each(DELIVERY_TIMING_REASONS)('accepts only the fully unavailable scalar state for %s', (reason) => {
  const unavailable = { ...zero, pause_count: null, total_pause_duration_seconds: null, longest_pause_seconds: null, unavailable_reason: reason }
  expect(validDeliveryMetrics(unavailable)).toBe(true)
  expect(validLiveDeliveryMetrics(unavailable)).toBe(true)
})

test.each([
  null, undefined, [], {}, { ...zero, extra: [] }, { ...zero, version: '' }, { ...zero, source: ' ' },
  { ...zero, version: null }, { ...zero, source: 1 }, { ...zero, pause_count: null },
  { ...zero, pause_count: false }, { ...zero, pause_count: '0' }, { ...zero, pause_count: 0.1 },
  { ...zero, pause_count: -1 }, { ...zero, pause_count: Number.MAX_SAFE_INTEGER + 1 },
  { ...zero, total_pause_duration_seconds: -0.5 }, { ...zero, longest_pause_seconds: Infinity },
  { ...zero, total_pause_duration_seconds: NaN }, { ...zero, total_pause_duration_seconds: null },
  { ...zero, pause_count: 1 }, { ...zero, total_pause_duration_seconds: 0.5, longest_pause_seconds: 0.5 },
  { ...positive, longest_pause_seconds: 2 }, { ...positive, longest_pause_seconds: 0 },
  { ...zero, unavailable_reason: 'not_recorded' }, { ...zero, unavailable_reason: 'private_message' },
  { ...zero, unavailable_reason: 'missing_timings' },
])('rejects malformed or mixed delivery states with strict exact fields (case %#)', (value) => {
  expect(validDeliveryMetrics(value)).toBe(false)
  expect(validLiveDeliveryMetrics(value)).toBe(false)
})

test('duration formatting rounds display only, including measured zero', () => {
  const value = 0.5678912345
  expect(formatDeliveryDuration(value)).toBe('0.6 s')
  expect(formatDeliveryDuration(0)).toBe('0.0 s')
  expect(value).toBe(0.5678912345)
})
