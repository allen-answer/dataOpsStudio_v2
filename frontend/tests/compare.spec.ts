import { test, expect, type Page, type Route } from '@playwright/test'
import { deferred, json, mockLicense, seedAdminAuth, trackConsoleErrors } from './helpers'

/**
 * 2.2.0 Compare frontend —— 任务编辑(自动推断确认制)/ 4 桶结果(单元格分裂)/
 * 差异画像 / 采样快检 / AI 归因。
 *
 * ★ 所有 mock 响应字段锚自后端契约测试 tests/contract/test_api.py(test_compare_*):
 *   - CompareInferResponse  : { project_id, source_id, target_id, source_table, target_table,
 *                              mappings[], pk_candidates[], needs_manual_pk, compare_rules, columns[] }
 *   - ColumnMappingCandidate: { source_column, target_column, source_type, target_type,
 *                              confidence, reason, conflict, conflict_kind }
 *   - PrimaryKeyCandidate   : { source_columns[], target_columns[], confidence, reason }
 *   - CompareTaskResponse   : { id, project_id, name, source_id, target_id, source_ref, target_ref,
 *                              columns[], compare_rules, run_limits, created_by, created_at, updated_at }
 *   - CompareRunCreateResponse: { job_id, run_id }
 *   - CompareRunResultResponse: { job_id, run_id, bucket, offset, limit, bucket_counts,
 *                              progress, diff_profile, sample_result, rows[] }
 *   - CompareResultRow      : { pk, source, target, cells[{column, source, target}] }
 *   - CompareRunProfileResponse: 同 result 去掉 rows
 *   - CompareAiAttributionResponse: { run_id, ok, attribution, provider, model, error, egress_level }
 */

const now = '2026-06-13T06:00:00Z'

