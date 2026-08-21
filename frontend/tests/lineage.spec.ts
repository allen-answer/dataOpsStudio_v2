import { test, expect, type Page } from '@playwright/test'
import { deferred, json, mockLicense, seedAdminAuth, trackConsoleErrors } from './helpers'

const now = '2026-06-13T06:00:00Z'

async function mockBase(page: Page): Promise<void> {
  await mockLicense(page)
  await page.route(/\/api\/datasources\?/, (r) => json(r, 200, []))
  await page.route(/\/api\/jobs\?/, (r) => json(r, 200, []))
}

let consoleErrors: string[] = []
test.beforeEach(async ({ page }) => {
  consoleErrors = trackConsoleErrors(page)
  await seedAdminAuth(page)
})

function expectNoConsoleErrors(): void {
  expect(consoleErrors, consoleErrors.join('\n')).toEqual([])
}

test('pending lineage batch response cannot restart polling after route unmount', async ({
  page,
}) => {
  await mockBase(page)
  await page.route(/\/api\/projects\/project-1\/uploads\?/, (r) =>
    json(r, 201, {
      upload_id: 'upload-1',
      project_id: 'project-1',
      purpose: 'lineage_batch',
      filename: 'batch.sql',
      content_type: 'text/plain',
      bytes: 9,
      created_at: now,
    }),
  )
  await page.route('**/api/projects/project-1/lineage/batch', (r) =>
    json(r, 202, { job_id: 'batch-job-pending' }),
  )

  let batchReads = 0
  const firstBatchRead = deferred()
  const batchResponseGate = deferred()
  await page.route(
    '**/api/projects/project-1/lineage/batch/batch-job-pending',
    async (r) => {
      batchReads += 1
      firstBatchRead.resolve()
      await batchResponseGate.promise
      await json(r, 200, {
        job_id: 'batch-job-pending',
        status: 'running',
        error: null,
        report: null,
      })
    },
  )

  await page.goto('/projects/project-1/lineage')
  await page.getByRole('button', { name: 'Batch', exact: true }).click()
  await page.locator('input[type="file"]').setInputFiles({
    name: 'batch.sql',
    mimeType: 'text/plain',
    buffer: Buffer.from('select 1;'),
  })
  await page.getByRole('button', { name: 'Upload & analyze' }).click()
  await firstBatchRead.promise

  // Keep the same project id in the new route so an accidentally rescheduled
  // stale poll cannot be masked by LineageView's projectId guard.
  await page.getByRole('button', { name: 'Jobs', exact: true }).click()
  await expect(page).toHaveURL(/\/projects\/project-1\/jobs$/)
  batchResponseGate.resolve()

  // Lineage polls every 2 s. A stale response used to schedule a second request here.
  await page.waitForTimeout(2500)
  expect(batchReads).toBe(1)
  expectNoConsoleErrors()
})

test('repair mode keeps lineage reads available while disabling SQL analysis', async ({ page }) => {
  await mockBase(page)
  await mockLicense(page, { mode: 'repair' })
  await page.route(/\/api\/datasources\?/, (route) =>
    json(route, 200, [
      {
        id: 'ds-1',
        name: 'warehouse',
        db_type: 'mysql',
        host: 'db.local',
        port: 3306,
        environment: 'sandbox',
        environment_verified: false,
        database: 'app',
        operation_policy: { allow_select: true },
        created_at: now,
      },
    ]),
  )

  await page.goto('/projects/project-1/lineage')
  await page.getByRole('button', { name: 'SQL analyze', exact: true }).click()
  const analyze = page.getByRole('button', { name: 'Analyze', exact: true })
  await expect(analyze).toBeDisabled()
  await expect(analyze).toHaveAttribute(
    'title',
    'Write actions are disabled in the current license state (view / license update only)',
  )
  expectNoConsoleErrors()
})
