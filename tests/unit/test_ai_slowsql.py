from __future__ import annotations

from app.domain.ai_slowsql import (
    BaselineStats,
    build_diagnose_prompt,
    build_plan_payload,
    build_table_stats,
    extract_table_refs,
    mask_sql,
    summarize_baseline,
)
from app.domain.schema import Column, ColumnType, Index


def _cols(n: int) -> list[Column]:
    return [Column(name=f"c{i}", type=ColumnType.INTEGER) for i in range(n)]


def test_mask_sql_redacts_literals_keeps_structure() -> None:
    masked = mask_sql("SELECT * FROM orders WHERE name = 'alice' AND amount > 100")
    # 字面量被抹掉,表名 / 列名 / 结构保留(L3 出站前置)。
    assert "alice" not in masked
    assert "100" not in masked
    assert "orders" in masked
    assert "name" in masked


def test_extract_table_refs_returns_schema_and_table_dedup() -> None:
    sql = "SELECT o.id FROM app.orders o JOIN customers c ON o.cid = c.id JOIN app.orders o2"
    refs = extract_table_refs(sql, dialect="mysql")
    assert ("app", "orders") in refs
    assert (None, "customers") in refs
    # app.orders 出现两次,仅收一次(保序去重)。
    assert refs.count(("app", "orders")) == 1


def test_extract_table_refs_unparseable_returns_empty() -> None:
    assert extract_table_refs(")))not sql(((", dialect="mysql") == []


def test_build_table_stats_emits_structure_and_indexes_only() -> None:
    tables = [
        (
            "app",
            "orders",
            [
                Column(name="id", type=ColumnType.INTEGER, primary_key=True, nullable=False),
                Column(name="amount", type=ColumnType.DECIMAL),
            ],
            [Index(name="idx_amount", columns=["amount"], is_unique=False)],
        )
    ]
    payload, tables_used, truncated = build_table_stats(tables)
    assert truncated is False
    assert tables_used == ["app.orders"]
    assert payload[0]["schema"] == "app"
    assert payload[0]["table"] == "orders"
    assert payload[0]["columns"] == [
        {"name": "id", "type": "integer", "nullable": False, "primary_key": True},
        {"name": "amount", "type": "decimal", "nullable": True, "primary_key": False},
    ]
    assert payload[0]["indexes"] == [
        {"name": "idx_amount", "columns": ["amount"], "unique": False, "primary": False}
    ]
    # ★ egress L2 guard:列项只含结构键,绝无 value/sample/行数据键。
    for col in payload[0]["columns"]:
        assert set(col.keys()) == {"name", "type", "nullable", "primary_key"}


def test_build_table_stats_truncates_tables_and_columns() -> None:
    tables: list[tuple[str, str, list[Column], list[Index]]] = [
        (f"s{i}", f"t{i}", _cols(2), []) for i in range(20)
    ]
    payload, tables_used, truncated = build_table_stats(tables, max_tables=5)
    assert truncated is True
    assert len(payload) == 5
    assert len(tables_used) == 5

    wide: list[tuple[str, str, list[Column], list[Index]]] = [("app", "wide", _cols(100), [])]
    _, _, col_truncated = build_table_stats(wide, max_columns_per_table=10)
    assert col_truncated is True


def test_build_plan_payload_json_safe_and_truncates() -> None:
    payload, truncated = build_plan_payload(
        ["QUERY PLAN"],
        [["Seq Scan on orders"], [{"nested": 1}]],
        max_rows=10,
    )
    assert truncated is False
    assert payload["columns"] == ["QUERY PLAN"]
    # 非标量单元格被 str 化(json-safe),不丢信息也不带对象。
    assert payload["rows"][0] == ["Seq Scan on orders"]
    assert isinstance(payload["rows"][1][0], str)

    _, over = build_plan_payload(["c"], [[i] for i in range(50)], max_rows=10)
    assert over is True


def test_summarize_baseline_available_and_empty() -> None:
    assert summarize_baseline(BaselineStats(runs=0)) == {"available": False}
    summary = summarize_baseline(
        BaselineStats(runs=4, avg_seconds=1.2345, min_seconds=0.5, max_seconds=3.0, p95_seconds=2.9)
    )
    assert summary["available"] is True
    assert summary["runs"] == 4
    assert summary["avg_seconds"] == 1.234  # 3 位小数取整


def test_build_diagnose_prompt_reflects_plan_and_baseline_flags() -> None:
    with_all = build_diagnose_prompt(dialect="postgresql", has_plan=True, baseline_available=True)
    assert "postgresql" in with_all
    assert "execution plan (EXPLAIN) is provided" in with_all
    assert "baseline for this exact query is provided" in with_all

    degraded = build_diagnose_prompt(dialect="mysql", has_plan=False, baseline_available=False)
    assert "No execution plan" in degraded
    assert "No historical baseline" in degraded
