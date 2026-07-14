import { expect, test, type Page, type Route } from '@playwright/test'

import {
  json,
  mockLicense,
  seedAdminAuth,
  trackConsoleErrors,
} from './helpers'

const now = '2026-07-13T08:00:00Z'

const notificationTargets = [
  {
    id: 'target-email',
    channel: 'email',
    events: ['failed'],
    enabled: true,
    timeout_seconds: 5,
    smtp_host: 'smtp.example.invalid',
    smtp_port: 587,
    smtp_from: 'robot@example.invalid',
    smtp_to: ['ops@example.invalid'],
    smtp_user: 'robot',
    url_secret_ref: 'internal-url-ref-must-not-render',
    password_secret_ref: 'internal-password-ref-must-not-render',
  },
  {
    id: 'target-hook',
    channel: 'webhook',
    events: ['all'],
    enabled: true,
    timeout_seconds: 5,
    url_secret_ref: 'internal-hook-ref-must-not-render',
  },
]

const workflow = {
  id: 'wf-1',
  project_id: 'project-1',
  name: 'Daily QA',
  enabled: true,
  schedule_cron: '0 3 * * *',
  schedule_enabled: true,
  created_by: 'user-1',
  created_at: now,
  updated_at: now,
  spec: {
    nodes: [
      {
        id: 'extract',
        job_kind: 'sql_query',
        payload: { datasource_id: 'ds-1', sql: 'SELECT 1', params: { region: 'cn' } },
        retry_policy: null,
        timeout_seconds: 60,
        on_failure: 'abort',
        when: null,
      },
      {
        id: 'compare',
        job_kind: 'compare_run',
        payload: { task_id: 'task-old' },
        retry_policy: null,
        timeout_seconds: 300,
        on_failure: 'abort',
        when: null,
      },
      {
        id: 'export',
        job_kind: 'export_excel',
        payload: {
          source_result_set_id: '${nodes.extract.result_set_id}',
          filename: 'daily.xlsx',
        },
        retry_policy: null,
        timeout_seconds: 120,
        on_failure: 'abort',
        when: null,
      },
      {
        id: 'route',
        job_kind: 'branch',
        payload: {},
        retry_policy: null,
        timeout_seconds: 30,
        on_failure: 'abort',
        when: null,
      },
      {
        id: 'notify',
        job_kind: 'notify',
        payload: { target_ids: ['target-email'], message: 'Daily QA complete' },
        retry_policy: null,
        timeout_seconds: 30,
        on_failure: 'continue',
        when: null,
      },
      {
        id: 'guard',
        job_kind: 'sql_explain',
        payload: { datasource_id: 'ds-1', sql: 'SELECT 1' },
        retry_policy: null,
        timeout_seconds: 60,
        on_failure: 'branch',
        when: null,
      },
    ],
    edges: [
      { source: 'extract', target: 'compare', trigger: 'success', when: null, is_default: false },
      { source: 'extract', target: 'export', trigger: 'success', when: null, is_default: false },
      {
        source: 'route',
        target: 'notify',
        trigger: 'success',
        when: '${nodes.compare.diff_count} > 0',
        is_default: false,
      },
      { source: 'route', target: 'export', trigger: 'success', when: null, is_default: true },
      {
        source: 'guard',
        target: 'notify',
        trigger: 'failure',
        when: '${nodes.guard.error_code} == "timeout"',
        is_default: false,
      },
      { source: 'guard', target: 'export', trigger: 'failure', when: null, is_default: true },
    ],
    schedule: { cron: '0 3 * * *', enabled: true },
    sensor: {
      sql: 'SELECT COUNT(*) FROM arrivals',
      datasource_id: 'ds-1',
      check_interval_seconds: 60,
      cooldown_seconds: 300,
      enabled: true,
    },
    notifications: notificationTargets,
    variables: { region: 'cn', tenants: ['a', 'b'] },
  },
}

const secondaryWorkflow = {
  ...workflow,
  id: 'wf-2',
  name: 'Secondary QA',
  schedule_cron: null,
  schedule_enabled: false,
  spec: {
    ...workflow.spec,
    nodes: [
      {
        id: 'secondary_pause',
        job_kind: 'sleep',
        payload: { duration_seconds: 5 },
        retry_policy: null,
        timeout_seconds: 60,
        on_failure: 'abort',
        when: null,
      },
    ],
    edges: [],
    schedule: null,
    notifications: [],
  },
}

