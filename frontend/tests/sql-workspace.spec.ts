import { test, expect, type Page, type Route } from '@playwright/test'
import { json, mockLicense, seedAdminAuth, trackConsoleErrors } from './helpers'

const now = '2026-06-12T06:00:00Z'

function datasource() {
  return {
    id: 'ds-1',
    name: 'warehouse',
    db_type: 'mysql',
    host: 'db.local',
    port: 3306,
    environment: 'sandbox',
    environment_verified: false,
    database: 'app',
    operation_policy: {
      allow_select: true,
      allow_explain: true,
      allow_oracle_plan_table: false,
      allow_dm_explain: false,
      allow_schema_import: false,
      allow_schema_save: false,
      allow_scenario_write: false,
      allow_record_task: false,
    },
    created_at: now,
  }
}

function consoleRow(overrides: Record<string, unknown> = {}) {
  return {
    id: 'console-1',
    name: 'query_1.sql',
    datasource_id: 'ds-1',
    sql: 'SELECT id, name FROM users LIMIT 2',
    pinned: false,
    created_at: now,
    updated_at: now,
    ...overrides,
  }
}

function historyRow() {
  return {
    job_id: 'job-history-1',
    datasource_id: 'ds-1',
    datasource_name: 'warehouse',
    sql: 'SELECT COUNT(*) AS total FROM users',
    sql_hash: 'hash-1',
    status: 'success',
    created_at: now,
    finished_at: now,
    result_set_id: 'rs-history-1',
  }
}

function templateRow() {
  return {
    id: 'template-1',
    name: 'Recent users',
    description: 'Pick a table and limit',
    sql_text: 'SELECT * FROM {{table_name}} LIMIT {{limit}}',
    variables: ['table_name', 'limit'],
    category: 'General',
    project_id: null,
    created_at: now,
    updated_at: now,
  }
}

async function mockWorkspace(page: Page): Promise<{ patches: unknown[]; renders: unknown[] }> {
  const patches: unknown[] = []
  const renders: unknown[] = []
  let jobReads = 0

  await mockLicense(page)
  await page.route(/\/api\/datasources\?/, (r) => json(r, 200, [datasource()]))
  await page.route('**/api/sql/consoles', async (r: Route) => {
    if (r.request().method() === 'GET') return json(r, 200, [consoleRow()])
    if (r.request().method() === 'POST') return json(r, 201, consoleRow({ id: 'console-2', name: 'query_2.sql' }))
    return r.fallback()
  })
  await page.route('**/api/sql/consoles/console-1', async (r: Route) => {
    if (r.request().method() === 'PATCH') {
      const body = r.request().postDataJSON()
      patches.push(body)
      return json(r, 200, consoleRow(body))
    }
    if (r.request().method() === 'DELETE') return r.fulfill({ status: 204 })
    return r.fallback()
  })
  await page.route(/\/api\/sql\/history\?/, (r) => json(r, 200, [historyRow()]))
  await page.route('**/api/sql/templates', (r) => {
    if (r.request().method() === 'GET') return json(r, 200, [templateRow()])
    return r.fallback()
  })
  await page.route(/\/api\/sql\/templates\?/, (r) => json(r, 200, [templateRow()]))
  await page.route('**/api/sql/templates/template-1/render', async (r) => {
    renders.push(r.request().postDataJSON())
    return json(r, 200, { sql_text: 'SELECT * FROM users LIMIT 10' })
  })
  await page.route('**/api/sql/execute', (r) =>
    json(r, 200, { job_id: 'job-1', result_set_id: 'rs-1' }),
  )
  await page.route(/\/api\/jobs\/job-1\/result\?/, (r) =>
    json(r, 200, {
      job_id: 'job-1',
      result_set_id: 'rs-1',
      offset: 0,
      limit: 1000,
      columns: [
        { name: 'id', type: 'integer', driver_type: 'INT', nullable: false, primary_key: true },
        { name: 'name', type: 'string', driver_type: 'VARCHAR', nullable: true, primary_key: false },
      ],
      rows: [{ values: [1, 'Ada'] }, { values: [2, 'Lin'] }],
      loaded_rows: 2,
      total_rows: null,
      state: 'running',
      truncated: false,
    }),
  )
  await page.route('**/api/jobs/job-1', (r) => {
    jobReads += 1
    return json(r, 200, {
      id: 'job-1',
      kind: 'sql',
      status: jobReads <= 1 ? 'running' : 'success',
      created_at: now,
      started_at: now,
      finished_at: jobReads <= 1 ? null : now,
      error: null,
      error_code: null,
      message: null,
      result_set_id: 'rs-1',
    })
  })

  return { patches, renders }
}

let consoleErrors: string[] = []
test.beforeEach(async ({ page }) => {
  consoleErrors = trackConsoleErrors(page)
  await seedAdminAuth(page)
})

function expectNoConsoleErrors(): void {
  expect(consoleErrors, consoleErrors.join('\n')).toEqual([])
}

test('SQL workspace tabs, history, templates, and progressive result render', async ({ page }) => {
  const state = await mockWorkspace(page)

  await page.goto('/projects/project-1/sql')
  await expect(page.getByText('SQL workspace')).toBeVisible()
  await expect(page.locator('aside').getByText('query_1.sql')).toBeVisible()
  await expect(page.locator('main select')).toHaveValue('ds-1')

  await page.locator('button[title="History"]').click()
  await expect(page.getByText('SELECT COUNT(*) AS total FROM users')).toBeVisible()
  await page.getByText('SELECT COUNT(*) AS total FROM users').click()
  await expect.poll(() => state.patches.length).toBeGreaterThan(0)
  expect(state.patches).toContainEqual(
    expect.objectContaining({ sql: 'SELECT COUNT(*) AS total FROM users' }),
  )

  await page.locator('button[title="Templates"]').click()
  await expect(page.getByText('Recent users')).toBeVisible()
  await page.getByText('Recent users').click()
  await page.getByLabel('table_name').fill('users')
  await page.getByLabel('limit').fill('10')
  await page.getByRole('button', { name: 'Insert template' }).click()
  await expect.poll(() => state.renders.length).toBe(1)
  expect(state.renders[0]).toEqual({ values: { table_name: 'users', limit: '10' } })

  await page.getByRole('button', { name: 'Run' }).click()
  await expect(page.getByText(/loaded 2 rows/)).toBeVisible()
  await expect(page.getByText('Ada')).toBeVisible()
  await expect(page.getByText('Lin')).toBeVisible()
  expectNoConsoleErrors()
})
