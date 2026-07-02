"""DM(达梦)adapter 单测。

DM 无官方可信的 GitHub Actions service container(社区镜像大且 license 受限),
故 DM adapter 用 fake dmPython 模块做单测 + 契约测试覆盖,不依赖真 DM 实例。
为何 CI 没有 DM 集成 job 见 PR 描述。
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from app.dbclients.dm_adapter import DMAdapter, InvalidDatasourceError
from app.dbclients.dm_types import (
    data_type_string_to_column_type,
    type_code_to_column_type,
)
from app.dbclients.protocol import AdapterConnectionError
from app.domain.datasource import DatasourceConnInfo, DbType
from app.domain.schema import Column, ColumnType, Row
from app.domain.secret import HashedRef, RotationReport, SecretKind, SecretRef
from app.infrastructure.secretstore.protocol import SecretStore


def test_requires_dm_db_type() -> None:
    conn_info = _conn_info().model_copy(update={"db_type": DbType.MYSQL})
    with pytest.raises(InvalidDatasourceError):
        DMAdapter(conn_info, cast(SecretStore, _SecretStore("pwd")), dm_module=_FakeDM())


def test_capabilities_reflect_dm_reality() -> None:
    adapter = DMAdapter(_conn_info(), cast(SecretStore, _SecretStore("pwd")), dm_module=_FakeDM())
    caps = adapter.capabilities
    # DM 无 driver-level cancel(V1_AS_IS §2.8)
    assert caps.server_side_cancel is False
    assert caps.execute_select is True
    assert caps.list_columns is True


def test_kill_query_is_soft_cancel_false() -> None:
    adapter = DMAdapter(_conn_info(), cast(SecretStore, _SecretStore("pwd")), dm_module=_FakeDM())
    assert adapter.kill_query("any-conn-id") is False


def test_execute_select_streams_with_plain_cursor_and_closes_resources() -> None:
    fake_dm = _FakeDM()
    secret_store = _SecretStore("pwd")
    adapter = DMAdapter(
        _conn_info(),
        cast(SecretStore, secret_store),
        dm_module=fake_dm,
        fetch_chunk_size=1,
    )

    rows = list(adapter.execute_select("SELECT 1", {}))

    assert rows == [Row(values=[1]), Row(values=[2])]
    assert secret_store.revealed_refs == ["secret-1"]
    # DM 用普通 cursor(非 SSCursor),不应请求任何 cursorclass
    assert fake_dm.connections[0].cursor_called_with_class is False
    assert all(cursor.closed for cursor in fake_dm.connections[0].cursors)
    assert fake_dm.connections[0].closed is True


def test_execute_select_emits_columns_mapped_to_unified_enum() -> None:
    fake_dm = _FakeDM()
    captured: list[Column] = []
    adapter = DMAdapter(
        _conn_info(),
        cast(SecretStore, _SecretStore("pwd")),
        dm_module=fake_dm,
        fetch_chunk_size=1,
        column_sink=captured.extend,
    )

    list(adapter.execute_select("SELECT N FROM T", {}))

    # description[1] == dmPython.NUMBER (type-object) → DECIMAL;driver_type 使用干净名
    assert captured == [Column(name="N", type=ColumnType.DECIMAL, driver_type="NUMBER")]
    assert all(not hasattr(column, "cursor") for column in captured)


def test_list_columns_maps_data_type_and_keeps_driver_type() -> None:
    fake_dm = _FakeDM()
    adapter = DMAdapter(_conn_info(), cast(SecretStore, _SecretStore("pwd")), dm_module=fake_dm)

    columns = adapter.list_columns("APP", "USERS")
    column_sql = fake_dm.connections[0].cursors[0].executed_sql

    assert columns == [
        Column(
            name="ID",
            type=ColumnType.DECIMAL,
            driver_type="NUMBER",
            nullable=False,
            primary_key=True,
            comment="primary key",
        ),
        Column(
            name="NAME",
            type=ColumnType.STRING,
            driver_type="VARCHAR2",
            nullable=True,
            primary_key=False,
        ),
    ]
    assert 'cc.COMMENTS AS "COMMENT"' in column_sql
    assert "as comment" not in column_sql.lower()


def test_test_connection_records_dm_server_version() -> None:
    fake_dm = _FakeDM()
    adapter = DMAdapter(_conn_info(), cast(SecretStore, _SecretStore("pwd")), dm_module=fake_dm)

    assert adapter.test_connection() is True
    assert adapter.last_server_version == "DM DM Database Server x64 V8"


def test_test_connection_sanitizes_error_on_failure() -> None:
    fake_dm = _FailingDM(
        RuntimeError("login failed password=top-secret dm://user:top-secret@host/app")
    )
    adapter = DMAdapter(
        _conn_info(),
        cast(SecretStore, _SecretStore("top-secret")),
        dm_module=fake_dm,
    )

    assert adapter.test_connection() is False
    assert adapter.last_connection_error is not None
    assert "top-secret" not in adapter.last_connection_error
    assert "dm://user" not in adapter.last_connection_error
    assert "<redacted-url>" in adapter.last_connection_error
    assert "***REDACTED***" in adapter.last_connection_error


def test_stream_select_preserves_connection_error_cause() -> None:
    # from exc(非 from None):AdapterConnectionError 必须链住原始驱动异常,
    # 便于 worker logger.exception 排障(R5 脱敏 processor 兜底敏感值)。
    original = RuntimeError("driver connect boom")
    adapter = DMAdapter(
        _conn_info(),
        cast(SecretStore, _SecretStore("pwd")),
        dm_module=_FailingDM(original),
    )

    with pytest.raises(AdapterConnectionError) as excinfo:
        list(adapter.execute_select("SELECT 1", {}))

    assert excinfo.value.__cause__ is original


def test_soft_cancel_raises_on_safe_point() -> None:
    fake_dm = _FakeDM()
    cancelled = {"flag": False}

    adapter = DMAdapter(
        _conn_info(),
        cast(SecretStore, _SecretStore("pwd")),
        dm_module=fake_dm,
        fetch_chunk_size=1,
        cancel_check=lambda: cancelled["flag"],
    )

    iterator = adapter.execute_select("SELECT 1", {})
    assert next(iterator) == Row(values=[1])
    cancelled["flag"] = True
    from app.dbclients.dm_adapter import QueryCancelledError

    with pytest.raises(QueryCancelledError):
        next(iterator)
    # 软取消后连接仍被 finally 释放
    assert fake_dm.connections[0].closed is True


def test_data_type_string_mapping_units() -> None:
    assert data_type_string_to_column_type("NUMBER") is ColumnType.DECIMAL
    assert data_type_string_to_column_type("VARCHAR2(64)") is ColumnType.STRING
    assert data_type_string_to_column_type("TIMESTAMP(6) WITH TIME ZONE") is ColumnType.DATETIME
    assert data_type_string_to_column_type("DATE") is ColumnType.DATE
    assert data_type_string_to_column_type("BLOB") is ColumnType.BYTES
    assert data_type_string_to_column_type("BIGINT") is ColumnType.INTEGER
    assert data_type_string_to_column_type("WEIRDTYPE") is ColumnType.UNKNOWN
    assert data_type_string_to_column_type(None) is ColumnType.UNKNOWN


def test_type_code_object_mapping_units() -> None:
    dm = _FakeDM()
    assert type_code_to_column_type(dm, _FakeDM.NUMBER) is ColumnType.DECIMAL
    assert type_code_to_column_type(dm, _FakeDM.STRING) is ColumnType.STRING
    assert type_code_to_column_type(dm, object()) is ColumnType.UNKNOWN
    assert type_code_to_column_type(dm, None) is ColumnType.UNKNOWN


# ──────────────────────────── fakes ────────────────────────────


def _conn_info() -> DatasourceConnInfo:
    return DatasourceConnInfo(
        host="127.0.0.1",
        port=5236,
        username="DATAOPS",
        database="APP",
        password_ref=SecretRef(ref="secret-1", kind=SecretKind.DATASOURCE_PASSWORD),
        db_type=DbType.DM,
    )


class _SecretStore:
    def __init__(self, password: str) -> None:
        self._password = password
        self.revealed_refs: list[str] = []

    def reveal_secret(self, ref: SecretRef) -> str:
        self.revealed_refs.append(ref.ref)
        return self._password

    def store_secret(self, plaintext: str, kind: SecretKind) -> SecretRef:
        raise NotImplementedError

    def rotate_secret(self, ref: SecretRef, new_plaintext: str) -> SecretRef:
        raise NotImplementedError

    def delete_secret(self, ref: SecretRef) -> None:
        raise NotImplementedError

    def hash_password(self, plaintext: str) -> HashedRef:
        raise NotImplementedError

    def verify_password(self, plaintext: str, ref: HashedRef) -> bool:
        raise NotImplementedError

    def rotate_master_key(self, new_key: bytes) -> RotationReport:
        raise NotImplementedError


class _DMTypeObject:
    """dmPython DB-API type-object 替身(== 自身;不可哈希无所谓)。"""

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return f"<DMTYPE {self.name}>"


class _FakeDM:
    # DB-API type-object 常量(模拟 dmPython.NUMBER / dmPython.STRING 等)
    NUMBER = _DMTypeObject("NUMBER")
    STRING = _DMTypeObject("STRING")
    DATETIME = _DMTypeObject("DATETIME")
    BINARY = _DMTypeObject("BINARY")

    def __init__(self) -> None:
        self.connections: list[_FakeConnection] = []

    def connect(self, **kwargs: Any) -> _FakeConnection:
        conn = _FakeConnection(kwargs)
        self.connections.append(conn)
        return conn


class _FailingDM(_FakeDM):
    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self._exc = exc

    def connect(self, **kwargs: Any) -> _FakeConnection:
        raise self._exc


class _FakeConnection:
    def __init__(self, kwargs: dict[str, Any]) -> None:
        self.kwargs = kwargs
        self.closed = False
        self.callTimeout: int | None = None
        self.cursors: list[_FakeCursor] = []
        self.cursor_called_with_class = False

    def cursor(self, cursorclass: object | None = None) -> _FakeCursor:
        if cursorclass is not None:
            self.cursor_called_with_class = True
        cursor = _FakeCursor()
        self.cursors.append(cursor)
        return cursor

    def close(self) -> None:
        self.closed = True


class _FakeCursor:
    def __init__(self) -> None:
        self.closed = False
        self.description: tuple[tuple[Any, ...], ...] = (("ok",),)
        self._rows: list[tuple[Any, ...]] = []
        self._offset = 0
        self.executed_sql = ""

    def execute(self, sql: str, params: object = None) -> None:
        self.executed_sql = sql
        normalized = " ".join(sql.lower().split())
        if "from dual" in normalized and "select 1 as ok" in normalized:
            self.description = (("ok",),)
            self._rows = [(1,)]
        elif "v$version" in normalized:
            self.description = (("banner",),)
            self._rows = [("DM Database Server x64 V8",)]
        elif "all_tab_columns" in normalized:
            self.description = (("name",), ("data_type",), ("nullable",), ("COMMENT",))
            self._rows = [
                ("ID", "NUMBER", "N", "primary key"),
                ("NAME", "VARCHAR2", "Y", None),
            ]
        elif "all_constraints" in normalized and "all_cons_columns" in normalized:
            self.description = (("name",),)
            self._rows = [("ID",)]
        elif "all_constraints" in normalized:
            self.description = (("name",),)
            self._rows = []
        elif "select n from t" in normalized:
            self.description = (("N", _FakeDM.NUMBER),)
            self._rows = [(1,), (2,)]
        else:
            self.description = (("col",),)
            self._rows = [(1,), (2,)]
        self._offset = 0

    def fetchone(self) -> tuple[Any, ...] | None:
        rows = self.fetchmany(1)
        return rows[0] if rows else None

    def fetchmany(self, size: int) -> list[tuple[Any, ...]]:
        batch = self._rows[self._offset : self._offset + size]
        self._offset += len(batch)
        return batch

    def fetchall(self) -> list[tuple[Any, ...]]:
        rows = self._rows[self._offset :]
        self._offset = len(self._rows)
        return rows

    def close(self) -> None:
        self.closed = True
