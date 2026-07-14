# AI SQL Assistant Redesign and Workspace Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the one-shot AI SQL modal with a schema-confirming, validated right-side assistant and prevent long SQL from widening the entire workspace.

**Architecture:** Keep the HTTP flow synchronous, but separate provider normalization, deterministic table ranking, SQL policy/validation, route orchestration, and panel rendering. The route rereads trusted metadata, chooses reasoning mode deterministically, allows exactly one targeted repair, and returns additive diagnostics; the frontend previews before applying and never executes generated SQL automatically.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, sqlglot, pytest, Vue 3, TypeScript, Vite, Monaco Editor, Playwright.

## Global Constraints

- Preserve all existing `SqlGenerateRequest` and `SqlGenerateResponse` fields; every new field is additive and has a default.
- Do not add a database migration or a runtime dependency.
- Do not send row values, credentials, connection details, or unconfirmed table schemas to AI.
- Initial generation is egress L2; a revision containing current SQL is egress L3 and must pass the existing Gateway policy.
- Do not log or audit the natural-language prompt, schema names, table names, columns, candidate SQL, final SQL, provider body, HTTP error body, or `reasoning_content` text.
- Allow at most one repair call; authentication, rate-limit, timeout, unreachable, metadata, and egress failures are not retried.
- Generated SQL is previewed only. Applying it updates Monaco; it never calls `/api/sql/execute`.
- Reuse `validate_readonly_sql`; do not create a second, weaker write-statement policy.
- Limit changes to the AI SQL path, its focused component/tests/i18n, and the AppShell/SQL workspace width constraints.
- Git commits use `answer <95461401+allen-answer@users.noreply.github.com>` and include `Co-Authored-By: OpenAI Codex <codex@openai.com>`.

---

## File Structure

- Modify `app/domain/ai.py`: additive reasoning and normalized response metadata shared by providers and callers.
- Modify `app/services/ai/errors.py`: safe structured Provider diagnostic code and optional HTTP status.
- Modify `app/services/ai/providers.py`: normalize finish reason, reasoning presence/length, usage, duration, and DeepSeek-compatible thinking controls without retaining raw reasoning.
- Create `app/services/ai/sql_assistant.py`: deterministic complexity classification, candidate scoring, SQL extraction, schema validation, repair policy, and safe prompt construction.
- Modify `app/api/schemas.py`: additive request/response/validation types and candidate-table endpoint contracts.
- Modify `app/api/routes/core.py`: permission checks, trusted metadata reread, Gateway calls, one repair, safe audit, and both AI SQL endpoints.
- Modify `frontend/src/api/ai.ts`: typed candidate and generation contracts.
- Create `frontend/src/components/AiSqlAssistantPanel.vue`: candidate confirmation, preview, diagnostics, apply/copy, and one-round revision UI.
- Modify `frontend/src/views/SqlWorkspaceView.vue`: replace modal state with panel integration and reset on datasource change.
- Modify `frontend/src/views/AppShellLayout.vue`: stop flex min-content width propagation.
- Modify `frontend/src/i18n/en.ts` and `frontend/src/i18n/zh-CN.ts`: panel copy and diagnostic messages.
- Modify `tests/unit/test_ai_gateway.py`: Provider normalization and thinking-control coverage.
- Create `tests/unit/test_ai_sql_assistant.py`: pure policy, ranking, extraction, and validation coverage.
- Modify `tests/contract/test_api.py`: endpoint, retry, audit, and compatibility coverage.
- Modify `frontend/tests/sql-workspace-w2.spec.ts`: panel workflow and page-width regression coverage.

---

### Task 1: Normalize Provider responses without exposing reasoning

**Files:**
- Modify: `app/domain/ai.py:35-55`
- Modify: `app/services/ai/errors.py:36-45`
- Modify: `app/services/ai/providers.py:70-190`
- Modify: `tests/unit/test_ai_gateway.py:30-125`

**Interfaces:**
- Consumes: existing `AiOptions`, `AiResponse`, `ProviderError`, and `OpenAICompatibleProvider.complete()`.
- Produces: `ReasoningMode`, additive `AiOptions.reasoning_mode`, normalized `AiResponse.finish_reason/reasoning_chars/tokens_total/duration_ms`, and `ProviderError(diagnostic_code, status_code)`.

- [ ] **Step 1: Add failing response-normalization tests**

Add these imports and tests to `tests/unit/test_ai_gateway.py`:

```python
from app.domain.ai import ReasoningMode


def test_openai_compatible_normalizes_reasoning_finish_and_usage() -> None:
    transport = _FakeTransport(
        {
            "model": "deepseek-v4-pro",
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {
                        "role": "assistant",
                        "reasoning_content": "private chain that must not escape",
                        "content": "",
                    },
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 90, "total_tokens": 100},
        }
    )
    provider = OpenAICompatibleProvider(
        api_key="not-a-real-key",
        endpoint="https://example.invalid/v1",
        model="deepseek-v4-pro",
        transport=transport,
    )

    response = provider.complete("query", AiContext(), AiOptions(max_tokens=100))

    assert response.content == ""
    assert response.finish_reason == "length"
    assert response.reasoning_chars == len("private chain that must not escape")
    assert response.tokens_total == 100
    assert "private chain" not in response.model_dump_json()


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (ReasoningMode.ENABLED, {"type": "enabled"}),
        (ReasoningMode.DISABLED, {"type": "disabled"}),
    ],
)
def test_deepseek_model_maps_reasoning_mode_to_thinking(
    mode: ReasoningMode, expected: dict[str, str]
) -> None:
    transport = _FakeTransport(_OPENAI_RESPONSE)
    provider = OpenAICompatibleProvider(
        api_key="not-a-real-key",
        endpoint="https://example.invalid/v1",
        model="deepseek-v4-pro",
        transport=transport,
    )

    provider.complete("query", AiContext(), AiOptions(reasoning_mode=mode))

    assert transport.last_body["thinking"] == expected


def test_generic_model_does_not_receive_vendor_thinking_field() -> None:
    transport = _FakeTransport(_OPENAI_RESPONSE)
    provider = OpenAICompatibleProvider(
        api_key="not-a-real-key",
        endpoint="https://example.invalid/v1",
        model="generic-model",
        transport=transport,
    )

    provider.complete(
        "query", AiContext(), AiOptions(reasoning_mode=ReasoningMode.DISABLED)
    )

    assert "thinking" not in transport.last_body


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "provider_auth_failed"), (403, "provider_auth_failed"), (429, "provider_rate_limited")],
)
def test_urllib_transport_classifies_http_without_exposing_body(
    monkeypatch: pytest.MonkeyPatch, status: int, code: str
) -> None:
    error = urllib.error.HTTPError(
        "https://example.invalid", status, "provider secret body", {}, None
    )
    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(error))

    with pytest.raises(ProviderError) as caught:
        UrllibTransport().post_json("https://example.invalid", {}, {})

    assert caught.value.diagnostic_code == code
    assert caught.value.status_code == status
    assert "provider secret body" not in str(caught.value)


@pytest.mark.parametrize(
    ("reason", "code"),
    [(TimeoutError(), "provider_timeout"), (OSError("offline"), "provider_unreachable")],
)
def test_urllib_transport_classifies_network_failure(
    monkeypatch: pytest.MonkeyPatch, reason: Exception, code: str
) -> None:
    error = urllib.error.URLError(reason)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(error))

    with pytest.raises(ProviderError) as caught:
        UrllibTransport().post_json("https://example.invalid", {}, {})

    assert caught.value.diagnostic_code == code
```

Add `import urllib.error`, `import urllib.request`, and `UrllibTransport` to this test module's imports.

Change the existing empty-content test to assert that empty final content is normalized, not thrown away:

```python
def test_openai_compatible_empty_content_is_preserved_for_caller_diagnostics() -> None:
    response: dict[str, object] = {
        "model": "test-model",
        "choices": [
            {"finish_reason": "stop", "message": {"role": "assistant", "content": "   "}}
        ],
    }
    provider = OpenAICompatibleProvider(
        api_key="not-a-real-key",
        endpoint="https://example.invalid",
        model="test-model",
        transport=_FakeTransport(response),
    )

    result = provider.complete("hi", AiContext(), AiOptions())

    assert result.content == ""
    assert result.reasoning_chars == 0
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/unit/test_ai_gateway.py -q
```

Expected: failures for missing `ReasoningMode`, missing response fields, empty-content behavior, and absent `thinking` request field.

- [ ] **Step 3: Add additive domain fields and structured ProviderError**

Update `app/domain/ai.py`:

```python
from enum import IntEnum, StrEnum


class ReasoningMode(StrEnum):
    DISABLED = "disabled"
    ENABLED = "enabled"


class AiOptions(BaseModel):
    provider: str | None = None
    model: str | None = None
    max_tokens: int | None = None
    temperature: float | None = None
    reasoning_mode: ReasoningMode | None = None
    purpose: str = "unspecified"


class AiResponse(BaseModel):
    content: str
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_total: int = 0
    provider: str = "unknown"
    model: str = "unknown"
    finish_reason: str | None = None
    reasoning_chars: int = 0
    duration_ms: int = 0
```

Replace `ProviderError` in `app/services/ai/errors.py` with:

