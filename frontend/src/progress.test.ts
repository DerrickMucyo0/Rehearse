import { expect, test } from 'vitest'
import type { HistoryFinalizedPoint, HistoryMeasurement, HistorySummary } from './historyApi'
import {
  compareProgressPoints, describeUnavailableReason, finalizedProgressPoints, formatProgressValue,
  progressOverview, projectProgress,
} from './progress'
import type { ProgressMetricId, ProgressPoint } from './progress'

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
  return {
    question_index: 0, attempt_id: FIRST, attempt_number: 2, submitted_at: '2026-10-05T12:00:00Z',
    measurement: measurement(), ...changes,
  }
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
function metric(id: ProgressMetricId, points: HistoryFinalizedPoint[] = [point()]) {
  return projectProgress([summary({ finalized_points: points })]).groups[0].metrics.find((item) => item.id === id)!
}
function sortable(changes: Partial<ProgressPoint> = {}): ProgressPoint {
  return { ...point(), session_id: FIRST, session_status: 'active', ...changes }
}

test('empty summaries produce zero for all six objective counts and no points or groups', () => {
  expect(projectProgress([])).toEqual({
    overview: { completedSessions: 0, activeSessions: 0, finalizedQuestions: 0, savedAttempts: 0, savedRetries: 0, measuredFinalAnswers: 0 },
    points: [], groups: [],
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
