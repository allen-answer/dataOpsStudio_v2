import { test, expect, type Page, type Route } from '@playwright/test'
import { json, mockLicense, trackConsoleErrors, ADMIN_JWT } from './helpers'

/**
 * 结果区三 tab(结果 / 执行计划 / 统计)布局回归 —— 防「图标+中文被 28px 固定方块
 * 挤成竖排单字」。根因:之前用 .chrome-btn-ghost(固定 1.75rem 方块、无 nowrap),
 * 现改用 .chrome-tab(内容自适应 + white-space:nowrap)。
 *
 * 用 zh-CN 锁定(中文标签最能暴露竖排 bug),分别在「无结果」「有结果」两态截图,
 * 并断言计算样式 white-space === 'nowrap' 且按钮高度 ≈ 单行(< 36px)。
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

// zh-CN 锁定:中文标签最能暴露竖排 bug。
async function seedZhAdmin(page: Page): Promise<void> {
  await page.addInitScript(
    ([token]) => {
      localStorage.setItem('dataops-token', token)
      localStorage.setItem('dataops-locale', 'zh-CN')
    },
    [ADMIN_JWT],
  )
}

async function mockBase(page: Page): Promise<void> {
  await mockLicense(page)
  // 这些用例锚的是 **job 路径**:如实声明该部署没开控制台会话
  // (409 console_session_disabled),前端据此整体回落 job 路径(设计 §3.3)。
  await page.route('**/api/sql/sessions/attach', (r) =>
    json(r, 409, {
      error: 'console_session_disabled',
      message: 'Console sessions are disabled on this deployment',
    }),
  )
  await page.route(/\/api\/datasources\?/, (r) => json(r, 200, [datasource()]))
  await page.route('**/api/sql/consoles', (r: Route) =>
    r.request().method() === 'GET' ? json(r, 200, [consoleRow()]) : r.fallback(),
  )
  await page.route('**/api/sql/consoles/console-1', (r: Route) =>
    r.request().method() === 'PATCH'
      ? json(r, 200, consoleRow(r.request().postDataJSON()))
      : r.fallback(),
  )
}

// 三个结果 tab 的本地化文案(zh-CN)。
const TAB_LABELS = ['结果', '执行计划', '统计']

async function assertTabsSingleLine(page: Page): Promise<void> {
  // 只在结果区 tab 组(.chrome-tab)里找,避免和编辑器工具栏的「执行计划」EXPLAIN 按钮撞名。
  for (const label of TAB_LABELS) {
    const btn = page.locator('button.chrome-tab', { hasText: label })
    await expect(btn).toBeVisible()
    const whiteSpace = await btn.evaluate((el) => getComputedStyle(el).whiteSpace)
    expect(whiteSpace, `tab "${label}" must not wrap`).toBe('nowrap')
    const box = await btn.boundingBox()
    expect(box, `tab "${label}" box`).not.toBeNull()
    // 单行图标+文字按钮高度应 ≈ 2rem(32px);竖排会把高度撑到 ≥ 60px。
    expect(box!.height, `tab "${label}" height must be single-line`).toBeLessThan(40)
    // 竖排单字会让宽度坍到 ≈ 一个字宽;水平排版宽度应明显更大。
    expect(box!.width, `tab "${label}" width must fit icon + text on one line`).toBeGreaterThan(40)
  }
}

let consoleErrors: string[] = []
test.beforeEach(async ({ page }) => {
  consoleErrors = trackConsoleErrors(page)
  await seedZhAdmin(page)
})

test('result tabs render single-line (no CJK vertical collapse) — empty state', async ({
  page,
}) => {
  await mockBase(page)
  await page.goto('/projects/project-1/sql')

  // 无结果占位态:右侧状态摘要 + 结果区占位都显示「执行后在此查看结果」。
  await expect(page.getByText('执行后在此查看结果').first()).toBeVisible()
  await assertTabsSingleLine(page)

  await page
    .locator('div.flex.items-center.justify-between')
    .filter({ hasText: '结果' })
    .first()
    .screenshot({ path: 'test-results/result-tabs-empty.png' })

  expect(consoleErrors, consoleErrors.join('\n')).toEqual([])
})

test('result tabs render single-line (no CJK vertical collapse) — with result', async ({
  page,
}) => {
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
  await page.route(/\/api\/jobs\/job-1\/progress\?/, (r) =>
    json(r, 200, {
      job_id: 'job-1', result_set_id: 'rs-1', status: 'success', loaded_rows: 1,
      result_version: 1, columns_ready: true, first_batch_ready: true, terminal: true,
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
      columns: [
        { name: 'id', type: 'integer', driver_type: 'INT', nullable: false, primary_key: true },
      ],
      rows: [{ values: [1] }],
      loaded_rows: 1,
      total_rows: 1,
      state: 'success',
      truncated: false,
    }),
  )

  await page.goto('/projects/project-1/sql')
  await page.getByRole('button', { name: '执行', exact: true }).click()
  // 成功后状态摘要出现(zh-CN「完成 · …」)。
  await expect(page.getByText(/完成 ·/)).toBeVisible()

  await assertTabsSingleLine(page)

  await page
    .locator('div.flex.items-center.justify-between')
    .filter({ hasText: '结果' })
    .first()
    .screenshot({ path: 'test-results/result-tabs-with-result.png' })

  expect(consoleErrors, consoleErrors.join('\n')).toEqual([])
})