```python
class ProviderError(AiGatewayError):
    """Safe provider failure; never contains a response body or credential."""

    def __init__(
        self,
        diagnostic_code: str = "provider_invalid_response",
        *,
        status_code: int | None = None,
    ) -> None:
        self.diagnostic_code = diagnostic_code
        self.status_code = status_code
        super().__init__(diagnostic_code)
```

- [ ] **Step 4: Normalize transport and completion metadata**

In `app/services/ai/providers.py`, import `time`, `ReasoningMode`, and classify safe errors:

```python
import socket
import time


def _http_diagnostic(status: int) -> str:
    if status in {401, 403}:
        return "provider_auth_failed"
    if status == 429:
        return "provider_rate_limited"
    return "provider_invalid_response"
```

Use these exception branches in `UrllibTransport.post_json`:

```python
        except urllib.error.HTTPError as exc:
            raise ProviderError(
                _http_diagnostic(exc.code), status_code=exc.code
            ) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise ProviderError("provider_timeout") from exc
        except urllib.error.URLError as exc:
            code = "provider_timeout" if isinstance(exc.reason, TimeoutError) else "provider_unreachable"
            raise ProviderError(code) from exc
```

Before posting, map thinking only for supported DeepSeek model names and measure duration:

```python
def _supports_deepseek_thinking(model: str) -> bool:
    lowered = model.lower()
    return lowered.startswith("deepseek-v4") or lowered == "deepseek-reasoner"


        if options.reasoning_mode is not None and _supports_deepseek_thinking(model):
            body["thinking"] = {"type": options.reasoning_mode.value}
        started = time.perf_counter()
        payload = self.transport.post_json(self._url(), headers, body)
        duration_ms = max(0, round((time.perf_counter() - started) * 1000))
        response = _parse_chat_completion(
            payload, fallback_provider=self.name, fallback_model=model
        )
        return response.model_copy(update={"duration_ms": duration_ms})
```

Replace `_parse_chat_completion` content/usage handling with:

```python
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProviderError("provider_invalid_response")
    first = choices[0]
    if not isinstance(first, dict):
        raise ProviderError("provider_invalid_response")
    message = first.get("message")
    if not isinstance(message, dict):
        raise ProviderError("provider_invalid_response")
    content_raw = message.get("content")
    if content_raw is None:
        content = ""
    elif isinstance(content_raw, str):
        content = content_raw.strip()
    else:
        raise ProviderError("provider_invalid_response")
    reasoning_raw = message.get("reasoning_content")
    reasoning_chars = len(reasoning_raw) if isinstance(reasoning_raw, str) else 0
    finish_raw = first.get("finish_reason")
    finish_reason = finish_raw if isinstance(finish_raw, str) else None
    usage_raw = payload.get("usage")
    usage = usage_raw if isinstance(usage_raw, dict) else {}
    tokens_in = _as_int(usage.get("prompt_tokens"))
    tokens_out = _as_int(usage.get("completion_tokens"))
    tokens_total = _as_int(usage.get("total_tokens")) or tokens_in + tokens_out
    model_raw = payload.get("model")
    model = model_raw if isinstance(model_raw, str) else fallback_model
    return AiResponse(
        content=content,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        tokens_total=tokens_total,
        provider=fallback_provider,
        model=model,
        finish_reason=finish_reason,
        reasoning_chars=reasoning_chars,
    )
```

- [ ] **Step 5: Run Provider and Gateway regression tests**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/unit/test_ai_gateway.py tests/contract/test_ai_gateway.py -q
```

Expected: all tests pass; no test requires network access.

- [ ] **Step 6: Commit Task 1**

```powershell
git add app/domain/ai.py app/services/ai/errors.py app/services/ai/providers.py tests/unit/test_ai_gateway.py
git commit -m "feat(ai): normalize reasoning provider responses" -m "Co-Authored-By: OpenAI Codex <codex@openai.com>"
```

---

### Task 2: Add deterministic AI SQL policy, ranking, and validation

**Files:**
- Create: `app/services/ai/sql_assistant.py`
- Create: `tests/unit/test_ai_sql_assistant.py`

**Interfaces:**
- Consumes: `Column`, `ReasoningMode`, `sqlglot`, and `validate_readonly_sql(sql) -> str`.
- Produces: `TableSchema`, `TableCandidate`, `SqlValidation`, `classify_reasoning_mode()`, `rank_table_candidates()`, `extract_sql()`, `validate_generated_sql()`, `diagnose_empty_response()`, `should_repair()`, `build_generation_prompt()`, and `build_repair_prompt()`.

- [ ] **Step 1: Write failing policy and ranking tests**

Create `tests/unit/test_ai_sql_assistant.py` with:

```python
from app.domain.ai import AiResponse, ReasoningMode
from app.domain.schema import Column, ColumnType
from app.services.ai.sql_assistant import (
    TableSchema,
    classify_reasoning_mode,
    diagnose_empty_response,
    extract_sql,
    rank_table_candidates,
    should_repair,
    validate_generated_sql,
)


USERS = TableSchema(
    schema_name="app",
    table_name="users",
    columns=(
        Column(name="id", type=ColumnType.INTEGER),
        Column(name="name", type=ColumnType.STRING),
    ),
)
ORDERS = TableSchema(
    schema_name="app",
    table_name="orders",
    columns=(
        Column(name="id", type=ColumnType.INTEGER),
        Column(name="customer_id", type=ColumnType.INTEGER),
        Column(name="amount", type=ColumnType.DECIMAL),
    ),
)


def test_reasoning_mode_is_disabled_only_for_simple_single_table_query() -> None:
    assert classify_reasoning_mode("list user names", [USERS]) is ReasoningMode.DISABLED
    assert classify_reasoning_mode("sum order amount by customer", [ORDERS]) is ReasoningMode.ENABLED
    assert classify_reasoning_mode("join users and orders", [USERS, ORDERS]) is ReasoningMode.ENABLED


def test_candidate_ranking_prefers_editor_then_table_then_column_match() -> None:
    ranked = rank_table_candidates(
        "customer order amount",
        "SELECT * FROM app.users",
        [ORDERS, USERS],
        limit=10,
    )
    assert ranked[0].table_name == "users"
    assert ranked[0].matched_by == ("editor_reference",)
    assert ranked[1].table_name == "orders"
    assert "table_name" in ranked[1].matched_by
    assert "column_name" in ranked[1].matched_by


def test_extract_sql_accepts_fence_and_rejects_multiple_statements() -> None:
    assert extract_sql("```sql\nSELECT id FROM app.users\n```") == "SELECT id FROM app.users"
    assert extract_sql("SELECT 1; SELECT 2") is None


def test_validation_understands_alias_cte_and_unknown_identifiers() -> None:
    valid = validate_generated_sql(
        "WITH paid AS (SELECT customer_id, amount FROM app.orders) "
        "SELECT u.name, p.amount FROM app.users u JOIN paid p ON p.customer_id = u.id",
        dialect="mysql",
        tables=[USERS, ORDERS],
    )
    assert valid.ok is True
    assert valid.readonly == "passed"
    assert valid.tables == "passed"
    assert valid.columns == "passed"

    unknown = validate_generated_sql(
        "SELECT missing FROM app.users", dialect="mysql", tables=[USERS]
    )
    assert unknown.ok is False
    assert unknown.diagnostic_code == "sql_unknown_column"


def test_validation_rejects_write_and_unknown_table() -> None:
    write = validate_generated_sql(
        "DELETE FROM app.users", dialect="mysql", tables=[USERS]
    )
    assert write.diagnostic_code == "sql_not_readonly"
    unknown = validate_generated_sql(
        "SELECT id FROM app.accounts", dialect="mysql", tables=[USERS]
    )
    assert unknown.diagnostic_code == "sql_unknown_table"


def test_response_diagnostics_and_repair_policy_are_bounded() -> None:
    truncated = AiResponse(content="", finish_reason="length", reasoning_chars=20)
    reasoning_only = AiResponse(content="", finish_reason="stop", reasoning_chars=20)
    assert diagnose_empty_response(truncated) == "provider_output_truncated"
    assert diagnose_empty_response(reasoning_only) == "provider_reasoning_only"
    assert should_repair("provider_output_truncated", attempts=1) is True
    assert should_repair("sql_parse_failed", attempts=1) is True
    assert should_repair("provider_auth_failed", attempts=1) is False
    assert should_repair("sql_parse_failed", attempts=2) is False
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/unit/test_ai_sql_assistant.py -q
```

Expected: collection fails because `app.services.ai.sql_assistant` does not exist.

- [ ] **Step 3: Implement focused immutable models and deterministic policy**

Create `app/services/ai/sql_assistant.py` with these public models and policy constants:

```python
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from app.dbclients.sql_guard import SqlGuardError, validate_readonly_sql
from app.domain.ai import AiResponse, ReasoningMode
from app.domain.schema import Column

ValidationState = Literal["passed", "failed", "partial"]
_COMPLEX_WORDS = re.compile(
    r"\b(sum|count|avg|average|group|aggregate|join|subquery|cte|window|rank|union|intersect|except)\b",
    re.IGNORECASE,
)
_REPAIRABLE = {
    "provider_reasoning_only",
    "provider_output_truncated",
    "provider_invalid_response",
    "sql_parse_failed",
    "sql_unknown_table",
    "sql_unknown_column",
}


@dataclass(frozen=True)
class TableSchema:
    schema_name: str
    table_name: str
    columns: tuple[Column, ...]


