# AI Metadata Error and Target Expression Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Localize AI schema-probe failures with actionable guidance and expose target-side `RESULT_n` SQL expressions in the compare-column editor.

**Architecture:** Keep AI generation fail-closed when real metadata is unavailable, but audit the structured failure before returning the existing 503 error. Reuse the existing projection-detail model and `CompareExpressionLabel` component for the target side, matching details first by target name and then by projection position.

**Tech Stack:** Python 3.12, FastAPI, pytest, Vue 3, TypeScript, vue-i18n, Playwright, Ruff, mypy.

## Global Constraints

- Do not generate SQL without real schema metadata.
- Do not log datasource connection details, credentials, schema payloads, prompts, or SQL.
- Do not change metadata-cache policy, AI egress rules, SQL execution, dependencies, or database schema.
- Preserve behavior for ordinary columns and responses without projection details.
- Git commits use the configured `answer` identity.

---

### Task 1: Audit AI metadata-probe failures

**Files:**
- Modify: `tests/contract/test_api.py`
- Modify: `app/api/routes/core.py:1482`

**Interfaces:**
- Consumes: `ApiError(status_code: int, code: str, message: str)` and the local `audit(result, detail)` closure.
- Produces: an `ai_copilot_run` audit with `result="failed"` and `detail={"error": "metadata_probe_failed"}` before the original 503 is re-raised.

- [ ] **Step 1: Write the failing contract test**

Add a test beside `test_ai_sql_generate_uses_l2_schema_context_and_audits`:

```python
def test_ai_sql_generate_metadata_failure_is_audited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        core_routes,
        "build_database_adapter",
        lambda conn_info, secret_store, **kwargs: _FailingMetadataAdapter(),
    )
    engine = _FakeEngine(
        [
            _datasource_row(),
            {"id": "project-1"},
            _ai_config_row(),
            None,
        ]
    )
    services = _Services(engine)
    app = create_app(services=cast(ApiServices, services))

    response = AsgiClient(app).post(
        "/api/datasources/ds-1/ai/sql-generate",
        headers=_auth_headers(),
        json_body={"natural_language": "list rows"},
    )

    assert response.status_code == 503
    assert response.json()["error"] == "metadata_probe_failed"
    audit = next(a for a in services.audits if a["action"] == "ai_copilot_run")
    assert audit["result"] == "failed"
    assert audit["detail"] == {"error": "metadata_probe_failed"}
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```powershell
& 'D:\myproject\dataops-studio-v2\runtime\uv\uv.exe' run pytest tests/contract/test_api.py::test_ai_sql_generate_metadata_failure_is_audited -q
```

Expected: FAIL because no `ai_copilot_run` audit exists when `_schema_tables_for_ai` raises.

- [ ] **Step 3: Add the minimal audited re-raise**

Wrap only the schema-context call in `generate_sql_from_nl`:

```python
try:
    tables, more_tables = _schema_tables_for_ai(services, row, body)
except ApiError as exc:
    audit("failed", {"error": exc.code})
    raise
