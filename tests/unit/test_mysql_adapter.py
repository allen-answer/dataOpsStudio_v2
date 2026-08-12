from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from app.dbclients.mysql_adapter import MySQLAdapter
from app.dbclients.protocol import AdapterConnectionError
from app.dbclients.sql_guard import SqlGuardError, validate_readonly_sql
from app.domain.datasource import DatasourceConnInfo, DbType
from app.domain.result import ResultSet
from app.domain.schema import Column, ColumnType, Row
from app.domain.secret import HashedRef, RotationReport, SecretKind, SecretRef
from app.infrastructure.resultstore.local_fs import LocalFsResultStore
from app.infrastructure.secretstore.protocol import SecretStore


def test_sql_guard_accepts_select_and_with() -> None:
    assert validate_readonly_sql("-- comment\nSELECT 1") == "SELECT 1"
    assert validate_readonly_sql("/* c */ WITH q AS (SELECT 1) SELECT * FROM q")


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE users SET name = 'x'",
        "SELECT * FROM users; SELECT * FROM projects",
        "SELECT * FROM users FOR UPDATE",
        "WITH q AS (DELETE FROM users RETURNING id) SELECT * FROM q",
    ],
)
def test_sql_guard_rejects_non_readonly(sql: str) -> None:
    with pytest.raises(SqlGuardError):
        validate_readonly_sql(sql)


def test_execute_select_uses_sscursor_reveals_secret_and_closes_resources() -> None:
    fake_pymysql = _FakePyMySQL()
    secret_store = _SecretStore("pwd")
    adapter = MySQLAdapter(
        _conn_info(),
        cast(SecretStore, secret_store),
        pymysql_module=fake_pymysql,
        fetch_chunk_size=1,
    )

    rows = list(adapter.execute_select("SELECT 1", {}))

    assert rows == [Row(values=[1]), Row(values=[2])]
    assert secret_store.revealed_refs == ["secret-1"]
    assert fake_pymysql.connections[0].stream_cursor_used is True
    assert all(cursor.closed for cursor in fake_pymysql.connections[0].cursors)
    assert fake_pymysql.connections[0].closed is True


def test_execute_select_uses_request_scoped_fetch_size() -> None:
    fake_pymysql = _FakePyMySQL()
    adapter = MySQLAdapter(
        _conn_info(),
        cast(SecretStore, _SecretStore("pwd")),
        pymysql_module=fake_pymysql,
        fetch_chunk_size=100,
    )

    list(adapter.execute_select("SELECT 1", {}))

    assert fake_pymysql.connections[0].cursors[-1].fetchmany_sizes == [100, 100]


def test_execute_select_emits_columns_without_leaking_cursor_reference() -> None:
    fake_pymysql = _FakePyMySQL()
    captured_columns: list[Column] = []
    adapter = MySQLAdapter(
        _conn_info(),
        cast(SecretStore, _SecretStore("pwd")),
        pymysql_module=fake_pymysql,
        fetch_chunk_size=1,
        column_sink=captured_columns.extend,
    )

    rows = list(adapter.execute_select("SELECT 1 AS n", {}))

    assert rows == [Row(values=[1]), Row(values=[2])]
    assert captured_columns == [Column(name="n", type=ColumnType.UNKNOWN)]
    assert all(not hasattr(column, "cursor") for column in captured_columns)
    assert fake_pymysql.connections[0].closed is True


def test_execute_select_emits_columns_for_zero_row_result() -> None:
    fake_pymysql = _FakePyMySQL()
    captured_columns: list[Column] = []
    adapter = MySQLAdapter(
        _conn_info(),
        cast(SecretStore, _SecretStore("pwd")),
        pymysql_module=fake_pymysql,
        column_sink=captured_columns.extend,
    )

    rows = list(adapter.execute_select("SELECT 1 AS n WHERE 1 = 0", {}))

    assert rows == []
    assert captured_columns == [Column(name="n", type=ColumnType.UNKNOWN)]


def test_test_connection_records_server_version() -> None:
    fake_pymysql = _FakePyMySQL()
    adapter = MySQLAdapter(
        _conn_info(),
        cast(SecretStore, _SecretStore("pwd")),
        pymysql_module=fake_pymysql,
    )

    assert adapter.test_connection() is True

    assert adapter.last_server_version == "MySQL 8.0.test"
    assert fake_pymysql.connections[0].closed is True


