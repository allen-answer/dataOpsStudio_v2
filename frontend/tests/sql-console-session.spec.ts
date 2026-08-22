import { test, expect, type Page, type Route } from '@playwright/test'
import { json, mockLicense, seedAdminAuth, trackConsoleErrors } from './helpers'

/**
 * SQL 控制台会话闭环(Session Broker A6,设计 §3.3 / §4)。
 *
 * 覆盖:懒 attach、同 console 串行提交、执行→取消→再执行、双 tab 接管、
 * session_lost + 重连、`console_session_enabled` / 非会话方言 / 旧 API 的回退路由。
 *
 * 全部 route-mock,不连后端 —— 与仓内既有 Playwright 约定一致
 * (playwright.config.ts:「只验证前端在各 API 形态下正确渲染」)。
 */

const now = '2026-08-22T06:00:00Z'

function datasource(dbType = 'mysql') {
  return {
    id: 'ds-1',
    name: 'warehouse',
    db_type: dbType,
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
  }
}

function consoleRow() {
  return {
    id: 'console-1',
    name: 'query_1.sql',
    datasource_id: 'ds-1',
    sql: 'SELECT id FROM users; SELECT id FROM orders',
    pinned: false,
    created_at: now,
    updated_at: now,
  }
}

interface SessionScript {
  /** attach 应答:200 会话体,或 {status, body} 直接给错误。 */
  attach?: (call: number) => { status: number; body: unknown }
  /** 语句 progress 应答工厂,按 statement_id + 该语句的轮询次数产出。 */
  progress?: (statementId: string, poll: number) => Record<string, unknown>
  submitStatus?: (call: number) => { status: number; body: unknown } | null
  dbType?: string
}

interface SessionCalls {
  attach: number
  submits: Record<string, unknown>[]
  cancels: { url: string; body: Record<string, unknown> }[]
  closes: number
  executes: Record<string, unknown>[]
  progressPolls: Record<string, number>
}

function sessionBody(overrides: Record<string, unknown> = {}) {
  return {
    session_id: 'sess-1',
    epoch: 1,
    current_epoch: 1,
    state: 'idle',
    db_type: 'mysql',
    server_cancel: 'available',
    current_statement_id: null,
    idle_deadline: null,
    last_activity_at: now,
    close_reason: null,
    error_code: null,
    ...overrides,
  }
}

function progressBody(
  statementId: string,
  state: string,
  overrides: Record<string, unknown> = {},
) {
  const terminal = !['accepted', 'executing', 'streaming'].includes(state)
  return {
    statement_id: statementId,
    session: { session_id: 'sess-1', state: 'executing', current_epoch: 1 },
    result_set_id: `rs-${statementId}`,
    state,
    loaded_rows: 2,
    result_version: 1,
    columns_ready: true,
    first_batch_ready: true,
    terminal,
    error: null,
    error_code: null,
    retry_after_ms: terminal ? 0 : 1000,
    has_new_result: true,
    truncated: false,
    has_more: false,
    pagination_mode: 'unavailable',
    pagination_reason: 'top_level_order_by_required',
    timings: null,
    execution: {
      queued_at: now,
      claimed_at: now,
      finished_at: terminal ? now : null,
      max_rows: 1000,
      output_limit_applied: false,
      limit_pushdown: true,
    },
    ...overrides,
  }
}

function resultBody(statementId: string) {
  return {
    statement_id: statementId,
    statement_state: 'succeeded',
    result_set_id: `rs-${statementId}`,
    offset: 0,
    limit: 100,
    columns: [
      { name: 'id', type: 'integer', driver_type: 'INT', nullable: false, primary_key: true },
    ],
    rows: [{ values: [1] }, { values: [2] }],
    loaded_rows: 2,
    total_rows: null,
    state: 'complete',
    truncated: false,
    has_more: false,
    page_size: 100,
    max_result_rows: 1000,
    preview_truncated_cells: 0,
    pagination_mode: 'unavailable',
    pagination_reason: 'top_level_order_by_required',
  }
}

