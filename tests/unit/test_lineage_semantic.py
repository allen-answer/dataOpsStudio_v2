"""L-4 血缘语义 domain 层单测(app/domain/lineage/semantic.py)。

直接构造 LineageReport / TargetSummary(不经真 parser),覆盖:
- classify_refresh_mode 全部 7 枚举 + None;
- identify_table_roles 结构角色 / 命名角色 / remote_dblink / primary 优先级;
- build_target_integration 跨文件聚合(counts 汇总、source_files 去重、refresh_mode);
- build_observations / build_risks 计数与 level 映射;
- build_semantic_view 端到端。
"""

from __future__ import annotations

from app.domain.lineage.models import (
    LineageReport,
    RefreshMode,
    SemanticView,
    TableRole,
    TableRoleKind,
    TargetIntegration,
    TargetSummary,
)
from app.domain.lineage.semantic import (
    build_observations,
    build_risks,
    build_semantic_view,
    build_target_integration,
    classify_refresh_mode,
    identify_table_roles,
)


def _ts(
    table: str,
    operation: str,
    statement_index: int = 0,
    has_where: bool | None = None,
) -> TargetSummary:
    return TargetSummary(
        table=table, operation=operation, statement_index=statement_index, has_where=has_where
    )


def _role(roles: list[TableRole], table_norm: str) -> TableRole:
    match = [r for r in roles if r.table.lower() == table_norm]
    assert match, f"角色未找到:{table_norm} in {[r.table for r in roles]}"
    return match[0]


def _target(targets: list[TargetIntegration], table_norm: str) -> TargetIntegration:
    match = [t for t in targets if t.table.lower() == table_norm]
    assert match, f"目标表未找到:{table_norm}"
    return match[0]


# ---------------------------------------------------------------------------
# 1. classify_refresh_mode
# ---------------------------------------------------------------------------


def test_classify_none_when_empty() -> None:
    assert classify_refresh_mode([]) is None


def test_classify_truncate_insert() -> None:
    ops = [_ts("t", "truncate", 0), _ts("t", "insert", 1)]
    assert classify_refresh_mode(ops) == RefreshMode.TRUNCATE_INSERT.value


def test_classify_delete_insert_full() -> None:
    # 无 WHERE 的 DELETE + INSERT = 全量重刷。
    ops = [_ts("t", "delete", 0, has_where=False), _ts("t", "insert", 1)]
    assert classify_refresh_mode(ops) == RefreshMode.DELETE_INSERT.value


def test_classify_delete_insert_partial() -> None:
    # 带 WHERE 的 DELETE + INSERT = 分区/增量重刷。
    ops = [_ts("t", "delete", 0, has_where=True), _ts("t", "insert", 1)]
    assert classify_refresh_mode(ops) == RefreshMode.DELETE_INSERT_PARTIAL.value


def test_classify_merge() -> None:
    assert classify_refresh_mode([_ts("t", "merge", 0)]) == RefreshMode.MERGE.value


def test_classify_update() -> None:
    assert classify_refresh_mode([_ts("t", "update", 0)]) == RefreshMode.UPDATE.value


def test_classify_append() -> None:
    ops = [_ts("t", "insert", 0), _ts("t", "insert", 1)]
    assert classify_refresh_mode(ops) == RefreshMode.APPEND.value


def test_classify_mixed() -> None:
    # insert + merge 共存但无删+插序列 → mixed。
    ops = [_ts("t", "insert", 0), _ts("t", "merge", 1)]
    assert classify_refresh_mode(ops) == RefreshMode.MIXED.value


def test_classify_create_counts_as_insert() -> None:
    # CREATE TABLE AS 计入 insert;delete(无 WHERE) 在前 → delete_insert。
    ops = [_ts("t", "delete", 0, has_where=False), _ts("t", "create", 1)]
    assert classify_refresh_mode(ops) == RefreshMode.DELETE_INSERT.value


# ---------------------------------------------------------------------------
# 2. identify_table_roles
# ---------------------------------------------------------------------------


