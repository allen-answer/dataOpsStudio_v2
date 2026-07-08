"""C-8 trace_compare:沿列级血缘上游链逐跳生成 ``compare_run`` 节点组装成 workflow。

纯函数 + 构造期校验,**零 IO**(R1:domain 层禁 import 数据库驱动)。

给定一条列级血缘上游链(链上每个节点 = 某张表上的被追踪字段),为链上**每个节点**生成
一个 ``compare_run`` 工作流节点:在两套环境(``source_id`` / ``target_id`` 两个数据源)之间,
按共享主键 ``key_columns`` 对比该表的被追踪字段。节点串成**线性 DAG**(逐跳顺序,与
血缘链一致),供用户在 workflow 页一键触发,定位"哪一跳 / 哪张表把数据搞错"。

设计裁决(见 PR body):

- **两数据源(两套环境)对比**:每个节点在 ``source_id`` / ``target_id`` 之间对比同名表的
  被追踪字段;第一处已发散的表即污染引入点。(另一路"链内上下游表互比"需跨变换列映射,
  首版不做。)
- **复用现有 ``compare_run`` 节点**(R7:不新增节点类型):节点 payload 形状对齐交互式
  ``run_compare_task``(``source_ref`` / ``target_ref`` / ``columns`` / ``compare_rules`` /
  ``run_limits``)。``run_id`` 与 bucket 由 workflow 执行器在子 job 入队时注入(与所有
  ``compare_run`` 工作流节点同一口径,非本模块职责)。
- **只生成不触发**:本模块产出 :class:`WorkflowSpec`,由 API 落库返回 ``workflow_id``。
- **主键跨链稳定假设**:首版所有节点共用同一组 ``key_columns``(join 键在链上不改名);
  链上字段改名由被追踪列名逐跳变化承载,主键改名需用户在 workflow 页手工调整。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from app.domain.schema import Column, ColumnType
from app.domain.workflow import WorkflowEdge, WorkflowNode, WorkflowSpec

__all__ = [
    "TRACE_COMPARE_MAX_HOPS",
    "TraceChainNode",
    "TraceCompareBuildError",
    "TraceCompareNode",
    "build_trace_compare_nodes",
    "build_trace_compare_spec",
]

# 单次 trace_compare 生成的 compare 节点数上限(防爆:链爆炸时 workflow 节点数失控)。
# 与 workflow.MAX_WORKFLOW_NODES(50)留足余量;超出由 API 映射成 400。
TRACE_COMPARE_MAX_HOPS = 20

# compare 节点默认超时(秒),对齐 compare run_limits.query_timeout_seconds 默认。
_DEFAULT_NODE_TIMEOUT_SECONDS = 1800

# 标识符位安全字符集(表名 / 列名):这些值会进 compare_ref 的 table_name / columns /
# key_columns 最终拼进 SQL 标识符位,必须白名单校验(纵深防御,与 C-7 §5.3 同口径)。
# 允许 字母 / 数字 / `_` / `.`(schema.table) / `$`(部分方言合法标识符)。
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.$]*$")


class TraceCompareBuildError(ValueError):
    """trace_compare 计划构造失败(空链 / 超上限 / 缺主键 / 非法标识符)。

    消息形如 ``"<code>: <detail>"``;由 API 层映射成结构化 400。
    """


@dataclass(frozen=True)
class TraceChainNode:
    """血缘链上一个待对比节点:表 ``table`` 上的被追踪字段 ``column``(``depth`` 用于排序)。"""

    depth: int
    table: str
    column: str


class TraceCompareNode(BaseModel):
    """单个 compare 节点的生成结果:预览字段 + 完整 workflow 节点 payload。"""

    node_id: str
    depth: int
    table: str
    column: str
    key_columns: list[str]
    # compare_run 工作流节点 payload(形状对齐 run_compare_task;run_id/bucket 由执行器注入)。
    payload: dict[str, Any]


def _validate_identifier(kind: str, value: str) -> None:
    if not value or not _IDENTIFIER_RE.fullmatch(value):
        # ★ R5:只暴露种类 + 值本身是表 / 列标识符(非敏感);不含数据行值。
        raise TraceCompareBuildError(f"unsafe_{kind}: {value!r}")


def _split_qualified_table(table: str, schema_name: str | None) -> tuple[str | None, str]:
    """把血缘的限定表名 ``schema.table`` 拆成 (schema, table);已显式给 schema 时不拆。"""
    if schema_name is not None:
        return schema_name, table
    if "." in table:
        left, _, right = table.rpartition(".")
        return left, right
    return None, table


def _compare_ref(schema: str | None, table: str) -> dict[str, Any]:
    ref: dict[str, Any] = {"kind": "table", "table_name": table}
    if schema:
        ref["schema_name"] = schema
    return ref


def _node_columns(column: str, key_columns: Sequence[str]) -> list[dict[str, Any]]:
    """节点 columns = 主键列 + 被追踪列(去重,主键标 primary_key)。类型未知(血缘不带类型)。"""
    ordered: list[str] = []
    for name in [*key_columns, column]:
        if name not in ordered:
            ordered.append(name)
    key_set = set(key_columns)
    return [
        Column(name=name, type=ColumnType.UNKNOWN, primary_key=name in key_set).model_dump(
            mode="json"
        )
        for name in ordered
    ]


def build_trace_compare_nodes(
    *,
    chain: Sequence[TraceChainNode],
    source_id: str,
    target_id: str,
    key_columns: Sequence[str],
    schema_name: str | None = None,
    max_hops: int = TRACE_COMPARE_MAX_HOPS,
) -> list[TraceCompareNode]:
    """把血缘上游链转成逐跳 compare 节点列表(纯函数,可单测)。

    - ``chain`` 为空 → :class:`TraceCompareBuildError` ``no_upstream_lineage``;
    - ``len(chain) > max_hops`` → ``too_many_hops``(防爆);
    - ``key_columns`` 为空 → ``key_columns_required``(无 join 键无法行级对比);
    - 表名 / 主键含非法标识符字符 → ``unsafe_table`` / ``unsafe_key_column``(注入防御)。

    节点顺序 = 传入 ``chain`` 顺序(API 侧已按 depth 升序去重);``node_id`` 形如 ``hop{i}``。
    """
    if not key_columns:
        raise TraceCompareBuildError("key_columns_required: 逐跳对比需至少一个 join 主键列")
    if not chain:
        raise TraceCompareBuildError("no_upstream_lineage: 焦点字段无上游血缘,无可对比的跳")
    cap = max(1, min(int(max_hops), TRACE_COMPARE_MAX_HOPS))
    if len(chain) > cap:
        raise TraceCompareBuildError(
            f"too_many_hops: 链长 {len(chain)} 超过上限 {cap}(缩小 max_hops 或收窄追溯深度)"
        )
    keys = list(key_columns)
    for key in keys:
        _validate_identifier("key_column", key)

    nodes: list[TraceCompareNode] = []
    for index, chain_node in enumerate(chain):
        _validate_identifier("table", chain_node.table)
        _validate_identifier("column", chain_node.column)
        schema, table = _split_qualified_table(chain_node.table, schema_name)
        payload: dict[str, Any] = {
            "source_id": source_id,
            "target_id": target_id,
            "source_ref": _compare_ref(schema, table),
            "target_ref": _compare_ref(schema, table),
            "columns": _node_columns(chain_node.column, keys),
            "compare_rules": {"key_columns": keys},
            "run_limits": {},
        }
        nodes.append(
            TraceCompareNode(
                node_id=f"hop{index}",
                depth=chain_node.depth,
                table=chain_node.table,
                column=chain_node.column,
                key_columns=keys,
                payload=payload,
            )
        )
    return nodes


def build_trace_compare_spec(
    nodes: Sequence[TraceCompareNode],
    *,
    timeout_seconds: int = _DEFAULT_NODE_TIMEOUT_SECONDS,
) -> WorkflowSpec:
    """把逐跳 compare 节点串成线性 DAG(``hop0 -> hop1 -> ...``)的 :class:`WorkflowSpec`。

    线性串行反映血缘链顺序(逐跳)。各 compare 节点结果彼此独立,串行只决定调度次序,
    不影响对比结论;R7 双门禁在 ``WorkflowNode`` 构造期对 ``job_kind='compare_run'`` 生效。
    """
    if not nodes:
        raise TraceCompareBuildError("no_upstream_lineage: 无节点可组装 workflow")
    workflow_nodes = [
        WorkflowNode(
            id=node.node_id,
            job_kind="compare_run",
            payload=node.payload,
            timeout_seconds=timeout_seconds,
        )
        for node in nodes
    ]
    edges = [
        WorkflowEdge(source=nodes[i].node_id, target=nodes[i + 1].node_id)
        for i in range(len(nodes) - 1)
    ]
    return WorkflowSpec(nodes=workflow_nodes, edges=edges)
