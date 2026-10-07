import { expect, test } from 'vitest'
import type { HistoryFinalizedPoint, HistoryMeasurement, HistorySummary } from './historyApi'
import {
  compareProgressPoints, describeUnavailableReason, finalizedProgressPoints, formatProgressValue,
  progressOverview, projectProgress,
} from './progress'
import type { ProgressMetricId, ProgressPoint } from './progress'
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
  return {
    question_index: 0, attempt_id: FIRST, attempt_number: 2, submitted_at: '2026-10-05T12:00:00Z',
    measurement: measurement(), ...changes,
  }
}
function summary(changes: Partial<HistorySummary> = {}): HistorySummary {
  return {
    session_id: FIRST, scenario_type: 'job_interview', question_engine: 'deterministic-v1', status: 'active', created_at: '2026-10-05T10:00:00Z', completed_at: null,
    current_question_number: 2, total_questions: 5, finalized_question_count: 1,
    questions_practiced_count: 2, total_attempt_count: 4, total_retry_count: 2,
    measured_final_answer_count: 1, last_submitted_at: '2026-10-05T12:00:00Z',
    last_saved_activity_at: '2026-10-05T12:00:00Z', finalized_points: [point()], ...changes,
  }
}
function metric(id: ProgressMetricId, points: HistoryFinalizedPoint[] = [point()]) {
  return projectProgress([summary({ finalized_points: points })]).groups[0].metrics.find((item) => item.id === id)!
}
function sortable(changes: Partial<ProgressPoint> = {}): ProgressPoint {
  return { ...point(), session_id: FIRST, session_status: 'active', ...changes }
}

test('empty summaries produce zero for all six objective counts and no points or groups', () => {
  expect(projectProgress([])).toEqual({
    overview: { completedSessions: 0, activeSessions: 0, finalizedQuestions: 0, savedAttempts: 0, savedRetries: 0, measuredFinalAnswers: 0 },
    points: [], groups: [], deliveryGroups: [],
  })
})
test('active summary includes objective persisted totals', () => {
  expect(progressOverview([summary()])).toEqual({
    completedSessions: 0, activeSessions: 1, finalizedQuestions: 1, savedAttempts: 4, savedRetries: 2, measuredFinalAnswers: 1,
  })
})
test('completed summary is counted without reconstructing totals from metric rows', () => {
  expect(progressOverview([summary({ status: 'completed', finalized_question_count: 5, total_attempt_count: 8, total_retry_count: 3, measured_final_answer_count: 4 })])).toEqual({
    completedSessions: 1, activeSessions: 0, finalizedQuestions: 5, savedAttempts: 8, savedRetries: 3, measuredFinalAnswers: 4,
  })
})
test.each([
  ['completedSessions', 1], ['activeSessions', 1], ['finalizedQuestions', 6],
  ['savedAttempts', 12], ['savedRetries', 5], ['measuredFinalAnswers', 5],
] as const)('multiple summaries preserve summed %s', (field, total) => {
  const projected = progressOverview([summary(), summary({ status: 'completed', finalized_question_count: 5, total_attempt_count: 8, total_retry_count: 3, measured_final_answer_count: 4 })])
  expect(projected[field]).toBe(total)
})

