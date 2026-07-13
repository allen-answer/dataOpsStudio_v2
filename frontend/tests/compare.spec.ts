import { test, expect, type Page, type Route } from '@playwright/test'
import { json, mockLicense, seedAdminAuth, trackConsoleErrors } from './helpers'

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

async function mockBase(page: Page): Promise<void> {
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
  await page.route(/\/api\/compare\/tasks(\?|$)/, (r: Route) => {
    if (r.request().method() === 'GET') return json(r, 200, [compareTask()])
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

test('auto-infer shows PK candidates + mapping draft for confirmation', async ({ page }) => {
  await mockBase(page)
  await page.route('**/api/projects/project-1/compare/infer', (r) => json(r, 200, inferResponse))

  await page.goto('/projects/project-1/compare')
  // 填表名后推断
  await page.locator('input[placeholder="Table"]').first().fill('orders')
  await page.locator('input[placeholder="Table"]').nth(1).fill('orders')
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
  await page.locator('input[placeholder="Table"]').first().fill('orders')
  await page.locator('input[placeholder="Table"]').nth(1).fill('orders')
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
