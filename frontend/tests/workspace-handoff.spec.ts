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

function resultInputDescriptor(
  partial: boolean,
  origin: { kind: 'statement' | 'job'; id: string } = { kind: 'job', id: 'job-1' },
) {
  return {
    id: 'input-1',
    project_id: 'project-1',
    origin_kind: origin.kind,
    origin_id: origin.id,
    columns: [
      { name: 'id', type: 'integer', driver_type: 'INT', nullable: false, primary_key: true },
      { name: 'name', type: 'string', driver_type: 'VARCHAR', nullable: true, primary_key: false },
    ],
    loaded_rows: 2,
    total_rows: partial ? null : 2,
    truncated: partial,
    has_more: partial,
    state: 'ready',
    created_at: now,
    expires_at: '2026-08-24T06:00:00Z',
  }
}

async function mockCompletedSqlResult(
  page: Page,
  runtimePartial = false,
  snapshotPartial = runtimePartial,
): Promise<Record<string, unknown>[]> {
  const captures: Record<string, unknown>[] = []
  await page.route('**/api/sql/execute', (route) =>
    json(route, 202, { job_id: 'job-1', result_set_id: 'rs-1' }),
  )
  await page.route(/\/api\/jobs\/job-1\/progress\?/, (route) =>
    json(route, 200, {
      job_id: 'job-1',
      result_set_id: 'rs-1',
      status: 'success',
      loaded_rows: 2,
      result_version: 1,
      columns_ready: true,
      first_batch_ready: true,
      terminal: true,
      error: null,
      error_code: null,
      retry_after_ms: 0,
      has_new_result: true,
      truncated: runtimePartial,
      has_more: runtimePartial,
      timings: null,
      execution: {
        queued_at: now,
        claimed_at: now,
        finished_at: now,
        max_rows: 1000,
        output_limit_applied: true,
        limit_pushdown: true,
        query_shape: 'simple_select',
      },
    }),
  )
  await page.route(/\/api\/jobs\/job-1\/result\?/, (route) =>
    json(route, 200, {
      job_id: 'job-1',
      result_set_id: 'rs-1',
      offset: 0,
      limit: 100,
      columns: resultInputDescriptor(runtimePartial).columns,
      rows: [{ values: [1, 'Ada'] }, { values: [2, 'Lin'] }],
      loaded_rows: 2,
      total_rows: runtimePartial ? null : 2,
      state: 'complete',
      truncated: runtimePartial,
      has_more: runtimePartial,
      page_size: 100,
      max_result_rows: 1000,
      preview_truncated_cells: 0,
      pagination_mode: 'unavailable',
      pagination_reason: 'top_level_order_by_required',
    }),
  )
  await page.route('**/api/jobs/job-1', (route) =>
    json(route, 200, {
      id: 'job-1',
      kind: 'sql_query',
      status: 'success',
      created_at: now,
      started_at: now,
      finished_at: now,
      error: null,
      error_code: null,
      message: null,
      result_set_id: 'rs-1',
    }),
  )
  await page.route('**/api/projects/project-1/compare/result-inputs', (route) => {
    const body = route.request().postDataJSON() as Record<string, unknown>
    captures.push(body)
    if (snapshotPartial && body.allow_partial !== true) {
      return json(route, 409, {
        error: 'result_partial_confirmation_required',
        message: 'Result is partial; confirm before comparing saved rows',
      })
    }
    return json(route, 201, resultInputDescriptor(snapshotPartial))
  })
  await page.route('**/api/projects/project-1/compare/result-inputs/input-1', (route) =>
    json(route, 200, resultInputDescriptor(snapshotPartial)),
  )
  return captures
}

