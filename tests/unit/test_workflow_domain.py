"""Workflow 领域模型单测(设计稿 §2.8、R7、用户拍板语义)。

覆盖:R7 拒绝(forbidden)与首版暂不支持(unsupported)两层错误区分、
DAG 环检测(直接 + 间接)、边引用不存在节点、自环、retry 边界、
on_failure='branch' 拒绝、cron 基本格式、when 校验、合法 spec 全字段构造往返。
"""

from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from app.domain.job import ALLOWED_WORKFLOW_NODE_KINDS
from app.domain.workflow import (
    SUPPORTED_WORKFLOW_NODE_KINDS_V1,
    CronSchedule,
    RetryPolicy,
    WorkflowEdge,
    WorkflowNode,
    WorkflowSpec,
)


def make_node(node_id: str = "n1", **overrides: object) -> WorkflowNode:
    payload: dict[str, object] = {
        "id": node_id,
        "job_kind": "sql_query",
        "payload": {"sql": "SELECT 1"},
        "timeout_seconds": 60,
    }
    payload.update(overrides)
    return WorkflowNode.model_validate(payload)


# ---------------------------------------------------------------- 节点 kind 两层校验


@pytest.mark.parametrize("kind", ["shell", "http_request", "python_script", "ddl_exec", ""])
def test_forbidden_node_kind_rejected_as_r7(kind: str) -> None:
    """R7 白名单外的 kind → forbidden_node_kind(红线违规,非「暂不支持」)。"""
    with pytest.raises(ValidationError, match="forbidden_node_kind"):
        make_node(job_kind=kind)


@pytest.mark.parametrize(
    "kind", sorted(ALLOWED_WORKFLOW_NODE_KINDS - SUPPORTED_WORKFLOW_NODE_KINDS_V1)
)
def test_allowed_but_unsupported_kind_rejected_as_unsupported(kind: str) -> None:
    """白名单内但首版不支持(notify/sleep/branch/scenario_*)→ unsupported_node_kind。"""
    with pytest.raises(ValidationError, match="unsupported_node_kind"):
        make_node(job_kind=kind)


def test_unsupported_and_forbidden_messages_are_distinct() -> None:
    with pytest.raises(ValidationError) as forbidden:
        make_node(job_kind="shell")
    with pytest.raises(ValidationError) as unsupported:
        make_node(job_kind="notify")
    assert "forbidden_node_kind" in str(forbidden.value)
    assert "unsupported_node_kind" not in str(forbidden.value)
    assert "unsupported_node_kind" in str(unsupported.value)
    assert "forbidden_node_kind" not in str(unsupported.value)


@pytest.mark.parametrize("kind", sorted(SUPPORTED_WORKFLOW_NODE_KINDS_V1))
def test_all_v1_supported_kinds_accepted(kind: str) -> None:
    assert make_node(job_kind=kind).job_kind == kind


def test_v1_supported_set_is_subset_of_r7_allowlist() -> None:
    assert SUPPORTED_WORKFLOW_NODE_KINDS_V1 <= ALLOWED_WORKFLOW_NODE_KINDS


# ---------------------------------------------------------------- 节点其余字段


def test_on_failure_branch_rejected_as_unsupported() -> None:
    with pytest.raises(ValidationError, match="unsupported_on_failure"):
        make_node(on_failure="branch")


@pytest.mark.parametrize("value", ["abort", "continue"])
def test_on_failure_abort_and_continue_accepted(value: str) -> None:
    assert make_node(on_failure=value).on_failure == value


def test_on_failure_out_of_literal_rejected() -> None:
    with pytest.raises(ValidationError):
        make_node(on_failure="retry_forever")


def test_node_id_blank_rejected() -> None:
    with pytest.raises(ValidationError, match="invalid_node_id"):
        make_node(node_id="   ")


def test_timeout_seconds_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        make_node(timeout_seconds=0)


def test_when_stored_and_blank_rejected() -> None:
    node = make_node(when="prev.status == 'success'")
    assert node.when == "prev.status == 'success'"
    assert make_node().when is None
    with pytest.raises(ValidationError, match="invalid_when"):
        make_node(when="   ")
    with pytest.raises(ValidationError):
        make_node(when="x" * 513)


# ---------------------------------------------------------------- RetryPolicy 边界


def test_retry_policy_bounds() -> None:
    policy = RetryPolicy(max_retries=5, backoff_seconds=3600)
    assert policy.max_retries == 5
    assert policy.backoff_seconds == 3600
    assert RetryPolicy(max_retries=0, backoff_seconds=0).max_retries == 0
    with pytest.raises(ValidationError):
        RetryPolicy(max_retries=6, backoff_seconds=0)
    with pytest.raises(ValidationError):
        RetryPolicy(max_retries=-1, backoff_seconds=0)
    with pytest.raises(ValidationError):
        RetryPolicy(max_retries=0, backoff_seconds=-1)
    with pytest.raises(ValidationError):
        RetryPolicy(max_retries=0, backoff_seconds=3601)


def test_node_retry_policy_defaults_to_none_meaning_inherit_global() -> None:
    assert make_node().retry_policy is None


# ---------------------------------------------------------------- CronSchedule


