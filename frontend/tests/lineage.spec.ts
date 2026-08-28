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
  await page.getByTestId('lineage-batch-file').setInputFiles({
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

/* ──────────────────────────────────────────────────────────────────────────
 * 方案 A:Lineage impact / analyze / batch 横向铺满
 * ────────────────────────────────────────────────────────────────────────── */

/** 旧限制:impact/batch = max-w-4xl(896px),analyze = max-w-3xl(768px)。 */
const LEGACY_MAX_W_3XL = 768
const LEGACY_MAX_W_4XL = 896

async function expectFullWidthBody(page: Page, testId: string, legacyCap: number): Promise<void> {
  const body = page.getByTestId(testId)
  await expect(body).toBeVisible()
  const cls = (await body.getAttribute('class')) ?? ''
  expect(cls).not.toMatch(/max-w-(3xl|4xl)/)
  const box = await body.boundingBox()
  const containerWidth = await body.evaluate((el) => (el.parentElement as HTMLElement).clientWidth)
  expect(box!.width).toBeGreaterThan(legacyCap)
  expect(box!.width).toBeGreaterThanOrEqual(containerWidth - 1)
}

test('lineage impact / analyze / batch bodies use the full available width', async ({ page }) => {
  await mockBase(page)
  // analyze tab 的 Analyze 按钮需要至少一个数据源。
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
  await page.setViewportSize({ width: 1600, height: 960 })
  await page.goto('/projects/project-1/lineage')

  await page.getByRole('button', { name: 'Impact analysis', exact: true }).click()
  await expectFullWidthBody(page, 'lineage-impact-body', LEGACY_MAX_W_4XL)

  await page.getByRole('button', { name: 'SQL analyze', exact: true }).click()
  await expectFullWidthBody(page, 'lineage-analyze-body', LEGACY_MAX_W_3XL)
  await expect(page.getByRole('button', { name: 'Analyze', exact: true })).toBeVisible()

  await page.getByRole('button', { name: 'Batch', exact: true }).click()
  await expectFullWidthBody(page, 'lineage-batch-body', LEGACY_MAX_W_4XL)
  await expect(page.getByTestId('lineage-batch-file')).toHaveCount(1)
  // DDL 输入框自带隐藏 file input,裸 input[type=file] 已不唯一;仍然锁总数,
  // 免得将来又多出无人认领的文件输入(等价于本例原先的 toHaveCount(1) 保护)。
  // 3 = 批量 ZIP 输入 + analyze / batch 两个 tab 各自的 DDL 文件输入(都已挂载)。
  await expect(page.locator('input[type="file"]')).toHaveCount(3)
  await expect(page.getByTestId('lineage-ddl-file')).toHaveCount(2)

  expectNoConsoleErrors()
})

/* ──────────────────────────────────────────────────────────────────────────
 * DDL 文本数据源(无库 / 缺元数据时补列元数据 → 列级血缘)
 * ────────────────────────────────────────────────────────────────────────── */

// 合成表结构 + 达梦导出实拍形态(NOT CLUSTER PRIMARY KEY / STORAGE 尾巴)。
const DDL_TEXT = [
  'CREATE TABLE "ODS"."ORDERS" ("ORDER_ID" NUMBER(18), "AMT" NUMBER(12,2),',
  '  NOT CLUSTER PRIMARY KEY("ORDER_ID")) STORAGE(ON "MAIN", CLUSTERBTR);',
  'CREATE TABLE "ODS"."DWD_ORDERS" ("ORDER_ID" NUMBER(18), "AMT" NUMBER(12,2));',
].join('\n')

const datasourceList = [
  {
    id: 'ds-1',
    name: 'warehouse',
    db_type: 'dm',
    host: 'db.local',
    port: 5236,
    environment: 'sandbox',
    environment_verified: false,
    database: 'app',
    operation_policy: { allow_select: true },
    created_at: now,
  },
]

test('SQL analyze sends ddl_text and renders the DDL source summary', async ({ page }) => {
  await mockBase(page)
  await page.route(/\/api\/datasources\?/, (r) => json(r, 200, datasourceList))

  let sentDdl: string | null | undefined
  await page.route('**/api/projects/project-1/lineage/analyze*', async (r) => {
    sentDdl = r.request().postDataJSON().ddl_text
    await json(r, 201, {
      run_id: 'run-ddl',
      project_id: 'project-1',
      datasource_id: 'ds-1',
      dialect: 'dm',
      source_ref: 'etl/orders.sql',
      sql_hash: 'a'.repeat(64),
      parser_version: 'sqlglot-w1-v3',
      status: 'success',
      cached: false,
      // 后端把 DDL 摘要挂在 parse_summary.ddl_schema(与 dialect_detection 同范式)
      parse_summary: {
        statement_count: 1,
        table_edge_count: 1,
        column_mapping_count: 2,
        parse_error_count: 0,
        ddl_schema: {
          table_count: 2,
          column_count: 4,
          parsed_table_count: 2,
          parsed_column_count: 4,
          skipped_statement_count: 0,
          skipped_reasons: {},
          failed_column_entry_count: 0,
          dialect: 'dm',
        },
      },
      table_edge_count: 1,
      column_edge_count: 2,
    })
  })

  await page.goto('/projects/project-1/lineage')
  await page.getByRole('button', { name: 'SQL analyze', exact: true }).click()

  await page.getByRole('textbox', { name: 'Source ref' }).fill('etl/orders.sql')
  await page
    .getByTestId('lineage-analyze-body')
    .getByTestId('lineage-ddl-input')
    .fill(DDL_TEXT)
  await page
    .getByRole('textbox', { name: 'SQL text' })
    .fill('INSERT INTO ODS.DWD_ORDERS SELECT ORDER_ID, AMT FROM ODS.ORDERS')
  await page.getByRole('button', { name: 'Analyze', exact: true }).click()

  await expect(
    page.getByTestId('lineage-analyze-body').getByTestId('lineage-ddl-summary'),
  ).toContainText('DDL source: added 2 tables / 4 columns')
  expect(sentDdl).toBe(DDL_TEXT)
  expectNoConsoleErrors()
})

test('DDL input loads a .sql file into the textarea and clears it', async ({ page }) => {
  await mockBase(page)
  await page.route(/\/api\/datasources\?/, (r) => json(r, 200, datasourceList))

  await page.goto('/projects/project-1/lineage')
  await page.getByRole('button', { name: 'SQL analyze', exact: true }).click()

  const analyzeBody = page.getByTestId('lineage-analyze-body')
  const ddlBox = analyzeBody.getByTestId('lineage-ddl-input')
  await expect(ddlBox).toHaveValue('')
  // .sql 只在浏览器本地读成文本,不走上传接口
  await analyzeBody.getByTestId('lineage-ddl-file').setInputFiles({
    name: 'schema.sql',
    mimeType: 'text/plain',
    buffer: Buffer.from(DDL_TEXT),
  })
  await expect(ddlBox).toHaveValue(DDL_TEXT)

  await analyzeBody.getByRole('button', { name: 'Clear', exact: true }).click()
  await expect(ddlBox).toHaveValue('')
  expectNoConsoleErrors()
})

test('DDL input over the size cap keeps the textarea in sync with the model', async ({ page }) => {
  // F14:超限时不 emit → modelValue 不变 → Vue 不回写 :value,textarea 会一直显示
  // 那段超限文本,而提交的却是陈旧 / 空的 ddl_text。这里守"拒绝后 DOM 被写回"。
  await mockBase(page)
  await page.route(/\/api\/datasources\?/, (r) => json(r, 200, datasourceList))

  await page.goto('/projects/project-1/lineage')
  await page.getByRole('button', { name: 'SQL analyze', exact: true }).click()

  const analyzeBody = page.getByTestId('lineage-analyze-body')
  const ddlBox = analyzeBody.getByTestId('lineage-ddl-input')
  await ddlBox.fill(DDL_TEXT)
  await expect(ddlBox).toHaveValue(DDL_TEXT)

  // 连续两次超限输入:第二次 error 被赋同一字符串,相等性守卫会抑制重渲染 ——
  // DOM 回写必须是命令式的,不能指望响应式。
  for (let attempt = 0; attempt < 2; attempt += 1) {
    await ddlBox.evaluate((element, size) => {
      const textarea = element as HTMLTextAreaElement
      textarea.value = 'x'.repeat(size)
      textarea.dispatchEvent(new Event('input', { bubbles: true }))
    }, 1_000_001)
    await expect(analyzeBody.getByText(/exceeds the .* character limit/)).toBeVisible()
    // ★ textarea 必须已经被写回模型值,而不是留着那 1000001 个 x。
    await expect(ddlBox).toHaveValue(DDL_TEXT)
  }
  expectNoConsoleErrors()
})

test('batch tab sends ddl_text and shows the summary from the report', async ({ page }) => {
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

  let sentDdl: string | null | undefined
  await page.route('**/api/projects/project-1/lineage/batch', async (r) => {
    sentDdl = r.request().postDataJSON().ddl_text
    await json(r, 202, { job_id: 'batch-ddl' })
  })
  await page.route('**/api/projects/project-1/lineage/batch/batch-ddl', (r) =>
    json(r, 200, {
      job_id: 'batch-ddl',
      status: 'success',
      error: null,
      report: {
        file_count: 1,
        parsed: 1,
        failed: 0,
        skipped: { non_sql: 0, too_large: 0, over_file_limit: 0 },
        table_edge_total: 1,
        column_mapping_total: 2,
        files: [
          {
            source_ref: 'load.sql',
            status: 'parsed',
            table_edge_count: 1,
            column_mapping_count: 2,
          },
        ],
        script_edges: [],
        ddl_schema: {
          table_count: 2,
          column_count: 4,
          parsed_table_count: 3,
          parsed_column_count: 6,
          skipped_statement_count: 2,
          // 拆开的跳过原因:两条各自成文,不再混成一个"非建表语句"数字。
          skipped_reasons: { non_create_table: 1, parse_failed: 1 },
          failed_column_entry_count: 1,
          dialect: 'mysql',
        },
      },
    }),
  )

  await page.goto('/projects/project-1/lineage')
  await page.getByRole('button', { name: 'Batch', exact: true }).click()
  await page.getByTestId('lineage-batch-file').setInputFiles({
    name: 'batch.sql',
    mimeType: 'text/plain',
    buffer: Buffer.from('select 1;'),
  })
  await page.getByTestId('lineage-batch-body').getByTestId('lineage-ddl-input').fill(DDL_TEXT)
  await page.getByRole('button', { name: 'Upload & analyze' }).click()

  const batchSummary = page.getByTestId('lineage-batch-body').getByTestId('lineage-ddl-summary')
  await expect(batchSummary).toContainText('DDL source: added 2 tables / 4 columns')
  // ★ 生效数与解析数不等 → 说明有表被元数据缓存遮蔽,徽标要讲清楚。
  await expect(batchSummary).toContainText('1 of them already had cached metadata')
  // ★ 跳过原因逐条成文,而不是"跳过 2 条非建表语句"把用户引向错误方向。
  await expect(batchSummary).toContainText('1 non-CREATE-TABLE statements skipped')
  await expect(batchSummary).toContainText('1 CREATE TABLE statements failed to parse')
  await expect(batchSummary).toContainText('1 column definitions failed to parse')
  await expect(batchSummary).toContainText('parsed as mysql')
  expect(sentDdl).toBe(DDL_TEXT)
  expectNoConsoleErrors()
})

test('lineage parse history lists runs with confidence and supersede state', async ({
  page,
}) => {
  await mockBase(page)
  let requestedUrl = ''
  await page.route(/\/api\/projects\/project-1\/lineage\/runs\?/, (r) => {
    requestedUrl = r.request().url()
    return json(r, 200, {
      project_id: 'project-1',
      has_more: false,
      items: [
        {
          run_id: '0c3f42aa-9a1b-4d21-8c66-1f2e3d4a5b6c',
          project_id: 'project-1',
          datasource_id: 'ds-1',
          datasource_name: 'prod-mysql',
          dialect: 'mysql',
          source_ref: 'etl/orders_daily.sql',
          sql_hash: 'hash-1',
          status: 'success',
          table_edge_count: 12,
          column_edge_count: 38,
          min_confidence: 0.74,
          unconfirmed_inferred_count: 2,
          target_tables: ['ads.orders_agg'],
          active: true,
          superseded_at: null,
          superseded_by: null,
          created_at: '2026-08-28T09:12:00Z',
        },
        {
          run_id: '9d11f8bb-02aa-4c10-9e55-7a8b9c0d1e2f',
          project_id: 'project-1',
          datasource_id: 'ds-1',
          datasource_name: 'prod-mysql',
          dialect: 'mysql',
          source_ref: 'etl/orders_daily.sql',
          sql_hash: 'hash-0',
          status: 'success',
          table_edge_count: 11,
          column_edge_count: 35,
          min_confidence: 0.91,
          unconfirmed_inferred_count: 0,
          target_tables: ['ads.orders_agg'],
          active: false,
          superseded_at: '2026-08-28T09:12:00Z',
          superseded_by: '0c3f42aa-9a1b-4d21-8c66-1f2e3d4a5b6c',
          created_at: '2026-08-25T14:03:00Z',
        },
      ],
    })
  })

  await page.goto('/projects/project-1/lineage')
  await page.getByTestId('lineage-runs-tab').click()

  const body = page.getByTestId('lineage-runs-body')
  await expect(body.getByText('etl/orders_daily.sql').first()).toBeVisible()
  // 置信度按血缘边最小值展示为百分比;未确认推断边数量单独提示
  await expect(body.getByText('74%')).toBeVisible()
  await expect(body.getByText('2 unconfirmed inferred edge(s)')).toBeVisible()
  // 留痕:旧记录不消失,标"已被取代"并指向取代者
  await expect(body.getByText('Superseded → 0c3f42aa')).toBeVisible()
  await expect(body.getByText('Active', { exact: true })).toBeVisible()
  // 首屏默认只看生效记录
  expect(requestedUrl).toContain('active_only=true')

  expectNoConsoleErrors()
})
