import ts from 'typescript'
import { afterEach, expect, test, vi } from 'vitest'
import { getHistoryDetail } from './historyApi'
import type { HistoryDetail, HistoryMeasurement } from './historyApi'
import { buildInterviewSummary } from './interviewSummary'
import summaryModuleSource from './interviewSummary.ts?raw'

const sessionId = '11111111-1111-4111-8111-111111111111'
const createdAt = '2026-10-06T10:00:00.000001Z'
const completedAt = '2026-10-06T10:00:10.000001Z'
const attemptId = (index: number) => `00000000-0000-4000-8000-${String(index + 1).padStart(12, '0')}`
const submittedAt = (index: number) => `2026-10-06T10:00:0${index + 1}.000002Z`
const invalidMessage = 'Completed interview summary is unavailable.'

function completedDetail(counts = [1, 1, 1, 1, 1], numbers = counts): HistoryDetail {
  const totalAttempts = counts.reduce((total, count) => total + count, 0)
  return {
    summary: {
      session_id: sessionId, scenario_type: 'job_interview', question_engine: 'deterministic-v1', status: 'completed', created_at: createdAt, completed_at: completedAt,
      current_question_number: null, total_questions: 5, finalized_question_count: 5, questions_practiced_count: 5,
      total_attempt_count: totalAttempts, total_retry_count: totalAttempts - 5, measured_final_answer_count: 0,
      last_submitted_at: submittedAt(4), last_saved_activity_at: completedAt,
      finalized_points: counts.map((_, question_index) => ({ question_index, attempt_id: attemptId(question_index),
        attempt_number: numbers[question_index], submitted_at: submittedAt(question_index), measurement: null })),
    },
    questions: counts.map((attempt_count, question_index) => ({ question_index, question_text: `Question ${question_index + 1}`,
      finalized: true, attempt_count, latest_attempt_id: attemptId(question_index), latest_attempt_number: numbers[question_index],
      final_attempt_id: attemptId(question_index), final_attempt_number: numbers[question_index] })),
    selected_question: null,
  }
}

function measured(changes: Partial<HistoryMeasurement> = {}): HistoryMeasurement {
  return { measurement_version: 'speaking-metrics-v1', measurement_source: 'original_transcription',
    recognized_word_count: 4, um_count: 0, uh_count: 0, filler_unavailable_reason: null,
    timed_utterance_span_seconds: 1.234567890123, estimated_words_per_minute: 194.40000174967392,
    timing_unavailable_reason: null, delivery_metrics: null, ...changes }
}

function delivery(changes: Partial<NonNullable<HistoryMeasurement['delivery_metrics']>> = {}) {
  return { version: 'pause-metrics-v1', source: 'original_transcription', pause_count: 2,
    total_pause_duration_seconds: 1.234567890789, longest_pause_seconds: 0.734567890789,
    unavailable_reason: null, ...changes } satisfies NonNullable<HistoryMeasurement['delivery_metrics']>
}

function setMeasurement(detail: HistoryDetail, index: number, measurement: HistoryMeasurement | null) {
  detail.summary.finalized_points[index].measurement = measurement
  detail.summary.measured_final_answer_count = detail.summary.finalized_points.filter((point) => point.measurement !== null).length
}

function typedFinalDetail(): HistoryDetail {
  const detail = completedDetail([2, 1, 1, 1, 1])
  detail.selected_question = { question_index: 0, has_more: false, next_after_attempt_number: null, attempts: [
    { attempt_id: attemptId(99), attempt_number: 1, answer_text: 'Earlier recorded answer.',
      submitted_at: '2026-10-06T10:00:00.500001Z', is_final: false, measurement: measured({ delivery_metrics: delivery() }) },
    { attempt_id: attemptId(0), attempt_number: 2, answer_text: 'Final typed answer.',
      submitted_at: submittedAt(0), is_final: true, measurement: null },
  ] }
  return detail
}

