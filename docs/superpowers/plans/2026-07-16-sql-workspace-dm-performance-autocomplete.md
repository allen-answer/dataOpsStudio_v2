# SQL Workspace DM Responsiveness and Autocomplete Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep large DM query results responsive in the browser and add schema-qualified table completion while preserving complete backend execution and Monaco SQL highlighting.

**Architecture:** Keep the existing DM adapter, worker spool, result API, and Monaco editor. Bound progressive result responses and DOM rendering to 100 rows at a one-second poll cadence, then register a component-scoped Monaco completion provider backed by the existing metadata tables endpoint and a datasource/schema request cache.

**Tech Stack:** Vue 3 Composition API, TypeScript, Monaco Editor 0.52, Playwright, FastAPI/Python adapter tests.

## Global Constraints

- Do not rewrite user SQL, inject a `LIMIT`, or silently truncate the backend result.
- Do not add or change backend API contracts, database schemas, dependencies, or migrations.
- Do not log SQL text, result rows, datasource credentials, API keys, tokens, or personal data.
- Keep changes limited to the SQL workspace view and its existing Playwright tests.
- The D drive is the runtime deployment; the E drive repository is the source of truth.
- Real DM/browser acceptance cannot be claimed without an authenticated browser session and an available DM datasource.

## File Map

- Modify `frontend/src/views/SqlWorkspaceView.vue`: reduce result load, register/dispose Monaco completion provider, and cache table metadata.
- Modify `frontend/tests/sql-workspace.spec.ts`: prove progressive result requests use 100-row pages and stop after success.
- Modify `frontend/tests/sql-workspace-w2.spec.ts`: prove schema-qualified completion, stale datasource isolation, and SQL token highlighting.
- Reference `frontend/src/api/metadata.ts`: reuse `listMetadataTables`; no interface changes.
- Reference `app/dbclients/dm_adapter.py`: retain existing `fetchmany` streaming; no production changes.

---

### Task 1: Bound progressive result requests and DOM work

**Files:**
- Modify: `frontend/tests/sql-workspace.spec.ts:90-190`
- Modify: `frontend/src/views/SqlWorkspaceView.vue:114-115`

**Interfaces:**
- Consumes: `getJobResult(jobId: string, offset: number, limit: number)`.
- Produces: SQL result requests with `limit=100` at a 1,000 ms cadence; existing `ResultTable` receives at most 100 rows per page.

- [ ] **Step 1: Write the failing progressive-result regression**

In `mockWorkspace`, record result request URLs and return 100 synthetic rows without including their values in logs:

```ts
async function mockWorkspace(
  page: Page,
  options: {
    datasource?: Record<string, unknown>
    jobCreatedAt?: string
    jobFinishedAt?: string
    progressiveRows?: number
  } = {},
): Promise<{
  patches: unknown[]
  renders: unknown[]
  resultRequestUrls: string[]
  getJobReads: () => number
}> {
  const patches: unknown[] = []
  const renders: unknown[] = []
  const resultRequestUrls: string[] = []
  let jobReads = 0
  // keep the existing license, datasource, console, history, and template routes

  await page.route(/\/api\/jobs\/job-1\/result\?/, (route) => {
    resultRequestUrls.push(route.request().url())
    if (options.progressiveRows === undefined) {
      return json(route, 200, {
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
    }
    const rowCount = options.progressiveRows
    return json(route, 200, {
      job_id: 'job-1',
      result_set_id: 'rs-1',
      offset: 0,
      limit: 100,
      columns: [
        { name: 'id', type: 'integer', driver_type: 'INT', nullable: false, primary_key: true },
      ],
      rows: Array.from({ length: rowCount }, (_, index) => ({ values: [index + 1] })),
      loaded_rows: rowCount,
      total_rows: null,
      state: jobReads <= 1 ? 'running' : 'success',
      truncated: false,
    })
  })

  return { patches, renders, resultRequestUrls, getJobReads: () => jobReads }
}
```

Add a focused test:

