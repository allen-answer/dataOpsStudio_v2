import { test, expect, type Page, type Route } from '@playwright/test'
import { json, mockLicense, seedAdminAuth, trackConsoleErrors } from './helpers'

const now = '2026-06-12T06:00:00Z'

function datasource(overrides: Record<string, unknown> = {}) {
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
    ...overrides,
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

async function mockWorkspace(
  page: Page,
  options: {
    datasource?: Record<string, unknown>
    jobCreatedAt?: string
    jobFinishedAt?: string
    progressiveRows?: number
  } = {},
): Promise<{
  patches: unknown[]
  renders: unknown[]
  executeRequests: Record<string, unknown>[]
  resultRequestUrls: string[]
  getJobReads: () => number
}> {
  const patches: unknown[] = []
  const renders: unknown[] = []
  const executeRequests: Record<string, unknown>[] = []
  const resultRequestUrls: string[] = []
  let jobReads = 0
  const jobCreatedAt = options.jobCreatedAt ?? now
  const jobFinishedAt = options.jobFinishedAt ?? now

  await mockLicense(page)
  await page.route(/\/api\/datasources\?/, (r) =>
    json(r, 200, [datasource(options.datasource)]),
  )
  await page.route('**/api/datasources/ds-1/metadata/schemas', (r) => json(r, 200, []))
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
  await page.route('**/api/sql/execute', (r) => {
    executeRequests.push(r.request().postDataJSON())
    return json(r, 200, { job_id: 'job-1', result_set_id: 'rs-1' })
  })
  await page.route(/\/api\/jobs\/job-1\/result\?/, (r) => {
    resultRequestUrls.push(r.request().url())
    if (options.progressiveRows !== undefined) {
      const rowCount = options.progressiveRows
      return json(r, 200, {
        job_id: 'job-1',
        result_set_id: 'rs-1',
        offset: 0,
        limit: 100,
        columns: [
          { name: 'id', type: 'integer', driver_type: 'INT', nullable: false, primary_key: true },
        ],
        rows: Array.from({ length: rowCount }, (_, index) => ({ values: [index + 1] })),
        loaded_rows: rowCount,
        total_rows: null,
        state: jobReads <= 1 ? 'running' : 'success',
        truncated: false,
      })
    }
    return json(r, 200, {
      job_id: 'job-1',
      result_set_id: 'rs-1',
      offset: 0,
      limit: 100,
      columns: [
        { name: 'id', type: 'integer', driver_type: 'INT', nullable: false, primary_key: true },
        { name: 'name', type: 'string', driver_type: 'VARCHAR', nullable: true, primary_key: false },
      ],
      rows: [{ values: [1, 'Ada'] }, { values: [2, 'Lin'] }],
      loaded_rows: 2,
      total_rows: null,
      state: 'running',
      truncated: false,
    })
  })
  await page.route('**/api/jobs/job-1', (r) => {
    jobReads += 1
    return json(r, 200, {
      id: 'job-1',
      kind: 'sql',
      status: jobReads <= 1 ? 'running' : 'success',
      created_at: jobCreatedAt,
      started_at: now,
      finished_at: jobReads <= 1 ? null : jobFinishedAt,
      error: null,
      error_code: null,
      message: null,
      result_set_id: 'rs-1',
    })
  })

  return { patches, renders, executeRequests, resultRequestUrls, getJobReads: () => jobReads }
}

let consoleErrors: string[] = []
test.beforeEach(async ({ page }) => {
  consoleErrors = trackConsoleErrors(page)
  await seedAdminAuth(page)
})

function expectNoConsoleErrors(): void {
  expect(consoleErrors, consoleErrors.join('\n')).toEqual([])
}

test('SQL execution sends the default maximum row limit', async ({ page }) => {
  const state = await mockWorkspace(page)

  await page.goto('/projects/project-1/sql')
  await expect(page.getByLabel('Max rows')).toHaveValue('1000')
  await page.getByRole('button', { name: 'Run' }).click()

  await expect.poll(() => state.executeRequests.length).toBe(1)
  await expect.poll(() => state.getJobReads()).toBeGreaterThanOrEqual(2)
  expect(state.executeRequests[0]).toMatchObject({ max_rows: 1000 })
  expectNoConsoleErrors()
})

test('SQL execution accepts a custom maximum row limit', async ({ page }) => {
  const state = await mockWorkspace(page)

  await page.goto('/projects/project-1/sql')
  await page.getByLabel('Max rows').selectOption('custom')
  await page.getByLabel('Custom maximum rows').fill('2500')
  await page.getByRole('button', { name: 'Run' }).click()

  await expect.poll(() => state.executeRequests.length).toBe(1)
  await expect.poll(() => state.getJobReads()).toBeGreaterThanOrEqual(2)
  expect(state.executeRequests[0]).toMatchObject({ max_rows: 2500 })
  expectNoConsoleErrors()
})

test('SQL workspace tabs, history, templates, and progressive result render', async ({ page }) => {
  const state = await mockWorkspace(page)

  await page.goto('/projects/project-1/sql')
  await expect(page.getByText('SQL workspace')).toBeVisible()
  await expect(page.locator('aside').getByText('query_1.sql')).toBeVisible()
  await expect(page.getByLabel('Datasource')).toHaveValue('ds-1')

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
  await expect(page.getByRole('cell', { name: 'Lin', exact: true })).toBeVisible()
  expectNoConsoleErrors()
})

test('editor and result panels share the viewport without page scrolling', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 720 })
  await mockWorkspace(page, { progressiveRows: 100 })

  await page.goto('/projects/project-1/sql')
  await page.getByRole('button', { name: 'Run' }).click()
  await expect(page.getByText(/loaded 100 rows/)).toBeVisible()

  const editorPanel = page.getByTestId('sql-editor-panel')
  const resultPanel = page.getByTestId('sql-result-panel')
  await expect(editorPanel).toBeVisible()
  await expect(resultPanel).toBeInViewport()

  const editorBox = await editorPanel.boundingBox()
  const resultBox = await resultPanel.boundingBox()
  expect(editorBox).not.toBeNull()
  expect(resultBox).not.toBeNull()
  expect(editorBox!.height).toBeLessThan(page.viewportSize()!.height / 2)
  expect(resultBox!.y).toBeGreaterThan(editorBox!.y + editorBox!.height)
  expect(resultBox!.y + resultBox!.height).toBeLessThanOrEqual(page.viewportSize()!.height)
  expect(await page.evaluate(() => window.scrollY)).toBe(0)
  expectNoConsoleErrors()
})