const timingReasons = ['missing_timings', 'timing_coverage_mismatch', 'invalid_timing', 'invalid_timing_order', 'unusable_span'] as const
const measurementCases = [
  { name: 'missing final measurement', measurement: null },
  { name: 'legacy delivery not recorded', measurement: measured() },
  { name: 'unsupported-language fillers', measurement: measured({ um_count: null, uh_count: null, filler_unavailable_reason: 'unsupported_language' }) },
  ...timingReasons.map((timing_unavailable_reason) => ({ name: `unavailable timing: ${timing_unavailable_reason}`,
    measurement: measured({ timed_utterance_span_seconds: null, estimated_words_per_minute: null, timing_unavailable_reason }) })),
  ...timingReasons.map((unavailable_reason) => ({ name: `unavailable delivery: ${unavailable_reason}`,
    measurement: measured({ delivery_metrics: delivery({ pause_count: null, total_pause_duration_seconds: null,
      longest_pause_seconds: null, unavailable_reason }) }) })),
  { name: 'available measured zeros', measurement: measured({ delivery_metrics: delivery({ pause_count: 0,
    total_pause_duration_seconds: 0, longest_pause_seconds: 0 }) }) },
  { name: 'zero recognized words with unavailable timings', measurement: measured({ recognized_word_count: 0,
    timed_utterance_span_seconds: null, estimated_words_per_minute: null, timing_unavailable_reason: 'missing_timings' }) },
  { name: 'independent historical provenance', measurement: measured({ measurement_version: 'speaking-historical',
    delivery_metrics: delivery({ version: 'delivery-historical' }) }) },
  { name: 'precise recorded delivery', measurement: measured({ delivery_metrics: delivery() }) },
] satisfies { name: string; measurement: HistoryMeasurement | null }[]

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })

test('completed persisted facts produce the exact minimal summary contract', () => {
  const detail = completedDetail()
  const result = buildInterviewSummary(detail)
  expect(result).toEqual({ summary_version: 'interview-summary-v1', session_id: sessionId, status: 'completed',
    questions_completed: 5, question_count: 5, total_attempts: 5, total_retries: 0,
    questions: Array.from({ length: 5 }, (_, question_index) => ({ question_index,
      question_text: `Question ${question_index + 1}`, final_attempt_number: 1, retry_count: 0, measurement: null })) })
  expect(Object.keys(result).sort()).toEqual(['question_count', 'questions', 'questions_completed', 'session_id',
    'status', 'summary_version', 'total_attempts', 'total_retries'])
  for (const row of result.questions) {
    expect(Object.keys(row).sort()).toEqual(['final_attempt_number', 'measurement', 'question_index', 'question_text', 'retry_count'])
  }
})

test('a valid active session does not receive a completed summary', () => {
  const detail = completedDetail()
  detail.summary = { ...detail.summary, status: 'active', completed_at: null, current_question_number: 1,
    finalized_question_count: 0, questions_practiced_count: 0, total_attempt_count: 0, total_retry_count: 0,
    last_submitted_at: null, last_saved_activity_at: createdAt, finalized_points: [] }
  detail.questions = detail.questions.map((question) => ({ ...question, finalized: false, attempt_count: 0,
    latest_attempt_id: null, latest_attempt_number: null, final_attempt_id: null, final_attempt_number: null }))
  expect(() => buildInterviewSummary(detail)).toThrowError(invalidMessage)
})

test('the submitted fifth question is provisional until persisted Continue completes the session', () => {
  const detail = completedDetail()
  detail.summary.status = 'active'
  detail.summary.completed_at = null
  detail.summary.current_question_number = 5
  detail.summary.finalized_question_count = 4
  detail.summary.finalized_points.pop()
  detail.summary.last_saved_activity_at = submittedAt(4)
  detail.questions[4].finalized = false
  detail.questions[4].final_attempt_id = null
  detail.questions[4].final_attempt_number = null
  expect(() => buildInterviewSummary(detail)).toThrowError(invalidMessage)
})

test.each([
  { name: 'zero retries', counts: [1, 1, 1, 1, 1], retries: [0, 0, 0, 0, 0] },
  { name: 'multiple retries', counts: [3, 2, 1, 4, 2], retries: [2, 1, 0, 3, 1] },
])('$name uses actual persisted attempt counts', ({ counts, retries }) => {
  const result = buildInterviewSummary(completedDetail(counts))
  expect(result.total_attempts).toBe(counts.reduce((total, count) => total + count, 0))
  expect(result.total_retries).toBe(retries.reduce((total, count) => total + count, 0))
  expect(result.questions.map((question) => question.retry_count)).toEqual(retries)
})

test('sparse attempt numbering preserves final numbers and counts retries from actual rows', () => {
  const result = buildInterviewSummary(completedDetail([2, 1, 3, 1, 2], [7, 4, 12, 1, 5]))
  expect(result.questions.map((question) => question.final_attempt_number)).toEqual([7, 4, 12, 1, 5])
  expect(result.questions.map((question) => question.retry_count)).toEqual([1, 0, 2, 0, 1])
  expect(result.total_attempts).toBe(9)
  expect(result.total_retries).toBe(4)
})