test('final points flatten with owning session identity and status', () => {
  expect(finalizedProgressPoints([summary(), summary({ session_id: SECOND, status: 'completed' })])).toEqual([
    { ...point(), session_id: FIRST, session_status: 'active' }, { ...point(), session_id: SECOND, session_status: 'completed' },
  ])
})
test('only finalized_points are read, so superseded and open attempts cannot enter metrics', () => {
  const saved = summary({ total_attempt_count: 100, total_retry_count: 98 })
  Object.assign(saved, { attempts: [point({ attempt_id: SECOND, attempt_number: 1 })], current_attempt: point({ question_index: 1 }) })
  const result = projectProgress([saved])
  expect(result.points).toHaveLength(1)
  expect(result.points[0].attempt_number).toBe(2)
  expect(result.points.some((item) => item.attempt_id === SECOND)).toBe(false)
  expect(result.points.some((item) => item.question_index === 1)).toBe(false)
})
test('earlier finalized questions of an active session remain visible', () => {
  const result = projectProgress([summary({ current_question_number: 3, finalized_points: [point(), point({ question_index: 1 })] })])
  expect(result.points.map((item) => item.question_index)).toEqual([0, 1])
  expect(result.points.every((item) => item.session_status === 'active')).toBe(true)
})
test('typed final retains its identity and no inherited metric from an earlier measured retry', () => {
  const result = projectProgress([summary({ finalized_points: [point({ measurement: null })], measured_final_answer_count: 0 })])
  expect(result.points[0].measurement).toBeNull()
  expect(result.groups[0].kind).toBe('no_measurement')
  expect(result.groups[0].metrics.every((item) => item.rows[0].value === null && item.rows[0].unavailableReason === 'no_measurement')).toBe(true)
  expect(result.overview.finalizedQuestions).toBe(1)
  expect(result.overview.measuredFinalAnswers).toBe(0)
})
test('measured final retained with unmodified provenance and scalar values', () => {
  const original = measurement()
  const projected = projectProgress([summary({ finalized_points: [point({ measurement: original })] })])
  expect(projected.points[0].measurement).toBe(original)
  expect(projected.groups[0].kind).toBe('measurement')
})

test('chronology is ascending rather than loaded-summary order', () => {
  const older = summary({ session_id: SECOND, finalized_points: [point({ submitted_at: '2026-10-04T12:00:00Z' })] })
  const newer = summary()
  expect(projectProgress([newer, older]).points.map((item) => item.session_id)).toEqual([SECOND, FIRST])
  expect(projectProgress([older, newer])).toEqual(projectProgress([newer, older]))
})
test.each([
  ['session ID', { session_id: SECOND }, { session_id: FIRST }],
  ['question index', { question_index: 3 }, { question_index: 1 }],
  ['attempt number', { attempt_number: 5 }, { attempt_number: 2 }],
] as const)('equal timestamps tie-break by %s', (_, later, earlier) => {
  const points = [sortable(later), sortable(earlier)].sort(compareProgressPoints)
  expect(points).toEqual([sortable(earlier), sortable(later)])
})
test('timestamp ordering retains PostgreSQL microseconds', () => {
  const later = sortable({ submitted_at: '2026-10-05T12:00:00.000009Z', session_id: FIRST })
  const earlier = sortable({ submitted_at: '2026-10-05T12:00:00.000001Z', session_id: SECOND })
  expect([later, earlier].sort(compareProgressPoints)).toEqual([earlier, later])
})
test('equivalent timezone spellings tie-break by session identity', () => {
  const later = sortable({ submitted_at: '2026-10-05T13:00:00.123456+01:00', session_id: SECOND })
  const earlier = sortable({ submitted_at: '2026-10-05T12:00:00.123456Z' })
  expect([later, earlier].sort(compareProgressPoints)).toEqual([earlier, later])
})

test('same exact version/source tuple forms one group', () => {
  const result = projectProgress([summary({ finalized_points: [point(), point({ question_index: 1 })] })])
  expect(result.groups).toHaveLength(1)
  expect(result.groups[0].points).toHaveLength(2)
  expect(result.groups[0].metrics).toHaveLength(5)
})
test('different measurement versions form separate cohorts', () => {
  const result = projectProgress([summary({ finalized_points: [
    point(), point({ measurement: measurement({ measurement_version: 'speaking_metrics_v2' }) }),
  ] })])
  expect(result.groups).toHaveLength(2)
  expect(result.groups.map((group) => group.kind === 'measurement' && group.measurementVersion)).toEqual(['speaking_metrics_v1', 'speaking_metrics_v2'])
})
test('unknown future measurement source stays in a distinct exact cohort', () => {
  const result = projectProgress([summary({ finalized_points: [
    point(), point({ measurement: measurement({ measurement_source: 'future_source' as HistoryMeasurement['measurement_source'] }) }),
  ] })])
  expect(result.groups).toHaveLength(2)
  expect(result.groups.map((group) => group.kind === 'measurement' && group.measurementSource)).toEqual(['future_source', 'original_transcription'])
})
test('tuple keys cannot collide through separators embedded in future provenance', () => {
  const result = projectProgress([summary({ finalized_points: [
    point({ measurement: measurement({ measurement_version: 'a:b', measurement_source: 'c' as HistoryMeasurement['measurement_source'] }) }),
    point({ measurement: measurement({ measurement_version: 'a', measurement_source: 'b:c' as HistoryMeasurement['measurement_source'] }) }),
  ] })])
  expect(result.groups).toHaveLength(2)
})
test('no-measurement rows stay outside provenance and are not silently discarded', () => {
  const result = projectProgress([summary({ finalized_points: [point({ measurement: null }), point()] })])
  expect(result.groups.map((group) => group.kind)).toEqual(['measurement', 'no_measurement'])
  expect(result.groups[1]).not.toHaveProperty('measurementVersion')
  expect(result.groups[1].metrics[0].coverage).toEqual({ available: 0, total: 1 })
})

