import { expect, test, type Page, type Route } from '@playwright/test'
import { json, mockLicense, seedAdminAuth, trackConsoleErrors } from './helpers'

const now = '2026-08-23T06:00:00Z'
const sql = 'SELECT * FROM app.customer_accounts'

function datasource(id = 'ds-source', name = 'DM') {
  return {
    id,
    name,
    db_type: 'dm',
    host: 'db.local',
    port: 5236,
    environment: 'sandbox',
    environment_verified: false,
    database: 'app',
    operation_policy: {
      allow_select: true,
      allow_explain: false,
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
    datasource_id: 'ds-source',
    sql,
    pinned: false,
    created_at: now,
    updated_at: now,
    ...overrides,
  }
}

function compareTask() {
  return {
    id: 'task-1',
    project_id: 'project-1',
    name: 'customer compare',
    source_id: 'ds-source',
    target_id: 'ds-target',
    source_ref: { kind: 'table', schema_name: 'app', table_name: 'customers_source' },
    target_ref: { kind: 'table', schema_name: 'app', table_name: 'customers_target' },
    columns: [{ name: 'id', type: 'integer', driver_type: 'INT', nullable: false, primary_key: true }],
    compare_rules: {
      key_columns: ['id'],
      ignore_columns: [],
      column_mappings: {},
      numeric_tolerance: null,
      trim_strings: false,
      case_insensitive: false,
      empty_as_null: false,
      schema_policy: 'warn',
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
  }
}

async function mockWorkspaceApis(
  page: Page,
  compareTasks: unknown[] = [],
): Promise<Record<string, unknown>[]> {
  const createdConsoles: Record<string, unknown>[] = []
  await mockLicense(page)
  await page.route(/\/api\/datasources\?/, (route) =>
    json(route, 200, [datasource(), datasource('ds-target', 'DM target')]),
  )
  await page.route('**/api/sql/sessions/attach', (route) =>
    json(route, 409, {
      error: 'console_session_disabled',
      message: 'Console sessions are disabled on this deployment',
    }),
  )
  await page.route('**/api/sql/consoles', (route: Route) => {
    if (route.request().method() === 'GET') return json(route, 200, [consoleRow()])
    if (route.request().method() === 'POST') {
      const body = route.request().postDataJSON() as Record<string, unknown>
      createdConsoles.push(body)
      return json(route, 201, consoleRow({ id: 'console-from-compare', ...body }))
    }
    return route.fallback()
  })
  await page.route('**/api/sql/consoles/console-1', (route: Route) =>
    route.request().method() === 'PATCH'
      ? json(route, 200, consoleRow(route.request().postDataJSON()))
      : route.fallback(),
  )
  await page.route(/\/api\/sql\/history/, (route) => json(route, 200, []))
  await page.route(/\/api\/sql\/templates/, (route) => json(route, 200, []))
  await page.route(/\/api\/datasources\/[^/]+\/metadata\/schemas/, (route) =>
    json(route, 200, [{ name: 'app' }]),
  )
  await page.route(/\/api\/datasources\/[^/]+\/metadata\/tables/, (route) =>
    json(route, 200, []),
  )
  await page.route(/\/api\/projects\/project-1\/compare\/runs-dashboard/, (route) =>
    json(route, 200, {
      project_id: 'project-1',
      days: 30,
      total_runs: 0,
      status_counts: {},
      success_rate: 0,
      top_abort_reasons: [],
    }),
  )
  await page.route(/\/api\/compare\/tasks(\?|$)/, (route) => json(route, 200, compareTasks))
  return createdConsoles
}

test.beforeEach(async ({ page }) => {
  await seedAdminAuth(page)
})

test('SQL opens the current statement as a one-shot Compare source without SQL in the URL', async ({
  page,
}) => {
  const consoleErrors = trackConsoleErrors(page)
  await mockWorkspaceApis(page)

  await page.goto('/projects/project-1/sql')
  await page.getByTestId('sql-send-to-compare').click()

  await expect(page).toHaveURL(/\/projects\/project-1\/compare$/)
  await expect(page.getByRole('button', { name: 'Custom SQL' }).first()).toHaveClass(/chrome-accent/)
  await expect(page.locator('.monaco-editor .view-lines').first()).toContainText(
    'app.customer_accounts',
  )
  expect(page.url()).not.toContain('SELECT')
  expect(
    await page.evaluate(() =>
      Object.keys(sessionStorage).filter((key) => key.startsWith('dataops:workspace-handoff:')),
    ),
  ).toEqual([])
  expect(consoleErrors, consoleErrors.join('\n')).toEqual([])
})

test('Compare handoff creates a bound SQL console and consumes the token', async ({ page }) => {
  const consoleErrors = trackConsoleErrors(page)
  const createdConsoles = await mockWorkspaceApis(page)
  const token = '00000000-0000-4000-8000-000000000001'
  await page.addInitScript(
    ({ storageKey, payload }) => sessionStorage.setItem(storageKey, JSON.stringify(payload)),
    {
      storageKey: `dataops:workspace-handoff:${token}`,
      payload: {
        version: 1,
        kind: 'compare_to_sql',
        projectId: 'project-1',
        datasourceId: 'ds-source',
        sql,
        consoleName: 'compare_source_diff.sql',
        createdAt: Date.now(),
      },
    },
  )

  await page.goto(`/projects/project-1/sql?handoff=${token}`)

  await expect.poll(() => createdConsoles.length).toBe(1)
  expect(createdConsoles[0]).toMatchObject({
    name: 'compare_source_diff.sql',
    datasource_id: 'ds-source',
    sql,
  })
  await expect(page).toHaveURL(/\/projects\/project-1\/sql$/)
  await expect(page.locator('.monaco-editor .view-lines')).toContainText('app.customer_accounts')
  expect(consoleErrors, consoleErrors.join('\n')).toEqual([])
})

test('Compare diff SQL button opens a new SQL console with the source datasource', async ({
  page,
}) => {
  const consoleErrors = trackConsoleErrors(page)
  const createdConsoles = await mockWorkspaceApis(page, [compareTask()])
  await page.route('**/api/compare/tasks/task-1/run', (route) =>
    json(route, 202, { job_id: 'job-1', run_id: 'run-1' }),
  )
  await page.route('**/api/jobs/job-1', (route) =>
    json(route, 200, {
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
    bucket_counts: { only_source: 0, only_target: 0, diff: 1, same: 0 },
    progress: {},
    diff_profile: { generated: false, columns: {} },
    sample_result: null,
  }
  await page.route(/\/api\/compare\/runs\/run-1\/results/, (route) =>
    json(route, 200, {
      ...resultPayload,
      bucket: 'diff',
      offset: 0,
      limit: 100,
      rows: [
        {
          pk: { id: 7 },
          source: { id: 7 },
          target: { id: 7 },
          cells: [{ column: 'id', source: 7, target: 8 }],
        },
      ],
    }),
  )
  await page.route(/\/api\/compare\/runs\/run-1\/profile/, (route) =>
    json(route, 200, resultPayload),
  )
  const locateSql = 'SELECT * FROM app.customers_source WHERE id IN (7)'
  await page.route(/\/api\/compare\/runs\/run-1\/diff-sql/, (route) =>
    json(route, 200, {
      run_id: 'run-1',
      bucket: 'diff',
      key_columns: ['id'],
      pk_count: 1,
      truncated: false,
      cap: 500,
      source: { available: true, sql: locateSql, reason: null },
      target: { available: true, sql: 'SELECT 1', reason: null },
    }),
  )

  await page.goto('/projects/project-1/compare')
  await page.getByRole('button', { name: 'Start compare' }).click()
  await page.getByRole('button', { name: 'Locate rows SQL' }).click()
  await page.getByTestId('compare-open-source-sql').click()

  await expect(page).toHaveURL(/\/projects\/project-1\/sql$/)
  await expect.poll(() => createdConsoles.length).toBe(1)
  expect(createdConsoles[0]).toMatchObject({ datasource_id: 'ds-source', sql: locateSql })
  expect(consoleErrors, consoleErrors.join('\n')).toEqual([])
})

test('handoff rejects another project and still consumes the one-shot token', async ({ page }) => {
  const createdConsoles = await mockWorkspaceApis(page)
  const token = '00000000-0000-4000-8000-000000000002'
  await page.addInitScript(
    ({ storageKey, payload }) => sessionStorage.setItem(storageKey, JSON.stringify(payload)),
    {
      storageKey: `dataops:workspace-handoff:${token}`,
      payload: {
        version: 1,
        kind: 'compare_to_sql',
        projectId: 'another-project',
        datasourceId: 'ds-source',
        sql,
        consoleName: 'must_not_open.sql',
        createdAt: Date.now(),
      },
    },
  )

  await page.goto(`/projects/project-1/sql?handoff=${token}`)

  await expect(page.getByText('The workspace handoff expired or was already used.')).toBeVisible()
  expect(createdConsoles).toHaveLength(0)
  expect(
    await page.evaluate(() =>
      Object.keys(sessionStorage).filter((key) => key.startsWith('dataops:workspace-handoff:')),
    ),
  ).toEqual([])
})