@dataclass(frozen=True)
class TableCandidate:
    schema_name: str
    table_name: str
    matched_by: tuple[str, ...]


@dataclass(frozen=True)
class SqlValidation:
    ok: bool
    readonly: ValidationState
    tables: ValidationState
    columns: ValidationState
    diagnostic_code: str | None = None
    warnings: tuple[str, ...] = ()
```

Implement these exact signatures:

```python
def classify_reasoning_mode(
    natural_language: str, tables: list[TableSchema]
) -> ReasoningMode:
    if len(tables) != 1 or _COMPLEX_WORDS.search(natural_language):
        return ReasoningMode.ENABLED
    return ReasoningMode.DISABLED


def diagnose_empty_response(response: AiResponse) -> str | None:
    if response.content.strip():
        return None
    if response.finish_reason == "length":
        return "provider_output_truncated"
    if response.reasoning_chars > 0:
        return "provider_reasoning_only"
    return "provider_invalid_response"


def should_repair(diagnostic_code: str, *, attempts: int) -> bool:
    return attempts == 1 and diagnostic_code in _REPAIRABLE
```

Add the deterministic candidate scorer:

```python
def _editor_table_names(editor_sql: str) -> set[str]:
    if not editor_sql.strip():
        return set()
    try:
        statements = sqlglot.parse(editor_sql)
    except ParseError:
        return set()
    return {
        table.name.casefold()
        for statement in statements
        for table in statement.find_all(exp.Table)
        if table.name
    }


def rank_table_candidates(
    natural_language: str,
    editor_sql: str,
    tables: list[TableSchema],
    *,
    limit: int,
) -> list[TableCandidate]:
    words = set(re.findall(r"[\w]+", natural_language.casefold()))
    editor_tables = _editor_table_names(editor_sql)
    scored: list[tuple[int, int, TableCandidate]] = []
    for index, table in enumerate(tables):
        matched: list[str] = []
        score = 0
        table_name = table.table_name.casefold()
        if table_name in editor_tables:
            matched.append("editor_reference")
            score += 100
        if table_name in words or any(word in table_name for word in words):
            matched.append("table_name")
            score += 20
        if any(
            column.name.casefold() in words
            or any(word in column.name.casefold() for word in words)
            for column in table.columns
        ):
            matched.append("column_name")
            score += 2
        if score:
            scored.append(
                (
                    -score,
                    index,
                    TableCandidate(
                        schema_name=table.schema_name,
                        table_name=table.table_name,
                        matched_by=tuple(matched),
                    ),
                )
            )
    scored.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in scored[:limit]]
```

- [ ] **Step 4: Implement extraction, prompt construction, and validation**

Add:

```python
def extract_sql(content: str) -> str | None:
    fenced = re.search(r"```(?:sql)?\s*(.*?)```", content, flags=re.IGNORECASE | re.DOTALL)
    candidate = (fenced.group(1) if fenced else content).strip()
    if not candidate:
        return None
    try:
        statements = [item for item in sqlglot.parse(candidate) if item is not None]
    except ParseError:
        return candidate
    return candidate.rstrip(";").strip() if len(statements) == 1 else None


def build_generation_prompt(
    natural_language: str, *, dialect: str, revision_instruction: str | None = None
) -> str:
    if revision_instruction is None:
        return (
            f"Generate one read-only {dialect} SELECT for this request. "
            "Use only the supplied schema. Return SQL only.\nRequest: "
            f"{natural_language.strip()}"
        )
    return (
        f"Revise the supplied candidate into one read-only {dialect} SELECT. "
        "Use only the supplied schema. Return SQL only.\nRevision: "
        f"{revision_instruction.strip()}"
    )


def build_repair_prompt(candidate_sql: str, diagnostic_code: str, *, dialect: str) -> str:
    return (
        f"Repair this candidate into one read-only {dialect} SELECT using only the supplied schema. "
        f"Validation code: {diagnostic_code}. Return SQL only.\nCandidate SQL:\n{candidate_sql}"
    )
```

Add these helpers and the complete validator. No exception text is returned:

```python
def _table_match(node: exp.Table, tables: list[TableSchema]) -> TableSchema | None:
    name = node.name.casefold()
    schema = node.db.casefold() if node.db else None
    matches = [
        item
        for item in tables
        if item.table_name.casefold() == name
        and (schema is None or item.schema_name.casefold() == schema)
    ]
    return matches[0] if len(matches) == 1 else None


def _has_column(table: TableSchema, name: str) -> bool:
    lowered = name.casefold()
    return any(column.name.casefold() == lowered for column in table.columns)


def validate_generated_sql(
    sql: str, *, dialect: str, tables: list[TableSchema]
) -> SqlValidation:
    try:
        guarded = validate_readonly_sql(sql)
    except SqlGuardError:
        return SqlValidation(
            ok=False,
            readonly="failed",
            tables="failed",
            columns="failed",
            diagnostic_code="sql_not_readonly",
        )
    try:
        statements = sqlglot.parse(guarded, read=dialect)
    except ParseError:
        return SqlValidation(
            ok=False,
            readonly="passed",
            tables="failed",
            columns="failed",
            diagnostic_code="sql_parse_failed",
        )
    if len(statements) != 1:
        return SqlValidation(
            ok=False,
            readonly="passed",
            tables="failed",
            columns="failed",
            diagnostic_code="sql_parse_failed",
        )

    statement = statements[0]
    cte_names = {
        cte.alias_or_name.casefold()
        for cte in statement.find_all(exp.CTE)
        if cte.alias_or_name
    }
    alias_tables: dict[str, TableSchema] = {}
    derived_aliases = set(cte_names)
    for node in statement.find_all(exp.Table):
        if node.name.casefold() in cte_names:
            derived_aliases.add(node.alias_or_name.casefold())
            continue
        matched = _table_match(node, tables)
        if matched is None:
            return SqlValidation(
                ok=False,
                readonly="passed",
                tables="failed",
                columns="failed",
                diagnostic_code="sql_unknown_table",
            )
        alias_tables[node.alias_or_name.casefold()] = matched
        alias_tables[node.name.casefold()] = matched

    projected_aliases = {
        select.alias_or_name.casefold()
        for select in statement.selects
        if select.alias_or_name
    }
    partial = False
    for column in statement.find_all(exp.Column):
        if column.name == "*":
            continue
        qualifier = column.table.casefold() if column.table else ""
        if qualifier:
            if qualifier in derived_aliases:
                partial = True
                continue
            table = alias_tables.get(qualifier)
            if table is None or not _has_column(table, column.name):
                return SqlValidation(
                    ok=False,
                    readonly="passed",
                    tables="passed",
                    columns="failed",
                    diagnostic_code="sql_unknown_column",
                )
            continue
        matches = [table for table in tables if _has_column(table, column.name)]
        if not matches and column.name.casefold() not in projected_aliases:
            return SqlValidation(
                ok=False,
                readonly="passed",
                tables="passed",
                columns="failed",
                diagnostic_code="sql_unknown_column",
            )
        if len(matches) != 1:
            partial = True

    return SqlValidation(
        ok=True,
        readonly="passed",
        tables="passed",
        columns="partial" if partial else "passed",
        warnings=("sql_column_validation_partial",) if partial else (),
    )
```

- [ ] **Step 5: Run focused unit tests and static checks**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/unit/test_ai_sql_assistant.py tests/unit/test_mysql_adapter.py -q
& .\.venv\Scripts\ruff.exe check app/services/ai/sql_assistant.py tests/unit/test_ai_sql_assistant.py
& .\.venv\Scripts\mypy.exe app/services/ai/sql_assistant.py
```

Expected: all tests and checks pass.

- [ ] **Step 6: Commit Task 2**

```powershell
git add app/services/ai/sql_assistant.py tests/unit/test_ai_sql_assistant.py
git commit -m "feat(ai): add deterministic SQL assistant policy" -m "Co-Authored-By: OpenAI Codex <codex@openai.com>"
```

---

### Task 3: Add candidate-table and additive generation contracts

**Files:**
- Modify: `app/api/schemas.py:135-158`
- Modify: `app/api/routes/core.py:1440-1585`
- Modify: `tests/contract/test_api.py:1930-2040`

**Interfaces:**
- Consumes: `rank_table_candidates()` and existing metadata cache/probe helpers.
- Produces: `POST /datasources/{id}/ai/sql-table-candidates`, additive generation request fields, and additive response diagnostics.

- [ ] **Step 1: Write failing contract tests for recommendation and compatibility**

Add to `tests/contract/test_api.py`:

