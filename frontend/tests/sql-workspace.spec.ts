import { test, expect, type Page, type Route } from '@playwright/test'
import { deferred, json, mockLicense, seedAdminAuth, trackConsoleErrors } from './helpers'

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
    hasMore?: boolean
    progressiveColumns?: number
    progressivePageSize?: number
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
  const jobStartedAt = jobCreatedAt
  const jobFinishedAt = options.jobFinishedAt ?? now

  await mockLicense(page)
  await page.route('**/api/version', (r) =>
    json(r, 200, { version: '2.0.1-test', commit: 'abcdef0123456789', image_version: 'test' }),
  )
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
      const columnCount = options.progressiveColumns ?? 1
      const pageSize = options.progressivePageSize ?? 100
      return json(r, 200, {
        job_id: 'job-1',
        result_set_id: 'rs-1',
        offset: 0,
        limit: pageSize,
        columns: Array.from({ length: columnCount }, (_, columnIndex) => ({
          name: columnIndex === 0 ? 'id' : `value_${columnIndex}`,
          type: columnIndex === 0 ? 'integer' : 'string',
          driver_type: columnIndex === 0 ? 'INT' : 'VARCHAR',
          nullable: columnIndex !== 0,
          primary_key: columnIndex === 0,
        })),
        // This helper normally returns one bounded page. A larger page size
        // is opt-in for the synthetic virtualization stress test below.
        rows: Array.from({ length: Math.min(rowCount, pageSize) }, (_, index) => ({
          values: Array.from({ length: columnCount }, (_, columnIndex) =>
            columnIndex === 0 ? index + 1 : `value-${index + 1}-${columnIndex}`,
          ),
        })),
        loaded_rows: rowCount,
        total_rows: null,
        state: jobReads <= 1 ? 'running' : 'success',
        truncated: false,
        has_more: options.hasMore ?? false,
        page_size: pageSize,
        max_result_rows: 1000,
        pagination_mode: options.hasMore ? 'ordered_offset' : 'unavailable',
        pagination_reason: options.hasMore
          ? 'fresh_read_ordered_offset'
          : 'top_level_order_by_required',
        preview_truncated_cells: 0,
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
  await page.route(/\/api\/jobs\/job-1\/progress\?/, (r) => {
    jobReads += 1
    const running = jobReads <= 1
    const loadedRows = options.progressiveRows ?? 2
    return json(r, 200, {
      job_id: 'job-1',
      result_set_id: 'rs-1',
      status: running ? 'running' : 'success',
      loaded_rows: loadedRows,
      result_version: 1,
      columns_ready: true,
      first_batch_ready: true,
      terminal: !running,
      error: null,
      error_code: null,
      retry_after_ms: running ? 1000 : 0,
      has_new_result: running,
      truncated: false,
      has_more: options.hasMore ?? false,
      pagination_mode: options.hasMore ? 'ordered_offset' : 'unavailable',
      pagination_reason: options.hasMore
        ? 'fresh_read_ordered_offset'
        : 'top_level_order_by_required',
      timings: null,
      execution: {
        queued_at: jobCreatedAt,
        claimed_at: jobStartedAt,
        finished_at: running ? null : jobFinishedAt,
        max_rows: 1000,
        output_limit_applied: true,
        limit_pushdown: true,
        query_shape: 'simple_select',
      },
    })
  })
  await page.route('**/api/jobs/job-1', (r) => {
    return json(r, 200, {
      id: 'job-1',
      kind: 'sql',
      status: jobReads <= 1 ? 'running' : 'success',
      created_at: jobCreatedAt,
      started_at: jobStartedAt,
      finished_at: jobReads <= 1 ? null : jobFinishedAt,
      error: null,
      error_code: null,
      message: null,
      result_set_id: 'rs-1',
    })
  })

  return { patches, renders, executeRequests, resultRequestUrls, getJobReads: () => jobReads }
}

interface PerfBaselineMetrics {
  firstScreenMs: number
  scrollDurationMs: number
  frames: number
  longTasks: number
  initialRows: number
}

async function startPerfBaseline(page: Page): Promise<void> {
  await page.evaluate(() => {
    const baseline = { startedAt: performance.now(), longTasks: 0 }
    ;(window as typeof window & { __sqlPerfBaseline?: typeof baseline }).__sqlPerfBaseline = baseline
    if (typeof PerformanceObserver === 'undefined') return
    const observer = new PerformanceObserver((list) => {
      baseline.longTasks += list.getEntries().length
    })
    observer.observe({ type: 'longtask', buffered: true })
  })
}

async function measurePerfBaseline(page: Page): Promise<PerfBaselineMetrics> {
  await expect(page.locator('tr[data-row-index="0"]')).toBeVisible()
  const firstScreenMs = await page.evaluate(() => {
    const baseline = (window as typeof window & {
      __sqlPerfBaseline?: { startedAt: number }
    }).__sqlPerfBaseline
    return baseline ? performance.now() - baseline.startedAt : -1
  })
  const initialRows = await page.locator('table.text-data tbody tr[data-row-index]').count()
  const scrollMetrics = await page.getByTestId('result-table-scroll').evaluate(async (element) => {
    const start = performance.now()
    let frames = 0
    const maxScrollTop = Math.max(0, element.scrollHeight - element.clientHeight)
    await new Promise<void>((resolve) => {
      const tick = (timestamp: number) => {
        frames += 1
        const progress = Math.min(1, (timestamp - start) / 1000)
        element.scrollTop = maxScrollTop * progress
        if (progress >= 1) resolve()
        else requestAnimationFrame(tick)
      }
      requestAnimationFrame(tick)
    })
    return { durationMs: performance.now() - start, frames }
  })
  const longTasks = await page.evaluate(
    () =>
      (window as typeof window & { __sqlPerfBaseline?: { longTasks: number } }).__sqlPerfBaseline
        ?.longTasks ?? 0,
  )
  return {
    firstScreenMs,
    scrollDurationMs: scrollMetrics.durationMs,
    frames: scrollMetrics.frames,
    longTasks,
    initialRows,
  }
}

function logPerfBaseline(label: string, metrics: PerfBaselineMetrics): void {
  console.log(
    `[perf-baseline] ${label} first-screen=${metrics.firstScreenMs.toFixed(1)}ms ` +
      `scroll=${metrics.scrollDurationMs.toFixed(1)}ms frames=${metrics.frames} ` +
      `longtasks=${metrics.longTasks} initialRows=${metrics.initialRows}`,
  )
}

let consoleErrors: string[] = []
test.beforeEach(async ({ page }) => {
  consoleErrors = trackConsoleErrors(page)
  await seedAdminAuth(page)
})

function expectNoConsoleErrors(): void {
  expect(consoleErrors, consoleErrors.join('\n')).toEqual([])
}

test('SQL execution sends separate default page size and safety limit', async ({ page }) => {
  const state = await mockWorkspace(page)

  await page.goto('/projects/project-1/sql')
  await expect(page.getByLabel('Page size')).toHaveValue('100')
  await expect(page.getByLabel('Safety limit')).toHaveValue('1000')
  await page.getByRole('button', { name: 'Run' }).click()

  await expect.poll(() => state.executeRequests.length).toBe(1)
  await expect.poll(() => state.getJobReads()).toBeGreaterThanOrEqual(2)
  expect(state.executeRequests[0]).toMatchObject({ page_size: 100, max_result_rows: 1000 })
  expectNoConsoleErrors()
})

test('SQL execution accepts a custom maximum row limit', async ({ page }) => {
  const state = await mockWorkspace(page)

  await page.goto('/projects/project-1/sql')
  await page.getByLabel('Safety limit').selectOption('custom')
  await page.getByLabel('Custom maximum rows').fill('2500')
  await page.getByRole('button', { name: 'Run' }).click()

  await expect.poll(() => state.executeRequests.length).toBe(1)
  await expect.poll(() => state.getJobReads()).toBeGreaterThanOrEqual(2)
  expect(state.executeRequests[0]).toMatchObject({ page_size: 100, max_result_rows: 2500 })
  expectNoConsoleErrors()
})

test('next page enqueues a database continuation while previous page stays cached', async ({
  page,
}) => {
  const state = await mockWorkspace(page, { progressiveRows: 100, hasMore: true })
  const pageRequests: Record<string, unknown>[] = []
  await page.route('**/api/jobs/job-1/pages', (r) => {
    pageRequests.push(r.request().postDataJSON())
    return json(r, 202, {
      job_id: 'job-2',
      result_set_id: 'rs-1',
      offset: 100,
      cached: false,
    })
  })
  await page.route('**/api/jobs/job-2', (r) =>
    json(r, 200, {
      id: 'job-2',
      kind: 'sql_query',
      status: 'success',
      created_at: now,
      started_at: now,
      finished_at: now,
      error: null,
      error_code: null,
      message: null,
      result_set_id: 'rs-1',
      timings: {
        queue_ms: 5,
        connect_ms: 10,
        execute_first_row_ms: 20,
        fetch_ms: 30,
        spool_ms: 4,
        total_ms: 69,
      },
    }),
  )
  await page.route(/\/api\/jobs\/job-2\/progress\?/, (r) =>
    json(r, 200, {
      job_id: 'job-2', result_set_id: 'rs-1', status: 'success', loaded_rows: 200,
      result_version: 2, columns_ready: true, first_batch_ready: true, terminal: true,
      error: null, error_code: null, retry_after_ms: 0, has_new_result: true,
      truncated: false, has_more: true, pagination_mode: 'ordered_offset',
      pagination_reason: 'fresh_read_ordered_offset',
      timings: { queue_ms: 5, connect_ms: 10, execute_first_row_ms: 20, fetch_ms: 30, spool_ms: 4, total_ms: 69 },
      execution: null,
    }),
  )
  const newPageResultStarted = deferred()
  const releaseNewPageResult = deferred()
  let newPageResultFinished = false
  await page.route(/\/api\/jobs\/job-2\/result\?/, async (r) => {
    const offset = Number(new URL(r.request().url()).searchParams.get('offset') ?? 0)
    if (offset === 100) {
      newPageResultStarted.resolve()
      await releaseNewPageResult.promise
    }
    await json(r, 200, {
      job_id: 'job-2',
      result_set_id: 'rs-1',
      offset,
      limit: 100,
      columns: [
        { name: 'id', type: 'integer', driver_type: 'INT', nullable: false, primary_key: true },
      ],
      rows: Array.from({ length: 100 }, (_, index) => ({ values: [index + offset + 1] })),
      loaded_rows: 200,
      total_rows: null,
      state: 'complete',
      truncated: false,
      has_more: true,
      page_size: 100,
      max_result_rows: 1000,
      pagination_mode: 'ordered_offset',
      pagination_reason: 'fresh_read_ordered_offset',
      preview_truncated_cells: 0,
    })
    if (offset === 100) newPageResultFinished = true
  })

  await page.goto('/projects/project-1/sql')
  await page.getByRole('button', { name: 'Run' }).click()
  await expect.poll(() => state.getJobReads()).toBeGreaterThanOrEqual(2)
  await expect(page.getByText('1-100 of 100+')).toBeVisible()
  await page.getByRole('button', { name: 'Next page' }).click()

  await expect.poll(() => pageRequests).toEqual([{ offset: 100 }])
  await newPageResultStarted.promise
  await expect(page.getByRole('button', { name: 'Next page' })).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Previous page' })).toBeDisabled()
  releaseNewPageResult.resolve()
  await expect.poll(() => newPageResultFinished).toBe(true)
  await expect(page.getByRole('cell', { name: '101', exact: true }).last()).toBeVisible()
  await expect(page.getByRole('button', { name: 'Previous page' })).toBeEnabled()
  await page.getByRole('button', { name: 'Stats' }).click()
  await expect(page.getByRole('cell', { name: '5ms', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Result', exact: true }).click()
  await page.getByRole('button', { name: 'Previous page' }).click()
  await expect(page.getByRole('cell', { name: '1', exact: true }).last()).toBeVisible()
  expectNoConsoleErrors()
})

test('late continuation response cannot overwrite a runtime after console switch', async ({
  page,
}) => {
  await mockWorkspace(page, { progressiveRows: 50, hasMore: true })
  const continuationStarted = deferred()
  const releaseContinuation = deferred()
  let continuationFinished = false
  await page.route('**/api/jobs/job-1/pages', async (r) => {
    continuationStarted.resolve()
    await releaseContinuation.promise
    await json(r, 202, {
      job_id: 'job-2',
      result_set_id: 'rs-1',
      offset: 100,
      cached: false,
    })
    continuationFinished = true
  })

  await page.goto('/projects/project-1/sql')
  await page.getByRole('button', { name: 'Run' }).click()
  await expect(page.getByText('1-50 of 50+')).toBeVisible()
  await page.getByRole('button', { name: 'Next page' }).click()
  await continuationStarted.promise

  await page.getByTitle('New console').click()
  await expect(page.locator('aside').getByText('query_2.sql')).toBeVisible()
  releaseContinuation.resolve()
  await expect.poll(() => continuationFinished).toBe(true)

  await page.locator('aside').getByText('query_1.sql').click()
  await expect(page.getByText('1-50 of 50+')).toBeVisible()
  await expect(page.getByRole('cell', { name: '1', exact: true }).last()).toBeVisible()
  expectNoConsoleErrors()
})

test('late previous-page result cannot leave navigation loading after console switch', async ({
  page,
}) => {
  await mockWorkspace(page, { progressiveRows: 100, hasMore: true })
  await page.route('**/api/jobs/job-1/pages', (r) =>
    json(r, 202, { job_id: 'job-2', result_set_id: 'rs-1', offset: 100, cached: false }),
  )
  await page.route(/\/api\/jobs\/job-2\/progress\?/, (r) =>
    json(r, 200, {
      job_id: 'job-2', result_set_id: 'rs-1', status: 'success', loaded_rows: 200,
      result_version: 2, columns_ready: true, first_batch_ready: true, terminal: true,
      error: null, error_code: null, retry_after_ms: 0, has_new_result: true,
      truncated: false, has_more: true, pagination_mode: 'ordered_offset',
      pagination_reason: 'fresh_read_ordered_offset', timings: null, execution: null,
    }),
  )
  const previousFetchStarted = deferred()
  const releasePreviousFetch = deferred()
  let previousFetchCount = 0
  let previousFetchFinished = false
  await page.route(/\/api\/jobs\/job-2\/result\?/, async (r) => {
    const offset = Number(new URL(r.request().url()).searchParams.get('offset') ?? 0)
    if (offset === 0 && previousFetchCount === 0) {
      previousFetchCount += 1
      previousFetchStarted.resolve()
      await releasePreviousFetch.promise
      await json(r, 200, {
        job_id: 'job-2', result_set_id: 'rs-1', offset: 0, limit: 100,
        columns: [{ name: 'id', type: 'integer', driver_type: 'INT', nullable: false, primary_key: true }],
        rows: Array.from({ length: 100 }, (_, index) => ({ values: [index + 1] })),
        loaded_rows: 200, total_rows: null, state: 'complete', truncated: false,
        has_more: true, page_size: 100, max_result_rows: 1000,
        pagination_mode: 'ordered_offset', pagination_reason: 'fresh_read_ordered_offset',
        preview_truncated_cells: 0,
      })
      previousFetchFinished = true
      return
    }
    return json(r, 200, {
      job_id: 'job-2', result_set_id: 'rs-1', offset, limit: 100,
      columns: [{ name: 'id', type: 'integer', driver_type: 'INT', nullable: false, primary_key: true }],
      rows: Array.from({ length: 100 }, (_, index) => ({ values: [index + offset + 1] })),
      loaded_rows: 200, total_rows: null, state: 'complete', truncated: false,
      has_more: true, page_size: 100, max_result_rows: 1000,
      pagination_mode: 'ordered_offset', pagination_reason: 'fresh_read_ordered_offset',
      preview_truncated_cells: 0,
    })
  })

  await page.goto('/projects/project-1/sql')
  await page.getByRole('button', { name: 'Run' }).click()
  await expect(page.getByText('1-100 of 100+')).toBeVisible()
  await page.getByRole('button', { name: 'Next page' }).click()
  await expect(page.getByRole('cell', { name: '101', exact: true }).last()).toBeVisible()
  await page.getByRole('button', { name: 'Previous page' }).click()
  await previousFetchStarted.promise

  await page.getByTitle('New console').click()
  await expect(page.locator('aside').getByText('query_2.sql')).toBeVisible()
  releasePreviousFetch.resolve()
  await expect.poll(() => previousFetchFinished).toBe(true)

  await page.locator('aside').getByText('query_1.sql').click()
  await expect(page.getByRole('button', { name: 'Previous page' })).toBeEnabled()
  await page.getByRole('button', { name: 'Previous page' }).click()
  await expect(page.getByRole('cell', { name: '1', exact: true }).last()).toBeVisible()
  expectNoConsoleErrors()
})

test('terminal failure without a result clears result loading state', async ({ page }) => {
  await mockWorkspace(page)
  let progressReads = 0
  await page.route(/\/api\/jobs\/job-1\/progress\?/, (r) => {
    progressReads += 1
    return json(r, 200, {
      job_id: 'job-1', result_set_id: 'rs-1', status: 'failed', loaded_rows: 0,
      result_version: 0, columns_ready: false, first_batch_ready: false, terminal: true,
      error: 'query failed', error_code: 'sql_failed', retry_after_ms: 0, has_new_result: false,
      truncated: false, has_more: false, pagination_mode: null, pagination_reason: null,
      timings: null, execution: null,
    })
  })

  await page.goto('/projects/project-1/sql')
  await page.getByRole('button', { name: 'Run' }).click()
  await expect.poll(() => progressReads).toBeGreaterThanOrEqual(1)
  await expect(page.getByRole('button', { name: 'Cancel' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Run' })).toBeEnabled()
  expectNoConsoleErrors()
})

test('deleting a console cancels its active query before closing it', async ({ page }) => {
  await mockWorkspace(page)
  let cancelRequests = 0
  await page.route(/\/api\/jobs\/job-1\/progress\?/, (r) =>
    json(r, 200, {
      job_id: 'job-1', result_set_id: 'rs-1', status: 'running', loaded_rows: 0,
      result_version: 0, columns_ready: false, first_batch_ready: false, terminal: false,
      error: null, error_code: null, retry_after_ms: 1000, has_new_result: false,
      truncated: false, has_more: false, timings: null, execution: null,
    }),
  )
  await page.route('**/api/jobs/job-1', (r) =>
    json(r, 200, {
      id: 'job-1',
      kind: 'sql_query',
      status: 'running',
      created_at: now,
      started_at: now,
      finished_at: null,
      error: null,
      error_code: null,
      message: null,
      result_set_id: 'rs-1',
    }),
  )
  await page.route('**/api/jobs/job-1/cancel', (r) => {
    cancelRequests += 1
    return json(r, 200, { cancelled: true })
  })

  await page.goto('/projects/project-1/sql')
  await page.getByRole('button', { name: 'Run' }).click()
  await expect(page.getByRole('button', { name: 'Cancel' })).toBeVisible()
  const consoleItem = page.locator('.group', { hasText: 'query_1.sql' })
  await consoleItem.hover()
  await consoleItem.getByTitle('Delete').click()

  await expect.poll(() => cancelRequests).toBe(1)
  await expect(page.getByText('query_1.sql')).toHaveCount(0)
  expectNoConsoleErrors()
})

test('SQL workspace tabs, history, templates, and progressive result render', async ({ page }) => {
  const state = await mockWorkspace(page)

  await page.goto('/projects/project-1/sql')
  await expect(page.getByText('SQL workspace')).toBeVisible()
  await expect(page.getByTestId('build-version')).toHaveText('v2.0.1-test · abcdef0')
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
  await page.getByLabel('limit', { exact: true }).fill('10')
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
  const renderedRows = page.locator('table.text-data tbody tr[data-row-index]')
  await expect.poll(() => renderedRows.count()).toBeLessThan(100)
  const resultScroll = page.getByTestId('result-table-scroll')
  await resultScroll.evaluate((element) => {
    element.scrollTop = element.scrollHeight
    element.dispatchEvent(new Event('scroll'))
  })
  await expect(page.getByRole('cell', { name: '100', exact: true }).last()).toBeVisible()

  await expect.poll(() => state.getJobReads()).toBe(2)
  const terminalJobReads = state.getJobReads()
  await page.waitForTimeout(1_200)
  expect(state.getJobReads()).toBe(terminalJobReads)
  expectNoConsoleErrors()
})

test('large result windows render a bounded row slice and reveal the tail on scroll', async ({
  page,
}) => {
  // This deliberately requests a 1000-row mock page to stress the component;
  // the production UI exposes page sizes only up to 500.
  await mockWorkspace(page, { progressiveRows: 1000, progressivePageSize: 1000 })

  await page.goto('/projects/project-1/sql')
  await page.getByRole('button', { name: 'Run' }).click()
  await expect(page.getByText(/loaded 1000 rows/)).toBeVisible()

  const renderedRows = page.locator('table.text-data tbody tr[data-row-index]')
  await expect.poll(() => renderedRows.count()).toBeLessThan(100)
  await expect(page.locator('tr[data-row-index="0"]')).toBeVisible()
  await expect(page.locator('tr[data-row-index="999"]')).toHaveCount(0)

  await page.getByTestId('result-table-scroll').evaluate((element) => {
    element.scrollTop = element.scrollHeight
    element.dispatchEvent(new Event('scroll'))
  })
  await expect(page.locator('tr[data-row-index="999"]')).toBeVisible()
  await expect(page.locator('tr[data-row-index="0"]')).toHaveCount(0)
  expectNoConsoleErrors()
})

test('synthetic ResultTable stress baseline (1000 rows x 20 columns)', async ({ page }) => {
  // Synthetic component stress: 1000 rows exceed the production page-size
  // maximum of 500 and must not be read as a real API protocol baseline.
  await mockWorkspace(page, {
    progressiveRows: 1000,
    progressiveColumns: 20,
    progressivePageSize: 1000,
  })

  await page.goto('/projects/project-1/sql')
  await startPerfBaseline(page)
  await page.getByRole('button', { name: 'Run' }).click()
  const metrics = await measurePerfBaseline(page)
  logPerfBaseline('synthetic 1000x20', metrics)
  expect(metrics.initialRows).toBeGreaterThan(0)
  expect(metrics.initialRows).toBeLessThan(100)
  await expect(page.locator('tr[data-row-index="999"]')).toBeVisible()
  expectNoConsoleErrors()
})

test('production-shaped ResultTable baseline (500 rows x 20 columns)', async ({ page }) => {
  await mockWorkspace(page, {
    progressiveRows: 500,
    progressiveColumns: 20,
    progressivePageSize: 500,
  })

  await page.goto('/projects/project-1/sql')
  await page.getByLabel('Page size').selectOption('500')
  await startPerfBaseline(page)
  await page.getByRole('button', { name: 'Run' }).click()
  const metrics = await measurePerfBaseline(page)
  logPerfBaseline('production-shaped 500x20', metrics)
  expect(metrics.initialRows).toBeGreaterThan(0)
  expect(metrics.initialRows).toBeLessThan(100)
  await expect(page.locator('tr[data-row-index="499"]')).toBeVisible()
  expectNoConsoleErrors()
})

test('long multiline cells keep fixed row height and preserve virtual tail access', async ({
  page,
}) => {
  await mockWorkspace(page, { progressiveRows: 100, progressivePageSize: 100 })
  await page.route(/\/api\/jobs\/job-1\/result\?/, (r) =>
    json(r, 200, {
      job_id: 'job-1',
      result_set_id: 'rs-1',
      offset: 0,
      limit: 100,
      columns: [
        { name: 'payload', type: 'string', driver_type: 'TEXT', nullable: true, primary_key: false },
      ],
      rows: Array.from({ length: 100 }, (_, index) => ({
        values: [`row-${index + 1}\n${'x'.repeat(500)}`],
      })),
      loaded_rows: 100,
      total_rows: null,
      state: 'complete',
      truncated: false,
      has_more: false,
      page_size: 100,
      max_result_rows: 1000,
      pagination_mode: 'unavailable',
      pagination_reason: 'top_level_order_by_required',
      preview_truncated_cells: 0,
    }),
  )

  await page.goto('/projects/project-1/sql')
  await page.getByRole('button', { name: 'Run' }).click()
  await expect(page.locator('tr[data-row-index="0"]')).toBeVisible()
  const tableSemantics = await page.locator('table.text-data').evaluate((table) => ({
    rowCount: table.getAttribute('aria-rowcount'),
    colCount: table.getAttribute('aria-colcount'),
    headerIndex: table.querySelector('thead tr')?.getAttribute('aria-rowindex'),
    firstDataIndex: table.querySelector('tbody tr[data-row-index]')?.getAttribute('aria-rowindex'),
  }))
  expect(tableSemantics).toEqual({
    rowCount: '101',
    colCount: '2',
    headerIndex: '1',
    firstDataIndex: '2',
  })

  const heights = await page.locator('tr[data-row-index]').evaluateAll((rows) =>
    rows.map((row) => Math.round(row.getBoundingClientRect().height)),
  )
  expect(heights.length).toBeGreaterThan(0)
  expect(new Set(heights).size).toBe(1)

  await page.getByTestId('result-table-scroll').evaluate((element) => {
    element.scrollTop = element.scrollHeight
    element.dispatchEvent(new Event('scroll'))
  })
  await expect(page.locator('tr[data-row-index="99"]')).toBeVisible()
  expectNoConsoleErrors()
})

test('virtual window follows a result container resize and disconnects on unmount', async ({ page }) => {
  await mockWorkspace(page, { progressiveRows: 500, progressivePageSize: 500 })

  await page.goto('/projects/project-1/sql')
  await page.getByLabel('Page size').selectOption('500')
  await page.getByRole('button', { name: 'Run' }).click()
  await expect(page.locator('tr[data-row-index="0"]')).toBeVisible()
  const renderedRows = page.locator('table.text-data tbody tr[data-row-index]')
  const initialCount = await renderedRows.count()
  await page.getByTestId('result-table-scroll').evaluate((element) => {
    element.style.flex = '0 0 auto'
    element.style.height = '480px'
  })
  await expect.poll(() => renderedRows.count()).toBeGreaterThan(initialCount)

  await page.goto('/projects/project-1/compare')
  await expect(page).toHaveURL(/\/projects\/project-1\/compare$/)
  expectNoConsoleErrors()
})

test('result table ref lifecycle recovers from empty to nonempty on the same page', async ({
  page,
}) => {
  await mockWorkspace(page)
  let progressReads = 0
  let resultReads = 0
  await page.route(/\/api\/jobs\/job-1\/progress\?/, (r) => {
    progressReads += 1
    const terminal = progressReads >= 2
    return json(r, 200, {
      job_id: 'job-1', result_set_id: 'rs-1', status: terminal ? 'success' : 'running',
      loaded_rows: terminal ? 1 : 0, result_version: terminal ? 2 : 1,
      columns_ready: true, first_batch_ready: true, terminal,
      error: null, error_code: null, retry_after_ms: terminal ? 0 : 1000,
      has_new_result: true, truncated: false, has_more: false,
      pagination_mode: 'unavailable', pagination_reason: 'top_level_order_by_required',
      timings: null, execution: null,
    })
  })
  await page.route(/\/api\/jobs\/job-1\/result\?/, (r) => {
    resultReads += 1
    return json(r, 200, {
      job_id: 'job-1', result_set_id: 'rs-1', offset: 0, limit: 100,
      columns: resultReads === 1
        ? []
        : [{ name: 'id', type: 'integer', driver_type: 'INT', nullable: false, primary_key: true }],
      rows: resultReads === 1 ? [] : [{ values: [1] }],
      loaded_rows: resultReads === 1 ? 0 : 1, total_rows: null,
      state: resultReads === 1 ? 'streaming' : 'complete', truncated: false,
      has_more: false, page_size: 100, max_result_rows: 1000,
      pagination_mode: 'unavailable', pagination_reason: 'top_level_order_by_required',
      preview_truncated_cells: 0,
    })
  })

  await page.goto('/projects/project-1/sql')
  await page.getByRole('button', { name: 'Run' }).click()
  await expect.poll(() => resultReads).toBe(2)
  await expect(page.getByTestId('result-table-scroll')).toBeVisible()
  await expect(page.getByRole('cell', { name: '1', exact: true }).last()).toBeVisible()
  expectNoConsoleErrors()
})

test('append-only progress does not refetch an already complete current page', async ({
  page,
}) => {
  const state = await mockWorkspace(page, { progressiveRows: 100, hasMore: true })
  let progressReads = 0
  const observedProgress: Array<{ afterVersion: string | null; loadedRows: number; version: number }> = []
  await page.route(/\/api\/jobs\/job-1\/progress\?/, (r) => {
    progressReads += 1
    const version = progressReads === 1 ? 1 : 2
    const loadedRows = progressReads === 1 ? 100 : 200
    observedProgress.push({
      afterVersion: new URL(r.request().url()).searchParams.get('after_version'),
      loadedRows,
      version,
    })
    return json(r, 200, {
      job_id: 'job-1', result_set_id: 'rs-1', status: progressReads >= 3 ? 'success' : 'running',
      loaded_rows: loadedRows, result_version: version, columns_ready: true,
      first_batch_ready: true, terminal: progressReads >= 3, error: null, error_code: null,
      retry_after_ms: progressReads >= 3 ? 0 : 1000, has_new_result: progressReads <= 2,
      truncated: false, has_more: true, pagination_mode: 'ordered_offset',
      pagination_reason: 'fresh_read_ordered_offset', timings: null, execution: null,
    })
  })

  await page.goto('/projects/project-1/sql')
  await page.getByRole('button', { name: 'Run' }).click()
  await expect.poll(() => page.locator('table.text-data tbody tr[data-row-index]').count()).toBeLessThan(100)
  await expect.poll(() => state.resultRequestUrls.length).toBe(1)
  await page.locator('table.text-data tbody').evaluate((tbody) => {
    const observedWindow = window as typeof window & {
      __sqlFirstResultRow?: Element | null
      __sqlResultMutations?: number
    }
    observedWindow.__sqlFirstResultRow = tbody.firstElementChild
    observedWindow.__sqlResultMutations = 0
    new MutationObserver((records) => {
      observedWindow.__sqlResultMutations =
        (observedWindow.__sqlResultMutations ?? 0) + records.length
    }).observe(tbody, { childList: true, subtree: true, characterData: true })
  })

  await expect.poll(() => progressReads).toBe(3)
  await expect(page.getByText('1-100 of 200+')).toBeVisible()
  await expect(page.locator('table.text-data')).toHaveAttribute('aria-rowcount', '-1')

  const domObservation = await page.locator('table.text-data tbody').evaluate((tbody) => {
    const observedWindow = window as typeof window & {
      __sqlFirstResultRow?: Element | null
      __sqlResultMutations?: number
    }
    return {
      firstRowPreserved: observedWindow.__sqlFirstResultRow === tbody.firstElementChild,
      mutations: observedWindow.__sqlResultMutations ?? 0,
    }
  })
  expect({
    observedProgress,
    resultOffsets: state.resultRequestUrls.map((url) =>
      new URL(url).searchParams.get('offset'),
    ),
    domObservation,
  }).toEqual({
    observedProgress: [
      { afterVersion: '0', loadedRows: 100, version: 1 },
      { afterVersion: '1', loadedRows: 200, version: 2 },
      { afterVersion: '2', loadedRows: 200, version: 2 },
    ],
    resultOffsets: ['0'],
    domObservation: { firstRowPreserved: true, mutations: 0 },
  })
  expectNoConsoleErrors()
})

test('progressive append refreshes an incomplete current page so new rows stay visible', async ({
  page,
}) => {
  await mockWorkspace(page)
  let progressReads = 0
  let loadedRows = 0
  const resultRequests: Array<{ offset: number; limit: number }> = []
  await page.route(/\/api\/jobs\/job-1\/progress\?/, (r) => {
    progressReads += 1
    loadedRows = progressReads === 1 ? 50 : 100
    const terminal = progressReads >= 3
    return json(r, 200, {
      job_id: 'job-1', result_set_id: 'rs-1', status: terminal ? 'success' : 'running',
      loaded_rows: loadedRows, result_version: progressReads === 1 ? 1 : 2,
      columns_ready: true, first_batch_ready: true, terminal, error: null, error_code: null,
      retry_after_ms: terminal ? 0 : 1000, has_new_result: progressReads <= 2,
      truncated: false, has_more: false, pagination_mode: 'unavailable',
      pagination_reason: 'top_level_order_by_required', timings: null, execution: null,
    })
  })
  await page.route(/\/api\/jobs\/job-1\/result\?/, (r) => {
    const requestUrl = new URL(r.request().url())
    const offset = Number(requestUrl.searchParams.get('offset') ?? 0)
    const limit = Number(requestUrl.searchParams.get('limit') ?? 100)
    resultRequests.push({ offset, limit })
    const responseRowCount = offset === 0 ? loadedRows : limit + 25
    return json(r, 200, {
      job_id: 'job-1', result_set_id: 'rs-1', offset, limit,
      columns: [
        { name: 'id', type: 'integer', driver_type: 'INT', nullable: false, primary_key: true },
      ],
      rows: Array.from(
        { length: Math.max(0, Math.min(responseRowCount, loadedRows - offset + 25)) },
        (_, index) => ({ values: [index + offset + 1] }),
      ),
      loaded_rows: loadedRows, total_rows: null,
      state: progressReads >= 3 ? 'complete' : 'streaming',
      truncated: false, has_more: false, page_size: 100, max_result_rows: 1000,
      pagination_mode: 'unavailable', pagination_reason: 'top_level_order_by_required',
      preview_truncated_cells: progressReads === 1 ? 1 : 2,
    })
  })

  await page.goto('/projects/project-1/sql')
  await page.getByRole('button', { name: 'Run' }).click()
  await expect.poll(() => page.locator('table.text-data tbody tr[data-row-index]').count()).toBeLessThan(100)
  await page.locator('tr[data-row-index="0"]').evaluate((row) => {
    ;(window as typeof window & { __sqlDeltaFirstRow?: Element }).__sqlDeltaFirstRow = row
  })
  await expect.poll(() => progressReads).toBe(3)
  const firstRowPreserved = await page.locator('table.text-data tbody').evaluate((tbody) => {
    const observedWindow = window as typeof window & { __sqlDeltaFirstRow?: Element }
    return observedWindow.__sqlDeltaFirstRow === tbody.querySelector('tr[data-row-index="0"]')
  })
  expect(firstRowPreserved).toBe(true)
  await page.getByTestId('result-table-scroll').evaluate((element) => {
    element.scrollTop = element.scrollHeight
    element.dispatchEvent(new Event('scroll'))
  })
  await expect(page.getByRole('cell', { name: '100', exact: true }).last()).toBeVisible()
  await expect(page.locator('tr[data-row-index="100"]')).toHaveCount(0)
  await expect(page.getByText('3 large cell(s) were shortened for safe preview')).toBeVisible()

  expect(resultRequests).toEqual([
    { offset: 0, limit: 100 },
    { offset: 50, limit: 50 },
  ])
  expectNoConsoleErrors()
})

test('empty result sets keep the result table empty state visible', async ({ page }) => {
  await mockWorkspace(page)
  await page.route(/\/api\/jobs\/job-1\/result\?/, (r) =>
    json(r, 200, {
      job_id: 'job-1',
      result_set_id: 'rs-1',
      offset: 0,
      limit: 100,
      columns: [],
      rows: [],
      loaded_rows: 0,
      total_rows: 0,
      state: 'complete',
      truncated: false,
      has_more: false,
      page_size: 100,
      max_result_rows: 1000,
      pagination_mode: 'unavailable',
      pagination_reason: 'top_level_order_by_required',
      preview_truncated_cells: 0,
    }),
  )

  await page.goto('/projects/project-1/sql')
  await page.getByRole('button', { name: 'Run' }).click()
  await expect(page.getByText('Empty result set')).toBeVisible()
  await expect(page.locator('table.text-data tbody tr[data-row-index]')).toHaveCount(0)
  expectNoConsoleErrors()
})

test('unchanged progress backs off and does not read result without a new version', async ({
  page,
}) => {
  const state = await mockWorkspace(page)
  let progressReads = 0
  await page.route(/\/api\/jobs\/job-1\/progress\?/, (r) => {
    progressReads += 1
    return json(r, 200, {
      job_id: 'job-1', result_set_id: 'rs-1', status: 'running', loaded_rows: 0,
      result_version: 0, columns_ready: false, first_batch_ready: false, terminal: false,
      error: null, error_code: null, retry_after_ms: 1000, has_new_result: false,
      truncated: false, has_more: false, timings: null, execution: null,
    })
  })

  await page.goto('/projects/project-1/sql')
  await page.getByRole('button', { name: 'Run' }).click()
  await page.waitForTimeout(6_500)

  expect(progressReads).toBe(3)
  expect(state.resultRequestUrls).toEqual([])
  expectNoConsoleErrors()
})

test('hidden SQL workspace lowers polling frequency and resumes when visible', async ({ page }) => {
  await mockWorkspace(page)
  let progressReads = 0
  await page.route(/\/api\/jobs\/job-1\/progress\?/, (r) => {
    progressReads += 1
    return json(r, 200, {
      job_id: 'job-1', result_set_id: 'rs-1', status: 'running', loaded_rows: 0,
      result_version: 0, columns_ready: false, first_batch_ready: false, terminal: false,
      error: null, error_code: null, retry_after_ms: 1000, has_new_result: false,
      truncated: false, has_more: false, timings: null, execution: null,
    })
  })

  await page.goto('/projects/project-1/sql')
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => true })
    document.dispatchEvent(new Event('visibilitychange'))
  })
  await page.getByRole('button', { name: 'Run' }).click()
  await page.waitForTimeout(2_200)
  expect(progressReads).toBe(0)

  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => false })
    document.dispatchEvent(new Event('visibilitychange'))
  })
  await expect.poll(() => progressReads).toBe(1)
  expectNoConsoleErrors()
})