const compareTask = {
  id: 'task-1',
  project_id: 'project-1',
  name: 'Orders parity',
  source_id: 'ds-1',
  target_id: 'ds-2',
  source_ref: { kind: 'table', schema_name: 'src', table_name: 'orders' },
  target_ref: { kind: 'table', schema_name: 'dst', table_name: 'orders' },
  columns: [{ name: 'id', type: 'integer', driver_type: null, nullable: false, primary_key: true }],
  compare_rules: {
    key_columns: ['id'],
    ignore_columns: [],
    column_mappings: {},
    numeric_tolerance: null,
    trim_strings: true,
    case_insensitive: false,
    empty_as_null: true,
    schema_policy: 'strict',
  },
  run_limits: { query_timeout_seconds: 120, result_format: 'json' },
  created_by: 'user-1',
  created_at: now,
  updated_at: now,
}

interface MockState {
  workflowWrites: unknown[]
  notificationWrites: unknown[]
  runOffsets: number[]
}

interface MockWorkflowOptions {
  emptyWorkflows?: boolean
  updateError?: { status?: number; error: string; message: string }
}

function workflowListItem(value = workflow) {
  return {
    id: value.id,
    project_id: value.project_id,
    name: value.name,
    node_count: value.spec.nodes.length,
    enabled: value.enabled,
    schedule_cron: value.schedule_cron,
    schedule_enabled: value.schedule_enabled,
    created_by: value.created_by,
    created_at: value.created_at,
    updated_at: value.updated_at,
  }
}

async function mockWorkflowPage(
  page: Page,
  options: MockWorkflowOptions = {},
): Promise<MockState> {
  const state: MockState = { workflowWrites: [], notificationWrites: [], runOffsets: [] }
  await mockLicense(page)
  await page.route(/\/api\/datasources\?project_id=project-1/, (route) =>
    json(route, 200, [
      { id: 'ds-1', name: 'warehouse_a', db_type: 'postgresql', status: 'ready' },
      { id: 'ds-2', name: 'warehouse_b', db_type: 'postgresql', status: 'ready' },
    ]),
  )
  await page.route(/\/api\/compare\/tasks\?project_id=project-1/, (route) =>
    json(route, 200, [
      compareTask,
      {
        ...compareTask,
        id: 'task-file',
        name: 'Uploaded file compare',
        source_id: null,
        source_ref: { kind: 'file', upload_id: 'upload-1', file_format: 'csv' },
      },
    ]),
  )
  await page.route('**/api/projects/project-1/**', async (route: Route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname
    if (path === '/api/projects/project-1/workflows' && request.method() === 'GET') {
      return json(
        route,
        200,
        options.emptyWorkflows
          ? []
          : [workflowListItem(), workflowListItem(secondaryWorkflow)],
      )
    }
    if (path === '/api/projects/project-1/workflows' && request.method() === 'POST') {
      state.workflowWrites.push(request.postDataJSON())
      return json(route, 201, workflow)
    }
    if (path === '/api/projects/project-1/workflows/wf-1' && request.method() === 'GET') {
      return json(route, 200, workflow)
    }
    if (path === '/api/projects/project-1/workflows/wf-2' && request.method() === 'GET') {
      return json(route, 200, secondaryWorkflow)
    }
    if (path === '/api/projects/project-1/workflows/wf-1' && request.method() === 'PUT') {
      state.workflowWrites.push(request.postDataJSON())
      if (options.updateError) {
        return json(route, options.updateError.status ?? 400, options.updateError)
      }
      return json(route, 200, workflow)
    }
    if (
      path === '/api/projects/project-1/workflows/wf-1/notifications' &&
      request.method() === 'POST'
    ) {
      state.notificationWrites.push(request.postDataJSON())
      return json(route, 201, {
        id: 'target-new',
        channel: 'email',
        events: ['failed'],
        enabled: true,
        timeout_seconds: 5,
        smtp_host: 'smtp.example.invalid',
        smtp_port: 587,
        smtp_from: 'robot@example.invalid',
        smtp_to: ['ops@example.invalid'],
        smtp_user: 'robot',
      })
    }
    if (path === '/api/projects/project-1/workflows/wf-1/runs' && request.method() === 'GET') {
      const offset = Number(url.searchParams.get('offset') ?? 0)
      state.runOffsets.push(offset)
      return json(route, 200, {
        workflow_id: 'wf-1',
        limit: 20,
        offset,
        has_more: offset === 0,
        runs: [
          {
            run_id: offset === 0 ? 'run-1' : 'run-21',
            job_id: offset === 0 ? 'run-1' : 'run-21',
            status: 'success',
            error: null,
            created_at: now,
            started_at: now,
            finished_at: now,
          },
        ],
      })
    }
    if (path === '/api/projects/project-1/workflow-runs/run-1') {
      return json(route, 200, {
        run_id: 'run-1',
        workflow_id: 'wf-1',
        project_id: 'project-1',
        status: 'success',
        error: null,
        created_at: now,
        started_at: now,
        finished_at: now,
        nodes: [
          {
            node_id: 'notify',
            job_kind: 'notify',
            status: 'success',
            job_id: 'job-notify',
            attempts: 1,
            error: null,
            outputs: { sent_count: 2, delivered: true, note: null },
          },
        ],
      })
    }
    return json(route, 404, { detail: 'unmocked' })
  })
  return state
}

