# SQL Compare Generated Expression Aliases Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically give unaliased SQL expressions stable `RESULT_n` names, expose their original expressions through an accessible UI, repair legacy numeric compare columns, and show failed runs as failures instead of empty results.

**Architecture:** Add one pure `app/domain/compare_sql.py` planner that parses the outer `SELECT`, assigns collision-free generated aliases, and returns execution SQL plus projection metadata. API preview/task responses and the Worker consume the same planner; Vue consumes additive metadata through a reusable label component and uses the existing task PATCH route for confirmed legacy repair.

**Tech Stack:** Python 3.12, sqlglot, FastAPI/Pydantic v2, SQLAlchemy Core, pytest, Vue 3, TypeScript, vue-i18n, lucide-vue-next, Playwright.

## Global Constraints

- Work only in `E:\work\dataops-studio-v2-sql-compare-fix` on `agent/fix-sql-compare-generated-alias`.
- Persist the user's original SQL unchanged; normalized SQL exists only in memory for execution.
- Generated aliases are uppercase `RESULT_n`; explicit aliases and simple columns win, with case-insensitive collision checks.
- Never log or commit real SQL, expression text, row data, datasource addresses, credentials, driver messages, tokens, cookies or sessions.
- Do not add a database migration or dependency.
- DB2 has no sqlglot dialect in the lockfile. Use generic parsing; preserve the legacy path for unsupported DB2 syntax with usable explicit aliases.
- Failed, timed-out or cancelled runs must never look like successful four-zero-bucket comparisons.
- Real DB2 preview/run remains unverified while the datasource is unavailable.
- Use red-green-refactor and commit each task as `answer` with `Co-Authored-By: OpenAI Codex <codex@openai.com>`.

---

### Task 1: Pure SQL Projection Planner

**Files:**
- Create: `app/domain/compare_sql.py`
- Create: `tests/unit/test_compare_sql.py`

**Interfaces:**
- Produces `CompareSqlProjection`, `CompareSqlPlan`, `CompareSqlProjectionError`.
- Produces `inspect_compare_sql(sql: str) -> tuple[CompareSqlProjection, ...]`.
- Produces `normalize_compare_sql(sql: str, db_type: DbType) -> CompareSqlPlan`.
- Produces `legacy_generated_aliases(columns, projections) -> dict[str, str]`.

- [ ] **Step 1: Write failing planner tests**

Create `tests/unit/test_compare_sql.py` with these core tests and separate cases for `COALESCE`, cast, arithmetic, blank/invalid SQL and idempotent output:

```python
from app.domain.compare_sql import (
    CompareSqlProjectionError,
    inspect_compare_sql,
    legacy_generated_aliases,
    normalize_compare_sql,
)
from app.domain.datasource import DbType


def test_unaliased_sum_and_case_receive_stable_aliases() -> None:
    sql = """SELECT CUST_NO,
      CASE WHEN BALANCE > 0 THEN 'Y' ELSE 'N' END,
      SUM(AMOUNT)
    FROM ACCOUNT GROUP BY CUST_NO, CASE WHEN BALANCE > 0 THEN 'Y' ELSE 'N' END"""
    plan = normalize_compare_sql(sql, DbType.DB2)
    assert [item.name for item in plan.projections] == [
        "CUST_NO", "RESULT_1", "RESULT_2"
    ]
    assert [item.generated for item in plan.projections] == [False, True, True]
    assert "AS RESULT_1" in plan.sql.upper()
    assert plan.projections[1].projection_index == 2
    assert "CASE" in (plan.projections[1].expression or "").upper()


def test_explicit_alias_and_existing_result_name_win_case_insensitively() -> None:
    plan = normalize_compare_sql(
        "SELECT amount AS result_1, SUM(tax), price * quantity AS total FROM sales",
        DbType.MYSQL,
    )
    assert [item.name for item in plan.projections] == [
        "result_1", "RESULT_2", "total"
    ]
    assert [item.generated for item in plan.projections] == [False, True, False]


def test_cte_supported_but_union_requires_explicit_aliases() -> None:
    plan = normalize_compare_sql(
        "WITH x AS (SELECT amount FROM sales) SELECT SUM(amount) FROM x",
        DbType.DB2,
    )
    assert plan.projections[0].name == "RESULT_1"
    try:
        normalize_compare_sql(
            "SELECT SUM(a) FROM x UNION ALL SELECT SUM(a) FROM y", DbType.DB2
        )
    except CompareSqlProjectionError as exc:
        assert exc.code == "explicit_alias_required"
    else:
        raise AssertionError("set operations must not be rewritten")


def test_legacy_repairs_only_proven_numeric_driver_names() -> None:
    projections = inspect_compare_sql(
        "SELECT D, K1, K2, K3, C, SUM(A), SUM(B), SUM(C) "
        "FROM T GROUP BY D, K1, K2, K3, C"
    )
    assert legacy_generated_aliases(
        ["D", "K1", "K2", "K3", "C", "6", "7", "8"], projections
    ) == {"6": "RESULT_1", "7": "RESULT_2", "8": "RESULT_3"}
    assert legacy_generated_aliases(
        ["6"], inspect_compare_sql('SELECT 1 AS "6"')
    ) == {}
```

