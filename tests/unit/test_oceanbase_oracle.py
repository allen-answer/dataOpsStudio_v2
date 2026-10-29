"""OB-Oracle:类型解码 / 驱动接线 / 适配器单测。

解码用例的原始字节取自 POC(OB 4.3.5.4,真库抓包),期望值是同一表达式 TO_CHAR(...) 的结果。
驱动接线用真实 PyMySQL 类(defer_connect,不连网)验证能力位、转义和逐列解码路径。
"""

from __future__ import annotations

import importlib
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, cast

import pytest

from app.dbclients import oceanbase_oracle_driver as ob_driver
from app.dbclients.factory import build_database_adapter
from app.dbclients.oceanbase_oracle_adapter import (
    InvalidDatasourceError,
    ObOracleAdapterError,
    ObOracleDdlUnavailableError,
    OceanBaseOracleAdapter,
)
from app.dbclients.oceanbase_oracle_types import (
    decode_interval_ds,
    decode_interval_ym,
    decode_timestamp,
    decode_timestamp_tz,
    field_to_column_type,
    field_type_name,
)
from app.dbclients.query_limit import apply_database_row_limit
from app.dbclients.sql_build import quote_identifier
from app.domain.datasource import DatasourceConnInfo, DbType
from app.domain.schema import ColumnType
from app.domain.secret import SecretKind, SecretRef
from app.infrastructure.secretstore.protocol import SecretStore

# R1:驱动不在 app/dbclients/ 之外静态 import;与集成测试同样按运行期加载真实 PyMySQL 类。
pymysql: Any = importlib.import_module("pymysql")

# ─────────────────────────── 类型解码(POC 真库字节) ───────────────────────────


@pytest.mark.parametrize(
    ("raw_hex", "expected"),
    [
        # TO_CHAR: 1999-12-31 23:59:58.000001000
        ("13630c1f173b3ae803000009", datetime(1999, 12, 31, 23, 59, 58, 1)),
        # 0001-01-01 00:00:00
        ("000101010000000000000009", datetime(1, 1, 1)),
        # 9999-12-31 23:59:59.999999000
        ("63630c1f173b3b18c69a3b09", datetime(9999, 12, 31, 23, 59, 59, 999999)),
        # TIMESTAMP(3) 2024-05-06 07:08:09.123
        ("14180506070809c0d4540703", datetime(2024, 5, 6, 7, 8, 9, 123000)),
        # TIMESTAMP WITH LOCAL TIME ZONE(会话 +08:00)2024-01-02 11:04:05
        ("141801020b04050000000006", datetime(2024, 1, 2, 11, 4, 5)),
    ],
)
def test_decode_timestamp_matches_to_char(raw_hex: str, expected: datetime) -> None:
    assert decode_timestamp(bytes.fromhex(raw_hex)) == expected


def test_decode_timestamp_tz_negative_half_hour_offset() -> None:
    # TO_CHAR: 2024-01-02 03:04:05.000000000 -03:30
    value = decode_timestamp_tz(bytes.fromhex("141801020304050000000009fd1e0000"))
    assert value == datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone(-timedelta(hours=3, minutes=30)))


def test_decode_timestamp_tz_with_region_name() -> None:
    # TO_CHAR: 2024-07-02 03:04:05.000000000 +08:00 Asia/Shanghai CST
    raw = bytes.fromhex("14180702030405000000000908000d417369612f5368616e6768616903435354")
    assert decode_timestamp_tz(raw) == datetime(
        2024, 7, 2, 3, 4, 5, tzinfo=timezone(timedelta(hours=8))
    )


def test_decode_intervals_match_to_char() -> None:
    # INTERVAL '-12-11' YEAR(3) TO MONTH → TO_CHAR: -012-11
    assert decode_interval_ym(bytes.fromhex("010c0000000b03")) == "-012-11"
    # INTERVAL '1-2' YEAR TO MONTH
    assert decode_interval_ym(bytes.fromhex("00010000000202")) == "+01-02"
    # INTERVAL '-123 01:02:03.456' DAY(3) TO SECOND(3)
    assert decode_interval_ds(bytes.fromhex("017b00000001020300022e1b0303")) == -timedelta(
        days=123, hours=1, minutes=2, seconds=3, milliseconds=456
    )


