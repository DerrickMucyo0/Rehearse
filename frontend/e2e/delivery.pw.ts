import { expect, test } from '@playwright/test'

test('provider-free delivery review, measured retry comparison and finalized history', async ({ page }) => {
  const id = '11111111-1111-4111-8111-111111111111'
  const questions = ['Question one', 'Question two', 'Question three', 'Question four', 'Question five']
  const date = '2026-10-05T12:00:00.123456Z'
  let current = 0
  let transcriptions = 0
  let summaryReads = 0
  const forbidden: string[] = []
  const errors: string[] = []
  const deliveries = [
    { version: 'pause-metrics-v1', source: 'original_transcription', pause_count: 2,
      total_pause_duration_seconds: 1.23456789, longest_pause_seconds: 0.73456789, unavailable_reason: null },
    { version: 'pause-metrics-v1', source: 'original_transcription', pause_count: 0,
      total_pause_duration_seconds: 0, longest_pause_seconds: 0, unavailable_reason: null },
  ]
  const metrics = { source: 'original_transcription', recognized_word_count: 3, um_count: 0, uh_count: 0,
    filler_unavailable_reason: null, timed_utterance_span_seconds: 2, estimated_words_per_minute: 90, timing_unavailable_reason: null }
  const saved: { id: string; question_index: number; attempt_number: number; answer: string;
    submitted_at: string; measurement_id: string | null }[] = []
  const session = () => ({ id, status: 'active', current_question_index: current, current_question: questions[current],
    current_question_latest_attempt_number: saved.filter((attempt) => attempt.question_index === current).length,
    questions, answers: current > 0 ? [saved.at(-1)!.answer] : [] })
  const measurement = (index: number) => ({ measurement_version: 'speaking-metrics-v1', measurement_source: metrics.source,
    recognized_word_count: metrics.recognized_word_count, um_count: metrics.um_count, uh_count: metrics.uh_count,
    filler_unavailable_reason: metrics.filler_unavailable_reason, timed_utterance_span_seconds: metrics.timed_utterance_span_seconds,
    estimated_words_per_minute: metrics.estimated_words_per_minute, timing_unavailable_reason: metrics.timing_unavailable_reason,
    delivery_metrics: deliveries[index] })
  const summary = () => ({ session_id: id, status: 'active', created_at: date, completed_at: null,
    current_question_number: current + 1, total_questions: 5, finalized_question_count: current,
    questions_practiced_count: saved.length ? 1 : 0, total_attempt_count: saved.length,
    total_retry_count: Math.max(0, saved.length - 1), measured_final_answer_count: current,
    last_submitted_at: saved.length ? date : null, last_saved_activity_at: date,
    finalized_points: current ? [{ question_index: 0, attempt_id: saved.at(-1)!.id,
      attempt_number: saved.at(-1)!.attempt_number, submitted_at: date, measurement: measurement(1) }] : [] })
  const change = (before: number, after: number) => ({ before, after, delta: after - before,
    before_unavailable_reason: null, after_unavailable_reason: null, comparable: true, comparison_unavailable_reason: null })
  page.on('pageerror', (error) => errors.push(error.message))
  await page.addInitScript(() => {
    class Recorder {
      static isTypeSupported() { return true }
      state = 'inactive'
      mimeType = 'audio/webm'
      ondataavailable: ((event: { data: Blob }) => void) | null = null
      onstop: (() => void) | null = null
      start() { this.state = 'recording' }
      stop() {
        this.state = 'inactive'
        queueMicrotask(() => {
          this.ondataavailable?.({ data: new Blob(['synthetic audio'], { type: this.mimeType }) })
          this.onstop?.()
        })
      }
    }
    Object.defineProperty(globalThis, 'MediaRecorder', { configurable: true, value: Recorder })
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true,
      value: { getUserMedia: async () => ({ getTracks: () => [{ stop() {} }] }) } })
  })
  // Every API response is an explicit fixture; no FastAPI or provider is called.
  await page.route('**/*', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (!['localhost', '127.0.0.1'].includes(url.hostname)) {
      forbidden.push('external request'); await route.abort(); return
    }
    if (!url.pathname.startsWith('/api/')) { await route.continue(); return }
    let body: unknown
    let status = 200
    if (url.pathname === '/api/health') body = { status: 'ok', service: 'rehearse-api' }
    else if (url.pathname === '/api/sessions' && request.method() === 'POST') { body = session(); status = 201 }
    else if (url.pathname === `/api/sessions/${id}`) body = session()
    else if (url.pathname.endsWith('/transcriptions')) {
      const index = transcriptions++
      expect(index).toBeLessThan(2)
      body = { session_id: id, question_index: current,
        measurement_id: `33333333-3333-4333-8333-${String(index + 1).padStart(12, '0')}`,
        text: 'one two three', language: 'eng', words: index === 0 ? [
          { text: 'one', start: 0, end: 0.2 }, { text: 'two', start: 0.7, end: 0.9 }, { text: 'three', start: 1.63456789, end: 2 },
        ] : [{ text: 'one', start: 0, end: 0.2 }, { text: 'two', start: 0.2, end: 0.4 }, { text: 'three', start: 0.4, end: 2 }],
        metrics, delivery_metrics: deliveries[index] }
    } else if (url.pathname.endsWith('/attempts') && request.method() === 'POST') {
      const input = request.postDataJSON() as { answer: string; expected_last_attempt_number: number; measurement_id: string | null }
      expect(input.expected_last_attempt_number).toBe(saved.length)
      const attempt = { id: `44444444-4444-4444-8444-${String(saved.length + 1).padStart(12, '0')}`,
        question_index: current, attempt_number: saved.length + 1, answer: input.answer,
        submitted_at: date, measurement_id: input.measurement_id }
      saved.push(attempt); body = { attempt, session: session() }; status = 201
    } else if (url.pathname.endsWith('/attempts')) {
      const index = Number(/\/questions\/(\d+)\//.exec(url.pathname)?.[1])
      body = saved.filter((attempt) => attempt.question_index === index)
    }
    else if (url.pathname.endsWith('/comparison')) {
      const index = Number(/\/questions\/(\d+)\//.exec(url.pathname)?.[1])
      const attempts = saved.filter((attempt) => attempt.question_index === index)
      const identity = (index: number) => ({ id: attempts[index].id, attempt_number: attempts[index].attempt_number,
        measurement_id: attempts[index].measurement_id, measurement_version: 'speaking-metrics-v1', measurement_source: metrics.source })
      body = { session_id: id, question_index: index, before_attempt: attempts.length ? identity(0) : null,
        after_attempt: attempts.length > 1 ? identity(1) : null,
        comparison: attempts.length > 1 ? { recognized_word_count: change(3, 3), um_count: change(0, 0), uh_count: change(0, 0),
          timed_utterance_span_seconds: change(2, 2), estimated_words_per_minute: change(90, 90) } : null,
        delivery_comparison: attempts.length > 1 ? { before_version: 'pause-metrics-v1', after_version: 'pause-metrics-v1',
          before_source: metrics.source, after_source: metrics.source, pause_count: change(2, 0),
          total_pause_duration_seconds: change(deliveries[0].total_pause_duration_seconds, 0),
          longest_pause_seconds: change(deliveries[0].longest_pause_seconds, 0) } : null }
    } else if (url.pathname.endsWith('/continue')) { current += 1; body = session() }
    else if (url.pathname === '/api/history/summaries') {
      summaryReads += 1; body = { summaries: [summary()], missing_session_ids: [] }
    } else if (url.pathname.endsWith('/history-detail')) {
      body = { summary: summary(), questions: questions.map((question_text, index) => ({
        question_index: index, question_text, finalized: index < current, attempt_count: index === 0 ? saved.length : 0,
        latest_attempt_id: index === 0 ? saved.at(-1)!.id : null, latest_attempt_number: index === 0 ? saved.length : null,
        final_attempt_id: index === 0 && current > 0 ? saved.at(-1)!.id : null,
        final_attempt_number: index === 0 && current > 0 ? saved.length : null,
      })), selected_question: url.searchParams.has('question_index') ? { question_index: 0,
        attempts: saved.map((attempt, index) => ({ attempt_id: attempt.id, attempt_number: attempt.attempt_number,
          answer_text: attempt.answer, submitted_at: date, is_final: index === saved.length - 1, measurement: measurement(index) })),
        has_more: false, next_after_attempt_number: null } : null }
    } else { forbidden.push('unmocked API'); await route.abort(); return }
    await route.fulfill({ status, json: body })
  })
  await page.goto('/')
  await expect(page.getByText('Backend connected', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Start Interview', exact: true }).click()
  for (let index = 0; index < 2; index += 1) {
    await page.getByRole('button', { name: 'Record Answer', exact: true }).click()
    await page.getByRole('button', { name: 'Stop Recording', exact: true }).click()
    await page.getByRole('button', { name: 'Transcribe Recording', exact: true }).click()
    const pauses = page.getByRole('region', { name: 'Timed pauses', exact: true })
    await expect(pauses).toBeVisible()
    await expect(pauses.getByText(index === 0 ? '1.2 s' : '0.0 s', { exact: true }).first()).toBeVisible()
    await expect(page.getByText('Based on your original recording. Editing the transcript won’t change these measurements.')).toBeVisible()
    await page.getByRole('textbox', { name: 'Your answer' }).fill(`Edited attempt ${index + 1}`)
    await page.getByRole('button', { name: 'Submit Attempt', exact: true }).click()
    await expect(page.getByRole('button', { name: 'Continue', exact: true })).toBeEnabled()
    if (index === 0) {
      await page.getByRole('button', { name: 'Retry', exact: true }).click()
      await expect(page.getByRole('region', { name: 'Timed pauses', exact: true })).toHaveCount(0)
    }
  }
  const comparison = page.getByRole('table').filter({ has: page.getByRole('rowheader', { name: 'Pause count', exact: true }) })
  await expect(comparison.getByRole('row', { name: 'Pause count 2 0 -2', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Continue', exact: true }).click()
  await expect(page.getByText('Question 2 of 5', { exact: true })).toBeVisible()
  await page.getByRole('navigation').getByRole('button', { name: 'Progress', exact: true }).click()
  const progress = page.getByRole('region', { name: 'Progress', exact: true })
  const pauses = progress.getByRole('region', { name: 'Pause count', exact: true })
  await expect(pauses.getByText(/available for 1 of 1/)).toBeVisible()
  await expect(pauses.getByRole('row')).toHaveCount(2)
  await expect(pauses.getByRole('cell', { name: '0', exact: true })).toBeVisible()
  const reads = summaryReads
  await page.getByRole('navigation').getByRole('button', { name: 'History', exact: true }).click()
  await page.getByRole('button', { name: 'Open session', exact: true }).click()
  await page.getByRole('button', { name: 'Question 1', exact: true }).click()
  await expect(page.getByRole('region', { name: 'Timed pauses', exact: true })).toHaveCount(2)
  expect(summaryReads).toBe(reads)
  expect(transcriptions).toBe(2)
  expect(await page.evaluate(() => JSON.stringify(Object.entries(localStorage)))).not.toMatch(/pause-metrics|pause_count|Edited attempt|total_pause/)
  expect(forbidden).toEqual([])
  expect(errors).toEqual([])
})
