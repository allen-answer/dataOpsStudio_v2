"""build_lineage_edge_rows 纯函数单测(API analyze 与 worker persist_run 共用行构造)。"""

from __future__ import annotations

from collections.abc import Callable
from itertools import count

from app.domain.lineage import build_lineage_edge_rows


def _seq_id_factory() -> Callable[[], str]:
    counter = count()

    def _factory() -> str:
        return f"id-{next(counter)}"

    return _factory


def test_build_rows_produces_expected_table_and_column_dicts() -> None:
    table_rows, column_rows = build_lineage_edge_rows(
        run_id="run-1",
        project_id="proj-1",
        sql_hash="hash-1",
        graph_edges=[{"source_table": "src", "target_table": "tgt"}],
        insert_mappings=[
            {
                "source_table": "src",
                "source_column": "id",
                "target_table": "tgt",
                "target_column": "id",
                "transformation": "AGGREGATION",
                "transformation_subtype": "SUM",
            }
        ],
        id_factory=_seq_id_factory(),
    )

    assert table_rows == [
        {
            "id": "id-0",
            "run_id": "run-1",
            "project_id": "proj-1",
            "source_table": "src",
            "target_table": "tgt",
            "edge_kind": "table",
            "inferred": False,
            "inference_status": "confirmed",
            "confidence": 1.0,
            "sql_hash": "hash-1",
        }
    ]
    assert column_rows == [
        {
            "id": "id-1",  # id_factory 在表行(id-0)之后继续,跨两表主键唯一
            "run_id": "run-1",
            "project_id": "proj-1",
            "source_table": "src",
            "source_column": "id",
            "target_table": "tgt",
            "target_column": "id",
            "transformation": "AGGREGATION",
            "transformation_subtype": "SUM",
            "inferred": False,
            "inference_status": "confirmed",
            "confidence": 1.0,
            "sql_hash": "hash-1",
        }
    ]


def test_build_rows_defaults_transformation_and_coerces_values() -> None:
    _, column_rows = build_lineage_edge_rows(
        run_id="run-1",
        project_id="proj-1",
        sql_hash="hash-1",
        graph_edges=[],
        insert_mappings=[
            {
                "source_table": "src",
                "source_column": "id",
                "target_table": "tgt",
                "target_column": "id",
                # transformation / transformation_subtype 缺省 → DIRECT
            }
        ],
    )

    assert column_rows[0]["transformation"] == "DIRECT"
    assert column_rows[0]["transformation_subtype"] == "DIRECT"


def test_build_rows_filters_incomplete_edges_and_mappings() -> None:
    table_rows, column_rows = build_lineage_edge_rows(
        run_id="run-1",
        project_id="proj-1",
        sql_hash="hash-1",
        graph_edges=[
            {"source_table": "src", "target_table": "tgt"},
            {"source_table": "src"},  # 缺 target → 丢弃
            {"target_table": "tgt"},  # 缺 source → 丢弃
        ],
        insert_mappings=[
            {
                "source_table": "src",
                "source_column": "id",
                "target_table": "tgt",
                "target_column": "id",
            },
            {  # 缺 target_column → 丢弃
                "source_table": "src",
                "source_column": "id",
                "target_table": "tgt",
            },
        ],
    )

    assert len(table_rows) == 1
    assert len(column_rows) == 1
