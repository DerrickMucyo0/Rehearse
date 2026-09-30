import { expect, test } from '@playwright/test'

test('App retains the completed interview until an explicit restart', async ({ page }) => {
  const errors: string[] = []
  const navigations: string[] = []
  const creations: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  page.on('framenavigated', (frame) => {
    if (frame === page.mainFrame()) navigations.push(frame.url())
  })
  page.on('request', (request) => {
    if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/sessions') {
      creations.push(request.url())
    }
  })

  await page.goto('/')
  await expect(page.getByText('Backend connected', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Start Interview', exact: true }).click()
  // Retain the actual DOM node to detect an Interview unmount/remount.
  const interview = await page.getByRole('region', { name: 'Interview practice' }).elementHandle()
  expect(interview).not.toBeNull()

  for (let index = 0; index < 5; index += 1) {
    await expect(page.getByText(`Question ${index + 1} of 5`, { exact: true })).toBeVisible()
    await page.getByRole('textbox', { name: 'Your answer' }).fill(`Integration answer ${index + 1}`)
    const responsePromise = page.waitForResponse((response) =>
      response.request().method() === 'POST' && response.url().endsWith('/answers'))
    await page.getByRole('button', { name: 'Submit Answer', exact: true }).click()
    const response = await responsePromise
    expect(response.status()).toBe(200)
    expect(response.request().postDataJSON()).toEqual({
      question_index: index, answer: `Integration answer ${index + 1}`,
    })
    const session = await response.json()
    expect(session.status).toBe(index === 4 ? 'completed' : 'active')
    expect(session.answers).toHaveLength(index + 1)
    if (index === 4) expect(session.current_question).toBeNull()
  }

  const heading = page.getByRole('heading', { name: 'Interview Complete', exact: true })
  await expect(heading).toBeVisible()
  await expect(page.getByText('You completed all 5 questions.', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Start New Interview', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Start Interview', exact: true })).toHaveCount(0)
  await expect(page.getByRole('textbox')).toHaveCount(0)

  // Observe the idle screen so a delayed reset/reload also fails this test.
  await page.waitForTimeout(2000)
  await expect(heading).toBeVisible()
  expect(await interview!.evaluate((element) => element.isConnected)).toBe(true)
  expect(creations).toHaveLength(1)
  expect(navigations).toHaveLength(1)
  expect(errors).toEqual([])

  const restartResponse = page.waitForResponse((response) =>
    response.request().method() === 'POST' && new URL(response.url()).pathname === '/api/sessions')
  await page.getByRole('button', { name: 'Start New Interview', exact: true }).click()
  expect((await restartResponse).status()).toBe(201)
  await expect(page.getByText('Question 1 of 5', { exact: true })).toBeVisible()
  await expect(page.getByRole('textbox')).toHaveValue('')
  await expect(heading).toHaveCount(0)
  expect(creations).toHaveLength(2)
  expect(navigations).toHaveLength(1)
})
