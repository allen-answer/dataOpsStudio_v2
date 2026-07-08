"""AI Copilot C1 —— Schema-Aware NL→SQL 纯逻辑(设计稿 §2.7.4,egress L2)。

★ 本模块是 **纯 domain**:不碰 DB / 网络 / gateway,只做上下文组装 / prompt 构造 /
响应拆分。这样截断策略与 prompt 形状可离线单测,route 层只负责取缓存 + 调 gateway。

egress 铁律(§2.7.5 L2):上下文只含**结构信息**(表名 / 列名 / 类型 / 可空 / 主键 /
列注释),**绝不含样本数据 / 行值 / 过滤字面量**。构造函数只从 `Column` 取字段,
物理上无从取到行内容。
"""

from __future__ import annotations

import re
from typing import Any

from app.domain.schema import Column

# 截断上限:控制 prompt token + egress 面。宁可少送也绝不送样本数据。
MAX_TABLES = 12
MAX_COLUMNS_PER_TABLE = 60


def build_schema_context(
    tables: list[tuple[str, str, list[Column]]],
    *,
    max_tables: int = MAX_TABLES,
    max_columns_per_table: int = MAX_COLUMNS_PER_TABLE,
) -> tuple[list[dict[str, Any]], list[str], bool]:
    """把 (schema, table, columns) 列表压成 L2 结构 JSON。

    返回 (payload, tables_used, truncated):
    - payload:逐表 {schema, table, columns:[{name,type,nullable,primary_key,comment}]}
    - tables_used:实际入 prompt 的 "schema.table" 名(前端可见 AI 看了哪些表)
    - truncated:表数或任一表列数被截断则 True
    """
    truncated = False
    if len(tables) > max_tables:
        truncated = True
        tables = tables[:max_tables]

    payload: list[dict[str, Any]] = []
    tables_used: list[str] = []
    for schema_name, table_name, columns in tables:
        cols = columns
        if len(cols) > max_columns_per_table:
            truncated = True
            cols = cols[:max_columns_per_table]
        payload.append(
            {
                "schema": schema_name,
                "table": table_name,
                "columns": [_column_entry(col) for col in cols],
            }
        )
        tables_used.append(f"{schema_name}.{table_name}" if schema_name else table_name)
    return payload, tables_used, truncated


def _column_entry(col: Column) -> dict[str, Any]:
    # ★ 只取结构字段;绝不含行值。comment 是列注释(L2 结构元数据)。
    return {
        "name": col.name,
        "type": col.type.value,
        "nullable": col.nullable,
        "primary_key": col.primary_key,
        "comment": col.comment,
    }


def build_nl2sql_prompt(natural_language: str, *, dialect: str) -> str:
    """C1 生成指令。约束:只读、只用给定 schema、不臆造表列、不含样本数据。

    NL 原文进 prompt(送 provider),但**不进日志**(R5;route 不 log NL)。
    """
    return (
        "You are a SQL assistant. Generate a single read-only "
        f"{dialect} SQL query that answers the user's request, using ONLY the "
        "tables and columns provided in the schema context. "
        "Do not invent tables or columns. Do not include sample data or "
        "hard-coded literal values beyond what the request requires. "
        "Return the SQL inside a ```sql fenced block, followed by a one or two "
        "sentence explanation.\n\n"
        f"User request: {natural_language}"
    )


_SQL_FENCE = re.compile(r"```(?:sql)?\s*(?P<sql>.+?)```", re.IGNORECASE | re.DOTALL)


def split_sql_and_explanation(content: str) -> tuple[str, str | None]:
    """宽松拆分 provider 响应为 (sql, explanation)。

    - 有 ```sql 围栏:sql = 围栏内容,explanation = 围栏外剩余文本(去空则 None)。
    - 无围栏:整段作 sql,explanation = None(MockProvider 回 "ok" 走这支)。
    """
    text = content.strip()
    match = _SQL_FENCE.search(text)
    if match is None:
        return text, None
    sql = match.group("sql").strip()
    explanation = (text[: match.start()] + " " + text[match.end() :]).strip()
    return sql, explanation or None
