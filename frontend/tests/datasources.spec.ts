import { expect, test, type Page, type Route } from '@playwright/test'

import { json, mockLicense, seedAdminAuth, trackConsoleErrors } from './helpers'

async function mockDatasourceList(page: Page): Promise<void> {
  await page.route(/\/api\/datasources\?project_id=project-1/, (route) => json(route, 200, []))
}

async function fillDatasourceForm(page: Page): Promise<void> {
  const form = page.locator('form')
  const textInputs = form.locator('input[type="text"]')
  await textInputs.nth(0).fill('warehouse')
  await textInputs.nth(1).fill('db.local')
  await textInputs.nth(2).fill('dataops')
  await form.locator('input[type="password"]').fill('test-only-value')
}

for (const scenario of [
  {
    locale: 'en' as const,
    mode: 'repair',
    title: 'Write actions are disabled in the current license state (view / license update only)',
  },
  {
    locale: 'zh-CN' as const,
    mode: 'in_grace',
    title: '当前 license 状态下写操作已禁用(仅允许查看 / 更新 license)',
  },
]) {
  test(`${scenario.locale}: ${scenario.mode} disables the primary datasource write entry`, async ({
    page,
  }) => {
    const consoleErrors = trackConsoleErrors(page)
    await seedAdminAuth(page, scenario.locale)
    await mockLicense(page, { mode: scenario.mode })
    await mockDatasourceList(page)

    let writes = 0
    page.on('request', (request) => {
      if (!['GET', 'HEAD', 'OPTIONS'].includes(request.method())) writes += 1
    })

    await page.goto('/projects/project-1/datasources')
    const create = page.getByRole('button', {
      name: scenario.locale === 'en' ? 'New datasource' : '新建数据源',
    })
    await expect(create).toBeDisabled()
    await expect(create).toHaveAttribute('title', scenario.title)
    expect(writes).toBe(0)
    expect(consoleErrors, consoleErrors.join('\n')).toEqual([])
  })
}

test('disabled enforcement keeps datasource writes available', async ({ page }) => {
  await seedAdminAuth(page)
  await mockLicense(page, { mode: 'repair', license_enforcement_enabled: false })
  await mockDatasourceList(page)
  let createBody: Record<string, unknown> | null = null
  await page.route('**/api/datasources', async (route: Route) => {
    if (route.request().method() === 'GET') return json(route, 200, [])
    if (route.request().method() !== 'POST') return route.fallback()
    createBody = route.request().postDataJSON()
    return json(route, 201, {
      id: 'ds-created',
      name: 'warehouse',
      db_type: 'mysql',
      host: 'db.local',
      port: 3306,
      username: 'dataops',
      database: null,
      environment: 'unknown',
      environment_verified: false,
      operation_policy: { allow_select: true },
      created_at: '2026-08-21T00:00:00Z',
    })
  })

  await page.goto('/projects/project-1/datasources')
  const create = page.getByRole('button', { name: 'New datasource' })
  await expect(create).toBeEnabled()
  await create.click()
  await fillDatasourceForm(page)
  await page.locator('form').getByRole('button', { name: 'Create' }).click()
  await expect.poll(() => createBody).not.toBeNull()
})

test('stale valid status falls back to a localized license 403 message without console errors', async ({
  page,
}) => {
  const consoleErrors = trackConsoleErrors(page)
  await seedAdminAuth(page)
  await mockLicense(page)
  await mockDatasourceList(page)
  await page.route('**/api/datasources', async (route: Route) => {
    if (route.request().method() === 'GET') return json(route, 200, [])
    if (route.request().method() === 'POST') {
      return json(route, 403, {
        error: 'license_repair_mode',
        message: 'This action is disabled while license repair is required',
      })
    }
    return route.fallback()
  })

  await page.goto('/projects/project-1/datasources')
  await page.getByRole('button', { name: 'New datasource' }).click()
  const form = page.locator('form')
  await fillDatasourceForm(page)
  await form.getByRole('button', { name: 'Create' }).click()

  await expect(form.getByText('Write actions are disabled in the current license state')).toBeVisible()
  expect(consoleErrors, consoleErrors.join('\n')).toEqual([])
})

test('metadata sync runs as a background job and reports what it pulled', async ({ page }) => {
  const consoleErrors = trackConsoleErrors(page)
  await seedAdminAuth(page)
  await mockLicense(page)
  await page.route(/\/api\/datasources\?project_id=project-1/, (route) =>
    json(route, 200, [
      {
        id: 'ds-1',
        project_id: 'project-1',
        name: 'warehouse',
        db_type: 'mysql',
        host: 'db.local',
        port: 3306,
        database: 'demo',
        username: 'dataops',
        environment: 'dev',
        extra: {},
        operation_policy: {
          allow_select: true,
          allow_explain: true,
          allow_dm_explain: false,
          allow_oracle_plan_table: false,
          allow_schema_import: false,
          allow_schema_save: false,
          allow_scenario_write: false,
          allow_record_task: false,
        },
        created_at: '2026-06-13T06:00:00Z',
        updated_at: '2026-06-13T06:00:00Z',
      },
    ]),
  )
  let started = 0
  await page.route('**/api/datasources/ds-1/metadata/sync', (r) => {
    started += 1
    return json(r, 202, { job_id: 'sync-job-1' })
  })
  let polls = 0
  await page.route('**/api/datasources/ds-1/metadata/sync/sync-job-1', (r) => {
    polls += 1
    return polls <= 1
      ? json(r, 200, { job_id: 'sync-job-1', status: 'running', error: null, report: null })
      : json(r, 200, {
          job_id: 'sync-job-1',
          status: 'success',
          error: null,
          report: {
            datasource_id: 'ds-1',
            schema_count: 3,
            synced_tables: 128,
            synced_columns: 1642,
            failed_count: 2,
            failed: [{ schema: 'ods', table: 'broken_view', error: 'AdapterConnectionError' }],
            truncated: false,
            max_tables: 5000,
          },
        })
  })

  await page.goto('/projects/project-1/datasources')
  await page.getByTestId('datasource-sync-ds-1').click()

  const state = page.getByTestId('datasource-sync-state-ds-1')
  await expect(state).toBeVisible()
  // 长跑过程要看得见,不能点完没反应
  await expect(state).toContainText('Syncing')
  // 完成后如实报出拉到多少、失败多少(失败的表跳过而不是整轮失败)
  await expect(state).toContainText('Synced 128 tables / 1642 columns, 2 failed')
  expect(started).toBe(1)
  expect(consoleErrors, consoleErrors.join('\n')).toEqual([])
})