```python
def test_ai_sql_table_candidates_rank_without_calling_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    calls = {"gateway": 0}

    def fail_gateway(*args: object, **kwargs: object) -> None:
        calls["gateway"] += 1
        raise AssertionError("candidate ranking must not call AI")

    monkeypatch.setattr(core_routes, "build_gateway_from_runtime_config", fail_gateway)
    monkeypatch.setattr(
        core_routes,
        "_metadata_all_tables",
        lambda services, row, refresh: [
            SimpleNamespace(schema_name="app", name="orders"),
            SimpleNamespace(schema_name="app", name="users"),
        ],
    )
    monkeypatch.setattr(
        core_routes,
        "_metadata_columns_for_table",
        lambda services, row, schema_name, table_name, refresh: [
            Column(name="customer_id", type=ColumnType.INTEGER),
            Column(
                name="amount" if table_name == "orders" else "name",
                type=ColumnType.STRING,
            ),
        ],
    )
    engine = _FakeEngine([_datasource_row(), {"id": "project-1"}])
    services = _Services(engine)
    app = create_app(services=cast(ApiServices, services))

    response = AsgiClient(app).post(
        "/api/datasources/ds-1/ai/sql-table-candidates",
        headers=_auth_headers(),
        json_body={
            "natural_language": "customer order amount",
            "schema_name": None,
            "editor_sql": "SELECT * FROM app.users",
        },
    )

    assert response.status_code == 200
    assert response.json()["candidates"][0]["table_name"] == "users"
    assert calls["gateway"] == 0


def test_ai_sql_generate_rejects_empty_confirmed_table_scope() -> None:
    engine = _FakeEngine([_datasource_row(), {"id": "project-1"}, _ai_config_row()])
    services = _Services(engine)
    app = create_app(services=cast(ApiServices, services))

    response = AsgiClient(app).post(
        "/api/datasources/ds-1/ai/sql-generate",
        headers=_auth_headers(),
        json_body={"natural_language": "list rows", "schema_name": "app", "table_names": []},
    )

    assert response.status_code == 422


def test_sql_generate_response_additive_defaults_preserve_old_construction() -> None:
    payload = SqlGenerateResponse(ok=False, error="ProviderError").model_dump(mode="json")
    assert payload["stage"] == "failed"
    assert payload["diagnostic_code"] is None
    assert payload["attempts"] == 0
    assert payload["validation"] is None
```

Add `Column` and `ColumnType` to the test imports used by the monkeypatched metadata helpers above. The test contains no adapter connection or Provider call.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/contract/test_api.py -q -k "ai_sql_table_candidates or empty_confirmed_table_scope or additive_defaults"
```

Expected: failures because the endpoint and additive fields do not exist and empty `table_names` is currently allowed.

- [ ] **Step 3: Add exact Pydantic contracts**

In `app/api/schemas.py`, add:

```python
SqlValidationState = Literal["passed", "failed", "partial"]
SqlGenerateStage = Literal["failed", "validated"]
ReasoningModeValue = Literal["disabled", "enabled"]


class SqlTableCandidatesRequest(BaseModel):
    natural_language: str = Field(min_length=1, max_length=2000)
    schema_name: str | None = None
    editor_sql: str | None = Field(default=None, max_length=20000)


class SqlTableCandidateItem(BaseModel):
    schema_name: str | None
    table_name: str
    matched_by: list[Literal["editor_reference", "table_name", "column_name"]]


class SqlTableCandidatesResponse(BaseModel):
    candidates: list[SqlTableCandidateItem] = Field(default_factory=list)
    truncated: bool = False


class SqlGenerateValidation(BaseModel):
    readonly: SqlValidationState
    tables: SqlValidationState
    columns: SqlValidationState
    warnings: list[str] = Field(default_factory=list)
```

Extend the existing models:

```python
class SqlGenerateRequest(BaseModel):
    natural_language: str = Field(min_length=1, max_length=2000)
    schema_name: str | None = None
    table_names: list[str] = Field(default_factory=list, max_length=12)
    candidate_sql: str | None = Field(default=None, max_length=20000)
    revision_instruction: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _validate_revision_pair(self) -> SqlGenerateRequest:
        if (self.candidate_sql is None) != (self.revision_instruction is None):
            raise ValueError("candidate_sql and revision_instruction must be supplied together")
        return self


class SqlGenerateResponse(BaseModel):
    # retain every existing field unchanged
    stage: SqlGenerateStage = "failed"
    diagnostic_code: str | None = None
    attempts: int = Field(default=0, ge=0, le=2)
    reasoning_mode: ReasoningModeValue | None = None
    validation: SqlGenerateValidation | None = None
    request_id: str | None = None
```

Keep the request field optional for parsing compatibility. At the beginning of `generate_sql_from_nl`, reject an empty scope with `ApiError(422, "ai_table_scope_required", "Confirm at least one table")`. Update existing generation tests that intend to reach metadata or Provider logic to pass a confirmed fixture table.

- [ ] **Step 4: Implement the deterministic candidate endpoint**

Add the route immediately before `sql-generate`:

```python
@router.post(
    "/datasources/{datasource_id}/ai/sql-table-candidates",
    response_model=SqlTableCandidatesResponse,
)
def suggest_ai_sql_tables(
    datasource_id: str,
    body: SqlTableCandidatesRequest,
    request: Request,
) -> SqlTableCandidatesResponse:
    services = services_from(request)
    row = _datasource_for_current_user(request, datasource_id)
    all_tables = _metadata_all_tables(services, row, refresh=False)
    filtered = [
        item for item in all_tables if body.schema_name is None or item.schema_name == body.schema_name
    ]
    table_schemas = [
        TableSchema(
            schema_name=item.schema_name,
            table_name=item.name,
            columns=tuple(
                _metadata_columns_for_table(
                    services,
                    row,
                    schema_name=item.schema_name,
                    table_name=item.name,
                    refresh=False,
                )
            ),
        )
        for item in filtered[:MAX_TABLES]
    ]
    ranked = rank_table_candidates(
        body.natural_language, body.editor_sql or "", table_schemas, limit=8
    )
    return SqlTableCandidatesResponse(
        candidates=[
            SqlTableCandidateItem(
                schema_name=item.schema_name,
                table_name=item.table_name,
                matched_by=list(item.matched_by),
            )
            for item in ranked
        ],
        truncated=len(filtered) > MAX_TABLES,
    )
```

Import the new schemas and service symbols explicitly. Do not audit `natural_language` or `editor_sql`; the existing request ID remains available for operational tracing.

- [ ] **Step 5: Run focused contracts and quality checks**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/contract/test_api.py -q -k "ai_sql"
& .\.venv\Scripts\ruff.exe check app/api/schemas.py app/api/routes/core.py tests/contract/test_api.py
& .\.venv\Scripts\mypy.exe app/api
```

Expected: candidate and schema-contract tests pass; existing AI SQL tests pass after adding explicit confirmed table names.

- [ ] **Step 6: Commit Task 3**

```powershell
git add app/api/schemas.py app/api/routes/core.py tests/contract/test_api.py
git commit -m "feat(ai): add confirmed table scope contracts" -m "接口追加: AI SQL candidate and diagnostic fields" -m "Co-Authored-By: OpenAI Codex <codex@openai.com>"
```

---

### Task 4: Orchestrate reasoning, validation, one repair, and safe audit

**Files:**
- Modify: `app/api/routes/core.py:1445-1533`
- Modify: `tests/contract/test_api.py:1930-2045`

**Interfaces:**
- Consumes: Task 1 normalized Provider metadata, Task 2 policy/validation helpers, Task 3 additive API models.
- Produces: validated preview responses with bounded retry and safe `ai_copilot_run` audit details.

- [ ] **Step 1: Write failing orchestration tests**

Add a capturing sequence Gateway fixture and these tests to `tests/contract/test_api.py`:

```python
class _SequenceGateway:
    def __init__(self, responses: list[AiResponse] | None = None, error: Exception | None = None):
        self.responses = list(responses or [])
        self.error = error
        self.calls: list[tuple[str, AiContext, AiOptions]] = []

    def complete(self, prompt: str, context: AiContext, options: AiOptions) -> AiResponse:
        self.calls.append((prompt, context, options))
        if self.error is not None:
            raise self.error
        return self.responses.pop(0)


def test_ai_sql_generate_validates_preview_without_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = _SequenceGateway(
        [AiResponse(content="SELECT id, name FROM app.users", provider="mock", model="m")]
    )
    monkeypatch.setattr(core_routes, "build_gateway_from_runtime_config", lambda runtime: gateway)
    services = _Services(_ai_sql_engine())
    app = create_app(services=cast(ApiServices, services))

    response = AsgiClient(app).post(
        "/api/datasources/ds-1/ai/sql-generate",
        headers=_auth_headers(),
        json_body={
            "natural_language": "list user names",
            "schema_name": "app",
            "table_names": ["users"],
        },
    )

    payload = response.json()
    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["stage"] == "validated"
    assert payload["attempts"] == 1
    assert payload["reasoning_mode"] == "disabled"
    assert payload["validation"] == {
        "readonly": "passed", "tables": "passed", "columns": "passed", "warnings": []
    }
    assert not any(a["action"] == "sql_execute" for a in services.audits)


def test_ai_sql_generate_repairs_truncated_output_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = _SequenceGateway(
        [
            AiResponse(content="", finish_reason="length", reasoning_chars=100),
            AiResponse(content="SELECT id FROM app.users", finish_reason="stop"),
        ]
    )
    monkeypatch.setattr(core_routes, "build_gateway_from_runtime_config", lambda runtime: gateway)
    services = _Services(_ai_sql_engine())
    response = AsgiClient(create_app(services=cast(ApiServices, services))).post(
        "/api/datasources/ds-1/ai/sql-generate",
        headers=_auth_headers(),
        json_body={
            "natural_language": "list users",
            "schema_name": "app",
            "table_names": ["users"],
        },
    )

    assert response.json()["ok"] is True
    assert response.json()["attempts"] == 2
    assert len(gateway.calls) == 2
    assert gateway.calls[1][2].max_tokens > gateway.calls[0][2].max_tokens


@pytest.mark.parametrize(
    "code", ["provider_auth_failed", "provider_rate_limited", "provider_timeout"]
)
def test_ai_sql_generate_does_not_retry_deterministic_provider_failures(
    monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    gateway = _SequenceGateway(error=ProviderError(code))
    monkeypatch.setattr(core_routes, "build_gateway_from_runtime_config", lambda runtime: gateway)
    services = _Services(_ai_sql_engine())
    response = AsgiClient(create_app(services=cast(ApiServices, services))).post(
        "/api/datasources/ds-1/ai/sql-generate",
        headers=_auth_headers(),
        json_body={
            "natural_language": "list users",
            "schema_name": "app",
            "table_names": ["users"],
        },
    )
    assert response.json()["diagnostic_code"] == code
    assert len(gateway.calls) == 1


def test_ai_sql_audit_contains_only_allowlisted_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = _SequenceGateway([AiResponse(content="SELECT id FROM app.users")])
    monkeypatch.setattr(core_routes, "build_gateway_from_runtime_config", lambda runtime: gateway)
    services = _Services(_ai_sql_engine())
    AsgiClient(create_app(services=cast(ApiServices, services))).post(
        "/api/datasources/ds-1/ai/sql-generate",
        headers=_auth_headers(),
        json_body={
            "natural_language": "private prompt marker",
            "schema_name": "app",
            "table_names": ["users"],
        },
    )
    detail = next(a["detail"] for a in services.audits if a["action"] == "ai_copilot_run")
    assert set(detail) <= {
        "diagnostic_code", "stage", "duration_ms", "provider_duration_ms", "attempts",
        "provider", "model", "reasoning_mode", "table_count", "tokens_in", "tokens_out",
        "tokens_total", "egress_level",
    }
    assert "private prompt marker" not in str(detail)
    assert "users" not in str(detail)
```

