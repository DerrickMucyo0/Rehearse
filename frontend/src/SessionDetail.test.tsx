// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import SessionDetail from './SessionDetail'
import { getHistoryDetail, HistoryApiError } from './historyApi'
import type { HistoryAttempt, HistoryDetail, HistoryMeasurement } from './historyApi'
import type { DeliveryMetrics } from './deliveryMetrics'

vi.mock('./historyApi', async (importOriginal) => {
  const original = await importOriginal<typeof import('./historyApi')>()
  return { ...original, getHistoryDetail: vi.fn() }
})

const FIRST = '00000000-0000-4000-8000-000000000001'
const SECOND = '00000000-0000-4000-8000-000000000002'
const CREATED = '2026-10-05T10:00:00Z'
const SUBMITTED = '2026-10-05T12:00:00Z'
const COMPLETED = '2026-10-05T13:00:00Z'
const PRIVATE_ERROR = 'private-server-answer-provider-error'
const PAGINATED_NUMBERS = [1, 3, 5, 7, 9, 11, 13, 15, 17, 19, 21, 25, 39]
const PAGINATED_QUESTIONS = [PAGINATED_NUMBERS, [2, 8, 12], [], [], []]
const readDetail = vi.mocked(getHistoryDetail)
const forbiddenFetch = vi.fn()
const microphoneAccess = vi.fn()
const recorder = vi.fn()

function attemptId(questionIndex: number, number: number): string {
  return `11111111-1111-4000-8000-${String(questionIndex * 1000 + number).padStart(12, '0')}`
}

function overview(
  sessionId = FIRST,
  completed = false,
  numbers: readonly (readonly number[])[] = completed ? [[1, 4, 9], [2, 8, 12], [1], [1], [1]] : [[1, 4, 9], [2, 8, 12], [], [], []],
): HistoryDetail {
  const finalizedCount = completed ? 5 : 1
  const questions = numbers.map((saved, question_index) => {
    const latest = saved.at(-1) ?? null
    const finalized = question_index < finalizedCount
    return {
      question_index, question_text: `Persisted question ${question_index + 1}.`, finalized,
      attempt_count: saved.length,
      latest_attempt_id: latest === null ? null : attemptId(question_index, latest),
      latest_attempt_number: latest,
      final_attempt_id: finalized && latest !== null ? attemptId(question_index, latest) : null,
      final_attempt_number: finalized ? latest : null,
    }
  })
  const practiced = questions.filter((question) => question.attempt_count > 0).length
  const attemptCount = questions.reduce((total, question) => total + question.attempt_count, 0)
  return {
    summary: {
      session_id: sessionId, status: completed ? 'completed' : 'active', created_at: CREATED,
      completed_at: completed ? COMPLETED : null, current_question_number: completed ? null : 2,
      total_questions: 5, finalized_question_count: finalizedCount, questions_practiced_count: practiced,
      total_attempt_count: attemptCount, total_retry_count: attemptCount - practiced,
      measured_final_answer_count: 0, last_submitted_at: SUBMITTED,
      last_saved_activity_at: completed ? COMPLETED : SUBMITTED,
      finalized_points: questions.filter((question) => question.finalized).map((question) => ({
        question_index: question.question_index, attempt_id: question.final_attempt_id!,
        attempt_number: question.final_attempt_number!, submitted_at: SUBMITTED, measurement: null,
      })),
    },
    questions,
    selected_question: null,
  }
}

function attempt(questionIndex: number, number: number, answer = `Saved answer for Question ${questionIndex + 1}, Attempt ${number}.`, isFinal = false): HistoryAttempt {
  return {
    attempt_id: attemptId(questionIndex, number), attempt_number: number, answer_text: answer,
    submitted_at: SUBMITTED, is_final: isFinal, measurement: null,
  }
}

function page(questionIndex: number, attempts: HistoryAttempt[], options: {
  sessionId?: string; numbers?: readonly (readonly number[])[]; hasMore?: boolean; nextAfter?: number;
} = {}): HistoryDetail {
  return {
    ...overview(options.sessionId, false, options.numbers),
    selected_question: {
      question_index: questionIndex, attempts, has_more: options.hasMore ?? false,
      next_after_attempt_number: options.nextAfter ?? null,
    },
  }
}

