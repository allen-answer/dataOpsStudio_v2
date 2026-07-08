"""UX-2 C-3:差异行定位 SQL 生成(SELECT ... WHERE pk IN (...))。

纯函数,不接库、不执行:结果是给用户复制到自己 DBA 控制台跑的文本逃生口。
- 表达式构造复用 sql_build 的方言引用(MySQL 反引号 / 其余双引号大写)。
- 值字面量复用 sql_build.sql_literal(None->NULL、数字原样、其余转义单引号)。
- 单主键 -> `col IN (v1, v2)`;复合主键 -> `(c1, c2) IN ((a1,a2), ...)`。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.dbclients.sql_build import quote_identifier, sql_literal
from app.domain.datasource import DbType


def compare_side_expression(db_type: DbType, ref: Mapping[str, object]) -> str | None:
    """把 CompareDataRef(dict 形态)构造成 FROM 后的表达式;文件源 / 缺表返回 None。"""
    kind = str(ref.get("kind") or "table")
    if kind == "file":
        return None
    if kind == "sql":
        sql = str(ref.get("sql") or "").strip()
        return f"({sql}) DATAOPS_DIFF" if sql else None
    schema = ref.get("schema_name")
    table = ref.get("table_name")
    if not table:
        return None
    identifier = f"{schema}.{table}" if schema else str(table)
    try:
        return quote_identifier(db_type, identifier)
    except ValueError:
        return None


def build_diff_row_select(
    db_type: DbType,
    *,
    ref: Mapping[str, object],
    columns: Sequence[str],
    pk_rows: Sequence[Sequence[object]],
) -> str | None:
    """生成单侧定位 SQL;无法生成(文件源 / 无表达式 / 无主键 / 空桶)返回 None。"""
    expr = compare_side_expression(db_type, ref)
    if expr is None or not columns or not pk_rows:
        return None
    try:
        quoted_cols = [quote_identifier(db_type, str(col)) for col in columns]
    except ValueError:
        return None
    if len(quoted_cols) == 1:
        values = ", ".join(sql_literal(row[0]) for row in pk_rows)
        where = f"{quoted_cols[0]} IN ({values})"
    else:
        tuples = ", ".join(
            "(" + ", ".join(sql_literal(value) for value in row) + ")" for row in pk_rows
        )
        where = f"({', '.join(quoted_cols)}) IN ({tuples})"
    return f"SELECT * FROM {expr} WHERE {where}"


__all__ = ["build_diff_row_select", "compare_side_expression"]
