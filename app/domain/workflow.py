"""Workflow 领域模型(设计稿 v0.3.2 §2.8、契约 §4 R7、ADR-0009)。

Workflow = Job DAG:只编排 DataOpsStudio 内置 Job,不是低配 Airflow。
本模块纯模型 + 构造期校验,零 IO(R1:domain 层禁止 import 数据库驱动)。

两层节点 kind 校验(错误语义分开,勿混淆):

- ``forbidden_node_kind`` —— kind 不在 ``ALLOWED_WORKFLOW_NODE_KINDS``(R7 红线,
  永不开放:shell / python / 任意 HTTP 等);
- ``unsupported_node_kind`` —— kind 在 R7 白名单内但不在
  ``SUPPORTED_WORKFLOW_NODE_KINDS_V1``(当前仅 Scenario 两种等待 2.6.0)。

``on_failure`` 支持 ``abort/continue/branch``;branch 的 failure 边在
``WorkflowSpec`` 构造期校验 first-match + 唯一 default。
节点级 ``when`` 条件首版只做存储 + 非空校验,表达式求值语义属执行器(PR-4)。
Cron 校验两道关(PR-4b 落地精确解析):先 5 段 + 字符集,再 croniter 语义解析,
拒绝越界 / 不可解析表达式(如 ``99 * * * *``)。运行期"上一触发点"计算属调度器
(``app/services/workflow_scheduler.py``),本模块只做构造期校验。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Literal

from croniter import croniter
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.job import ALLOWED_WORKFLOW_NODE_KINDS
from app.domain.notify import NotifyTarget
from app.domain.workflow_outputs import (
    NODE_OUTPUT_FIELDS_BY_KIND,
    iter_placeholder_expressions,
    parse_node_output_reference,
)

# 2.4.x 支持的节点集:R7 白名单中除 Scenario Lab(2.6.0)外的节点。
SUPPORTED_WORKFLOW_NODE_KINDS_V1: frozenset[str] = frozenset(
    {
        "sql_query",
        "sql_explain",
        "compare_run",
        "lineage_analyze",
        "export_excel",
        "notify",
        "sleep",
        "branch",
    }
)

MAX_WORKFLOW_NODES = 50
MAX_RETRIES_LIMIT = 5
MAX_BACKOFF_SECONDS = 3600
MAX_WHEN_LENGTH = 512

# ── C-10 sensor 触发(数据到达触发)边界 ──────────────────────────────────────
# 检查间隔下限拦住"每 tick 都派检查"(调度 tick 默认 30s,间隔再小也被 tick 粒度吃掉);
# 上限一天。冷却期 0 = 不冷却(条件持续为真则每次检查都可再触发),上限 7 天。
MIN_SENSOR_INTERVAL_SECONDS = 10
MAX_SENSOR_INTERVAL_SECONDS = 86_400
MAX_SENSOR_COOLDOWN_SECONDS = 604_800
MAX_SENSOR_SQL_LENGTH = 10_000

# cron 单字段字符集:数字 + * , - /(第一道关;第二道 croniter 语义解析见 CronSchedule)
_CRON_FIELD_RE = re.compile(r"^[0-9*,\-/]+$")

# ── workflow ${var} 变量来源校验(C-7 PR2)────────────────────────────────────
# 变量有两个来源(spec 默认变量 + 触发时运行时变量),二者共用这套校验(单一实现)。
# 合并后的快照写进 run payload 的 when_variables,同时服务 when 求值与 payload 插值。

# 内置确定性变量名(必须与 workflow_when.builtin_when_variables 的 key 集合一致;
# 单测断言两者不漂移)。spec / 触发变量**禁止**与之冲突 —— 合并优先级
# builtin < spec.variables < 触发,禁冲突让 builtin 不可被覆盖、优先级无歧义。
BUILTIN_VARIABLE_NAMES: frozenset[str] = frozenset({"today", "now", "year", "month", "day"})

MAX_WORKFLOW_VARIABLES = 32
MAX_VARIABLE_VALUE_LENGTH = 512
# list 型变量的元素数上限(C-7 PR3;供 ${var | sql_in} / ${var | csv} 展开)。
MAX_VARIABLE_LIST_LENGTH = 256

# workflow 变量值:标量 str,或 list[str](list 供过滤器展开成 SQL IN(...) / CSV)。
# 内置变量恒为 str;spec / 触发变量可为 str 或 list[str]。
VariableValue = str | list[str]

_VARIABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# ★ 变量值安全字符集(本 PR 注入防御核心):值会被字符串替换进 compare 节点的
# table_name / schema_name(标识符位)或 sql(字面量位),任何位置都不能逃逸。
# 只允许 字母 / 数字 / `_` / `-` / `.` / `:` —— 天然排除:
#   单双引号 ' " 、反引号 ` 、分号 ; 、反斜杠 \ 、注释起始 /* (`/`/`*` 不在集内)、
#   空格 / 换行 / 控制符。单个 `-` 允许(日期 2026-07-07),但 SQL 行注释 `--`
#   由下方 `"--" in value` 显式再拦一道(单 `-` 无害、连写才成注释)。
# 引号型字面量 / list 展开(`WHERE x IN (${ids | sql_in})`)留到 PR3 的过滤器。
_VARIABLE_VALUE_RE = re.compile(r"^[A-Za-z0-9_.:-]*$")


def _validate_scalar_value(name: str, value: str) -> None:
    """单个标量值(或 list 的一个元素)的长度 + 安全字符集校验。

    ★ R5:错误只含**变量名**,绝不含取值 / 元素值。
    """
    if len(value) > MAX_VARIABLE_VALUE_LENGTH:
        raise ValueError(f"variable_value_too_long: {name}")
    if not _VARIABLE_VALUE_RE.fullmatch(value) or "--" in value:
        raise ValueError(f"unsafe_variable_value: {name}")


def validate_workflow_variables(variables: Mapping[str, object]) -> dict[str, VariableValue]:
    """校验一组 workflow 变量(spec 默认变量 + 触发时运行时变量的单一实现)。

    - 数量 <= ``MAX_WORKFLOW_VARIABLES``;
    - key 匹配 ``^[A-Za-z_][A-Za-z0-9_]*$``,且**禁与内置变量名冲突**;
    - value 是 ``str`` 或 ``list[str]``:
      - ``str``:长度 <= ``MAX_VARIABLE_VALUE_LENGTH``、只含 ``_VARIABLE_VALUE_RE``
        的保守安全字符集(``--`` 另行显式拦截);
      - ``list[str]``(C-7 PR3;供 ``${var | sql_in}`` / ``${var | csv}`` 展开):
        元素数 <= ``MAX_VARIABLE_LIST_LENGTH``,**每个元素**走与标量同一套校验。

    ★ R5:任何校验错误只含**变量名**,绝不含变量取值 / list 元素值。
    抛 :class:`ValueError`(消息形如 ``"<code>: <name>"``);返回浅拷贝 dict。
    """
    if len(variables) > MAX_WORKFLOW_VARIABLES:
        raise ValueError(f"too_many_variables: 变量数量超过上限 {MAX_WORKFLOW_VARIABLES}")
    validated: dict[str, VariableValue] = {}
    for name, value in variables.items():
        if not _VARIABLE_NAME_RE.fullmatch(name):
            raise ValueError(f"invalid_variable_name: {name}")
        if name in BUILTIN_VARIABLE_NAMES:
            raise ValueError(f"variable_name_collides_builtin: {name}")
        if isinstance(value, str):
            _validate_scalar_value(name, value)
            validated[name] = value
        elif isinstance(value, list):
            if len(value) > MAX_VARIABLE_LIST_LENGTH:
                raise ValueError(f"variable_list_too_long: {name}")
            for element in value:
                if not isinstance(element, str):
                    raise ValueError(f"invalid_variable_value: {name}")
                _validate_scalar_value(name, element)
            validated[name] = list(value)
        else:
            raise ValueError(f"invalid_variable_value: {name}")
    return validated


class RetryPolicy(BaseModel):
    """节点重试策略(简单版,用户拍板):次数 + 固定退避秒数。"""

    model_config = ConfigDict(frozen=True)

    max_retries: int = Field(ge=0, le=MAX_RETRIES_LIMIT)
    backoff_seconds: int = Field(ge=0, le=MAX_BACKOFF_SECONDS)


class CronSchedule(BaseModel):
    """cron 调度(两道校验:5 段 + 字符集,再 croniter 语义解析)。"""

    model_config = ConfigDict(frozen=True)

    cron: str
    enabled: bool = True

    @field_validator("cron")
    @classmethod
    def _validate_cron(cls, value: str) -> str:
        fields = value.split()
        if len(fields) != 5:
            raise ValueError(f"invalid_cron: cron 表达式必须是 5 段空格分隔(实际 {len(fields)} 段)")
        for index, cron_field in enumerate(fields):
            if not _CRON_FIELD_RE.fullmatch(cron_field):
                raise ValueError(
                    f"invalid_cron: 第 {index + 1} 段 {cron_field!r} 含非法字符"
                    "(只允许数字与 * , - /)"
                )
        # 语义解析:字符集过关但越界 / 组合非法(如 "99 * * * *" / "5-1 * * * *")
        # croniter 才能识别;调度器用同一 croniter 求触发点,校验口径与运行口径一致。
        if not croniter.is_valid(value):
            raise ValueError(f"invalid_cron: cron 表达式 {value!r} 无法解析(字段越界或组合非法)")
        return value


class SensorTrigger(BaseModel):
    """SQL sensor 触发(C-10:数据到达触发 + 冷却期)。

    调度线程周期在 ``datasource_id`` 上执行只读 ``sql``(worker 侧连库,不在 API 进程,
    R1);结果**第一行第一列 truthy 即触发** workflow run。``check_interval_seconds``
    控制两次检查最小间隔;``cooldown_seconds`` 控制触发后多久内不再触发(冷却期)。

    ★ ``sql`` 只做长度/非空的构造期校验;**只读校验(SELECT/WITH)在路由层用
    sql_guard 落地**(写语句 → 400),worker 执行前再兜一道(纵深防御)。域层不 import
    dbclients(层次纯净),故不在此调 sql_guard。
    """

    model_config = ConfigDict(frozen=True)

    sql: str = Field(min_length=1, max_length=MAX_SENSOR_SQL_LENGTH)
    datasource_id: str = Field(min_length=1)
    check_interval_seconds: int = Field(
        ge=MIN_SENSOR_INTERVAL_SECONDS, le=MAX_SENSOR_INTERVAL_SECONDS
    )
    cooldown_seconds: int = Field(ge=0, le=MAX_SENSOR_COOLDOWN_SECONDS)
    enabled: bool = True

    @field_validator("datasource_id")
    @classmethod
    def _validate_datasource_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("invalid_sensor_datasource: datasource_id 不能为空")
        return value


class WorkflowNode(BaseModel):
    """Workflow DAG 节点(设计稿 §2.8.2)。

    ``retry_policy=None`` 表示继承全局重试策略(用户拍板语义)。
    """

    model_config = ConfigDict(frozen=True)

    id: str
    job_kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
    retry_policy: RetryPolicy | None = None
    timeout_seconds: int = Field(ge=1)
    on_failure: Literal["abort", "continue", "branch"] = "abort"
    when: str | None = Field(default=None, max_length=MAX_WHEN_LENGTH)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("invalid_node_id: 节点 id 不能为空")
        return value

    @field_validator("job_kind")
    @classmethod
    def _validate_job_kind(cls, value: str) -> str:
        if value not in ALLOWED_WORKFLOW_NODE_KINDS:
            raise ValueError(
                f"forbidden_node_kind: job_kind {value!r} 不在 R7 白名单"
                " ALLOWED_WORKFLOW_NODE_KINDS 内(契约 §4 R7,永不开放)"
            )
        if value not in SUPPORTED_WORKFLOW_NODE_KINDS_V1:
            raise ValueError(
                f"unsupported_node_kind: job_kind {value!r} 在 R7 白名单内"
                "但首版暂不支持(SUPPORTED_WORKFLOW_NODE_KINDS_V1)"
            )
        return value

    @field_validator("when")
    @classmethod
    def _validate_when(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("invalid_when: when 条件不能为空白字符串(不写请传 null)")
        return value

    @model_validator(mode="after")
    def _validate_intrinsic_payload(self) -> WorkflowNode:
        if self.job_kind == "branch":
            if self.payload:
                raise ValueError("invalid_branch_payload: branch 节点 payload 必须为空")
            return self
        if self.job_kind == "sleep":
            duration = self.payload.get("duration_seconds")
            if (
                set(self.payload) != {"duration_seconds"}
                or isinstance(duration, bool)
                or not isinstance(duration, int)
                or not 1 <= duration <= 86_400
            ):
                raise ValueError(
                    "invalid_sleep_payload: sleep 节点只接受 1..86400 的 duration_seconds"
                )
            return self
        if self.job_kind == "lineage_analyze" and "ddl_text" in self.payload:
            # ★ workflow 的 lineage_analyze 是第三条分析路径,只从元数据目录构造
            # schema_context,不接 DDL 文本数据源。此前 ddl_text 能通过校验、带进
            # 子 job、然后被**静默忽略** —— 用户以为补了列元数据,实际什么也没发生。
            # 明确拒绝,不做静默忽略。
            #
            # 没有选择接通,是因为 workflow 是被调度反复重跑的存量规格,而 ddl_text
            # 上限 1 MB 会整段内联进 workflow spec 与每次 run 的 payload;真要支持,
            # 该走 upload_id / storage_uri 引用(与同作业的 SQL zip 同款),那是独立
            # 设计,不该顺手塞进本次修复。
            raise ValueError(
                "invalid_lineage_analyze_payload: lineage_analyze 节点不支持 ddl_text"
                "(DDL 文本数据源仅在 SQL 解析与批量分析两个入口可用)"
            )
        if self.job_kind == "notify":
            if not set(self.payload) <= {"target_ids", "message"}:
                raise ValueError("invalid_notify_payload: notify 节点含未知字段")
            target_ids = self.payload.get("target_ids")
            if (
                not isinstance(target_ids, list)
                or not 1 <= len(target_ids) <= 10
                or not all(
                    isinstance(target_id, str) and target_id.strip() for target_id in target_ids
                )
                or len(set(target_ids)) != len(target_ids)
            ):
                raise ValueError("invalid_notify_payload: target_ids 必须是 1..10 个唯一非空字符串")
            message = self.payload.get("message")
            if message is not None and (not isinstance(message, str) or len(message) > 512):
                raise ValueError("invalid_notify_payload: message 最长 512 字符")
        return self


class WorkflowEdge(BaseModel):
    """DAG 有向边;旧边默认保持 source 成功后调度 target。"""

    model_config = ConfigDict(frozen=True)

    source: str
    target: str
    trigger: Literal["success", "failure"] = "success"
    when: str | None = Field(default=None, max_length=MAX_WHEN_LENGTH)
    is_default: bool = False

    @field_validator("when")
    @classmethod
    def _validate_when(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("invalid_edge_when: 边条件不能为空白字符串(不写请传 null)")
        return value


class WorkflowSpec(BaseModel):
    """Workflow 定义(设计稿 §2.8.2):节点 + 边 + 可选 cron 调度。

    构造 / ``model_validate`` 时强制 DAG 结构校验:
    id 唯一、边引用存在、无自环、无环;孤立节点合法(单节点 workflow)。
    """

    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)

    nodes: list[WorkflowNode] = Field(min_length=1, max_length=MAX_WORKFLOW_NODES)
    edges: list[WorkflowEdge] = Field(default_factory=list)
    schedule: CronSchedule | None = None
    # SQL sensor 触发(C-10;存进 dag_jsonb,冗余 sensor_enabled 列供调度器扫表)。
    # None = 无 sensor(默认)。cron 与 sensor 可并存(两条独立触发路径)。
    sensor: SensorTrigger | None = None
    # run 终态通知目标(C-9;存进 dag_jsonb,无需迁移)。空 = 不通知(默认)。
    notifications: list[NotifyTarget] = Field(default_factory=list)
    # workflow 级默认变量(C-7 PR2;存进 dag_jsonb,无需迁移)。触发时并入
    # when_variables 快照,服务 ${var} 插值 + when 求值。值走安全字符集校验。
    # str 或 list[str](list 供 ${var | sql_in} / ${var | csv} 展开;C-7 PR3)。
    variables: dict[str, VariableValue] = Field(default_factory=dict)

    @field_validator("variables")
    @classmethod
    def _validate_variables(cls, value: dict[str, VariableValue]) -> dict[str, VariableValue]:
        return validate_workflow_variables(value)

    @model_validator(mode="after")
    def _validate_dag(self) -> WorkflowSpec:
        node_ids: set[str] = set()
        for node in self.nodes:
            if node.id in node_ids:
                raise ValueError(f"duplicate_node_id: 节点 id {node.id!r} 重复")
            node_ids.add(node.id)

        adjacency: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
        incoming: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
        outgoing: dict[str, list[WorkflowEdge]] = {node_id: [] for node_id in node_ids}
        edge_keys: set[tuple[str, str, str]] = set()
        for edge in self.edges:
            if edge.source not in node_ids:
                raise ValueError(f"unknown_edge_node: 边 source {edge.source!r} 不是已声明节点")
            if edge.target not in node_ids:
                raise ValueError(f"unknown_edge_node: 边 target {edge.target!r} 不是已声明节点")
            if edge.source == edge.target:
                raise ValueError(f"self_loop: 节点 {edge.source!r} 不允许自环")
            edge_key = (edge.source, edge.target, edge.trigger)
            if edge_key in edge_keys:
                raise ValueError(
                    "duplicate_edge: "
                    f"{edge.source!r}->{edge.target!r} trigger={edge.trigger!r} 重复"
                )
            edge_keys.add(edge_key)
            adjacency[edge.source].append(edge.target)
            incoming[edge.target].append(edge.source)
            outgoing[edge.source].append(edge)

        cycle = _find_cycle(self.nodes, adjacency)
        if cycle is not None:
            raise ValueError(f"cycle_detected: DAG 存在环,环上节点: {' -> '.join(cycle)}")

        ancestors = _workflow_ancestors(self.nodes, incoming)
        nodes_by_id = {node.id: node for node in self.nodes}
        for node in self.nodes:
            _validate_output_references(
                node.payload,
                allowed_nodes=ancestors[node.id],
                nodes_by_id=nodes_by_id,
            )
            _validate_output_references(
                node.when,
                allowed_nodes=ancestors[node.id],
                nodes_by_id=nodes_by_id,
            )
        for edge in self.edges:
            _validate_output_references(
                edge.when,
                allowed_nodes=ancestors[edge.source] | {edge.source},
                nodes_by_id=nodes_by_id,
            )

        for node in self.nodes:
            _validate_node_routes(node, outgoing[node.id])

        notify_ids: set[str] = set()
        for target in self.notifications:
            if target.id in notify_ids:
                raise ValueError(f"duplicate_notify_target_id: 通知目标 id {target.id!r} 重复")
            notify_ids.add(target.id)
        for node in self.nodes:
            if node.job_kind != "notify":
                continue
            target_ids = node.payload["target_ids"]
            for target_id in target_ids:
                if target_id not in notify_ids:
                    raise ValueError(
                        f"unknown_notify_target: notify 节点 {node.id!r} 引用未知目标 {target_id!r}"
                    )
        return self


def _validate_node_routes(node: WorkflowNode, outgoing: list[WorkflowEdge]) -> None:
    success_edges = [edge for edge in outgoing if edge.trigger == "success"]
    failure_edges = [edge for edge in outgoing if edge.trigger == "failure"]

    if node.job_kind == "branch":
        _validate_first_match_routes(
            node_id=node.id,
            edges=success_edges,
            minimum_edges=2,
            error_code="invalid_branch_routes",
        )
    elif any(edge.when is not None or edge.is_default for edge in success_edges):
        raise ValueError(
            "conditional_edge_requires_branch: "
            f"普通节点 {node.id!r} 的 success 边不能设置 when/is_default"
        )

    if node.on_failure == "branch":
        if not failure_edges:
            raise ValueError(
                f"missing_failure_route: 节点 {node.id!r} on_failure='branch' 但无 failure 边"
            )
        _validate_first_match_routes(
            node_id=node.id,
            edges=failure_edges,
            minimum_edges=1,
            error_code="invalid_failure_routes",
        )
    elif failure_edges:
        raise ValueError(
            "failure_edge_requires_branch: "
            f"节点 {node.id!r} 只有 on_failure='branch' 才能声明 failure 边"
        )


def _validate_first_match_routes(
    *,
    node_id: str,
    edges: list[WorkflowEdge],
    minimum_edges: int,
    error_code: str,
) -> None:
    defaults = [edge for edge in edges if edge.is_default]
    conditions_valid = all(
        edge.when is None if edge.is_default else edge.when is not None for edge in edges
    )
    if len(edges) < minimum_edges or len(defaults) != 1 or not conditions_valid:
        raise ValueError(f"{error_code}: 节点 {node_id!r} 路由边数量/条件/default 配置非法")


def _workflow_ancestors(
    nodes: list[WorkflowNode],
    incoming: Mapping[str, list[str]],
) -> dict[str, set[str]]:
    ancestors: dict[str, set[str]] = {}

    def collect(node_id: str) -> set[str]:
        cached = ancestors.get(node_id)
        if cached is not None:
            return cached
        found: set[str] = set()
        for parent in incoming[node_id]:
            found.add(parent)
            found.update(collect(parent))
        ancestors[node_id] = found
        return found

    for node in nodes:
        collect(node.id)
    return ancestors


def _validate_output_references(
    value: object,
    *,
    allowed_nodes: set[str],
    nodes_by_id: Mapping[str, WorkflowNode],
) -> None:
    for expression in iter_placeholder_expressions(value):
        if not expression.startswith("nodes."):
            continue
        reference = parse_node_output_reference(expression)
        if reference is None:
            raise ValueError("invalid_node_output_reference")
        node_id, field = reference
        referenced_node = nodes_by_id.get(node_id)
        if referenced_node is None:
            raise ValueError("unknown_node_output")
        allowed_fields = NODE_OUTPUT_FIELDS_BY_KIND[referenced_node.job_kind]
        if field not in allowed_fields:
            raise ValueError("forbidden_node_output_field")
        if node_id not in allowed_nodes:
            raise ValueError("non_upstream_node_output")


def _find_cycle(nodes: list[WorkflowNode], adjacency: dict[str, list[str]]) -> list[str] | None:
    """迭代 DFS 三色标记找环;返回环上节点序列(source→…→source 顺序),无环返回 None。"""

    white, gray, black = 0, 1, 2
    color: dict[str, int] = {node.id: white for node in nodes}
    parent: dict[str, str] = {}
    for start in (node.id for node in nodes):
        if color[start] != white:
            continue
        color[start] = gray
        stack: list[tuple[str, int]] = [(start, 0)]
        while stack:
            current, next_index = stack[-1]
            neighbors = adjacency[current]
            if next_index >= len(neighbors):
                color[current] = black
                stack.pop()
                continue
            stack[-1] = (current, next_index + 1)
            neighbor = neighbors[next_index]
            if color[neighbor] == white:
                color[neighbor] = gray
                parent[neighbor] = current
                stack.append((neighbor, 0))
            elif color[neighbor] == gray:
                cycle_reversed = [current]
                walker = current
                while walker != neighbor:
                    walker = parent[walker]
                    cycle_reversed.append(walker)
                return list(reversed(cycle_reversed))
    return None