test.each(['um_count', 'uh_count', 'recognized_word_count'] as const)('%s measured zero remains available numeric zero', (id) => {
  const projected = metric(id, [point({ measurement: measurement({ [id]: 0 }) })])
  expect(projected.rows[0].value).toBe(0)
  expect(projected.coverage).toEqual({ available: 1, total: 1 })
})
test.each(['um_count', 'uh_count'] as const)('%s unavailable language does not become zero', (id) => {
  const projected = metric(id, [point({ measurement: measurement({ um_count: null, uh_count: null, filler_unavailable_reason: 'unsupported_language' }) })])
  expect(projected.rows[0].value).toBeNull()
  expect(projected.rows[0].unavailableReason).toBe('unsupported_language')
  expect(projected.coverage).toEqual({ available: 0, total: 1 })
})
test.each([
  ['estimated_words_per_minute', 49.491231198], ['timed_utterance_span_seconds', 12.123456789],
] as const)('%s exact persisted floating point remains unrounded', (id, value) => {
  expect(metric(id).rows[0].value).toBe(value)
})
test.each(['estimated_words_per_minute', 'timed_utterance_span_seconds'] as const)('%s retains timing availability cause', (id) => {
  const projected = metric(id, [point({ measurement: measurement({ timed_utterance_span_seconds: null, estimated_words_per_minute: null, timing_unavailable_reason: 'missing_timings' }) })])
  expect(projected.rows[0].value).toBeNull()
  expect(projected.rows[0].unavailableReason).toBe('missing_timings')
})
test('recognized words remain available when fillers and timings are unavailable', () => {
  const original = measurement({ um_count: null, uh_count: null, filler_unavailable_reason: 'unsupported_language', timed_utterance_span_seconds: null, estimated_words_per_minute: null, timing_unavailable_reason: 'missing_timings' })
  const result = projectProgress([summary({ finalized_points: [point({ measurement: original })] })])
  expect(result.groups[0].metrics.find((item) => item.id === 'recognized_word_count')?.coverage).toEqual({ available: 1, total: 1 })
})
test('coverage includes unavailable cohort members in denominator', () => {
  const projected = metric('um_count', [point(), point({ measurement: measurement({ um_count: null, uh_count: null, filler_unavailable_reason: 'unsupported_language' }) })])
  expect(projected.coverage).toEqual({ available: 1, total: 2 })
  expect(projected.rows).toHaveLength(2)
})
test('coverage never combines versions or invents typed provenance', () => {
  const result = projectProgress([summary({ finalized_points: [point(), point({ measurement: measurement({ measurement_version: 'speaking_metrics_v2' }) }), point({ measurement: null })] })])
  expect(result.groups.map((group) => group.metrics[0].coverage)).toEqual([{ available: 1, total: 1 }, { available: 1, total: 1 }, { available: 0, total: 1 }])
})
test.each([
  ['no_measurement', 'No measurement'], ['unsupported_language', 'Unsupported language'],
  ['missing_timings', 'Missing timings'], ['timing_coverage_mismatch', 'Timing coverage mismatch'],
  ['invalid_timing', 'Invalid timing'], ['invalid_timing_order', 'Invalid timing order'], ['unusable_span', 'Unusable span'],
])('unavailable reason %s maps to factual text', (reason, text) => {
  expect(describeUnavailableReason(reason)).toBe(text)
})
test.each([null, 'future_reason', '__proto__', 'constructor'])('unknown reason %s maps to generic unavailable', (reason) => {
  expect(describeUnavailableReason(reason)).toBe('Unavailable')
})
test('future unavailable reason keeps numeric values unavailable defensively', () => {
  const projected = metric('um_count', [point({ measurement: measurement({ filler_unavailable_reason: 'future_reason' as HistoryMeasurement['filler_unavailable_reason'] }) })])
  expect(projected.rows[0]).toMatchObject({ value: null, unavailableReason: 'future_reason' })
  expect(projected.coverage.available).toBe(0)
})
test('display rounding does not mutate exact metric value', () => {
  const projected = metric('estimated_words_per_minute')
  expect(formatProgressValue(projected.rows[0].value!, projected.id)).toBe('49.5')
  expect(projected.rows[0].value).toBe(49.491231198)
  expect(formatProgressValue(12.123456789, 'timed_utterance_span_seconds')).toBe('12.1')
  expect(formatProgressValue(0, 'um_count')).toBe('0')
})
test('projection does not mutate source summaries, point arrays, measurement, or counts', () => {
  const original = summary({ finalized_points: [point({ question_index: 1 }), point()] })
  const before = JSON.stringify(original)
  Object.freeze(original.finalized_points)
  for (const item of original.finalized_points) { Object.freeze(item.measurement); Object.freeze(item) }
  Object.freeze(original)
  projectProgress(Object.freeze([original]))
  expect(JSON.stringify(original)).toBe(before)
})

