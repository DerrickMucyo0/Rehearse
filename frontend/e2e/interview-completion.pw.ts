import { expect, test } from '@playwright/test'

test('typed retries preserve attempts and require Continue through completion and reload', async ({ page }) => {
  const errors: string[] = []
  const creations: string[] = []
  const retiredSubmissions: string[] = []
  const providerRoutes: string[] = []
  let externalRequests = 0
  await page.route('**/*', async (route) => {
    const hostname = new URL(route.request().url()).hostname
    if (hostname === 'localhost' || hostname === '127.0.0.1') await route.continue()
    else {
      externalRequests += 1
      await route.abort()
    }
  })
  page.on('pageerror', (error) => errors.push(error.message))
  page.on('request', (request) => {
    const path = new URL(request.url()).pathname
    if (request.method() === 'POST' && path === '/api/sessions') creations.push(request.url())
    if (request.method() === 'POST' && path.endsWith('/answers')) retiredSubmissions.push(request.url())
    if (path.endsWith('/transcriptions') || path.endsWith('/audio')) providerRoutes.push(request.url())
  })

  await page.goto('/')
  await expect(page.getByText('Backend connected', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Start Interview', exact: true }).click()
  await expect(page.getByText('Question 1 of 5', { exact: true })).toBeVisible()
  await page.reload()
  await expect(page.getByRole('textbox', { name: 'Your answer' })).toHaveValue('')
  expect(creations).toHaveLength(1)

  async function submit(questionIndex: number, revision: number, answer: string) {
    await page.getByRole('textbox', { name: 'Your answer' }).fill(answer)
    const result = page.waitForResponse((response) => response.request().method() === 'POST'
      && new URL(response.url()).pathname.endsWith(`/questions/${questionIndex}/attempts`))
    await page.getByRole('button', { name: 'Submit Attempt', exact: true }).click()
    const response = await result
    expect(response.status()).toBe(201)
    expect(response.request().postDataJSON()).toEqual({
      expected_last_attempt_number: revision, answer, measurement_id: null,
    })
    const saved = await response.json()
    expect(saved.attempt.attempt_number).toBe(revision + 1)
    expect(saved.session.current_question_index).toBe(questionIndex)
    expect(saved.session.status).toBe('active')
    expect(saved.session.current_question_latest_attempt_number).toBe(revision + 1)
    await expect(page.getByRole('button', { name: 'Continue', exact: true })).toBeVisible()
    await expect(page.getByRole('heading', { name: `Attempt ${revision + 1}`, exact: true })).toBeVisible()
    await expect(page.getByText(`Question ${questionIndex + 1} of 5`, { exact: true })).toBeVisible()
    await expect(page.getByRole('textbox')).toHaveCount(0)
  }
  async function continueQuestion(questionIndex: number, revision: number) {
    const result = page.waitForResponse((response) => response.request().method() === 'POST'
      && new URL(response.url()).pathname.endsWith(`/questions/${questionIndex}/continue`))
    await page.getByRole('button', { name: 'Continue', exact: true }).click()
    const response = await result
    expect(response.status()).toBe(200)
    expect(response.request().postDataJSON()).toEqual({ expected_last_attempt_number: revision })
    const session = await response.json()
    expect(session.status).toBe(questionIndex === 4 ? 'completed' : 'active')
    expect(session.answers).toHaveLength(questionIndex + 1)
    if (questionIndex === 4) expect(session.current_question).toBeNull()
  }

  await submit(0, 0, 'Integration baseline answer')
  await expect(page.getByRole('table')).toHaveCount(0)
  await page.reload()
  await expect(page.getByRole('heading', { name: 'Attempt 1', exact: true })).toBeVisible()
  await expect(page.getByRole('textbox')).toHaveCount(0)
  await page.getByRole('button', { name: 'Retry', exact: true }).click()
  await expect(page.getByRole('textbox')).toHaveValue('')
  await expect(page.getByText('Integration baseline answer', { exact: true })).toBeVisible()
  await submit(0, 1, 'Integration retry answer')
  await expect(page.getByRole('heading', { name: 'Attempt 1', exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Attempt 2', exact: true })).toBeVisible()
  await expect(page.getByText('Integration baseline answer', { exact: true })).toBeVisible()
  await expect(page.getByText('Integration retry answer', { exact: true })).toBeVisible()
  const comparison = page.getByRole('table')
  await expect(comparison).toBeVisible()
  for (const name of ['Before', 'After', 'Change']) {
    await expect(comparison.getByRole('columnheader', { name, exact: true })).toBeVisible()
  }
  await expect(comparison.getByRole('row', { name: /Recognized words/ }).getByRole('cell').first()).toContainText('Unavailable')
  await continueQuestion(0, 2)
  await expect(page.getByText('Question 2 of 5', { exact: true })).toBeVisible()
  await expect(page.getByRole('table')).toHaveCount(0)

  for (let index = 1; index < 5; index += 1) {
    await expect(page.getByText(`Question ${index + 1} of 5`, { exact: true })).toBeVisible()
    await submit(index, 0, `Integration answer ${index + 1}`)
    await expect(page.getByRole('heading', { name: 'Interview Complete', exact: true })).toHaveCount(0)
    await continueQuestion(index, 1)
  }

  const heading = page.getByRole('heading', { name: 'Interview Complete', exact: true })
  await expect(heading).toBeVisible()
  await expect(page.getByText('You completed all 5 questions.', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Start New Interview', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Start Interview', exact: true })).toHaveCount(0)
  await expect(page.getByRole('textbox')).toHaveCount(0)
  await expect(page.getByRole('button', { name: /^Retry/ })).toHaveCount(0)
  await page.reload()
  await expect(heading).toBeVisible()
  await expect(page.getByRole('textbox')).toHaveCount(0)
  expect(creations).toHaveLength(1)
  expect(retiredSubmissions).toEqual([])
  expect(providerRoutes).toEqual([])
  expect(externalRequests).toBe(0)
  expect(errors).toEqual([])

  const restartResponse = page.waitForResponse((response) => response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/sessions')
  await page.getByRole('button', { name: 'Start New Interview', exact: true }).click()
  expect((await restartResponse).status()).toBe(201)
  await expect(page.getByText('Question 1 of 5', { exact: true })).toBeVisible()
  await expect(page.getByRole('textbox')).toHaveValue('')
  await expect(heading).toHaveCount(0)
  expect(creations).toHaveLength(2)
})
