import { expect, test } from '@playwright/test'

async function forbidProviderTraffic(page: import('@playwright/test').Page) {
  const unexpected: string[] = []
  await page.route('**/*', async (route) => {
    const url = new URL(route.request().url())
    const local = url.hostname === 'localhost' || url.hostname === '127.0.0.1'
    if (!local || url.pathname.endsWith('/audio') || url.pathname.endsWith('/transcriptions')) {
      unexpected.push(local ? 'provider-route' : 'external-host')
      await route.abort()
    } else await route.continue()
  })
  return unexpected
}

test('persisted typed final answers project objective counts and preserve usable Practice and History', async ({ page }) => {
  const unexpected = await forbidProviderTraffic(page)
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  await page.goto('/')
  await expect(page.getByText('Backend connected', { exact: true })).toBeVisible()
  const navigation = page.getByRole('navigation')
  const practice = navigation.getByRole('button', { name: 'Practice', exact: true })
  const history = navigation.getByRole('button', { name: 'History', exact: true })
  const progress = navigation.getByRole('button', { name: 'Progress', exact: true })

  const created = page.waitForResponse((response) => response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/sessions')
  await page.getByRole('button', { name: 'Start Interview', exact: true }).click()
  expect((await created).status()).toBe(201)
  const answer = 'A persisted typed final answer for the Progress browser integration.'
  await page.getByRole('textbox', { name: 'Your answer' }).fill(answer)
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
  await page.getByRole('textbox', { name: 'Your answer' }).fill('The next Practice draft survives both factual views.')

  let summaryRequests = 0
  page.on('request', (request) => {
    if (new URL(request.url()).pathname === '/api/history/summaries') summaryRequests += 1
  })
  await progress.click()
  await expect(progress).toHaveAttribute('aria-current', 'page')
  const dashboard = page.getByRole('region', { name: 'Progress', exact: true })
  const overview = dashboard.getByRole('region', { name: 'Progress overview', exact: true })
  const expected = { 'Completed sessions': '0', 'Active sessions': '1', 'Finalized questions': '1',
    'Saved attempts': '1', 'Saved retries': '0', 'Measured final answers': '0' }
  for (const [label, value] of Object.entries(expected)) {
    await expect(overview.getByText(label, { exact: true }).locator('..').locator('dd')).toHaveText(value)
  }
  const metricNames = ['Estimated WPM', 'Um count', 'Uh count', 'Timed speech span', 'Recognized words']
  for (const name of metricNames) {
    const metric = dashboard.getByRole('region', { name, exact: true })
    await expect(metric.getByRole('cell', { name: 'Unavailable — No measurement', exact: true })).toHaveCount(1)
    await expect(metric.getByRole('cell', { name: 'Question 1', exact: true })).toHaveCount(1)
    await expect(metric.getByRole('cell', { name: 'Question 2', exact: true })).toHaveCount(0)
  }
  expect(summaryRequests).toBe(1)
  await history.click()
  await expect(page.getByRole('button', { name: 'Open session', exact: true })).toHaveCount(1)
  expect(summaryRequests).toBe(1)
  await page.getByRole('button', { name: 'Open session', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Session detail', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Question 1', exact: true }).click()
  await expect(page.getByText(answer, { exact: true })).toBeVisible()
  await expect(page.getByText('Final', { exact: true })).toBeVisible()
  await progress.click()
  await expect(dashboard.getByRole('region', { name: 'Progress overview', exact: true })).toBeVisible()
  expect(summaryRequests).toBe(1)
  await practice.click()
  await expect(page.getByRole('textbox', { name: 'Your answer' })).toHaveValue('The next Practice draft survives both factual views.')
  await expect(page.getByRole('button', { name: 'Submit Attempt', exact: true })).toBeEnabled()
  expect(new URL(page.url()).searchParams.toString()).toBe('')
  expect(unexpected).toEqual([])
  expect(errors).toEqual([])
})

test('isolated persisted measured final fixture preserves zero and excludes its saved open-question attempt', async ({ page }) => {
  const fixtureId = process.env.REHEARSE_E2E_MEASURED_SESSION_ID
  test.skip(!fixtureId, 'An isolated provider-free PostgreSQL fixture was not supplied.')
  expect(fixtureId).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/)
  const unexpected = await forbidProviderTraffic(page)
  await page.addInitScript((id: string) => {
    localStorage.setItem(`rehearse.history.v1:${id}`, '1')
  }, fixtureId!)
  await page.goto('/')
  await expect(page.getByText('Backend connected', { exact: true })).toBeVisible()
  await page.getByRole('navigation').getByRole('button', { name: 'Progress', exact: true }).click()
  const dashboard = page.getByRole('region', { name: 'Progress', exact: true })
  const overview = dashboard.getByRole('region', { name: 'Progress overview', exact: true })
  await expect(overview.getByText('Finalized questions', { exact: true }).locator('..').locator('dd')).toHaveText('1')
  await expect(overview.getByText('Measured final answers', { exact: true }).locator('..').locator('dd')).toHaveText('1')
  await expect(overview.getByText('Saved attempts', { exact: true }).locator('..').locator('dd')).toHaveText('2')
  const metrics = { 'Estimated WPM': '57.6', 'Um count': '0', 'Uh count': '1', 'Timed speech span': '12.5', 'Recognized words': '12' }
  for (const [name, value] of Object.entries(metrics)) {
    const metric = dashboard.getByRole('region', { name, exact: true })
    await expect(metric.getByText('Measurement version: speaking-metrics-v1', { exact: true })).toBeVisible()
    await expect(metric.getByText('Source: Original transcription', { exact: true })).toBeVisible()
    const finalRow = metric.getByRole('row').filter({ has: page.getByRole('cell', { name: 'Question 1', exact: true }) })
    await expect(finalRow.locator('td').last()).toHaveText(value)
    await expect(metric.getByRole('cell', { name: 'Question 1', exact: true })).toHaveCount(1)
    await expect(metric.getByRole('cell', { name: 'Question 2', exact: true })).toHaveCount(0)
  }
  await expect(dashboard.getByText(fixtureId!, { exact: false })).toHaveCount(0)
  expect(unexpected).toEqual([])
})