function delivery(changes: Partial<DeliveryMetrics> = {}): DeliveryMetrics {
  return { version: 'pause-metrics-v1', source: 'original_transcription', pause_count: 2,
    total_pause_duration_seconds: 1.234567890123, longest_pause_seconds: 0.765432109876,
    unavailable_reason: null, ...changes }
}
function deliveryPoint(delivery_metrics: DeliveryMetrics | null = delivery(), changes: Partial<HistoryFinalizedPoint> = {}): HistoryFinalizedPoint {
  return point({ measurement: measurement({ delivery_metrics }), ...changes })
}

test('recorded pause facts create three separate delivery metrics without changing speaking groups or overview semantics', () => {
  const source = summary({ finalized_points: [deliveryPoint()] })
  const projected = projectProgress([source])
  expect(projected.overview).toEqual(progressOverview([source]))
  expect(projected.groups).toHaveLength(1)
  expect(projected.groups[0].metrics).toHaveLength(5)
  expect(projected.deliveryGroups).toHaveLength(1)
  expect(projected.deliveryGroups[0].metrics.map((item) => item.id)).toEqual(['pause_count', 'total_pause_duration_seconds', 'longest_pause_seconds'])
  expect(projected.deliveryGroups[0].deliveryVersion).toBe('pause-metrics-v1')
  expect(projected.deliveryGroups[0].measurementSource).toBe('original_transcription')
})

test('same delivery provenance forms one cohort independently of differing speaking versions', () => {
  const points = [deliveryPoint(), deliveryPoint(delivery(), { measurement: measurement({
    measurement_version: 'speaking-metrics-v2', delivery_metrics: delivery(),
  }) })]
  const projected = projectProgress([summary({ finalized_points: points })])
  expect(projected.groups).toHaveLength(2)
  expect(projected.deliveryGroups).toHaveLength(1)
  expect(projected.deliveryGroups[0].points).toHaveLength(2)
})

test('different delivery versions form independent cohorts within the same speaking version', () => {
  const projected = projectProgress([summary({ finalized_points: [deliveryPoint(), deliveryPoint(delivery({ version: 'pause-metrics-v2' }))] })])
  expect(projected.groups).toHaveLength(1)
  expect(projected.deliveryGroups.map((group) => group.deliveryVersion)).toEqual(['pause-metrics-v1', 'pause-metrics-v2'])
  expect(projected.deliveryGroups.every((group) => group.metrics.every((item) => item.coverage.total === 1))).toBe(true)
})

