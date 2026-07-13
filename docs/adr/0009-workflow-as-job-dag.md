# ADR 0009: Workflow as Job DAG

Status: Accepted
Date: 2026-07-02 (backfilled; the core "Workflow = Job DAG" decision dates back to the 2.0 charter / tech design v0.3.x §2.8, and was referenced by number since then. The four 2.4.0 first-release decisions below were confirmed by the owner on 2026-07-02.)

## Context

DataOpsStudio 2.4.0 introduces the Workflow capability domain. The core constraint from the charter stands: Workflow is not a low-rent Airflow. It only orchestrates DataOpsStudio built-in jobs.

The 2.0 job infrastructure already anticipates this: `Job.kind` includes `workflow_run`, every job carries `parent_workflow_run_id`, and redline R7 (`ALLOWED_WORKFLOW_NODE_KINDS` whitelist) has had consistency unit tests active in CI since 2.0.0 (`tests/unit/test_redlines.py`).

Lessons from 1.x (`docs/legacy/V1_AS_IS.md` §5.2):

- The 1.x engine (`services/workflow_engine.py`) had 5 node types: `params` / `compare` / `lineage` / `http` / `excel_export`. Topological execution by `depends_on`, single-node failure bypassed pure downstream as SKIPPED, `when:` expressions for conditional skip. These execution semantics proved useful in daily operation and are worth keeping.
- The 1.x `http` node was removed in 2.0 and stays removed. An arbitrary-HTTP node holds secrets and calls arbitrary URLs from inside the data platform — an exfiltration and SSRF vector that contradicts R2/R7. Its one legitimate use (notifications) is replaced by a dedicated `notify` node restricted to existing configured Workflow notification targets (webhook, WeCom, or email).
- 1.x scheduling used APScheduler with a polling-loop fallback. 2.0 does not want a new scheduler dependency or a new process.

## Decision

**Workflow is a Job DAG.** A WorkflowRun is itself a job (`kind=workflow_run`); every DAG node executes as a child job pointing back via `parent_workflow_run_id`. Scheduling, audit, heartbeat, retry accounting, and cancel all reuse the existing PG job queue — no second execution engine.

Four first-release (2.4.0) decisions, confirmed 2026-07-02:

### 1. Scheduling: in-process cron tick, no scheduler process

A lightweight background thread inside the API process ticks periodically, scans `workflows.schedule_cron`, and enqueues a `workflow_run` job for each schedule that is due. There is no independent scheduler process and no APScheduler dependency.

- Rationale: the deployment topology stays API + worker + PG across all three form factors (portable launcher unchanged). The tick only enqueues; execution stays on workers. A single API process is the supported topology, so no distributed scheduler lock is needed.
- Deferred: independent scheduler process / HA multi-instance dedup; catch-up/backfill semantics for schedules missed while the API was down (first release fires a due schedule once at the next tick, no backfill).

### 2. RetryPolicy: simple fixed-interval version

Per-node `max_retries` (0–5) plus a fixed interval `backoff_seconds`. A node that does not set a policy inherits the global `job_default_max_retries`.

- Rationale: reuses the existing job `retry_count` machinery; fixed interval is enough for the dominant failure mode (transient datasource errors) and is trivially explainable in the UI.
- Deferred: exponential backoff, jitter, error-class-conditional retry (`retry_on`).

### 3. Branch semantics: `when` bypass only; `branch` reserved

First release keeps the proven 1.x semantics: a node-level `when` condition expression — when false, the node and its pure downstream are bypassed as SKIPPED. `on_failure` supports only `abort` / `continue`. The `branch` node kind and the `on_failure: "branch"` value remain in the whitelist and type definitions, but the API rejects them at workflow creation; true branch semantics move to 2.4.x.

- Rationale: `when` + SKIPPED bypass covered real 1.x usage; explicit branch routing needs edge-condition design that should not block the first release.
- Deferred: `branch` node semantics and `on_failure: "branch"` routing (2.4.x).

### 4. First-release open node kinds: only the 5 implemented ones