def test_roles_target_intermediate_source_fact() -> None:
    report = LineageReport(
        target_summary=[_ts("stg.final", "insert"), _ts("stg.mid", "insert")],
        graph_edges=[
            {"source_table": "src.raw", "target_table": "stg.mid", "statement_index": 0},
            {"source_table": "stg.mid", "target_table": "stg.final", "statement_index": 1},
        ],
    )
    roles = identify_table_roles(report)

    # stg.final:仅写 → target。
    assert _role(roles, "stg.final").primary_role == TableRoleKind.TARGET.value
    # stg.mid:既写又读 → intermediate。
    assert _role(roles, "stg.mid").primary_role == TableRoleKind.INTERMEDIATE.value
    # src.raw:纯读无命名 → source_fact 兜底。
    assert _role(roles, "src.raw").primary_role == TableRoleKind.SOURCE_FACT.value


def test_roles_named_by_basename_and_schema() -> None:
    report = LineageReport(
        target_summary=[_ts("app.orders", "insert")],
        graph_edges=[
            {"source_table": "app.dim_customer", "target_table": "app.orders"},
            {"source_table": "app.ref_status", "target_table": "app.orders"},
            {"source_table": "config.job_params", "target_table": "app.orders"},
        ],
    )
    roles = identify_table_roles(report)

    assert _role(roles, "app.dim_customer").primary_role == TableRoleKind.DIMENSION.value
    assert _role(roles, "app.ref_status").primary_role == TableRoleKind.REFERENCE.value
    # config schema 段整段精确 → config。
    assert _role(roles, "config.job_params").primary_role == TableRoleKind.CONFIG.value


def test_roles_remote_dblink() -> None:
    report = LineageReport(
        target_summary=[_ts("app.orders", "insert")],
        graph_edges=[{"source_table": "scott.emp@remote_db", "target_table": "app.orders"}],
    )
    roles = identify_table_roles(report)
    entry = _role(roles, "scott.emp@remote_db")
    assert TableRoleKind.REMOTE_DBLINK.value in entry.roles
    assert entry.primary_role == TableRoleKind.REMOTE_DBLINK.value


def test_roles_primary_priority_target_over_dimension() -> None:
    # 既是 target(仅写)又命名 dim → primary=target(target 优先于 dimension)。
    report = LineageReport(target_summary=[_ts("dw.dim_date", "insert")])
    roles = identify_table_roles(report)
    entry = _role(roles, "dw.dim_date")
    assert TableRoleKind.TARGET.value in entry.roles
    assert TableRoleKind.DIMENSION.value in entry.roles
    assert entry.primary_role == TableRoleKind.TARGET.value


def test_roles_read_from_tables_source_role() -> None:
    report = LineageReport(
        target_summary=[_ts("app.tgt", "insert")],
        tables=[
            {"name": "app.tgt", "role": "target"},
            {"name": "app.plain_src", "role": "source"},
        ],
    )
    roles = identify_table_roles(report)
    assert _role(roles, "app.plain_src").primary_role == TableRoleKind.SOURCE_FACT.value


# ---------------------------------------------------------------------------
# 3. build_target_integration
# ---------------------------------------------------------------------------


def test_target_integration_cross_files() -> None:
    report_a = LineageReport(
        target_summary=[_ts("app.orders", "insert", 0)],
        graph_edges=[{"source_table": "app.raw_a", "target_table": "app.orders"}],
    )
    report_b = LineageReport(
        target_summary=[
            _ts("APP.ORDERS", "delete", 0, has_where=False),  # 大小写归一同表
            _ts("app.orders", "insert", 1),
        ],
        graph_edges=[{"source_table": "app.raw_b", "target_table": "app.orders"}],
    )
    targets = build_target_integration([("a.sql", report_a), ("b.sql", report_b)])

    assert len(targets) == 1
    tgt = _target(targets, "app.orders")
    # counts 跨文件汇总:2 insert(含 report_a、report_b 各一)+ 1 delete。
    assert tgt.counts["insert"] == 2
    assert tgt.counts["delete"] == 1
    # source_files 去重保序。
    assert tgt.source_files == ["a.sql", "b.sql"]
    # source_tables 归一去重。
    assert tgt.source_tables == ["app.raw_a", "app.raw_b"]
    # b.sql 内 delete(无 WHERE, idx0) 在 insert(idx1) 前 → delete_insert。
    assert tgt.refresh_mode == RefreshMode.DELETE_INSERT.value
    assert tgt.titles == []


