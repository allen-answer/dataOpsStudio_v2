"""跨方言 SQL 片段构造(标识符引用 / limit 子句)。

从 app/worker.py 原样抽出(compare 内核黄金一致性真机验证过的行为,勿改语义):
- MySQL 反引号;其余方言(DM/Oracle/PG/DB2)双引号 + 大写(DM/Oracle 目录大写存储)
- limit:MySQL `LIMIT n`;其余 `FETCH FIRST n ROWS ONLY`(SQL:2008,DM/Oracle 12c+/PG/DB2 通)

供 worker(compare 规范化 SQL)与 API(compare 数据预览)两侧复用。
"""

from __future__ import annotations

import re

from app.domain.datasource import DbType

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.$-]+$")


def quote_identifier(db_type: DbType, identifier: str) -> str:
    if not identifier or _IDENTIFIER_RE.fullmatch(identifier) is None:
        raise ValueError("Invalid database identifier")
    if db_type is DbType.MYSQL:
        return ".".join(f"`{part.replace('`', '``')}`" for part in identifier.split("."))
    return ".".join(
        f'"{part.replace(chr(34), chr(34) + chr(34)).upper()}"' for part in identifier.split(".")
    )


def quote_alias(db_type: DbType, alias: str) -> str:
    if not alias or _IDENTIFIER_RE.fullmatch(alias) is None:
        raise ValueError("Invalid database identifier")
    if db_type is DbType.MYSQL:
        return f"`{alias.replace('`', '``')}`"
    return f'"{alias.replace(chr(34), chr(34) + chr(34))}"'


def limit_clause(db_type: DbType, limit: int | None) -> str:
    if limit is None:
        return ""
    if limit <= 0:
        raise ValueError("limit must be positive")
    if db_type is DbType.MYSQL:
        return f" LIMIT {limit}"
    return f" FETCH FIRST {limit} ROWS ONLY"
