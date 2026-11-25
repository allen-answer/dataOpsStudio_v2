"""Oracle 会话缝单测(不连库)。

fake 驱动的形态贴住 11.2.0.4 + Instant Client 19.29 的实测:
异常 `args[0]` 带 `full_code` / `code`,DPY 码的 `code` 为 0;marker 由
`DBMS_DEBUG_JDWP` 拼成 "sid,serial#";`Connection.cancel()` 跨线程打断执行。
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from app.dbclients import oracle_connect
from app.dbclients.interactive import (
    CancelChannelError,
    ErrorCategory,
    InteractiveConnectError,
    OracleCancelChannel,
    OracleErrorClassifier,
    OracleInteractiveConnection,
    ServerCancelSupport,
    StatementRequest,
)
from app.dbclients.interactive.errors import oracle_full_code
from app.domain.datasource import DbType
from app.domain.schema import Column
from app.infrastructure.secretstore.protocol import SecretStore
from tests.unit._interactive_fakes import SecretStoreStub, conn_info


class _OracleErrorObject:
    def __init__(self, full_code: str) -> None:
        self.full_code = full_code
        prefix, _, digits = full_code.partition("-")
        self.code = int(digits) if prefix == "ORA" else 0
        self.message = f"{full_code}: boom"


class _OracleDatabaseError(Exception):
    pass


def _oracle_error(full_code: str) -> _OracleDatabaseError:
    return _OracleDatabaseError(_OracleErrorObject(full_code))


class _FakeCursor:
    def __init__(self, conn: _FakeConnection) -> None:
        self._conn = conn
        self.description: tuple[tuple[Any, ...], ...] = ()
        self._rows: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        self._conn.executed.append(sql)
        for fragment, exc in self._conn.errors.items():
            if fragment in sql:
                raise exc
        if "DBMS_DEBUG_JDWP" in sql:
            self._rows = [("2958,3137",)]
        elif "SYS_CONTEXT" in sql:
            self._rows = [("2958",)]
        elif sql == "SELECT 1 FROM DUAL":
            self._rows = [(1,)]
        else:
            self.description = self._conn.description
            self._rows = list(self._conn.rows)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows.pop(0) if self._rows else None

    def fetchmany(self, size: int) -> list[tuple[Any, ...]]:
        batch, self._rows = self._rows[:size], self._rows[size:]
        return batch

    def close(self) -> None:
        return None


class _FakeConnection:
    def __init__(self, kwargs: dict[str, Any], version: str) -> None:
        self.kwargs = kwargs
        self.version = version
        self.call_timeout = 0
        self.cancel_calls = 0
        self.cancel_error: Exception | None = None
        self.closed = False
        self.executed: list[str] = []
        self.errors: dict[str, Exception] = {}
        self.rows: list[tuple[Any, ...]] = [(1, "a"), (2, "b")]
        self.description: tuple[tuple[Any, ...], ...] = (
            ("ID", None, None, None, 10, 0, False),
            ("NAME", None, None, None, None, None, True),
        )
        self.call_timeouts: list[int] = []

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "call_timeout" and "call_timeouts" in self.__dict__:
            self.__dict__["call_timeouts"].append(value)
        super().__setattr__(name, value)

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def cancel(self) -> None:
        self.cancel_calls += 1
        if self.cancel_error is not None:
            raise self.cancel_error

    def close(self) -> None:
        self.closed = True


class _FakeOracleDb:
    def __init__(self, *, version: str = "19.0.0.0.0", errors: dict[str, Exception] | None = None):
        self.version = version
        self.errors = errors or {}
        self.connections: list[_FakeConnection] = []
        self.init_calls: list[str] = []

    def connect(self, **kwargs: Any) -> _FakeConnection:
        conn = _FakeConnection(kwargs, self.version)
        conn.errors.update(self.errors)
        self.connections.append(conn)
        return conn

    def init_oracle_client(self, *, lib_dir: str) -> None:
        self.init_calls.append(lib_dir)


def _info(**extra: Any) -> Any:
    return conn_info(DbType.ORACLE, port=1521, database="ORCL", extra=extra)


def _connection(fake: _FakeOracleDb, **kwargs: Any) -> OracleInteractiveConnection:
    return OracleInteractiveConnection(
        _info(), cast(SecretStore, SecretStoreStub()), oracledb_module=fake, **kwargs
    )


def _channel(fake: _FakeOracleDb) -> OracleCancelChannel:
    return OracleCancelChannel(_info(), cast(SecretStore, SecretStoreStub()), oracledb_module=fake)


@pytest.fixture(autouse=True)
def _thin_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oracle_connect, "_client_init_done", False)
    monkeypatch.setattr(oracle_connect, "_client_init_error", None)
    monkeypatch.delenv(oracle_connect.CLIENT_DIR_ENV, raising=False)


# ── 会话连接 ────────────────────────────────────────────────────────────────


def test_open_returns_sid_and_serial_marker() -> None:
    fake = _FakeOracleDb()
    connection = _connection(fake)

    assert connection.open() == "2958,3137"
    assert connection.session_marker == "2958,3137"
    assert fake.connections[0].kwargs["dsn"] == "db.internal:1521/ORCL"
    assert fake.connections[0].kwargs["tcp_connect_timeout"] == 10


def test_marker_falls_back_to_sid_when_jdwp_is_revoked() -> None:
    fake = _FakeOracleDb(errors={"DBMS_DEBUG_JDWP": _oracle_error("ORA-00904")})

    assert _connection(fake).open() == "2958"


def test_open_failure_is_classified_by_full_code() -> None:
    class _RefusingDb(_FakeOracleDb):
        def connect(self, **kwargs: Any) -> _FakeConnection:
            raise _oracle_error("ORA-01017")

    with pytest.raises(InteractiveConnectError) as excinfo:
        _connection(_RefusingDb()).open()

    assert excinfo.value.classified.category is ErrorCategory.AUTH_FAILED
    assert excinfo.value.classified.driver_code == 1017


def test_local_cancel_interrupts_the_open_connection() -> None:
    fake = _FakeOracleDb()
    connection = _connection(fake)
    assert connection.local_cancel() is False  # 未建连

    connection.open()
    assert connection.local_cancel() is True
    assert fake.connections[0].cancel_calls == 1

    fake.connections[0].cancel_error = RuntimeError("driver refused")
    assert connection.local_cancel() is False


def test_call_timeout_belt_follows_each_statement_timeout() -> None:
    fake = _FakeOracleDb()
    connection = _connection(fake)
    connection.open()

    list(connection.execute(StatementRequest(sql="SELECT 1 x FROM t", timeout_seconds=600)))
    list(connection.execute(StatementRequest(sql="SELECT 2 x FROM t", timeout_seconds=600)))
    list(connection.execute(StatementRequest(sql="SELECT 3 x FROM t", timeout_seconds=0)))

    # 同值不重复设置;0(不限)关闭保险带
    assert fake.connections[0].call_timeouts == [630_000, 0]


def test_pre12c_fetch_first_is_rewritten_to_rownum() -> None:
    fake = _FakeOracleDb(version="11.2.0.4.0")
    connection = _connection(fake)
    connection.open()

    list(connection.execute(StatementRequest(sql="SELECT id FROM t FETCH FIRST 5 ROWS ONLY")))

    executed = fake.connections[0].executed[-1]
    assert "FETCH FIRST" not in executed
    assert "ROWNUM <= 5" in executed


def test_pre12c_offset_rewrite_drops_the_row_number_column() -> None:
    fake = _FakeOracleDb(version="11.2.0.4.0")
    connection = _connection(fake)
    connection.open()
    conn = fake.connections[0]
    conn.rows = [(1, "a", 6), (2, "b", 7)]
    conn.description = (*conn.description, ("DATAOPS_RN__", None, None, None, None, 0, True))
    emitted: list[list[Column]] = []

    rows = list(
        connection.execute(
            StatementRequest(
                sql="SELECT id, name FROM t ORDER BY id OFFSET 5 ROWS FETCH NEXT 2 ROWS ONLY",
                column_sink=emitted.append,
            )
        )
    )

    assert [row.values for row in rows] == [[1, "a"], [2, "b"]]
    assert [column.name for column in emitted[0]] == ["ID", "NAME"]
    assert "DATAOPS_RN__ > 5" in conn.executed[-1]


def test_12c_and_later_keep_fetch_first() -> None:
    fake = _FakeOracleDb(version="19.0.0.0.0")
    connection = _connection(fake)
    connection.open()

    sql = "SELECT id FROM t FETCH FIRST 5 ROWS ONLY"
    list(connection.execute(StatementRequest(sql=sql)))

    assert fake.connections[0].executed[-1] == sql


def test_thick_mode_is_initialized_once_and_shared_with_the_job_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(oracle_connect.CLIENT_DIR_ENV, "C:/oracle/instantclient_19_29")
    fake = _FakeOracleDb()

    _connection(fake).open()
    _channel(fake).open()
    oracle_connect.ensure_client_mode(fake)  # job 路径同一入口

    assert fake.init_calls == ["C:/oracle/instantclient_19_29"]


# ── 控制通道 ────────────────────────────────────────────────────────────────


def test_channel_reports_available_without_probing_a_cancel() -> None:
    fake = _FakeOracleDb()
    channel = _channel(fake)

    assert channel.open() is ServerCancelSupport.AVAILABLE
    assert not any("ALTER SYSTEM" in sql for sql in fake.connections[0].executed)


def test_channel_destroy_kills_session_by_sid_and_serial() -> None:
    fake = _FakeOracleDb()
    channel = _channel(fake)
    channel.open()

    channel.destroy("2958,3137")

    assert fake.connections[0].executed[-1] == "ALTER SYSTEM KILL SESSION '2958,3137' IMMEDIATE"


def test_channel_destroy_without_serial_cannot_address_the_session() -> None:
    channel = _channel(_FakeOracleDb())
    channel.open()

    with pytest.raises(CancelChannelError):
        channel.destroy("2958")


def test_channel_destroy_without_privilege_fails_loudly() -> None:
    fake = _FakeOracleDb(errors={"ALTER SYSTEM": _oracle_error("ORA-01031")})
    channel = _channel(fake)
    channel.open()

    with pytest.raises(CancelChannelError):
        channel.destroy("2958,3137")


@pytest.mark.parametrize("marker", ["2958;DROP", "2958,3137,1", "x,1", ""])
def test_channel_rejects_malformed_markers(marker: str) -> None:
    channel = _channel(_FakeOracleDb())
    channel.open()

    with pytest.raises(CancelChannelError):
        channel.destroy(marker)


def test_channel_has_no_sql_cancel() -> None:
    channel = _channel(_FakeOracleDb())
    channel.open()

    with pytest.raises(CancelChannelError):
        channel.cancel("2958,3137")


# ── 错误分类 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("full_code", "category", "driver_code", "confirmed"),
    [
        ("ORA-01013", ErrorCategory.CANCELLED, 1013, True),
        ("DPY-4024", ErrorCategory.TIMEOUT, 4024, True),
        ("ORA-01017", ErrorCategory.AUTH_FAILED, 1017, False),
        ("ORA-12541", ErrorCategory.HOST_UNREACHABLE, 12541, False),
        ("ORA-00942", ErrorCategory.STATEMENT_ERROR, 942, False),
        ("ORA-03113", ErrorCategory.CONNECTION_DEAD, 3113, False),
        ("ORA-99999", ErrorCategory.UNKNOWN, 99999, False),
    ],
)
def test_classifier_uses_full_code(
    full_code: str, category: ErrorCategory, driver_code: int, confirmed: bool
) -> None:
    classified = OracleErrorClassifier().classify(_oracle_error(full_code))

    assert classified.category is category
    assert classified.driver_code == driver_code
    assert classified.server_confirmed is confirmed


def test_classifier_never_confuses_dpy_codes_with_their_zero_code_field() -> None:
    # DPY 码的 .code 恒为 0;只看整数会落到码 0。
    exc = _oracle_error("DPY-4024")
    assert exc.args[0].code == 0
    assert oracle_full_code(exc) == "DPY-4024"


def test_classifier_falls_back_to_exception_type() -> None:
    classified = OracleErrorClassifier().classify(ConnectionResetError("reset"))

    assert classified.category is ErrorCategory.CONNECTION_DEAD
    assert classified.driver_code is None