```

Continue building schema payload and invoking the gateway only after that block.

- [ ] **Step 4: Run the focused contract tests and verify GREEN**

Run:

```powershell
& 'D:\myproject\dataops-studio-v2\runtime\uv\uv.exe' run pytest tests/contract/test_api.py::test_ai_sql_generate_metadata_failure_is_audited tests/contract/test_api.py::test_ai_sql_generate_uses_l2_schema_context_and_audits tests/contract/test_api.py::test_ai_sql_generate_disabled_returns_structured_409 -q
```

Expected: `3 passed`.

- [ ] **Step 5: Commit Task 1**

```powershell
git add app/api/routes/core.py tests/contract/test_api.py
git commit -m "fix(ai): audit metadata probe failures"
```

### Task 2: Localize the AI schema-probe error

**Files:**
- Modify: `frontend/src/i18n/en.ts`
- Modify: `frontend/src/i18n/zh-CN.ts`
- Modify: `frontend/src/views/SqlWorkspaceView.vue:1057`
- Modify: `frontend/tests/sql-workspace-w2.spec.ts`

**Interfaces:**
- Consumes: `ApiError.code === "metadata_probe_failed"` from `frontend/src/api/client.ts`.
- Produces: i18n key `sql.ai_metadata_unavailable` and an actionable localized error in the AI modal.

- [ ] **Step 1: Write the failing Playwright test**

Add this case to `frontend/tests/sql-workspace-w2.spec.ts`:

```typescript
test('AI SQL metadata failure shows actionable localized guidance', async ({ page }) => {
  await mockBase(page)
  await page.route('**/api/datasources/ds-1/ai/sql-generate', (r) =>
    json(r, 503, {
      error: 'metadata_probe_failed',
      message: 'Datasource metadata probe failed',
    }),
  )

  await page.goto('/projects/project-1/sql')
  await page.getByRole('button', { name: 'AI Generate' }).click()
  await page.getByRole('textbox', { name: 'What do you want to query?' }).fill('list customers')
  await page.getByRole('button', { name: 'AI Generate', exact: true }).last().click()

  await expect(page.getByText(/test the datasource connection and refresh metadata/i)).toBeVisible()
  await expect(page.getByText('Datasource metadata probe failed')).toHaveCount(0)
  expectNoConsoleErrors()
})
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```powershell
npm run test:e2e -- sql-workspace-w2.spec.ts --grep "AI SQL metadata failure"
```

Expected: FAIL because the modal displays `Datasource metadata probe failed`.

- [ ] **Step 3: Add the translations**

Add under the existing `sql.ai_failed` entries:

```typescript
// en.ts
ai_metadata_unavailable:
  'Unable to read the datasource schema. Test the datasource connection and refresh metadata, then try again.',

// zh-CN.ts
ai_metadata_unavailable: '无法读取数据源结构，请先测试数据源连接并刷新元数据后重试。',
```

- [ ] **Step 4: Map the structured error code in the modal**

Extend `onGenerateSql` without changing other error branches:

```typescript
if (e instanceof ApiError && e.code === 'ai_disabled') {
  aiDisabled.value = true
} else if (e instanceof ApiError && e.code === 'metadata_probe_failed') {
  aiError.value = t('sql.ai_metadata_unavailable')
} else {
  aiError.value = errorMessage(e)
}
```

- [ ] **Step 5: Run the focused Playwright test and typecheck**

Run:

```powershell
npm run test:e2e -- sql-workspace-w2.spec.ts --grep "AI SQL metadata failure"
npm run typecheck
```

Expected: Playwright case passes and typecheck exits 0.

- [ ] **Step 6: Commit Task 2**

```powershell
git add frontend/src/i18n/en.ts frontend/src/i18n/zh-CN.ts frontend/src/views/SqlWorkspaceView.vue frontend/tests/sql-workspace-w2.spec.ts
git commit -m "fix(ai): localize metadata probe guidance"
```

### Task 3: Show target-side generated expressions

**Files:**
- Modify: `frontend/src/views/CompareView.vue:386-397`
- Modify: `frontend/src/views/CompareView.vue:2728-2737`
- Modify: `frontend/tests/compare.spec.ts`

**Interfaces:**
- Consumes: `activeTask.target_projection_details: CompareProjectionDetail[]` and `CompareExpressionLabel`.
- Produces: `targetColumnDetail(index: number, name: string): CompareProjectionDetail | null` and a target-side expression button.

- [ ] **Step 1: Write the failing Playwright test**

Add a compare test whose source and target expressions differ:

```typescript
test('target generated aliases expose the target SQL expression', async ({ page }) => {
  const task = compareTask({
    columns: [{ name: 'RESULT_1', type: 'decimal' }],
    source_projection_details: [
      { name: 'RESULT_1', generated: true, projection_index: 1, expression: 'SUM(SOURCE_AMOUNT)' },
    ],
    target_projection_details: [
      { name: 'RESULT_1', generated: true, projection_index: 1, expression: 'SUM(TARGET_FARE)' },
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
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```powershell
npm run test:e2e -- compare.spec.ts --grep "target generated aliases"
```

Expected: FAIL because only the source-side expression button exists.

- [ ] **Step 3: Implement target detail lookup**

Add beside `sourceColumnDetail`:

```typescript
function targetColumnDetail(index: number, name: string): CompareProjectionDetail | null {
  const details = activeTask.value?.target_projection_details
  return (
    detailByName(details, name) ??
    details?.find((item) => item.generated && item.projection_index === index + 1) ??
    null
  )
}
```

- [ ] **Step 4: Render the target expression label**

Under the target input in the same table cell, add:

```vue
<div v-if="targetColumnDetail(ci, targetColName(col.name))?.generated" class="mt-1 text-[10px]">
  <CompareExpressionLabel
    :name="targetColumnDetail(ci, targetColName(col.name))?.name ?? targetColName(col.name)"
    :detail="targetColumnDetail(ci, targetColName(col.name))"
  />