- [ ] **Step 2: Verify the red state**

Run `& 'E:\work\dataops-studio-v2-source\.venv\Scripts\python.exe' -m pytest tests/unit/test_compare_sql.py -q`.

Expected: collection fails because `app.domain.compare_sql` does not exist.

- [ ] **Step 3: Implement the public planner types and signatures**

Create `app/domain/compare_sql.py` with:

```python
class CompareSqlProjectionError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class CompareSqlProjection:
    name: str
    generated: bool
    projection_index: int
    expression: str | None = None


@dataclass(frozen=True)
class CompareSqlPlan:
    sql: str
    projections: tuple[CompareSqlProjection, ...]
    rewritten: bool
```

Parse one outer `exp.Select`; reject `exp.SetOperation` for automatic rewriting. First reserve case-folded simple-column and explicit-alias names. Leave bare columns and stars unchanged; alias other unaliased projections with the next free `RESULT_n`. Preserve a formatted pre-alias expression in metadata. Map MySQL→`mysql`, DM/Oracle→`oracle`, PostgreSQL→`postgres`, DB2→generic. `legacy_generated_aliases` maps only when a configured numeric name equals the one-based position of a proven generated projection.

- [ ] **Step 4: Verify planner quality**

Run:

```powershell
& 'E:\work\dataops-studio-v2-source\.venv\Scripts\python.exe' -m pytest tests/unit/test_compare_sql.py -q
& 'E:\work\dataops-studio-v2-source\.venv\Scripts\ruff.exe' format app/domain/compare_sql.py tests/unit/test_compare_sql.py
& 'E:\work\dataops-studio-v2-source\.venv\Scripts\ruff.exe' check app/domain/compare_sql.py tests/unit/test_compare_sql.py
& 'E:\work\dataops-studio-v2-source\.venv\Scripts\mypy.exe' app/domain/compare_sql.py
```

Expected: tests pass and all quality commands exit 0.

- [ ] **Step 5: Commit**

```powershell
git add app/domain/compare_sql.py tests/unit/test_compare_sql.py
git commit -m "feat(compare): plan stable aliases for SQL expressions" -m "Co-Authored-By: OpenAI Codex <codex@openai.com>"
```

---

### Task 2: API Metadata, Preview Normalization and Legacy Guard

**Files:**
- Modify: `app/api/schemas.py:210-390`
- Modify: `app/api/routes/core.py:2377-2460`
- Modify: `app/api/routes/core.py:2653-2750`
- Modify: `app/api/routes/core.py:2760-2895`
- Modify: `app/api/routes/core.py:3099-3280`
- Modify: `app/api/routes/core.py:6650-6670`
- Modify: `tests/contract/test_api.py:983-1080`
- Modify: `tests/contract/test_api.py:1420-1470`

**Interfaces:**
- Consumes Task 1 planner functions.
- Adds `CompareProjectionDetail` and optional projection-detail lists without removing `columns`.
- Adds HTTP 409 code `compare_sql_aliases_stale` and HTTP 400 code `compare_sql_alias_required`.

- [ ] **Step 1: Write failing API contracts**

Add a preview contract whose fake DB2 adapter captures SQL, emits `CUST_NO` and `RESULT_1`, and asserts:

```python
assert response.status_code == 200
assert "AS RESULT_1" in captured_sql[0].upper()
assert response.json()["columns"] == ["CUST_NO", "RESULT_1"]
assert response.json()["column_details"][1] == {
    "name": "RESULT_1",
    "generated": True,
    "projection_index": 2,
    "expression": "SUM(AMOUNT)",
}
```