async function mockCancelledStatementResult(page: Page): Promise<Record<string, unknown>[]> {
  const captures: Record<string, unknown>[] = []
  const origin = { kind: 'statement' as const, id: 'statement-1' }
  await page.route('**/api/sql/sessions/attach', (route) =>
    json(route, 200, {
      session_id: 'session-1',
      epoch: 1,
      current_epoch: 1,
      state: 'idle',
      db_type: 'dm',
      server_cancel: 'available',
      current_statement_id: null,
      idle_deadline: null,
      last_activity_at: now,
      close_reason: null,
      error_code: null,
    }),
  )
  await page.route('**/api/sql/sessions/session-1/statements', (route) =>
    json(route, 202, {
      statement_id: origin.id,
      result_set_id: 'rs-statement-1',
      seq: 1,
      deduplicated: false,
    }),
  )
  await page.route(/\/api\/sql\/statements\/statement-1\/progress\?/, (route) =>
    json(route, 200, {
      statement_id: origin.id,
      session: { session_id: 'session-1', state: 'idle', current_epoch: 1 },
      result_set_id: 'rs-statement-1',
      state: 'cancelled',
      loaded_rows: 2,
      result_version: 1,
      columns_ready: true,
      first_batch_ready: true,
      terminal: true,
      error: null,
      error_code: 'cancelled',
      retry_after_ms: 0,
      has_new_result: true,
      truncated: false,
      has_more: false,
      timings: null,
      execution: {
        queued_at: now,
        claimed_at: now,
        finished_at: now,
        max_rows: 1000,
        output_limit_applied: false,
        limit_pushdown: true,
        query_shape: 'simple_select',
      },
    }),
  )
  await page.route(/\/api\/sql\/statements\/statement-1\/result\?/, (route) =>
    json(route, 200, {
      statement_id: origin.id,
      statement_state: 'cancelled',
      result_set_id: 'rs-statement-1',
      offset: 0,
      limit: 100,
      columns: resultInputDescriptor(false, origin).columns,
      rows: [{ values: [1, 'Ada'] }, { values: [2, 'Lin'] }],
      loaded_rows: 2,
      total_rows: 2,
      state: 'complete',
      truncated: false,
      has_more: false,
      page_size: 100,
      max_result_rows: 1000,
      preview_truncated_cells: 0,
      pagination_mode: 'unavailable',
      pagination_reason: 'top_level_order_by_required',
    }),
  )
  await page.route('**/api/projects/project-1/compare/result-inputs', (route) => {
    captures.push(route.request().postDataJSON() as Record<string, unknown>)
    return json(route, 201, resultInputDescriptor(true, origin))
  })
  await page.route('**/api/projects/project-1/compare/result-inputs/input-1', (route) =>
    json(route, 200, resultInputDescriptor(true, origin)),
  )
  return captures
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

test('SQL freezes a completed result and opens it as a one-shot Compare snapshot source', async ({
  page,
}) => {
  const consoleErrors = trackConsoleErrors(page)
  await mockWorkspaceApis(page)
  const captures = await mockCompletedSqlResult(page)

  await page.goto('/projects/project-1/sql')
  await page.getByTestId('sql-execute').click()
  await expect(page.getByTestId('sql-result-to-compare')).toBeEnabled()
  await page.getByTestId('sql-result-to-compare').click()

  await expect(page).toHaveURL(/\/projects\/project-1\/compare$/)
  await expect(page.getByTestId('compare-source-result-snapshot')).toContainText('input-1')
  await expect(page.locator('input[value="id"]').first()).toHaveValue('id')
  expect(captures).toEqual([
    { origin_kind: 'job', origin_id: 'job-1', allow_partial: false },
  ])
  expect(page.url()).not.toContain('input-1')
  expect(
    await page.evaluate(() =>
      Object.keys(sessionStorage).filter((key) => key.startsWith('dataops:workspace-handoff:')),
    ),
  ).toEqual([])
  expect(consoleErrors, consoleErrors.join('\n')).toEqual([])
})

test('SQL requires explicit confirmation before sending a partial result to Compare', async ({ page }) => {
  const consoleErrors = trackConsoleErrors(page)
  await mockWorkspaceApis(page)
  const captures = await mockCompletedSqlResult(page, true)

  await page.goto('/projects/project-1/sql')
  await page.getByTestId('sql-execute').click()
  await expect(page.getByTestId('sql-result-to-compare')).toBeEnabled()

  page.once('dialog', async (dialog) => {
    expect(dialog.type()).toBe('confirm')
    expect(dialog.message()).toContain('partial or truncated result')
    await dialog.dismiss()
  })
  await page.getByTestId('sql-result-to-compare').click()

  await expect(page).toHaveURL(/\/projects\/project-1\/sql$/)
  expect(captures).toEqual([])

  page.once('dialog', async (dialog) => dialog.accept())
  await page.getByTestId('sql-result-to-compare').click()

  await expect(page).toHaveURL(/\/projects\/project-1\/compare$/)
  await expect(page.getByTestId('compare-source-result-snapshot')).toContainText('input-1')
  expect(captures).toEqual([
    { origin_kind: 'job', origin_id: 'job-1', allow_partial: true },
  ])
  expect(consoleErrors, consoleErrors.join('\n')).toEqual([])
})

test('SQL retries capture only after confirming a server-detected partial result', async ({ page }) => {
  const consoleErrors = trackConsoleErrors(page)
  await mockWorkspaceApis(page)
  const captures = await mockCompletedSqlResult(page, false, true)

  await page.goto('/projects/project-1/sql')
  await page.getByTestId('sql-execute').click()
  await expect(page.getByTestId('sql-result-to-compare')).toBeEnabled()

  page.once('dialog', async (dialog) => dialog.accept())
  await page.getByTestId('sql-result-to-compare').click()

  await expect(page).toHaveURL(/\/projects\/project-1\/compare$/)
  await expect(page.getByTestId('compare-source-result-snapshot')).toContainText('input-1')
  expect(captures).toEqual([
    { origin_kind: 'job', origin_id: 'job-1', allow_partial: false },
    { origin_kind: 'job', origin_id: 'job-1', allow_partial: true },
  ])
  expect(consoleErrors, consoleErrors.join('\n')).toEqual([])
})

test('cancelled Session Broker results preserve explicit partial confirmation in the handoff', async ({
  page,
}) => {
  const consoleErrors = trackConsoleErrors(page)
  await mockWorkspaceApis(page)
  const captures = await mockCancelledStatementResult(page)
  await page.addInitScript(() => {
    const originalSetItem = Storage.prototype.setItem
    Storage.prototype.setItem = function (key: string, value: string): void {
      if (
        this === globalThis.sessionStorage &&
        key.startsWith('dataops:workspace-handoff:') &&
        value.includes('"result_to_compare"')
      ) {
        originalSetItem.call(this, 'test:last-result-handoff', value)
      }
      originalSetItem.call(this, key, value)
    }
  })

  await page.goto('/projects/project-1/sql')
  await page.getByTestId('sql-execute').click()
  await expect(page.getByTestId('sql-result-to-compare')).toBeEnabled()

  page.once('dialog', async (dialog) => dialog.accept())
  await page.getByTestId('sql-result-to-compare').click()

  await expect(page).toHaveURL(/\/projects\/project-1\/compare$/)
  expect(captures).toEqual([
    { origin_kind: 'statement', origin_id: 'statement-1', allow_partial: true },
  ])
  expect(
    await page.evaluate(() => {
      const value = sessionStorage.getItem('test:last-result-handoff')
      return value ? JSON.parse(value).allowPartial : null
    }),
  ).toBe(true)
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

test('result handoff rejects another project without reading the snapshot', async ({ page }) => {
  await mockWorkspaceApis(page)
  const token = '00000000-0000-4000-8000-000000000003'
  let snapshotReads = 0
  await page.route('**/api/projects/project-1/compare/result-inputs/input-1', (route) => {
    snapshotReads += 1
    return json(route, 200, resultInputDescriptor(false))
  })
  await page.addInitScript(
    ({ storageKey, payload }) => sessionStorage.setItem(storageKey, JSON.stringify(payload)),
    {
      storageKey: `dataops:workspace-handoff:${token}`,
      payload: {
        version: 1,
        kind: 'result_to_compare',
        projectId: 'another-project',
        inputId: 'input-1',
        allowPartial: false,
        createdAt: Date.now(),
      },
    },
  )

  await page.goto(`/projects/project-1/compare?handoff=${token}`)

  await expect(page.getByText('The workspace handoff expired or was already used.')).toBeVisible()
  expect(snapshotReads).toBe(0)
  expect(
    await page.evaluate(() =>
      Object.keys(sessionStorage).filter((key) => key.startsWith('dataops:workspace-handoff:')),
    ),
  ).toEqual([])
})

test('result handoff surfaces an expired snapshot and still consumes the token', async ({ page }) => {
  await mockWorkspaceApis(page)
  const token = '00000000-0000-4000-8000-000000000004'
  await page.route('**/api/projects/project-1/compare/result-inputs/input-1', (route) =>
    json(route, 410, {
      error: 'compare_input_expired',
      message: 'Result input is no longer available',
    }),
  )
  await page.addInitScript(
    ({ storageKey, payload }) => sessionStorage.setItem(storageKey, JSON.stringify(payload)),
    {
      storageKey: `dataops:workspace-handoff:${token}`,
      payload: {
        version: 1,
        kind: 'result_to_compare',
        projectId: 'project-1',
        inputId: 'input-1',
        allowPartial: false,
        createdAt: Date.now(),
      },
    },
  )

  await page.goto(`/projects/project-1/compare?handoff=${token}`)

  await expect(page.getByText('Result input is no longer available')).toBeVisible()
  await expect(page).toHaveURL(/\/projects\/project-1\/compare$/)
  expect(
    await page.evaluate(() =>
      Object.keys(sessionStorage).filter((key) => key.startsWith('dataops:workspace-handoff:')),
    ),
  ).toEqual([])
})

test('result handoff rejects duplicate snapshot columns with an actionable error', async ({ page }) => {
  await mockWorkspaceApis(page)
  const token = '00000000-0000-4000-8000-000000000005'
  const descriptor = resultInputDescriptor(false)
  descriptor.columns[1] = { ...descriptor.columns[1], name: 'id' }
  await page.route('**/api/projects/project-1/compare/result-inputs/input-1', (route) =>
    json(route, 200, descriptor),
  )
  await page.addInitScript(
    ({ storageKey, payload }) => sessionStorage.setItem(storageKey, JSON.stringify(payload)),
    {
      storageKey: `dataops:workspace-handoff:${token}`,
      payload: {
        version: 1,
        kind: 'result_to_compare',
        projectId: 'project-1',
        inputId: 'input-1',
        allowPartial: false,
        createdAt: Date.now(),
      },
    },
  )

  await page.goto(`/projects/project-1/compare?handoff=${token}`)

  await expect(
    page.getByText('This result contains duplicate column names. Add unique SQL aliases and run it again.'),
  ).toBeVisible()
  await expect(page.getByTestId('compare-source-result-snapshot')).toHaveCount(0)
})
