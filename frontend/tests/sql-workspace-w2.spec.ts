import { test, expect, type Page, type Route } from '@playwright/test'
import { json, mockLicense, seedAdminAuth, trackConsoleErrors } from './helpers'

/**
 * W2 SQL workspace —— 元数据浏览器 / 工具栏(format / expand-star / explain)/
 * 结果三 tab(Result/Plan/Stats)/ 4 格式导出 + 一次性 token 下载。
 *
 * ★ 所有 mock 响应字段锚自后端契约测试 tests/contract/test_api.py:
 *   - MetadataSchemaItem  : { name }
 *   - MetadataTableItem   : { schema_name, name, table_type }
 *   - MetadataColumnItem  : { name, type, driver_type, nullable, primary_key, comment }
 *   - SqlFormatResponse   : { formatted_sql }
 *   - SqlExpandStarResponse: { expanded_sql }
 *   - /sql/explain 202    : { job_id, result_set_id }(计划落普通 ResultSet)
 *   - ExportCreateResponse: { job_id, download_token, expires_at, format, filename }
 *   - /exports/{token}    : 二进制 + Content-Disposition;一次性,二次 410
 */

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
    sql: 'SELECT * FROM users',
    pinned: false,
    created_at: now,
    updated_at: now,
    ...overrides,
  }
}

async function mockBase(page: Page): Promise<{ patches: unknown[] }> {
  const patches: unknown[] = []
  await mockLicense(page)
  await page.route(/\/api\/datasources\?/, (r) => json(r, 200, [datasource()]))
  await page.route('**/api/sql/consoles', (r: Route) =>
    r.request().method() === 'GET' ? json(r, 200, [consoleRow()]) : r.fallback(),
  )
  await page.route('**/api/sql/consoles/console-1', (r: Route) => {
    if (r.request().method() === 'PATCH') {
      const body = r.request().postDataJSON()
      patches.push(body)
      return json(r, 200, consoleRow(body))
    }
    return r.fallback()
  })
  return { patches }
}

let consoleErrors: string[] = []
test.beforeEach(async ({ page }) => {
  consoleErrors = trackConsoleErrors(page)
  await seedAdminAuth(page)
})
function expectNoConsoleErrors(): void {
  expect(consoleErrors, consoleErrors.join('\n')).toEqual([])
}

test('metadata browser drills schema → table → columns and inserts SELECT', async ({ page }) => {
  const { patches } = await mockBase(page)
  await page.route(/\/api\/datasources\/ds-1\/metadata\/schemas/, (r) =>
    json(r, 200, [{ name: 'app' }]),
  )
  await page.route(/\/api\/datasources\/ds-1\/metadata\/tables/, (r) =>
    json(r, 200, [{ schema_name: 'app', name: 'users', table_type: 'BASE TABLE' }]),
  )
  await page.route(/\/api\/datasources\/ds-1\/metadata\/columns/, (r) =>
    json(r, 200, [
      {
        name: 'id',
        type: 'integer',
        driver_type: 'INT',
        nullable: false,
        primary_key: true,
        comment: 'primary key',
      },
      {
        name: 'name',
        type: 'string',
        driver_type: 'VARCHAR(64)',
        nullable: true,
        primary_key: false,
        comment: null,
      },
    ]),
  )

  await page.goto('/projects/project-1/sql')
  await page.locator('button[title="Metadata"]').click()
  const tree = page.locator('aside')
  await expect(tree.getByText('app', { exact: true })).toBeVisible()

  await tree.getByText('app', { exact: true }).click()
  await expect(tree.getByRole('button', { name: 'users', exact: true })).toBeVisible()

  // 展开列(点表名前的箭头按钮)
  await page.locator('button[title="Show columns"]').click()
  await expect(tree.getByText('id', { exact: true })).toBeVisible()
  await expect(tree.getByText('integer', { exact: true })).toBeVisible()

  // 点表名 → 写入 SELECT * FROM app.users LIMIT 100
  await tree.getByRole('button', { name: 'users', exact: true }).click()
  await expect.poll(() => patches.length).toBeGreaterThan(0)
  expect(patches).toContainEqual(
    expect.objectContaining({ sql: 'SELECT * FROM app.users LIMIT 100' }),
  )
  expectNoConsoleErrors()
})

