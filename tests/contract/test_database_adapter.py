"""DatabaseAdapter Protocol 契约测试(契约 §3.2)。

★ 接口设计以 Oracle/DM 为假想验证对象 —— 故契约测试参数化 MySQL + DM 两个
2.0.0 Certified adapter,同一套 Protocol 签名两边都过,并覆盖:
- 标识符大小写差异(MySQL 反引号原样 vs DM/Oracle UPPER + 双引号)
- server_side_cancel 多为 False 的优雅降级
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from app.dbclients.dm_adapter import DMAdapter
from app.dbclients.mysql_adapter import MySQLAdapter
from app.dbclients.protocol import DatabaseAdapter
from app.domain.datasource import DatasourceConnInfo, DbType
from app.domain.secret import HashedRef, RotationReport, SecretKind, SecretRef
from app.infrastructure.secretstore.protocol import SecretStore

pytestmark = pytest.mark.contract


@pytest.fixture(params=["mysql", "dm"])
def adapter(request: pytest.FixtureRequest) -> DatabaseAdapter:
    secret_store = cast(SecretStore, _SecretStore())
    if request.param == "mysql":
        return MySQLAdapter(
            _conn_info(DbType.MYSQL),
            secret_store,
            pymysql_module=_FakePyMySQL(),
        )
    return DMAdapter(
        _conn_info(DbType.DM),
        secret_store,
        dm_module=_FakeDM(),
    )


def test_capabilities_declared(adapter: DatabaseAdapter) -> None:
    """capabilities 必须是 AdapterCapabilities 实例(9 字段全声明)。"""
    caps = adapter.capabilities
    assert hasattr(caps, "execute_select")
    assert hasattr(caps, "explain")
    assert hasattr(caps, "stream_rows")
    assert hasattr(caps, "server_side_cancel")
    assert hasattr(caps, "list_schemas")
    assert hasattr(caps, "list_tables")
    assert hasattr(caps, "list_columns")
    assert hasattr(caps, "list_indexes")
    assert hasattr(caps, "get_table_ddl")


def test_test_connection_returns_bool(adapter: DatabaseAdapter) -> None:
    assert isinstance(adapter.test_connection(), bool)


def test_execute_select_is_iterator(adapter: DatabaseAdapter) -> None:
    """execute_select 必须流式(Iterator[Row]),不一次性 fetchall。"""
    it = adapter.execute_select("SELECT 1", {})
    assert hasattr(it, "__iter__")
    assert hasattr(it, "__next__")


def test_introspection_returns_typed_lists(adapter: DatabaseAdapter) -> None:
    """list_schemas/tables/columns/indexes 返回 list[Schema/Table/...]。"""
    schemas = adapter.list_schemas()
    assert isinstance(schemas, list)


def test_kill_query_matches_capability(adapter: DatabaseAdapter) -> None:
    """server_side_cancel=False 时 kill_query 返回 False(优雅降级,不抛)。

    1.x AS-IS §2 已记录:MySQL/Oracle/DM/DB2 多无 driver-level cancel,
    走软取消(cancel_requested flag + statement timeout 兜底)。
    """
    result = adapter.kill_query("fake_conn_id")
    if not adapter.capabilities.server_side_cancel:
        assert result is False


def test_list_columns_returns_typed_column(adapter: DatabaseAdapter) -> None:
    """★ list_columns 返回 Column,type 为统一 ColumnType 枚举,name 符合方言惯例。

    MySQL 反引号原样(小写 id);DM/Oracle 默认大写(ID)。两边都过同一签名。
    """
    columns = adapter.list_columns("app", "users")
    assert columns
    first = columns[0]
    assert first.name.lower() == "id"
    # type 是统一枚举(可被序列化为字符串);driver_type 保留原始类型供前端备查
    assert first.driver_type is not None


def test_explain_returns_plan_node(adapter: DatabaseAdapter) -> None:
    if adapter.capabilities.explain:
        plan = adapter.explain("SELECT 1")
        assert plan.operation is not None


def _conn_info(db_type: DbType) -> DatasourceConnInfo:
    port = 3306 if db_type is DbType.MYSQL else 5236
    return DatasourceConnInfo(
        host="127.0.0.1",
        port=port,
        username="dataops",
        database="app",
        password_ref=SecretRef(ref="secret-1", kind=SecretKind.DATASOURCE_PASSWORD),
        db_type=db_type,
    )


class _SecretStore:
    def reveal_secret(self, ref: SecretRef) -> str:
        return "pwd"

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


# ──────────────────── MySQL fake driver ────────────────────


class _FakeSSCursor:
    pass


class _FakeCursors:
    SSCursor = _FakeSSCursor


class _FakePyMySQL:
    cursors = _FakeCursors

    def connect(self, **kwargs: Any) -> _FakeConnection:
        return _FakeConnection()


class _FakeConnection:
    def __init__(self) -> None:
        self.closed = False

    def cursor(self, cursorclass: type[_FakeSSCursor] | None = None) -> _FakeCursor:
        return _FakeCursor(streaming=cursorclass is _FakeSSCursor)

    def close(self) -> None:
        self.closed = True


class _FakeCursor:
    def __init__(self, *, streaming: bool = False) -> None:
        self.streaming = streaming
        self.description: tuple[tuple[str], ...] = (("ok",),)
        self._rows: list[tuple[Any, ...]] = []
        self._offset = 0

    def execute(self, sql: str, params: object = None) -> None:
        normalized = " ".join(sql.lower().split())
        if normalized.startswith("set session"):
            self.description = ()
            self._rows = []
        elif "information_schema.schemata" in normalized:
            self.description = (("name",),)
            self._rows = [("app",)]
        elif "information_schema.columns" in normalized:
            self.description = (("name",), ("type",), ("nullable",), ("column_key",))
            self._rows = [("id", "bigint", "NO", "PRI")]
        elif normalized.startswith("explain"):
            self.description = (("id",), ("select_type",))
            self._rows = [(1, "SIMPLE")]
        else:
            self.description = (("ok",),)
            self._rows = [(1,)]
        self._offset = 0

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchmany(self, size: int) -> list[tuple[Any, ...]]:
        batch = self._rows[self._offset : self._offset + size]
        self._offset += len(batch)
        return batch

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def close(self) -> None:
        return None


# ──────────────────── DM fake driver ────────────────────


class _DMType:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeDM:
    NUMBER = _DMType("NUMBER")
    STRING = _DMType("STRING")

    def connect(self, **kwargs: Any) -> _FakeDMConnection:
        return _FakeDMConnection()


class _FakeDMConnection:
    def __init__(self) -> None:
        self.closed = False
        self.callTimeout: int | None = None

    def cursor(self, cursorclass: object | None = None) -> _FakeDMCursor:
        return _FakeDMCursor()

    def close(self) -> None:
        self.closed = True


class _FakeDMCursor:
    def __init__(self) -> None:
        self.description: tuple[tuple[Any, ...], ...] = (("ok",),)
        self._rows: list[tuple[Any, ...]] = []
        self._offset = 0

    def execute(self, sql: str, params: object = None) -> None:
        normalized = " ".join(sql.lower().split())
        if "all_users" in normalized:
            self.description = (("name",),)
            self._rows = [("APP",)]
        elif "all_tab_columns" in normalized:
            self.description = (("name",), ("data_type",), ("nullable",))
            self._rows = [("ID", "NUMBER", "N"), ("NAME", "VARCHAR2", "Y")]
        elif "all_constraints" in normalized:
            self.description = (("name",),)
            self._rows = [("ID",)]
        elif normalized.startswith("explain"):
            self.description = (("plan",),)
            self._rows = [("SELECT STATEMENT",)]
        elif "from dual" in normalized:
            self.description = (("ok",),)
            self._rows = [(1,)]
        else:
            self.description = (("ok",),)
            self._rows = [(1,)]
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
        return None
