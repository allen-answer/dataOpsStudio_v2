"""Workflow DAG 推进纯逻辑单测(PR-4:拓扑波次 / when SKIPPED / abort / continue / retry)。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.domain.job import JobStatus
from app.domain.workflow import WorkflowEdge, WorkflowNode, WorkflowSpec
from app.domain.workflow_execution import (
    WorkflowChildJob,
    WorkflowNodeExecStatus,
    _topological_nodes,
    plan_workflow_step,
)

_NOW = datetime(2026, 7, 2, 8, 0, 0, tzinfo=UTC)


def _spec(
    nodes: list[dict[str, Any]],
    edges: list[tuple[str, str]] | None = None,
) -> WorkflowSpec:
    return WorkflowSpec.model_validate(
        {
            "nodes": [{"job_kind": "sql_query", "timeout_seconds": 60, **node} for node in nodes],
            "edges": [{"source": source, "target": target} for source, target in edges or []],
        }
    )


def _child(
    node_id: str,
    status: JobStatus,
    *,
    retry_count: int = 0,
    finished_at: datetime | None = None,
    error: str | None = None,
) -> WorkflowChildJob:
    return WorkflowChildJob(
        node_id=node_id,
        job_id=f"job-{node_id}",
        status=status,
        retry_count=retry_count,
        finished_at=finished_at,
        error=error,
    )


# ── 拓扑波次 ──────────────────────────────────────────────────────────────────


def test_first_step_enqueues_only_root_nodes() -> None:
    spec = _spec([{"id": "a"}, {"id": "b"}, {"id": "c"}], [("a", "b"), ("b", "c")])

    plan = plan_workflow_step(spec, {}, now=_NOW)

    assert plan.enqueue_node_ids == ("a",)
    assert plan.run_status is None
    assert plan.node_states["b"].status is WorkflowNodeExecStatus.WAITING
    assert plan.node_states["c"].status is WorkflowNodeExecStatus.WAITING


def test_downstream_becomes_ready_only_after_all_upstreams_succeed() -> None:
    spec = _spec(
        [{"id": "a"}, {"id": "b"}, {"id": "join"}],
        [("a", "join"), ("b", "join")],
    )
    children = {
        "a": _child("a", JobStatus.SUCCESS),
        "b": _child("b", JobStatus.RUNNING),
    }

    plan = plan_workflow_step(spec, children, now=_NOW)

    assert plan.enqueue_node_ids == ()
    assert plan.node_states["join"].status is WorkflowNodeExecStatus.WAITING

    children["b"] = _child("b", JobStatus.SUCCESS)
    plan = plan_workflow_step(spec, children, now=_NOW)
    assert plan.enqueue_node_ids == ("join",)


def test_parallel_branches_enqueue_together() -> None:
    spec = _spec(
        [{"id": "root"}, {"id": "left"}, {"id": "right"}],
        [("root", "left"), ("root", "right")],
    )
    children = {"root": _child("root", JobStatus.SUCCESS)}

    plan = plan_workflow_step(spec, children, now=_NOW)

    assert plan.enqueue_node_ids == ("left", "right")


def test_all_nodes_success_finishes_run_success() -> None:
    spec = _spec([{"id": "a"}, {"id": "b"}], [("a", "b")])
    children = {
        "a": _child("a", JobStatus.SUCCESS),
        "b": _child("b", JobStatus.SUCCESS),
    }

    plan = plan_workflow_step(spec, children, now=_NOW)

    assert plan.run_status is JobStatus.SUCCESS
    assert plan.run_error is None


# ── when:假 → 本节点及纯下游 SKIPPED ────────────────────────────────────────


def test_when_false_skips_node_and_pure_downstream_but_not_siblings() -> None:
    spec = _spec(
        [
            {"id": "root"},
            {"id": "gated", "when": "false"},
            {"id": "after_gated"},
            {"id": "sibling"},
        ],
        [("root", "gated"), ("gated", "after_gated"), ("root", "sibling")],
    )
    children = {"root": _child("root", JobStatus.SUCCESS)}

    plan = plan_workflow_step(spec, children, now=_NOW)

    assert plan.node_states["gated"].status is WorkflowNodeExecStatus.SKIPPED
    assert plan.node_states["after_gated"].status is WorkflowNodeExecStatus.SKIPPED
    assert plan.enqueue_node_ids == ("sibling",)


def test_when_skipped_nodes_do_not_fail_the_run() -> None:
    spec = _spec([{"id": "a"}, {"id": "b", "when": "false"}], [("a", "b")])
    children = {"a": _child("a", JobStatus.SUCCESS)}

    plan = plan_workflow_step(spec, children, now=_NOW)

    assert plan.run_status is JobStatus.SUCCESS
    assert plan.node_states["b"].status is WorkflowNodeExecStatus.SKIPPED


def test_when_evaluation_error_marks_node_failed() -> None:
    spec = _spec([{"id": "a", "when": "${unknown_var} == '1'", "on_failure": "continue"}])

    plan = plan_workflow_step(spec, {}, now=_NOW)

    assert plan.node_states["a"].status is WorkflowNodeExecStatus.FAILED
    assert "when evaluation failed" in (plan.node_states["a"].error or "")
    assert plan.run_status is JobStatus.FAILED


def test_when_uses_builtin_variables() -> None:
    spec = _spec([{"id": "a", "when": "${day} == '02'"}])

    plan = plan_workflow_step(spec, {}, now=_NOW, when_variables={"day": "02"})

    assert plan.enqueue_node_ids == ("a",)


# ── 失败 + on_failure ────────────────────────────────────────────────────────


def test_failed_node_continue_skips_pure_downstream_other_branch_continues() -> None:
    spec = _spec(
        [
            {"id": "root"},
            {"id": "bad", "on_failure": "continue"},
            {"id": "after_bad"},
            {"id": "sibling"},
        ],
        [("root", "bad"), ("bad", "after_bad"), ("root", "sibling")],
    )
    children = {
        "root": _child("root", JobStatus.SUCCESS),
        "bad": _child("bad", JobStatus.FAILED, error="sql_failed"),
    }

    plan = plan_workflow_step(spec, children, now=_NOW)

    assert plan.node_states["bad"].status is WorkflowNodeExecStatus.FAILED
    assert plan.node_states["after_bad"].status is WorkflowNodeExecStatus.SKIPPED
    assert plan.enqueue_node_ids == ("sibling",)
    assert plan.run_status is None  # sibling 还没跑完,run 未终态


def test_continue_run_ends_failed_when_any_node_failed() -> None:
    spec = _spec(
        [{"id": "bad", "on_failure": "continue"}, {"id": "sibling"}],
    )
    children = {
        "bad": _child("bad", JobStatus.FAILED, error="sql_failed"),
        "sibling": _child("sibling", JobStatus.SUCCESS),
    }

    plan = plan_workflow_step(spec, children, now=_NOW)

    assert plan.run_status is JobStatus.FAILED
    assert "bad" in (plan.run_error or "")


def test_failed_node_abort_cancels_remaining_and_fails_run() -> None:
    spec = _spec(
        [
            {"id": "bad", "on_failure": "abort"},
            {"id": "running_sibling"},
            {"id": "pending_sibling"},
            {"id": "not_started"},
        ],
        [("running_sibling", "not_started")],
    )
    children = {
        "bad": _child("bad", JobStatus.FAILED, error="boom"),
        "running_sibling": _child("running_sibling", JobStatus.RUNNING),
        "pending_sibling": _child("pending_sibling", JobStatus.PENDING),
    }

    plan = plan_workflow_step(spec, children, now=_NOW)

    assert plan.run_status is JobStatus.FAILED
    assert plan.enqueue_node_ids == ()
    assert plan.retry_job_ids == ()
    assert set(plan.cancel_job_ids) == {"job-running_sibling", "job-pending_sibling"}
    assert plan.node_states["running_sibling"].status is WorkflowNodeExecStatus.CANCELLED
    assert plan.node_states["not_started"].status is WorkflowNodeExecStatus.CANCELLED
    assert "bad" in (plan.run_error or "")


def test_cancelled_child_blocks_downstream_and_fails_run() -> None:
    spec = _spec([{"id": "a"}, {"id": "b"}], [("a", "b")])
    children = {"a": _child("a", JobStatus.CANCELLED)}

    plan = plan_workflow_step(spec, children, now=_NOW)

    assert plan.node_states["b"].status is WorkflowNodeExecStatus.SKIPPED
    assert plan.run_status is JobStatus.FAILED


# ── retry 计数(per-node RetryPolicy;None 继承全局)─────────────────────────


def test_retry_requeued_after_backoff_elapsed() -> None:
    spec = _spec(
        [
            {
                "id": "a",
                "on_failure": "abort",
                "retry_policy": {"max_retries": 2, "backoff_seconds": 60},
            }
        ]
    )
    finished = _NOW - timedelta(seconds=61)
    children = {"a": _child("a", JobStatus.FAILED, retry_count=0, finished_at=finished)}

    plan = plan_workflow_step(spec, children, now=_NOW)

    assert plan.retry_job_ids == ("job-a",)
    assert plan.node_states["a"].status is WorkflowNodeExecStatus.RETRY_WAIT
    assert plan.run_status is None


def test_retry_waits_for_fixed_backoff() -> None:
    spec = _spec(
        [
            {
                "id": "a",
                "retry_policy": {"max_retries": 2, "backoff_seconds": 60},
            }
        ]
    )
    finished = _NOW - timedelta(seconds=10)
    children = {"a": _child("a", JobStatus.FAILED, retry_count=0, finished_at=finished)}

    plan = plan_workflow_step(spec, children, now=_NOW)

    assert plan.retry_job_ids == ()
    assert plan.node_states["a"].status is WorkflowNodeExecStatus.RETRY_WAIT
    assert plan.wait_seconds is not None
    assert 49 < plan.wait_seconds <= 50


def test_retry_budget_exhausted_becomes_final_failure() -> None:
    spec = _spec(
        [
            {
                "id": "a",
                "on_failure": "abort",
                "retry_policy": {"max_retries": 2, "backoff_seconds": 0},
            }
        ]
    )
    children = {"a": _child("a", JobStatus.FAILED, retry_count=2, error="still failing")}

    plan = plan_workflow_step(spec, children, now=_NOW)

    assert plan.retry_job_ids == ()
    assert plan.node_states["a"].status is WorkflowNodeExecStatus.FAILED
    assert plan.run_status is JobStatus.FAILED


def test_retry_policy_none_inherits_global_default_max_retries() -> None:
    spec = _spec([{"id": "a"}])
    children = {"a": _child("a", JobStatus.FAILED, retry_count=0, finished_at=_NOW)}

    # 全局默认 0:不重试,直接最终失败
    plan = plan_workflow_step(spec, children, now=_NOW, default_max_retries=0)
    assert plan.node_states["a"].status is WorkflowNodeExecStatus.FAILED

    # 全局默认 1:继承(backoff=0,立即重排)
    plan = plan_workflow_step(spec, children, now=_NOW, default_max_retries=1)
    assert plan.retry_job_ids == ("job-a",)
    assert plan.node_states["a"].status is WorkflowNodeExecStatus.RETRY_WAIT


def test_timeout_child_is_retryable_like_failed() -> None:
    spec = _spec(
        [
            {
                "id": "a",
                "retry_policy": {"max_retries": 1, "backoff_seconds": 0},
            }
        ]
    )
    children = {"a": _child("a", JobStatus.TIMEOUT, retry_count=0, finished_at=_NOW)}

    plan = plan_workflow_step(spec, children, now=_NOW)

    assert plan.retry_job_ids == ("job-a",)


# ── 环检测:防御性校验用 raise 而非 assert(-O 下不被剥离)──────────────────────


def test_topological_nodes_raises_on_cycle() -> None:
    # WorkflowSpec 正常构造会拒环;用 model_construct 绕过校验模拟运行期损坏 spec,
    # 断言 _topological_nodes 显式 raise ValueError(而非 assert,后者 -O 下消失)。
    spec = WorkflowSpec.model_construct(
        nodes=[
            WorkflowNode(id="a", job_kind="sql_query", timeout_seconds=60),
            WorkflowNode(id="b", job_kind="sql_query", timeout_seconds=60),
        ],
        edges=[
            WorkflowEdge(source="a", target="b"),
            WorkflowEdge(source="b", target="a"),
        ],
    )

    with pytest.raises(ValueError, match="cycle_detected"):
        _topological_nodes(spec)
