"""定位 SQL 血缘反推纯函数单测(app/domain/compare_upstream_sql.py)。

覆盖设计稿 D4 的判定矩阵:DIRECT 放行 / CAST 降级 / EXPRESSION 断链 /
inferred 默认排除 / 复合主键部分可追 / 置信度取最小值并 clamp。
"""

from __future__ import annotations

from app.domain.compare_upstream_sql import (
    CONFIDENCE_FLOOR,
    UpstreamEdge,
    build_upstream_hops,
    dedupe_pk_rows,
    hop_risks,
    render_upstream_sql,
    upstream_header_comment,
)
from app.domain.datasource import DbType


def _edge(
    *,
    source_table: str,
    source_column: str,
    target_table: str,
    target_column: str,
    subtype: str = "DIRECT",
    transformation: str = "DIRECT",
    inference_status: str = "confirmed",
    confidence: float = 1.0,
    run_id: str = "run-1",
) -> UpstreamEdge:
    return UpstreamEdge(
        edge_id=f"{source_table}.{source_column}->{target_table}.{target_column}",
        run_id=run_id,
        source_table=source_table,
        source_column=source_column,
        target_table=target_table,
        target_column=target_column,
        transformation=transformation,
        transformation_subtype=subtype,
        inference_status=inference_status,
        confidence=confidence,
    )


def test_direct_edge_maps_primary_key_to_upstream_column() -> None:
    hops = build_upstream_hops(
        focus_table="ads.orders_agg",
        key_columns=["order_id"],
        edges=[
            _edge(
                source_table="dwd.orders_clean",
                source_column="id",
                target_table="ads.orders_agg",
                target_column="order_id",
            )
        ],
    )

    assert len(hops) == 1
    hop = hops[0]
    assert hop.available is True
    assert hop.depth == 1
    assert hop.table == "dwd.orders_clean"
    assert hop.key_columns == ["id"]
    assert hop.warnings == []
    assert hop.confidence == 1.0


def test_expression_edge_breaks_the_chain_with_reason() -> None:
    hops = build_upstream_hops(
        focus_table="ads.orders_agg",
        key_columns=["order_id"],
        edges=[
            _edge(
                source_table="ods.orders_raw",
                source_column="order_no",
                target_table="ads.orders_agg",
                target_column="order_id",
                subtype="EXPRESSION",
            )
        ],
    )

    assert len(hops) == 1
    assert hops[0].available is False
    assert hops[0].reason == "non_invertible_transformation"
    assert hops[0].blocked_by == "EXPRESSION"


def test_cast_edge_is_generated_but_carries_a_value_mismatch_warning() -> None:
    hops = build_upstream_hops(
        focus_table="ads.orders_agg",
        key_columns=["order_id"],
        edges=[
            _edge(
                source_table="dwd.orders_clean",
                source_column="order_id",
                target_table="ads.orders_agg",
                target_column="order_id",
                subtype="CAST",
                confidence=0.9,
            )
        ],
    )

    assert hops[0].available is True
    assert "cast_value_mismatch_risk" in hops[0].warnings
    assert hops[0].confidence == 0.9


def test_unconfirmed_inferred_edge_is_excluded_unless_opted_in() -> None:
    edges = [
        _edge(
            source_table="ods.orders_raw",
            source_column="order_no",
            target_table="ads.orders_agg",
            target_column="order_id",
            inference_status="inferred",
            confidence=0.74,
        )
    ]

    excluded = build_upstream_hops(
        focus_table="ads.orders_agg", key_columns=["order_id"], edges=edges
    )
    assert excluded[0].available is False
    assert excluded[0].reason == "unconfirmed_inferred_edge"

    included = build_upstream_hops(
        focus_table="ads.orders_agg",
        key_columns=["order_id"],
        edges=edges,
        include_inferred=True,
    )
    assert included[0].available is True
    assert "inferred_edge_used" in included[0].warnings
    assert included[0].confidence == 0.74


def test_rejected_edge_is_never_used_even_with_include_inferred() -> None:
    hops = build_upstream_hops(
        focus_table="ads.orders_agg",
        key_columns=["order_id"],
        edges=[
            _edge(
                source_table="ods.orders_raw",
                source_column="order_no",
                target_table="ads.orders_agg",
                target_column="order_id",
                inference_status="rejected",
            )
        ],
        include_inferred=True,
    )

    assert hops[0].available is False
    assert hops[0].reason == "rejected_edge"


