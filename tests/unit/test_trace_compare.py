"""C-8 trace_compare 纯函数单测:链→compare 节点 payload、hop 上限、断链、R7 白名单。"""

from __future__ import annotations

import pytest

from app.domain.lineage.trace_compare import (
    TRACE_COMPARE_MAX_HOPS,
    TraceChainNode,
    TraceCompareBuildError,
    build_trace_compare_nodes,
    build_trace_compare_spec,
)
from app.domain.workflow import SUPPORTED_WORKFLOW_NODE_KINDS_V1


def _chain(*pairs: tuple[str, str]) -> list[TraceChainNode]:
    return [
        TraceChainNode(depth=index, table=table, column=column)
        for index, (table, column) in enumerate(pairs)
    ]


def test_build_nodes_generates_per_hop_compare_payload() -> None:
    chain = _chain(("app.orders", "amount"), ("app.stg_orders", "amt"))
    nodes = build_trace_compare_nodes(
        chain=chain,
        source_id="ds-prod",
        target_id="ds-test",
        key_columns=["id"],
    )

    assert [node.node_id for node in nodes] == ["hop0", "hop1"]
    first = nodes[0].payload
    # 两套环境:同表在 source/target 两数据源间对比
    assert first["source_id"] == "ds-prod"
    assert first["target_id"] == "ds-test"
    # 限定表名拆成 schema + table
    assert first["source_ref"] == {"kind": "table", "schema_name": "app", "table_name": "orders"}
    assert first["target_ref"] == {"kind": "table", "schema_name": "app", "table_name": "orders"}
    assert first["compare_rules"]["key_columns"] == ["id"]
    # columns = 主键 + 被追踪列(主键标 primary_key)
    names = [column["name"] for column in first["columns"]]
    assert names == ["id", "amount"]
    key_col = next(column for column in first["columns"] if column["name"] == "id")
    assert key_col["primary_key"] is True
    # 第二跳被追踪列名随血缘改名(amt)
    assert nodes[1].column == "amt"
    assert [column["name"] for column in nodes[1].payload["columns"]] == ["id", "amt"]


def test_traced_column_reused_as_key_not_duplicated() -> None:
    chain = _chain(("app.orders", "id"))
    nodes = build_trace_compare_nodes(chain=chain, source_id="a", target_id="b", key_columns=["id"])
    assert [column["name"] for column in nodes[0].payload["columns"]] == ["id"]


def test_explicit_schema_name_not_split_from_table() -> None:
    chain = _chain(("orders", "amount"))
    nodes = build_trace_compare_nodes(
        chain=chain,
        source_id="a",
        target_id="b",
        key_columns=["id"],
        schema_name="dw",
    )
    assert nodes[0].payload["source_ref"] == {
        "kind": "table",
        "schema_name": "dw",
        "table_name": "orders",
    }


def test_empty_chain_rejected() -> None:
    with pytest.raises(TraceCompareBuildError, match="no_upstream_lineage"):
        build_trace_compare_nodes(chain=[], source_id="a", target_id="b", key_columns=["id"])


def test_missing_key_columns_rejected() -> None:
    with pytest.raises(TraceCompareBuildError, match="key_columns_required"):
        build_trace_compare_nodes(
            chain=_chain(("app.t", "c")), source_id="a", target_id="b", key_columns=[]
        )


def test_hop_cap_exceeded_rejected() -> None:
    chain = _chain(*[(f"app.t{i}", "c") for i in range(6)])
    with pytest.raises(TraceCompareBuildError, match="too_many_hops"):
        build_trace_compare_nodes(
            chain=chain, source_id="a", target_id="b", key_columns=["id"], max_hops=5
        )


def test_hop_cap_clamped_to_module_max() -> None:
    # max_hops 请求值超过模块硬顶时被夹到 TRACE_COMPARE_MAX_HOPS
    chain = _chain(*[(f"app.t{i}", "c") for i in range(TRACE_COMPARE_MAX_HOPS + 1)])
    with pytest.raises(TraceCompareBuildError, match="too_many_hops"):
        build_trace_compare_nodes(
            chain=chain, source_id="a", target_id="b", key_columns=["id"], max_hops=999
        )


# 含危险字符的标识符(引号 / 分号 / 空格 / 注释符 / 反引号):进 SQL 标识符位必被拒
_INJECTION_TOKENS = ["a b", "a;b", "a'b", 'a"b', "a`b", "a--b", "a/*b", "a)b"]


@pytest.mark.parametrize("bad", _INJECTION_TOKENS)
def test_unsafe_table_identifier_rejected(bad: str) -> None:
    with pytest.raises(TraceCompareBuildError, match="unsafe_"):
        build_trace_compare_nodes(
            chain=_chain((bad, "c")), source_id="a", target_id="b", key_columns=["id"]
        )


@pytest.mark.parametrize("bad", [*_INJECTION_TOKENS, "1abc", ""])
def test_unsafe_key_column_rejected(bad: str) -> None:
    with pytest.raises(TraceCompareBuildError, match="unsafe_key_column"):
        build_trace_compare_nodes(
            chain=_chain(("app.t", "c")), source_id="a", target_id="b", key_columns=[bad]
        )


def test_spec_is_linear_dag_of_compare_run_nodes() -> None:
    nodes = build_trace_compare_nodes(
        chain=_chain(("app.a", "c"), ("app.b", "c"), ("app.d", "c")),
        source_id="s",
        target_id="t",
        key_columns=["id"],
    )
    spec = build_trace_compare_spec(nodes)

    assert [node.job_kind for node in spec.nodes] == ["compare_run"] * 3
    # 线性串行:hop0 -> hop1 -> hop2
    assert [(edge.source, edge.target) for edge in spec.edges] == [
        ("hop0", "hop1"),
        ("hop1", "hop2"),
    ]


def test_generated_nodes_stay_in_r7_whitelist() -> None:
    # R7:生成的节点 kind 必须落在首版支持集(WorkflowSpec 构造期已双门禁,这里显式断言)
    nodes = build_trace_compare_nodes(
        chain=_chain(("app.a", "c")), source_id="s", target_id="t", key_columns=["id"]
    )
    spec = build_trace_compare_spec(nodes)
    assert all(node.job_kind in SUPPORTED_WORKFLOW_NODE_KINDS_V1 for node in spec.nodes)


def test_spec_roundtrips_through_workflowspec_validation() -> None:
    from app.domain.workflow import WorkflowSpec

    nodes = build_trace_compare_nodes(
        chain=_chain(("app.a", "c"), ("app.b", "c")),
        source_id="s",
        target_id="t",
        key_columns=["id"],
    )
    spec = build_trace_compare_spec(nodes)
    restored = WorkflowSpec.model_validate(spec.model_dump(mode="json"))
    assert restored == spec