test('authoritative final identity wins over a later timestamp on an earlier attempt', () => {
  const detail = typedFinalDetail()
  detail.questions[0].latest_attempt_number = detail.questions[0].final_attempt_number = 3
  detail.summary.finalized_points[0].attempt_number = 3
  setMeasurement(detail, 0, measured({ recognized_word_count: 7 }))
  const attempts = detail.selected_question!.attempts
  attempts[0].submitted_at = '2026-10-06T10:00:09.000002Z'
  attempts[0].measurement = measured({ recognized_word_count: 99 })
  attempts[1].attempt_number = 3
  attempts[1].measurement = detail.summary.finalized_points[0].measurement
  detail.summary.last_submitted_at = attempts[0].submitted_at
  const result = buildInterviewSummary(detail)
  expect(result.questions[0].final_attempt_number).toBe(3)
  expect(result.questions[0].measurement?.recognized_word_count).toBe(7)
})

const contradictoryCases: { name: string; change: (detail: HistoryDetail) => void }[] = [
  { name: 'missing completion time', change: (detail) => { detail.summary.completed_at = null } },
  { name: 'current question on completed session', change: (detail) => { detail.summary.current_question_number = 5 } },
  { name: 'incomplete finalized count', change: (detail) => { detail.summary.finalized_question_count = 4 } },
  { name: 'incomplete practiced count', change: (detail) => { detail.summary.questions_practiced_count = 4 } },
  { name: 'missing question', change: (detail) => { detail.questions.pop() } },
  { name: 'missing final point', change: (detail) => { detail.summary.finalized_points.pop() } },
  { name: 'unfinalized question', change: (detail) => { detail.questions[0].finalized = false } },
  { name: 'missing final identity', change: (detail) => { detail.questions[0].final_attempt_id = null } },
  { name: 'wrong final identity', change: (detail) => { detail.questions[0].final_attempt_id = attemptId(99) } },
  { name: 'wrong latest identity', change: (detail) => { detail.questions[0].latest_attempt_id = attemptId(99) } },
  { name: 'point final-number contradiction', change: (detail) => { detail.summary.finalized_points[0].attempt_number = 7 } },
  { name: 'missing final number', change: (detail) => { detail.questions[0].final_attempt_number = null } },
  { name: 'latest final-number contradiction', change: (detail) => { detail.questions[0].latest_attempt_number = 7 } },
  { name: 'duplicate final identity', change: (detail) => {
    detail.questions[1].latest_attempt_id = detail.questions[1].final_attempt_id = attemptId(0)
    detail.summary.finalized_points[1].attempt_id = attemptId(0)
  } },
  { name: 'zero saved rows for finalized question', change: (detail) => { detail.questions[0].attempt_count = 0 } },
  { name: 'attempt totals disagree with question rows', change: (detail) => {
    detail.summary.total_attempt_count = 6; detail.summary.total_retry_count = 1
  } },
  { name: 'retry totals disagree with row counts', change: (detail) => { detail.summary.total_retry_count = 1 } },
  { name: 'question order changed', change: (detail) => { detail.questions.reverse() } },
  { name: 'point order changed', change: (detail) => { detail.summary.finalized_points.reverse() } },
]

test.each(contradictoryCases)('rejects $name with the fixed non-sensitive error', ({ change }) => {
  const detail = completedDetail()
  change(detail)
  expect(() => buildInterviewSummary(detail)).toThrowError(new Error(invalidMessage))
})

test.each(measurementCases)('preserves $name without normalization or inferred values', ({ measurement }) => {
  const detail = completedDetail()
  setMeasurement(detail, 0, measurement)
  expect(buildInterviewSummary(detail).questions[0].measurement).toEqual(measurement)
})

test('a typed final answer never inherits the earlier recording measurement from a selected history page', () => {
  const detail = typedFinalDetail()
  const before = structuredClone(detail)
  const result = buildInterviewSummary(detail)
  expect(result.questions[0].measurement).toBeNull()
  expect(result.questions[0].retry_count).toBe(1)
  expect(JSON.stringify(result)).not.toContain('Earlier recorded answer.')
  expect(JSON.stringify(result)).not.toContain('Final typed answer.')
  expect(detail).toEqual(before)
})