async function mockSessionWorkspace(page: Page, script: SessionScript = {}): Promise<SessionCalls> {
  const calls: SessionCalls = {
    attach: 0,
    submits: [],
    cancels: [],
    closes: 0,
    executes: [],
    progressPolls: {},
  }

  await mockLicense(page)
  await page.route('**/api/version', (r) =>
    json(r, 200, { version: '2.0.1-test', commit: 'abcdef0123456789', image_version: 'test' }),
  )
  await page.route(/\/api\/datasources\?/, (r) => json(r, 200, [datasource(script.dbType)]))
  await page.route('**/api/datasources/ds-1/metadata/schemas', (r) => json(r, 200, []))
  await page.route('**/api/sql/consoles', (r) => json(r, 200, [consoleRow()]))
  await page.route('**/api/sql/consoles/console-1', (r) =>
    r.request().method() === 'PATCH' ? json(r, 200, consoleRow()) : r.fallback(),
  )
  await page.route(/\/api\/sql\/history\?/, (r) => json(r, 200, []))
  await page.route(/\/api\/sql\/templates/, (r) => json(r, 200, []))

  await page.route('**/api/sql/sessions/attach', (r: Route) => {
    calls.attach += 1
    const response = script.attach?.(calls.attach) ?? { status: 200, body: sessionBody() }
    return json(r, response.status, response.body)
  })
  await page.route('**/api/sql/sessions/sess-1/statements', (r: Route) => {
    const body = r.request().postDataJSON() as Record<string, unknown>
    calls.submits.push(body)
    const failure = script.submitStatus?.(calls.submits.length)
    if (failure) return json(r, failure.status, failure.body)
    const statementId = `stmt-${calls.submits.length}`
    return json(r, 202, {
      statement_id: statementId,
      result_set_id: `rs-${statementId}`,
      seq: calls.submits.length,
      deduplicated: false,
    })
  })
  await page.route('**/api/sql/sessions/sess-1/close', (r: Route) => {
    calls.closes += 1
    return json(r, 200, sessionBody({ state: 'closed', close_reason: 'user' }))
  })
  await page.route('**/api/sql/sessions/sess-1', (r: Route) => json(r, 200, sessionBody()))

  await page.route(/\/api\/sql\/statements\/[^/]+\/progress/, (r: Route) => {
    const statementId = /statements\/([^/]+)\/progress/.exec(r.request().url())?.[1] ?? ''
    calls.progressPolls[statementId] = (calls.progressPolls[statementId] ?? 0) + 1
    const body =
      script.progress?.(statementId, calls.progressPolls[statementId]) ??
      progressBody(statementId, 'succeeded')
    return json(r, 200, body)
  })
  await page.route(/\/api\/sql\/statements\/[^/]+\/result/, (r: Route) => {
    const statementId = /statements\/([^/]+)\/result/.exec(r.request().url())?.[1] ?? ''
    return json(r, 200, resultBody(statementId))
  })
  await page.route(/\/api\/sql\/statements\/[^/]+\/cancel/, (r: Route) => {
    const statementId = /statements\/([^/]+)\/cancel/.exec(r.request().url())?.[1] ?? ''
    calls.cancels.push({ url: statementId, body: r.request().postDataJSON() })
    return json(r, 200, { accepted: true, statement_state: 'cancelled' })
  })

  // job 路径:回退路由用例断言执行确实落到这里。每条语句一个 job(与真实
  // 行为一致 —— 共用一个 job_id 会让跨 tab lease 只放行一条轮询)。
  await page.route('**/api/sql/execute', (r: Route) => {
    calls.executes.push(r.request().postDataJSON())
    const jobId = `job-${calls.executes.length}`
    return json(r, 200, { job_id: jobId, result_set_id: `rs-${jobId}` })
  })
  await page.route(/\/api\/jobs\/[^/]+\/progress/, (r) => {
    const jobId = /jobs\/([^/]+)\/progress/.exec(r.request().url())?.[1] ?? 'job-1'
    return json(r, 200, {
      job_id: jobId,
      result_set_id: `rs-${jobId}`,
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
      truncated: false,
      has_more: false,
      timings: null,
      execution: null,
    })
  })
  await page.route(/\/api\/jobs\/[^/]+\/result/, (r) => {
    const jobId = /jobs\/([^/]+)\/result/.exec(r.request().url())?.[1] ?? 'job-1'
    return json(r, 200, { ...resultBody(jobId), job_id: jobId, result_set_id: `rs-${jobId}` })
  })

  return calls
}