test('metadata probe failure shows error without blanking the tree', async ({ page }) => {
  await mockBase(page)
  await page.route(/\/api\/datasources\/ds-1\/metadata\/schemas/, (r) =>
    json(r, 503, { error: 'metadata_probe_failed', message: 'Datasource metadata probe failed' }),
  )

  await page.goto('/projects/project-1/sql')
  await page.locator('button[title="Metadata"]').click()
  await expect(page.getByText('Datasource metadata probe failed')).toBeVisible()
  expectNoConsoleErrors()
})

test('format and expand-star rewrite the editor SQL', async ({ page }) => {
  await mockBase(page)
  await page.route('**/api/sql/format', (r) =>
    json(r, 200, { formatted_sql: 'SELECT\n  *\nFROM users' }),
  )
  await page.route('**/api/sql/expand-star', (r) =>
    json(r, 200, { expanded_sql: 'SELECT id, name FROM users' }),
  )

  await page.goto('/projects/project-1/sql')
  await page.getByRole('button', { name: 'Format' }).click()
  await expect.poll(async () => (await page.locator('.monaco-editor').innerText()).includes('FROM')).toBeTruthy()

  await page.getByRole('button', { name: 'Expand *' }).click()
  await expect.poll(async () => (await page.locator('.monaco-editor').innerText()).includes('name')).toBeTruthy()
  expectNoConsoleErrors()
})

test('expand-star cache miss surfaces a refresh-metadata hint', async ({ page }) => {
  await mockBase(page)
  await page.route('**/api/sql/expand-star', (r) =>
    json(r, 409, { error: 'metadata_cache_missing', message: 'Column metadata cache is missing' }),
  )

  await page.goto('/projects/project-1/sql')
  await page.getByRole('button', { name: 'Expand *' }).click()
  await expect(page.getByText(/Refresh the metadata browser first/)).toBeVisible()
  expectNoConsoleErrors()
})

test('explain runs a job and renders the plan in the Plan tab', async ({ page }) => {
  await mockBase(page)
  let planReads = 0
  await page.route('**/api/sql/explain', (r) =>
    json(r, 202, { job_id: 'plan-job-1', result_set_id: 'rs-plan-1' }),
  )
  await page.route('**/api/jobs/plan-job-1', (r) => {
    planReads += 1
    return json(r, 200, {
      id: 'plan-job-1',
      kind: 'sql_explain',
      status: planReads <= 1 ? 'running' : 'success',
      created_at: now,
      finished_at: planReads <= 1 ? null : now,
      error: null,
      error_code: null,
      message: null,
      result_set_id: 'rs-plan-1',
    })
  })
  await page.route(/\/api\/jobs\/plan-job-1\/result\?/, (r) =>
    json(r, 200, {
      job_id: 'plan-job-1',
      result_set_id: 'rs-plan-1',
      offset: 0,
      limit: 1000,
      columns: [
        { name: 'id', type: 'unknown', driver_type: null, nullable: true, primary_key: false },
        { name: 'select_type', type: 'unknown', driver_type: null, nullable: true, primary_key: false },
        { name: 'table', type: 'unknown', driver_type: null, nullable: true, primary_key: false },
      ],
      rows: [{ values: ['1', 'SIMPLE', 'users'] }],
      loaded_rows: 1,
      total_rows: 1,
      state: 'success',
      truncated: false,
    }),
  )

  await page.goto('/projects/project-1/sql')
  await page.getByRole('button', { name: 'Explain' }).click()
  await expect(page.getByText('SIMPLE')).toBeVisible()
  expectNoConsoleErrors()
})