test('precision and measured zeros remain exact in the copied final snapshot', () => {
  const detail = completedDetail()
  setMeasurement(detail, 0, measured({ delivery_metrics: delivery({ pause_count: 0,
    total_pause_duration_seconds: 0, longest_pause_seconds: 0 }) }))
  setMeasurement(detail, 1, measured({ delivery_metrics: delivery() }))
  const rows = buildInterviewSummary(detail).questions
  expect(rows[0].measurement?.um_count).toBe(0)
  expect(rows[0].measurement?.uh_count).toBe(0)
  expect(rows[0].measurement?.delivery_metrics?.pause_count).toBe(0)
  expect(rows[0].measurement?.delivery_metrics?.total_pause_duration_seconds).toBe(0)
  expect(rows[0].measurement?.delivery_metrics?.longest_pause_seconds).toBe(0)
  expect(rows[1].measurement?.timed_utterance_span_seconds).toBe(1.234567890123)
  expect(rows[1].measurement?.estimated_words_per_minute).toBe(194.40000174967392)
  expect(rows[1].measurement?.delivery_metrics?.total_pause_duration_seconds).toBe(1.234567890789)
  expect(rows[1].measurement?.delivery_metrics?.longest_pause_seconds).toBe(0.734567890789)
})

test('repeated projections are deterministic in persisted question-index order and leave source facts mutable', () => {
  const detail = completedDetail([2, 1, 3, 1, 2], [7, 4, 12, 1, 5])
  setMeasurement(detail, 0, measured({ delivery_metrics: delivery() }))
  const before = structuredClone(detail)
  const first = buildInterviewSummary(detail)
  for (let repetition = 0; repetition < 3; repetition += 1) expect(buildInterviewSummary(detail)).toEqual(first)
  expect(first.questions.map((question) => question.question_index)).toEqual([0, 1, 2, 3, 4])
  expect(detail).toEqual(before)
  for (const value of [detail, detail.questions, detail.questions[0], detail.summary.finalized_points[0].measurement!,
    detail.summary.finalized_points[0].measurement!.delivery_metrics!]) expect(Object.isFrozen(value)).toBe(false)
})

test('mutating a returned summary cannot corrupt source facts or subsequent projections', () => {
  const detail = completedDetail()
  setMeasurement(detail, 0, measured({ delivery_metrics: delivery() }))
  const before = structuredClone(detail)
  const result = buildInterviewSummary(detail)
  const row = result.questions[0]
  for (const value of [result, result.questions, row, row.measurement!, row.measurement!.delivery_metrics!]) {
    expect(Object.isFrozen(value)).toBe(true)
  }
  expect(Reflect.set(result, 'total_attempts', 999)).toBe(false)
  expect(Reflect.set(result.questions, 'length', 0)).toBe(false)
  expect(Reflect.set(row, 'retry_count', 999)).toBe(false)
  expect(Reflect.set(row.measurement!, 'um_count', 999)).toBe(false)
  expect(Reflect.set(row.measurement!.delivery_metrics!, 'pause_count', 999)).toBe(false)
  expect(detail).toEqual(before)
  expect(buildInterviewSummary(detail)).toEqual(result)
})

test('summary snapshots do not share mutable source measurements or delivery objects', () => {
  const detail = completedDetail()
  const measurement = measured({ delivery_metrics: delivery() })
  setMeasurement(detail, 0, measurement)
  const result = buildInterviewSummary(detail)
  expect(result.questions[0].measurement).not.toBe(measurement)
  expect(result.questions[0].measurement?.delivery_metrics).not.toBe(measurement.delivery_metrics)
  measurement.um_count = 1
  measurement.delivery_metrics!.pause_count = 1
  expect(result.questions[0].measurement?.um_count).toBe(0)
  expect(result.questions[0].measurement?.delivery_metrics?.pause_count).toBe(2)
})

