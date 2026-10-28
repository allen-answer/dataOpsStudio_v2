"""Oracle 12c 以前(11g)的分页改写:最外层 FETCH FIRST / OFFSET → ROWNUM 嵌套。

API / worker 的安全限行与无状态翻页(query_limit)按 Oracle 方言渲染出
`FETCH FIRST n ROWS ONLY` / `OFFSET m ROWS`,这是 12c 语法;11g 报 ORA-00933。
渲染发生时还不知道库的版本,所以改写放在执行环节:OracleAdapter 连上库后从
`Connection.version` 得知主版本 < 12,再调本模块(设计稿 §3.2 limit_style=rownum)。

- 只有 FETCH:`SELECT * FROM (<原查询>) WHERE ROWNUM <= n`
- 带 OFFSET:内层 ROWNUM 截到 offset+n 并带出行号列,外层按行号过滤;多出的行号列
  固定排在最后,由调用方在交出列与行之前去掉(`drop_trailing_column`)
- ORDER BY 留在内层子查询里,ROWNUM 在排序之后编号,语义与 FETCH FIRST 一致

只认字面量行数;参数化行数、WITH TIES、PERCENT 或解析不了的 SQL 原样返回(交给数据库报错,
不臆造等价写法)。
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

ROWNUM_COLUMN = "DATAOPS_RN__"
_INNER_ALIAS = "DATAOPS_Q__"


@dataclass(frozen=True)
class LegacyPagingRewrite:
    sql: str
    drop_trailing_column: bool = False


def rewrite_for_pre12c(sql: str) -> LegacyPagingRewrite:
    unchanged = LegacyPagingRewrite(sql)
    try:
        expression = sqlglot.parse_one(sql, read="oracle")
    except ParseError:
        return unchanged
    if not isinstance(expression, exp.Query):
        return unchanged
    limit = expression.args.get("limit")
    offset = expression.args.get("offset")
    if limit is None and offset is None:
        return unchanged

    count: int | None = None
    if limit is not None:
        count = _fetch_count(limit)
        if count is None:
            return unchanged
    skip = 0
    if offset is not None:
        if not isinstance(offset, exp.Offset):
            return unchanged
        literal = _literal_int(offset.expression)
        if literal is None:
            return unchanged
        skip = literal

    inner = expression.copy()
    inner.set("limit", None)
    inner.set("offset", None)
    inner_sql = inner.sql(dialect="oracle")

    if skip == 0:
        if count is None:
            return LegacyPagingRewrite(inner_sql)
        return LegacyPagingRewrite(f"SELECT * FROM ({inner_sql}) WHERE ROWNUM <= {count}")

    upper_bound = f" WHERE ROWNUM <= {skip + count}" if count is not None else ""
    return LegacyPagingRewrite(
        f"SELECT * FROM (SELECT {_INNER_ALIAS}.*, ROWNUM AS {ROWNUM_COLUMN} "
        f"FROM ({inner_sql}) {_INNER_ALIAS}{upper_bound}) WHERE {ROWNUM_COLUMN} > {skip}",
        drop_trailing_column=True,
    )


def server_major_version(version: object) -> int | None:
    """python-oracledb `Connection.version`(如 "11.2.0.4.0")→ 主版本;拿不到返回 None。"""
    if not isinstance(version, str):
        return None
    head = version.split(".", 1)[0]
    return int(head) if head.isdigit() else None


def _fetch_count(limit: exp.Expression) -> int | None:
    if isinstance(limit, exp.Limit):
        return _literal_int(limit.expression)
    if isinstance(limit, exp.Fetch):
        options = limit.args.get("limit_options")
        if options is not None and (options.args.get("percent") or options.args.get("with_ties")):
            return None
        return _literal_int(limit.args.get("count"))
    return None


def _literal_int(node: object) -> int | None:
    if not isinstance(node, exp.Literal) or node.is_string:
        return None
    try:
        value = int(node.this)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


__all__ = ["ROWNUM_COLUMN", "LegacyPagingRewrite", "rewrite_for_pre12c", "server_major_version"]
