from __future__ import annotations

from sqlglot import Dialect
from sqlglot.dialects.oracle import Oracle


class DM(Oracle):
    """DM SQL is Oracle-compatible for the DML parsed in W1-PR1."""


def register_lineage_dialects() -> None:
    Dialect.classes.setdefault("dm", DM)


register_lineage_dialects()


def normalize_lineage_dialect(dialect: str) -> str:
    """把请求方言名归一到 sqlglot 原生方言名(血缘全域共用)。

    住在 dialects.py 而非 parser.py:DDL 文本数据源(``ddl_schema``)与 DML 解析
    (``parser``)都要按同一套规则归一,放这里避免两处漂移。
    """
    normalized = dialect.lower()
    if normalized in {"dameng"}:
        return "dm"
    if normalized == "postgresql":
        return "postgres"  # sqlglot 原生方言名
    # L-1:放开 sqlglot 原生支持的方言(tsql 随自动识别一并放开);db2 保持不加
    # (GA 未 Certified,差距矩阵 L-1)。
    if normalized not in {"mysql", "oracle", "dm", "postgres", "tsql"}:
        raise ValueError("lineage dialect must be mysql, oracle, dm, postgres, or tsql")
    return normalized


__all__ = ["DM", "normalize_lineage_dialect", "register_lineage_dialects"]
