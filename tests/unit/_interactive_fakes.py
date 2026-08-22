"""会话缝单测用的 fake 驱动(不连任何真库)。

形态刻意贴住**本机实测的真驱动**,否则单测只能证明"缝跟 fake 一致":

- fake dmPython 的异常携带 `DmError` 风格对象(码在 `.code`),建连失败还原
  `SystemError` + `__context__` 的真实形态;
- fake PyMySQL 的异常是 `OperationalError(errno, message)`;
- 两边的 cursor 都记录 `(sql, params)` 时序,供保险带同步顺序等断言使用。
"""

from __future__ import annotations

import time
from typing import Any

from app.domain.datasource import DatasourceConnInfo, DbType
from app.domain.secret import HashedRef, RotationReport, SecretKind, SecretRef


class SecretStoreStub:
    """只实现会话缝真正用到的 `reveal_secret`,并记录被 reveal 的 ref。"""

    def __init__(self, password: str = "pwd") -> None:
        self._password = password
        self.revealed_refs: list[str] = []

    def store_secret(self, kind: SecretKind, plaintext: str) -> SecretRef:
        raise NotImplementedError

    def reveal_secret(self, ref: SecretRef) -> str:
        self.revealed_refs.append(ref.ref)
        return self._password

    def rotate_master_key(self, new_key: bytes) -> RotationReport:
        raise NotImplementedError

    def delete_secret(self, ref: SecretRef) -> None:
        raise NotImplementedError

    def hash_password(self, plaintext: str) -> HashedRef:
        raise NotImplementedError

    def verify_password(self, plaintext: str, hashed: HashedRef) -> bool:
        raise NotImplementedError


def conn_info(db_type: DbType = DbType.DM, **overrides: Any) -> DatasourceConnInfo:
    base = DatasourceConnInfo(
        host="db.internal",
        port=5236 if db_type is DbType.DM else 3306,
        username="app_user",
        database="APPDB",
        db_type=db_type,
        password_ref=SecretRef(ref="secret-1", kind=SecretKind.DATASOURCE_PASSWORD),
    )
    return base.model_copy(update=overrides) if overrides else base


# ── DM ──────────────────────────────────────────────────────────────────────


class DmTypeObject:
    """模拟 dmPython.NUMBER / STRING 这类 DB-API type-object 常量。"""

    def __init__(self, name: str) -> None:
        self.__name__ = name

    def __repr__(self) -> str:  # pragma: no cover - 仅调试可读性
        return f"<DmType {self.__name__}>"


class DmError:
    """dmPython.DmError 的形状:码在 `.code`,消息在 `.message`(可能是乱码)。"""

    def __init__(self, code: int, message: str = "", context: str = "") -> None:
        self.code = code
        self.message = message
        self.context = context
        self.offset = 0


class DmDatabaseError(Exception):
    """dmPython.DatabaseError:`args[0]` 是 DmError 对象,**不是 int**。"""


def dm_error(code: int, message: str = "boom") -> DmDatabaseError:
    return DmDatabaseError(DmError(code, message))


def dm_connect_error(code: int, message: str = "boom") -> SystemError:
    """还原建连失败的真实形态:SystemError,真异常挂 `__context__`。"""
    outer = SystemError("<class 'dmPython.Connection'> returned a result with an exception set")
    outer.__context__ = dm_error(code, message)
    return outer


class FakeDM:
    NUMBER = DmTypeObject("NUMBER")
    STRING = DmTypeObject("STRING")
    DATETIME = DmTypeObject("DATETIME")
    BINARY = DmTypeObject("BINARY")

    def __init__(
        self,
        *,
        connect_error: BaseException | None = None,
        connect_delay_seconds: float = 0.0,
    ) -> None:
        self.connections: list[FakeConnection] = []
        self.connect_kwargs: list[dict[str, Any]] = []
        self.connect_error = connect_error
        # 让"建连烧满登录预算"这件事在单测里可确定地复现。
        self.connect_delay_seconds = connect_delay_seconds

    def connect(self, **kwargs: Any) -> FakeConnection:
        self.connect_kwargs.append(dict(kwargs))
        if self.connect_delay_seconds:
            time.sleep(self.connect_delay_seconds)
        if self.connect_error is not None:
            raise self.connect_error
        conn = FakeConnection(kwargs, marker_sql="select sessid()", marker_value=142)
        self.connections.append(conn)
        return conn


# ── MySQL ───────────────────────────────────────────────────────────────────


class MySQLOperationalError(Exception):
    """pymysql.err.OperationalError 的形状:`args = (errno, message)`。"""


