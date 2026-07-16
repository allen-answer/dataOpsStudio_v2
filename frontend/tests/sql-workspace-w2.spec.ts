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

test('typing schema dot suggests tables from the selected datasource', async ({ page }) => {
  await mockBase(page)
  const tableRequests: string[] = []
  await page.route(/\/api\/datasources\/ds-1\/metadata\/tables/, (route) => {
    tableRequests.push(route.request().url())
    return json(route, 200, [
      { schema_name: 'app', name: 'customers', table_type: 'BASE TABLE' },
      { schema_name: 'app', name: 'orders', table_type: 'BASE TABLE' },
    ])
  })

  await page.goto('/projects/project-1/sql')
  await expect(page.locator('main select')).toHaveValue('ds-1')
  const input = page.locator('.monaco-editor textarea.inputarea')
  await input.press('Control+A')
  await input.type('SELECT * FROM app')
  await input.press('.')

  const suggestions = page.locator('.suggest-widget')
  await expect
    .poll(async () => (await page.locator('.monaco-editor').innerText()).includes('app.'))
    .toBe(true)
  await expect.poll(() => tableRequests.length).toBe(1)
  await expect(suggestions).toBeVisible()
  await expect(suggestions.getByText('customers', { exact: true })).toBeVisible()
  await expect(suggestions.getByText('orders', { exact: true })).toBeVisible()
  expect(tableRequests).toHaveLength(1)
  expect(new URL(tableRequests[0]).searchParams.get('schema')).toBe('app')

  await input.press('Escape')
  await input.press('Control+A')
  await input.type('SELECT * FROM "app"')
  await input.press('.')
  await expect(suggestions.getByText('customers', { exact: true })).toBeVisible()
  expect(tableRequests).toHaveLength(1)
  expectNoConsoleErrors()
})

test('completion drops tables returned after the datasource changes', async ({ page }) => {
  await mockLicense(page)
  await page.route(/\/api\/datasources\?/, (route) =>
    json(route, 200, [datasource(), datasource({ id: 'ds-2', name: 'reporting' })]),
  )
  await page.route('**/api/sql/consoles', (route) =>
    json(route, 200, [consoleRow()]),
  )
  await page.route('**/api/sql/consoles/console-1', async (route) => {
    if (route.request().method() === 'PATCH') {
      return json(route, 200, consoleRow(route.request().postDataJSON()))
    }
    return route.fallback()
  })

  let markFirstStarted: () => void = () => undefined
  let releaseFirst: () => void = () => undefined
  const firstStarted = new Promise<void>((resolve) => {
    markFirstStarted = resolve
  })
  const firstReleased = new Promise<void>((resolve) => {
    releaseFirst = resolve
  })
  await page.route(/\/api\/datasources\/ds-1\/metadata\/tables/, async (route) => {
    markFirstStarted()
    await firstReleased
    return json(route, 200, [
      { schema_name: 'app', name: 'stale_table', table_type: 'BASE TABLE' },
    ])
  })
  await page.route(/\/api\/datasources\/ds-2\/metadata\/tables/, (route) =>
    json(route, 200, [
      { schema_name: 'app', name: 'current_table', table_type: 'BASE TABLE' },
    ]),
  )

  await page.goto('/projects/project-1/sql')
  const datasourceSelect = page.locator('main select')
  await expect(datasourceSelect).toHaveValue('ds-1')
  const input = page.locator('.monaco-editor textarea.inputarea')
  await input.press('Control+A')
  await input.type('SELECT * FROM app')
  await input.press('.')
  await firstStarted

  await datasourceSelect.selectOption('ds-2')
  releaseFirst()
  await input.press('Control+A')
  await input.type('SELECT * FROM app')
  await input.press('.')

  const suggestions = page.locator('.suggest-widget')
  await expect(suggestions.getByText('current_table', { exact: true })).toBeVisible()
  await expect(suggestions.getByText('stale_table', { exact: true })).toHaveCount(0)
  expectNoConsoleErrors()
})

