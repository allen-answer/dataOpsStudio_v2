from __future__ import annotations

import json

from app.domain.lineage import (
    LineageParseRequest,
    analyze_sql_lineage,
    build_semantic_view,
    schema_from_metadata_cache_rows,
)
from app.domain.lineage.ai_enrichment import (
    MAX_EDGES,
    MAX_OBSERVATIONS,
    MAX_TARGETS,
    build_enrichment_profile,
)
from app.domain.lineage.models import (
    LineageReport,
    SemanticRisk,
    SemanticView,
    TableRole,
    TargetIntegration,
)


def _view() -> SemanticView:
    return SemanticView(
        targets=[
            TargetIntegration(
                table="app.tgt",
                primary_role="target",
                roles=["target"],
                refresh_mode="truncate_insert",
                counts={"insert": 1, "truncate": 1},
                source_tables=["app.src"],
                titles=["加载订单"],
            )
        ],
        table_roles=[
            TableRole(table="app.tgt", roles=["target"], primary_role="target"),
            TableRole(table="app.src", roles=["source_fact"], primary_role="source_fact"),
        ],
        observations=["app.tgt 采用 TRUNCATE+INSERT 全量重刷"],
        risks=[SemanticRisk(level="high", type="full_refresh", message="全量重刷有丢数风险")],
    )


def _report() -> LineageReport:
    return LineageReport(
        statement_count=2,
        graph_edges=[{"source_table": "app.src", "target_table": "app.tgt"}],
        insert_mappings=[
            {
                "source_table": "app.src",
                "source_column": "id",
                "target_table": "app.tgt",
                "target_column": "id",
            }
        ],
        parse_errors=[{"statement_index": 0}],
        dynamic_sql_count=0,
    )


def test_build_enrichment_profile_projects_structure_and_stats() -> None:
    profile = build_enrichment_profile(_view(), _report())

    assert profile["stats"] == {
        "statement_count": 2,
        "table_edge_count": 1,
        "column_mapping_count": 1,
        "parse_error_count": 1,
        "dynamic_sql_count": 0,
    }
    assert profile["targets"][0]["table"] == "app.tgt"
    assert profile["targets"][0]["refresh_mode"] == "truncate_insert"
    assert profile["edges"] == [{"source_table": "app.src", "target_table": "app.tgt"}]
    assert profile["observations"] == ["app.tgt 采用 TRUNCATE+INSERT 全量重刷"]
    assert profile["risks"][0]["level"] == "high"
    # titles(业务注释)不投影进 AI 画像 —— 只出结构与统计。
    assert "titles" not in profile["targets"][0]


def test_build_enrichment_profile_omits_row_values_from_real_parse() -> None:
    # 从真实解析构造:画像只出表名与结构,SQL 原文 / 行值字面量绝不进画像。
    schema = schema_from_metadata_cache_rows(
        [
            {
                "cache_level": "columns",
                "schema_name": "app",
                "table_name": "src",
                "payload": [
                    {"name": "id", "type": "integer"},
                    {"name": "amount", "type": "integer"},
                ],
            },
            {
                "cache_level": "columns",
                "schema_name": "app",
                "table_name": "tgt",
                "payload": [{"name": "id", "type": "integer"}],
            },
        ]
    )
    sql_text = "INSERT INTO app.tgt (id) SELECT id FROM app.src WHERE app.src.amount > 42999777"
    report = analyze_sql_lineage(
        LineageParseRequest(
            sql_text=sql_text,
            dialect="mysql",
            schema=schema,
            default_schema=None,
        )
    )
    view = build_semantic_view([("script.sql", report)])

    profile = build_enrichment_profile(view, report)
    blob = json.dumps(profile, ensure_ascii=False)

    assert "42999777" not in blob
    assert "app.src" in blob
    assert profile["edges"] == [{"source_table": "app.src", "target_table": "app.tgt"}]


def test_build_enrichment_profile_caps_collections() -> None:
    view = SemanticView(
        targets=[
            TargetIntegration(table=f"app.t{i}", primary_role="target")
            for i in range(MAX_TARGETS + 10)
        ],
        observations=[f"obs {i}" for i in range(MAX_OBSERVATIONS + 10)],
    )
    report = LineageReport(
        statement_count=1,
        graph_edges=[
            {"source_table": f"app.s{i}", "target_table": f"app.t{i}"}
            for i in range(MAX_EDGES + 10)
        ],
    )

    profile = build_enrichment_profile(view, report)

    assert len(profile["targets"]) == MAX_TARGETS
    assert len(profile["observations"]) == MAX_OBSERVATIONS
    assert len(profile["edges"]) == MAX_EDGES


def test_build_enrichment_profile_dedupes_and_skips_blank_edges() -> None:
    report = LineageReport(
        statement_count=1,
        graph_edges=[
            {"source_table": "app.src", "target_table": "app.tgt"},
            {"source_table": "app.src", "target_table": "app.tgt"},
            {"source_table": "", "target_table": "app.tgt"},
            {"source_table": "app.src", "target_table": None},
        ],
    )

    profile = build_enrichment_profile(SemanticView(), report)

    assert profile["edges"] == [{"source_table": "app.src", "target_table": "app.tgt"}]