test('pure delivery projection separates future source tuples without normalizing them', () => {
  const projected = projectProgress([summary({ finalized_points: [
    deliveryPoint(), deliveryPoint(delivery({ source: 'future_source' }), {
      measurement: measurement({ measurement_source: 'future_source' as HistoryMeasurement['measurement_source'], delivery_metrics: delivery({ source: 'future_source' }) }),
    }),
  ] })])
  expect(projected.deliveryGroups.map((group) => group.measurementSource)).toEqual(['future_source', 'original_transcription'])
})

test('delivery tuple grouping does not collide through embedded separators', () => {
  const projected = projectProgress([summary({ finalized_points: [
    deliveryPoint(delivery({ version: 'a:b', source: 'c' })), deliveryPoint(delivery({ version: 'a', source: 'b:c' })),
  ] })])
  expect(projected.deliveryGroups).toHaveLength(2)
})

test('legacy delivery absence stays outside recorded cohorts without removing its speaking facts', () => {
  const projected = projectProgress([summary({ finalized_points: [deliveryPoint(null), deliveryPoint()] })])
  expect(projected.points).toHaveLength(2)
  expect(projected.groups[0].points).toHaveLength(2)
  expect(projected.deliveryGroups[0].points).toHaveLength(1)
  expect(projected.deliveryGroups[0].metrics[0].coverage).toEqual({ available: 1, total: 1 })
  expect(projected.points[0].measurement!.delivery_metrics).toBeNull()
})

test('typed finals remain no measurement instead of masquerading as historical delivery absence', () => {
  const projected = projectProgress([summary({ finalized_points: [point({ measurement: null }), deliveryPoint(null)], measured_final_answer_count: 1 })])
  expect(projected.deliveryGroups).toEqual([])
  expect(projected.groups.map((group) => group.kind)).toEqual(['measurement', 'no_measurement'])
  expect(projected.overview.measuredFinalAnswers).toBe(1)
})

test.each(['pause_count', 'total_pause_duration_seconds', 'longest_pause_seconds'] as const)(
  'zero %s remains available numeric zero in its delivery cohort', (id) => {
    const projected = projectProgress([summary({ finalized_points: [deliveryPoint(delivery({ pause_count: 0, total_pause_duration_seconds: 0, longest_pause_seconds: 0 }))] })])
    const metric = projected.deliveryGroups[0].metrics.find((item) => item.id === id)!
    expect(metric.rows[0].value).toBe(0)
    expect(metric.rows[0].unavailableReason).toBeNull()
    expect(metric.coverage).toEqual({ available: 1, total: 1 })
  },
)

test.each(['missing_timings', 'timing_coverage_mismatch', 'invalid_timing', 'invalid_timing_order', 'unusable_span'] as const)(
  'recorded delivery %s contributes three nulls and denominator coverage', (unavailable_reason) => {
    const unavailable = delivery({ pause_count: null, total_pause_duration_seconds: null, longest_pause_seconds: null, unavailable_reason })
    const projected = projectProgress([summary({ finalized_points: [deliveryPoint(), deliveryPoint(unavailable)] })])
    for (const metric of projected.deliveryGroups[0].metrics) {
      expect(metric.rows[1].value).toBeNull()
      expect(metric.rows[1].unavailableReason).toBe(unavailable_reason)
      expect(metric.coverage).toEqual({ available: 1, total: 2 })
    }
  },
)

test('all unavailable delivery points still create a cohort with zero available values', () => {
  const unavailable = delivery({ pause_count: null, total_pause_duration_seconds: null, longest_pause_seconds: null, unavailable_reason: 'missing_timings' })
  const projected = projectProgress([summary({ finalized_points: [deliveryPoint(unavailable), deliveryPoint(unavailable)] })])
  expect(projected.deliveryGroups[0].metrics.every((metric) => metric.coverage.available === 0 && metric.coverage.total === 2)).toBe(true)
})