@pytest.mark.parametrize(
    ("type_code", "scale", "charsetnr", "column_type", "name"),
    [
        (246, 0, 63, ColumnType.INTEGER, "NUMBER"),
        (246, 2, 63, ColumnType.DECIMAL, "NUMBER"),
        (206, 171, 63, ColumnType.DECIMAL, "FLOAT"),
        (12, 0, 63, ColumnType.DATETIME, "DATE"),  # Oracle DATE 带时分秒
        (202, 9, 63, ColumnType.DATETIME, "TIMESTAMP"),
        (200, 9, 63, ColumnType.DATETIME, "TIMESTAMP WITH TIME ZONE"),
        (203, 255, 63, ColumnType.BYTES, "RAW"),
        (207, 255, 46, ColumnType.STRING, "NVARCHAR2"),
        (251, 255, 46, ColumnType.STRING, "CLOB"),
        (251, 255, 63, ColumnType.BYTES, "BLOB"),
        (5, 171, 63, ColumnType.FLOAT, "BINARY_DOUBLE"),
        (999, 0, 63, ColumnType.UNKNOWN, None),
    ],
)
def test_field_type_mapping(
    type_code: int, scale: int, charsetnr: int, column_type: ColumnType, name: str | None
) -> None:
    assert field_to_column_type(type_code, scale=scale, charsetnr=charsetnr) is column_type
    assert field_type_name(type_code, charsetnr=charsetnr) == name


# ─────────────────────────── 驱动接线(真实 PyMySQL 类) ───────────────────────────


@pytest.mark.parametrize(
    ("username", "extra", "expected"),
    [
        ("app_user", {"tenant": "t1", "cluster": "c1"}, "app_user@t1#c1"),
        ("app_user", {"tenant": "t1", "cluster": "c1", "access_mode": "proxy"}, "app_user@t1#c1"),
        ("app_user", {"tenant": "t1", "access_mode": "direct"}, "app_user@t1"),
        ("app_user@t9#c9", {}, "app_user@t9#c9"),  # 已拼好的完整串原样用
    ],
)
def test_assemble_username(username: str, extra: dict[str, str], expected: str) -> None:
    assert ob_driver.assemble_username(username, extra) == expected


@pytest.mark.parametrize(
    "extra",
    [{}, {"tenant": "t1"}, {"tenant": "t1", "cluster": "c1", "access_mode": "socks"}],
)
def test_assemble_username_rejects_incomplete_config(extra: dict[str, str]) -> None:
    with pytest.raises(ob_driver.ObOracleConfigError):
        ob_driver.assemble_username("app_user", extra)


def test_escape_literal_uses_oracle_rules() -> None:
    # 反斜杠在 Oracle 方言里是普通字符;只双写单引号(POC:PyMySQL 缺省 \' 会报 900)
    assert ob_driver.escape_literal("a'b\\c") == "'a''b\\c'"
    assert ob_driver.escape_literal(None) == "NULL"
    assert ob_driver.escape_literal(True) == "1"
    assert ob_driver.escape_literal(7) == "7"
    assert ob_driver.escape_literal(Decimal("1.50")) == "1.50"
    assert ob_driver.escape_literal(b"\x0a\xff") == "HEXTORAW('0AFF')"
    assert ob_driver.escape_literal(date(2024, 1, 2)) == "DATE '2024-01-02'"
    assert ob_driver.escape_literal(datetime(2024, 1, 2, 3, 4, 5, 6)) == (
        "TO_TIMESTAMP('2024-01-02 03:04:05.000006', 'YYYY-MM-DD HH24:MI:SS.FF6')"
    )
    assert ob_driver.escape_literal(("a", 1)) == "('a',1)"


def _deferred_connection(**kwargs: Any) -> Any:
    return ob_driver.connect(
        pymysql, host="127.0.0.1", port=1, user="u", password="p", defer_connect=True, **kwargs
    )


def test_connect_sets_oracle_mode_capability_bit() -> None:
    conn = _deferred_connection()
    assert conn.client_flag & ob_driver.OB_CLIENT_SUPPORT_ORACLE_MODE
    # PyMySQL 自己的缺省能力位仍在
    assert conn.client_flag & pymysql.constants.CLIENT.PROTOCOL_41


