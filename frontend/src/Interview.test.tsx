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
        current_question: questions[0], questions, answers: [],
      }
    } else if (options?.method === 'POST') {
      const body = JSON.parse(options.body as string) as { question_index: number; answer: string }
      expect(body.question_index).toBe(session.current_question_index)
      const nextIndex = session.current_question_index + 1
      session = {
        ...session,
        current_question_index: nextIndex,
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