Add this test helper beside the existing `_ai_config_row()` fixture:

```python
def _ai_sql_engine() -> _FakeEngine:
    return _FakeEngine(
        [
            _datasource_row(),
            {"id": "project-1"},
            _ai_config_row(),
            _metadata_cache(
                [
                    {
                        "name": "id",
                        "type": "integer",
                        "driver_type": "INT",
                        "nullable": False,
                        "primary_key": True,
                        "comment": None,
                    },
                    {
                        "name": "name",
                        "type": "string",
                        "driver_type": "VARCHAR(64)",
                        "nullable": True,
                        "primary_key": False,
                        "comment": None,
                    },
                ]
            ),
        ]
    )
```

Update the pre-existing successful AI SQL contract test to inject `_SequenceGateway` with valid SQL; the old `MockProvider(reply="ok")` response must no longer be treated as valid SQL.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/contract/test_api.py -q -k "ai_sql_generate"
```

Expected: new tests fail because the route still makes one fixed-budget call, accepts unvalidated output, and emits generic errors.

- [ ] **Step 3: Replace route orchestration with a bounded loop**

In `generate_sql_from_nl`:

1. Require confirmed `schema_name/table_names` and call `_schema_tables_for_ai` only for those names.
2. Convert metadata tuples to `TableSchema`.
3. Normalize the parser dialect with the existing `_sqlglot_dialect(str(row["db_type"]))`; use the original DB type only in user-facing prompt wording.
4. Select `ReasoningMode` and budgets:

```python
reasoning_mode = classify_reasoning_mode(body.natural_language, table_schemas)
first_budget = 1200 if reasoning_mode is ReasoningMode.DISABLED else 3200
repair_budget = 1800 if reasoning_mode is ReasoningMode.DISABLED else 4800
egress_level = EgressLevel.L3 if body.candidate_sql is not None else EgressLevel.L2
```

5. Build context with the existing schema JSON at L2. For revision, add candidate SQL as a separate L3 `ContextItem`; never include it in audit.
6. Use the generation prompt for attempt 1 and repair prompt for attempt 2.
7. After each response, call `diagnose_empty_response`, `extract_sql`, and `validate_generated_sql` in that order.
8. Retry only when `should_repair(code, attempts=attempts)` returns true.
9. Catch `ProviderError` and use only `exc.diagnostic_code`; map other `AiGatewayError` to `ai_egress_blocked` or `provider_invalid_response` by explicit exception class.

Use this response shape on success:

```python
return SqlGenerateResponse(
    ok=True,
    sql=sql,
    explanation=None,
    provider=response.provider,
    model=response.model,
    egress_level=int(egress_level),
    tables_used=tables_used,
    truncated=truncated,
    stage="validated",
    diagnostic_code=None,
    attempts=attempts,
    reasoning_mode=reasoning_mode.value,
    validation=SqlGenerateValidation(
        readonly=validation.readonly,
        tables=validation.tables,
        columns=validation.columns,
        warnings=list(validation.warnings),
    ),
    request_id=getattr(request.state, "request_id", None),
)
```

Use this response shape after the final failure:

```python
return SqlGenerateResponse(
    ok=False,
    error=diagnostic_code,
    stage="failed",
    diagnostic_code=diagnostic_code,
    attempts=attempts,
    reasoning_mode=reasoning_mode.value,
    request_id=getattr(request.state, "request_id", None),
)
```

- [ ] **Step 4: Centralize allowlisted audit construction**

Add a local helper that receives scalar metadata only:

```python
def audit_detail(
    *,
    diagnostic_code: str | None,
    stage: str,
    duration_ms: int,
    provider_duration_ms: int,
    attempts: int,
    provider: str | None,
    model: str | None,
    reasoning_mode: str,
    table_count: int,
    tokens_in: int,
    tokens_out: int,
    tokens_total: int,
    egress_level: int,
) -> dict[str, object]:
    return {
        "diagnostic_code": diagnostic_code,
        "stage": stage,
        "duration_ms": duration_ms,
        "provider_duration_ms": provider_duration_ms,
        "attempts": attempts,
        "provider": provider,
        "model": model,
        "reasoning_mode": reasoning_mode,
        "table_count": table_count,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tokens_total": tokens_total,
        "egress_level": egress_level,
    }
```

Call `audit("success", detail)` once on success or `audit("failed", detail)` once on final failure. Metadata/disabled early exits continue to audit only their safe diagnostic and stage.

- [ ] **Step 5: Run contract and Gateway regression tests**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/contract/test_api.py tests/unit/test_ai_gateway.py tests/unit/test_ai_sql_assistant.py -q -k "ai_sql or openai_compatible or reasoning"
& .\.venv\Scripts\ruff.exe check app/api/routes/core.py tests/contract/test_api.py
& .\.venv\Scripts\mypy.exe app/api app/services/ai
```

Expected: all selected tests and checks pass.

- [ ] **Step 6: Commit Task 4**

```powershell
git add app/api/routes/core.py tests/contract/test_api.py
git commit -m "feat(ai): validate and repair generated SQL" -m "Co-Authored-By: OpenAI Codex <codex@openai.com>"
```

---

### Task 5: Build the right-side AI SQL assistant panel

**Files:**
- Modify: `frontend/src/api/ai.ts:1-50`
- Create: `frontend/src/components/AiSqlAssistantPanel.vue`
- Modify: `frontend/src/i18n/en.ts:292-315`
- Modify: `frontend/src/i18n/zh-CN.ts:290-312`
- Modify: `frontend/tests/sql-workspace-w2.spec.ts`

**Interfaces:**
- Consumes: Task 3 candidate endpoint and Task 4 additive generation response.
- Produces: `AiSqlAssistantPanel` with props `datasourceId`, `editorSql`, `open`; emits `apply` and `close`.

- [ ] **Step 1: Write failing Playwright tests for the panel flow**

Add to `frontend/tests/sql-workspace-w2.spec.ts`:

