from __future__ import annotations

import json

from app.domain.lineage.ai_fallback import (
    DEFAULT_INFERRED_CONFIDENCE,
    MAX_INFERRED_EDGES,
    InferredColumnEdge,
    InferredTableEdge,
    fallback_statements,
    parse_inferred_edges,
    redact_sql_literals,
)


def test_redact_sql_literals_masks_strings_and_numbers_keeps_identifiers() -> None:
    sql = (
        "INSERT INTO app.tgt2 (id, name) SELECT t1.id, 'top-secret o''brien' "
        "FROM app.src_1 t1 WHERE t1.amount > 42.5 AND t1.city = '上海'"
    )

    redacted = redact_sql_literals(sql)

    assert "top-secret" not in redacted
    assert "上海" not in redacted
    assert "42.5" not in redacted
    assert "'?'" in redacted
    # 表列名(含带数字的标识符)必须保留 —— L2 = 表名/列名/结构。
    assert "app.tgt2" in redacted
    assert "app.src_1" in redacted
    assert "t1.amount" in redacted


def test_fallback_statements_maps_parse_error_indexes_to_redacted_fragments() -> None:
    sql_text = (
        "INSERT INTO app.tgt (id) SELECT id FROM app.src WHERE name = 'secret-a'; "
        "INSERT INTO app.tgt2 (id) SELECT id FROM app.src2 WHERE name = 'secret-b'"
    )
    parse_errors = [
        {"statement_index": 1, "error_type": "unsupported_schema", "message": "x"},
        {"statement_index": 1, "error_type": "qualify_error", "message": "y"},
    ]

    fragments = fallback_statements(sql_text=sql_text, dialect="mysql", parse_errors=parse_errors)

    assert len(fragments) == 1
    assert fragments[0]["statement_index"] == 1
    assert "app.src2" in str(fragments[0]["sql"])
    assert "secret-b" not in str(fragments[0]["sql"])


def test_fallback_statements_empty_when_no_errors_or_bad_indexes() -> None:
    sql_text = "INSERT INTO app.tgt (id) SELECT id FROM app.src"

    assert fallback_statements(sql_text=sql_text, dialect="mysql", parse_errors=[]) == []
    assert (
        fallback_statements(
            sql_text=sql_text,
            dialect="mysql",
            parse_errors=[{"statement_index": 99}, {"statement_index": "x"}, "junk"],
        )
        == []
    )


def test_fallback_statements_respects_limit() -> None:
    sql_text = "; ".join(f"INSERT INTO app.tgt{i} (id) SELECT id FROM app.src{i}" for i in range(5))
    parse_errors = [{"statement_index": i} for i in range(5)]

    fragments = fallback_statements(
        sql_text=sql_text, dialect="mysql", parse_errors=parse_errors, limit=2
    )

    assert [fragment["statement_index"] for fragment in fragments] == [0, 1]


def test_fallback_statements_uses_plsql_split_for_procedures() -> None:
    sql_text = (
        "CREATE OR REPLACE PROCEDURE app.load_orders AS BEGIN "
        "INSERT INTO app.tgt (id) SELECT id FROM app.src WHERE region = 'north'; "
        "END load_orders;"
    )

    fragments = fallback_statements(
        sql_text=sql_text,
        dialect="oracle",
        parse_errors=[{"statement_index": 0}],
    )

    assert len(fragments) == 1
    assert str(fragments[0]["sql"]).startswith("INSERT INTO app.tgt")
    assert "north" not in str(fragments[0]["sql"])


def test_parse_inferred_edges_accepts_plain_and_fenced_json() -> None:
    payload = {
        "table_edges": [{"source_table": "app.src", "target_table": "app.tgt", "confidence": 0.8}],
        "column_edges": [
            {
                "source_table": "app.src",
                "source_column": "id",
                "target_table": "app.tgt",
                "target_column": "id",
                "confidence": 0.7,
            }
        ],
    }

    plain = parse_inferred_edges(json.dumps(payload))
    fenced = parse_inferred_edges(f"```json\n{json.dumps(payload)}\n```")

    for edges in (plain, fenced):
        assert edges is not None
        assert edges.table_edges == [
            InferredTableEdge(source_table="app.src", target_table="app.tgt", confidence=0.8)
        ]
        assert edges.column_edges == [
            InferredColumnEdge(
                source_table="app.src",
                source_column="id",
                target_table="app.tgt",
                target_column="id",
                confidence=0.7,
            )
        ]


def test_parse_inferred_edges_rejects_non_json() -> None:
    assert parse_inferred_edges("ok") is None
    assert parse_inferred_edges("[1, 2, 3]") is None


def test_parse_inferred_edges_clamps_confidence_and_defaults_missing() -> None:
    content = json.dumps(
        {
            "table_edges": [
                {"source_table": "a", "target_table": "b", "confidence": 7},
                {"source_table": "c", "target_table": "d"},
                {"source_table": "e", "target_table": "f", "confidence": "high"},
            ]
        }
    )

    edges = parse_inferred_edges(content)

    assert edges is not None
    assert [edge.confidence for edge in edges.table_edges] == [
        1.0,
        DEFAULT_INFERRED_CONFIDENCE,
        DEFAULT_INFERRED_CONFIDENCE,
    ]


def test_parse_inferred_edges_drops_invalid_rows_and_dedupes() -> None:
    content = json.dumps(
        {
            "table_edges": [
                {"source_table": "app.src", "target_table": "app.tgt"},
                {"source_table": "app.src", "target_table": "app.tgt"},
                {"source_table": "app.same", "target_table": "app.same"},
                {"source_table": "", "target_table": "app.tgt"},
                {"source_table": "x" * 513, "target_table": "app.tgt"},
                "junk",
            ],
            "column_edges": [
                {
                    "source_table": "app.src",
                    "source_column": "id",
                    "target_table": "app.src",
                    "target_column": "id",
                },
                {"source_table": "app.src", "source_column": "id"},
            ],
        }
    )

    edges = parse_inferred_edges(content)

    assert edges is not None
    assert len(edges.table_edges) == 1
    assert edges.column_edges == []


def test_parse_inferred_edges_bounds_edge_count() -> None:
    content = json.dumps(
        {
            "table_edges": [
                {"source_table": f"s{i}", "target_table": f"t{i}"}
                for i in range(MAX_INFERRED_EDGES + 50)
            ]
        }
    )

    edges = parse_inferred_edges(content)

    assert edges is not None
    assert len(edges.table_edges) == MAX_INFERRED_EDGES