def test_test_connection_records_sanitized_error_summary_on_failure() -> None:
    fake_pymysql = _FailingPyMySQL(
        RuntimeError("connect failed password=super-secret mysql://user:super-secret@db/app")
    )
    adapter = MySQLAdapter(
        _conn_info(),
        cast(SecretStore, _SecretStore("super-secret")),
        pymysql_module=fake_pymysql,
    )

    assert adapter.test_connection() is False

    assert adapter.last_server_version is None
    assert adapter.last_connection_error is not None
    assert adapter.last_connection_error.startswith("RuntimeError")
    assert "super-secret" not in adapter.last_connection_error
    assert "mysql://user" not in adapter.last_connection_error
    assert "<redacted-url>" in adapter.last_connection_error
    assert "***REDACTED***" in adapter.last_connection_error


def test_stream_select_preserves_connection_error_cause() -> None:
    # from exc(非 from None):AdapterConnectionError 必须链住原始驱动异常,
    # 便于 worker logger.exception 排障(R5 脱敏 processor 兜底敏感值)。
    original = RuntimeError("driver connect boom")
    adapter = MySQLAdapter(
        _conn_info(),
        cast(SecretStore, _SecretStore("pwd")),
        pymysql_module=_FailingPyMySQL(original),
    )

    with pytest.raises(AdapterConnectionError) as excinfo:
        list(adapter.execute_select("SELECT 1", {}))

    assert excinfo.value.__cause__ is original


def test_stream_to_spool_then_resultset_reads_without_cursor(tmp_path: Path) -> None:
    fake_pymysql = _FakePyMySQL()
    adapter = MySQLAdapter(
        _conn_info(),
        cast(SecretStore, _SecretStore("pwd")),
        pymysql_module=fake_pymysql,
        fetch_chunk_size=1,
    )
    store = LocalFsResultStore(tmp_path)
    result_set_id = "rs-1"

    for row in adapter.execute_select("SELECT 1", {}):
        store.append_spool(result_set_id, [row])

    result_set = ResultSet(
        result_set_id=result_set_id,
        execution_id="exec-1",
        storage_ref=store.spool_ref(result_set_id),
        columns=[Column(name="n", type=ColumnType.INTEGER, driver_type="int", nullable=False)],
        loaded_rows=2,
        total_rows=2,
        state="complete",
        created_at=datetime.now(UTC),
    )

    assert result_set.storage_ref == store.spool_ref(result_set_id)
    assert "cursor" not in ResultSet.model_fields
    assert fake_pymysql.connections[0].closed is True
    assert store.fetch_range(result_set_id, 0, 2) == [Row(values=[1]), Row(values=[2])]


def test_spool_row_limit_marks_truncated(tmp_path: Path) -> None:
    store = LocalFsResultStore(tmp_path, spool_max_rows=2)

    store.append_spool("rs-1", [Row(values=[1]), Row(values=[2]), Row(values=[3])])

    assert store.fetch_range("rs-1", 0, 10) == [Row(values=[1]), Row(values=[2])]
    assert store.get_spool_manifest("rs-1")["truncated"] is True


def test_spool_can_be_marked_truncated_idempotently(tmp_path: Path) -> None:
    store = LocalFsResultStore(tmp_path)
    store.append_spool("rs-1", [Row(values=[1])])

    store.mark_spool_truncated("rs-1")
    store.mark_spool_truncated("rs-1")

    manifest = store.get_spool_manifest("rs-1")
    assert manifest["loaded_rows"] == 1
    assert manifest["truncated"] is True


def test_spool_byte_limit_marks_truncated(tmp_path: Path) -> None:
    store = LocalFsResultStore(tmp_path, spool_max_bytes=1)

    store.append_spool("rs-1", [Row(values=["x" * 1024])])

    assert store.fetch_range("rs-1", 0, 10) == []
    assert store.get_spool_manifest("rs-1")["truncated"] is True


@pytest.mark.integration
def test_mysql_integration_releases_connection_and_spool_survives_disconnect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _mysql_env_or_skip()
    pymysql = pytest.importorskip("pymysql")
    secret_store = _SecretStore(env["password"])
    adapter = MySQLAdapter(
        _conn_info(
            host=env["host"],
            port=int(env["port"]),
            username=env["user"],
            database=env["database"],
        ),
        cast(SecretStore, secret_store),
        statement_timeout_seconds=30,
    )
    store = LocalFsResultStore(tmp_path)
    result_set_id = "mysql-rs-1"

    before = _threads_connected(pymysql, env)
    for row in adapter.execute_select("SELECT 1 AS n UNION ALL SELECT 2 AS n", {}):
        store.append_spool(result_set_id, [row])
    after = _threads_connected(pymysql, env)

    assert after <= before

    def fail_connect(**kwargs: Any) -> object:
        raise AssertionError("fetch_range must not touch MySQL")

    monkeypatch.setattr(pymysql, "connect", fail_connect)
    assert store.fetch_range(result_set_id, 0, 2) == [Row(values=[1]), Row(values=[2])]