function firstPage(): HistoryDetail {
  return page(0, PAGINATED_NUMBERS.slice(0, 10).map((number) => attempt(0, number)), {
    numbers: PAGINATED_QUESTIONS, hasMore: true, nextAfter: 19,
  })
}

function lastPage(answer?: string): HistoryDetail {
  return page(0, PAGINATED_NUMBERS.slice(10).map((number) => attempt(0, number, answer, number === 39)), {
    numbers: PAGINATED_QUESTIONS,
  })
}

function props(sessionId = FIRST) {
  return { sessionId, onBack: vi.fn(), onRemove: vi.fn() }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (cause: unknown) => void
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}

function questionCard(number: number): HTMLElement {
  return screen.getByRole('button', { name: `Question ${number}` }).closest('article')!
}

function signalAt(index: number): AbortSignal {
  const signal = readDetail.mock.calls[index][1]?.signal
  expect(signal).toBeInstanceOf(AbortSignal)
  return signal!
}

function savedHeadings(questionNumber: number): string[] {
  return within(screen.getByRole('region', { name: `Saved attempts for Question ${questionNumber}` }))
    .queryAllByRole('heading', { level: 5 }).map((heading) => heading.textContent!)
}

beforeEach(() => {
  readDetail.mockReset().mockResolvedValue(overview())
  forbiddenFetch.mockReset().mockRejectedValue(new Error('Unmocked network is forbidden'))
  microphoneAccess.mockReset().mockRejectedValue(new Error('Microphone access is forbidden in History'))
  recorder.mockReset().mockImplementation(() => { throw new Error('History must not create a recorder') })
  vi.stubGlobal('fetch', forbiddenFetch)
  vi.stubGlobal('navigator', { mediaDevices: { getUserMedia: microphoneAccess } })
  vi.stubGlobal('MediaRecorder', recorder)
})