def test_target_integration_empty_source_file_skipped() -> None:
    report = LineageReport(target_summary=[_ts("t", "insert")])
    targets = build_target_integration([("", report)])
    assert targets[0].source_files == []


# ---------------------------------------------------------------------------
# 4 & 5. build_observations / build_risks
# ---------------------------------------------------------------------------


def test_observations_full_reload_and_merge() -> None:
    view = SemanticView(
        targets=[
            TargetIntegration(table="t1", refresh_mode=RefreshMode.TRUNCATE_INSERT.value),
            TargetIntegration(table="t2", refresh_mode=RefreshMode.MERGE.value),
            TargetIntegration(
                table="t3",
                refresh_mode=RefreshMode.APPEND.value,
                primary_role=TableRoleKind.INTERMEDIATE.value,
            ),
        ],
        table_roles=[
            TableRole(table="r", roles=[TableRoleKind.REMOTE_DBLINK.value]),
        ],
    )
    obs = build_observations(view)
    assert obs[0] == "脚本共写入 3 张目标表"
    assert "其中 1 张走全量重刷(truncate_insert / delete_insert)" in obs
    assert "1 张走 MERGE 合并写入" in obs
    assert "1 张为中转表(既写又读)" in obs
    assert "1 张通过 DB Link 引用远端" in obs


def test_observations_empty_when_no_targets() -> None:
    assert build_observations(SemanticView()) == []


def test_risks_parse_error_and_dynamic_sql_levels() -> None:
    report = LineageReport(
        parse_errors=[{"message": "boom"}, {}],  # 第二条无 message → 默认「解析失败」
        dynamic_sql_segments=[
            {"sql": "x", "confidence": "low"},
            {"sql": "y", "confidence": "medium"},
            {"sql": "z", "confidence": "high"},  # 跳过
        ],
    )
    view = SemanticView()
    risks = build_risks([("f.sql", report)], view)

    parse = [r for r in risks if r.type == "parse_error"]
    assert [r.message for r in parse] == ["boom", "解析失败"]
    assert all(r.level == "high" for r in parse)

    dyn = [r for r in risks if r.type == "dynamic_sql_low_confidence"]
    assert len(dyn) == 2  # high 被跳过
    # 置信越低风险越高:low→medium、medium→low。
    assert {(d.level) for d in dyn} == {"medium", "low"}
    low_conf = next(d for d in dyn if "置信 low" in d.message)
    assert low_conf.level == "medium"


def test_risks_full_reload_guard() -> None:
    view = SemanticView(
        targets=[TargetIntegration(table="dw.fact", refresh_mode=RefreshMode.TRUNCATE_INSERT.value)]
    )
    risks = build_risks([], view)
    full = [r for r in risks if r.type == "full_reload"]
    assert len(full) == 1
    assert full[0].level == "medium"
    assert "dw.fact" in full[0].message


# ---------------------------------------------------------------------------
# 6. build_semantic_view 端到端
# ---------------------------------------------------------------------------


def test_build_semantic_view_end_to_end() -> None:
    report = LineageReport(
        target_summary=[
            _ts("dw.fact_sales", "truncate", 0),
            _ts("dw.fact_sales", "insert", 1),
        ],
        graph_edges=[
            {"source_table": "src.orders", "target_table": "dw.fact_sales", "statement_index": 1},
            {"source_table": "dim.dim_date", "target_table": "dw.fact_sales", "statement_index": 1},
        ],
        parse_errors=[{"message": "unsupported stmt"}],
    )
    view = build_semantic_view([("etl.sql", report)])

    assert isinstance(view, SemanticView)
    tgt = _target(view.targets, "dw.fact_sales")
    assert tgt.refresh_mode == RefreshMode.TRUNCATE_INSERT.value
    assert tgt.source_files == ["etl.sql"]

    # 观察:1 张目标表 + 1 张全量重刷。
    assert "脚本共写入 1 张目标表" in view.observations
    assert any("全量重刷" in o for o in view.observations)

    # 风险:parse_error(high) + full_reload(medium)。
    types = {r.type for r in view.risks}
    assert "parse_error" in types
    assert "full_reload" in types

    # dim.dim_date 归入 dimension 角色。
    dim = _role(view.table_roles, "dim.dim_date")
    assert dim.primary_role == TableRoleKind.DIMENSION.value