Add contracts that task responses derive source/target projection details from stored SQL, persistence retains the original SQL, and running a task whose sixth proven generated projection is saved as `"6"` returns `409 compare_sql_aliases_stale` before enqueue. Add a control proving explicit `1 AS "1"` is allowed.

- [ ] **Step 2: Verify the API tests fail**

Run `pytest tests/contract/test_api.py -k "compare_preview or compare_task or compare_run" -q` through the shared E-source Python executable.

Expected: metadata assertions fail and the stale task still enqueues.

- [ ] **Step 3: Add the Pydantic contract**

Add:

```python
class CompareProjectionDetail(BaseModel):
    name: str
    generated: bool = False
    projection_index: int = Field(ge=1)
    expression: str | None = None
```

Add `source_projection_details` and `target_projection_details` with empty-list defaults to `CompareTaskResponse`. Add `column_details` with an empty-list default to `ComparePreviewResponse`.

- [ ] **Step 4: Normalize preview SQL and safely expose metadata**

For SQL refs, validate read-only SQL, call `normalize_compare_sql(inner_sql, db_type)`, and wrap `plan.sql` in `DATAOPS_PREVIEW`. If parsing is unsupported, execute the original SQL; after cursor metadata arrives, reject unsafe numeric driver names unless inspection proves an explicit numeric alias. Return `CompareProjectionDetail` values without logging expressions. Apply the same normalization before the SQL-ref `DATAOPS_PRECHECK` subquery so a generated key alias is usable by the existing primary-key health check.

Update `_compare_task_response` to inspect source and target SQL independently; unsupported dialect syntax returns empty details rather than making a readable task fail.

- [ ] **Step 5: Guard create/update/run without modifying SQL**

Add `_compare_ref_projections(ref: object) -> tuple[CompareSqlProjection, ...]`,
`_legacy_compare_aliases(columns: list[object], ref: object) -> dict[str, str]`, and
`_reject_stale_compare_aliases(columns: list[object], ref: object) -> None`. The first
accepts either a Pydantic `CompareDataRef` or stored JSON mapping, returns an empty tuple
for non-SQL refs, and catches only `CompareSqlProjectionError`. The second extracts column
names before calling Task 1's pure repair helper. The third raises the sanitized 409 only
when that mapping is non-empty.

Use effective stored values for PATCH. Call the guard for both SQL sides on create/update and immediately before run enqueue. Error details may include counts/indices only, never SQL or expressions.

- [ ] **Step 6: Verify and commit API work**

Run targeted API pytest, ruff format/check and mypy on changed API files. Then:

```powershell
git add app/api/schemas.py app/api/routes/core.py tests/contract/test_api.py
git commit -m "feat(compare): expose generated SQL projection metadata" -m "接口追加:CompareProjectionDetail and optional projection detail fields" -m "Co-Authored-By: OpenAI Codex <codex@openai.com>"
```

---

### Task 3: Worker Uses the Same Normalized SQL

**Files:**
- Modify: `app/worker.py:850-930`
- Modify: `app/worker.py:2613-2815`
- Modify: `app/worker.py:3276-3300`
- Modify: `tests/unit/test_worker.py:265-430`

**Interfaces:**
- Consumes Task 1 planner.
- Preserves `_compare_table_expression(db_type, data_ref) -> str`.

- [ ] **Step 1: Write a failing SQL-shape Worker test**

Run a normal `COMPARE_RUN` job through a recording fake adapter with SQL `SELECT CUST_NO, SUM(A), SUM(B), SUM(C) FROM T GROUP BY CUST_NO` and configured columns `CUST_NO, RESULT_1, RESULT_2, RESULT_3`. Assert joined captured SQL contains `SUM(A) AS RESULT_1` and outer `"RESULT_1" AS "RESULT_1"`, and contains none of `"2"`, `"3"`, `"4"`.

- [ ] **Step 2: Verify the Worker test fails**

Run `pytest tests/unit/test_worker.py -k generated_aliases -q`.

Expected: the inner query lacks generated aliases.

- [ ] **Step 3: Normalize SQL refs in `_compare_table_expression`**

Implement:

```python
if kind == "sql":
    sql = data_ref.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("sql compare ref requires sql")
    try:
        sql = normalize_compare_sql(sql, db_type).sql
    except CompareSqlProjectionError:
        pass
    return f"({sql}) DATAOPS_COMPARE_SOURCE"
```

