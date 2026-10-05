import { expect, test } from '@playwright/test'

test('remembered typed sessions open persisted finalized answers and leave Practice usable', async ({ page }) => {
  const unexpectedRequests: string[] = []
  const errors: string[] = []
  await page.route('**/*', async (route) => {
    const url = new URL(route.request().url())
    const local = url.hostname === 'localhost' || url.hostname === '127.0.0.1'
    if (!local || url.pathname.endsWith('/audio') || url.pathname.endsWith('/transcriptions')) {
      unexpectedRequests.push(local ? 'provider-route' : 'external-host')
      await route.abort()
    } else await route.continue()
  })
  page.on('pageerror', (error) => errors.push(error.message))
  await page.goto('/')
  await expect(page.getByText('Backend connected', { exact: true })).toBeVisible()
  const navigation = page.getByRole('navigation')
  const practice = navigation.getByRole('button', { name: 'Practice', exact: true })
  const history = navigation.getByRole('button', { name: 'History', exact: true })

  const created = page.waitForResponse((response) => response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/sessions')
  await page.getByRole('button', { name: 'Start Interview', exact: true }).click()
  const createdResponse = await created
  expect(createdResponse.status()).toBe(201)
  const session = await createdResponse.json()
  await expect(page.getByRole('textbox', { name: 'Your answer' })).toBeVisible()
  const remembered = await page.evaluate((id: string) => localStorage.getItem(`rehearse.history.v1:${id}`), session.id)
  expect(remembered).toBe('1')
  const privateAnswer = 'A persisted typed answer for the History browser integration.'
  await page.getByRole('textbox', { name: 'Your answer' }).fill(privateAnswer)
  const submitted = page.waitForResponse((response) => response.request().method() === 'POST'
    && new URL(response.url()).pathname.endsWith('/questions/0/attempts'))
  await page.getByRole('button', { name: 'Submit Attempt', exact: true }).click()
  expect((await submitted).status()).toBe(201)
  await expect(page.getByRole('button', { name: 'Continue', exact: true })).toBeEnabled()
  const advanced = page.waitForResponse((response) => response.request().method() === 'POST'
    && new URL(response.url()).pathname.endsWith('/questions/0/continue'))
  await page.getByRole('button', { name: 'Continue', exact: true }).click()
  expect((await advanced).status()).toBe(200)
  await expect(page.getByText('Question 2 of 5', { exact: true })).toBeVisible()
  await page.getByRole('textbox', { name: 'Your answer' }).fill('An idle Practice draft survives History navigation.')

  await history.click()
  await expect(history).toHaveAttribute('aria-current', 'page')
  await expect(page.getByRole('heading', { name: 'History', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Open session', exact: true })).toHaveCount(1)
  const overview = page.waitForResponse((response) => response.request().method() === 'GET'
    && new URL(response.url()).pathname.endsWith('/history-detail') && !new URL(response.url()).searchParams.has('question_index'))
  await page.getByRole('button', { name: 'Open session', exact: true }).click()
  expect((await overview).status()).toBe(200)
  await expect(page.getByRole('heading', { name: 'Session detail', exact: true })).toBeVisible()
  const firstQuestion = page.getByRole('button', { name: 'Question 1', exact: true })
  await expect(firstQuestion).toHaveAttribute('aria-expanded', 'false')
  await expect(page.getByText('Finalized', { exact: true }).first()).toBeVisible()
  const selected = page.waitForResponse((response) => response.request().method() === 'GET'
    && new URL(response.url()).pathname.endsWith('/history-detail')
    && new URL(response.url()).searchParams.get('question_index') === '0')
  await firstQuestion.click()
  const selectedResponse = await selected
  expect(selectedResponse.status()).toBe(200)
  expect(new URL(selectedResponse.url()).searchParams.get('limit')).toBe('10')
  await expect(firstQuestion).toHaveAttribute('aria-expanded', 'true')
  await expect(page.getByRole('heading', { name: 'Attempt 1', exact: true })).toBeVisible()
  await expect(page.getByText(privateAnswer, { exact: true })).toBeVisible()
  await expect(page.getByText('Final', { exact: true })).toBeVisible()
  expect(new URL(page.url()).searchParams.toString()).toBe('')
  expect(new URL(page.url()).pathname).toBe('/')
  const stored = await page.evaluate(() => Object.entries(localStorage).filter(([key]) => key.startsWith('rehearse.history.v1:')))
  expect(stored).toEqual([[`rehearse.history.v1:${session.id}`, '1']])

  await practice.click()
  await expect(practice).toHaveAttribute('aria-current', 'page')
  await expect(page.getByRole('textbox', { name: 'Your answer' })).toHaveValue('An idle Practice draft survives History navigation.')
  await page.reload()
  await expect(page.getByText('Question 2 of 5', { exact: true })).toBeVisible()
  await expect(page.getByRole('textbox', { name: 'Your answer' })).toHaveValue('')
  await history.click()
  await expect(page.getByRole('button', { name: 'Open session', exact: true })).toHaveCount(1)
  await practice.click()
  await expect(page.getByRole('textbox', { name: 'Your answer' })).toBeEnabled()
  expect(unexpectedRequests).toEqual([])
  expect(errors).toEqual([])
})
