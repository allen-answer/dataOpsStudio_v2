"""单条写语句(DML / DDL)执行:一条语句一个事务,成功提交,出错或取消回滚。

各 adapter 的 `execute_write` 负责建连与会话准备(超时 / schema / 关 autocommit),
然后把连接交给 `run_write_statement`。这里不做语句校验 —— 调用方必须先过
`sql_guard.classify_executable_sql`,并按数据源 operation_policy.allow_dml 放行。

DDL 在 Oracle / DM / MySQL / OceanBase 上会隐式提交,rollback 撤不回;
PostgreSQL / DB2 的 DDL 在事务内,出错同样回滚。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WriteResult:
    # 驱动报告的影响行数;DDL 或驱动不给(rowcount 为 -1 / None)时为 None。
    affected_rows: int | None


def run_write_statement(
    conn: Any,
    sql: str,
    params: dict[str, Any] | None,
    *,
    check_cancel: Callable[[], None],
    execute: Callable[[Any, str, dict[str, Any] | None], None] | None = None,
) -> WriteResult:
    """在已建好的连接上执行一条写语句并提交。

    execute:自定义执行(PostgreSQL 的取消看门狗、Oracle 的驱动错误包装);
    缺省为 `cursor.execute`。无参数时不传 params,避免 pyformat 驱动把 SQL 里的
    `%` 当占位符。
    """

    cursor = None
    bound = params or None
    try:
        cursor = conn.cursor()
        if execute is not None:
            execute(cursor, sql, bound)
        elif bound is None:
            cursor.execute(sql)
        else:
            cursor.execute(sql, bound)
        rowcount = getattr(cursor, "rowcount", None)
        # 执行完、提交前最后一次检查取消:取消即回滚,不提交。
        check_cancel()
        conn.commit()
    except BaseException:
        safe_rollback(conn)
        raise
    finally:
        _safe_close(cursor)
    return WriteResult(affected_rows=_affected_rows(rowcount))


def safe_rollback(conn: object | None) -> None:
    rollback = getattr(conn, "rollback", None)
    if rollback is None:
        return
    try:
        rollback()
    except Exception:
        return


def disable_autocommit(conn: Any, attribute: str = "autocommit") -> None:
    """关掉连接 autocommit,让 commit / rollback 真正生效;驱动不支持时保持原样。"""

    try:
        setattr(conn, attribute, False)
    except Exception:
        return


def _affected_rows(rowcount: object) -> int | None:
    if isinstance(rowcount, bool) or not isinstance(rowcount, int) or rowcount < 0:
        return None
    return rowcount


def _safe_close(obj: object | None) -> None:
    close = getattr(obj, "close", None)
    if close is None:
        return
    try:
        close()
    except Exception:
        return