async function openExistingEditor(page: Page): Promise<void> {
  await page.goto('/projects/project-1/workflows')
  await expect(page.getByText('Daily QA', { exact: true }).first()).toBeVisible()
  await page.getByRole('button', { name: 'Edit definition' }).click()
  await expect(page.getByTestId('workflow-editor')).toBeVisible()
}

test.beforeEach(async ({ page }) => {
  await seedAdminAuth(page)
})

test('structured edit sends enabled, scalar/list variables, and SQL sensor', async ({ page }) => {
  const consoleErrors = trackConsoleErrors(page)
  const state = await mockWorkflowPage(page)
  await openExistingEditor(page)

  await page.getByTestId('workflow-name').fill('Daily QA v2')
  await page.getByTestId('workflow-enabled').uncheck()
  await expect(page.getByTestId('variable-row')).toHaveCount(2)
  await expect(page.getByTestId('variable-list-value')).toHaveCount(2)
  await page.getByTestId('sensor-interval').fill('120')
  await page.getByRole('button', { name: 'Save changes' }).click()

  expect(state.workflowWrites).toHaveLength(1)
  const body = state.workflowWrites[0] as Record<string, any>
  expect(body.enabled).toBe(false)
  expect(body.spec.variables).toEqual({ region: 'cn', tenants: ['a', 'b'] })
  expect(body.spec.sensor).toMatchObject({
    datasource_id: 'ds-1',
    check_interval_seconds: 120,
    cooldown_seconds: 300,
    enabled: true,
  })
  expect(consoleErrors).toEqual([])
})

test('structured Workflow error codes surface translated messages', async ({ page }) => {
  const rawMessage = 'RAW backend validation detail must not render'
  const state = await mockWorkflowPage(page, {
    updateError: {
      error: 'invalid_node_output_reference',
      message: rawMessage,
    },
  })
  await openExistingEditor(page)
  await page.getByRole('button', { name: 'Save changes' }).click()

  const toast = page.getByTestId('toast')
  await expect(toast).toContainText('Node output reference is invalid')
  await expect(toast).not.toContainText(rawMessage)
  await expect(toast).not.toContainText('Workflow spec is invalid')
  expect(state.workflowWrites).toHaveLength(1)
})

test('variable values accept 512 characters and reject 513', async ({ page }) => {
  const state = await mockWorkflowPage(page)
  await openExistingEditor(page)

  const scalarValue = page.getByTestId('variable-row').first().locator('input').nth(1)
  const listValue = page.getByTestId('variable-list-value').first()
  await listValue.fill('b'.repeat(513))
  await page.getByRole('button', { name: 'Save changes' }).click()
  expect(state.workflowWrites).toEqual([])

  await scalarValue.fill('a'.repeat(257))
  await listValue.fill('b'.repeat(512))
  await page.getByRole('button', { name: 'Save changes' }).click()

  expect(state.workflowWrites).toHaveLength(1)
  const variables = (state.workflowWrites[0] as Record<string, any>).spec.variables
  expect(variables.region).toHaveLength(257)
  expect(variables.tenants[0]).toHaveLength(512)
})