```typescript
test('AI assistant confirms tables, previews, and applies without executing', async ({ page }) => {
  const { patches } = await mockBase(page)
  let executeCalls = 0
  await page.route('**/api/sql/execute', (r) => {
    executeCalls += 1
    return json(r, 500, { error: 'must_not_execute' })
  })
  await page.route('**/api/datasources/ds-1/ai/sql-table-candidates', (r) =>
    json(r, 200, {
      candidates: [
        { schema_name: 'app', table_name: 'users', matched_by: ['table_name'] },
        { schema_name: 'app', table_name: 'orders', matched_by: ['column_name'] },
      ],
      truncated: false,
    }),
  )
  await page.route('**/api/datasources/ds-1/ai/sql-generate', (r) =>
    json(r, 200, {
      ok: true,
      sql: 'SELECT id, name FROM app.users',
      explanation: null,
      provider: 'mock',
      model: 'mock-model',
      error: null,
      egress_level: 2,
      tables_used: ['app.users'],
      truncated: false,
      stage: 'validated',
      diagnostic_code: null,
      attempts: 1,
      reasoning_mode: 'disabled',
      validation: { readonly: 'passed', tables: 'passed', columns: 'passed', warnings: [] },
      request_id: 'request-1',
    }),
  )

  await page.goto('/projects/project-1/sql')
  await page.getByRole('button', { name: 'AI Generate' }).click()
  await expect(page.getByRole('complementary', { name: 'AI SQL Assistant' })).toBeVisible()
  await page.getByLabel('Query request').fill('list user names')
  await page.getByRole('button', { name: 'Recommend tables' }).click()
  await expect(page.getByRole('checkbox', { name: 'app.users' })).toBeChecked()
  await page.getByRole('button', { name: 'Generate preview' }).click()
  await expect(page.getByText('SELECT id, name FROM app.users', { exact: true })).toBeVisible()
  expect(await page.locator('.monaco-editor').innerText()).not.toContain('id, name')
  await page.getByRole('button', { name: 'Apply to editor' }).click()
  await expect.poll(() => patches.some((p) => JSON.stringify(p).includes('id, name'))).toBeTruthy()
  expect(executeCalls).toBe(0)
  expectNoConsoleErrors()
})


test('AI assistant revises only the current draft and shows diagnostic guidance', async ({ page }) => {
  await mockBase(page)
  const requests: Record<string, unknown>[] = []
  await page.route('**/api/datasources/ds-1/ai/sql-table-candidates', (r) =>
    json(r, 200, {
      candidates: [{ schema_name: 'app', table_name: 'users', matched_by: ['table_name'] }],
      truncated: false,
    }),
  )
  await page.route('**/api/datasources/ds-1/ai/sql-generate', (r) => {
    requests.push(r.request().postDataJSON())
    if (requests.length === 1) {
      return json(r, 200, {
        ok: true, sql: 'SELECT id FROM app.users', explanation: null, provider: 'mock', model: 'm',
        error: null, egress_level: 2, tables_used: ['app.users'], truncated: false,
        stage: 'validated', diagnostic_code: null, attempts: 1, reasoning_mode: 'disabled',
        validation: { readonly: 'passed', tables: 'passed', columns: 'passed', warnings: [] },
        request_id: 'r1',
      })
    }
    return json(r, 200, {
      ok: false, sql: null, explanation: null, provider: 'mock', model: 'm',
      error: 'provider_output_truncated', egress_level: 3, tables_used: [], truncated: true,
      stage: 'failed', diagnostic_code: 'provider_output_truncated', attempts: 2,
      reasoning_mode: 'enabled', validation: null, request_id: 'diag-1',
    })
  })

  await page.goto('/projects/project-1/sql')
  await page.getByRole('button', { name: 'AI Generate' }).click()
  await page.getByLabel('Query request').fill('list users')
  await page.getByRole('button', { name: 'Recommend tables' }).click()
  await page.getByRole('button', { name: 'Generate preview' }).click()
  await page.getByLabel('Revision request').fill('add a date filter')
  await page.getByRole('button', { name: 'Revise preview' }).click()

  expect(requests[1]).toMatchObject({
    candidate_sql: 'SELECT id FROM app.users',
    revision_instruction: 'add a date filter',
  })
  await expect(page.getByText(/output was truncated/i)).toBeVisible()
  await expect(page.getByText('diag-1')).toBeVisible()
  expectNoConsoleErrors()
})
```

- [ ] **Step 2: Run the Playwright tests and verify RED**

Run:

```powershell
Push-Location frontend
npm run build
npm run test:e2e -- sql-workspace-w2.spec.ts --grep "AI assistant"
Pop-Location
```

Expected: tests fail because the current implementation opens a modal and has no candidate or preview flow.

- [ ] **Step 3: Add typed frontend API functions**

In `frontend/src/api/ai.ts`, add exact interfaces:

```typescript
export type SqlValidationState = 'passed' | 'failed' | 'partial'
export type SqlDiagnosticCode =
  | 'provider_auth_failed' | 'provider_rate_limited' | 'provider_timeout'
  | 'provider_unreachable' | 'provider_reasoning_only' | 'provider_output_truncated'
  | 'provider_invalid_response' | 'metadata_probe_failed' | 'ai_egress_blocked'
  | 'sql_parse_failed' | 'sql_not_readonly' | 'sql_unknown_table' | 'sql_unknown_column'

export interface SqlTableCandidate {
  schema_name: string | null
  table_name: string
  matched_by: Array<'editor_reference' | 'table_name' | 'column_name'>
}

export interface SqlTableCandidatesResponse {
  candidates: SqlTableCandidate[]
  truncated: boolean
}

export interface SqlGenerateValidation {
  readonly: SqlValidationState
  tables: SqlValidationState
  columns: SqlValidationState
  warnings: string[]
}
```

Replace the request and response interfaces with the additive forms below, then add the
candidate function:

```typescript
export interface SqlGenerateRequest {
  natural_language: string
  schema_name?: string | null
  table_names?: string[]
  candidate_sql?: string | null
  revision_instruction?: string | null
}

export interface SqlGenerateResponse {
  ok: boolean
  sql: string | null
  explanation: string | null
  provider: string | null
  model: string | null
  error: string | null
  egress_level: number
  tables_used: string[]
  truncated: boolean
  stage: 'failed' | 'validated'
  diagnostic_code: SqlDiagnosticCode | null
  attempts: number
  reasoning_mode: 'disabled' | 'enabled' | null
  validation: SqlGenerateValidation | null
  request_id: string | null
}

export function suggestSqlTables(
  datasourceId: string,
  req: { natural_language: string; schema_name?: string | null; editor_sql?: string | null },
): Promise<SqlTableCandidatesResponse> {
  return apiClient.post(
    `/datasources/${encodeURIComponent(datasourceId)}/ai/sql-table-candidates`,
    {
      natural_language: req.natural_language,
      schema_name: req.schema_name ?? null,
      editor_sql: req.editor_sql ?? null,
    },
  )
}
```

In `generateSql`, include the two revision fields in the request body:

```typescript
      candidate_sql: req.candidate_sql ?? null,
      revision_instruction: req.revision_instruction ?? null,
```

- [ ] **Step 4: Implement `AiSqlAssistantPanel.vue`**

Create the component with this complete state and request flow; use the repository's existing
`chrome-*` utility classes for the shown class names:

```vue
<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  generateSql,
  suggestSqlTables,
  type SqlDiagnosticCode,
  type SqlGenerateResponse,
  type SqlTableCandidate,
} from '../api/ai'
import { ApiError } from '../api/types'

const props = defineProps<{ open: boolean; datasourceId: string; editorSql: string }>()
const emit = defineEmits<{ apply: [sql: string]; close: [] }>()
const { t } = useI18n()

const prompt = ref('')
const candidates = ref<SqlTableCandidate[]>([])
const selected = ref(new Set<string>())
const preview = ref<SqlGenerateResponse | null>(null)
const revision = ref('')
const busy = ref<'candidates' | 'generate' | 'revise' | ''>('')
const errorCode = ref<SqlDiagnosticCode | null>(null)
const localErrorKey = ref<string | null>(null)
const diagnosticId = ref<string | null>(null)

const selectedCandidates = computed(() =>
  candidates.value.filter((item) => selected.value.has(candidateKey(item))),
)

function candidateKey(item: SqlTableCandidate): string {
  return item.schema_name ? `${item.schema_name}.${item.table_name}` : item.table_name
}

function reset(): void {
  prompt.value = ''
  candidates.value = []
  selected.value = new Set()
  preview.value = null
  revision.value = ''
  busy.value = ''
  errorCode.value = null
  localErrorKey.value = null
  diagnosticId.value = null
}

function clearError(): void {
  errorCode.value = null
  localErrorKey.value = null
  diagnosticId.value = null
}

function toggleCandidate(item: SqlTableCandidate, checked: boolean): void {
  const next = new Set(selected.value)
  if (checked) next.add(candidateKey(item))
  else next.delete(candidateKey(item))
  selected.value = next
}

function onCandidateChange(item: SqlTableCandidate, event: Event): void {
  toggleCandidate(item, (event.target as HTMLInputElement).checked)
}

async function recommend(): Promise<void> {
  if (!prompt.value.trim() || !props.datasourceId) return
  clearError()
  busy.value = 'candidates'
  try {
    const response = await suggestSqlTables(props.datasourceId, {
      natural_language: prompt.value.trim(),
      editor_sql: props.editorSql || null,
    })
    candidates.value = response.candidates
    selected.value = new Set(response.candidates.map(candidateKey))
    if (response.candidates.length === 0) localErrorKey.value = 'sql.ai_no_tables'
  } catch (error) {
    if (error instanceof ApiError && error.code === 'metadata_probe_failed') {
      errorCode.value = 'metadata_probe_failed'
    } else {
      localErrorKey.value = 'sql.ai_candidates_failed'
    }
  } finally {
    busy.value = ''
  }
}

async function requestPreview(isRevision: boolean): Promise<void> {
  clearError()
  const chosen = selectedCandidates.value
  if (chosen.length === 0) {
    localErrorKey.value = 'sql.ai_no_tables'
    return
  }
  const schemas = [...new Set(chosen.map((item) => item.schema_name))]
  if (schemas.length !== 1) {
    localErrorKey.value = 'sql.ai_tables_one_schema'
    return
  }
  if (isRevision && (!preview.value?.sql || !revision.value.trim())) return
  busy.value = isRevision ? 'revise' : 'generate'
  try {
    const response = await generateSql(props.datasourceId, {
      natural_language: prompt.value.trim(),
      schema_name: schemas[0],
      table_names: chosen.map((item) => item.table_name),
      candidate_sql: isRevision ? preview.value?.sql : undefined,
      revision_instruction: isRevision ? revision.value.trim() : undefined,
    })
    diagnosticId.value = response.request_id
    if (!response.ok || !response.sql) {
      if (!isRevision) preview.value = null
      errorCode.value = response.diagnostic_code ?? 'provider_invalid_response'
      return
    }
    preview.value = response
    revision.value = ''
  } catch (error) {
    errorCode.value =
      error instanceof ApiError && error.code === 'metadata_probe_failed'
        ? 'metadata_probe_failed'
        : 'provider_invalid_response'
  } finally {
    busy.value = ''
  }
}

function applyPreview(): void {
  if (preview.value?.sql) emit('apply', preview.value.sql)
}

async function copyPreview(): Promise<void> {
  if (preview.value?.sql) await navigator.clipboard.writeText(preview.value.sql)
}

watch(() => props.datasourceId, reset)
</script>

<template>
  <aside
    v-if="open"
    role="complementary"
    :aria-label="t('sql.ai_assistant_title')"
    class="fixed inset-y-0 right-0 z-40 w-[min(410px,100vw)] md:static md:z-auto md:w-[410px]
             shrink-0 min-w-0 max-w-full border-l chrome-border chrome-bg-panel
             flex flex-col overflow-hidden"
  >
    <header class="flex items-center justify-between border-b chrome-border px-4 py-3">
      <h2 class="font-semibold chrome-text-heading">{{ t('sql.ai_assistant_title') }}</h2>
      <button type="button" class="chrome-btn-ghost" @click="emit('close')">×</button>
    </header>
    <div class="flex-1 overflow-y-auto p-4 space-y-4">
      <label class="block text-xs chrome-text-muted">
        {{ t('sql.ai_prompt_label') }}
        <textarea v-model="prompt" :aria-label="t('sql.ai_prompt_label')" class="chrome-input mt-1 w-full min-h-24" />
      </label>
      <button
        type="button"
        class="chrome-btn-secondary"
        :disabled="busy !== '' || !prompt.trim() || !datasourceId"
        @click="recommend"
      >
        {{ busy === 'candidates' ? t('sql.ai_recommending_tables') : t('sql.ai_recommend_tables') }}
      </button>

      <fieldset v-if="candidates.length" class="space-y-2">
        <legend class="text-xs chrome-text-muted">{{ t('sql.ai_confirm_tables') }}</legend>
        <label v-for="item in candidates" :key="candidateKey(item)" class="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            :aria-label="candidateKey(item)"
            :checked="selected.has(candidateKey(item))"
            @change="onCandidateChange(item, $event)"
          />
          <span class="font-mono">{{ candidateKey(item) }}</span>
        </label>
      </fieldset>

      <button
        type="button"
        class="chrome-btn-primary"
        :disabled="busy !== '' || selectedCandidates.length === 0"
        @click="requestPreview(false)"
      >
        {{ busy === 'generate' ? t('sql.ai_generating') : t('sql.ai_generate_preview') }}
      </button>

      <section v-if="preview?.sql" class="space-y-3">
        <pre class="max-w-full overflow-auto rounded-card chrome-bg-elevated p-3 text-xs"><code>{{ preview.sql }}</code></pre>
        <div v-if="preview.validation" class="flex flex-wrap gap-2 text-xs">
          <span>{{ t('sql.ai_validation_readonly') }}: {{ preview.validation.readonly }}</span>
          <span>{{ t('sql.ai_validation_tables') }}: {{ preview.validation.tables }}</span>
          <span>{{ t('sql.ai_validation_columns') }}: {{ preview.validation.columns }}</span>
        </div>
        <div class="flex gap-2">
          <button type="button" class="chrome-btn-primary" @click="applyPreview">{{ t('sql.ai_apply_editor') }}</button>
          <button type="button" class="chrome-btn-secondary" @click="copyPreview">{{ t('sql.ai_copy_sql') }}</button>
        </div>
        <label class="block text-xs chrome-text-muted">
          {{ t('sql.ai_revision_label') }}
          <textarea
            v-model="revision"
            :aria-label="t('sql.ai_revision_label')"
            :placeholder="t('sql.ai_revision_placeholder')"
            class="chrome-input mt-1 w-full min-h-16"
          />
        </label>
        <button
          type="button"
          class="chrome-btn-secondary"
          :disabled="busy !== '' || !revision.trim()"
          @click="requestPreview(true)"
        >
          {{ busy === 'revise' ? t('sql.ai_generating') : t('sql.ai_revise_preview') }}
        </button>
      </section>

      <p v-if="localErrorKey" class="text-sm text-red-600">{{ t(localErrorKey) }}</p>
      <div v-if="errorCode" class="rounded-card border border-red-300 p-3 text-sm text-red-700">
        <p>{{ t(`sql.ai_diagnostic.${errorCode}`) }}</p>
        <p v-if="diagnosticId" class="mt-1 font-mono text-xs">
          {{ t('sql.ai_diagnostic_id', { id: diagnosticId }) }}
        </p>
      </div>
    </div>
  </aside>
</template>
```

- [ ] **Step 5: Add localized panel and diagnostic copy**

Under the existing `sql` i18n object in both files, add matching keys:

```typescript
// en.ts values
ai_assistant_title: 'AI SQL Assistant',
ai_recommend_tables: 'Recommend tables',
ai_recommending_tables: 'Finding tables…',
ai_confirm_tables: 'Confirm table scope',
ai_generate_preview: 'Generate preview',
ai_apply_editor: 'Apply to editor',
ai_copy_sql: 'Copy SQL',
ai_revision_label: 'Revision request',
ai_revision_placeholder: 'e.g. add a date filter',
ai_revise_preview: 'Revise preview',
ai_tables_one_schema: 'Select tables from one schema for this generation.',
ai_no_tables: 'Confirm at least one table before generating.',
ai_candidates_failed: 'Unable to recommend tables. Check datasource metadata and try again.',
ai_diagnostic_id: 'Diagnostic ID: {id}',
ai_validation_readonly: 'Read-only',
ai_validation_tables: 'Tables',
ai_validation_columns: 'Columns',
ai_diagnostic: {
  provider_auth_failed: 'Provider authentication failed. Ask an administrator to verify the AI configuration.',
  provider_rate_limited: 'The provider rate limit was reached. Wait and try again.',
  provider_timeout: 'The provider timed out. Try again later.',
  provider_unreachable: 'The provider could not be reached. Check network access and provider status.',
  provider_reasoning_only: 'The provider returned reasoning but no final SQL.',
  provider_output_truncated: 'The provider output was truncated even after one repair attempt.',
  provider_invalid_response: 'The provider returned an unsupported response.',
  metadata_probe_failed: 'Unable to read the datasource schema. Test the connection and refresh metadata.',
  ai_egress_blocked: 'The configured AI egress policy blocked this request.',
  sql_parse_failed: 'The generated SQL could not be parsed.',
  sql_not_readonly: 'The generated statement was not read-only and was rejected.',
  sql_unknown_table: 'The generated SQL referenced a table outside the confirmed scope.',
  sql_unknown_column: 'The generated SQL referenced a column outside the confirmed schema.',
},
```

Add the matching Chinese keys exactly:

```typescript
ai_assistant_title: 'AI SQL 助手',
ai_recommend_tables: '推荐数据表',
ai_recommending_tables: '正在查找数据表…',
ai_confirm_tables: '确认数据表范围',
ai_generate_preview: '生成预览',
ai_apply_editor: '应用到编辑器',
ai_copy_sql: '复制 SQL',
ai_revision_label: '修改要求',
ai_revision_placeholder: '例如：增加日期过滤条件',
ai_revise_preview: '修改预览',
ai_tables_one_schema: '本次生成只能选择同一个 schema 下的数据表。',
ai_no_tables: '生成前请至少确认一张数据表。',
ai_candidates_failed: '无法推荐数据表，请检查数据源元数据后重试。',
ai_diagnostic_id: '诊断编号：{id}',
ai_validation_readonly: '只读',
ai_validation_tables: '数据表',
ai_validation_columns: '字段',
ai_diagnostic: {
  provider_auth_failed: 'AI 服务鉴权失败，请联系管理员检查 AI 配置。',
  provider_rate_limited: 'AI 服务当前请求过多，请稍后重试。',
  provider_timeout: 'AI 服务响应超时，请稍后重试。',
  provider_unreachable: '无法连接 AI 服务，请检查网络和服务状态。',
  provider_reasoning_only: '模型返回了推理过程，但没有返回最终 SQL。',
  provider_output_truncated: '自动修复一次后，模型输出仍被截断。',
  provider_invalid_response: 'AI 服务返回了不支持的响应格式。',
  metadata_probe_failed: '无法读取数据源结构，请先测试连接并刷新元数据。',
  ai_egress_blocked: '当前 AI 出站策略阻止了本次请求。',
  sql_parse_failed: '生成的 SQL 无法解析。',
  sql_not_readonly: '生成结果不是只读语句，已拒绝使用。',
  sql_unknown_table: '生成的 SQL 引用了确认范围外的数据表。',
  sql_unknown_column: '生成的 SQL 引用了真实结构中不存在的字段。',
},
```

Keep keys identical between locales.

- [ ] **Step 6: Run component-flow Playwright and typecheck**

Run:

```powershell
Push-Location frontend
npm run typecheck
npm run build
npm run test:e2e -- sql-workspace-w2.spec.ts --grep "AI assistant"
Pop-Location
```

Expected: both new tests pass, Console has no errors, and typecheck/build exit 0.

- [ ] **Step 7: Commit Task 5**