def mysql_error(code: int, message: str = "boom") -> MySQLOperationalError:
    return MySQLOperationalError(code, message)


class _FieldType:
    LONG = 3
    VAR_STRING = 253


class _Constants:
    FIELD_TYPE = _FieldType


class SSCursorMarker:
    """pymysql.cursors.SSCursor 的替身,只用于断言"确实要了流式 cursor"。"""


class _Cursors:
    SSCursor = SSCursorMarker


class FakePyMySQL:
    constants = _Constants
    cursors = _Cursors

    def __init__(self, *, connect_error: BaseException | None = None) -> None:
        self.connections: list[FakeConnection] = []
        self.connect_kwargs: list[dict[str, Any]] = []
        self.connect_error = connect_error

    def connect(self, **kwargs: Any) -> FakeConnection:
        self.connect_kwargs.append(dict(kwargs))
        if self.connect_error is not None:
            raise self.connect_error
        conn = FakeConnection(kwargs, marker_sql="select connection_id()", marker_value=99)
        self.connections.append(conn)
        return conn


# ── 共用 connection / cursor ────────────────────────────────────────────────


class FakeConnection:
    """记录每条 SQL 与 cursor 类,并允许按 SQL 片段注入错误。"""

    def __init__(self, kwargs: dict[str, Any], *, marker_sql: str, marker_value: int) -> None:
        self.kwargs = kwargs
        self.closed = False
        self.cursors: list[FakeCursor] = []
        self.cursor_classes: list[Any] = []
        self.marker_sql = marker_sql
        self.marker_value = marker_value
        # SQL 片段(小写) → 抛出的异常;命中即在 execute 时抛。
        self.errors: dict[str, BaseException] = {}
        self.rows: list[tuple[Any, ...]] = [(1,), (2,), (3,)]
        self.description: tuple[tuple[Any, ...], ...] = (("N", 3, None, None, None, 0, True),)

    @property
    def statements(self) -> list[str]:
        """本连接上按时序发出的所有 SQL(小写、压空白)。"""
        return [sql for cursor in self.cursors for sql in cursor.executed]

    def cursor(self, cursorclass: Any | None = None) -> FakeCursor:
        self.cursor_classes.append(cursorclass)
        cursor = FakeCursor(self)
        self.cursors.append(cursor)
        return cursor

    def close(self) -> None:
        self.closed = True


class FakeCursor:
    def __init__(self, conn: FakeConnection) -> None:
        self._conn = conn
        self.closed = False
        self.executed: list[str] = []
        self.params: list[Any] = []
        self.fetchmany_sizes: list[int] = []
        self.description: tuple[tuple[Any, ...], ...] = ()
        self._rows: list[tuple[Any, ...]] = []
        self._offset = 0
        # fetch 第 N 批时抛的异常(N 从 1 数)。
        self.fetch_errors: dict[int, BaseException] = {}
        self._fetch_calls = 0

    def execute(self, sql: str, params: Any = None) -> None:
        normalized = " ".join(sql.lower().split())
        self.executed.append(normalized)
        self.params.append(params)
        for fragment, exc in self._conn.errors.items():
            if fragment in normalized:
                raise exc
        if self._conn.marker_sql in normalized:
            self.description = (("marker", 3, None, None, None, 0, False),)
            self._rows = [(self._conn.marker_value,)]
        elif normalized.startswith("select 1"):
            self.description = (("ok", 3, None, None, None, 0, False),)
            self._rows = [(1,)]
        else:
            self.description = self._conn.description
            self._rows = list(self._conn.rows)
        self._offset = 0

    def fetchone(self) -> tuple[Any, ...] | None:
        batch = self._slice(1)
        return batch[0] if batch else None

    def fetchmany(self, size: int) -> list[tuple[Any, ...]]:
        self.fetchmany_sizes.append(size)
        self._fetch_calls += 1
        exc = self.fetch_errors.get(self._fetch_calls)
        if exc is not None:
            raise exc
        return self._slice(size)

    def _slice(self, size: int) -> list[tuple[Any, ...]]:
        batch = self._rows[self._offset : self._offset + size]
        self._offset += len(batch)
        return batch

    def close(self) -> None:
        self.closed = True


__all__ = [
    "DmDatabaseError",
    "DmError",
    "DmTypeObject",
    "FakeConnection",
    "FakeCursor",
    "FakeDM",
    "FakePyMySQL",
    "MySQLOperationalError",
    "SSCursorMarker",
    "SecretStoreStub",
    "conn_info",
    "dm_connect_error",
    "dm_error",
    "mysql_error",
]