test('Monaco tokenizes SQL keywords and identifiers with distinct styles', async ({ page }) => {
  await mockLicense(page)
  await page.route(/\/api\/datasources\?/, (route) => json(route, 200, [datasource()]))
  await page.route('**/api/sql/consoles', (route) =>
    json(route, 200, [consoleRow({ sql: 'SELECT customer_id FROM app.customers' })]),
  )

  await page.goto('/projects/project-1/sql')
  const viewLines = page.locator('.monaco-editor .view-lines')
  await expect(viewLines).toContainText('SELECT')
  const tokens = await viewLines.evaluate((root) =>
    Array.from(root.querySelectorAll('span[class*="mtk"]'))
      .map((node) => ({ text: node.textContent ?? '', className: node.className }))
      .filter((token) => token.text.trim().length > 0),
  )

  expect(tokens.some((token) => token.text.includes('SELECT'))).toBe(true)
  expect(new Set(tokens.map((token) => token.className)).size).toBeGreaterThan(1)
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

test('AI SQL metadata failure shows actionable localized guidance', async ({ page }) => {
  await mockBase(page)
  await page.route('**/api/datasources/ds-1/ai/sql-table-candidates', (r) =>
    json(r, 503, {
      error: 'metadata_probe_failed',
      message: 'Datasource metadata probe failed',
    }),
  )

  await page.goto('/projects/project-1/sql')
  await page.getByRole('button', { name: 'AI Generate' }).first().click()
  await page.getByRole('textbox', { name: 'Query request' }).fill('list customers')
  await page.getByRole('button', { name: 'Recommend tables' }).click()

  await expect(page.getByText(/test the connection and refresh metadata/i)).toBeVisible()
  await expect(page.getByText('Datasource metadata probe failed')).toHaveCount(0)
  expectNoConsoleErrors()
})

test('AI assistant confirms tables, previews, and applies without executing', async ({ page }) => {
  const { patches } = await mockBase(page)
  let executeCalls = 0
  await page.route('**/api/sql/execute', (r) => {
    executeCalls += 1
    return json(r, 500, { error: 'must_not_execute' })
  })
  await page.route('**/api/datasources/ds-1/ai/sql-table-candidates', (r) =>
    json(r, 200, {
      candidates: [
        { schema_name: 'app', table_name: 'users', matched_by: ['table_name'] },
        { schema_name: 'app', table_name: 'orders', matched_by: ['column_name'] },
      ],
      truncated: false,
    }),
  )
  await page.route('**/api/datasources/ds-1/ai/sql-generate', (r) =>
    json(r, 200, {
      ok: true,
      sql: 'SELECT id, name FROM app.users',
      explanation: null,
      provider: 'mock',
      model: 'mock-model',
      error: null,
      egress_level: 2,
      tables_used: ['app.users'],
      truncated: false,
      stage: 'validated',
      diagnostic_code: null,
      attempts: 1,
      reasoning_mode: 'disabled',
      validation: { readonly: 'passed', tables: 'passed', columns: 'passed', warnings: [] },
      request_id: 'request-1',
    }),
  )

  await page.goto('/projects/project-1/sql')
  await page.getByRole('button', { name: 'AI Generate' }).click()
  await expect(page.getByRole('complementary', { name: 'AI SQL Assistant' })).toBeVisible()
  await page.getByLabel('Query request').fill('list user names')
  await page.getByRole('button', { name: 'Recommend tables' }).click()
  await expect(page.getByRole('checkbox', { name: 'app.users' })).toBeChecked()
  await page.getByRole('button', { name: 'Generate preview' }).click()
  await expect(page.getByText('SELECT id, name FROM app.users', { exact: true })).toBeVisible()
  expect(await page.locator('.monaco-editor').innerText()).not.toContain('id, name')
  await page.getByRole('button', { name: 'Apply to editor' }).click()
  await expect.poll(() => patches.some((p) => JSON.stringify(p).includes('id, name'))).toBeTruthy()
  expect(executeCalls).toBe(0)
  expectNoConsoleErrors()
})

test('AI assistant revises only the current draft and shows diagnostic guidance', async ({ page }) => {
  await mockBase(page)
  const requests: Record<string, unknown>[] = []
  await page.route('**/api/datasources/ds-1/ai/sql-table-candidates', (r) =>
    json(r, 200, {
      candidates: [{ schema_name: 'app', table_name: 'users', matched_by: ['table_name'] }],
      truncated: false,
    }),
  )
  await page.route('**/api/datasources/ds-1/ai/sql-generate', (r) => {
    requests.push(r.request().postDataJSON())
    if (requests.length === 1) {
      return json(r, 200, {
        ok: true, sql: 'SELECT id FROM app.users', explanation: null, provider: 'mock', model: 'm',
        error: null, egress_level: 2, tables_used: ['app.users'], truncated: false,
        stage: 'validated', diagnostic_code: null, attempts: 1, reasoning_mode: 'disabled',
        validation: { readonly: 'passed', tables: 'passed', columns: 'passed', warnings: [] },
        request_id: 'r1',
      })
    }
    return json(r, 200, {
      ok: false, sql: null, explanation: null, provider: 'mock', model: 'm',
      error: 'provider_output_truncated', egress_level: 3, tables_used: [], truncated: true,
      stage: 'failed', diagnostic_code: 'provider_output_truncated', attempts: 2,
      reasoning_mode: 'enabled', validation: null, request_id: 'diag-1',
    })
  })

  await page.goto('/projects/project-1/sql')
  await page.getByRole('button', { name: 'AI Generate' }).click()
  await page.getByLabel('Query request').fill('list users')
  await page.getByRole('button', { name: 'Recommend tables' }).click()
  await page.getByRole('button', { name: 'Generate preview' }).click()
  await page.getByLabel('Revision request').fill('add a date filter')
  await page.getByRole('button', { name: 'Revise preview' }).click()

  expect(requests[1]).toMatchObject({
    candidate_sql: 'SELECT id FROM app.users',
    revision_instruction: 'add a date filter',
  })
  await expect(page.getByText(/output was truncated/i)).toBeVisible()
  await expect(page.getByText('diag-1')).toBeVisible()
  await expect(page.getByText('SELECT id FROM app.users', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Revise preview' })).toBeDisabled()
  expect(requests).toHaveLength(2)
  expectNoConsoleErrors()
})

test('AI assistant ignores a stale table response after datasource switch', async ({ page }) => {
  await mockBase(page)
  await page.route(/\/api\/datasources\?/, (r) =>
    json(r, 200, [
      datasource(),
      datasource({ id: 'ds-2', name: 'analytics', database: 'analytics' }),
    ]),
  )
  let releaseResponse: (() => void) | undefined
  let finishResponse: (() => void) | undefined
  const delayed = new Promise<void>((resolve) => { releaseResponse = resolve })
  const finished = new Promise<void>((resolve) => { finishResponse = resolve })
  await page.route('**/api/datasources/ds-1/ai/sql-table-candidates', async (r) => {
    await delayed
    await json(r, 200, {
      candidates: [{ schema_name: 'old_schema', table_name: 'old_table', matched_by: ['table_name'] }],
      truncated: false,
    })
    finishResponse?.()
  })

  await page.goto('/projects/project-1/sql')
  await page.getByRole('button', { name: 'AI Generate' }).click()
  await page.getByLabel('Query request').fill('list old records')
  await page.getByRole('button', { name: 'Recommend tables' }).click()
  await expect.poll(() => Boolean(releaseResponse)).toBeTruthy()
  await page.getByRole('combobox').first().selectOption('ds-2')
  await expect(page.getByRole('complementary', { name: 'AI SQL Assistant' })).toHaveCount(0)
  releaseResponse?.()
  await finished
  await page.getByRole('button', { name: 'AI Generate' }).click()
  await expect(page.getByRole('checkbox', { name: 'old_schema.old_table' })).toHaveCount(0)
  expectNoConsoleErrors()
})

test('AI assistant maps stable gateway diagnostics and disabled configuration', async ({ page }) => {
  await mockBase(page)
  await page.route('**/api/datasources/ds-1/ai/sql-table-candidates', (r) =>
    json(r, 200, {
      candidates: [{ schema_name: 'app', table_name: 'users', matched_by: ['table_name'] }],
      truncated: false,
    }),
  )
  let generation = 0
  await page.route('**/api/datasources/ds-1/ai/sql-generate', (r) => {
    generation += 1
    if (generation === 1) {
      return json(r, 200, {
        ok: false, sql: null, explanation: null, provider: 'mock', model: 'm',
        error: 'ai_budget_exceeded', egress_level: 2, tables_used: [], truncated: false,
        stage: 'failed', diagnostic_code: 'ai_budget_exceeded', attempts: 1,
        reasoning_mode: 'disabled', validation: null, request_id: 'budget-1',
      })
    }
    return json(r, 409, { error: 'ai_disabled', message: 'AI copilot is not enabled' })
  })

  await page.goto('/projects/project-1/sql')
  await page.getByRole('button', { name: 'AI Generate' }).click()
  await page.getByLabel('Query request').fill('list users')
  await page.getByRole('button', { name: 'Recommend tables' }).click()
  await page.getByRole('button', { name: 'Generate preview' }).click()
  await expect(page.getByText(/request budget was exceeded/i)).toBeVisible()
  await expect(page.getByText('budget-1')).toBeVisible()
  await page.getByRole('button', { name: 'Generate preview' }).click()
  await expect(page.getByText(/not enabled/i)).toBeVisible()
  await expect(page.getByText(/unsupported response/i)).toHaveCount(0)
  expectNoConsoleErrors()
})

test('AI assistant overlays compact viewports and contains long preview SQL', async ({ page }) => {
  await mockBase(page)
  const longSql = `SELECT ${'very_long_expression_'.repeat(120)} FROM app.users`
  await page.route('**/api/datasources/ds-1/ai/sql-table-candidates', (r) =>
    json(r, 200, {
      candidates: [{ schema_name: 'app', table_name: 'users', matched_by: ['table_name'] }],
      truncated: false,
    }),
  )
  await page.route('**/api/datasources/ds-1/ai/sql-generate', (r) =>
    json(r, 200, {
      ok: true, sql: longSql, explanation: null, provider: 'mock', model: 'm', error: null,
      egress_level: 2, tables_used: ['app.users'], truncated: false, stage: 'validated',
      diagnostic_code: null, attempts: 1, reasoning_mode: 'disabled',
      validation: { readonly: 'passed', tables: 'passed', columns: 'passed', warnings: [] },
      request_id: 'narrow-1',
    }),
  )

  await page.goto('/projects/project-1/sql')
  await page.getByRole('button', { name: 'AI Generate' }).click()
  await page.getByLabel('Query request').fill('show a long expression')
  await page.getByRole('button', { name: 'Recommend tables' }).click()
  await page.getByRole('button', { name: 'Generate preview' }).click()
  const panel = page.getByRole('complementary', { name: 'AI SQL Assistant' })
  for (const viewportWidth of [700, 800, 1024]) {
    await page.setViewportSize({ width: viewportWidth, height: 800 })
    const box = await panel.boundingBox()
    const editorBox = await page.locator('.monaco-editor').boundingBox()
    expect(box).not.toBeNull()
    expect(box!.x).toBeGreaterThanOrEqual(0)
    expect(box!.x + box!.width).toBeLessThanOrEqual(viewportWidth + 1)
    expect(editorBox).not.toBeNull()
    expect(editorBox!.width).toBeGreaterThanOrEqual(Math.max(300, viewportWidth - 410))
    const widths = await page.evaluate(() => ({
      scroll: document.documentElement.scrollWidth,
      client: document.documentElement.clientWidth,
      offenders: [...document.querySelectorAll<HTMLElement>('body *')]
        .map((element) => ({
          tag: element.tagName,
          className: element.className,
          right: element.getBoundingClientRect().right,
          width: element.getBoundingClientRect().width,
        }))
        .filter((item) => item.right > document.documentElement.clientWidth + 1)
        .slice(0, 8),
    }))
    expect(widths.scroll, JSON.stringify(widths.offenders)).toBeLessThanOrEqual(widths.client + 1)
  }
  expectNoConsoleErrors()
})

test('long SQL stays inside Monaco without widening the document', async ({ page }) => {
  const longExpression = Array.from({ length: 120 }, (_, i) => `column_${i}`).join(' + ')
  await mockLicense(page)
  await page.route(/\/api\/datasources\?/, (r) => json(r, 200, [datasource()]))
  await page.route('**/api/sql/consoles', (r) =>
    json(r, 200, [consoleRow({ sql: `SELECT ${longExpression} FROM users` })]),
  )

  await page.goto('/projects/project-1/sql')
  await expect(page.getByRole('button', { name: 'Run' })).toBeVisible()
  await expect(page.getByRole('combobox').first()).toBeVisible()
  const widths = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth,
  }))
  expect(widths.scroll).toBeLessThanOrEqual(widths.client + 1)
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
