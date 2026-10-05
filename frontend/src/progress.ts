import type { HistoryFinalizedPoint, HistoryMeasurement, HistorySummary } from './historyApi'
import type { DeliveryTimingReason } from './deliveryMetrics'

export interface ProgressOverview {
  completedSessions: number
  activeSessions: number
  finalizedQuestions: number
  savedAttempts: number
  savedRetries: number
  measuredFinalAnswers: number
}

export interface ProgressPoint extends HistoryFinalizedPoint {
  session_id: string
  session_status: HistorySummary['status']
}

export const PROGRESS_METRICS = [
  { id: 'estimated_words_per_minute', label: 'Estimated WPM', unit: 'words/minute' },
  { id: 'um_count', label: 'Um count', unit: 'count' },
  { id: 'uh_count', label: 'Uh count', unit: 'count' },
  { id: 'timed_utterance_span_seconds', label: 'Timed speech span', unit: 'seconds' },
  { id: 'recognized_word_count', label: 'Recognized words', unit: 'words' },
] as const

export type ProgressMetricId = typeof PROGRESS_METRICS[number]['id']
export const DELIVERY_PROGRESS_METRICS = [
  { id: 'pause_count', label: 'Pause count', unit: 'count' },
  { id: 'total_pause_duration_seconds', label: 'Total pause time', unit: 'seconds' },
  { id: 'longest_pause_seconds', label: 'Longest pause', unit: 'seconds' },
] as const
export type DeliveryProgressMetricId = typeof DELIVERY_PROGRESS_METRICS[number]['id']
export interface ProgressMetricRow {
  point: ProgressPoint
  value: number | null
  unavailableReason: string | null
}
export interface ProgressMetric {
  id: ProgressMetricId
  label: string
  unit: string
  rows: ProgressMetricRow[]
  coverage: { available: number; total: number }
}
export type ProgressGroup = {
  kind: 'measurement'
  measurementVersion: string
  measurementSource: string
  points: ProgressPoint[]
  metrics: ProgressMetric[]
} | {
  kind: 'no_measurement'
  points: ProgressPoint[]
  metrics: ProgressMetric[]
}
export interface ProgressProjection {
  overview: ProgressOverview
  points: ProgressPoint[]
  groups: ProgressGroup[]
  deliveryGroups: DeliveryProgressGroup[]
}
export interface DeliveryProgressMetric {
  id: DeliveryProgressMetricId
  label: string
  unit: string
  rows: { point: ProgressPoint; value: number | null; unavailableReason: DeliveryTimingReason | null }[]
  coverage: { available: number; total: number }
}
export interface DeliveryProgressGroup {
  deliveryVersion: string
  measurementSource: string
  points: ProgressPoint[]
  metrics: DeliveryProgressMetric[]
}

function compareText(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0
}

export function compareProgressPoints(left: ProgressPoint, right: ProgressPoint): number {
  const milliseconds = Date.parse(left.submitted_at) - Date.parse(right.submitted_at)
  if (milliseconds !== 0) return milliseconds
  // Preserve the sub-millisecond precision of persisted PostgreSQL timestamps.
  const fraction = (value: string) => (/\.(\d+)(?:Z|[+-]\d{2}:\d{2})$/.exec(value)?.[1].slice(3) ?? '').padEnd(9, '0')
  return compareText(fraction(left.submitted_at), fraction(right.submitted_at)) ||
    compareText(left.session_id, right.session_id) ||
    left.question_index - right.question_index ||
    left.attempt_number - right.attempt_number
}

export function progressOverview(summaries: readonly HistorySummary[]): ProgressOverview {
  return summaries.reduce<ProgressOverview>((counts, summary) => ({
    completedSessions: counts.completedSessions + Number(summary.status === 'completed'),
    activeSessions: counts.activeSessions + Number(summary.status === 'active'),
    finalizedQuestions: counts.finalizedQuestions + summary.finalized_question_count,
    savedAttempts: counts.savedAttempts + summary.total_attempt_count,
    savedRetries: counts.savedRetries + summary.total_retry_count,
    measuredFinalAnswers: counts.measuredFinalAnswers + summary.measured_final_answer_count,
  }), {
    completedSessions: 0, activeSessions: 0, finalizedQuestions: 0,
    savedAttempts: 0, savedRetries: 0, measuredFinalAnswers: 0,
  })
}

export function finalizedProgressPoints(summaries: readonly HistorySummary[]): ProgressPoint[] {
  return summaries.flatMap((summary) => summary.finalized_points.map((point) => ({
    ...point, session_id: summary.session_id, session_status: summary.status,
  }))).sort(compareProgressPoints)
}

