export type DeliveryTimingReason = 'missing_timings' | 'timing_coverage_mismatch' | 'invalid_timing' | 'invalid_timing_order' | 'unusable_span'

export interface DeliveryMetrics {
  version: string
  source: string
  pause_count: number | null
  total_pause_duration_seconds: number | null
  longest_pause_seconds: number | null
  unavailable_reason: DeliveryTimingReason | null
}

export const DELIVERY_TIMING_REASONS: readonly DeliveryTimingReason[] = [
  'missing_timings', 'timing_coverage_mismatch', 'invalid_timing', 'invalid_timing_order', 'unusable_span',
]
export const TIMED_PAUSES_EXPLANATION = 'Timed pauses are gaps of at least 0.50 seconds between consecutive recognized words in the original transcription.'
export const TIMED_PAUSES_LIMITATION = 'These gaps are not necessarily acoustic silence.'

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}
function nonnegative(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
}

export function validDeliveryMetrics(value: unknown): value is DeliveryMetrics {
  if (!record(value)) return false
  const keys = ['version', 'source', 'pause_count', 'total_pause_duration_seconds', 'longest_pause_seconds', 'unavailable_reason']
  if (Object.keys(value).length !== keys.length || !keys.every((key) => Object.hasOwn(value, key)) ||
      typeof value.version !== 'string' || !value.version.trim() || typeof value.source !== 'string' || !value.source.trim()) return false
  if (value.unavailable_reason !== null) return DELIVERY_TIMING_REASONS.includes(value.unavailable_reason as DeliveryTimingReason) &&
    value.pause_count === null && value.total_pause_duration_seconds === null && value.longest_pause_seconds === null
  if (!nonnegative(value.pause_count) || !Number.isSafeInteger(value.pause_count) ||
      !nonnegative(value.total_pause_duration_seconds) || !nonnegative(value.longest_pause_seconds)) return false
  return value.pause_count === 0
    ? value.total_pause_duration_seconds === 0 && value.longest_pause_seconds === 0
    : value.total_pause_duration_seconds > 0 && value.longest_pause_seconds > 0 && value.longest_pause_seconds <= value.total_pause_duration_seconds
}

export function validLiveDeliveryMetrics(value: unknown): value is DeliveryMetrics {
  return validDeliveryMetrics(value) && value.version === 'pause-metrics-v1' && value.source === 'original_transcription'
}

export function deliveryUnavailableText(reason: DeliveryTimingReason): string {
  const messages: Record<DeliveryTimingReason, string> = {
    missing_timings: 'Timing information was not available.',
    timing_coverage_mismatch: 'Word timing did not cover the recognized text.',
    invalid_timing: 'Word timing was invalid.',
    invalid_timing_order: 'Word timing order was invalid.',
    unusable_span: 'Timed span was not usable.',
  }
  return messages[reason]
}

export function formatDeliveryDuration(value: number): string {
  return `${value.toFixed(1)} s`
}