def test_cron_valid_five_fields() -> None:
    schedule = CronSchedule(cron="0 2 * * 1-5")
    assert schedule.enabled is True
    assert CronSchedule(cron="*/15 0,12 1-10 * *", enabled=False).enabled is False


@pytest.mark.parametrize(
    "expr",
    [
        "0 2 * *",  # 4 段
        "0 2 * * * *",  # 6 段
        "",  # 空
        "0 2 * * mon",  # 字母不在受限字符集
        "0 2 * * ?",  # quartz 问号不支持
    ],
)
def test_cron_invalid_rejected(expr: str) -> None:
    with pytest.raises(ValidationError, match="invalid_cron"):
        CronSchedule(cron=expr)


# ---------------------------------------------------------------- Spec DAG 结构校验


def make_spec(
    node_ids: list[str], edge_pairs: list[tuple[str, str]], **overrides: object
) -> WorkflowSpec:
    payload: dict[str, object] = {
        "nodes": [make_node(node_id) for node_id in node_ids],
        "edges": [WorkflowEdge(source=s, target=t) for s, t in edge_pairs],
    }
    payload.update(overrides)
    return WorkflowSpec.model_validate(payload)


def test_single_node_no_edges_is_valid() -> None:
    spec = make_spec(["only"], [])
    assert [node.id for node in spec.nodes] == ["only"]


def test_isolated_node_alongside_chain_is_valid() -> None:
    spec = make_spec(["a", "b", "lonely"], [("a", "b")])
    assert len(spec.nodes) == 3


def test_duplicate_node_id_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate_node_id"):
        make_spec(["a", "a"], [])


def test_empty_nodes_rejected() -> None:
    with pytest.raises(ValidationError):
        WorkflowSpec(nodes=[], edges=[])


def test_node_count_upper_bound_50() -> None:
    ids = [f"n{i}" for i in range(50)]
    assert len(make_spec(ids, []).nodes) == 50
    with pytest.raises(ValidationError):
        make_spec([f"n{i}" for i in range(51)], [])


@pytest.mark.parametrize("edge", [("ghost", "a"), ("a", "ghost")])
def test_edge_referencing_unknown_node_rejected(edge: tuple[str, str]) -> None:
    with pytest.raises(ValidationError, match="unknown_edge_node"):
        make_spec(["a", "b"], [edge])


def test_self_loop_rejected() -> None:
    with pytest.raises(ValidationError, match="self_loop"):
        make_spec(["a"], [("a", "a")])


def _reported_cycle_nodes(error: ValidationError) -> set[str]:
    """从错误信息中提取「环上节点: x -> y -> z」报告的节点集合。"""
    match = re.search(r"环上节点: ([^\[\n]+)", str(error))
    assert match is not None, f"cycle nodes missing in: {error}"
    return {part.strip() for part in match.group(1).split("->")}


def test_direct_cycle_rejected_with_cycle_nodes() -> None:
    with pytest.raises(ValidationError, match="cycle_detected") as exc:
        make_spec(["a", "b"], [("a", "b"), ("b", "a")])
    assert _reported_cycle_nodes(exc.value) == {"a", "b"}


def test_indirect_cycle_rejected_with_cycle_nodes() -> None:
    with pytest.raises(ValidationError, match="cycle_detected") as exc:
        make_spec(
            ["entry", "a", "b", "c", "tail"],
            [("entry", "a"), ("a", "b"), ("b", "c"), ("c", "a"), ("c", "tail")],
        )
    # 报告的环节点恰为 a/b/c,环外前驱 entry 与下游 tail 不在其中
    assert _reported_cycle_nodes(exc.value) == {"a", "b", "c"}


def test_diamond_dag_is_acyclic_and_valid() -> None:
    spec = make_spec(["a", "b", "c", "d"], [("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")])
    assert len(spec.edges) == 4


# ---------------------------------------------------------------- 合法 spec 全字段往返


def test_full_spec_construction_roundtrip() -> None:
    spec = WorkflowSpec(
        nodes=[
            WorkflowNode(
                id="extract",
                job_kind="sql_query",
                payload={"sql": "SELECT * FROM t", "datasource_id": "ds-1"},
                retry_policy=RetryPolicy(max_retries=3, backoff_seconds=30),
                timeout_seconds=600,
                on_failure="abort",
                when=None,
            ),
            WorkflowNode(
                id="verify",
                job_kind="compare_run",
                payload={"task_id": "cmp-1"},
                retry_policy=None,
                timeout_seconds=1800,
                on_failure="continue",
                when="extract.status == 'success'",
            ),
        ],
        edges=[WorkflowEdge(source="extract", target="verify")],
        schedule=CronSchedule(cron="0 2 * * 1-5", enabled=True),
    )
    dumped = spec.model_dump()
    restored = WorkflowSpec.model_validate(dumped)
    assert restored == spec
    assert restored.nodes[0].retry_policy == RetryPolicy(max_retries=3, backoff_seconds=30)
    assert restored.nodes[1].retry_policy is None
    assert restored.schedule is not None
    assert restored.schedule.cron == "0 2 * * 1-5"
    # JSON 往返(API 层将以 JSON 收发 spec)
    restored_json = WorkflowSpec.model_validate_json(spec.model_dump_json())
    assert restored_json == spec