Before building readers, reject proven stale numeric columns as a Worker safety net for workflow-created jobs that bypass HTTP. Raise `CompareSqlProjectionError("stale_generated_aliases")`; public handling remains `sql_failed` and logs no SQL/expression.

- [ ] **Step 4: Verify and commit Worker work**

Run `tests/unit/test_worker.py`, `tests/unit/test_compare_kernel.py`, ruff and mypy. Then:

```powershell
git add app/worker.py tests/unit/test_worker.py
git commit -m "fix(compare): execute SQL refs with stable expression aliases" -m "Co-Authored-By: OpenAI Codex <codex@openai.com>"
```

---

### Task 4: Reusable Expression Label UI

**Files:**
- Create: `frontend/src/components/CompareExpressionLabel.vue`
- Modify: `frontend/src/api/compare.ts:95-125`
- Modify: `frontend/src/api/compare.ts:275-310`
- Modify: `frontend/src/i18n/en.ts:510-550`
- Modify: `frontend/src/i18n/zh-CN.ts:498-540`

**Interfaces:**
- Adds TypeScript `CompareProjectionDetail`.
- Produces component props `{ name: string; detail?: CompareProjectionDetail | null }`.

- [ ] **Step 1: Add TypeScript response types**

```typescript
export interface CompareProjectionDetail {
  name: string
  generated: boolean
  projection_index: number
  expression: string | null
}
```

Add source/target detail arrays to `CompareTaskResponse`. Add optional `column_details?: CompareProjectionDetail[]` to preview for old-server tolerance.

- [ ] **Step 2: Implement `CompareExpressionLabel.vue`**

Use existing `Modal.vue`, `Info`, and `Copy`. Implement:

```typescript
const props = defineProps<{
  name: string
  detail?: CompareProjectionDetail | null
}>()
const open = ref(false)
const copied = ref(false)
const expression = computed(() => props.detail?.expression ?? '')
const summary = computed(() => {
  const oneLine = expression.value.replace(/\s+/g, ' ').trim()
  return oneLine.length > 160 ? `${oneLine.slice(0, 157)}…` : oneLine
})
```

Ordinary names render as text. Generated names render as a native focusable button with info icon, `aria-label` and `title=summary`. Click opens a modal with alias, one-based projection position, scrollable `<pre>` and clipboard action. Never use `v-html`.

- [ ] **Step 3: Add bilingual copy**

Add keys for generated alias, original projection expression, position, copy states, legacy warning/action/confirmation, alias-required error, and failed-run banner. Chinese uses “原始投影表达式”, not “完整列名”.

- [ ] **Step 4: Typecheck, build and commit**

Run `npm run typecheck` and `npm run build` from `frontend`. Then commit the API type, component and i18n files as `feat(compare): add inspectable generated-expression labels` with the Codex co-author trailer.

---

### Task 5: Integrate the Page, Repair Legacy Tasks and Fix Failure Rendering

**Files:**
- Modify: `frontend/src/views/CompareView.vue:1-100`
- Modify: `frontend/src/views/CompareView.vue:321-720`
- Modify: `frontend/src/views/CompareView.vue:880-955`
- Modify: `frontend/src/views/CompareView.vue:1320-1380`
- Modify: `frontend/src/views/CompareView.vue:2160-3040`
- Modify: `frontend/tests/compare.spec.ts`

**Interfaces:**
- Consumes Task 4 types/component and existing task PATCH/job polling APIs.

- [ ] **Step 1: Write three failing Playwright scenarios**

Add sanitized tests for:

1. SQL preview returns `CUST_NO, RESULT_1` plus a long CASE detail; hover title is truncated, click shows the full expression, and adopting columns writes `RESULT_1`.
2. A saved legacy task returns columns ending `6/7/8` plus generated details; accept confirmation, click “Update generated aliases”, and assert editor plus captured PATCH contain `RESULT_1/2/3` with key/ignore/mapping references updated.
3. Polling returns `status=failed, error=sql_failed`; assert an execution-failed banner appears while four zero cards and bucket-empty text do not.

- [ ] **Step 2: Verify the browser tests fail**

Run:

```powershell
npx playwright test tests/compare.spec.ts --grep "generated expression|legacy aliases|execution failed"
```

Expected: all new behaviors are absent.

- [ ] **Step 3: Add detail lookup and legacy repair**

Add:

```typescript
function detailByName(
  details: CompareProjectionDetail[] | undefined,
  name: string,
): CompareProjectionDetail | null {
  return details?.find((item) => item.name.toLowerCase() === name.toLowerCase()) ?? null
}
```

