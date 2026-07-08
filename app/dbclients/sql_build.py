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


def sql_literal(value: object) -> str:
    """把主键值渲染成可粘贴到 DBA 控制台的 SQL 字面量(非执行路径,仅生成文本)。

    UX-2 差异行定位 SQL 用:结果只交给用户复制到自己的库里跑,不由平台执行。
    - None -> NULL
    - bool -> 1 / 0(bool 是 int 子类,须先于 int 判断;跨方言 TRUE/FALSE 不通用)
    - int / float(非 NaN/Inf) -> 原样数字
    - 其余(含 spool 里被 default=str 转成字符串的日期等) -> 单引号包裹并转义 '
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):  # NaN / Inf 无字面量
            return "NULL"
        return repr(value)
    text = str(value).replace("'", "''")
    return f"'{text}'"