afterEach(() => {
  cleanup()
  expect(forbiddenFetch).not.toHaveBeenCalled()
  expect(microphoneAccess).not.toHaveBeenCalled()
  expect(recorder).not.toHaveBeenCalled()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

test('loads an overview without a question selector and shows five factual, accessible question summaries', async () => {
  const pending = deferred<HistoryDetail>()
  readDetail.mockReturnValueOnce(pending.promise)
  const options = props()
  const view = render(<SessionDetail {...options} />)
  const region = screen.getByRole('region', { name: 'Session detail' })
  expect(screen.getByRole('heading', { name: 'Session detail' })).toBeTruthy()
  expect(region.getAttribute('aria-busy')).toBe('true')
  expect(screen.getByRole('status').textContent).toBe('Loading session history…')
  expect(readDetail).toHaveBeenCalledOnce()
  expect(readDetail.mock.calls[0][0]).toBe(FIRST)
  expect(readDetail.mock.calls[0][1]).not.toHaveProperty('questionIndex')
  expect(readDetail.mock.calls[0][1]).not.toHaveProperty('afterAttemptNumber')
  expect(signalAt(0).aborted).toBe(false)
  await act(async () => pending.resolve(overview()))

  expect(region.getAttribute('aria-busy')).toBe('false')
  expect(screen.getByText('Active', { exact: true })).toBeTruthy()
  const questions = screen.getByRole('region', { name: 'Saved questions' })
  const buttons = within(questions).getAllByRole('button')
  expect(buttons.map((button) => button.textContent)).toEqual(['Question 1', 'Question 2', 'Question 3', 'Question 4', 'Question 5'])
  for (const button of buttons) expect(button.getAttribute('aria-expanded')).toBe('false')
  expect(within(questionCard(1)).getByText('Finalized', { exact: true })).toBeTruthy()
  expect(within(questionCard(1)).getByText('Final attempt: 9')).toBeTruthy()
  expect(within(questionCard(1)).getByText('Attempts: 3')).toBeTruthy()
  expect(within(questionCard(2)).getByText('Current', { exact: true })).toBeTruthy()
  expect(within(questionCard(2)).getByText('Latest attempt: 12')).toBeTruthy()
  expect(within(questionCard(2)).queryByText(/^Final attempt:/)).toBeNull()
  for (const number of [3, 4, 5]) {
    expect(within(questionCard(number)).getByText('Upcoming', { exact: true })).toBeTruthy()
    expect(within(questionCard(number)).getByText('Attempts: 0')).toBeTruthy()
    expect(within(questionCard(number)).queryByText(/^(Final|Latest) attempt:/)).toBeNull()
  }
  for (const [label, value] of [['Finalized questions', '1 / 5'], ['Attempts', '6'], ['Retries', '4'], ['Measured final answers', '0'], ['Current question', '2 of 5']]) {
    expect(screen.getByText(label, { selector: 'dt' }).nextElementSibling?.textContent).toBe(value)
  }
  expect(view.container.querySelector(`time[datetime="${CREATED}"]`)).toBeTruthy()
  expect(view.container.textContent).not.toContain(FIRST)
  expect(view.container.textContent).not.toMatch(/\b(improved|better|worse|good|bad|score|performance|readiness|confidence)\b/i)
  fireEvent.click(screen.getByRole('button', { name: 'Back to History' }))
  expect(options.onBack).toHaveBeenCalledOnce()
  expect(options.onRemove).not.toHaveBeenCalled()
})

test('completed sessions show the persisted completion timestamp and five finalized questions without a current question', async () => {
  readDetail.mockResolvedValueOnce(overview(FIRST, true))
  const view = render(<SessionDetail {...props()} />)
  await screen.findByRole('button', { name: 'Question 1' })
  expect(screen.getByText('Completed', { selector: 'p', exact: true })).toBeTruthy()
  expect(view.container.querySelector(`time[datetime="${COMPLETED}"]`)).toBeTruthy()
  expect(screen.getAllByText('Finalized', { exact: true })).toHaveLength(5)
  expect(screen.getByText('5 / 5')).toBeTruthy()
  expect(screen.queryByText('Current question', { selector: 'dt' })).toBeNull()
  expect(screen.queryByText('Current', { exact: true })).toBeNull()
  expect(screen.queryByText('Upcoming', { exact: true })).toBeNull()
  expect(screen.queryByText(/^Latest attempt:/)).toBeNull()
})

test('expansion reads the selected question and renders persisted answers as plain text with backend final badges', async () => {
  const markup = '<script>window.answerExecuted = true</script>\n<img src=x onerror=alert(1)>'
  readDetail.mockResolvedValueOnce(overview()).mockResolvedValueOnce(page(0, [
    attempt(0, 1, markup), attempt(0, 4), attempt(0, 9, 'Persisted final answer.', true),
  ]))
  const view = render(<SessionDetail {...props()} />)
  const question = await screen.findByRole('button', { name: 'Question 1' })
  fireEvent.click(question)
  await screen.findByRole('heading', { name: 'Attempt 9' })
  expect(question.getAttribute('aria-expanded')).toBe('true')
  expect(readDetail.mock.calls[1][0]).toBe(FIRST)
  expect(readDetail.mock.calls[1][1]).toMatchObject({ questionIndex: 0, limit: 10 })
  expect(readDetail.mock.calls[1][1]?.afterAttemptNumber).toBeUndefined()
  expect(savedHeadings(1)).toEqual(['Attempt 1', 'Attempt 4', 'Attempt 9'])
  expect(screen.getByText(/window.answerExecuted/).textContent).toBe(markup)
  expect(view.container.querySelector('script, img')).toBeNull()
  expect(window).not.toHaveProperty('answerExecuted')
  for (const number of [1, 4]) {
    const article = screen.getByRole('heading', { name: `Attempt ${number}` }).closest('article')!
    expect(within(article).queryByText('Final', { exact: true })).toBeNull()
    expect(article.querySelector('time')?.getAttribute('datetime')).toBe(SUBMITTED)
  }
  const finalArticle = screen.getByRole('heading', { name: 'Attempt 9' }).closest('article')!
  expect(within(finalArticle).getByText('Final', { exact: true })).toBeTruthy()
  expect(screen.getAllByText('Final', { exact: true })).toHaveLength(1)
  expect(view.container.textContent).not.toContain(attemptId(0, 9))
  expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull()
})

test('the current question keeps its latest attempt distinct from final attempts', async () => {
  readDetail.mockResolvedValueOnce(overview()).mockResolvedValueOnce(page(1, [2, 8, 12].map((number) => attempt(1, number))))
  render(<SessionDetail {...props()} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Question 2' }))
  await screen.findByRole('heading', { name: 'Attempt 12' })
  expect(within(questionCard(2)).getByText('Latest attempt: 12')).toBeTruthy()
  expect(within(questionCard(2)).queryByText(/^Final attempt:/)).toBeNull()
  expect(within(questionCard(2)).queryByText('Final', { exact: true })).toBeNull()
  expect(readDetail.mock.calls[1][1]).toMatchObject({ questionIndex: 1, limit: 10 })
})

test('an upcoming question can show an empty saved-attempt page and collapse without another request', async () => {
  readDetail.mockResolvedValueOnce(overview()).mockResolvedValueOnce(page(2, []))
  render(<SessionDetail {...props()} />)
  const question = await screen.findByRole('button', { name: 'Question 3' })
  fireEvent.click(question)
  await screen.findByText('No saved attempts in this page.')
  expect(question.getAttribute('aria-expanded')).toBe('true')
  fireEvent.click(question)
  expect(question.getAttribute('aria-expanded')).toBe('false')
  expect(screen.queryByRole('region', { name: 'Saved attempts for Question 3' })).toBeNull()
  expect(readDetail).toHaveBeenCalledTimes(2)
})

test('Load more uses the backend cursor, blocks duplicate clicks, preserves numbering gaps, and stops at the last page', async () => {
  const pending = deferred<HistoryDetail>()
  readDetail.mockResolvedValueOnce(overview(FIRST, false, PAGINATED_QUESTIONS))
    .mockResolvedValueOnce(firstPage()).mockReturnValueOnce(pending.promise)
  render(<SessionDetail {...props()} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Question 1' }))
  await screen.findByRole('heading', { name: 'Attempt 19' })
  const more = screen.getByRole('button', { name: 'Load more' })
  act(() => { fireEvent.click(more); fireEvent.click(more) })
  expect(readDetail).toHaveBeenCalledTimes(3)
  expect(readDetail.mock.calls[2][1]).toMatchObject({ questionIndex: 0, afterAttemptNumber: 19, limit: 10 })
  expect(more.hasAttribute('disabled')).toBe(true)
  expect(screen.getByRole('region', { name: 'Saved attempts for Question 1' }).getAttribute('aria-busy')).toBe('true')
  expect(savedHeadings(1)).toHaveLength(10)
  await act(async () => pending.resolve(lastPage()))
  expect(savedHeadings(1)).toEqual(PAGINATED_NUMBERS.map((number) => `Attempt ${number}`))
  expect(new Set(savedHeadings(1)).size).toBe(13)
  expect(screen.getByRole('heading', { name: 'Attempt 39' }).closest('article')?.textContent).toContain('Final')
  expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull()
  expect(screen.getByRole('region', { name: 'Saved attempts for Question 1' }).getAttribute('aria-busy')).toBe('false')
})

test('an overview failure is safe and retried only through Retry session request', async () => {
  readDetail.mockRejectedValueOnce(new HistoryApiError(PRIVATE_ERROR, 503)).mockResolvedValueOnce(overview())
  const options = props()
  const view = render(<SessionDetail {...options} />)
  expect((await screen.findByRole('alert')).textContent).toBe('Session history could not be loaded.')
  expect(readDetail).toHaveBeenCalledOnce()
  expect(view.container.textContent).not.toContain(PRIVATE_ERROR)
  expect(screen.queryByRole('button', { name: 'Remove from this browser' })).toBeNull()
  const oldSignal = signalAt(0)
  fireEvent.click(screen.getByRole('button', { name: 'Retry session request' }))
  await screen.findByRole('button', { name: 'Question 1' })
  expect(oldSignal.aborted).toBe(true)
  expect(readDetail).toHaveBeenCalledTimes(2)
  expect(readDetail.mock.calls[1][0]).toBe(FIRST)
  expect(readDetail.mock.calls[1][1]).not.toHaveProperty('questionIndex')
  expect(screen.queryByRole('alert')).toBeNull()
  expect(options.onRemove).not.toHaveBeenCalled()
})

test('a missing overview shows safe unavailability and removes only through the explicit local callback', async () => {
  readDetail.mockRejectedValueOnce(new HistoryApiError(PRIVATE_ERROR, 404))
  const options = props()
  const view = render(<SessionDetail {...options} />)
  expect((await screen.findByRole('alert')).textContent).toBe('This session is unavailable.')
  expect(screen.queryByRole('region', { name: 'Saved questions' })).toBeNull()
  expect(screen.getByRole('button', { name: 'Retry session request' })).toBeTruthy()
  expect(view.container.textContent).not.toContain(PRIVATE_ERROR)
  expect(view.container.textContent).not.toContain(FIRST)
  expect(options.onRemove).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Remove from this browser' }))
  expect(options.onRemove).toHaveBeenCalledOnce()
  expect(options.onRemove).toHaveBeenCalledWith(FIRST)
  expect(readDetail).toHaveBeenCalledOnce()
})

test('Retry saved attempts repeats the selected first page without exposing error text', async () => {
  readDetail.mockResolvedValueOnce(overview()).mockRejectedValueOnce(new HistoryApiError(PRIVATE_ERROR, 503))
    .mockResolvedValueOnce(page(0, [attempt(0, 1), attempt(0, 4), attempt(0, 9, 'Saved final answer.', true)]))
  const view = render(<SessionDetail {...props()} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Question 1' }))
  expect((await screen.findByRole('alert')).textContent).toBe('Saved attempts could not be loaded.')
  expect(readDetail).toHaveBeenCalledTimes(2)
  expect(view.container.textContent).not.toContain(PRIVATE_ERROR)
  fireEvent.click(screen.getByRole('button', { name: 'Retry saved attempts' }))
  await screen.findByRole('heading', { name: 'Attempt 9' })
  expect(readDetail.mock.calls[2][1]).toMatchObject({ questionIndex: 0, limit: 10 })
  expect(readDetail.mock.calls[2][1]?.afterAttemptNumber).toBeUndefined()
  expect(screen.queryByRole('alert')).toBeNull()
  expect(savedHeadings(1)).toEqual(['Attempt 1', 'Attempt 4', 'Attempt 9'])
})

test('a failed later page preserves saved attempts and retries the same backend cursor', async () => {
  readDetail.mockResolvedValueOnce(overview(FIRST, false, PAGINATED_QUESTIONS)).mockResolvedValueOnce(firstPage())
    .mockRejectedValueOnce(new HistoryApiError(PRIVATE_ERROR, 503)).mockResolvedValueOnce(lastPage())
  render(<SessionDetail {...props()} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Question 1' }))
  await screen.findByRole('heading', { name: 'Attempt 19' })
  fireEvent.click(screen.getByRole('button', { name: 'Load more' }))
  await screen.findByRole('button', { name: 'Retry saved attempts' })
  expect(savedHeadings(1)).toEqual(PAGINATED_NUMBERS.slice(0, 10).map((number) => `Attempt ${number}`))
  fireEvent.click(screen.getByRole('button', { name: 'Retry saved attempts' }))
  await screen.findByRole('heading', { name: 'Attempt 39' })
  expect(readDetail.mock.calls[3][1]).toMatchObject({ questionIndex: 0, afterAttemptNumber: 19, limit: 10 })
  expect(savedHeadings(1)).toEqual(PAGINATED_NUMBERS.map((number) => `Attempt ${number}`))
  expect(screen.queryByRole('alert')).toBeNull()
})

test('a selected-page 404 clears stale details and offers explicit removal from this browser', async () => {
  readDetail.mockResolvedValueOnce(overview()).mockRejectedValueOnce(new HistoryApiError(PRIVATE_ERROR, 404))
  const options = props()
  render(<SessionDetail {...options} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Question 1' }))
  expect((await screen.findByRole('alert')).textContent).toBe('This session is unavailable.')
  expect(screen.queryByRole('region', { name: 'Saved questions' })).toBeNull()
  expect(screen.queryByRole('button', { name: 'Retry saved attempts' })).toBeNull()
  expect(options.onRemove).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Remove from this browser' }))
  expect(options.onRemove).toHaveBeenCalledWith(FIRST)
  expect(readDetail).toHaveBeenCalledTimes(2)
})

test.each(['resolve', 'reject'] as const)('switching questions aborts a pending later page and ignores its late %s', async (outcome) => {
  const oldPage = deferred<HistoryDetail>()
  readDetail.mockResolvedValueOnce(overview(FIRST, false, PAGINATED_QUESTIONS)).mockResolvedValueOnce(firstPage())
    .mockReturnValueOnce(oldPage.promise).mockResolvedValueOnce(page(1, [2, 8, 12].map((number) => attempt(1, number)), {
      numbers: PAGINATED_QUESTIONS,
    }))
  render(<SessionDetail {...props()} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Question 1' }))
  await screen.findByRole('heading', { name: 'Attempt 19' })
  fireEvent.click(screen.getByRole('button', { name: 'Load more' }))
  const oldSignal = signalAt(2)
  fireEvent.click(screen.getByRole('button', { name: 'Question 2' }))
  await screen.findByRole('heading', { name: 'Attempt 12' })
  expect(oldSignal.aborted).toBe(true)
  expect(screen.getByRole('button', { name: 'Question 1' }).getAttribute('aria-expanded')).toBe('false')
  expect(screen.getByRole('button', { name: 'Question 2' }).getAttribute('aria-expanded')).toBe('true')
  await act(async () => {
    if (outcome === 'resolve') oldPage.resolve(lastPage('Late answer from Question 1.'))
    else oldPage.reject(new HistoryApiError(PRIVATE_ERROR, 503))
  })
  expect(screen.queryByText('Late answer from Question 1.')).toBeNull()
  expect(screen.queryByRole('region', { name: 'Saved attempts for Question 1' })).toBeNull()
  expect(savedHeadings(2)).toEqual(['Attempt 2', 'Attempt 8', 'Attempt 12'])
  expect(screen.queryByRole('alert')).toBeNull()
  expect(readDetail).toHaveBeenCalledTimes(4)
})

test('collapsing a question cancels its pending first page and excludes late answers', async () => {
  const pending = deferred<HistoryDetail>()
  readDetail.mockResolvedValueOnce(overview()).mockReturnValueOnce(pending.promise)
  render(<SessionDetail {...props()} />)
  const question = await screen.findByRole('button', { name: 'Question 1' })
  fireEvent.click(question)
  const signal = signalAt(1)
  expect(screen.getByRole('region', { name: 'Saved attempts for Question 1' }).getAttribute('aria-busy')).toBe('true')
  fireEvent.click(question)
  expect(signal.aborted).toBe(true)
  await act(async () => pending.resolve(page(0, [attempt(0, 9, 'Late collapsed answer.', true)])))
  expect(question.getAttribute('aria-expanded')).toBe('false')
  expect(screen.queryByText('Late collapsed answer.')).toBeNull()
  expect(screen.queryByRole('region', { name: 'Saved attempts for Question 1' })).toBeNull()
  expect(readDetail).toHaveBeenCalledTimes(2)
})

test('switching sessions aborts the old overview and ignores its late detail response', async () => {
  const oldOverview = deferred<HistoryDetail>()
  readDetail.mockReturnValueOnce(oldOverview.promise).mockResolvedValueOnce(overview(SECOND, true))
  const options = props()
  const view = render(<SessionDetail {...options} />)
  const oldSignal = signalAt(0)
  view.rerender(<SessionDetail {...options} sessionId={SECOND} />)
  await screen.findByText('Completed', { selector: 'p', exact: true })
  expect(oldSignal.aborted).toBe(true)
  const late = overview()
  late.questions[0].question_text = 'Private prompt from the previous session.'
  await act(async () => oldOverview.resolve(late))
  expect(screen.queryByText('Private prompt from the previous session.')).toBeNull()
  expect(screen.queryByText('Active', { exact: true })).toBeNull()
  expect(screen.getAllByText('Finalized', { exact: true })).toHaveLength(5)
  expect(readDetail.mock.calls[1][0]).toBe(SECOND)
  expect(view.container.textContent).not.toContain(FIRST)
})

test('switching sessions invalidates a late page without clearing the new session page loading state', async () => {
  const oldPage = deferred<HistoryDetail>()
  const newPage = deferred<HistoryDetail>()
  readDetail.mockResolvedValueOnce(overview(FIRST, false, PAGINATED_QUESTIONS)).mockResolvedValueOnce(firstPage())
    .mockReturnValueOnce(oldPage.promise).mockResolvedValueOnce(overview(SECOND)).mockReturnValueOnce(newPage.promise)
  const options = props()
  const view = render(<SessionDetail {...options} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Question 1' }))
  await screen.findByRole('heading', { name: 'Attempt 19' })
  fireEvent.click(screen.getByRole('button', { name: 'Load more' }))
  const oldSignal = signalAt(2)
  view.rerender(<SessionDetail {...options} sessionId={SECOND} />)
  expect(screen.queryByRole('heading', { name: 'Attempt 19' })).toBeNull()
  fireEvent.click(await screen.findByRole('button', { name: 'Question 1' }))
  const newSignal = signalAt(4)
  expect(oldSignal.aborted).toBe(true)
  await act(async () => oldPage.resolve(lastPage('Late answer from the previous session.')))
  expect(newSignal.aborted).toBe(false)
  expect(screen.getByRole('region', { name: 'Saved attempts for Question 1' }).getAttribute('aria-busy')).toBe('true')
  expect(screen.getByRole('status').textContent).toBe('Loading saved attempts…')
  expect(screen.queryByText('Late answer from the previous session.')).toBeNull()
  await act(async () => newPage.resolve(page(0, [
    attempt(0, 1), attempt(0, 4), attempt(0, 9, 'Answer from the selected session.', true),
  ], { sessionId: SECOND })))
  expect(screen.getByText('Answer from the selected session.')).toBeTruthy()
  expect(savedHeadings(1)).toEqual(['Attempt 1', 'Attempt 4', 'Attempt 9'])
  expect(readDetail).toHaveBeenCalledTimes(5)
  expect(readDetail.mock.calls[4][0]).toBe(SECOND)
})

test.each(['overview', 'page'] as const)('unmounting aborts a pending %s read and ignores its late response', async (pendingRead) => {
  const pending = deferred<HistoryDetail>()
  if (pendingRead === 'overview') readDetail.mockReturnValueOnce(pending.promise)
  else readDetail.mockResolvedValueOnce(overview()).mockReturnValueOnce(pending.promise)
  const options = props()
  const view = render(<SessionDetail {...options} />)
  if (pendingRead === 'page') fireEvent.click(await screen.findByRole('button', { name: 'Question 1' }))
  const signal = signalAt(pendingRead === 'overview' ? 0 : 1)
  view.unmount()
  expect(signal.aborted).toBe(true)
  await act(async () => pending.resolve(pendingRead === 'overview' ? overview() : page(0, [attempt(0, 9, 'Late unmounted answer.', true)])))
  expect(view.container.textContent).toBe('')
  expect(screen.queryByRole('region', { name: 'Session detail' })).toBeNull()
  expect(options.onBack).not.toHaveBeenCalled()
  expect(options.onRemove).not.toHaveBeenCalled()
})

function persistedMeasurement(delivery_metrics: DeliveryMetrics | null): HistoryMeasurement {
  return { measurement_version: 'speaking-metrics-v1', measurement_source: 'original_transcription',
    recognized_word_count: 10, um_count: 0, uh_count: 1, filler_unavailable_reason: null,
    timed_utterance_span_seconds: 12.123456789, estimated_words_per_minute: 49.491231198,
    timing_unavailable_reason: null, delivery_metrics }
}
function recordedDelivery(changes: Partial<DeliveryMetrics> = {}): DeliveryMetrics {
  return { version: 'pause-metrics-v1', source: 'original_transcription', pause_count: 2,
    total_pause_duration_seconds: 1.234567890123, longest_pause_seconds: 0.765432109876,
    unavailable_reason: null, ...changes }
}
async function openMeasuredAttempt(delivery_metrics: DeliveryMetrics | null) {
  const selected = attempt(0, 9, 'An edited saved answer; original timing facts remain unchanged.', true)
  selected.measurement = persistedMeasurement(delivery_metrics)
  readDetail.mockResolvedValueOnce(overview()).mockResolvedValueOnce(page(0, [selected]))
  render(<SessionDetail {...props()} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Question 1' }))
  const article = (await screen.findByRole('heading', { name: 'Attempt 9' })).closest('article')!
  return { article, selected }
}

test('historical selected voice attempt displays Not recorded without fabricating a delivery version', async () => {
  const { article } = await openMeasuredAttempt(null)
  expect(within(article).getAllByText('Not recorded').length).toBeGreaterThan(0)
  expect(article.textContent).not.toContain('pause-metrics-v1')
  expect(article.textContent).not.toContain('Unavailable — No measurement')
})

test('selected persisted delivery values render factual labels and display-only rounding', async () => {
  const original = recordedDelivery()
  const { article, selected } = await openMeasuredAttempt(original)
  expect(within(article).getByRole('heading', { name: 'Timed pauses' })).toBeTruthy()
  expect(within(article).getByText('Pause count').nextElementSibling?.textContent).toBe('2')
  expect(within(article).getByText('Total pause time').nextElementSibling?.textContent).toBe('1.2 s')
  expect(within(article).getByText('Longest pause').nextElementSibling?.textContent).toBe('0.8 s')
  expect(selected.measurement!.delivery_metrics).toEqual(original)
  expect(article.textContent).toContain('original transcription')
  expect(article.textContent).toContain('not necessarily acoustic silence')
})

test('zero-pause selected attempt renders three measured zeros, not unavailable or historical absence', async () => {
  const { article } = await openMeasuredAttempt(recordedDelivery({ pause_count: 0, total_pause_duration_seconds: 0, longest_pause_seconds: 0 }))
  expect(within(article).getByText('Pause count').nextElementSibling?.textContent).toBe('0')
  expect(within(article).getAllByText('0.0 s')).toHaveLength(2)
  expect(article.textContent).not.toContain('Not recorded')
  expect(article.textContent).not.toContain('Unavailable')
})

test.each(['missing_timings', 'timing_coverage_mismatch', 'invalid_timing', 'invalid_timing_order', 'unusable_span'] as const)(
  'selected recorded delivery %s displays unavailable rather than unrecorded or zero', async (unavailable_reason) => {
    const { article } = await openMeasuredAttempt(recordedDelivery({
      pause_count: null, total_pause_duration_seconds: null, longest_pause_seconds: null, unavailable_reason,
    }))
    expect(article.textContent).toContain('Unavailable')
    expect(article.textContent).not.toContain(unavailable_reason)
    expect(article.textContent).not.toContain('Not recorded')
    expect(article.textContent).not.toContain('0.0 s')
  },
)

test('typed selected final keeps no measurement distinct from historical voice Not recorded', async () => {
  readDetail.mockResolvedValueOnce(overview()).mockResolvedValueOnce(page(0, [attempt(0, 9, 'Typed final.', true)]))
  render(<SessionDetail {...props()} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Question 1' }))
  expect(await screen.findByText('Timed pauses: Unavailable — No measurement')).toBeTruthy()
  expect(screen.queryByText('Not recorded')).toBeNull()
})

test('delivery on an obsolete selected question cannot leak into the replacement question', async () => {
  const old = deferred<HistoryDetail>()
  const previous = attempt(0, 9, 'Old question measured answer', true)
  previous.measurement = persistedMeasurement(recordedDelivery())
  const current = attempt(1, 12, 'Current typed answer')
  readDetail.mockResolvedValueOnce(overview()).mockReturnValueOnce(old.promise).mockResolvedValueOnce(page(1, [current]))
  render(<SessionDetail {...props()} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Question 1' }))
  fireEvent.click(screen.getByRole('button', { name: 'Question 2' }))
  await screen.findByText('Current typed answer')
  await act(async () => old.resolve(page(0, [previous])))
  expect(screen.queryByText('Old question measured answer')).toBeNull()
  expect(screen.queryByRole('heading', { name: 'Timed pauses' })).toBeNull()
  expect(readDetail).toHaveBeenCalledTimes(3)
})

test('a paginated selected question preserves delivery facts attached to each persisted attempt', async () => {
  const older = attempt(0, 1)
  older.measurement = persistedMeasurement(null)
  const first = firstPage()
  first.selected_question!.attempts[0] = older
  const final = lastPage()
  final.selected_question!.attempts.at(-1)!.measurement = persistedMeasurement(recordedDelivery({ pause_count: 0, total_pause_duration_seconds: 0, longest_pause_seconds: 0 }))
  readDetail.mockResolvedValueOnce(overview(FIRST, false, PAGINATED_QUESTIONS)).mockResolvedValueOnce(first).mockResolvedValueOnce(final)
  render(<SessionDetail {...props()} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Question 1' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Load more' }))
  const article = (await screen.findByRole('heading', { name: 'Attempt 39' })).closest('article')!
  expect(within(article).getByText('Final')).toBeTruthy()
  expect(within(article).getByText('Pause count').nextElementSibling?.textContent).toBe('0')
  expect(within(screen.getByRole('heading', { name: 'Attempt 1' }).closest('article')!).getAllByText('Not recorded').length).toBeGreaterThan(0)
  expect(readDetail.mock.calls[2][1]?.afterAttemptNumber).toBe(19)
})