```ts
test('progressive results use bounded pages and polling stops at success', async ({ page }) => {
  const state = await mockWorkspace(page, { progressiveRows: 100 })

  await page.goto('/projects/project-1/sql')
  await page.getByRole('button', { name: 'Run' }).click()
  await expect(page.getByText(/loaded 100 rows/)).toBeVisible()
  await expect.poll(() => state.resultRequestUrls.length).toBeGreaterThan(0)

  expect(state.resultRequestUrls.every((url) => new URL(url).searchParams.get('limit') === '100')).toBe(true)
  await expect(page.locator('tbody tr')).toHaveCount(100)

  const terminalJobReads = state.getJobReads()
  await page.waitForTimeout(1_200)
  expect(state.getJobReads()).toBe(terminalJobReads)
  expectNoConsoleErrors()
})
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```powershell
cd frontend
npm run test:e2e -- tests/sql-workspace.spec.ts --grep "progressive results use bounded pages"
```

Expected: FAIL because the request contains `limit=1000` and the current poll cadence can perform an additional read inside the 1.2-second observation window.

- [ ] **Step 3: Make the minimal production change**

Change only the two constants in `SqlWorkspaceView.vue`:

```ts
const PAGE_SIZE = 100
const POLL_MS = 1000
```

Do not change the API limit, ResultStore, worker batch size, or `ResultTable` pagination contract.

- [ ] **Step 4: Run the focused test and related SQL workspace test**

Run:

```powershell
cd frontend
npm run test:e2e -- tests/sql-workspace.spec.ts
```

Expected: all tests in `sql-workspace.spec.ts` PASS and Console error assertions remain empty.

- [ ] **Step 5: Commit the performance regression and fix**

```powershell
git add frontend/tests/sql-workspace.spec.ts frontend/src/views/SqlWorkspaceView.vue
git commit -m "fix(sql):bound-progressive-result-rendering"
```

---

### Task 2: Add schema-qualified Monaco table completion

**Files:**
- Modify: `frontend/tests/sql-workspace-w2.spec.ts:1-150`
- Modify: `frontend/src/views/SqlWorkspaceView.vue:190-245, 887-925, 1203-1207`

**Interfaces:**
- Consumes: `listMetadataTables(datasourceId: string, schema: string, refresh?: boolean): Promise<MetadataTableItem[]>`.
- Produces: `registerSqlCompletionProvider(): void`, a component-scoped provider that returns `monaco.languages.CompletionList` for `schema.tableFragment` input.

- [ ] **Step 1: Write the failing schema completion E2E**

Add this test to `sql-workspace-w2.spec.ts` using only synthetic schema/table names:

```ts
test('typing schema dot suggests tables from the selected datasource', async ({ page }) => {
  await mockBase(page)
  const tableRequests: string[] = []
  await page.route(/\/api\/datasources\/ds-1\/metadata\/tables/, (route) => {
    tableRequests.push(route.request().url())
    return json(route, 200, [
      { schema_name: 'app', name: 'customers', table_type: 'BASE TABLE' },
      { schema_name: 'app', name: 'orders', table_type: 'BASE TABLE' },
    ])
  })

  await page.goto('/projects/project-1/sql')
  const input = page.locator('.monaco-editor textarea.inputarea')
  await input.press('Control+End')
  await input.press('Control+A')
  await input.type('SELECT * FROM app.')

  await expect(page.locator('.suggest-widget')).toBeVisible()
  await expect(page.locator('.suggest-widget').getByText('customers', { exact: true })).toBeVisible()
  await expect(page.locator('.suggest-widget').getByText('orders', { exact: true })).toBeVisible()
  expect(tableRequests).toHaveLength(1)
  expect(new URL(tableRequests[0]).searchParams.get('schema')).toBe('app')

  await input.press('Escape')
  await input.press('Control+A')
  await input.type('SELECT * FROM "app".')
  await expect(page.locator('.suggest-widget').getByText('customers', { exact: true })).toBeVisible()
  expect(tableRequests).toHaveLength(1)
  expectNoConsoleErrors()
})
```

- [ ] **Step 2: Run the completion test and verify RED**

Run:

```powershell
cd frontend
npm run test:e2e -- tests/sql-workspace-w2.spec.ts --grep "typing schema dot suggests tables"
```

Expected: FAIL because no SQL completion provider exists and the tables metadata route is never called.

- [ ] **Step 3: Add component-scoped cache and identifier helpers**

Add these declarations near the other runtime maps in `SqlWorkspaceView.vue`:

```ts
const completionTableRequests = new Map<string, Promise<MetadataTableItem[]>>()
let sqlCompletionProvider: monaco.IDisposable | null = null

