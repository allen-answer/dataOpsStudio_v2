"""AI Copilot C4 —— 慢 SQL 根因诊断的纯逻辑(设计稿 §2.7.4,egress L3)。

★ 本模块是 **纯 domain**:不碰 DB / 网络 / gateway,只做出站上下文的组装 /
prompt 构造 / 历史基线汇总。这样截断策略、字面量遮蔽、prompt 形状可离线单测,
route 层只负责取 explain 结果 + 元数据缓存 + 聚合 jobs + 调 gateway。

egress 分层(§2.7.5):
- **SQL 原文** = L3。出站前用 `redact_sql_literals`(#83 血缘 AI 兜底先例)遮蔽
  字符串 / 数字字面量:保留 SQL 结构与表列名,抹掉业务过滤值(那已逼近 L4 样本值)。
- **执行计划**(EXPLAIN)= L1:算子 / 代价 / 行估,不含行值。
- **表统计**(schema 结构 + 索引)= L2:表名 / 列名 / 类型 / 索引定义,无行值。
- **历史基线** = L1:同一 SQL 近 N 次执行的 duration 聚合数字(count/avg/min/max/p95),
  纯统计量,不含任何业务数据。

基线为空(工具刚上线、无运行历史)→ `summarize_baseline` 返回 available=False,
prompt 如实告知"无历史基线",AI 只据 plan + 结构给建议(提前交付的优雅降级)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import sqlglot
from sqlglot import exp

from app.domain.lineage.ai_fallback import redact_sql_literals
from app.domain.schema import Column, Index

# 送 AI 取统计的表数上限:控 prompt token + egress 面(与 C1 同数量级)。
MAX_TABLES = 12
# 每表列数上限:宽表只送前若干列(结构信息,截断标记透传)。
MAX_COLUMNS_PER_TABLE = 60
# 执行计划行数上限:防超长 plan 打爆 prompt。
MAX_PLAN_ROWS = 200

__all__ = [
    "MAX_COLUMNS_PER_TABLE",
    "MAX_PLAN_ROWS",
    "MAX_TABLES",
    "BaselineStats",
    "build_diagnose_prompt",
    "build_plan_payload",
    "build_table_stats",
    "extract_table_refs",
    "mask_sql",
    "summarize_baseline",
]


def mask_sql(sql: str) -> str:
    """遮蔽 SQL 字面量后出站(L3 出站前置,复用 #83 血缘先例)。"""
    return redact_sql_literals(sql)


def extract_table_refs(sql: str, *, dialect: str | None = None) -> list[tuple[str | None, str]]:
    """从 SQL 抽取引用的 (schema, table) —— 用于取这些表的结构统计。

    宽松解析:先按方言 parse,失败回落无方言 parse,再失败返回空(诊断降级为无表统计)。
    schema 取 `table.db`(未限定则 None,由 route 用 datasource 默认库补齐或跳过)。
    去重且保序,便于前端展示"AI 看了哪些表"。
    """
    statements = _safe_parse(sql, dialect)
    refs: list[tuple[str | None, str]] = []
    seen: set[tuple[str | None, str]] = set()
    for statement in statements:
        if statement is None:
            continue
        for table in statement.find_all(exp.Table):
            name = table.name
            if not name:
                continue
            schema = table.db or None
            key = (schema, name)
            if key in seen:
                continue
            seen.add(key)
            refs.append(key)
    return refs


def _safe_parse(sql: str, dialect: str | None) -> list[exp.Expression | None]:
    for read in (dialect, None):
        try:
            return cast("list[exp.Expression | None]", sqlglot.parse(sql, read=read))
        except Exception:
            continue
    return []


def build_table_stats(
    tables: list[tuple[str, str, list[Column], list[Index]]],
    *,
    max_tables: int = MAX_TABLES,
    max_columns_per_table: int = MAX_COLUMNS_PER_TABLE,
) -> tuple[list[dict[str, Any]], list[str], bool]:
    """把 (schema, table, columns, indexes) 压成 L2 结构 JSON。

    返回 (payload, tables_used, truncated):
    - payload:逐表 {schema, table, columns:[{name,type,nullable,primary_key}], indexes:[...]}
    - tables_used:实际入 prompt 的 "schema.table" 名
    - truncated:表数或任一表列数被截断则 True
    ★ 只取结构字段;物理上无从取到行值(L2)。索引给 AI 判"缺索引 / 用错索引"。
    """
    truncated = False
    if len(tables) > max_tables:
        truncated = True
        tables = tables[:max_tables]

    payload: list[dict[str, Any]] = []
    tables_used: list[str] = []
    for schema_name, table_name, columns, indexes in tables:
        cols = columns
        if len(cols) > max_columns_per_table:
            truncated = True
            cols = cols[:max_columns_per_table]
        payload.append(
            {
                "schema": schema_name,
                "table": table_name,
                "columns": [_column_entry(col) for col in cols],
                "indexes": [_index_entry(idx) for idx in indexes],
            }
        )
        tables_used.append(f"{schema_name}.{table_name}" if schema_name else table_name)
    return payload, tables_used, truncated


def _column_entry(col: Column) -> dict[str, Any]:
    return {
        "name": col.name,
        "type": col.type.value,
        "nullable": col.nullable,
        "primary_key": col.primary_key,
    }


def _index_entry(idx: Index) -> dict[str, Any]:
    return {
        "name": idx.name,
        "columns": list(idx.columns),
        "unique": idx.is_unique,
        "primary": idx.is_primary,
    }


def build_plan_payload(
    columns: list[str],
    rows: list[list[Any]],
    *,
    max_rows: int = MAX_PLAN_ROWS,
) -> tuple[dict[str, Any], bool]:
    """把 EXPLAIN 结果集(列名 + 行)压成 L1 plan JSON。

    返回 (payload, truncated)。plan 是算子 / 代价 / 行估,不含任何业务行值(L1)。
    """
    truncated = len(rows) > max_rows
    capped = rows[:max_rows]
    return (
        {
            "columns": columns,
            "rows": [[_json_safe(cell) for cell in row] for row in capped],
        },
        truncated,
    )


def _json_safe(cell: Any) -> Any:
    if cell is None or isinstance(cell, (str, int, float, bool)):
        return cell
    return str(cell)


@dataclass(frozen=True)
class BaselineStats:
    """同一 SQL(sql_hash 相同)近期成功执行的 duration 聚合(L1 统计量)。"""

    runs: int
    avg_seconds: float | None = None
    min_seconds: float | None = None
    max_seconds: float | None = None
    p95_seconds: float | None = None


def summarize_baseline(stats: BaselineStats) -> dict[str, Any]:
    """基线汇总为 prompt 友好 dict。runs==0 → available=False(优雅降级)。"""
    if stats.runs <= 0:
        return {"available": False}
    return {
        "available": True,
        "runs": stats.runs,
        "avg_seconds": _round(stats.avg_seconds),
        "min_seconds": _round(stats.min_seconds),
        "max_seconds": _round(stats.max_seconds),
        "p95_seconds": _round(stats.p95_seconds),
    }


def _round(value: float | None) -> float | None:
    return None if value is None else round(float(value), 3)


def build_diagnose_prompt(
    *,
    dialect: str,
    has_plan: bool,
    baseline_available: bool,
) -> str:
    """C4 诊断指令:据 plan + 结构 + 基线做根因排序 + 建议;不臆造行数据。"""
    plan_line = (
        "An execution plan (EXPLAIN) is provided."
        if has_plan
        else "No execution plan was provided; reason from schema and baseline only."
    )
    baseline_line = (
        "Historical duration baseline for this exact query is provided."
        if baseline_available
        else (
            "No historical baseline is available yet (the query has no recorded run "
            "history); do not fabricate one and say so if timing context is needed."
        )
    )
    return (
        f"You are a {dialect} performance engineer diagnosing a slow SQL query. "
        f"{plan_line} {baseline_line} "
        "Use ONLY the provided execution plan, table structure/indexes, and aggregate "
        "baseline statistics. Do NOT invent row values, cardinalities, or data samples "
        "beyond what is given. "
        "Rank the most likely root causes (e.g. missing/unused index, full scan, stale "
        "statistics, bad join order, non-sargable predicate, large intermediate result), "
        "most-likely first, and give concrete, actionable suggestions (index to add, "
        "rewrite, ANALYZE/statistics refresh). Be concise."
    )
