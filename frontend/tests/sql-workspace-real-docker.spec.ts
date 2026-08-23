import { expect, test } from '@playwright/test'

const username = process.env.DATAOPS_REAL_E2E_USERNAME
const password = process.env.DATAOPS_REAL_E2E_PASSWORD
const projectId = process.env.DATAOPS_REAL_E2E_PROJECT_ID
const datasourceId = process.env.DATAOPS_REAL_E2E_DATASOURCE_ID
const expectedResultOrigin = process.env.DATAOPS_REAL_E2E_RESULT_ORIGIN ?? 'job'

test.skip(
  !username || !password || !projectId || !datasourceId,
  'requires an isolated real API/worker/PostgreSQL fixture',
)

test('real Docker backend executes SQL and hands its result to Compare without 429', async ({
  page,
}) => {
  const consoleErrors: string[] = []
  let progressRequests = 0
  let resultRequests = 0
  let rateLimitedResponses = 0
  let captureRequest: Record<string, unknown> | null = null
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text())
  })
  page.on('request', (request) => {
    if (/\/api\/jobs\/[^/]+\/progress/.test(request.url())) progressRequests += 1
    if (/\/api\/jobs\/[^/]+\/result/.test(request.url())) resultRequests += 1
    if (
      request.method() === 'POST' &&
      /\/api\/projects\/[^/]+\/compare\/result-inputs$/.test(request.url())
    ) {
      captureRequest = request.postDataJSON() as Record<string, unknown>
    }
  })
  page.on('response', (response) => {
    if (response.status() === 429) rateLimitedResponses += 1
  })

  await page.goto('/login')
  await page.locator('input[autocomplete="username"]').fill(username as string)
  await page.locator('input[autocomplete="current-password"]').fill(password as string)
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page).toHaveURL(/\/projects$/)

  await page.goto(`/projects/${projectId}/sql`)
  await expect(page.getByText('SQL workspace')).toBeVisible()
  await expect(page.getByTestId('build-version')).toHaveText('v2.0.1-test · working')
  await page.getByTitle('New console').click()
  await expect(page.getByLabel('Datasource')).toHaveValue(datasourceId as string)

  const editor = page.locator('.monaco-editor textarea.inputarea')
  await editor.press('Control+A')
  await editor.type('SELECT 1 AS real_value')
  await page.getByRole('button', { name: 'Run' }).click()

  await expect(page.locator('table.text-data tbody tr')).toHaveCount(1)
  await expect(page.getByRole('cell', { name: '1', exact: true }).last()).toBeVisible()
  await page.getByRole('button', { name: 'Stats' }).click()
  await expect(page.getByText('Driver rows read').locator('..')).toContainText('1')
  await expect(page.getByText('Scan-reducing limit').locator('..')).toContainText('Yes')

  await page.getByRole('button', { name: 'Results' }).click()
  await page.getByTestId('sql-result-to-compare').click()
  await expect(page).toHaveURL(new RegExp(`/projects/${projectId}/compare$`))
  await expect(page.getByTestId('compare-source-result-snapshot')).toBeVisible()
  await expect(page.locator('input[value="real_value"]').first()).toHaveValue('real_value')

  expect(progressRequests).toBeGreaterThan(0)
  expect(resultRequests).toBe(1)
  expect(rateLimitedResponses).toBe(0)
  expect(captureRequest).toMatchObject({
    origin_kind: expectedResultOrigin,
    allow_partial: false,
  })
  expect(consoleErrors).toEqual([])
})