const SIMPLE_SQL_IDENTIFIER = /^[A-Za-z_][A-Za-z0-9_$#]*$/
const SCHEMA_COMPLETION_PREFIX = /(?:"((?:[^"]|"")*)"|([A-Za-z_][A-Za-z0-9_$#]*))\.([A-Za-z_][A-Za-z0-9_$#]*)?$/

function completionCacheKey(datasourceId: string, schemaName: string): string {
  return `${datasourceId}\u0000${schemaName}`
}

function completionInsertText(tableName: string): string {
  return SIMPLE_SQL_IDENTIFIER.test(tableName)
    ? tableName
    : `"${tableName.replaceAll('"', '""')}"`
}

function completionTables(datasourceId: string, schemaName: string): Promise<MetadataTableItem[]> {
  const key = completionCacheKey(datasourceId, schemaName)
  const cached = completionTableRequests.get(key)
  if (cached) return cached
  const request = listMetadataTables(datasourceId, schemaName, false).catch(() => [])
  completionTableRequests.set(key, request)
  return request
}

function clearCompletionTables(datasourceId: string): void {
  const prefix = `${datasourceId}\u0000`
  for (const key of completionTableRequests.keys()) {
    if (key.startsWith(prefix)) completionTableRequests.delete(key)
  }
}
```

- [ ] **Step 4: Register the provider and dispose it with the view**

Add the provider implementation before `onEditorMount`:

```ts
function registerSqlCompletionProvider(): void {
  sqlCompletionProvider?.dispose()
  sqlCompletionProvider = monaco.languages.registerCompletionItemProvider('sql', {
    triggerCharacters: ['.'],
    async provideCompletionItems(model, position) {
      const linePrefix = model
        .getLineContent(position.lineNumber)
        .slice(0, position.column - 1)
      const match = SCHEMA_COMPLETION_PREFIX.exec(linePrefix)
      const datasource = selectedDs.value
      if (!match || !datasource || !SUPPORTED_TOOL_DB_TYPES.has(datasource.db_type)) {
        return { suggestions: [] }
      }

      const datasourceId = datasource.id
      const schemaName = (match[1] ?? match[2] ?? '').replaceAll('""', '"')
      const fragment = match[3] ?? ''
      const tables = await completionTables(datasourceId, schemaName)
      if (selectedDsId.value !== datasourceId) return { suggestions: [] }

      const range = new monaco.Range(
        position.lineNumber,
        position.column - fragment.length,
        position.lineNumber,
        position.column,
      )
      return {
        suggestions: tables.map((table) => ({
          label: table.name,
          kind: monaco.languages.CompletionItemKind.Class,
          insertText: completionInsertText(table.name),
          filterText: table.name,
          sortText: table.name.toLocaleLowerCase(),
          detail: table.schema_name,
          range,
        })),
      }
    },
  })
}
```

Update mount/unmount hooks:

```ts
function onEditorMount(editor: monaco.editor.IStandaloneCodeEditor): void {
  registerSqlCompletionProvider()
  editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.Enter, () => {
    void onExecute()
  })
}

