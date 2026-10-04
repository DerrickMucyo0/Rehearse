// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import Interview from './Interview'
import type { InterviewSession } from './interviewApi'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

function mockSessionApi() {
  let creations = 0
  let session: InterviewSession
  const questions = ['Question one', 'Question two', 'Question three', 'Question four', 'Question five']
  const fetchMock = vi.fn(async (_url: string, options?: RequestInit) => {
    if (options?.method === 'POST' && !options.body) {
      creations += 1
      session = {
        id: `session-${creations}`, status: 'active', current_question_index: 0,
        current_question: questions[0], current_prompt: questions[0], turn_revision: 0, probe_count: 0, turns: [], questions, answers: [],
      }
    } else if (options?.method === 'POST') {
      const body = JSON.parse(options.body as string) as { question_index: number; turn_revision: number; submission_id: string; answer: string }
      expect(body.question_index).toBe(session.current_question_index)
      expect(body.turn_revision).toBe(session.turn_revision)
      expect(body.submission_id).toBeTruthy()
      const nextIndex = session.current_question_index + 1
      session = {
        ...session,
        current_question_index: nextIndex,
        turn_revision: session.turn_revision + 1,
        current_prompt: questions[nextIndex] ?? null,
        answers: [...session.answers, body.answer],
        status: nextIndex === questions.length ? 'completed' : 'active',
        current_question: questions[nextIndex] ?? null,
      }
    }
    return new Response(JSON.stringify(session), { status: 200 })
  })
  vi.stubGlobal('fetch', fetchMock)
  return { fetchMock, creations: () => creations }
}

async function completeInterview() {
  fireEvent.click(screen.getByRole('button', { name: 'Start Interview' }))
  for (let index = 0; index < 5; index += 1) {
    await screen.findByText(`Question ${index + 1} of 5`)
    const textbox = screen.getByRole('textbox', { name: 'Your answer' })
    await waitFor(() => expect((textbox as HTMLTextAreaElement).disabled).toBe(false))
    fireEvent.change(textbox, { target: { value: `Answer ${index + 1}` } })
    fireEvent.click(screen.getByRole('button', { name: 'Submit Answer' }))
  }
  // Wait for the final response to remove the answer form.
  await waitFor(() => expect(screen.queryByRole('textbox')).toBeNull())
}

test('keeps the completed session until the user explicitly starts a new interview', async () => {
  const api = mockSessionApi()
  render(<Interview />)
  await completeInterview()

  expect(screen.getByRole('heading', { name: 'Interview Complete' })).toBeTruthy()
  expect(screen.getByText('You completed all 5 questions.')).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Start Interview' })).toBeNull()
  expect(api.creations()).toBe(1)

  fireEvent.click(screen.getByRole('button', { name: 'Start New Interview' }))
  await screen.findByText('Question 1 of 5')
  expect(api.creations()).toBe(2)
  expect(screen.queryByRole('heading', { name: 'Interview Complete' })).toBeNull()
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('')
})

test('preserves completion if starting a new interview fails', async () => {
  const api = mockSessionApi()
  render(<Interview />)
  await completeInterview()
  api.fetchMock.mockRejectedValueOnce(new TypeError('Network unavailable'))

  fireEvent.click(screen.getByRole('button', { name: 'Start New Interview' }))
  await screen.findByRole('alert')
  expect(screen.getByRole('heading', { name: 'Interview Complete' })).toBeTruthy()
  expect(screen.getByText('You completed all 5 questions.')).toBeTruthy()
  expect(screen.queryByRole('textbox')).toBeNull()
  expect(api.creations()).toBe(1)
})

const adaptiveSession: InterviewSession = {
  id: 'adaptive-session', status: 'active', current_question_index: 0,
  current_question: 'Describe a project.', current_prompt: 'Describe a project.',
  turn_revision: 0, probe_count: 0, turns: [], questions: ['Describe a project.'], answers: [],
}