def _mysql_env_or_skip() -> dict[str, str]:
    keys = {
        "host": "DATAOPS_TEST_MYSQL_HOST",
        "port": "DATAOPS_TEST_MYSQL_PORT",
        "user": "DATAOPS_TEST_MYSQL_USER",
        "password": "DATAOPS_TEST_MYSQL_PASSWORD",
        "database": "DATAOPS_TEST_MYSQL_DATABASE",
    }
    values = {name: os.environ.get(env_name) for name, env_name in keys.items()}
    missing = [keys[name] for name, value in values.items() if not value]
    if missing:
        pytest.skip(f"MySQL integration env not set: {', '.join(missing)}")
    return {name: str(value) for name, value in values.items()}


def _threads_connected(pymysql: Any, env: dict[str, str]) -> int:
    conn = pymysql.connect(
        host=env["host"],
        port=int(env["port"]),
        user=env["user"],
        password=env["password"],
        database=env["database"],
        connect_timeout=5,
    )
    cursor = conn.cursor()
    try:
        cursor.execute("SHOW STATUS LIKE 'Threads_connected'")
        row = cursor.fetchone()
        return int(row[1])
    finally:
        cursor.close()
        conn.close()


def _conn_info(
    *,
    host: str = "127.0.0.1",
    port: int = 3306,
    username: str = "dataops",
    database: str = "app",
) -> DatasourceConnInfo:
    return DatasourceConnInfo(
        host=host,
        port=port,
        username=username,
        database=database,
        password_ref=SecretRef(ref="secret-1", kind=SecretKind.DATASOURCE_PASSWORD),
        db_type=DbType.MYSQL,
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


class _FakeSSCursor:
    pass


class _FakeCursors:
    SSCursor = _FakeSSCursor


class _FakePyMySQL:
    cursors = _FakeCursors

    def __init__(self) -> None:
        self.connections: list[_FakeConnection] = []

    def connect(self, **kwargs: Any) -> _FakeConnection:
        conn = _FakeConnection(kwargs)
        self.connections.append(conn)
        return conn


class _FailingPyMySQL:
    cursors = _FakeCursors

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def connect(self, **kwargs: Any) -> _FakeConnection:
        raise self._exc


class _FakeConnection:
    def __init__(self, kwargs: dict[str, Any]) -> None:
        self.kwargs = kwargs
        self.closed = False
        self.cursors: list[_FakeCursor] = []
        self.stream_cursor_used = False

    def cursor(self, cursorclass: type[_FakeSSCursor] | None = None) -> _FakeCursor:
        cursor = _FakeCursor(streaming=cursorclass is _FakeSSCursor)
        if cursor.streaming:
            self.stream_cursor_used = True
        self.cursors.append(cursor)
        return cursor

    def close(self) -> None:
        self.closed = True


class _FakeCursor:
    def __init__(self, *, streaming: bool = False) -> None:
        self.streaming = streaming
        self.closed = False
        self.description: tuple[tuple[str], ...] = (("n",),)
        self._rows: list[tuple[Any, ...]] = []
        self._offset = 0
        self.fetchmany_sizes: list[int] = []

    def execute(self, sql: str, params: object = None) -> None:
        if sql.lower().startswith("set session"):
            self._rows = []
        elif "version()" in sql.lower():
            self._rows = [("8.0.test",)]
        elif "where 1 = 0" in sql.lower():
            self._rows = []
        else:
            self._rows = [(1,), (2,)]
        self._offset = 0

    def fetchone(self) -> tuple[Any, ...] | None:
        rows = self.fetchmany(1)
        return rows[0] if rows else None

    def fetchmany(self, size: int) -> list[tuple[Any, ...]]:
        self.fetchmany_sizes.append(size)
        batch = self._rows[self._offset : self._offset + size]
        self._offset += len(batch)
        return batch

    def close(self) -> None:
        self.closed = True