test('export picks a format, polls the job, then downloads a one-time link', async ({ page }) => {
  await mockBase(page)
  // 先跑一个成功查询,使导出按钮可用。
  await page.route('**/api/sql/execute', (r) =>
    json(r, 200, { job_id: 'job-1', result_set_id: 'rs-1' }),
  )
  await page.route('**/api/jobs/job-1', (r) =>
    json(r, 200, {
      id: 'job-1',
      kind: 'sql',
      status: 'success',
      created_at: now,
      finished_at: now,
      error: null,
      error_code: null,
      message: null,
      result_set_id: 'rs-1',
    }),
  )
  await page.route(/\/api\/jobs\/job-1\/result\?/, (r) =>
    json(r, 200, {
      job_id: 'job-1',
      result_set_id: 'rs-1',
      offset: 0,
      limit: 1000,
      columns: [{ name: 'id', type: 'integer', driver_type: 'INT', nullable: false, primary_key: true }],
      rows: [{ values: [1] }],
      loaded_rows: 1,
      total_rows: 1,
      state: 'success',
      truncated: false,
    }),
  )

  // 导出 job
  await page.route('**/api/jobs/job-1/export', (r) =>
    json(r, 202, {
      job_id: 'export-job-1',
      download_token: 'tok-123',
      expires_at: '2026-06-12T07:00:00Z',
      format: 'csv',
      filename: 'job-1.csv',
    }),
  )
  await page.route('**/api/jobs/export-job-1', (r) =>
    json(r, 200, {
      id: 'export-job-1',
      kind: 'result_export',
      status: 'success',
      created_at: now,
      finished_at: now,
      error: null,
      error_code: null,
      message: null,
      result_set_id: null,
    }),
  )
  let downloadHits = 0
  await page.route('**/api/exports/tok-123', (r) => {
    downloadHits += 1
    if (downloadHits > 1) {
      return json(r, 410, { error: 'download_token_consumed', message: 'used' })
    }
    return r.fulfill({
      status: 200,
      headers: {
        'Content-Type': 'text/csv; charset=utf-8',
        'Content-Disposition': 'attachment; filename="job-1.csv"',
      },
      body: 'id\n1\n',
    })
  })

  await page.goto('/projects/project-1/sql')
  await page.getByRole('button', { name: 'Run' }).click()
  await expect(page.getByText(/Done · /)).toBeVisible()

  await page.getByRole('button', { name: 'Export' }).click()
  await page.getByRole('button', { name: 'csv', exact: true }).click()
  await expect(page.getByText(/Export ready: job-1\.csv/)).toBeVisible()

  const downloadPromise = page.waitForEvent('download')
  await page.getByRole('button', { name: 'Download', exact: true }).click()
  const download = await downloadPromise
  expect(download.suggestedFilename()).toBe('job-1.csv')
  expectNoConsoleErrors()
})

test('export rate limit (429) shows a friendly hint', async ({ page }) => {
  await mockBase(page)
  await page.route('**/api/sql/execute', (r) =>
    json(r, 200, { job_id: 'job-1', result_set_id: 'rs-1' }),
  )
  await page.route('**/api/jobs/job-1', (r) =>
    json(r, 200, {
      id: 'job-1',
      kind: 'sql',
      status: 'success',
      created_at: now,
      finished_at: now,
      error: null,
      error_code: null,
      message: null,
      result_set_id: 'rs-1',
    }),
  )
  await page.route(/\/api\/jobs\/job-1\/result\?/, (r) =>
    json(r, 200, {
      job_id: 'job-1',
      result_set_id: 'rs-1',
      offset: 0,
      limit: 1000,
      columns: [{ name: 'id', type: 'integer', driver_type: 'INT', nullable: false, primary_key: true }],
      rows: [{ values: [1] }],
      loaded_rows: 1,
      total_rows: 1,
      state: 'success',
      truncated: false,
    }),
  )
  await page.route('**/api/jobs/job-1/export', (r) =>
    json(r, 429, { error: 'export_rate_limited', message: 'Export rate limit exceeded' }),
  )

  await page.goto('/projects/project-1/sql')
  await page.getByRole('button', { name: 'Run' }).click()
  await expect(page.getByText(/Done · /)).toBeVisible()

  await page.getByRole('button', { name: 'Export' }).click()
  await page.getByRole('button', { name: 'json', exact: true }).click()
  await expect(page.getByText(/Export rate limit reached/)).toBeVisible()
  expectNoConsoleErrors()
})