</div>
```

- [ ] **Step 5: Add the legacy-response assertion**

Add this separate case:

```typescript
test('missing target projection details keeps the compare editor stable', async ({ page }) => {
  const task = compareTask({
    columns: [{ name: 'RESULT_1', type: 'decimal' }],
    source_projection_details: [
      { name: 'RESULT_1', generated: true, projection_index: 1, expression: 'SUM(SOURCE_AMOUNT)' },
    ],
    target_projection_details: [],
  })
  await mockBase(page)
  await page.route(/\/api\/compare\/tasks(\?|$)/, (r) => json(r, 200, [task]))

  await page.goto('/projects/project-1/compare')
  await expect(page.getByRole('button', { name: 'Inspect expression for RESULT_1' })).toHaveCount(1)
  await expect(page.locator('input[placeholder="Column"]').first()).toHaveValue('RESULT_1')
  expectNoConsoleErrors()
})
```

- [ ] **Step 6: Run compare Playwright tests and typecheck**

Run:

```powershell
npm run test:e2e -- compare.spec.ts
npm run typecheck
```

Expected: all compare cases pass and typecheck exits 0.

- [ ] **Step 7: Commit Task 3**

```powershell
git add frontend/src/views/CompareView.vue frontend/tests/compare.spec.ts
git commit -m "fix(compare): expose target generated expressions"
```

### Task 4: Full verification and PR handoff

**Files:**
- Verify only; no production file changes expected.

**Interfaces:**
- Consumes: all changes from Tasks 1-3.
- Produces: clean local verification evidence and a pushed branch ready for CI.

- [ ] **Step 1: Run backend quality gates**

```powershell
& 'D:\myproject\dataops-studio-v2\runtime\uv\uv.exe' run ruff check app tests tools
& 'D:\myproject\dataops-studio-v2\runtime\uv\uv.exe' run ruff format --check app tests tools
& 'D:\myproject\dataops-studio-v2\runtime\uv\uv.exe' run mypy app tests
& 'D:\myproject\dataops-studio-v2\runtime\uv\uv.exe' run pytest -q
```

Expected: all commands exit 0; pytest reports zero unexpected failures.

- [ ] **Step 2: Run frontend quality gates**

```powershell
Push-Location frontend
npm run typecheck
npm run build
npm run test:e2e -- sql-workspace-w2.spec.ts compare.spec.ts
Pop-Location
```

Expected: commands exit 0. Record any pre-existing build warnings separately; do not call warning output pristine.

- [ ] **Step 3: Perform safety and diff review**

```powershell
git diff origin/main...HEAD
git status --short
git diff origin/main...HEAD --unified=0 | Select-String -Pattern '(password|token|secret|private_key|api_key|cookie|session)' -CaseSensitive:$false
```

Expected: only planned files are changed; no credentials, logs, screenshots, dumps, `.env`, or temporary files are tracked.

- [ ] **Step 4: Push and open a Draft PR**

```powershell
git push -u origin agent/fix-ai-metadata-and-target-expression
```

Create a Draft PR targeting `main`, include root causes, local verification, the fail-closed AI decision, and any unverified live-DB2 limitation.

- [ ] **Step 5: Wait for CI**

```powershell
$prNumber = gh pr view --json number --jq .number
gh pr checks $prNumber --repo allen-answer/dataOpsStudio_v2 --watch --interval 10
```

Expected: every required check is `pass`. Do not merge or deploy to D while any check is pending or failing.