Compute repair maps only when the configured name equals `String(detail.projection_index)` and `detail.generated`. After one confirmation, rename source columns through existing rename semantics, update target mapping values, and save via `updateCompareTask`; never edit PostgreSQL directly.

- [ ] **Step 4: Use the label consistently**

Use `CompareExpressionLabel` in source/target preview headers, compare-column editor rows, results headers, match-rate names and profile names. Plain fields remain plain through the component fallback.

- [ ] **Step 5: Render terminal failure before result summaries**

Add:

```typescript
const runFailed = computed<boolean>(() =>
  run.status === 'failed' || run.status === 'timeout' || run.status === 'cancelled'
)
```

When true, render the terminal banner and hide progress, bucket cards, match/sample cards and bucket-empty content. Map `compare_sql_alias_required` and `compare_sql_aliases_stale` to localized safe guidance.

- [ ] **Step 6: Verify and commit frontend integration**

Run full `frontend/tests/compare.spec.ts`, typecheck and build. Confirm the test helper reports no console errors. Commit `CompareView.vue` and the spec as `fix(compare): repair generated aliases and surface failed runs` with the co-author trailer.

---

### Task 6: Full Verification, GitHub Handoff and D-Drive Deployment Gate

**Files:**
- Modify only if a failing test identifies a defect in Tasks 1–5 files.
- Read: `docs/superpowers/specs/2026-07-10-sql-compare-generated-expression-aliases-design.md`
- Read: `docs/deployment/win10-offline-bundle.md`

- [ ] **Step 1: Run targeted backend suites**

```powershell
& 'E:\work\dataops-studio-v2-source\.venv\Scripts\python.exe' -m pytest tests/unit/test_compare_sql.py tests/unit/test_worker.py tests/unit/test_compare_kernel.py tests/contract/test_api.py -q
```

- [ ] **Step 2: Run full verification**

Run full pytest, ruff check, ruff format check and mypy. From `frontend`, run typecheck, build and the complete compare Playwright spec. Record exact pass/fail/skip counts.

- [ ] **Step 3: Run redline and secret review**

```powershell
& 'E:\work\dataops-studio-v2-source\.venv\Scripts\python.exe' -m pytest tests/unit/test_redlines.py tests/unit/test_redaction.py tests/unit/test_models.py -q
git diff origin/main --check
git status --short
```

Run gitleaks if installed. Independently inspect all changed filenames and `git diff origin/main` for `.env`, logs, dumps, screenshots, SQL, addresses, credentials, tokens, cookies and sessions. Stop on any suspicion.

- [ ] **Step 4: Review implementation against the approved design**

Check alias stability/collisions, shared preview/Worker planner, unchanged SQL persistence, migration-free task metadata, guarded legacy repair including rules, accessible hover/click/copy, failed-run rendering, safe logs, and the explicit unavailable-DB2 limitation. Any gap gets a failing regression test first.

- [ ] **Step 5: Push and open a Draft PR**

Push `agent/fix-sql-compare-generated-alias`, create a Draft PR targeting `main`, and include sanitized root cause, test evidence, risks and DB2 limitation. Do not merge.

- [ ] **Step 6: Browser-verify without claiming datasource success**

Open `http://localhost:8020/projects/9b8d493a-c1d0-48fc-935a-7df319612036/compare`. Verify DOM, Console, Network, legacy repair banner, expression modal, and failed historical run display. The known datasource connection failure is allowed; live preview/run remains unverified.

- [ ] **Step 7: Ask for D-drive overwrite/restart confirmation**

Before replacing anything in `D:\myproject\dataops-studio-v2`, report source commit, exact replacement set, backup path and rollback command, then request explicit confirmation.

- [ ] **Step 8: After confirmation, deploy and repair through UI/API**

Build using the documented Windows packaging flow, back up only replaced files, stop gracefully, copy verified artifacts and restart. Verify `/healthz`, process liveness, browser DOM, Console and Network. Use the page's confirmed repair action to PATCH the existing task; verify proven positions now use `RESULT_1/2/3`. Never edit PostgreSQL directly and do not start a compare until the datasource returns.

- [ ] **Step 9: Final report**

Report branch/commit/PR, D runtime version, changed files, exact validation results, browser evidence, current task repair result, rollback path, and the remaining no-live-DB2 limitation.
