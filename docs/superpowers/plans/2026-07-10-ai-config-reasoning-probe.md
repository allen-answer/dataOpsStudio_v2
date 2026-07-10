# AI Config Reasoning Probe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent the admin AI connection test from falsely rejecting reasoning models whose first eight output tokens contain only reasoning content.

**Architecture:** Keep the OpenAI-compatible provider parser strict about requiring non-empty final content. Change only the admin connectivity probe budget from 8 to 256 tokens, with a route-level regression test that captures the exact `AiOptions` passed to the gateway.

**Tech Stack:** Python 3.12, FastAPI, pytest, ruff, mypy, repository redline checks.

## Global Constraints

- Modify only the admin connectivity probe and its unit test; do not change normal AI calls, provider parsing, egress policy, or secret handling.
- Never log or commit API keys, credentials, endpoints from the diagnosed environment, or other sensitive values.
- Work on `agent/fix-ai-config-reasoning-probe`; push a Draft PR and never push directly to `main` or merge the PR.
- Use TDD: observe the regression test fail with 8 before changing production code.

---

### Task 1: Raise the Admin Connectivity Probe Budget

**Files:**
- Modify: `tests/unit/test_admin_routes.py:183-201`
- Modify: `app/api/routes/admin.py:540-545`

**Interfaces:**
- Consumes: `build_gateway_from_runtime_config(runtime)` and `DefaultAiGateway.complete(prompt, context, options)`.
- Produces: the existing `POST /api/admin/ai-config/test` response contract, now invoked with `AiOptions.max_tokens == 256`.

- [ ] **Step 1: Write the failing route-level regression test**

Add imports for `pytest`, `AiContext`, `AiOptions`, and `AiResponse`. Replace the existing mock-provider test with a capturing gateway:

```python
def test_admin_ai_config_test_uses_reasoning_safe_budget_and_audits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_max_tokens: list[int | None] = []

    class _Gateway:
        def complete(
            self,
            prompt: str,
            context: AiContext,
            options: AiOptions,
        ) -> AiResponse:
            del prompt, context
            captured_max_tokens.append(options.max_tokens)
            return AiResponse(
                content="pong",
                tokens_in=1,
                tokens_out=1,
                provider="mock",
                model="mock-model",
            )

    monkeypatch.setattr(
        "app.api.routes.admin.build_gateway_from_runtime_config",
        lambda runtime: _Gateway(),
    )
    services = _AdminServices(_QueueEngine([[_ai_config_row(provider="mock")]]))
    app = create_app(services=cast(ApiServices, services))
    token = create_access_token(user_id="admin-1", role="admin", secret=services.jwt_secret)

    response = AsgiClient(app).post(
        "/api/admin/ai-config/test",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["provider"] == "mock"
    assert payload["error"] is None
    assert captured_max_tokens == [256]
    assert any(item["action"] == "admin_ai_config_test" for item in services.audits)
```

- [ ] **Step 2: Run the regression test and verify RED**

Run:

```text
uv run pytest tests/unit/test_admin_routes.py::test_admin_ai_config_test_uses_reasoning_safe_budget_and_audits -q
```

Expected: FAIL because `captured_max_tokens` is `[8]`, proving the test covers the diagnosed bug.

- [ ] **Step 3: Implement the minimal production change**

In `test_admin_ai_config`, change only the probe options:

```python
AiOptions(purpose="admin_ai_config_test", max_tokens=256),
```

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```text
uv run pytest tests/unit/test_admin_routes.py::test_admin_ai_config_test_uses_reasoning_safe_budget_and_audits tests/unit/test_ai_gateway.py -q
```

Expected: all selected tests PASS.

- [ ] **Step 5: Commit the behavior fix**

Stage only the two intended files and commit with a co-author trailer:

```text
git add tests/unit/test_admin_routes.py app/api/routes/admin.py
git commit -m "fix(ai): allow reasoning model connectivity probes" -m "Co-Authored-By: OpenAI Codex <codex@openai.com>"
```

### Task 2: Validate and Publish

**Files:**
- Verify: `app/api/routes/admin.py`
- Verify: `tests/unit/test_admin_routes.py`
- Verify: `tests/unit/test_ai_gateway.py`

**Interfaces:**
- Consumes: repository test, lint, typecheck, and redline commands.
- Produces: a pushed feature branch and Draft PR targeting `main`.

- [ ] **Step 1: Run relevant and repository checks**

Run:

```text
uv run pytest tests/unit/test_admin_routes.py tests/unit/test_ai_gateway.py -q
uv run ruff check app/api/routes/admin.py tests/unit/test_admin_routes.py
uv run ruff format --check app/api/routes/admin.py tests/unit/test_admin_routes.py
uv run mypy app
make check-redlines
```

Expected: every command exits 0. If `gitleaks` is unavailable locally, report that explicit gap; `make check-redlines` still runs the other checks and CI will enforce gitleaks.

- [ ] **Step 2: Inspect scope and sensitive-data risk**

Run:

```text
git status -sb
git diff main...HEAD --check
git diff main...HEAD --stat
git diff main...HEAD
```

Expected: only the design, plan, admin route, and admin route test are changed; no credentials, environment-specific endpoints, IP addresses, logs, dumps, screenshots, or temporary files appear.

- [ ] **Step 3: Commit the implementation plan**

Stage this plan only and commit with a co-author trailer:

```text
git add docs/superpowers/plans/2026-07-10-ai-config-reasoning-probe.md
git commit -m "docs: plan AI reasoning probe fix" -m "Co-Authored-By: OpenAI Codex <codex@openai.com>"
```

- [ ] **Step 4: Push the feature branch**

Run:

```text
git push -u origin agent/fix-ai-config-reasoning-probe
```

Expected: the branch is created on `origin` with upstream tracking.

- [ ] **Step 5: Open a Draft PR**

Create a Draft PR targeting `main` with title `fix(ai): allow reasoning model connectivity probes`. The body must describe the eight-token root cause, the 256-token minimal fix, the unchanged provider parser, and all validation commands/results.

Expected: GitHub returns a Draft PR URL; do not merge it.
