from __future__ import annotations

import pytest

from app.domain.lineage import (
    LineageParseRequest,
    analyze_sql_lineage,
    detect_dialect,
    resolve_dialect,
)
from app.domain.lineage.detect import AUTO_DIALECT, DETECT_CANDIDATES


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        # Oracle: NVL / ROWNUM / DUAL / (+) 外连接
        ("SELECT NVL(a, 0) FROM t WHERE ROWNUM <= 10", "oracle"),
        ("SELECT SYSDATE FROM dual", "oracle"),
        ("SELECT a FROM t, s WHERE t.id = s.id (+)", "oracle"),
        # TSQL: TOP n / ISNULL / 方括号标识符 / @变量
        ("SELECT TOP 10 * FROM [dbo].[orders]", "tsql"),
        ("SELECT ISNULL(a, 0), LEN(b) FROM t", "tsql"),
        ("DECLARE @x INT; SELECT DATEADD(day, 1, GETDATE())", "tsql"),
        # Postgres: :: 转换 / RETURNING / ILIKE / JSONB
        ("INSERT INTO t (a) VALUES (1) RETURNING id", "postgres"),
        ("SELECT a::int FROM t WHERE b ILIKE 'x%'", "postgres"),
        ("SELECT data->>'k' FROM t WHERE meta @> '{}'::jsonb", "postgres"),
        # MySQL: 反引号 / AUTO_INCREMENT / IFNULL / LIMIT o,c
        ("SELECT `id` FROM `orders` LIMIT 10, 20", "mysql"),
        ("SELECT IFNULL(a, 0) FROM t", "mysql"),
    ],
)
def test_detect_dialect_by_features(sql: str, expected: str) -> None:
    detection = detect_dialect(sql, default="oracle")
    assert detection.dialect == expected
    assert detection.auto_detected is True
    assert detection.method in {"heuristic", "trial_parse"}


def test_detect_no_features_falls_back_to_default() -> None:
    # 无任何方言特征 → 回落 default(现行为不变)。
    detection = detect_dialect("INSERT INTO app.tgt SELECT id FROM app.src", default="mysql")
    assert detection.dialect == "mysql"
    assert detection.method == "default"
    assert detection.auto_detected is True
    assert all(score == 0 for score in detection.scores.values())


def test_detect_default_param_used_on_tie_break() -> None:
    detection = detect_dialect("SELECT 1", default="dm")
    assert detection.dialect == "dm"
    assert detection.method == "default"


def test_resolve_explicit_dialect_always_wins() -> None:
    # 显式方言原样透传,绝不触发识别;哪怕 SQL 里全是别方言特征。
    detection = resolve_dialect("mysql", "SELECT NVL(a,0) FROM dual", default="oracle")
    assert detection.dialect == "mysql"
    assert detection.auto_detected is False
    assert detection.method == "explicit"


def test_resolve_explicit_normalizes_case_and_whitespace() -> None:
    detection = resolve_dialect("  Oracle ", "SELECT 1", default="mysql")
    assert detection.dialect == "oracle"
    assert detection.auto_detected is False


def test_resolve_auto_sentinel_triggers_detection() -> None:
    detection = resolve_dialect(AUTO_DIALECT, "SELECT TOP 5 * FROM [t]", default="oracle")
    assert detection.dialect == "tsql"
    assert detection.auto_detected is True


def test_resolve_auto_is_case_insensitive() -> None:
    detection = resolve_dialect("AUTO", "SELECT `id` FROM `t`", default="oracle")
    assert detection.dialect == "mysql"
    assert detection.auto_detected is True


def test_detected_dialect_parses_and_analyzes() -> None:
    # 识别结果必须是解析器接受的方言(端到端 sanity:tsql 已放开)。
    detection = detect_dialect("SELECT TOP 10 * FROM [dbo].[orders]", default="oracle")
    assert detection.dialect == "tsql"
    report = analyze_sql_lineage(
        LineageParseRequest(
            sql_text="INSERT INTO app.tgt (id) SELECT TOP 10 id FROM app.src",
            dialect=detection.dialect,
            schema={"app": {"tgt": {"id": "int"}, "src": {"id": "int"}}},
            default_schema="app",
        )
    )
    assert any(edge["source_table"] == "app.src" for edge in report.graph_edges)


def test_as_summary_is_json_serializable_shape() -> None:
    summary = detect_dialect("SELECT NVL(a,0) FROM dual", default="oracle").as_summary()
    assert summary["dialect"] == "oracle"
    assert summary["auto_detected"] is True
    assert "scores" in summary and isinstance(summary["scores"], dict)
    assert set(summary["scores"]) == set(DETECT_CANDIDATES)