test('switching workflows closes the unsaved editor instead of retargeting its draft', async ({
  page,
}) => {
  const state = await mockWorkflowPage(page)
  await openExistingEditor(page)
  await page.getByTestId('workflow-name').fill('Unsaved wrong target')

  await page.getByRole('button', { name: /Secondary QA/ }).click()

  await expect(page.getByTestId('workflow-editor')).toHaveCount(0)
  await expect(page.getByText('Secondary QA', { exact: true }).first()).toBeVisible()
  expect(state.workflowWrites).toEqual([])
})

test('create form lists all eight kinds and keeps Notify unavailable until first save', async ({
  page,
}) => {
  const consoleErrors = trackConsoleErrors(page)
  const state = await mockWorkflowPage(page)
  await page.goto('/projects/project-1/workflows')
  await page.getByRole('button', { name: 'New' }).click()

  const kind = page.getByTestId('node-kind-0')
  await expect(kind.locator('option')).toHaveCount(8)
  await expect(kind.locator('option[value="notify"]')).toHaveAttribute('disabled', '')
  await expect(page.getByText(/Save the workflow before adding Notify/)).toBeVisible()
  await page.getByTestId('workflow-name').fill('Created from form')
  await page.getByTestId('workflow-enabled').uncheck()
  await page.getByRole('button', { name: 'Save changes' }).click()
  expect(state.workflowWrites).toHaveLength(1)
  expect(state.workflowWrites[0]).toMatchObject({ name: 'Created from form', enabled: false })
  expect(consoleErrors).toEqual([])
})

test('empty workflow list opens the structured create form', async ({ page }) => {
  const consoleErrors = trackConsoleErrors(page)
  await mockWorkflowPage(page, { emptyWorkflows: true })
  await page.goto('/projects/project-1/workflows')

  await expect(page.getByText('No workflows yet')).toBeVisible()
  await page.getByRole('button', { name: 'New' }).click()

  await expect(page.getByTestId('workflow-editor')).toBeVisible()
  expect(consoleErrors).toEqual([])
})

test('all eight kinds map core payloads, including Compare snapshot and Export placeholder', async ({
  page,
}) => {
  const state = await mockWorkflowPage(page)
  await openExistingEditor(page)

  await page.getByTestId('compare-task-1').selectOption('task-1')
  await page.getByTestId('export-source-2').selectOption('extract')
  await page.getByTestId('workflow-add-node').click()
  const lineage = page.getByTestId('workflow-node-6')
  await lineage.getByLabel('Kind').selectOption('lineage_analyze')
  await lineage.getByLabel('Datasource').selectOption('ds-1')
  await lineage.getByLabel('Dialect (optional)').fill('postgres')
  await lineage.getByLabel('Default schema (optional)').fill('public')
  await lineage.getByLabel('Source reference (optional)').fill('orders.sql')
  await lineage.getByLabel('SQL', { exact: true }).fill('SELECT * FROM orders')
  await page.getByTestId('workflow-add-node').click()
  const sleep = page.getByTestId('workflow-node-7')
  await sleep.getByLabel('Kind').selectOption('sleep')
  await sleep.getByLabel('Duration seconds (1–86400)').fill('45')
  await page.getByRole('button', { name: 'Save changes' }).click()

  const body = state.workflowWrites[0] as Record<string, any>
  expect(new Set(body.spec.nodes.map((node: any) => node.job_kind))).toEqual(
    new Set([
      'sql_query',
      'sql_explain',
      'compare_run',
      'lineage_analyze',
      'export_excel',
      'notify',
      'sleep',
      'branch',
    ]),
  )
  expect(body.spec.nodes[0].payload.params).toEqual({ region: 'cn' })
  expect(body.spec.nodes[1].payload).toMatchObject({
    task_id: 'task-1',
    source_id: 'ds-1',
    target_id: 'ds-2',
    source_ref: compareTask.source_ref,
    target_ref: compareTask.target_ref,
    columns: compareTask.columns,
    compare_rules: compareTask.compare_rules,
    run_limits: compareTask.run_limits,
  })
  expect(body.spec.nodes[1].payload.run_id).toBeUndefined()
  expect(body.spec.nodes[2].payload.source_result_set_id).toBe(
    '${nodes.extract.result_set_id}',
  )
  expect(body.spec.nodes.find((node: any) => node.job_kind === 'branch').payload).toEqual({})
  expect(body.spec.nodes.find((node: any) => node.job_kind === 'notify').payload).toMatchObject({
    target_ids: ['target-email'],
    message: 'Daily QA complete',
  })
  expect(body.spec.nodes.find((node: any) => node.job_kind === 'lineage_analyze').payload)
    .toEqual({
      datasource_id: 'ds-1',
      sql_text: 'SELECT * FROM orders',
      dialect: 'postgres',
      default_schema: 'public',
      source_ref: 'orders.sql',
    })
  expect(body.spec.nodes.find((node: any) => node.job_kind === 'sleep').payload)
    .toEqual({ duration_seconds: 45 })
  expect(body.spec.edges).toContainEqual({
    source: 'extract',
    target: 'export',
    trigger: 'success',
    when: null,
    is_default: false,
  })
})