async function openWorkspace(page: Page): Promise<string[]> {
  const consoleErrors = trackConsoleErrors(page)
  await seedAdminAuth(page)
  await page.goto('/projects/p-1/sql')
  await expect(page.getByTestId('sql-editor-panel')).toBeVisible()
  return consoleErrors
}

test('lazy attach: opening the workspace never claims a session', async ({ page }) => {
  const calls = await mockSessionWorkspace(page)
  const consoleErrors = await openWorkspace(page)

  await expect(page.getByTestId('sql-session-bar')).toHaveCount(0)
  expect(calls.attach).toBe(0)
  expect(consoleErrors, consoleErrors.join('\n')).toEqual([])
})

test('one console submits statements serially, then cancel and re-run reuse the session', async ({
  page,
}) => {
  const cancelledAfter = new Set<string>()
  const calls = await mockSessionWorkspace(page, {
    progress: (statementId, poll) => {
      if (cancelledAfter.has(statementId)) return progressBody(statementId, 'cancelled')
      // 第一条语句一直在跑,给取消留出窗口;第二条直接终态。
      if (statementId === 'stmt-1' && poll <= 6) return progressBody(statementId, 'executing')
      return progressBody(statementId, 'succeeded')
    },
  })
  const consoleErrors = await openWorkspace(page)

  await page.getByTestId('sql-execute').click()

  // 懒 attach 恰好一次,两条语句**按序**提交,各带独立幂等键。
  await expect.poll(() => calls.submits.length).toBe(2)
  expect(calls.attach).toBe(1)
  const requestIds = calls.submits.map((body) => body.client_request_id)
  expect(new Set(requestIds).size).toBe(2)
  expect(calls.submits.map((body) => body.sql)).toEqual([
    'SELECT id FROM users',
    'SELECT id FROM orders',
  ])
  expect(calls.submits.every((body) => body.epoch === 1)).toBe(true)

  // 会话状态条随 progress 内嵌的 session 块渲染。
  await expect(page.getByTestId('sql-session-bar')).toBeVisible()
  await expect(page.getByTestId('sql-statement-results')).toBeVisible()

  // 执行中 → 取消:带提交时的 epoch(取消权随 epoch 移交,M8)。
  await page.getByTestId('sql-cancel').click()
  await expect.poll(() => calls.cancels.length).toBeGreaterThan(0)
  expect(calls.cancels[0].body.epoch).toBe(1)
  cancelledAfter.add('stmt-1')

  // 取消保留已落的行 —— 明示是部分结果,不冒充完整结果。
  await expect(page.getByTestId('sql-session-partial')).toBeVisible()

  // 再执行:会话复用,不再 attach。
  await page.getByTestId('sql-execute').click()
  await expect.poll(() => calls.submits.length).toBe(4)
  expect(calls.attach).toBe(1)
  expect(consoleErrors, consoleErrors.join('\n')).toEqual([])
})

test('a second window taking the session over is surfaced, and reconnect takes it back', async ({
  page,
}) => {
  let attachEpoch = 1
  const calls = await mockSessionWorkspace(page, {
    attach: () => {
      attachEpoch += attachEpoch === 1 ? 0 : 0
      return { status: 200, body: sessionBody({ epoch: attachEpoch, current_epoch: attachEpoch }) }
    },
    progress: (statementId) =>
      progressBody(statementId, 'executing', {
        // 另一个窗口已 attach:会话当前 epoch 比本 tab 手上的新。
        session: { session_id: 'sess-1', state: 'executing', current_epoch: 2 },
      }),
  })
  const consoleErrors = await openWorkspace(page)

  await page.getByTestId('sql-execute').click()
  await expect(page.getByTestId('sql-session-takeover')).toBeVisible()

  attachEpoch = 3
  await page.getByTestId('sql-session-reconnect').click()
  await expect.poll(() => calls.attach).toBe(2)
  await expect(page.getByTestId('sql-session-takeover')).toHaveCount(0)
  expect(consoleErrors, consoleErrors.join('\n')).toEqual([])
})