async function startAdaptive() {
  render(<Interview />)
  fireEvent.click(screen.getByRole('button', { name: 'Start Interview' }))
  await screen.findByRole('textbox')
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'I built a calendar.' } })
}

test.each([502, 503, 504, 409])('reasoning error %s preserves draft and reuses the submission identifier on retry', async (status) => {
  const response = () => new Response(JSON.stringify(adaptiveSession))
  const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(response()))
  fetchMock.mockResolvedValueOnce(response()).mockResolvedValueOnce(response())
    .mockResolvedValueOnce(new Response('{}', { status }))
  vi.stubGlobal('fetch', fetchMock)
  await startAdaptive()
  fireEvent.click(screen.getByRole('button', { name: 'Submit Answer' }))
  await screen.findByRole('alert')
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('I built a calendar.')
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).disabled).toBe(false)
  const firstBody = JSON.parse(fetchMock.mock.calls[2][1].body)
  fetchMock.mockResolvedValueOnce(response()).mockResolvedValueOnce(new Response(JSON.stringify({
    ...adaptiveSession, turn_revision: 1, probe_count: 1, current_prompt: 'What changed for users?',
  })))
  fireEvent.click(screen.getByRole('button', { name: 'Submit Answer' }))
  await screen.findByRole('heading', { name: 'What changed for users?' })
  expect(JSON.parse(fetchMock.mock.calls[4][1].body)).toEqual(firstBody)
  expect(screen.getByText('Question 1 of 1')).toBeTruthy()
})

test('pending reasoning blocks duplicate submits and preserves input until acceptance', async () => {
  let finish!: (value: Response) => void
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(new Response(JSON.stringify(adaptiveSession)))
    .mockResolvedValueOnce(new Response(JSON.stringify(adaptiveSession)))
    .mockImplementationOnce(() => new Promise<Response>((resolve) => { finish = resolve }))
  vi.stubGlobal('fetch', fetchMock)
  await startAdaptive()
  const button = screen.getByRole('button', { name: 'Submit Answer' })
  fireEvent.click(button)
  fireEvent.submit(button.closest('form')!)
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3))
  expect(screen.getByText('Waiting for the interviewer…')).toBeTruthy()
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).disabled).toBe(true)
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('I built a calendar.')
  finish(new Response(JSON.stringify({ ...adaptiveSession, turn_revision: 1, current_prompt: 'How did you test it?' })))
  await screen.findByRole('heading', { name: 'How did you test it?' })
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('')
})

test('lost response reconciles the accepted same-question turn without resubmitting', async () => {
  let accepted: InterviewSession = adaptiveSession
  let submissions = 0
  const fetchMock = vi.fn(async (_url: string, options?: RequestInit) => {
    if (options?.body) {
      submissions += 1
      const body = JSON.parse(options.body as string)
      accepted = { ...adaptiveSession, turn_revision: 1, probe_count: 1, current_prompt: 'What was your result?', turns: [{
        ...body, prompt: adaptiveSession.current_prompt!, action: 'FOLLOW_UP', transition_source: 'nemotron',
      }] }
      throw new TypeError('Lost response')
    }
    return new Response(JSON.stringify(accepted))
  })
  vi.stubGlobal('fetch', fetchMock)
  await startAdaptive()
  fireEvent.click(screen.getByRole('button', { name: 'Submit Answer' }))
  await screen.findByRole('alert')
  fireEvent.click(screen.getByRole('button', { name: 'Submit Answer' }))
  await screen.findByRole('heading', { name: 'What was your result?' })
  expect(submissions).toBe(1)
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('')
})

test('an unrelated same-question turn does not silently discard the draft', async () => {
  vi.stubGlobal('fetch', vi.fn()
    .mockResolvedValueOnce(new Response(JSON.stringify(adaptiveSession)))
    .mockResolvedValueOnce(new Response(JSON.stringify({ ...adaptiveSession, turn_revision: 1 }))))
  await startAdaptive()
  fireEvent.click(screen.getByRole('button', { name: 'Submit Answer' }))
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', expect.stringContaining('draft is preserved'))
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('I built a calendar.')
})