test('conditional and failure routes preserve semantic order and one default', async ({ page }) => {
  const state = await mockWorkflowPage(page)
  await openExistingEditor(page)

  await page.getByTestId('edge-down-2').click()
  await page.getByRole('button', { name: 'Save changes' }).click()

  const edges = (state.workflowWrites[0] as Record<string, any>).spec.edges
  expect(edges.slice(2, 4)).toEqual([
    { source: 'route', target: 'export', trigger: 'success', when: null, is_default: true },
    {
      source: 'route',
      target: 'notify',
      trigger: 'success',
      when: '${nodes.compare.diff_count} > 0',
      is_default: false,
    },
  ])
  expect(edges.filter((edge: any) => edge.source === 'guard' && edge.trigger === 'failure'))
    .toHaveLength(2)
})

test('one default failure route is accepted by the structured editor', async ({ page }) => {
  const state = await mockWorkflowPage(page)
  await openExistingEditor(page)

  await page.getByRole('button', { name: 'Advanced DAG JSON' }).click()
  const editor = page.getByTestId('advanced-json')
  const parsed = JSON.parse(await editor.inputValue())
  parsed.edges = parsed.edges.filter(
    (edge: any) =>
      edge.source !== 'guard' || edge.trigger !== 'failure' || edge.is_default,
  )
  await editor.fill(JSON.stringify(parsed, null, 2))
  await page.getByRole('button', { name: 'Form' }).click()
  await page.getByRole('button', { name: 'Save changes' }).click()

  expect(state.workflowWrites).toHaveLength(1)
  const edges = (state.workflowWrites[0] as Record<string, any>).spec.edges
  expect(edges.filter((edge: any) => edge.source === 'guard' && edge.trigger === 'failure'))
    .toEqual([
      { source: 'guard', target: 'export', trigger: 'failure', when: null, is_default: true },
    ])
})

test('changing routing node modes clears route fields that would become hidden', async ({ page }) => {
  const state = await mockWorkflowPage(page)
  await openExistingEditor(page)
  await page.getByTestId('node-kind-3').selectOption('sleep')
  await page.getByTestId('workflow-node-5').getByLabel('On failure').selectOption('abort')
  await page.getByRole('button', { name: 'Save changes' }).click()

  expect(state.workflowWrites).toHaveLength(1)
  const edges = (state.workflowWrites[0] as Record<string, any>).spec.edges
  expect(edges.filter((edge: any) => edge.source === 'route')).toEqual([
    { source: 'route', target: 'notify', trigger: 'success', when: null, is_default: false },
    { source: 'route', target: 'export', trigger: 'success', when: null, is_default: false },
  ])
  expect(edges.filter((edge: any) => edge.source === 'guard' && edge.trigger === 'failure'))
    .toEqual([])
})