test('switching consoles stops the previous job polling loop', async ({ page }) => {
  await mockWorkspace(page)
  let progressReads = 0
  await page.route(/\/api\/jobs\/job-1\/progress\?/, (r) => {
    progressReads += 1
    return json(r, 200, {
      job_id: 'job-1', result_set_id: 'rs-1', status: 'running', loaded_rows: 0,
      result_version: 0, columns_ready: false, first_batch_ready: false, terminal: false,
      error: null, error_code: null, retry_after_ms: 1000, has_new_result: false,
      truncated: false, has_more: false, timings: null, execution: null,
    })
  })

  await page.goto('/projects/project-1/sql')
  await page.getByRole('button', { name: 'Run' }).click()
  await expect.poll(() => progressReads).toBe(1)
  await page.getByTitle('New console').click()
  await expect(page.locator('aside').getByText('query_2.sql')).toBeVisible()
  const readsAfterSwitch = progressReads
  await page.waitForTimeout(2_200)

  expect(progressReads).toBe(readsAfterSwitch)
  expectNoConsoleErrors()
})

test('progress polling respects 429 Retry-After and recovers without a request storm', async ({
  page,
}) => {
  const state = await mockWorkspace(page, { progressiveRows: 1 })
  const progressReadTimes: number[] = []
  await page.route(/\/api\/jobs\/job-1\/progress\?/, (r) => {
    progressReadTimes.push(Date.now())
    if (progressReadTimes.length === 1) {
      return r.fulfill({
        status: 429,
        headers: { 'Content-Type': 'application/json', 'Retry-After': '2' },
        body: JSON.stringify({ error: 'rate_limited', message: 'Too many requests' }),
      })
    }
    return json(r, 200, {
      job_id: 'job-1', result_set_id: 'rs-1', status: 'success', loaded_rows: 1,
      result_version: 1, columns_ready: true, first_batch_ready: true, terminal: true,
      error: null, error_code: null, retry_after_ms: 0, has_new_result: true,
      truncated: false, has_more: false, timings: null,
      execution: {
        queued_at: now, claimed_at: now, finished_at: now, max_rows: 1000,
        rows_read: 1, rows_returned: 1, output_limit_applied: true,
        limit_pushdown: false, query_shape: 'aggregate',
      },
    })
  })

  await page.goto('/projects/project-1/sql')
  await page.getByRole('button', { name: 'Run' }).click()
  await expect(page.getByRole('cell', { name: '1', exact: true }).last()).toBeVisible()

  expect(progressReadTimes).toHaveLength(2)
  expect(progressReadTimes[1] - progressReadTimes[0]).toBeGreaterThanOrEqual(1_900)
  expect(state.resultRequestUrls).toHaveLength(1)
  await expect(page.getByTestId('sql-output-limit-warning')).toContainText(
    'database output is capped',
  )
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

test('repair mode disables SQL execution and the editor shortcut cannot enqueue a job', async ({
  page,
}) => {
  const state = await mockWorkspace(page)
  await mockLicense(page, { mode: 'repair' })

  await page.goto('/projects/project-1/sql')
  const run = page.getByRole('button', { name: 'Run' })
  await expect(run).toBeDisabled()
  await expect(run).toHaveAttribute(
    'title',
    'Write actions are disabled in the current license state (view / license update only)',
  )

  await page.locator('.monaco-editor').click()
  await page.keyboard.press('Control+Enter')
  await page.waitForTimeout(100)
  expect(state.executeRequests).toEqual([])
  expectNoConsoleErrors()
})