test('progressive results use bounded pages and polling stops at success', async ({ page }) => {
  const state = await mockWorkspace(page, { progressiveRows: 100 })

  await page.goto('/projects/project-1/sql')
  await page.getByRole('button', { name: 'Run' }).click()
  await expect(page.getByText(/loaded 100 rows/)).toBeVisible()
  await expect.poll(() => state.resultRequestUrls.length).toBeGreaterThan(0)

  expect(
    state.resultRequestUrls.every(
      (url) => new URL(url).searchParams.get('limit') === '100',
    ),
  ).toBe(true)
  await expect(page.locator('table.text-data tbody tr')).toHaveCount(100)

  await expect.poll(() => state.getJobReads()).toBe(2)
  const terminalJobReads = state.getJobReads()
  await page.waitForTimeout(1_200)
  expect(state.getJobReads()).toBe(terminalJobReads)
  expectNoConsoleErrors()
})

test('DM datasource remains executable', async ({ page }) => {
  await mockWorkspace(page, { datasource: { db_type: 'dm' } })

  await page.goto('/projects/project-1/sql')
  await expect(page.getByText('SQL workspace')).toBeVisible()
  await expect(page.getByLabel('Datasource')).toHaveValue('ds-1')
  await expect(page.getByText(/Execution currently supports MySQL \/ DM/)).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Run' })).toBeEnabled()
  expectNoConsoleErrors()
})

test('completed query freezes elapsed seconds after terminal status', async ({ page }) => {
  await page.clock.install({ time: new Date(now) })
  await mockWorkspace(page, {
    jobCreatedAt: '2026-06-12T06:00:00.000Z',
    jobFinishedAt: '2026-06-12T06:00:04.200Z',
  })

  await page.goto('/projects/project-1/sql')
  await page.getByRole('button', { name: 'Run' }).click()
  await expect(page.getByText(/Running.*loaded 2 rows/)).toBeVisible()

  await page.clock.runFor(500)
  const summary = page.getByText(/Done · \d+\.\ds · 2 rows/)
  await expect(summary).toHaveText('Done · 4.2s · 2 rows')
  const frozenSummary = await summary.textContent()

  await page.clock.runFor(5000)
  await expect(summary).toHaveText(frozenSummary ?? '')
  expectNoConsoleErrors()
})

test('terminal elapsed seconds use job timestamps instead of local clock', async ({ page }) => {
  await page.clock.install({ time: new Date('2031-01-01T00:00:00.000Z') })
  await mockWorkspace(page, {
    jobCreatedAt: '2026-06-12T06:00:10.000Z',
    jobFinishedAt: '2026-06-12T06:00:12.500Z',
  })

  await page.goto('/projects/project-1/sql')
  await page.getByRole('button', { name: 'Run' }).click()
  await page.clock.runFor(500)

  await expect(page.getByText(/Done · \d+\.\ds · 2 rows/)).toHaveText(
    'Done · 2.5s · 2 rows',
  )
  expectNoConsoleErrors()
})