test('advanced DAG JSON round-trips form fields without exposing notification refs', async ({
  page,
}) => {
  const state = await mockWorkflowPage(page)
  await openExistingEditor(page)

  await page.getByRole('button', { name: 'Advanced DAG JSON' }).click()
  const editor = page.getByTestId('advanced-json')
  await expect(editor).not.toContainText('url_secret_ref')
  await expect(editor).not.toContainText('password_secret_ref')
  const raw = await editor.inputValue()
  expect(raw).not.toContain('url_secret_ref')
  expect(raw).not.toContain('password_secret_ref')
  const parsed = JSON.parse(raw)
  parsed.nodes[0].payload.preserved_unknown_key = 'kept'
  parsed.notifications = []
  await editor.fill(JSON.stringify(parsed, null, 2))
  await page.getByRole('button', { name: 'Form' }).click()
  await page.getByRole('button', { name: 'Advanced DAG JSON' }).click()
  await expect(editor).toHaveValue(/preserved_unknown_key/)
  expect(state.workflowWrites).toHaveLength(0)

  await editor.fill('{ invalid')
  await page.getByRole('button', { name: 'Form' }).click()
  await expect(page.getByText('DAG JSON is not valid JSON')).toBeVisible()
  await expect(page.getByTestId('advanced-json')).toBeVisible()
  await editor.fill(JSON.stringify(parsed, null, 2))
  await page.getByRole('button', { name: 'Form' }).click()
  await page.getByRole('button', { name: 'Save changes' }).click()
  const savedSpec = (state.workflowWrites[0] as Record<string, any>).spec
  expect(savedSpec).not.toHaveProperty('notifications')
  expect(savedSpec.nodes[0].payload.preserved_unknown_key).toBe('kept')
})

test('changing Export source replaces its generated success edge', async ({ page }) => {
  const state = await mockWorkflowPage(page)
  await openExistingEditor(page)
  await page.getByRole('button', { name: 'Advanced DAG JSON' }).click()
  const editor = page.getByTestId('advanced-json')
  const parsed = JSON.parse(await editor.inputValue())
  parsed.nodes.splice(2, 0, {
    id: 'alt_extract', job_kind: 'sql_query',
    payload: { datasource_id: 'ds-1', sql: 'SELECT 2' },
    retry_policy: null, timeout_seconds: 60, on_failure: 'abort', when: null,
  })
  await editor.fill(JSON.stringify(parsed, null, 2))
  await page.getByRole('button', { name: 'Form' }).click()
  await page.getByTestId('node-kind-0').selectOption('sleep')
  await page.getByTestId('export-source-3').selectOption('alt_extract')
  await page.getByRole('button', { name: 'Save changes' }).click()

  const body = state.workflowWrites[0] as Record<string, any>
  expect(body.spec.nodes.find((node: any) => node.id === 'export').payload.source_result_set_id)
    .toBe('${nodes.alt_extract.result_set_id}')
  expect(body.spec.edges).toContainEqual({
    source: 'alt_extract', target: 'export', trigger: 'success', when: null, is_default: false,
  })
  expect(body.spec.edges).not.toContainEqual({
    source: 'extract', target: 'export', trigger: 'success', when: null, is_default: false,
  })
})

test('Export source supports dotted SQL node IDs without weakening edge validation', async ({
  page,
}) => {
  const state = await mockWorkflowPage(page)
  await openExistingEditor(page)
  await page.getByRole('button', { name: 'Advanced DAG JSON' }).click()
  const editor = page.getByTestId('advanced-json')
  const parsed = JSON.parse(await editor.inputValue())
  parsed.nodes[0].id = 'extract.v1'
  parsed.nodes[2].payload.source_result_set_id = '${nodes.extract.v1.result_set_id}'
  parsed.edges = parsed.edges.map((edge: any) =>
    edge.source === 'extract' ? { ...edge, source: 'extract.v1' } : edge,
  )
  await editor.fill(JSON.stringify(parsed, null, 2))
  await page.getByRole('button', { name: 'Form' }).click()

  await expect(page.getByTestId('export-source-2')).toHaveValue('extract.v1')
  await page.getByRole('button', { name: 'Save changes' }).click()

  expect(state.workflowWrites).toHaveLength(1)
  const body = state.workflowWrites[0] as Record<string, any>
  expect(body.spec.nodes[2].payload.source_result_set_id)
    .toBe('${nodes.extract.v1.result_set_id}')
  expect(body.spec.edges).toContainEqual({
    source: 'extract.v1', target: 'export', trigger: 'success', when: null, is_default: false,
  })
})

