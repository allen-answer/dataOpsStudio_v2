"""按 DbType 的方言特征表(纯领域,不 import 驱动)。

此前各模块各自写 `db_type is DbType.MYSQL` / 本地 `_DIALECTS` 字典来决定标识符引用、
limit 子句和 sqlglot 方言,新增一种数据库要改十来处,漏一处就静默落进 else 分支。
这里把这些按类型的差异收成一张表,调用方只读 `traits(db_type).xxx`;新增类型只需在
`DIALECT_TRAITS` 加一行(设计稿 DB-oracle-oceanbase-adaptation.md §3.2)。

本表复刻收敛前的现有行为,逐项不变,包括两处看似不一致的现状:
- PostgreSQL 标识符也被大写引用(与 DM/Oracle 同一分支);
- DB2 没有 sqlglot 方言:行数限制按 Oracle 渲染(`row_limit_dialect`),
  而比对 SQL 规范化 / AI 上游定位用通用解析器、SQL 格式化直接拒绝(`sqlglot_dialect=None`)。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from app.domain.datasource import DbType


class IdentifierStyle(StrEnum):
    BACKTICK = "backtick"  # MySQL 族:`name`,大小写原样
    DOUBLE_QUOTE_UPPER = "dq_upper"  # 其余:"NAME"(Oracle 族目录大写存储;PG/DB2 沿用现状)


@dataclass(frozen=True)
class DialectTraits:
    identifier_style: IdentifierStyle
    limit_style: Literal["limit", "fetch_first"]
    # 比对 SQL 规范化、AI 上游定位、SQL 格式化 / 展开 * 共用;None = 无对应 sqlglot 方言。
    sqlglot_dialect: str | None
    # 安全行数限制(query_limit)渲染用;须总有值,否则该类型无法执行带限行的查询。
    row_limit_dialect: str


DIALECT_TRAITS: dict[DbType, DialectTraits] = {
    DbType.MYSQL: DialectTraits(
        identifier_style=IdentifierStyle.BACKTICK,
        limit_style="limit",
        sqlglot_dialect="mysql",
        row_limit_dialect="mysql",
    ),
    DbType.ORACLE: DialectTraits(
        identifier_style=IdentifierStyle.DOUBLE_QUOTE_UPPER,
        limit_style="fetch_first",
        sqlglot_dialect="oracle",
        row_limit_dialect="oracle",
    ),
    DbType.DM: DialectTraits(
        identifier_style=IdentifierStyle.DOUBLE_QUOTE_UPPER,
        limit_style="fetch_first",
        sqlglot_dialect="oracle",  # DM 对 SELECT 兼容 Oracle
        row_limit_dialect="oracle",
    ),
    DbType.DB2: DialectTraits(
        identifier_style=IdentifierStyle.DOUBLE_QUOTE_UPPER,
        limit_style="fetch_first",
        sqlglot_dialect=None,
        # DB2 无 sqlglot 方言;Oracle 渲染的 FETCH FIRST n ROWS ONLY 在 DB2 上合法。
        row_limit_dialect="oracle",
    ),
    DbType.POSTGRESQL: DialectTraits(
        identifier_style=IdentifierStyle.DOUBLE_QUOTE_UPPER,
        limit_style="fetch_first",
        sqlglot_dialect="postgres",
        row_limit_dialect="postgres",
    ),
    # SQL 是 Oracle 方言(OB 4.x 实测支持 FETCH FIRST / OFFSET..FETCH,不支持 LIMIT)。
    DbType.OCEANBASE_ORACLE: DialectTraits(
        identifier_style=IdentifierStyle.DOUBLE_QUOTE_UPPER,
        limit_style="fetch_first",
        sqlglot_dialect="oracle",
        row_limit_dialect="oracle",
    ),
}


def traits(db_type: DbType) -> DialectTraits:
    return DIALECT_TRAITS[db_type]


def traits_for_name(name: str) -> DialectTraits | None:
    """按 db_type 字符串取特征(大小写不敏感);不认识的名字返回 None,由调用方决定回落。"""
    try:
        return DIALECT_TRAITS.get(DbType(name.lower()))
    except ValueError:
        return None


__all__ = [
    "DIALECT_TRAITS",
    "DialectTraits",
    "IdentifierStyle",
    "traits",
    "traits_for_name",
]
