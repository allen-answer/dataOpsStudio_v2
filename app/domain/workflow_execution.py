"""Workflow DAG 推进纯逻辑(2.4.0 PR-4;ADR-0009)。

WorkflowRun 本身是 job(kind=workflow_run),每个 DAG 节点是回指
``parent_workflow_run_id`` 的子 job。本模块只做**一步推进的纯计算**
(零 IO,R1):输入 WorkflowSpec + 各节点子 job 快照 + 当前时间,
输出「该 enqueue 哪些节点 / 该重试哪些子 job / 该取消哪些子 job /
run 是否终态」。worker 侧执行器与 API 侧状态查询共用同一函数,
保证两边看到的节点状态口径一致。

执行语义(ADR-0009 拍板):

- 就绪 = 所有上游终态 SUCCESS;
- ``when`` 求值为假 → 本节点及纯下游(可达闭包)SKIPPED,旁路分支继续;
  ``when`` 求值异常 → 本节点按 FAILED 处理(确定性错误,不重试);
- 节点失败:per-node RetryPolicy(max_retries + 固定 backoff;None 继承
  全局 job_default_max_retries,继承时 backoff=0 与 reaper 立即重排一致)
  耗尽后按 on_failure —— abort=取消剩余节点、run 失败;continue=纯下游
  SKIPPED、其余分支继续,run 最终仍 FAILED(与 1.x 口径一致:任一节点
  FAILED 则 run FAILED);
- 全节点终态(SUCCESS/FAILED/SKIPPED/CANCELLED)→ run 终态。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from math import isfinite
from typing import TypeGuard

from app.domain.job import JobStatus
from app.domain.workflow import WorkflowEdge, WorkflowNode, WorkflowSpec
from app.domain.workflow_interpolate import ParamInterpolationError, interpolate_payload
from app.domain.workflow_outputs import (
    COMMON_NODE_OUTPUT_FIELDS,
    NODE_OUTPUT_FIELDS_BY_KIND,
    NodeOutputValue,
)
from app.domain.workflow_when import WhenEvaluationError, evaluate_when

# RetryPolicy=None 继承全局 max_retries 时的固定退避秒数:
# 全局重试语义来自 reaper 立即重排,继承路径保持一致(0 = 立即重试)。
INHERITED_RETRY_BACKOFF_SECONDS = 0
UNSAFE_NODE_OUTPUT_ERROR_CODE = "unsafe_node_output_value"
WHEN_EVALUATION_ERROR_CODE = "when_evaluation_failed"
PARAM_INTERPOLATION_ERROR_CODE = "param_interpolation_failed"
BRANCH_EVALUATION_ERROR_CODE = "branch_evaluation_failed"


class _EdgeDecision(StrEnum):
    WAITING = "waiting"
    INACTIVE = "inactive"
    SATISFIED = "satisfied"
    BLOCKED = "blocked"


class WorkflowNodeExecStatus(StrEnum):
    """节点执行态(子 job 状态 + DAG 编排态的合并口径)。"""

    WAITING = "waiting"  # 上游未完成,尚未 enqueue
    RUNNING = "running"  # 子 job pending/running
    RETRY_WAIT = "retry_wait"  # 子 job 失败,重试预算未耗尽(等 backoff / 待重排)
    SUCCESS = "success"
    FAILED = "failed"  # 重试耗尽的最终失败(或 when 求值异常)
    SKIPPED = "skipped"  # when 为假 / 上游失败或跳过的纯下游
    CANCELLED = "cancelled"


TERMINAL_NODE_STATUSES: frozenset[WorkflowNodeExecStatus] = frozenset(
    {
        WorkflowNodeExecStatus.SUCCESS,
        WorkflowNodeExecStatus.FAILED,
        WorkflowNodeExecStatus.SKIPPED,
        WorkflowNodeExecStatus.CANCELLED,
    }
)


@dataclass(frozen=True)
class WorkflowChildJob:
    """节点子 job 的最小快照(由 jobs 表行投影,保持纯数据)。"""

    node_id: str
    job_id: str
    status: JobStatus
    retry_count: int = 0
    finished_at: datetime | None = None
    error: str | None = None
    error_code: str | None = None
    outputs: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkflowNodeState:
    node_id: str
    status: WorkflowNodeExecStatus
    job_id: str | None = None
    attempts: int = 0
    error: str | None = None
    outputs: dict[str, NodeOutputValue] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkflowStepPlan:
    """一步推进的产物(执行器照单执行;状态查询只读 node_states)。"""

    node_states: dict[str, WorkflowNodeState]
    enqueue_node_ids: tuple[str, ...]  # 就绪且 when 通过 → enqueue 子 job
    retry_job_ids: tuple[str, ...]  # backoff 已到 → 重排(retry_count+1)
    cancel_job_ids: tuple[str, ...]  # abort:需请求取消的 pending/running 子 job
    run_status: JobStatus | None  # SUCCESS/FAILED = run 终态;None = 继续推进
    run_error: str | None = None
    wait_seconds: float | None = None  # 最近一次 retry backoff 剩余秒数


def plan_workflow_step(
    spec: WorkflowSpec,
    children: Mapping[str, WorkflowChildJob],
    *,
    now: datetime,
    default_max_retries: int = 0,
    when_variables: Mapping[str, str | list[str]] | None = None,
) -> WorkflowStepPlan:
    """计算 DAG 的下一步推进计划(纯函数,幂等)。

    ``children``:node_id → 子 job 快照(未 enqueue 的节点不在其中)。
    """
    nodes_by_id = {node.id: node for node in spec.nodes}
    incoming_edges: dict[str, list[WorkflowEdge]] = {node.id: [] for node in spec.nodes}
    outgoing_edges: dict[str, list[WorkflowEdge]] = {node.id: [] for node in spec.nodes}
    for edge in spec.edges:
        incoming_edges[edge.target].append(edge)
        outgoing_edges[edge.source].append(edge)

    variables = dict(when_variables or {})
    states: dict[str, WorkflowNodeState] = {}
    route_selections: dict[tuple[str, str], tuple[str | None, str | None]] = {}
    inactive_node_ids: set[str] = set()
    enqueue: list[str] = []
    retry: list[str] = []
    wait_candidates: list[float] = []

    for node in _topological_nodes(spec):
        child = children.get(node.id)
        if child is not None:
            state = _state_from_child(
                node,
                child,
                now=now,
                default_max_retries=default_max_retries,
                retry_job_ids=retry,
                wait_candidates=wait_candidates,
            )
            states[node.id] = state
            states[node.id] = _with_failure_route_error(
                node=node,
                state=states[node.id],
                edges=outgoing_edges[node.id],
                variables=variables,
                states=states,
                cache=route_selections,
            )
            continue
        incoming = incoming_edges[node.id]
        if incoming:
            decisions = [
                _edge_decision(
                    edge,
                    nodes_by_id=nodes_by_id,
                    outgoing_edges=outgoing_edges,
                    variables=variables,
                    states=states,
                    route_selections=route_selections,
                    inactive_node_ids=inactive_node_ids,
                )
                for edge in incoming
            ]
            if _EdgeDecision.WAITING in decisions:
                states[node.id] = WorkflowNodeState(
                    node_id=node.id, status=WorkflowNodeExecStatus.WAITING
                )
                continue
            if all(decision is _EdgeDecision.INACTIVE for decision in decisions):
                states[node.id] = WorkflowNodeState(
                    node_id=node.id, status=WorkflowNodeExecStatus.SKIPPED
                )
                inactive_node_ids.add(node.id)
                continue
            if _EdgeDecision.BLOCKED in decisions:
                states[node.id] = WorkflowNodeState(
                    node_id=node.id, status=WorkflowNodeExecStatus.SKIPPED
                )
                continue
        # 就绪:所有上游终态成功(根节点上游为空,视为就绪)
        output_context = _output_context(states)
        try:
            should_run = evaluate_when(
                node.when,
                variables,
                node_outputs=output_context,
            )
        except WhenEvaluationError as exc:
            states[node.id] = WorkflowNodeState(
                node_id=node.id,
                status=WorkflowNodeExecStatus.FAILED,
                error=f"when evaluation failed: {exc}",
                outputs=_failed_node_outputs(
                    job_id=None,
                    error_code=WHEN_EVALUATION_ERROR_CODE,
                ),
            )
            states[node.id] = _with_failure_route_error(
                node=node,
                state=states[node.id],
                edges=outgoing_edges[node.id],
                variables=variables,
                states=states,
                cache=route_selections,
            )
            continue
        if not should_run:
            states[node.id] = WorkflowNodeState(
                node_id=node.id, status=WorkflowNodeExecStatus.SKIPPED
            )
            continue
        # payload ${var} 插值在入队时刻才真正渲染(worker 侧 _build_workflow_child_job),
        # 但这里先做一次纯校验:未解析变量 / 不支持的引用是确定性错误,与 when 求值
        # 异常同路径 —— 节点直接 FAILED、不重试、按 on_failure 语义处理,永不把 run 打挂
        try:
            interpolate_payload(
                node.payload,
                variables,
                node_outputs=output_context,
            )
        except ParamInterpolationError as exc:
            states[node.id] = WorkflowNodeState(
                node_id=node.id,
                status=WorkflowNodeExecStatus.FAILED,
                error=f"param interpolation failed: {exc}",
                outputs=_failed_node_outputs(
                    job_id=None,
                    error_code=PARAM_INTERPOLATION_ERROR_CODE,
                ),
            )
            states[node.id] = _with_failure_route_error(
                node=node,
                state=states[node.id],
                edges=outgoing_edges[node.id],
                variables=variables,
                states=states,
                cache=route_selections,
            )
            continue
        ready_outputs: dict[str, NodeOutputValue] = {}
        if node.job_kind == "branch":
            selected_target, route_error = _selected_route(
                node_id=node.id,
                trigger="success",
                edges=outgoing_edges[node.id],
                variables=variables,
                states=states,
                cache=route_selections,
            )
            if route_error is not None:
                states[node.id] = WorkflowNodeState(
                    node_id=node.id,
                    status=WorkflowNodeExecStatus.FAILED,
                    error=f"branch evaluation failed: {route_error}",
                    outputs=_failed_node_outputs(
                        job_id=None,
                        error_code=BRANCH_EVALUATION_ERROR_CODE,
                    ),
                )
                states[node.id] = _with_failure_route_error(
                    node=node,
                    state=states[node.id],
                    edges=outgoing_edges[node.id],
                    variables=variables,
                    states=states,
                    cache=route_selections,
                )
                continue
            ready_outputs["selected_target"] = selected_target
        states[node.id] = WorkflowNodeState(
            node_id=node.id,
            status=WorkflowNodeExecStatus.WAITING,
            outputs=ready_outputs,
        )
        enqueue.append(node.id)

    # on_failure=abort:任一最终失败节点要求 abort → 取消剩余,run 立即失败
    abort_error = _abort_error(states, nodes_by_id)
    if abort_error is not None:
        cancel_job_ids = tuple(
            child.job_id
            for child in children.values()
            if child.status in {JobStatus.PENDING, JobStatus.RUNNING}
        )
        for node_id, state in list(states.items()):
            if state.status in TERMINAL_NODE_STATUSES:
                continue
            states[node_id] = WorkflowNodeState(
                node_id=node_id,
                status=WorkflowNodeExecStatus.CANCELLED,
                job_id=state.job_id,
                attempts=state.attempts,
                error=state.error,
            )
        return WorkflowStepPlan(
            node_states=states,
            enqueue_node_ids=(),
            retry_job_ids=(),
            cancel_job_ids=cancel_job_ids,
            run_status=JobStatus.FAILED,
            run_error=abort_error,
        )

    if all(state.status in TERMINAL_NODE_STATUSES for state in states.values()):
        failed = [
            state
            for state in states.values()
            if state.status in {WorkflowNodeExecStatus.FAILED, WorkflowNodeExecStatus.CANCELLED}
        ]
        if failed:
            first = failed[0]
            return WorkflowStepPlan(
                node_states=states,
                enqueue_node_ids=(),
                retry_job_ids=(),
                cancel_job_ids=(),
                run_status=JobStatus.FAILED,
                run_error=_node_failure_message(first),
            )
        return WorkflowStepPlan(
            node_states=states,
            enqueue_node_ids=(),
            retry_job_ids=(),
            cancel_job_ids=(),
            run_status=JobStatus.SUCCESS,
        )

    return WorkflowStepPlan(
        node_states=states,
        enqueue_node_ids=tuple(enqueue),
        retry_job_ids=tuple(retry),
        cancel_job_ids=(),
        run_status=None,
        wait_seconds=min(wait_candidates) if wait_candidates else None,
    )


def _output_context(
    states: Mapping[str, WorkflowNodeState],
) -> dict[str, Mapping[str, NodeOutputValue]]:
    return {node_id: state.outputs for node_id, state in states.items()}


def _with_failure_route_error(
    *,
    node: WorkflowNode,
    state: WorkflowNodeState,
    edges: list[WorkflowEdge],
    variables: Mapping[str, str | list[str]],
    states: Mapping[str, WorkflowNodeState],
    cache: dict[tuple[str, str], tuple[str | None, str | None]],
) -> WorkflowNodeState:
    if state.status is not WorkflowNodeExecStatus.FAILED or node.on_failure != "branch":
        return state
    _, route_error = _selected_route(
        node_id=node.id,
        trigger="failure",
        edges=edges,
        variables=variables,
        states=states,
        cache=cache,
    )
    if route_error is None:
        return state
    detail = f"failure branch evaluation failed: {route_error}"
    return replace(
        state,
        error=f"{state.error}; {detail}" if state.error else detail,
    )


def _selected_route(
    *,
    node_id: str,
    trigger: str,
    edges: list[WorkflowEdge],
    variables: Mapping[str, str | list[str]],
    states: Mapping[str, WorkflowNodeState],
    cache: dict[tuple[str, str], tuple[str | None, str | None]],
) -> tuple[str | None, str | None]:
    key = (node_id, trigger)
    cached = cache.get(key)
    if cached is not None:
        return cached
    matching_edges = [edge for edge in edges if edge.trigger == trigger]
    default_edge = next(edge for edge in matching_edges if edge.is_default)
    decision: tuple[str | None, str | None]
    output_context = _output_context(states)
    if trigger == "success" and node_id not in output_context:
        output_context[node_id] = {
            "status": WorkflowNodeExecStatus.SUCCESS.value,
            "job_id": None,
            "error_code": None,
            "selected_target": None,
        }
    try:
        selected = default_edge.target
        for edge in matching_edges:
            if edge.is_default:
                continue
            if evaluate_when(
                edge.when,
                variables,
                node_outputs=output_context,
            ):
                selected = edge.target
                break
    except WhenEvaluationError as exc:
        decision = (None, str(exc))
    else:
        decision = (selected, None)
    cache[key] = decision
    return decision


def _edge_decision(
    edge: WorkflowEdge,
    *,
    nodes_by_id: Mapping[str, WorkflowNode],
    outgoing_edges: Mapping[str, list[WorkflowEdge]],
    variables: Mapping[str, str | list[str]],
    states: Mapping[str, WorkflowNodeState],
    route_selections: dict[tuple[str, str], tuple[str | None, str | None]],
    inactive_node_ids: set[str],
) -> _EdgeDecision:
    source_state = states[edge.source]
    if source_state.status not in TERMINAL_NODE_STATUSES:
        return _EdgeDecision.WAITING
    if edge.source in inactive_node_ids:
        return _EdgeDecision.INACTIVE
    source_node = nodes_by_id[edge.source]
    if edge.trigger == "success":
        if source_node.job_kind == "branch":
            if source_state.status is not WorkflowNodeExecStatus.SUCCESS:
                return _EdgeDecision.INACTIVE
            if source_state.outputs.get("selected_target") == edge.target:
                return _EdgeDecision.SATISFIED
            return _EdgeDecision.INACTIVE
        if source_state.status is WorkflowNodeExecStatus.SUCCESS:
            return _EdgeDecision.SATISFIED
        if (
            source_state.status is WorkflowNodeExecStatus.FAILED
            and source_node.on_failure == "branch"
        ):
            return _EdgeDecision.INACTIVE
        return _EdgeDecision.BLOCKED
    if source_state.status is not WorkflowNodeExecStatus.FAILED:
        return _EdgeDecision.INACTIVE
    selected, error = _selected_route(
        node_id=edge.source,
        trigger="failure",
        edges=outgoing_edges[edge.source],
        variables=variables,
        states=states,
        cache=route_selections,
    )
    if error is None and selected == edge.target:
        return _EdgeDecision.SATISFIED
    return _EdgeDecision.INACTIVE


def _topological_nodes(spec: WorkflowSpec) -> list[WorkflowNode]:
    """确定性拓扑序(Kahn;并列就绪时按 nodes 声明顺序,同 1.x)。"""
    index_by_id = {node.id: index for index, node in enumerate(spec.nodes)}
    in_degree = {node.id: 0 for node in spec.nodes}
    downstream: dict[str, list[str]] = {node.id: [] for node in spec.nodes}
    for edge in spec.edges:
        downstream[edge.source].append(edge.target)
        in_degree[edge.target] += 1
    ready = sorted(
        (node.id for node in spec.nodes if in_degree[node.id] == 0),
        key=lambda node_id: index_by_id[node_id],
    )
    order: list[WorkflowNode] = []
    nodes_by_id = {node.id: node for node in spec.nodes}
    while ready:
        current = ready.pop(0)
        order.append(nodes_by_id[current])
        for target in downstream[current]:
            in_degree[target] -= 1
            if in_degree[target] == 0:
                ready.append(target)
        ready.sort(key=lambda node_id: index_by_id[node_id])
    # WorkflowSpec 构造期已保证无环;此处防御性校验(assert 在 -O 下会被剥离,
    # 用显式 raise 保证优化运行也不会让环悄悄漏过)。与 WorkflowSpec 校验同口径。
    if len(order) != len(spec.nodes):
        raise ValueError("cycle_detected: workflow spec must be a DAG")
    return order


def _state_from_child(
    node: WorkflowNode,
    child: WorkflowChildJob,
    *,
    now: datetime,
    default_max_retries: int,
    retry_job_ids: list[str],
    wait_candidates: list[float],
) -> WorkflowNodeState:
    child_outputs = _child_outputs(node, child)
    if child_outputs is None:
        return WorkflowNodeState(
            node_id=node.id,
            status=WorkflowNodeExecStatus.FAILED,
            job_id=child.job_id,
            attempts=child.retry_count,
            error=UNSAFE_NODE_OUTPUT_ERROR_CODE,
            outputs=_failed_node_outputs(
                job_id=child.job_id,
                error_code=UNSAFE_NODE_OUTPUT_ERROR_CODE,
            ),
        )
    if child.status is JobStatus.SUCCESS:
        return WorkflowNodeState(
            node_id=node.id,
            status=WorkflowNodeExecStatus.SUCCESS,
            job_id=child.job_id,
            attempts=child.retry_count,
            outputs=child_outputs,
        )
    if child.status is JobStatus.CANCELLED:
        return WorkflowNodeState(
            node_id=node.id,
            status=WorkflowNodeExecStatus.CANCELLED,
            job_id=child.job_id,
            attempts=child.retry_count,
            error=child.error,
            outputs=child_outputs,
        )
    if child.status in {JobStatus.FAILED, JobStatus.TIMEOUT}:
        if node.retry_policy is not None:
            max_retries = node.retry_policy.max_retries
            backoff_seconds = node.retry_policy.backoff_seconds
        else:
            max_retries = default_max_retries
            backoff_seconds = INHERITED_RETRY_BACKOFF_SECONDS
        if child.retry_count < max_retries:
            finished_at = child.finished_at or now
            remaining = backoff_seconds - (now - finished_at).total_seconds()
            if remaining <= 0:
                retry_job_ids.append(child.job_id)
            else:
                wait_candidates.append(remaining)
            return WorkflowNodeState(
                node_id=node.id,
                status=WorkflowNodeExecStatus.RETRY_WAIT,
                job_id=child.job_id,
                attempts=child.retry_count,
                error=child.error,
                outputs=child_outputs,
            )
        return WorkflowNodeState(
            node_id=node.id,
            status=WorkflowNodeExecStatus.FAILED,
            job_id=child.job_id,
            attempts=child.retry_count,
            error=child.error,
            outputs=child_outputs,
        )
    # pending / running
    return WorkflowNodeState(
        node_id=node.id,
        status=WorkflowNodeExecStatus.RUNNING,
        job_id=child.job_id,
        attempts=child.retry_count,
        outputs=child_outputs,
    )


def _child_outputs(
    node: WorkflowNode,
    child: WorkflowChildJob,
) -> dict[str, NodeOutputValue] | None:
    allowed_fields = NODE_OUTPUT_FIELDS_BY_KIND[node.job_kind]
    filtered: dict[str, NodeOutputValue] = {}
    for key, value in child.outputs.items():
        if key not in allowed_fields or key in COMMON_NODE_OUTPUT_FIELDS:
            continue
        if not _is_safe_node_output_value(value):
            return None
        filtered[key] = value
    return {
        **filtered,
        "status": child.status.value,
        "job_id": child.job_id,
        "error_code": child.error_code,
    }


def _is_safe_node_output_value(value: object) -> TypeGuard[NodeOutputValue]:
    if value is None or isinstance(value, (str, bool, int)):
        return True
    return isinstance(value, float) and isfinite(value)


def _failed_node_outputs(
    *,
    job_id: str | None,
    error_code: str,
) -> dict[str, NodeOutputValue]:
    return {
        "status": WorkflowNodeExecStatus.FAILED.value,
        "job_id": job_id,
        "error_code": error_code,
    }


def _abort_error(
    states: Mapping[str, WorkflowNodeState],
    nodes_by_id: Mapping[str, WorkflowNode],
) -> str | None:
    for node_id, state in states.items():
        if (
            state.status is WorkflowNodeExecStatus.FAILED
            and nodes_by_id[node_id].on_failure == "abort"
        ):
            return _node_failure_message(state)
    return None


def _node_failure_message(state: WorkflowNodeState) -> str:
    detail = state.error or state.status.value
    return f"workflow node {state.node_id} {state.status.value}: {detail}"