test('clearing Export source removes its generated edge and blocks save', async ({ page }) => {
  const state = await mockWorkflowPage(page)
  await openExistingEditor(page)

  await page.getByTestId('export-source-2').selectOption('')
  await page.getByRole('button', { name: 'Advanced DAG JSON' }).click()
  const parsed = JSON.parse(await page.getByTestId('advanced-json').inputValue())
  expect(parsed.edges).not.toContainEqual({
    source: 'extract', target: 'export', trigger: 'success', when: null, is_default: false,
  })
  await page.getByRole('button', { name: 'Form' }).click()
  await page.getByRole('button', { name: 'Save changes' }).click()

  expect(state.workflowWrites).toEqual([])
})

test('existing workflow Notify selects configured targets', async ({ page }) => {
  const state = await mockWorkflowPage(page)
  await openExistingEditor(page)

  await page.getByTestId('notify-target-notify-target-hook').check()
  await page.getByRole('button', { name: 'Save changes' }).click()

  const notify = (state.workflowWrites[0] as Record<string, any>).spec.nodes.find(
    (node: any) => node.id === 'notify',
  )
  expect(notify.payload.target_ids).toEqual(['target-email', 'target-hook'])
})

test('Email target submits write-only password and never renders it or secret refs', async ({
  page,
}) => {
  const consoleErrors = trackConsoleErrors(page)
  const state = await mockWorkflowPage(page)
  await page.goto('/projects/project-1/workflows')
  await page.getByRole('button', { name: 'Notifications' }).click()
  await page.getByRole('button', { name: 'Add notification' }).click()
  await page.getByLabel('Channel').selectOption('email')
  await page.getByLabel('SMTP host').fill('smtp.example.invalid')
  await page.getByLabel('SMTP port').fill('587')
  await page.getByLabel('From').fill('robot@example.invalid')
  await page.getByLabel('Recipients').fill('ops@example.invalid, dba@example.invalid')
  await page.getByLabel('SMTP user').fill('robot')
  await page.getByLabel('SMTP password').fill('ephemeral-value')
  await page.getByLabel('failed').check()
  await page.getByRole('button', { name: 'Save' }).click()

  expect(state.notificationWrites).toHaveLength(1)
  expect(state.notificationWrites[0]).toMatchObject({
    channel: 'email',
    smtp_host: 'smtp.example.invalid',
    smtp_port: 587,
    smtp_from: 'robot@example.invalid',
    smtp_to: ['ops@example.invalid', 'dba@example.invalid'],
    smtp_user: 'robot',
    smtp_password: 'ephemeral-value',
    events: ['failed'],
  })
  await expect(page.locator('body')).not.toContainText('ephemeral-value')
  await expect(page.locator('body')).not.toContainText('internal-password-ref-must-not-render')
  await expect(page.locator('body')).not.toContainText('internal-url-ref-must-not-render')
  expect(consoleErrors).toEqual([])
})

test('run history paginates by offset and renders safe scalar outputs', async ({ page }) => {
  const consoleErrors = trackConsoleErrors(page)
  const state = await mockWorkflowPage(page)
  await page.goto('/projects/project-1/workflows')
  await page.getByRole('button', { name: 'Run history' }).click()
  await page.getByTestId('workflow-run-run-1').click()

  await expect(page.getByText('sent_count')).toBeVisible()
  await expect(page.getByText('2', { exact: true })).toBeVisible()
  await expect(page.getByText('delivered')).toBeVisible()
  await page.getByRole('button', { name: 'Next page' }).click()
  await expect(page.getByTestId('workflow-run-run-21')).toBeVisible()
  await page.getByRole('button', { name: 'Previous page' }).click()

  expect(state.runOffsets).toContain(0)
  expect(state.runOffsets).toContain(20)
  expect(consoleErrors).toEqual([])
})