```powershell
git add frontend/src/api/ai.ts frontend/src/components/AiSqlAssistantPanel.vue frontend/src/i18n/en.ts frontend/src/i18n/zh-CN.ts frontend/tests/sql-workspace-w2.spec.ts
git commit -m "feat(ai): add SQL assistant preview panel" -m "Co-Authored-By: OpenAI Codex <codex@openai.com>"
```

---

### Task 6: Integrate the panel and contain workspace width

**Files:**
- Modify: `frontend/src/views/SqlWorkspaceView.vue:251-260, 1030-1070, 1644-1710, 1755-1770, 2064-2100`
- Modify: `frontend/src/views/AppShellLayout.vue:55-58`
- Modify: `frontend/tests/sql-workspace-w2.spec.ts`

**Interfaces:**
- Consumes: Task 5 `AiSqlAssistantPanel`.
- Produces: panel open/apply/reset integration and a viewport-width regression guarantee.

- [ ] **Step 1: Write the failing long-SQL layout test**

Add:

```typescript
test('long SQL stays inside Monaco without widening the document', async ({ page }) => {
  const longExpression = Array.from({ length: 120 }, (_, i) => `column_${i}`).join(' + ')
  await mockLicense(page)
  await page.route(/\/api\/datasources\?/, (r) => json(r, 200, [datasource()]))
  await page.route('**/api/sql/consoles', (r) =>
    json(r, 200, [consoleRow({ sql: `SELECT ${longExpression} FROM users` })]),
  )

  await page.goto('/projects/project-1/sql')
  await expect(page.getByRole('button', { name: 'Run' })).toBeVisible()
  await expect(page.getByRole('combobox').first()).toBeVisible()
  const widths = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth,
  }))
  expect(widths.scroll).toBeLessThanOrEqual(widths.client + 1)
  expectNoConsoleErrors()
})
```

- [ ] **Step 2: Run the layout test and verify RED**

Run:

```powershell
Push-Location frontend
npm run build
npm run test:e2e -- sql-workspace-w2.spec.ts --grep "long SQL"
Pop-Location
```

Expected: width assertion fails because the document scroll width exceeds the viewport.

- [ ] **Step 3: Replace modal integration with panel integration**

In `SqlWorkspaceView.vue`:

- Import `AiSqlAssistantPanel`.
- Replace the old modal refs with `const aiPanelOpen = ref(false)`.
- Delete `openAiModal`, `onGenerateSql`, and the AI generation `<Modal>` block.
- Change the toolbar button to `@click="aiPanelOpen = true"`.
- Add:

```typescript
function applyGeneratedSql(sql: string): void {
  editorSql.value = sql
}

watch(selectedDsId, (value, previous) => {
  if (value !== previous) aiPanelOpen.value = false
  // retain the existing datasource/console synchronization body below this guard
})
```

Render the editor and panel as siblings inside a bounded flex row:

```vue
<div class="flex-1 min-h-0 min-w-0 max-w-full flex overflow-hidden">
  <div class="flex-1 min-w-0 max-w-full flex flex-col overflow-hidden">
    <!-- existing toolbar, Monaco, and result content -->
  </div>
  <AiSqlAssistantPanel
    :open="aiPanelOpen"
    :datasource-id="selectedDsId"
    :editor-sql="editorSql"
    @apply="applyGeneratedSql"
    @close="aiPanelOpen = false"
  />
</div>
```

Do not move result fetching, execution, console autosave, or slow-SQL diagnosis logic.

- [ ] **Step 4: Apply the surgical width constraints**

Change `AppShellLayout.vue`:

```vue
<main class="flex-1 min-w-0 max-w-full overflow-y-auto overflow-x-hidden">
  <RouterView />
</main>
```

In `SqlWorkspaceView.vue`, ensure the root, main content column, editor flex row, and Monaco host each include `min-w-0 max-w-full`; apply `overflow-hidden` at the editor host. Do not add a global CSS rule and do not set `overflow-x-hidden` on `html` or `body`.

- [ ] **Step 5: Run panel, layout, and existing workspace tests**

Run:

```powershell
Push-Location frontend
npm run typecheck
npm run build
npm run test:e2e -- sql-workspace-w2.spec.ts
Pop-Location
```

Expected: all SQL workspace tests pass; the document width is within 1px of the viewport; Console is error-free.

- [ ] **Step 6: Commit Task 6**

```powershell
git add frontend/src/views/SqlWorkspaceView.vue frontend/src/views/AppShellLayout.vue frontend/tests/sql-workspace-w2.spec.ts
git commit -m "fix(sql): contain editor and assistant width" -m "Co-Authored-By: OpenAI Codex <codex@openai.com>"
```

---

### Task 7: Full verification, real-browser evidence, and Draft PR

**Files:**
- Verify all changed files; no production edits expected.

**Interfaces:**
- Consumes: Tasks 1-6.
- Produces: clean local evidence and a Draft PR ready for human review; no merge or D-drive deployment.

- [ ] **Step 1: Run backend quality gates**

```powershell
& .\.venv\Scripts\ruff.exe check app tests tools
& .\.venv\Scripts\ruff.exe format --check app tests tools
& .\.venv\Scripts\mypy.exe app
& .\.venv\Scripts\python.exe -m pytest -q
```

Expected: Ruff, format, and mypy exit 0; pytest reports no unexpected failures. Record expected skips/xfailed separately.

- [ ] **Step 2: Run frontend quality gates**

```powershell
Push-Location frontend
npm run typecheck
npm run build
npm run test:e2e -- sql-workspace-w2.spec.ts
Pop-Location
```

Expected: all commands exit 0. Existing duplicate-i18n-key warnings must not increase; if Task 5 touches the same object and can remove only the exact duplicate keys without semantic change, do so in that task with a focused assertion, otherwise report them as baseline warnings.

- [ ] **Step 3: Run redline and secret checks**

```powershell
& .\.venv\Scripts\ruff.exe check app tests tools
& .\.venv\Scripts\ruff.exe format --check app tests tools
& .\.venv\Scripts\sg.exe scan --config tools/lint/sgconfig.yml
& .\.venv\Scripts\python.exe -m pytest tests/unit/test_redlines.py tests/unit/test_redaction.py tests/unit/test_models.py -q
git diff origin/main...HEAD --check
git status --short
git diff origin/main...HEAD --unified=0 | Select-String -Pattern '(password|token|secret|private_key|api_key|cookie|session)' -CaseSensitive:$false
```

Expected: redlines pass; only planned files are changed; any keyword hits are inspected and are type names, tests with explicitly fake values, or prohibition text—not credentials. If `gitleaks` is installed, also run `gitleaks detect --no-banner --redact -c .gitleaks.toml`.

- [ ] **Step 4: Start the isolated app and verify in a real browser**

Use the project launcher and a non-production test runtime. Open the SQL workspace in a real browser and verify:

1. Expected route content renders and Monaco is visible.
2. Console has no red errors or Vue/Vite overlay.
3. Opening the assistant shows the right panel; closing restores editor width.
4. With a long SQL line, `scrollWidth <= clientWidth + 1`, datasource selector and Run button remain visible.
5. Candidate and generation requests use the real local backend. If no datasource connection is available, verify the structured metadata error state and explicitly mark the connected-schema path unverified.
6. Do not make a real Provider call until the user separately authorizes sending the confirmed schema and prompt. Without that authorization, use the mock Provider only and mark the external call unverified.

Capture the route URL, Console status, key DOM text, and relevant HTTP statuses without printing auth tokens, datasource connection values, schema payloads, or SQL text from real data.

- [ ] **Step 5: Request code review before publishing**

Invoke `superpowers:requesting-code-review`, review every finding against the spec, and fix only confirmed issues. Rerun the focused tests for every modified area.

- [ ] **Step 6: Push and open a Draft PR**

```powershell
git push -u origin agent/redesign-ai-sql-assistant
```

Open a Draft PR targeting `main`. The PR body must include root causes, additive API fields, one-retry boundary, privacy controls, local verification counts, real-browser evidence, baseline warnings, and any unverified real-datasource/Provider path. Do not include prompts, schema/table names, SQL, internal addresses, credentials, or screenshots containing real data.

- [ ] **Step 7: Wait for every CI check**

```powershell
$pr = gh pr view --json number --jq .number
gh pr checks $pr --repo allen-answer/dataOpsStudio_v2 --watch --interval 10
```

Expected: every required check is green. If any check fails, invoke `github:gh-fix-ci`, inspect the failing job logs, reproduce locally where possible, fix on the same branch, and wait again. Do not merge and do not deploy to D while checks are pending or failing.

---

## Completion Criteria

- User confirms tables before any schema is sent to AI.
- Simple one-table requests use disabled reasoning; complex/uncertain requests use enabled reasoning with the planned budgets.
- Reasoning-only, truncated, invalid SQL, unknown table, and unknown column responses can trigger only one repair.
- Authentication, rate-limit, timeout, unreachable, metadata, and egress errors are never retried.
- Provider reasoning text and raw bodies are absent from API responses, audit, logs, snapshots, and diffs.
- Generated SQL is validated and previewed; only an explicit Apply updates Monaco; no execute request is made.
- The panel supports one revision of the current draft and resets safely on datasource change.
- Long SQL does not widen the page beyond the viewport.
- Backend, frontend, redline, browser, and CI evidence is recorded; any real integration not run is explicitly identified.