def test_composite_key_partially_traceable_still_generates_a_superset() -> None:
    hops = build_upstream_hops(
        focus_table="ads.orders_agg",
        key_columns=["order_id", "region"],
        edges=[
            _edge(
                source_table="dwd.orders_clean",
                source_column="id",
                target_table="ads.orders_agg",
                target_column="order_id",
            ),
            # region 经表达式加工 -> 该位置追不到
            _edge(
                source_table="dwd.orders_clean",
                source_column="region_code",
                target_table="ads.orders_agg",
                target_column="region",
                subtype="EXPRESSION",
            ),
        ],
    )

    assert len(hops) == 1
    hop = hops[0]
    assert hop.available is True
    assert hop.key_columns == ["id", None]
    assert hop.resolved_columns == ["id"]
    assert "partial_key" in hop.warnings


def test_multi_hop_walk_follows_direct_edges_and_stops_at_depth() -> None:
    edges = [
        _edge(
            source_table="dwd.orders_clean",
            source_column="order_id",
            target_table="ads.orders_agg",
            target_column="order_id",
        ),
        _edge(
            source_table="ods.orders_raw",
            source_column="raw_order_id",
            target_table="dwd.orders_clean",
            target_column="order_id",
            confidence=0.95,
        ),
    ]

    two_hops = build_upstream_hops(
        focus_table="ads.orders_agg", key_columns=["order_id"], edges=edges
    )
    assert [(h.depth, h.table) for h in two_hops] == [
        (1, "dwd.orders_clean"),
        (2, "ods.orders_raw"),
    ]
    assert two_hops[1].key_columns == ["raw_order_id"]
    assert two_hops[1].confidence == 0.95

    capped = build_upstream_hops(
        focus_table="ads.orders_agg", key_columns=["order_id"], edges=edges, max_depth=1
    )
    assert [h.depth for h in capped] == [1]


def test_fan_in_upstreams_are_reported_as_separate_hops() -> None:
    """同一列多个上游(UNION / 多脚本汇入)不合并 —— 物理表不同,SQL 必须分开。"""
    hops = build_upstream_hops(
        focus_table="ads.orders_agg",
        key_columns=["order_id"],
        edges=[
            _edge(
                source_table="dwd.orders_cn",
                source_column="order_id",
                target_table="ads.orders_agg",
                target_column="order_id",
            ),
            _edge(
                source_table="dwd.orders_us",
                source_column="order_id",
                target_table="ads.orders_agg",
                target_column="order_id",
            ),
        ],
    )

    assert sorted(hop.table for hop in hops) == ["dwd.orders_cn", "dwd.orders_us"]


def test_confidence_is_clamped_to_the_floor() -> None:
    hops = build_upstream_hops(
        focus_table="ads.orders_agg",
        key_columns=["order_id"],
        edges=[
            _edge(
                source_table="dwd.orders_clean",
                source_column="order_id",
                target_table="ads.orders_agg",
                target_column="order_id",
                inference_status="inferred",
                confidence=0.05,
            )
        ],
        include_inferred=True,
    )

    assert hops[0].confidence == CONFIDENCE_FLOOR


def test_render_upstream_sql_quotes_per_dialect() -> None:
    mysql = render_upstream_sql(
        DbType.MYSQL,
        table="ods.orders_raw",
        columns=["order_id"],
        pk_rows=[[1001], [1002]],
    )
    assert mysql == "SELECT * FROM `ods`.`orders_raw` WHERE `order_id` IN (1001, 1002)"

    composite = render_upstream_sql(
        DbType.POSTGRESQL,
        table="ods.orders_raw",
        columns=["order_id", "region_code"],
        pk_rows=[[1001, "CN"]],
    )
    assert composite is not None
    assert "IN ((1001, 'CN'))" in composite

    assert (
        render_upstream_sql(DbType.MYSQL, table="ods.orders_raw", columns=[], pk_rows=[[1001]])
        is None
    )


def test_dedupe_pk_rows_keeps_order() -> None:
    assert dedupe_pk_rows([[1], [2], [1], [3]]) == [[1], [2], [3]]


def test_header_comment_carries_confidence_and_risks() -> None:
    hops = build_upstream_hops(
        focus_table="ads.orders_agg",
        key_columns=["order_id"],
        edges=[
            _edge(
                source_table="dwd.orders_clean",
                source_column="order_id",
                target_table="ads.orders_agg",
                target_column="order_id",
                subtype="CAST",
                confidence=0.9,
            )
        ],
    )
    risks = hop_risks(hops[0], missing_columns=[])
    header = upstream_header_comment(hops[0], lineage_run_id="run-1", risks=risks)

    assert header.startswith("-- 血缘溯源 · run run-1 · hop1 · dwd.orders_clean")
    assert "置信度 90%" in header
    assert "cast_value_mismatch_risk" in header
    # 主键沿链未改名的假设凡生成即成立,必须每次都写出来
    assert "pk_name_stability_assumed" in header
    assert "平台不执行" in header
    assert all(line.startswith("--") for line in header.splitlines())