Workflow creation accepts only node kinds whose job implementations already exist:

```
sql_query / sql_explain / compare_run / lineage_analyze / export_excel
```

`scenario_materialize` / `scenario_run_all` / `notify` / `sleep` / `branch` stay in `ALLOWED_WORKFLOW_NODE_KINDS` but are rejected at creation with `unsupported_node_kind`. This is deliberately distinct from the R7 *forbidden* semantics: forbidden kinds are never allowed on the 2.0 main line; unsupported kinds are whitelisted-but-not-yet-available and will be opened one by one as their dependencies land (scenario_* with Scenario Lab in 2.6.0; notify once configured Workflow notification targets exist for webhook, WeCom, or email).

- Rationale: keeps the R7 whitelist (and its CI consistency tests) stable while gating actual availability at the API layer.

## Node whitelist and forbidden list (R7)

The full whitelist stays at 10 kinds (contract §4 R7):

```python
{"sql_query", "sql_explain", "compare_run", "scenario_materialize",
 "scenario_run_all", "lineage_analyze", "export_excel", "notify", "sleep", "branch"}
```

Forbidden — never on the 2.0 main line: shell / system command, Python script / arbitrary code execution, arbitrary HTTP request (secrets + arbitrary URLs; see the 1.x `http` lesson above), arbitrary direct DDL/DML execution, browser automation. `workflow_run` itself is also excluded from the whitelist: no workflow nesting. `notify` only targets existing configured Workflow notification targets (webhook, WeCom, or email).

## Consequences

- One execution engine: cancel, audit, heartbeat/reaper, and result handling for workflow nodes are the existing job semantics for free; a WorkflowRun cancel cascades to its child jobs via `parent_workflow_run_id`.
- API restart delays scheduled triggers by at most one tick interval; missed windows are not backfilled in the first release.
- Creating a workflow with a forbidden kind and with an unsupported kind must fail with distinguishable errors (R7 violation vs `unsupported_node_kind`).
- R7 stops being dormant: 2.4.0 adds runtime validation (creation + enqueue) on top of the whitelist consistency tests already in CI.
- 2.4.x roadmap inside the domain: `branch` semantics, `notify` (configured webhook/WeCom/email targets), `sleep`; `scenario_*` opens with 2.6.0 Scenario Lab.

## 2.4.x completion addendum (2026-07-11)

The three intrinsic nodes deferred above are now opened without changing the R7 allowlist:
`branch`, `notify`, and `sleep`. The supported set therefore contains eight kinds; the two
`scenario_*` kinds remain unavailable until Scenario Lab.

- Edges are additive and backward compatible: `trigger=success|failure`, optional `when`, and
  `is_default`. Legacy edges remain ordinary success dependencies.
- A branch uses edge declaration order, chooses the first true condition, and otherwise chooses
  its single default. `on_failure=branch` uses the same rule on failure edges. Compensation never
  rewrites the original failed run to success.
- `${nodes.<node_id>.<field>}` exposes only scalar, kind-specific metadata from a topological
  ancestor. Result rows, arbitrary ResultRef metadata/URI, SQL, and SecretRef values stay closed.
- `notify` references existing configured Workflow notification targets (webhook/WeCom/email);
  arbitrary HTTP remains forbidden.
  `sleep` is a delayed queue job rather than a worker-blocking sleep.
- Cron expressions use one process-wide IANA scheduler timezone. An explicit
  `DATAOPS_SCHEDULER_TIMEZONE` value is validated at startup; otherwise the server local zone is
  resolved. Fire points are converted back to UTC before comparison, persistence, audit, and Job
  construction. Configuration changes require restart; per-workflow timezones and backfill remain
  out of scope.

## Non-Goals

- Not a general-purpose orchestrator: no cross-system operators, no dynamic DAG generation from code, no plugin node SDK.
- No independent scheduler process or HA scheduling in 2.4.0.
- This ADR does not decide the workflow definition storage schema details (tables/columns); those belong to the 2.4.0 implementation PRs under this decision.