test.each([
  ['total_pause_duration_seconds', 1.234567890123], ['longest_pause_seconds', 0.765432109876],
] as const)('delivery %s preserves exact stored float without recomputation or rounding', (id, value) => {
  const projected = projectProgress([summary({ finalized_points: [deliveryPoint()] })])
  expect(projected.deliveryGroups[0].metrics.find((metric) => metric.id === id)!.rows[0].value).toBe(value)
})

test('delivery metric chronology uses persisted timestamps and all existing tie breakers', () => {
  const points = [
    deliveryPoint(delivery(), { submitted_at: '2026-10-05T12:00:00.000009Z', question_index: 3, attempt_number: 2 }),
    deliveryPoint(delivery(), { submitted_at: '2026-10-05T12:00:00.000001Z', question_index: 2, attempt_number: 4 }),
    deliveryPoint(delivery(), { submitted_at: '2026-10-05T12:00:00.000001Z', question_index: 2, attempt_number: 1 }),
    deliveryPoint(delivery(), { submitted_at: '2026-10-05T12:00:00.000001Z', question_index: 1, attempt_number: 3 }),
  ]
  const second = summary({ session_id: SECOND, finalized_points: [deliveryPoint(delivery(), { submitted_at: '2026-10-05T12:00:00.000001Z' })] })
  const projected = projectProgress([second, summary({ finalized_points: points })])
  const rows = projected.deliveryGroups[0].metrics[0].rows.map((row) => row.point)
  expect(rows.map((item) => [item.session_id, item.question_index, item.attempt_number])).toEqual([
    [FIRST, 1, 3], [FIRST, 2, 1], [FIRST, 2, 4], [SECOND, 0, 2], [FIRST, 3, 2],
  ])
})

test('only finalized-point delivery enters the projection, even when the active question has later measured retries', () => {
  const source = summary({ current_question_number: 2, finalized_points: [deliveryPoint()], total_attempt_count: 10, total_retry_count: 8 })
  Object.assign(source, { current_attempt: deliveryPoint(delivery(), { question_index: 1 }), superseded_attempt: deliveryPoint(delivery(), { attempt_number: 1 }) })
  const projected = projectProgress([source])
  expect(projected.deliveryGroups[0].points).toHaveLength(1)
  expect(projected.deliveryGroups[0].points[0].question_index).toBe(0)
  expect(projected.deliveryGroups[0].points[0].attempt_number).toBe(2)
  expect(projected.deliveryGroups[0].points[0].session_status).toBe('active')
})

test('delivery availability does not reinterpret measured_final_answer_count', () => {
  const unavailable = delivery({ pause_count: null, total_pause_duration_seconds: null, longest_pause_seconds: null, unavailable_reason: 'missing_timings' })
  const projected = projectProgress([summary({ measured_final_answer_count: 3,
    finalized_points: [deliveryPoint(), deliveryPoint(null), deliveryPoint(unavailable)] })])
  expect(projected.overview.measuredFinalAnswers).toBe(3)
  expect(projected.deliveryGroups[0].metrics[0].coverage).toEqual({ available: 1, total: 2 })
})

test('delivery projection leaves frozen source snapshots and precision unchanged', () => {
  const recorded = Object.freeze(delivery())
  const measured = Object.freeze(measurement({ delivery_metrics: recorded }))
  const finalized = Object.freeze(point({ measurement: measured }))
  const source = Object.freeze(summary({ finalized_points: [finalized] }))
  Object.freeze(source.finalized_points)
  const before = JSON.stringify(source)
  projectProgress([source])
  expect(JSON.stringify(source)).toBe(before)
})


test('mixed adaptive and deterministic sessions contribute only their persisted finalized facts', () => {
  const deterministic = summary()
  const adaptive = summary({ session_id: SECOND, question_engine: 'live-ai-roleplay-v1',
    finalized_points: [point({ attempt_id: SECOND, measurement: null })], measured_final_answer_count: 0 })
  const result = projectProgress([deterministic, adaptive])
  expect(result.overview.finalizedQuestions).toBe(2)
  expect(result.overview.savedAttempts).toBe(8)
  expect(result.points.map((item) => item.attempt_id)).toEqual([FIRST, SECOND])
  expect(result.groups.find((group) => group.kind === 'no_measurement')?.points).toHaveLength(1)
})