test('projection makes no network, storage, clock or random calls', () => {
  const detail = completedDetail()
  const networkCall = vi.fn(() => { throw new Error('Network access is forbidden.') })
  const storageAccess = vi.fn(() => { throw new Error('Storage access is forbidden.') })
  const storage = new Proxy({}, { get: storageAccess, set: storageAccess })
  const clockCall = vi.fn(() => { throw new Error('Clock access is forbidden.') })
  const clockNow = vi.fn(() => { throw new Error('Clock access is forbidden.') })
  const randomCall = vi.fn(() => { throw new Error('Random selection is forbidden.') })
  const randomSpy = vi.spyOn(Math, 'random').mockImplementation(randomCall)
  for (const name of ['fetch', 'XMLHttpRequest', 'WebSocket']) vi.stubGlobal(name, networkCall)
  for (const name of ['localStorage', 'sessionStorage', 'indexedDB']) vi.stubGlobal(name, storage)
  vi.stubGlobal('Date', Object.assign(clockCall, { now: clockNow }))
  try {
    buildInterviewSummary(detail)
  } finally {
    vi.unstubAllGlobals()
    randomSpy.mockRestore()
  }
  for (const call of [networkCall, storageAccess, clockCall, clockNow, randomCall]) expect(call).not.toHaveBeenCalled()
})

test('fixtures satisfy the unchanged History HTTP validator using only mocked responses', async () => {
  const { authenticateTestWorkspace } = await import('./authTestUtils')
  await authenticateTestWorkspace()
  const fixtures = [completedDetail(), completedDetail([2, 1, 3, 1, 2], [7, 4, 12, 1, 5]), typedFinalDetail(),
    ...measurementCases.map(({ measurement }) => {
      const detail = completedDetail(); setMeasurement(detail, 0, measurement); return detail
    })]
  const fetchMock = vi.fn()
  vi.stubGlobal('fetch', fetchMock)
  for (const detail of fixtures) {
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify(detail), { status: 200 }))
    const options = detail.selected_question === null ? {} : { questionIndex: detail.selected_question.question_index }
    const validated = await getHistoryDetail(sessionId, options)
    expect(validated).toEqual(detail)
    const reads = fetchMock.mock.calls.length
    expect(buildInterviewSummary(validated)).toEqual(buildInterviewSummary(detail))
    expect(fetchMock.mock.calls).toHaveLength(reads)
  }
  expect(fetchMock).toHaveBeenCalledTimes(fixtures.length)
})

test('the projection module has only a History type dependency and no state, provider or side-effect APIs', () => {
  const source = ts.createSourceFile('interviewSummary.ts', summaryModuleSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
  const imports = source.statements.filter(ts.isImportDeclaration)
  expect(imports).toHaveLength(1)
  expect(imports[0].importClause?.isTypeOnly).toBe(true)
  expect(ts.isStringLiteral(imports[0].moduleSpecifier) && imports[0].moduleSpecifier.text).toBe('./historyApi')
  const forbiddenIdentifiers = new Set(['React', 'useState', 'useReducer', 'useEffect', 'useLayoutEffect', 'fetch',
    'XMLHttpRequest', 'WebSocket', 'localStorage', 'sessionStorage', 'indexedDB', 'navigator', 'Date', 'performance',
    'setTimeout', 'setInterval', 'require'])
  const forbiddenReferences: string[] = []
  function inspect(node: ts.Node) {
    if (ts.isIdentifier(node) && forbiddenIdentifiers.has(node.text)) forbiddenReferences.push(node.text)
    if (ts.isCallExpression(node) && node.expression.kind === ts.SyntaxKind.ImportKeyword) forbiddenReferences.push('dynamic import')
    if (ts.isExportDeclaration(node) && node.moduleSpecifier) forbiddenReferences.push('module re-export')
    if (ts.isPropertyAccessExpression(node) && ts.isIdentifier(node.expression) &&
      node.expression.text === 'Math' && node.name.text === 'random') forbiddenReferences.push('Math.random')
    ts.forEachChild(node, inspect)
  }
  inspect(source)
  expect(forbiddenReferences).toEqual([])
})


test('completed adaptive history validates and projects all five persisted generated questions unchanged', async () => {
  const detail = completedDetail([2, 1, 1, 1, 1])
  detail.summary.question_engine = 'live-ai-roleplay-v1'
  detail.questions.forEach((question, index) => { question.question_text = `Saved adaptive question ${index + 1}` })
  const { authenticateTestWorkspace } = await import('./authTestUtils')
  await authenticateTestWorkspace()
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(detail)))
  vi.stubGlobal('fetch', fetchMock)
  const read = await getHistoryDetail(sessionId)
  const result = buildInterviewSummary(read)
  expect(result.question_count).toBe(5)
  expect(result.total_attempts).toBe(6)
  expect(result.total_retries).toBe(1)
  expect(result.questions.map((question) => question.question_text)).toEqual(detail.questions.map((question) => question.question_text))
  expect(fetchMock).toHaveBeenCalledOnce()
})