function datasource(overrides: Record<string, unknown> = {}) {
  return {
    id: 'ds-source',
    name: 'warehouse_a',
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

function compareTask(overrides: Record<string, unknown> = {}) {
  return {
    id: 'task-1',
    project_id: 'project-1',
    name: 'orders',
    source_id: 'ds-source',
    target_id: 'ds-target',
    source_ref: { kind: 'table', schema_name: 'app', table_name: 'orders_a' },
    target_ref: { kind: 'table', schema_name: 'app', table_name: 'orders_b' },
    columns: [
      { name: 'id', type: 'integer', driver_type: 'INT', nullable: false, primary_key: true },
      { name: 'amount', type: 'decimal', driver_type: 'DECIMAL(12,2)', nullable: true, primary_key: false },
    ],
    compare_rules: {
      key_columns: ['id'],
      ignore_columns: [],
      column_mappings: { id: 'ID' },
      numeric_tolerance: null,
      trim_strings: false,
      case_insensitive: false,
      empty_as_null: false,
      schema_policy: 'strict',
    },
    run_limits: {
      max_rows: null,
      export_max_rows: null,
      fetch_chunk_size: 1000,
      compare_batch_size: 10000,
      stream_compare: true,
      recursive_checksum: true,
      bisection_factor: 8,
      bisection_threshold: 16000,
      max_bisection_depth: 8,
      sample_quick_check: false,
      sample_size: 300,
      sample_confidence: 0.95,
      result_format: 'parquet',
      persist_same_bucket: false,
      query_timeout_seconds: 1800,
      run_disk_quota_mb: null,
    },
    created_by: 'admin-user-0001',
    created_at: now,
    updated_at: now,
    ...overrides,
  }
}

const inferResponse = {
  project_id: 'project-1',
  source_id: 'ds-source',
  target_id: 'ds-target',
  source_table: { schema_name: 'app', table_name: 'orders' },
  target_table: { schema_name: 'app', table_name: 'orders' },
  mappings: [
    {
      source_column: 'amount',
      target_column: 'amount',
      source_type: 'decimal',
      target_type: 'decimal',
      confidence: 0.95,
      reason: 'exact',
      conflict: false,
      conflict_kind: null,
    },
  ],
  pk_candidates: [
    { source_columns: ['id'], target_columns: ['ID'], confidence: 1.0, reason: 'primary_key' },
  ],
  needs_manual_pk: false,
  compare_rules: {
    key_columns: ['id'],
    ignore_columns: [],
    column_mappings: { id: 'ID' },
    numeric_tolerance: null,
    trim_strings: false,
    case_insensitive: false,
    empty_as_null: false,
    schema_policy: 'warn',
  },
  columns: [
    { name: 'id', type: 'integer', driver_type: 'INT', nullable: false, primary_key: true },
    { name: 'amount', type: 'decimal', driver_type: 'DECIMAL(12,2)', nullable: true, primary_key: false },
  ],
}

const diffProfile = {
  version: 1,
  generated: true,
  summary: { diff_rows: 1, same_rows: 10, paired_rows_observed: 11 },
  columns: {
    amount: {
      type: 'decimal',
      observed_rows: 11,
      changed_rows: 1,
      diff_rate: 1.0,
      numeric_delta: { count: 1, constant_offset: '-1.00', systematic_offset: true },
    },
  },
  missing_key_ranges: { only_source: [], only_target: [] },
}

const sampleResult = {
  enabled: true,
  mode: 'pk_random_anchor',
  requested_rows: 300,
  sampled_rows: 300,
  observed_differences: 0,
  all_sampled_equal: true,
  confidence: 0.95,
  difference_rate_upper_bound: 0.00994,
}

const bucketCounts = { only_source: 1, only_target: 1, diff: 1, same: 7 }
const progress = { scanned_segments: 2, skipped_segments: 1, skipped_rows: 7, row_mode_segments: 1 }

async function mockBase(
  page: Page,
  tasks: unknown[] = [compareTask()],
): Promise<void> {
  await mockLicense(page)
  await page.route(/\/api\/projects\/project-1\/compare\/runs-dashboard/, (r) =>
    json(r, 200, {
      project_id: 'project-1',
      days: 30,
      total_runs: 0,
      status_counts: {},
      success_rate: 0,
      top_abort_reasons: [],
    }),
  )
  await page.route(/\/api\/datasources\?/, (r) =>
    json(r, 200, [datasource(), datasource({ id: 'ds-target', name: 'warehouse_b' })]),
  )
  await page.route(/\/api\/datasources\/[^/]+\/metadata\/schemas/, (r) =>
    json(r, 200, [{ name: 'app' }]),
  )
  await page.route(/\/api\/datasources\/[^/]+\/metadata\/tables/, (r) =>
    json(r, 200, [
      { schema_name: 'app', name: 'orders', table_type: 'BASE TABLE' },
      { schema_name: 'app', name: 'orders_a', table_type: 'BASE TABLE' },
      { schema_name: 'app', name: 'orders_b', table_type: 'BASE TABLE' },
    ]),
  )
  await page.route(/\/api\/compare\/tasks(\?|$)/, (r: Route) => {
    if (r.request().method() === 'GET') return json(r, 200, tasks)
    return r.fallback()
  })
}

let consoleErrors: string[] = []
test.beforeEach(async ({ page }) => {
  consoleErrors = trackConsoleErrors(page)
  await seedAdminAuth(page)
})
function expectNoConsoleErrors(): void {
  expect(consoleErrors, consoleErrors.join('\n')).toEqual([])
}

async function expectTableValue(
  page: Page,
  side: 'source' | 'target',
  value: string,
): Promise<void> {
  await expect(page.getByTestId(`compare-${side}-table`)).toHaveAttribute('data-value', value)
}

async function selectTable(
  page: Page,
  side: 'source' | 'target',
  value: string,
): Promise<void> {
  const testId = `compare-${side}-table`
  await page.getByTestId(testId).click()
  await page.getByTestId(`${testId}-search`).fill(value)
  await page
    .getByTestId(`${testId}-options`)
    .getByRole('option', { name: value, exact: true })
    .click()
  await expectTableValue(page, side, value)
}

test('compare task list renders and editor loads from saved task', async ({ page }) => {
  await mockBase(page)
  await page.goto('/projects/project-1/compare')
  await expect(page.getByText('Data compare')).toBeVisible()
  // 任务卡
  await expect(page.locator('aside').getByText('orders', { exact: true })).toBeVisible()
  // 编辑区从已保存任务加载:比较列 chip + 规则区
  await expect(page.getByText('Compare columns')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Save task' })).toBeVisible()
  expectNoConsoleErrors()
})

test('result snapshot task reloads and saves without degrading to a table ref', async ({ page }) => {
  const task = compareTask({
    source_id: null,
    source_ref: {
      kind: 'result_snapshot',
      input_id: 'input-snapshot-1',
      allow_partial: true,
    },
  })
  await mockBase(page, [task])
  let targetTableRequests = 0
  await page.route(/\/api\/datasources\/ds-target\/metadata\/tables/, (route) => {
    targetTableRequests += 1
    return json(route, 200, [
      { schema_name: 'app', name: 'orders_b', table_type: 'BASE TABLE' },
    ])
  })
  let patchBody: Record<string, unknown> | null = null
  await page.route(/\/api\/compare\/tasks\/task-1$/, (route) => {
    if (route.request().method() !== 'PATCH') return route.fallback()
    patchBody = route.request().postDataJSON() as Record<string, unknown>
    return json(route, 200, { ...task, ...patchBody, updated_at: now })
  })

  await page.goto('/projects/project-1/compare')
  await expect(page.getByTestId('compare-source-result-snapshot')).toContainText(
    'input-snapshot-1',
  )
  await expect(page.getByTestId('compare-source-schema')).toHaveCount(0)
  await expectTableValue(page, 'target', 'orders_b')
  await page.getByRole('button', { name: 'Save task' }).click()

  await expect.poll(() => patchBody).not.toBeNull()
  expect(patchBody?.source_id).toBeNull()
  expect(patchBody?.source_ref).toEqual({
    kind: 'result_snapshot',
    input_id: 'input-snapshot-1',
    allow_partial: true,
  })
  await expect.poll(() => targetTableRequests).toBeGreaterThanOrEqual(2)
  expectNoConsoleErrors()
})

test('compare picker preserves saved schema and table values missing from current metadata', async ({
  page,
}) => {
  await mockBase(page, [
    compareTask({
      source_ref: {
        kind: 'table',
        schema_name: 'legacy_schema',
        table_name: 'legacy_table',
      },
    }),
  ])

  await page.goto('/projects/project-1/compare')
  await expect(page.getByTestId('compare-source-schema')).toHaveValue('legacy_schema')
  await expectTableValue(page, 'source', 'legacy_table')
  expectNoConsoleErrors()
})

test('compare source and target pick schemas and tables from datasource metadata', async ({
  page,
}) => {
  await mockBase(page)
  const tableRequests: string[] = []
  await page.route(/\/api\/datasources\/ds-source\/metadata\/schemas/, (route) =>
    json(route, 200, [{ name: 'app' }, { name: 'SJCJ' }]),
  )
  await page.route(/\/api\/datasources\/ds-source\/metadata\/tables/, (route) => {
    tableRequests.push(route.request().url())
    const schema = new URL(route.request().url()).searchParams.get('schema')
    return json(
      route,
      200,
      schema === 'SJCJ'
        ? [{ schema_name: 'SJCJ', name: 'A_KS_CUST_BASE_INFO', table_type: 'TABLE' }]
        : [{ schema_name: 'app', name: 'orders_a', table_type: 'BASE TABLE' }],
    )
  })

  await page.goto('/projects/project-1/compare')
  const sourceSchema = page.getByTestId('compare-source-schema')
  await expect(sourceSchema).toHaveValue('app')
  await sourceSchema.selectOption('SJCJ')

  await expectTableValue(page, 'source', '')
  await selectTable(page, 'source', 'A_KS_CUST_BASE_INFO')
  expect(
    tableRequests.some(
      (url) => new URL(url).searchParams.get('schema') === 'SJCJ',
    ),
  ).toBe(true)
  await expect(page.getByTestId('compare-target-schema')).toHaveValue('app')
  await expectTableValue(page, 'target', 'orders_b')
  expectNoConsoleErrors()
})

test('table picker supports fuzzy and exact matching across thousands of options', async ({ page }) => {
  await mockBase(page)
  const tables = Array.from({ length: 6535 }, (_, index) => ({
    schema_name: 'app',
    name: `table_${String(index).padStart(4, '0')}`,
    table_type: 'BASE TABLE',
  }))
  tables.push({
    schema_name: 'app',
    name: 'orders_a',
    table_type: 'BASE TABLE',
  })
  tables.push({
    schema_name: 'app',
    name: 'ZZ_NEEDLE_CUSTOMER',
    table_type: 'BASE TABLE',
  })
  await page.route(/\/api\/datasources\/ds-source\/metadata\/tables/, (route) =>
    json(route, 200, tables),
  )

  await page.goto('/projects/project-1/compare')
  const testId = 'compare-source-table'
  await expectTableValue(page, 'source', 'orders_a')
  await page.getByTestId(testId).click()
  const options = page.getByTestId(`${testId}-options`)
  const search = page.getByTestId(`${testId}-search`)
  await expect(search).toBeFocused()
  await expect(search).toHaveAttribute('role', 'combobox')
  await expect(search).toHaveAttribute('aria-controls', `${testId}-options`)
  await expect(search).toHaveAttribute('aria-activedescendant', `${testId}-option-0`)
  await expect(options.getByRole('option')).toHaveCount(200)
  await expect(options.getByRole('option').first()).toHaveText('orders_a')
  await search.press('Enter')
  await expectTableValue(page, 'source', 'orders_a')
  await expect(page.getByTestId(testId)).toBeFocused()

  await page.getByTestId(testId).click()
  await search.fill('znc')
  await expect(
    options.getByRole('option', {
      name: 'ZZ_NEEDLE_CUSTOMER',
      exact: true,
    }),
  ).toBeVisible()

  await page.getByTestId(`${testId}-mode-exact`).click()
  await expect(page.getByText('No matching tables')).toBeVisible()
  await expectTableValue(page, 'source', 'orders_a')
  await page.getByTestId(`${testId}-mode-exact`).press('Tab')
  await expect(options).toHaveCount(0)

  await page.getByTestId(testId).click()
  await search.fill('znc')
  await page.getByTestId(`${testId}-mode-exact`).click()
  await page.getByTestId(`${testId}-mode-exact`).press('Escape')
  await expect(options).toHaveCount(0)
  await expect(page.getByTestId(testId)).toBeFocused()

  await page.getByTestId(testId).click()
  await search.fill('zz_needle_customer')
  await expect(
    options.getByRole('option', {
      name: 'ZZ_NEEDLE_CUSTOMER',
      exact: true,
    }),
  ).toBeVisible()

  await search.fill('needle')
  await expect(page.getByText('No matching tables')).toBeVisible()
  await page.getByTestId(`${testId}-mode-fuzzy`).click()
  await options.getByRole('option', { name: 'ZZ_NEEDLE_CUSTOMER', exact: true }).click()
  await expectTableValue(page, 'source', 'ZZ_NEEDLE_CUSTOMER')
  await expect(page.getByTestId(testId)).toBeFocused()
  expectNoConsoleErrors()
})

test('auto-infer shows PK candidates + mapping draft for confirmation', async ({ page }) => {
  await mockBase(page)
  await page.route('**/api/projects/project-1/compare/infer', (r) => json(r, 200, inferResponse))

  await page.goto('/projects/project-1/compare')
  // 填表名后推断
  await selectTable(page, 'source', 'orders')
  await selectTable(page, 'target', 'orders')
  await page.getByRole('button', { name: 'Auto-infer' }).click()

  await expect(page.getByText('Primary key candidates')).toBeVisible()
  // 推断来源 badge + 主键左右列对齐
  await expect(page.getByText('primary key', { exact: true })).toBeVisible()
  await expect(page.locator('text=id').first()).toBeVisible()
  // 列映射候选 reason badge
  await expect(page.getByText('Column mappings', { exact: true })).toBeVisible()
  expectNoConsoleErrors()
})

test('needs_manual_pk blocks full run and surfaces a warning', async ({ page }) => {
  await mockBase(page)
  await page.route('**/api/projects/project-1/compare/infer', (r) =>
    json(r, 200, {
      ...inferResponse,
      needs_manual_pk: true,
      pk_candidates: [],
      compare_rules: { ...inferResponse.compare_rules, key_columns: [] },
    }),
  )

  await page.goto('/projects/project-1/compare')
  await selectTable(page, 'source', 'orders')
  await selectTable(page, 'target', 'orders')
  await page.getByRole('button', { name: 'Auto-infer' }).click()
  await expect(page.getByText(/Could not infer a primary key/)).toBeVisible()
  expectNoConsoleErrors()
})

test('suggestions list table pairs and create a draft task', async ({ page }) => {
  await mockBase(page)
  await page.route(/\/api\/projects\/project-1\/compare\/suggest-tasks/, (r) =>
    json(r, 200, {
      suggestions: [
        {
          source_schema: 'src',
          source_table: 'orders',
          target_schema: 'dst',
          target_table: 'orders',
          confidence: 1.0,
          reason: 'exact',
        },
        {
          source_schema: 'src',
          source_table: 'src_customer',
          target_schema: 'dst',
          target_table: 'customer',
          confidence: 0.8,
          reason: 'normalized',
        },
      ],
    }),
  )
  await page.route('**/api/projects/project-1/compare/draft-task', (r) =>
    json(r, 201, compareTask({ id: 'task-2', name: 'orders draft' })),
  )

  await page.goto('/projects/project-1/compare')
  await page.getByRole('button', { name: 'Find table pairs' }).click()
  await expect(page.getByText('src_customer')).toBeVisible()
  await expect(page.getByText('normalized', { exact: true })).toBeVisible()
  expectNoConsoleErrors()
})

test('generated expression preview shows summary and full expression', async ({ page }) => {
  await mockBase(page)
  const expression =
    "CASE WHEN BALANCE > 100000 THEN 'HIGH' WHEN BALANCE > 10000 THEN 'MEDIUM' ELSE 'LOW' END"
  await page.route('**/api/projects/project-1/compare/preview', (r) =>
    json(r, 200, {
      columns: ['CUST_NO', 'RESULT_1'],
      column_details: [
        { name: 'CUST_NO', generated: false, projection_index: 1, expression: null },
        { name: 'RESULT_1', generated: true, projection_index: 2, expression },
      ],
      rows: [['C1', 'HIGH']],
      row_count: 1,
      truncated: false,
    }),
  )

  await page.goto('/projects/project-1/compare')
  await page.getByRole('button', { name: 'Custom SQL' }).first().click()
  await page.locator('textarea').first().fill(`SELECT CUST_NO, ${expression} FROM ACCOUNT`)
  await page.getByRole('button', { name: 'Preview', exact: true }).first().click()

  const inspect = page.getByRole('button', { name: 'Inspect expression for RESULT_1' })
  await expect(inspect).toBeVisible()
  await expect(inspect).toHaveAttribute('title', /CASE WHEN BALANCE/)
  await inspect.click()
  await expect(page.getByText('Original projection expression')).toBeVisible()
  await expect(page.getByText(expression, { exact: true })).toBeVisible()

  await page.getByRole('button', { name: 'Close expression details' }).click()
  page.once('dialog', (dialog) => dialog.accept())
  await page.getByRole('button', { name: 'Adopt as source columns' }).click()
  await expect(page.locator('input[placeholder="Column"]').nth(1)).toHaveValue('RESULT_1')
  expectNoConsoleErrors()
})

test('target generated aliases expose the target SQL expression', async ({ page }) => {
  const task = compareTask({
    columns: [{ name: 'RESULT_1', type: 'decimal' }],
    source_projection_details: [
      {
        name: 'RESULT_1',
        generated: true,
        projection_index: 1,
        expression: 'SUM(SOURCE_AMOUNT)',
      },
    ],
    target_projection_details: [
      {
        name: 'RESULT_1',
        generated: true,
        projection_index: 1,
        expression: 'SUM(TARGET_FARE)',
      },
    ],
  })
  await mockBase(page)
  await page.route(/\/api\/compare\/tasks(\?|$)/, (r) => json(r, 200, [task]))

  await page.goto('/projects/project-1/compare')
  const buttons = page.getByRole('button', { name: 'Inspect expression for RESULT_1' })
  await expect(buttons).toHaveCount(2)
  await buttons.nth(1).click()
  await expect(page.getByText('SUM(TARGET_FARE)', { exact: true })).toBeVisible()
  await expect(page.getByText('SUM(SOURCE_AMOUNT)', { exact: true })).toHaveCount(0)
  expectNoConsoleErrors()
})

test('missing target projection details keeps the compare editor stable', async ({ page }) => {
  const task = compareTask({
    columns: [{ name: 'RESULT_1', type: 'decimal' }],
    source_projection_details: [
      {
        name: 'RESULT_1',
        generated: true,
        projection_index: 1,
        expression: 'SUM(SOURCE_AMOUNT)',
      },
    ],
    target_projection_details: [],
  })
  await mockBase(page)
  await page.route(/\/api\/compare\/tasks(\?|$)/, (r) => json(r, 200, [task]))

  await page.goto('/projects/project-1/compare')
  await expect(
    page.getByRole('button', { name: 'Inspect expression for RESULT_1' }),
  ).toHaveCount(1)
  await expect(page.locator('input[placeholder="Column"]').first()).toHaveValue('RESULT_1')
  expectNoConsoleErrors()
})

test('legacy aliases can be repaired and saved through the task API', async ({ page }) => {
  const expressions = ['SUM(A)', 'SUM(B)', 'SUM(C)']
  const baseNames = ['OCCUR_DATE', 'CUST_NO', 'SEC_CODE', 'SEC_TYPE', 'BUSINESS_CODE']
  const details = [
    ...baseNames.map((name, index) => ({
      name,
      generated: false,
      projection_index: index + 1,
      expression: null,
    })),
    ...expressions.map((expression, index) => ({
      name: `RESULT_${index + 1}`,
      generated: true,
      projection_index: index + 6,
      expression,
    })),
  ]
  const sql =
    'SELECT OCCUR_DATE, CUST_NO, SEC_CODE, SEC_TYPE, BUSINESS_CODE, SUM(A), SUM(B), SUM(C) FROM T GROUP BY OCCUR_DATE, CUST_NO, SEC_CODE, SEC_TYPE, BUSINESS_CODE'
  const legacyTask = compareTask({
    name: 'legacy aggregate',
    source_ref: { kind: 'sql', sql },
    target_ref: { kind: 'sql', sql },
    columns: [...baseNames, '6', '7', '8'].map((name) => ({ name, type: 'string' })),
    source_projection_details: details,
    target_projection_details: details,
    compare_rules: {
      ...compareTask().compare_rules,
      key_columns: ['6'],
      ignore_columns: ['7'],
      column_mappings: { '6': '6', '7': '7', '8': '8' },
    },
  })
  await mockBase(page)
  await page.route(/\/api\/compare\/tasks(\?|$)/, (r) => json(r, 200, [legacyTask]))
  let patchBody: Record<string, unknown> | null = null
  await page.route('**/api/compare/tasks/task-1', async (r) => {
    if (r.request().method() !== 'PATCH') return r.fallback()
    patchBody = r.request().postDataJSON() as Record<string, unknown>
    return json(r, 200, { ...legacyTask, ...patchBody })
  })

  await page.goto('/projects/project-1/compare')
  await expect(page.getByText(/legacy unnamed expression columns/i)).toBeVisible()
  page.once('dialog', (dialog) => dialog.accept())
  await page.getByRole('button', { name: 'Update generated aliases' }).click()

  await expect.poll(() => patchBody).not.toBeNull()
  const serialized = JSON.stringify(patchBody)
  expect(serialized).toContain('RESULT_1')
  expect(serialized).toContain('RESULT_2')
  expect(serialized).toContain('RESULT_3')
  expect(serialized).not.toMatch(/"(?:6|7|8)"/)
  expectNoConsoleErrors()
})

test('execution failed state does not render zero buckets as empty results', async ({ page }) => {
  await mockBase(page)
  await page.route('**/api/compare/tasks/task-1/run', (r) =>
    json(r, 202, { job_id: 'job-failed', run_id: 'run-failed' }),
  )
  await page.route('**/api/jobs/job-failed', (r) =>
    json(r, 200, {
      id: 'job-failed',
      kind: 'compare_run',
      status: 'failed',
      created_at: now,
      finished_at: now,
      error: 'sql_failed',
      error_code: 'sql_failed',
      message: null,
      result_set_id: null,
    }),
  )

  await page.goto('/projects/project-1/compare')
  await page.getByRole('button', { name: 'Start compare' }).click()

  await expect(page.getByText('Compare execution failed')).toBeVisible()
  await expect(page.getByText('No rows in this bucket.')).toBeHidden()
  await expect(page.getByRole('button', { name: /^Changed\s+0/ })).toBeHidden()
  expectNoConsoleErrors()
})

test('pending compare response cannot restart polling after route unmount', async ({ page }) => {
  await mockBase(page)
  await page.route('**/api/projects', (r) => json(r, 200, []))
  await page.route('**/api/compare/tasks/task-1/run', (r) =>
    json(r, 202, { job_id: 'job-pending', run_id: 'run-pending' }),
  )

  let jobReads = 0
  const firstJobRead = deferred()
  const jobResponseGate = deferred()
  await page.route('**/api/jobs/job-pending', async (r) => {
    jobReads += 1
    firstJobRead.resolve()
    await jobResponseGate.promise
    await json(r, 200, {
      id: 'job-pending',
      kind: 'compare_run',
      status: 'running',
      created_at: now,
      finished_at: null,
      error: null,
      error_code: null,
      message: null,
      result_set_id: null,
    })
  })

  await page.goto('/projects/project-1/compare')
  await page.getByRole('button', { name: 'Start compare' }).click()
  await firstJobRead.promise

  await page.getByRole('button', { name: 'Projects', exact: true }).click()
  await expect(page).toHaveURL(/\/projects$/)
  jobResponseGate.resolve()

  // Compare polls every 800 ms. A stale response used to schedule a second request here.
  await page.waitForTimeout(1200)
  expect(jobReads).toBe(1)
  expectNoConsoleErrors()
})

test('stale terminal results cannot overwrite a newer compare run', async ({ page }) => {
  await mockBase(page, [compareTask(), compareTask({ id: 'task-2', name: 'orders-next' })])
  await page.route('**/api/compare/tasks/task-1/run', (r) =>
    json(r, 202, { job_id: 'job-stale', run_id: 'run-stale' }),
  )
  await page.route('**/api/compare/tasks/task-2/run', (r) =>
    json(r, 202, { job_id: 'job-current', run_id: 'run-current' }),
  )
  await page.route('**/api/jobs/job-stale', (r) =>
    json(r, 200, {
      id: 'job-stale',
      kind: 'compare_run',
      status: 'success',
      created_at: now,
      finished_at: now,
      error: null,
      error_code: null,
      message: null,
      result_set_id: null,
    }),
  )
  const currentJobRead = deferred()
  await page.route('**/api/jobs/job-current', async (r) => {
    currentJobRead.resolve()
    await json(r, 200, {
      id: 'job-current',
      kind: 'compare_run',
      status: 'success',
      created_at: now,
      finished_at: now,
      error: null,
      error_code: null,
      message: null,
      result_set_id: null,
    })
  })

  const staleResultRead = deferred()
  const staleResultGate = deferred()
  await page.route(/\/api\/compare\/runs\/run-stale\/results/, async (r) => {
    staleResultRead.resolve()
    await staleResultGate.promise
    await json(r, 200, {
      job_id: 'job-stale',
      run_id: 'run-stale',
      bucket: 'diff',
      offset: 0,
      limit: 100,
      bucket_counts: bucketCounts,
      progress,
      diff_profile: diffProfile,
      sample_result: null,
      rows: [
        {
          pk: { id: 1 },
          source: { id: 1, amount: 'stale-source' },
          target: { id: 1, amount: 'stale-target' },
          cells: [{ column: 'amount', source: 'stale-source', target: 'stale-target' }],
        },
      ],
    })
  })
  await page.route(/\/api\/compare\/runs\/run-stale\/profile/, async (r) => {
    await staleResultGate.promise
    await json(r, 200, {
      job_id: 'job-stale',
      run_id: 'run-stale',
      bucket_counts: bucketCounts,
      progress,
      diff_profile: diffProfile,
      sample_result: null,
    })
  })
  const currentResultRead = deferred()
  await page.route(/\/api\/compare\/runs\/run-current\/results/, async (r) => {
    currentResultRead.resolve()
    await json(r, 200, {
      job_id: 'job-current',
      run_id: 'run-current',
      bucket: 'diff',
      offset: 0,
      limit: 100,
      bucket_counts: bucketCounts,
      progress,
      diff_profile: diffProfile,
      sample_result: null,
      rows: [
        {
          pk: { id: 2 },
          source: { id: 2, amount: 'current-source' },
          target: { id: 2, amount: 'current-target' },
          cells: [{ column: 'amount', source: 'current-source', target: 'current-target' }],
        },
      ],
    })
  })
  await page.route(/\/api\/compare\/runs\/run-current\/profile/, (r) =>
    json(r, 200, {
      job_id: 'job-current',
      run_id: 'run-current',
      bucket_counts: bucketCounts,
      progress,
      diff_profile: diffProfile,
      sample_result: null,
    }),
  )

  await page.goto('/projects/project-1/compare')
  await page.getByRole('button', { name: 'Start compare' }).click()
  await staleResultRead.promise

  await page.locator('aside').getByText('orders-next', { exact: true }).click()
  await expect(page.getByRole('button', { name: 'orders-next', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Start compare' }).click()
  await currentJobRead.promise
  await currentResultRead.promise
  await expect(page.getByText(/current-source/)).toBeVisible()

  staleResultGate.resolve()
  await page.waitForTimeout(200)
  await expect(page.getByText(/current-source/)).toBeVisible()
  await expect(page.getByText(/stale-source/)).toBeHidden()
  expectNoConsoleErrors()
})

test('large diff pages avoid per-cell linear scans while preserving pagination', async ({ page }) => {
  const valueColumns = Array.from({ length: 12 }, (_, index) => `value_${String(index).padStart(2, '0')}`)
  const task = compareTask({
    columns: [
      { name: 'id', type: 'integer', driver_type: 'INT', nullable: false, primary_key: true },
      ...valueColumns.map((name) => ({
        name,
        type: 'string',
        driver_type: 'VARCHAR',
        nullable: true,
        primary_key: false,
      })),
    ],
    compare_rules: {
      ...compareTask().compare_rules,
      key_columns: ['id'],
      column_mappings: {},
    },
  })
  await mockBase(page, [task])

  const failedRequests: string[] = []
  const errorResponses: string[] = []
  page.on('requestfailed', (request) => {
    failedRequests.push(`${request.method()} ${new URL(request.url()).pathname}`)
  })
  page.on('response', (response) => {
    if (response.url().includes('/api/') && response.status() >= 400) {
      errorResponses.push(`${response.status()} ${new URL(response.url()).pathname}`)
    }
  })

  await page.addInitScript(() => {
    const metrics = { calls: 0, predicateChecks: 0 }
    Object.defineProperty(window, '__compareCellFindMetrics', {
      configurable: true,
      value: metrics,
    })
    const originalFind = Array.prototype.find
    const instrumentedFind = function <T>(
      this: T[],
      predicate: (value: T, index: number, array: T[]) => unknown,
      thisArg?: unknown,
    ): T | undefined {
      const first = this[0]
      const isCompareCellArray = Boolean(
        first &&
          typeof first === 'object' &&
          'column' in first &&
          ('source' in first || 'target' in first),
      )
      if (!isCompareCellArray) {
        return Reflect.apply(originalFind, this, [predicate, thisArg]) as T | undefined
      }
      metrics.calls += 1
      const countedPredicate = (value: T, index: number, array: T[]): unknown => {
        metrics.predicateChecks += 1
        return predicate.call(thisArg, value, index, array)
      }
      return Reflect.apply(originalFind, this, [countedPredicate, thisArg]) as T | undefined
    }
    Object.defineProperty(Array.prototype, 'find', {
      configurable: true,
      value: instrumentedFind,
      writable: true,
    })
  })

  await page.route('**/api/compare/tasks/task-1/run', (r) =>
    json(r, 202, { job_id: 'job-large-diff', run_id: 'run-large-diff' }),
  )
  await page.route('**/api/jobs/job-large-diff', (r) =>
    json(r, 200, {
      id: 'job-large-diff',
      kind: 'compare_run',
      status: 'success',
      created_at: now,
      finished_at: now,
      error: null,
      error_code: null,
      message: null,
      result_set_id: null,
    }),
  )

  const resultOffsets: number[] = []
  await page.route(/\/api\/compare\/runs\/run-large-diff\/results/, (r) => {
    const offset = Number(new URL(r.request().url()).searchParams.get('offset') ?? 0)
    resultOffsets.push(offset)
    const rows = Array.from({ length: 100 }, (_, rowOffset) => {
      const rowIndex = offset + rowOffset
      return {
        pk: { id: rowIndex },
        source: null,
        target: null,
        cells: valueColumns.map((column, columnIndex) => ({
          column,
          source: `source-${rowIndex}-${columnIndex}`,
          target: `target-${rowIndex}-${columnIndex}`,
        })),
      }
    })
    return json(r, 200, {
      job_id: 'job-large-diff',
      run_id: 'run-large-diff',
      bucket: 'diff',
      offset,
      limit: 100,
      bucket_counts: { only_source: 0, only_target: 0, diff: 200, same: 0 },
      progress,
      diff_profile: diffProfile,
      sample_result: null,
      rows,
    })
  })
  await page.route(/\/api\/compare\/runs\/run-large-diff\/profile/, (r) =>
    json(r, 200, {
      job_id: 'job-large-diff',
      run_id: 'run-large-diff',
      bucket_counts: { only_source: 0, only_target: 0, diff: 200, same: 0 },
      progress,
      diff_profile: diffProfile,
      sample_result: null,
    }),
  )

  await page.goto('/projects/project-1/compare')
  await page.getByRole('button', { name: 'Start compare' }).click()
  const firstSourceValue = page.getByText('S: source-0-0', { exact: true })
  await expect(firstSourceValue).toBeVisible()
  const resultBody = page.locator('tbody').filter({ has: firstSourceValue })
  await expect(resultBody.locator('tr')).toHaveCount(100)
  const initialCellCount = await resultBody.locator('td').count()

  await page.getByRole('button', { name: 'Load 100 more' }).click()
  await expect(resultBody.locator('tr')).toHaveCount(200)
  const appendedCellCount = await resultBody.locator('td').count()
  const metrics = await page.evaluate(() => {
    const candidate = window as typeof window & {
      __compareCellFindMetrics?: { calls: number; predicateChecks: number }
    }
    return candidate.__compareCellFindMetrics ?? { calls: -1, predicateChecks: -1 }
  })

  expect(initialCellCount).toBe(1300)
  expect(appendedCellCount).toBe(2600)
  expect(resultOffsets).toEqual([0, 100])
  expect(metrics.calls).toBe(0)
  expect(metrics.predicateChecks).toBe(0)
  expect(failedRequests).toEqual([])
  expect(errorResponses).toEqual([])
  expectNoConsoleErrors()
})

test('run a compare, poll the job, then render 4 buckets with split cells', async ({ page }) => {
  await mockBase(page)
  await page.route('**/api/compare/tasks/task-1/run', (r) =>
    json(r, 202, { job_id: 'job-1', run_id: 'run-1' }),
  )
  let reads = 0
  await page.route('**/api/jobs/job-1', (r) => {
    reads += 1
    return json(r, 200, {
      id: 'job-1',
      kind: 'compare_run',
      status: reads <= 1 ? 'running' : 'success',
      created_at: now,
      finished_at: reads <= 1 ? null : now,
      error: null,
      error_code: null,
      message: null,
      result_set_id: null,
    })
  })
  await page.route(/\/api\/compare\/runs\/run-1\/results/, (r) =>
    json(r, 200, {
      job_id: 'job-1',
      run_id: 'run-1',
      bucket: 'diff',
      offset: 0,
      limit: 100,
      bucket_counts: bucketCounts,
      progress,
      diff_profile: diffProfile,
      sample_result: null,
      rows: [
        {
          pk: { id: 3 },
          source: { id: 3, amount: '10.00' },
          target: { id: 3, amount: '11.00' },
          cells: [{ column: 'amount', source: '10.00', target: '11.00' }],
        },
      ],
    }),
  )
  await page.route(/\/api\/compare\/runs\/run-1\/profile/, (r) =>
    json(r, 200, {
      job_id: 'job-1',
      run_id: 'run-1',
      bucket_counts: bucketCounts,
      progress,
      diff_profile: diffProfile,
      sample_result: null,
    }),
  )

  await page.route('**/api/compare/runs/run-1/export', (r) =>
    json(r, 202, {
      job_id: 'export-job-1',
      download_token: 'tok-compare',
      expires_at: '2026-06-12T07:00:00Z',
      format: 'excel',
      filename: 'compare-run-1.xlsx',
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
  await page.route('**/api/exports/tok-compare', (r) =>
    r.fulfill({
      status: 200,
      headers: {
        'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'Content-Disposition': 'attachment; filename="compare-run-1.xlsx"',
      },
      body: 'xlsx',
    }),
  )

  await page.goto('/projects/project-1/compare')
  await page.getByRole('button', { name: 'Start compare' }).click()

  // 4 桶徽章计数
  await expect(page.getByText('Changed').first()).toBeVisible()
  await expect(page.getByText('7', { exact: true })).toBeVisible() // same count
  // 单元格分裂:旧值红删除线 + 新值绿
  await expect(page.getByText('10.00')).toBeVisible()
  await expect(page.getByText('11.00')).toBeVisible()
  // 进度摘要
  await expect(page.getByText(/Scanned segments/)).toBeVisible()

  await page.getByRole('button', { name: 'Export' }).click()
  await expect(page.getByText(/Export ready: compare-run-1\.xlsx/)).toBeVisible()
  const downloadPromise = page.waitForEvent('download')
  await page.getByRole('button', { name: 'Download', exact: true }).click()
  const download = await downloadPromise
  expect(download.suggestedFilename()).toBe('compare-run-1.xlsx')
  expectNoConsoleErrors()
})

test('sample quick check card shows confidence bound and not-full caveat', async ({ page }) => {
  await mockBase(page)
  await page.route('**/api/compare/tasks/task-1/run', (r) =>
    json(r, 202, { job_id: 'job-1', run_id: 'run-1' }),
  )
  await page.route('**/api/jobs/job-1', (r) =>
    json(r, 200, {
      id: 'job-1',
      kind: 'compare_run',
      status: 'success',
      created_at: now,
      finished_at: now,
      error: null,
      error_code: null,
      message: null,
      result_set_id: null,
    }),
  )
  await page.route(/\/api\/compare\/runs\/run-1\/results/, (r) =>
    json(r, 200, {
      job_id: 'job-1',
      run_id: 'run-1',
      bucket: 'diff',
      offset: 0,
      limit: 100,
      bucket_counts: { only_source: 0, only_target: 0, diff: 0, same: 0 },
      progress,
      diff_profile: diffProfile,
      sample_result: sampleResult,
      rows: [],
    }),
  )
  await page.route(/\/api\/compare\/runs\/run-1\/profile/, (r) =>
    json(r, 200, {
      job_id: 'job-1',
      run_id: 'run-1',
      bucket_counts: { only_source: 0, only_target: 0, diff: 0, same: 0 },
      progress,
      diff_profile: diffProfile,
      sample_result: sampleResult,
    }),
  )

  await page.goto('/projects/project-1/compare')
  await page.getByRole('button', { name: 'Start compare' }).click()
  await expect(page.getByText(/difference rate upper bound/)).toBeVisible()
  await expect(page.getByText(/does not guarantee full-set equality/)).toBeVisible()
  expectNoConsoleErrors()
})

test('diff profile panel renders rates; AI explain overlays without blocking profile', async ({ page }) => {
  await mockBase(page)
  await page.route('**/api/compare/tasks/task-1/run', (r) =>
    json(r, 202, { job_id: 'job-1', run_id: 'run-1' }),
  )
  await page.route('**/api/jobs/job-1', (r) =>
    json(r, 200, {
      id: 'job-1',
      kind: 'compare_run',
      status: 'success',
      created_at: now,
      finished_at: now,
      error: null,
      error_code: null,
      message: null,
      result_set_id: null,
    }),
  )
  await page.route(/\/api\/compare\/runs\/run-1\/results/, (r) =>
    json(r, 200, {
      job_id: 'job-1',
      run_id: 'run-1',
      bucket: 'diff',
      offset: 0,
      limit: 100,
      bucket_counts: bucketCounts,
      progress,
      diff_profile: diffProfile,
      sample_result: null,
      rows: [],
    }),
  )
  await page.route(/\/api\/compare\/runs\/run-1\/profile/, (r) =>
    json(r, 200, {
      job_id: 'job-1',
      run_id: 'run-1',
      bucket_counts: bucketCounts,
      progress,
      diff_profile: diffProfile,
      sample_result: null,
    }),
  )
  // AI 关闭降级 —— 画像照常显示
  await page.route('**/api/compare/runs/run-1/ai-attribution', (r) =>
    json(r, 200, {
      run_id: 'run-1',
      ok: false,
      attribution: null,
      provider: null,
      model: null,
      error: 'ai_disabled',
      egress_level: 2,
    }),
  )

  await page.goto('/projects/project-1/compare')
  await page.getByRole('button', { name: 'Start compare' }).click()
  await page.getByRole('button', { name: 'Diff profile' }).click()

  await expect(page.getByText('Per-column diff rate')).toBeVisible()
  await expect(page.getByText('Systematic bias')).toBeVisible()

  await page.getByRole('button', { name: 'AI explain diff' }).click()
  await expect(page.getByText(/AI is disabled or unavailable/)).toBeVisible()
  // 画像仍在
  await expect(page.getByText('Per-column diff rate')).toBeVisible()
  expectNoConsoleErrors()
})

test('in-grace mode disables the primary compare write entry', async ({ page }) => {
  await mockBase(page)
  await mockLicense(page, { mode: 'in_grace' })
  await page.route(/\/api\/projects\/project-1\/compare\/suggest-tasks/, (route) =>
    json(route, 200, {
      suggestions: [
        {
          source_schema: 'src',
          source_table: 'orders',
          target_schema: 'dst',
          target_table: 'orders',
          confidence: 1.0,
          reason: 'exact',
        },
      ],
    }),
  )
  let draftWrites = 0
  await page.route('**/api/projects/project-1/compare/draft-task', (route) => {
    draftWrites += 1
    return json(route, 201, compareTask({ id: 'blocked-draft' }))
  })

  await page.goto('/projects/project-1/compare')
  const run = page.getByRole('button', { name: 'Start compare' })
  await expect(run).toBeDisabled()
  await expect(run).toHaveAttribute(
    'title',
    'Write actions are disabled in the current license state (view / license update only)',
  )
  await page.getByRole('button', { name: 'Find table pairs' }).click()
  const draft = page.locator('aside').getByTitle(
    'Write actions are disabled in the current license state (view / license update only)',
  ).last()
  await expect(draft).toBeDisabled()
  expect(draftWrites).toBe(0)
  expectNoConsoleErrors()
})

/* ──────────────────────────────────────────────────────────────────────────
 * 方案 A:Compare 横向铺满 + source/target/single SQL 放大还原
 * ────────────────────────────────────────────────────────────────────────── */

/** 旧 max-w-4xl = 56rem = 896px;铺满后正文必须显著超过它。 */
const LEGACY_MAX_W_4XL = 896

async function sourceCustomSql(page: Page): Promise<void> {
  await page
    .getByTestId('compare-source-fieldset')
    .getByRole('button', { name: 'Custom SQL', exact: true })
    .click()
}

async function targetCustomSql(page: Page): Promise<void> {
  await page
    .getByTestId('compare-target-fieldset')
    .getByRole('button', { name: 'Custom SQL', exact: true })
    .click()
}

test('compare editor body fills the container and keeps source/target on one row', async ({
  page,
}) => {
  await mockBase(page)
  await page.setViewportSize({ width: 1600, height: 960 })
  await page.goto('/projects/project-1/compare')

  const body = page.getByTestId('compare-editor-body')
  await expect(body).toBeVisible()
  const bodyBox = await body.boundingBox()
  const containerWidth = await body.evaluate((el) => (el.parentElement as HTMLElement).clientWidth)
  expect(bodyBox!.width).toBeGreaterThan(LEGACY_MAX_W_4XL)
  expect(bodyBox!.width).toBeGreaterThanOrEqual(containerWidth - 1)

  // 源 / 目标仍是同一行的双栏,大屏不得堆叠。
  const sourceBox = await page.getByTestId('compare-source-fieldset').boundingBox()
  const targetBox = await page.getByTestId('compare-target-fieldset').boundingBox()
  expect(targetBox!.x).toBeGreaterThan(sourceBox!.x + sourceBox!.width - 1)
  expect(Math.abs(targetBox!.y - sourceBox!.y)).toBeLessThan(4)
  expectNoConsoleErrors()
})

test('source and target previews stay inside their own column', async ({ page }) => {
  await mockBase(page)
  await page.route('**/api/projects/project-1/compare/preview', (r) =>
    json(r, 200, {
      columns: ['CUST_NO'],
      column_details: [
        { name: 'CUST_NO', generated: false, projection_index: 1, expression: null },
      ],
      rows: [['C1']],
      row_count: 1,
      truncated: false,
    }),
  )
  await page.setViewportSize({ width: 1600, height: 960 })
  await page.goto('/projects/project-1/compare')

  const sourceCol = page.getByTestId('compare-source-fieldset')
  const targetCol = page.getByTestId('compare-target-fieldset')

  await sourceCol.getByRole('button', { name: 'Preview', exact: true }).click()
  const sourcePreview = page.getByTestId('compare-source-preview')
  await expect(sourcePreview).toBeVisible()
  await expect(sourceCol.getByTestId('compare-source-preview')).toHaveCount(1)
  await expect(targetCol.getByTestId('compare-source-preview')).toHaveCount(0)

  await targetCol.getByRole('button', { name: 'Preview', exact: true }).click()
  const targetPreview = page.getByTestId('compare-target-preview')
  await expect(targetPreview).toBeVisible()
  await expect(targetCol.getByTestId('compare-target-preview')).toHaveCount(1)
  await expect(sourceCol.getByTestId('compare-target-preview')).toHaveCount(0)

  // 两个预览仍在各自列内,且左右分列(不是整行铺开)。
  const sourcePreviewBox = await sourcePreview.boundingBox()
  const targetPreviewBox = await targetPreview.boundingBox()
  const sourceColBox = await sourceCol.boundingBox()
  const targetColBox = await targetCol.boundingBox()
  expect(sourcePreviewBox!.x).toBeGreaterThanOrEqual(sourceColBox!.x - 1)
  expect(sourcePreviewBox!.x + sourcePreviewBox!.width).toBeLessThanOrEqual(
    sourceColBox!.x + sourceColBox!.width + 1,
  )
  expect(targetPreviewBox!.x).toBeGreaterThanOrEqual(targetColBox!.x - 1)
  expect(targetPreviewBox!.x).toBeGreaterThan(sourcePreviewBox!.x)

  // 关闭预览的交互保留。
  await sourcePreview.getByTitle('Close preview').click()
  await expect(sourcePreview).toHaveCount(0)
  expectNoConsoleErrors()
})

test('source SQL expands to a modal Monaco and restores the same content', async ({ page }) => {
  await mockBase(page, [
    compareTask({
      source_ref: { kind: 'sql', sql: 'SELECT 1 FROM INLINE_HISTORY' },
    }),
  ])
  await page.setViewportSize({ width: 1600, height: 960 })
  await page.goto('/projects/project-1/compare')

  const inline = page.getByTestId('compare-source-fieldset').locator('.monaco-editor').first()
  await expect(inline).toBeVisible()
  const inlineTextarea = inline.locator('textarea').first()
  await expect(inline.locator('.view-lines')).toContainText('INLINE_HISTORY')

  await page.getByTestId('compare-source-sql-expand').click()
  const modal = page.getByTestId('compare-source-sql-expand-modal')
  await expect(modal).toBeVisible()
  await expect(modal).toHaveAttribute('aria-modal', 'true')
  await expect(modal).toHaveAttribute('aria-label', 'Source SQL — expanded editor')
  await expect(modal.locator('.monaco-editor').first()).toBeVisible()

  // 模态键盘焦点不得越过遮罩落到背景页面。
  const collapse = page.getByTestId('compare-source-sql-collapse')
  await collapse.focus()
  await page.keyboard.press('Shift+Tab')
  await expect
    .poll(() => modal.evaluate((element) => element.contains(document.activeElement)))
    .toBe(true)

  // 放大层几乎铺满视口。
  const modalBox = await modal.boundingBox()
  expect(modalBox!.width).toBeGreaterThan(1400)
  expect(modalBox!.height).toBeGreaterThan(800)

  const modalTextarea = modal.locator('textarea').first()
  await modalTextarea.focus()
  await page.keyboard.press('Control+a')
  await page.keyboard.insertText('SELECT 1 FROM SRC_EXPANDED')
  await page.getByTestId('compare-source-sql-collapse').click()
  await expect(modal).not.toBeVisible()
  await expect(page.getByTestId('compare-source-sql-expand')).toBeFocused()

  // 同一个 v-model:还原后原编辑器内容一致。
  const inlineLines = inline.locator('.view-lines')
  await expect(inlineLines).toContainText('SRC_EXPANDED')
  await inlineTextarea.focus()
  for (let undo = 0; undo < 12; undo += 1) {
    if ((await inlineLines.innerText()).includes('INLINE_HISTORY')) break
    await page.keyboard.press('Control+z')
  }
  await expect(inlineLines).toContainText('INLINE_HISTORY')
  expectNoConsoleErrors()
})

test('target SQL expands and restores', async ({ page }) => {
  await mockBase(page)
  await page.setViewportSize({ width: 1600, height: 960 })
  await page.goto('/projects/project-1/compare')
  await targetCustomSql(page)

  const inline = page.getByTestId('compare-target-fieldset').locator('.monaco-editor').first()
  await expect(inline).toBeVisible()

  await page.getByTestId('compare-target-sql-expand').click()
  const modal = page.getByTestId('compare-target-sql-expand-modal')
  await expect(modal).toBeVisible()
  await expect(modal).toHaveAttribute('aria-label', 'Target SQL — expanded editor')

  await modal.locator('textarea').first().fill('SELECT 1 FROM TGT_EXPANDED')
  await page.getByTestId('compare-target-sql-collapse').click()
  await expect(modal).not.toBeVisible()
  await expect(inline.locator('.view-lines')).toContainText('TGT_EXPANDED')
  expectNoConsoleErrors()
})

test('single SQL mode expands and restores', async ({ page }) => {
  await mockBase(page)
  await page.setViewportSize({ width: 1600, height: 960 })
  await page.goto('/projects/project-1/compare')
  await page
    .getByText('Single SQL (both sides run the same statement)')
    .locator('input[type="checkbox"]')
    .check()

  await expect(page.getByTestId('compare-single-sql-expand')).toBeVisible()
  await page.getByTestId('compare-single-sql-expand').click()
  const modal = page.getByTestId('compare-single-sql-expand-modal')
  await expect(modal).toBeVisible()
  await expect(modal).toHaveAttribute('aria-label', 'Shared SQL — expanded editor')

  await modal.locator('textarea').first().fill('SELECT 1 FROM SHARED_EXPANDED')
  await page.getByTestId('compare-single-sql-collapse').click()
  await expect(modal).not.toBeVisible()
  await expect(page.locator('.monaco-editor').first().locator('.view-lines')).toContainText(
    'SHARED_EXPANDED',
  )
  expectNoConsoleErrors()
})

test('Escape closes the expanded SQL editor', async ({ page }) => {
  await mockBase(page)
  await page.setViewportSize({ width: 1600, height: 960 })
  await page.goto('/projects/project-1/compare')
  await sourceCustomSql(page)

  await page.getByTestId('compare-source-sql-expand').click()
  const modal = page.getByTestId('compare-source-sql-expand-modal')
  await expect(modal).toBeVisible()
  await modal.locator('textarea').first().fill('SELECT 1 FROM ESC_EXPANDED')

  await page.keyboard.press('Escape')
  await expect(modal).not.toBeVisible()
  await expect(
    page
      .getByTestId('compare-source-fieldset')
      .locator('.monaco-editor')
      .first()
      .locator('.view-lines'),
  ).toContainText('ESC_EXPANDED')
  expectNoConsoleErrors()
})

test('lineage trace section reverses the diff pk onto the upstream table with a risk gate', async ({
  page,
}) => {
  await mockBase(page)
  const bucketCounts = { only_source: 0, only_target: 0, diff: 1, same: 0 }
  const progress = {}
  const diffProfile = { generated: false, columns: {} }
  await page.route('**/api/compare/tasks/task-1/run', (r) =>
    json(r, 202, { job_id: 'job-1', run_id: 'run-1' }),
  )
  await page.route('**/api/jobs/job-1', (r) =>
    json(r, 200, {
      id: 'job-1',
      kind: 'compare_run',
      status: 'success',
      created_at: now,
      finished_at: now,
      error: null,
      error_code: null,
      message: null,
      result_set_id: null,
    }),
  )
  const resultPayload = {
    job_id: 'job-1',
    run_id: 'run-1',
    bucket_counts: bucketCounts,
    progress,
    diff_profile: diffProfile,
    sample_result: null,
  }
  await page.route(/\/api\/compare\/runs\/run-1\/results/, (r) =>
    json(r, 200, {
      ...resultPayload,
      bucket: 'diff',
      offset: 0,
      limit: 100,
      rows: [
        {
          pk: { id: 3 },
          source: { id: 3, amount: '10.00' },
          target: { id: 3, amount: '11.00' },
          cells: [{ column: 'amount', source: '10.00', target: '11.00' }],
        },
      ],
    }),
  )
  await page.route(/\/api\/compare\/runs\/run-1\/profile/, (r) => json(r, 200, resultPayload))
  await page.route(/\/api\/compare\/runs\/run-1\/diff-sql/, (r) =>
    json(r, 200, {
      run_id: 'run-1',
      bucket: 'diff',
      key_columns: ['id'],
      pk_count: 1,
      truncated: false,
      cap: 500,
      source: { available: true, sql: 'SELECT * FROM app.orders WHERE id IN (3)', reason: null },
      target: { available: true, sql: 'SELECT 1', reason: null },
    }),
  )

  const upstreamSql =
    '-- 血缘溯源 · run 0c3f42aa · hop1 · dwd.orders_clean\nSELECT * FROM `dwd`.`orders_clean` WHERE `order_id` IN (3)'
  let traceCalls = 0
  await page.route(/\/api\/compare\/runs\/run-1\/trace-sql/, (r) => {
    traceCalls += 1
    return json(r, 200, {
      run_id: 'run-1',
      bucket: 'diff',
      focus_table: 'ads.orders_agg',
      key_columns: ['id'],
      pk_count: 1,
      truncated: false,
      cap: 500,
      lineage_run_id: '0c3f42aa-9a1b-4d21-8c66-1f2e3d4a5b6c',
      lineage_source_ref: 'etl/orders_daily.sql',
      lineage_matched_by: 'target_table',
      include_inferred: false,
      dialect: 'mysql',
      dialect_assumed: true,
      available: true,
      reason: null,
      hops: [
        {
          depth: 1,
          table: 'dwd.orders_clean',
          available: true,
          sql: upstreamSql,
          reason: null,
          blocked_by: null,
          key_columns: ['order_id'],
          missing_key_columns: [],
          warnings: ['cast_value_mismatch_risk'],
          risks: ['cast_value_mismatch_risk', 'pk_name_stability_assumed'],
          confidence: 0.9,
          edges: [],
        },
        {
          depth: 2,
          table: 'ods.orders_raw',
          available: false,
          sql: null,
          reason: 'non_invertible_transformation',
          blocked_by: 'EXPRESSION',
          key_columns: [],
          missing_key_columns: ['id'],
          warnings: [],
          risks: [],
          confidence: 1,
          edges: [],
        },
      ],
    })
  })

  await page.goto('/projects/project-1/compare')
  await page.getByRole('button', { name: 'Start compare' }).click()
  await page.getByRole('button', { name: 'Locate rows SQL' }).click()
  await page.getByTestId('compare-trace-sql-load').click()

  const trace = page.getByTestId('compare-trace-sql')
  await expect(trace.getByText('Auto-matched lineage record 0c3f42aa')).toBeVisible()
  await expect(trace.getByText('Hop 1 · dwd.orders_clean')).toBeVisible()
  await expect(trace.getByText('Confidence 90%')).toBeVisible()
  await expect(trace.getByText(upstreamSql.split('\n')[1])).toBeVisible()
  // 断链跳如实说明为什么到此为止,不伪造 SQL
  await expect(trace.getByText(/non-invertible transformation \(EXPRESSION\)/)).toBeVisible()

  // 风险确认门:勾选前复制 / 在 SQL 打开都是禁用的
  const copyButton = trace.getByRole('button', { name: 'Copy' })
  await expect(copyButton).toBeDisabled()
  await trace.getByText('I understand the risks above').click()
  await expect(copyButton).toBeEnabled()

  expect(traceCalls).toBe(1)
  expectNoConsoleErrors()
})
