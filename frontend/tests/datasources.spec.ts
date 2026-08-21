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