onUnmounted(() => {
  sqlCompletionProvider?.dispose()
  sqlCompletionProvider = null
  completionTableRequests.clear()
  // retain the existing timer cleanup
})
```

At the start of `loadMetadataSchemas(refresh)`, invalidate completion cache only for an explicit refresh:

```ts
if (refresh) clearCompletionTables(metadataDsId.value)
```

- [ ] **Step 5: Run the completion test and verify GREEN**

Run:

```powershell
cd frontend
npm run test:e2e -- tests/sql-workspace-w2.spec.ts --grep "typing schema dot suggests tables"
```

Expected: PASS; quoted and unquoted `app.` inputs both show table suggestions, the second completion reuses the first request result, one request is made with `schema=app`, and Console errors are empty.

- [ ] **Step 6: Commit the completion feature**

```powershell
git add frontend/tests/sql-workspace-w2.spec.ts frontend/src/views/SqlWorkspaceView.vue
git commit -m "feat(sql):complete-schema-qualified-tables"
```

---

### Task 3: Guard stale completions and SQL highlighting

**Files:**
- Modify: `frontend/tests/sql-workspace-w2.spec.ts:80-150, 389-425`
- Modify only if the stale test exposes a defect: `frontend/src/views/SqlWorkspaceView.vue` provider guard from Task 2.

**Interfaces:**
- Consumes: the Task 2 completion provider and datasource-ID post-await guard.
- Produces: regression evidence that old datasource metadata cannot appear after a switch and Monaco remains configured for SQL tokenization.

- [ ] **Step 1: Add the stale datasource regression**

Add a test with two synthetic datasources and a delayed first request:

```ts
test('completion drops tables returned after the datasource changes', async ({ page }) => {
  await mockLicense(page)
  await page.route(/\/api\/datasources\?/, (route) =>
    json(route, 200, [datasource(), datasource({ id: 'ds-2', name: 'reporting' })]),
  )
  await page.route('**/api/sql/consoles', (route) =>
    json(route, 200, [consoleRow()]),
  )

  let releaseFirst: (() => void) | null = null
  const firstStarted = new Promise<void>((resolveStarted) => {
    void page.route(/\/api\/datasources\/ds-1\/metadata\/tables/, async (route) => {
      resolveStarted()
      await new Promise<void>((resolve) => { releaseFirst = resolve })
      return json(route, 200, [{ schema_name: 'app', name: 'stale_table', table_type: 'BASE TABLE' }])
    })
  })
  await page.route(/\/api\/datasources\/ds-2\/metadata\/tables/, (route) =>
    json(route, 200, [{ schema_name: 'app', name: 'current_table', table_type: 'BASE TABLE' }]),
  )

  await page.goto('/projects/project-1/sql')
  const input = page.locator('.monaco-editor textarea.inputarea')
  await input.press('Control+A')
  await input.type('SELECT * FROM app.')
  await firstStarted

  await page.locator('main select').selectOption('ds-2')
  releaseFirst?.()
  await input.press('Control+A')
  await input.type('SELECT * FROM app.')

  await expect(page.locator('.suggest-widget').getByText('current_table', { exact: true })).toBeVisible()
  await expect(page.locator('.suggest-widget').getByText('stale_table', { exact: true })).toHaveCount(0)
  expectNoConsoleErrors()
})
```

- [ ] **Step 2: Add the SQL highlighting regression**

Add a focused test using synthetic SQL:

```ts
test('Monaco tokenizes SQL keywords and identifiers with distinct styles', async ({ page }) => {
  await mockLicense(page)
  await page.route(/\/api\/datasources\?/, (route) => json(route, 200, [datasource()]))
  await page.route('**/api/sql/consoles', (route) =>
    json(route, 200, [consoleRow({ sql: 'SELECT customer_id FROM app.customers' })]),
  )

  await page.goto('/projects/project-1/sql')
  await expect(page.locator('.monaco-editor .view-lines')).toContainText('SELECT')
  const tokens = await page.locator('.monaco-editor .view-lines').evaluate((root) =>
    Array.from(root.querySelectorAll('span[class*="mtk"]'))
      .map((node) => ({ text: node.textContent ?? '', className: node.className }))
      .filter((token) => token.text.trim().length > 0),
  )

  expect(tokens.some((token) => token.text.includes('SELECT'))).toBe(true)
  expect(new Set(tokens.map((token) => token.className)).size).toBeGreaterThan(1)
  expectNoConsoleErrors()
})
```

- [ ] **Step 3: Run both guard tests**

Run:

```powershell
cd frontend
npm run test:e2e -- tests/sql-workspace-w2.spec.ts --grep "completion drops|Monaco tokenizes"
```

Expected: both tests PASS. If the stale test fails, retain the datasource ID captured before `await` and return `{ suggestions: [] }` when it no longer equals `selectedDsId.value`; do not add cancellation infrastructure.

- [ ] **Step 4: Run all SQL workspace E2E tests**

Run:

```powershell
cd frontend
npm run test:e2e -- tests/sql-workspace.spec.ts tests/sql-workspace-w2.spec.ts tests/sql-result-tabs.spec.ts
```

Expected: all SQL workspace tests PASS and no captured Console error is present.

- [ ] **Step 5: Commit the guard tests**

```powershell
git add frontend/tests/sql-workspace-w2.spec.ts frontend/src/views/SqlWorkspaceView.vue
git commit -m "test(sql):guard-completion-and-highlighting"
```

---

### Task 4: Full local gates and delivery evidence

**Files:**
- Review only: all branch changes since `origin/main`.
- Modify only when a command identifies a task-related defect; add a focused regression before fixing it.

**Interfaces:**
- Consumes: Tasks 1-3.
- Produces: verified branch suitable for push and Draft PR; later deployment evidence for D drive runtime.

- [ ] **Step 1: Run frontend static and build gates**

```powershell
cd frontend
npm run typecheck
npm run build
```

Expected: both commands exit 0.

- [ ] **Step 2: Run the complete frontend E2E suite**

```powershell
cd frontend
npm run test:e2e
```

Expected: all Playwright tests PASS. Existing unrelated warnings are reported but not refactored.

- [ ] **Step 3: Run relevant backend and DM tests**

From the repository root:

```powershell
uv run pytest tests/unit/test_dm_adapter.py tests/unit/test_worker.py tests/unit/test_api_routes.py -q
uv run pytest tests/integration/test_dm_real_instance.py -q
```

Expected: unit tests PASS. The real-DM integration test either PASSes with configured DM test environment or SKIPs explicitly when that environment is unavailable.

- [ ] **Step 4: Review scope and secrets before publication**

```powershell
git status --short
git diff --check origin/main...HEAD
git diff --stat origin/main...HEAD
git diff origin/main...HEAD -- frontend/src/views/SqlWorkspaceView.vue frontend/tests/sql-workspace.spec.ts frontend/tests/sql-workspace-w2.spec.ts docs/superpowers
```

Verify that no `.env`, key, log, dump, screenshot, SQL result, credential, token, cookie, session, or personal-data file is tracked. `gitleaks` is not installed on this workstation; report that fact and use the repository's CI secret scan when the branch is pushed.

- [ ] **Step 5: Publish only after every available local gate passes**

Use the repository's established GitHub workflow to push the branch and open a Draft PR. Monitor every GitHub check to completion. Fix only explicit task-related blockers, add a focused regression, and rerun the failed equivalent locally before pushing again.

- [ ] **Step 6: Deploy only after PR merge**

Synchronize the merged source to `D:\myproject\dataops-studio-v2` without creating a D-drive backup, rebuild the frontend, and restart the existing launcher/API/worker through the repository deployment scripts. Do not delete or overwrite runtime data.

- [ ] **Step 7: Perform real runtime acceptance**

Verify:

- `/healthz` returns success.
- launcher, API, and worker processes are healthy.
- A bounded, read-only DM query produces result requests with `limit=100` and the page stays interactive while rows continue loading.
- Later pages are available without query rewriting or truncation.
- Typing a real schema followed by `.` shows its tables.
- Monaco visibly highlights SQL keywords.
- Browser Console has no red errors and Network has no unexpected 4xx/5xx responses.

Report only sanitized counts, timings, statuses, and error summaries. If browser authentication or a DM datasource is unavailable, explicitly mark real runtime acceptance as not executed.
