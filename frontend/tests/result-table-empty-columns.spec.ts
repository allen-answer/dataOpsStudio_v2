import { test, expect, type Page, type Route } from '@playwright/test'
import { json, mockLicense, seedAdminAuth, trackConsoleErrors } from './helpers'

/**
 * 零行结果必须仍然显示列头(schema 事实与行数无关,DataGrip / DBeaver 惯例)。
 *
 * 回归根因:ResultTable 空态判据是 `rows.length === 0`,`v-else` 把整个
 * `<table>`(含 `<thead>`)一起干掉 —— 但父组件 SqlWorkspaceView 的
 * `shouldShowResultTable` 显式支持「零行有列」(`rows.length > 0 ||
 * columns.length > 0`),父子语义不一致。修复后:有列即走表格分支,
 * `<tbody>` 里给一条跨列的「无数据行」提示,`<thead>` 保留。
 *
 * mock 形状锚自后端契约(tests/contract/test_api.py 的 JobResultResponse):
 * 零行时后端照常返回非空 columns。
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
    sql: 'SELECT * FROM users WHERE 1=0',
    pinned: false,
    created_at: now,
    updated_at: now,
    ...overrides,
  }
}

async function mockBase(page: Page): Promise<void> {
  await mockLicense(page)
  // 锚 job 路径:如实声明该部署没开控制台会话(409),前端整体回落 job 路径。
  await page.route('**/api/sql/sessions/attach', (r) =>
    json(r, 409, {
      error: 'console_session_disabled',
      message: 'Console sessions are disabled on this deployment',
    }),
  )
  await page.route(/\/api\/datasources\?/, (r) => json(r, 200, [datasource()]))
  await page.route(/\/api\/datasources\/[^/]+\/metadata\/schemas/, (r) => json(r, 200, []))
  await page.route('**/api/sql/consoles', (r: Route) =>
    r.request().method() === 'GET' ? json(r, 200, [consoleRow()]) : r.fallback(),
  )
  await page.route('**/api/sql/consoles/console-1', (r: Route) =>
    r.request().method() === 'PATCH'
      ? json(r, 200, consoleRow(r.request().postDataJSON()))
      : r.fallback(),
  )
}

/** job 路径:execute → job → progress(terminal) → result。 */
async function mockZeroRowJob(page: Page, columns: Record<string, unknown>[]): Promise<void> {
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
  // 零行但 columns_ready —— 这正是 `select ... where 1=0` 的真实形状。
  await page.route(/\/api\/jobs\/job-1\/progress\?/, (r) =>
    json(r, 200, {
      job_id: 'job-1', result_set_id: 'rs-1', status: 'success', loaded_rows: 0,
      result_version: 1, columns_ready: true, first_batch_ready: false, terminal: true,
      error: null, error_code: null, retry_after_ms: 0, has_new_result: true,
      truncated: false, has_more: false, timings: null, execution: null,
    }),
  )
  await page.route(/\/api\/jobs\/job-1\/result\?/, (r) =>
    json(r, 200, {
      job_id: 'job-1',
      result_set_id: 'rs-1',
      offset: 0,
      limit: 1000,
      columns,
      rows: [],
      loaded_rows: 0,
      total_rows: 0,
      state: 'success',
      truncated: false,
    }),
  )
}

let consoleErrors: string[] = []
test.beforeEach(async ({ page }) => {
  consoleErrors = trackConsoleErrors(page)
  await seedAdminAuth(page)
})

test('zero-row result still renders column headers', async ({ page }) => {
  await mockBase(page)
  await mockZeroRowJob(page, [
    { name: 'id', type: 'integer', driver_type: 'INT', nullable: false, primary_key: true },
    { name: 'email', type: 'string', driver_type: 'VARCHAR(255)', nullable: true, primary_key: false },
    { name: 'created_at', type: 'datetime', driver_type: 'DATETIME', nullable: true, primary_key: false },
  ])

  await page.goto('/projects/project-1/sql')
  await page.getByRole('button', { name: 'Run' }).click()
  await expect(page.getByText(/Done · /)).toBeVisible()

  // ★ 核心断言:零行也必须挂出表格 + 列头。修复前整个 <table> 被 v-else 干掉。
  const scroll = page.locator('[data-testid="result-table-scroll"]')
  await expect(scroll).toBeVisible()

  const headers = scroll.locator('thead th')
  await expect(headers).toHaveCount(4) // 行号列 "#" + 3 个数据列
  await expect(headers.nth(1)).toContainText('id')
  await expect(headers.nth(2)).toContainText('email')
  await expect(headers.nth(3)).toContainText('created_at')

  // driver_type 副行同样是 schema 事实,零行时也应可见。
  await expect(scroll.locator('thead')).toContainText('VARCHAR(255)')
  // PK 标记保留。
  await expect(scroll.locator('thead')).toContainText('PK')

  // 列头之下给「无数据行」提示,而不是把列头换掉。
  await expect(scroll.locator('tbody')).toContainText('No rows returned')

  // footer 的 0 行分页信息保持自洽。
  await expect(page.getByText('0-0 of 0')).toBeVisible()

  expect(consoleErrors, consoleErrors.join('\n')).toEqual([])
})

test('result with neither columns nor rows keeps the empty-state placeholder', async ({
  page,
}) => {
  await mockBase(page)
  await mockZeroRowJob(page, [])

  await page.goto('/projects/project-1/sql')
  await page.getByRole('button', { name: 'Run' }).click()
  await expect(page.getByText(/Done · /)).toBeVisible()

  // 完全没有结果形状(DDL / 无 result set)仍走 EmptyState,不要退化成空表格。
  await expect(page.getByText('Empty result set')).toBeVisible()
  await expect(page.locator('[data-testid="result-table-scroll"]')).toHaveCount(0)

  expect(consoleErrors, consoleErrors.join('\n')).toEqual([])
})