def test_connection_escapes_parameters_with_oracle_rules() -> None:
    conn = _deferred_connection(charset="utf8mb4")
    cursor = conn.cursor()

    rendered = cursor.mogrify(
        "SELECT 1 FROM DUAL WHERE a = %(a)s AND b = %(b)s", {"a": "O'Brien\\x", "b": 3}
    )

    assert rendered == "SELECT 1 FROM DUAL WHERE a = 'O''Brien\\x' AND b = 3"


class _FakeField:
    def __init__(self, type_code: int, charsetnr: int) -> None:
        self.type_code = type_code
        self.charsetnr = charsetnr

    def description(self) -> tuple[Any, ...]:
        return ("X", self.type_code, None, 0, 0, 0, True)


class _FakePacket:
    def __init__(self, cells: list[bytes | None] | None = None) -> None:
        self._cells = list(cells or [])

    def is_eof_packet(self) -> bool:
        return True

    def read_length_coded_string(self) -> bytes | None:
        if not self._cells:
            raise IndexError
        return self._cells.pop(0)


def test_result_decoding_handles_extended_types_without_ascii_errors() -> None:
    """PyMySQL 原路径会把这些列按 ascii 解码并抛错、毁掉连接;接管后逐列正确。"""
    conn = _deferred_connection(charset="utf8mb4")
    fields = [
        _FakeField(207, 46),  # NVARCHAR2 含中文
        _FakeField(202, 63),  # TIMESTAMP(纳秒字节 ≥0x80)
        _FakeField(205, 63),  # INTERVAL DAY TO SECOND
        _FakeField(203, 63),  # RAW
        _FakeField(206, 63),  # FLOAT(b)
        _FakeField(253, 46),  # VARCHAR2 中文(PyMySQL 原生即可)
    ]
    # 用 OB 连接类实际使用的结果类
    result = _described_result(conn.result_class, conn, fields)

    row = result._read_row_from_packet(
        _FakePacket(
            [
                "n中".encode(),
                bytes.fromhex("13630c1f173b3ae803000009"),
                bytes.fromhex("017b00000001020300022e1b0303"),
                b"\n\x0b\xff",
                b"1.5",
                "abc中文".encode(),
            ]
        )
    )

    assert row == (
        "n中",
        datetime(1999, 12, 31, 23, 59, 58, 1),
        -timedelta(days=123, hours=1, minutes=2, seconds=3, milliseconds=456),
        b"\n\x0b\xff",
        Decimal("1.5"),
        "abc中文",
    )


def test_stock_pymysql_result_breaks_on_nvarchar2_chinese() -> None:
    """反证:不接管时 PyMySQL 按 ascii 解 207 列,中文直接 UnicodeDecodeError。"""
    conn = pymysql.connections.Connection(
        host="127.0.0.1", port=1, user="u", password="p", charset="utf8mb4", defer_connect=True
    )
    fields = [_FakeField(207, 46)]
    result = _described_result(pymysql.connections.MySQLResult, conn, fields)

    with pytest.raises(UnicodeDecodeError):
        result._read_row_from_packet(_FakePacket(["n中".encode()]))


def _described_result(result_class: Any, conn: Any, fields: list[_FakeField]) -> Any:
    """喂假字段包跑 PyMySQL 的 _get_descriptions(不读网络)。"""
    packets: list[object] = [*fields, _FakePacket()]
    conn._read_packet = lambda packet_type=None: packets.pop(0)
    result = result_class.__new__(result_class)
    result.connection = conn
    result.field_count = len(fields)
    result.unbuffered_active = False
    result._get_descriptions()
    return result


# ─────────────────────────── 方言登记 ───────────────────────────


def test_dialect_registration_uses_oracle_sql() -> None:
    assert quote_identifier(DbType.OCEANBASE_ORACLE, "app.orders") == '"APP"."ORDERS"'
    assert (
        apply_database_row_limit("SELECT a FROM t", DbType.OCEANBASE_ORACLE, 5)
        == "SELECT a FROM t FETCH FIRST 5 ROWS ONLY"
    )


def test_factory_builds_ob_oracle_adapter() -> None:
    adapter = build_database_adapter(_conn_info(), cast(SecretStore, _SecretStore()))
    assert isinstance(adapter, OceanBaseOracleAdapter)
    assert adapter.capabilities.compare_db_hash is False
    assert adapter.capabilities.explain is True


def test_adapter_rejects_other_db_types() -> None:
    info = _conn_info().model_copy(update={"db_type": DbType.MYSQL})
    with pytest.raises(InvalidDatasourceError):
        OceanBaseOracleAdapter(info, cast(SecretStore, _SecretStore()))


