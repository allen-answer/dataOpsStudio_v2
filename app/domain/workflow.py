"""Workflow 领域模型(设计稿 v0.3.2 §2.8、契约 §4 R7、ADR-0009)。

Workflow = Job DAG:只编排 DataOpsStudio 内置 Job,不是低配 Airflow。
本模块纯模型 + 构造期校验,零 IO(R1:domain 层禁止 import 数据库驱动)。

两层节点 kind 校验(错误语义分开,勿混淆):

- ``forbidden_node_kind`` —— kind 不在 ``ALLOWED_WORKFLOW_NODE_KINDS``(R7 红线,
  永不开放:shell / python / 任意 HTTP 等);
- ``unsupported_node_kind`` —— kind 在 R7 白名单内但不在
  ``SUPPORTED_WORKFLOW_NODE_KINDS_V1``(首版暂不支持,后续版本开放)。

``on_failure`` 类型层对齐设计稿 ``Literal["abort", "continue", "branch"]``,
但首版校验拒绝 ``"branch"``(``unsupported_on_failure``,暂不支持)。
节点级 ``when`` 条件首版只做存储 + 非空校验,表达式求值语义属执行器(PR-4)。
Cron 只做基本格式校验(5 段 + 字符集受限,不引入 croniter);精确解析属 PR-4。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.job import ALLOWED_WORKFLOW_NODE_KINDS
from app.domain.notify import NotifyTarget

# 首版(2.4.0 PR-1)支持的节点集:R7 白名单的真子集。
# notify / sleep / branch / scenario_* 在白名单内但首版执行器不实现。
SUPPORTED_WORKFLOW_NODE_KINDS_V1: frozenset[str] = frozenset(
    {
        "sql_query",
        "sql_explain",
        "compare_run",
        "lineage_analyze",
        "export_excel",
    }
)

MAX_WORKFLOW_NODES = 50
MAX_RETRIES_LIMIT = 5
MAX_BACKOFF_SECONDS = 3600
MAX_WHEN_LENGTH = 512

# cron 单字段字符集:数字 + * , - /(基本校验,精确解析属 PR-4)
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
    """cron 调度(基本格式校验:5 段空格分隔、字符集受限)。"""

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

    @field_validator("on_failure")
    @classmethod
    def _validate_on_failure(cls, value: str) -> str:
        if value == "branch":
            raise ValueError(
                "unsupported_on_failure: on_failure='branch' 首版暂不支持"
                "(类型层对齐设计稿保留,执行语义后续版本开放)"
            )
        return value

    @field_validator("when")
    @classmethod
    def _validate_when(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("invalid_when: when 条件不能为空白字符串(不写请传 null)")
        return value


class WorkflowEdge(BaseModel):
    """DAG 有向边:source 成功后 target 才可调度(执行语义属 PR-4)。"""

    model_config = ConfigDict(frozen=True)

    source: str
    target: str


class WorkflowSpec(BaseModel):
    """Workflow 定义(设计稿 §2.8.2):节点 + 边 + 可选 cron 调度。

    构造 / ``model_validate`` 时强制 DAG 结构校验:
    id 唯一、边引用存在、无自环、无环;孤立节点合法(单节点 workflow)。
    """

    model_config = ConfigDict(frozen=True)

    nodes: list[WorkflowNode] = Field(min_length=1, max_length=MAX_WORKFLOW_NODES)
    edges: list[WorkflowEdge] = Field(default_factory=list)
    schedule: CronSchedule | None = None
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
        for edge in self.edges:
            if edge.source not in node_ids:
                raise ValueError(f"unknown_edge_node: 边 source {edge.source!r} 不是已声明节点")
            if edge.target not in node_ids:
                raise ValueError(f"unknown_edge_node: 边 target {edge.target!r} 不是已声明节点")
            if edge.source == edge.target:
                raise ValueError(f"self_loop: 节点 {edge.source!r} 不允许自环")
            adjacency[edge.source].append(edge.target)

        cycle = _find_cycle(self.nodes, adjacency)
        if cycle is not None:
            raise ValueError(f"cycle_detected: DAG 存在环,环上节点: {' -> '.join(cycle)}")

        notify_ids: set[str] = set()
        for target in self.notifications:
            if target.id in notify_ids:
                raise ValueError(f"duplicate_notify_target_id: 通知目标 id {target.id!r} 重复")
            notify_ids.add(target.id)
        return self


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