export function describeUnavailableReason(reason: string | null): string {
  const descriptions: Record<string, string> = {
    no_measurement: 'No measurement',
    unsupported_language: 'Unsupported language',
    missing_timings: 'Missing timings',
    timing_coverage_mismatch: 'Timing coverage mismatch',
    invalid_timing: 'Invalid timing',
    invalid_timing_order: 'Invalid timing order',
    unusable_span: 'Unusable span',
  }
  return reason === null || !Object.hasOwn(descriptions, reason) ? 'Unavailable' : descriptions[reason]
}

function unavailableReason(measurement: HistoryMeasurement, id: ProgressMetricId): string | null {
  if (id === 'um_count' || id === 'uh_count') return measurement.filler_unavailable_reason
  if (id === 'estimated_words_per_minute' || id === 'timed_utterance_span_seconds') return measurement.timing_unavailable_reason
  return null
}

function projectMetric(points: ProgressPoint[], definition: typeof PROGRESS_METRICS[number]): ProgressMetric {
  const rows = points.map((point): ProgressMetricRow => {
    if (point.measurement === null) return { point, value: null, unavailableReason: 'no_measurement' }
    const reason = unavailableReason(point.measurement, definition.id)
    const stored = point.measurement[definition.id]
    // The safe history DTO enforces these pairings; retain unavailable semantics
    // defensively if a later DTO adds another availability reason.
    const value = reason === null && typeof stored === 'number' ? stored : null
    return { point, value, unavailableReason: value === null ? reason : null }
  })
  return {
    ...definition, rows,
    coverage: { available: rows.filter((row) => row.value !== null).length, total: rows.length },
  }
}

function projectDeliveryMetric(points: ProgressPoint[], definition: typeof DELIVERY_PROGRESS_METRICS[number]): DeliveryProgressMetric {
  const rows = points.map((point) => {
    // This cohort contains only recorded delivery snapshots. Legacy and typed
    // points remain outside it, with their existing speaking facts unchanged.
    const delivery = point.measurement!.delivery_metrics!
    return { point, value: delivery[definition.id], unavailableReason: delivery.unavailable_reason }
  })
  return { ...definition, rows,
    coverage: { available: rows.filter((row) => row.value !== null).length, total: rows.length } }
}

export function projectProgress(summaries: readonly HistorySummary[]): ProgressProjection {
  const points = finalizedProgressPoints(summaries)
  const measured = new Map<string, Extract<ProgressGroup, { kind: 'measurement' }>>()
  const noMeasurement: ProgressPoint[] = []
  const deliveryCohorts = new Map<string, DeliveryProgressGroup>()
  for (const point of points) {
    if (point.measurement === null) {
      noMeasurement.push(point)
      continue
    }
    const { measurement_version: measurementVersion, measurement_source: measurementSource } = point.measurement
    const key = JSON.stringify([measurementVersion, measurementSource])
    let group = measured.get(key)
    if (!group) {
      group = { kind: 'measurement', measurementVersion, measurementSource, points: [], metrics: [] }
      measured.set(key, group)
    }
    group.points.push(point)
    const delivery = point.measurement.delivery_metrics
    if (delivery !== null) {
      const deliveryKey = JSON.stringify([delivery.version, delivery.source])
      let deliveryGroup = deliveryCohorts.get(deliveryKey)
      if (!deliveryGroup) {
        deliveryGroup = { deliveryVersion: delivery.version, measurementSource: delivery.source, points: [], metrics: [] }
        deliveryCohorts.set(deliveryKey, deliveryGroup)
      }
      deliveryGroup.points.push(point)
    }
  }
  const groups: ProgressGroup[] = [...measured.values()].sort((left, right) =>
    compareText(left.measurementVersion, right.measurementVersion) || compareText(left.measurementSource, right.measurementSource))
  if (noMeasurement.length > 0) groups.push({ kind: 'no_measurement', points: noMeasurement, metrics: [] })
  for (const group of groups) group.metrics = PROGRESS_METRICS.map((definition) => projectMetric(group.points, definition))
  const deliveryGroups = [...deliveryCohorts.values()].sort((left, right) =>
    compareText(left.deliveryVersion, right.deliveryVersion) || compareText(left.measurementSource, right.measurementSource))
  for (const group of deliveryGroups) group.metrics = DELIVERY_PROGRESS_METRICS.map((definition) => projectDeliveryMetric(group.points, definition))
  return { overview: progressOverview(summaries), points, groups, deliveryGroups }
}

export function formatProgressValue(value: number, id: ProgressMetricId): string {
  return new Intl.NumberFormat('en-US', {
    maximumFractionDigits: id === 'estimated_words_per_minute' || id === 'timed_utterance_span_seconds' ? 1 : 0,
  }).format(value)
}