# ─────────────────────────── 适配器(fake 连接) ───────────────────────────


def test_session_is_prepared_with_timeouts_and_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_fake_driver(monkeypatch)
    adapter = _adapter(statement_timeout_seconds=30, cursor_max_hold_seconds=900)

    list(adapter.execute_select("SELECT 1 FROM DUAL", {}))

    kwargs = fake.connect_kwargs[0]
    assert kwargs["user"] == "app_user@t1#c1"
    assert kwargs["charset"] == "utf8mb4"
    assert "database" not in kwargs
    session_sql = fake.connections[0].cursors[0].executed
    assert session_sql == [
        "SET ob_query_timeout = 30000000",
        "SET net_write_timeout = 900",  # 不小于游标最长持有时间(POC V8)
        'ALTER SESSION SET CURRENT_SCHEMA = "APP"',
    ]


def test_streaming_uses_sscursor_and_emits_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_fake_driver(
        monkeypatch,
        description=(("ID", 246, None, 11, 11, 0, False), ("BODY", 251, None, 0, 0, 255, True)),
        charsets=[63, 46],
        rows=[(1, "a"), (2, "b")],
    )
    captured: list[Any] = []
    adapter = _adapter(column_sink=captured.extend)

    rows = list(adapter.execute_select("SELECT id, body FROM t", {}))

    assert [row.values for row in rows] == [[1, "a"], [2, "b"]]
    assert fake.connections[0].cursors[1].cursor_class is pymysql.cursors.SSCursor
    assert [(c.name, c.type, c.driver_type) for c in captured] == [
        ("ID", ColumnType.INTEGER, "NUMBER"),
        ("BODY", ColumnType.STRING, "CLOB"),
    ]


def test_test_connection_reports_version_and_checks_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_driver(monkeypatch, mode="ORACLE", version="4.3.5.4")
    adapter = _adapter()

    assert adapter.test_connection() is True
    assert adapter.last_server_version == "OceanBase 4.3.5.4 (Oracle mode)"


def test_test_connection_rejects_mysql_mode_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_driver(monkeypatch, mode="MYSQL")
    adapter = _adapter()

    assert adapter.test_connection() is False
    assert adapter.last_connection_error is not None
    assert "MySQL mode" in adapter.last_connection_error or "MYSQL" in adapter.last_connection_error


def test_list_tables_renders_pyformat_catalog_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_fake_driver(
        monkeypatch,
        description=(("SCHEMA_NAME",), ("NAME",), ("TABLE_TYPE",)),
        rows=[("APP", "ORDERS", "TABLE")],
    )

    tables = _adapter().list_tables("app")

    assert [(t.schema_name, t.name, t.table_type) for t in tables] == [("APP", "ORDERS", "TABLE")]
    cursor = fake.connections[0].cursors[1]
    assert "OWNER = UPPER(%(owner)s)" in cursor.executed[0]
    assert cursor.params[0] == {"owner": "app"}