test('a stale epoch rejected on submit renders the takeover banner', async ({ page }) => {
  const calls = await mockSessionWorkspace(page, {
    submitStatus: () => ({
      status: 409,
      body: {
        error: 'stale_session_epoch',
        message: 'Session was taken over by another window',
        current_epoch: 7,
      },
    }),
  })
  const consoleErrors = await openWorkspace(page)

  await page.getByTestId('sql-execute').click()
  await expect(page.getByTestId('sql-session-takeover')).toBeVisible()
  // 按序提交的语义:前一条被拒,后面的不再送出去。
  expect(calls.submits.length).toBe(1)
  expect(consoleErrors, consoleErrors.join('\n')).toEqual([])
})

test('a lost session shows the banner and reconnect rebuilds it', async ({ page }) => {
  let lost = true
  const calls = await mockSessionWorkspace(page, {
    progress: (statementId) =>
      progressBody(statementId, lost ? 'failed' : 'succeeded', {
        session: {
          session_id: 'sess-1',
          state: lost ? 'session_lost' : 'idle',
          current_epoch: 1,
        },
      }),
  })
  const consoleErrors = await openWorkspace(page)

  await page.getByTestId('sql-execute').click()
  await expect(page.getByTestId('sql-session-lost')).toBeVisible()

  lost = false
  await page.getByTestId('sql-session-reconnect').click()
  await expect.poll(() => calls.attach).toBe(2)
  await expect(page.getByTestId('sql-session-lost')).toHaveCount(0)
  await expect(page.getByTestId('sql-session-state')).toBeVisible()
  expect(consoleErrors, consoleErrors.join('\n')).toEqual([])
})

test('the degraded-cancel datasource says cancelling drops the session', async ({ page }) => {
  await mockSessionWorkspace(page, {
    attach: () => ({ status: 200, body: sessionBody({ server_cancel: 'degraded' }) }),
    progress: (statementId) => progressBody(statementId, 'executing'),
  })
  const consoleErrors = await openWorkspace(page)

  await page.getByTestId('sql-execute').click()
  await expect(page.getByTestId('sql-session-cancel-degraded')).toBeVisible()
  expect(consoleErrors, consoleErrors.join('\n')).toEqual([])
})

test('console_session_enabled=false falls back to the job path', async ({ page }) => {
  const calls = await mockSessionWorkspace(page, {
    attach: () => ({
      status: 409,
      body: {
        error: 'console_session_disabled',
        message: 'Console sessions are disabled on this deployment',
      },
    }),
  })
  const consoleErrors = await openWorkspace(page)

  await page.getByTestId('sql-execute').click()
  await expect.poll(() => calls.executes.length).toBe(2)
  expect(calls.submits.length).toBe(0)
  await expect(page.getByTestId('sql-session-bar')).toHaveCount(0)

  // 部署级开关只撞一次:第二次执行不再试 attach。
  await page.getByTestId('sql-execute').click()
  await expect.poll(() => calls.executes.length).toBe(4)
  expect(calls.attach).toBe(1)
  expect(consoleErrors, consoleErrors.join('\n')).toEqual([])
})

test('an API without the session endpoints falls back to the job path', async ({ page }) => {
  const calls = await mockSessionWorkspace(page, {
    attach: () => ({ status: 404, body: { detail: 'Not Found' } }),
  })
  const consoleErrors = await openWorkspace(page)

  await page.getByTestId('sql-execute').click()
  await expect.poll(() => calls.executes.length).toBe(2)
  await expect(page.getByTestId('sql-session-bar')).toHaveCount(0)
  expect(consoleErrors, consoleErrors.join('\n')).toEqual([])
})

test('a non-session dialect never attaches and keeps using the job path', async ({ page }) => {
  const calls = await mockSessionWorkspace(page, { dbType: 'postgresql' })
  const consoleErrors = await openWorkspace(page)

  await page.getByTestId('sql-execute').click()
  await expect.poll(() => calls.executes.length).toBe(2)
  expect(calls.attach).toBe(0)
  expect(consoleErrors, consoleErrors.join('\n')).toEqual([])
})