def test_get_table_ddl_permission_error_is_structured(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_driver(
        monkeypatch,
        execute_error=pymysql.err.OperationalError(31603, "OBE-31603: object not found"),
    )

    with pytest.raises(ObOracleDdlUnavailableError):
        _adapter().get_table_ddl("APP", "T")


def test_get_table_ddl_returns_view_ddl(monkeypatch: pytest.MonkeyPatch) -> None:
    # 真库实测:写死 'TABLE' 时视图报 OBE-31603;类型改从 ALL_OBJECTS 取
    fake = _install_fake_driver(
        monkeypatch,
        description=(("DDL",),),
        rows=[('CREATE OR REPLACE FORCE VIEW "V_ORDERS" AS SELECT 1 FROM DUAL',)],
    )

    ddl = _adapter().get_table_ddl("app", "v_orders")

    assert ddl.startswith('CREATE OR REPLACE FORCE VIEW "V_ORDERS"')
    cursor = fake.connections[0].cursors[1]
    assert "GET_DDL(o.OBJECT_TYPE, o.OBJECT_NAME, o.OWNER)" in cursor.executed[0]
    assert cursor.params[0] == {"tbl": "v_orders", "owner": "app"}


def test_get_table_ddl_missing_object_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_driver(monkeypatch, description=(("DDL",),), rows=[])

    with pytest.raises(ObOracleDdlUnavailableError):
        _adapter().get_table_ddl("APP", "NO_SUCH")


def test_internal_code_of_600_errors_is_surfaced(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_driver(
        monkeypatch,
        execute_error=pymysql.err.InternalError(
            600,
            "OBE-00600: internal error code, arguments: -4012, Timeout, query has reached "
            "the maximum query timeout",
        ),
    )

    with pytest.raises(ObOracleAdapterError) as excinfo:
        list(_adapter().execute_select("SELECT 1 FROM DUAL", {}))

    message = str(excinfo.value)
    assert "code=600" in message
    assert "ob_internal_code=-4012" in message


# ─────────────────────────── helpers / fakes ───────────────────────────


def _conn_info() -> DatasourceConnInfo:
    return DatasourceConnInfo(
        host="127.0.0.1",
        port=2883,
        username="app_user",
        database="app",
        password_ref=SecretRef(ref="secret-1", kind=SecretKind.DATASOURCE_PASSWORD),
        db_type=DbType.OCEANBASE_ORACLE,
        extra={"tenant": "t1", "cluster": "c1"},
    )


def _adapter(**kwargs: Any) -> OceanBaseOracleAdapter:
    return OceanBaseOracleAdapter(
        _conn_info(), cast(SecretStore, _SecretStore()), pymysql_module=pymysql, **kwargs
    )


class _SecretStore:
    def reveal_secret(self, ref: SecretRef) -> str:
        return "pwd"


class _FakeCursor:
    def __init__(self, conn: _FakeConnection, cursor_class: object) -> None:
        self._conn = conn
        self.cursor_class = cursor_class
        self.executed: list[str] = []
        self.params: list[object] = []
        self.description: tuple[Any, ...] | None = None
        self._rows: list[tuple[Any, ...]] = []
        self._result = type("R", (), {"fields": [_FakeField(0, c) for c in conn.charsets]})()

    def execute(self, sql: str, params: object = None) -> None:
        self.executed.append(sql)
        self.params.append(params)
        if sql.startswith(("SET ", "ALTER SESSION")):
            return
        if self._conn.execute_error is not None:
            raise self._conn.execute_error
        if sql.startswith("SHOW VARIABLES"):
            self._rows = [("ob_compatibility_mode", self._conn.mode)]
        elif "ob_version()" in sql:
            self._rows = [(self._conn.version,)]
        else:
            self.description = self._conn.description
            self._rows = list(self._conn.rows)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows.pop(0) if self._rows else None

    def fetchmany(self, size: int) -> list[tuple[Any, ...]]:
        batch, self._rows = self._rows[:size], self._rows[size:]
        return batch

    def fetchall(self) -> list[tuple[Any, ...]]:
        rows, self._rows = self._rows, []
        return rows

    def close(self) -> None:
        return None


class _FakeConnection:
    def __init__(self, fake: _FakeDriver) -> None:
        self.cursors: list[_FakeCursor] = []
        self.description = fake.description
        self.charsets = fake.charsets
        self.rows = fake.rows
        self.mode = fake.mode
        self.version = fake.version
        self.execute_error = fake.execute_error

    def cursor(self, cursor_class: object = None) -> _FakeCursor:
        cursor = _FakeCursor(self, cursor_class)
        self.cursors.append(cursor)
        return cursor

    def close(self) -> None:
        return None


class _FakeDriver:
    def __init__(self, **options: Any) -> None:
        self.description = options.get("description", (("OK",),))
        self.charsets = options.get("charsets", [])
        self.rows = options.get("rows", [(1,)])
        self.mode = options.get("mode", "ORACLE")
        self.version = options.get("version", "4.3.5.4")
        self.execute_error = options.get("execute_error")
        self.connect_kwargs: list[dict[str, Any]] = []
        self.connections: list[_FakeConnection] = []

    def connect(self, pymysql_module: Any, **kwargs: Any) -> _FakeConnection:
        self.connect_kwargs.append(kwargs)
        conn = _FakeConnection(self)
        self.connections.append(conn)
        return conn


def _install_fake_driver(monkeypatch: pytest.MonkeyPatch, **options: Any) -> _FakeDriver:
    fake = _FakeDriver(**options)
    monkeypatch.setattr(ob_driver, "connect", fake.connect)
    return fake
