"""Oracle adapter 单测(Preview:连接/DSN/流式/超时/探活/错误码反查/introspection)。

无真实 Oracle 实例(GA 决策 Oracle 进 2.0.x Preview):注入 fake oracledb 模块做
单测 + 契约测试。语义对照对象是同族 DM adapter(1.x DmDialect(OracleDialect))。
type_code 走 python-oracledb DbType(`.name` 形如 "DB_TYPE_NUMBER")的归一路径。
"""

from __future__ import annotations

import importlib
from typing import Any, cast

import pytest

from app.dbclients.oracle_adapter import (
    InvalidDatasourceError,
    OracleAdapter,
    OracleAdapterError,
    OracleDriverNotInstalledError,
    OracleUnsupportedOperationError,
    QueryCancelledError,
)
from app.dbclients.oracle_types import (
    data_type_string_to_column_type,
    description_item_to_column_type,
    description_type_to_driver_name,
)
from app.dbclients.protocol import AdapterConnectionError
from app.domain.datasource import DatasourceConnInfo, DbType
from app.domain.schema import Column, ColumnType, Index, Row, Schema, Table
from app.domain.secret import HashedRef, RotationReport, SecretKind, SecretRef
from app.infrastructure.secretstore.protocol import SecretStore


def test_requires_oracle_db_type() -> None:
    conn_info = _conn_info().model_copy(update={"db_type": DbType.MYSQL})
    with pytest.raises(InvalidDatasourceError):
        OracleAdapter(
            conn_info, cast(SecretStore, _SecretStore("pwd")), oracledb_module=_FakeOracleDb()
        )


def test_capabilities_declare_preview_scope() -> None:
    caps = _adapter(_FakeOracleDb()).capabilities
    assert caps.execute_select is True
    assert caps.stream_rows is True
    # thin Connection.cancel() 需另线程,当前软取消模型不接
    assert caps.server_side_cancel is False
    # introspection 复用 DM 验证过的 Oracle 目录视图 SQL,开
    assert caps.list_schemas is True
    assert caps.list_tables is True
    assert caps.list_columns is True
    assert caps.list_indexes is True
    # Preview 仍关:explain(PLAN_TABLE 依赖)/ DDL(DBMS_METADATA 权限)/ compare
    assert caps.explain is False
    assert caps.get_table_ddl is False
    assert caps.compare_db_hash is False


def test_kill_query_is_soft_cancel_false() -> None:
    assert _adapter(_FakeOracleDb()).kill_query("any-conn-id") is False


def test_unsupported_operations_raise_not_implemented() -> None:
    adapter = _adapter(_FakeOracleDb())
    for call in (
        lambda: adapter.explain("SELECT 1"),
        lambda: adapter.get_table_ddl("S", "T"),
    ):
        with pytest.raises(OracleUnsupportedOperationError) as excinfo:
            call()
        # 调用方按 capabilities 降级的契约形态:NotImplementedError 家族
        assert isinstance(excinfo.value, NotImplementedError)


def test_connect_builds_easy_connect_dsn_from_database_service() -> None:
    fake = _FakeOracleDb()
    secret_store = _SecretStore("s3cret")
    adapter = _adapter(fake, secret_store=secret_store)

    list(adapter.execute_select("SELECT 1 FROM dual", {}))

    kwargs = fake.connect_kwargs[0]
    # Easy Connect host:port/service_name;service_name 缺省取 database
    assert kwargs["dsn"] == "127.0.0.1:1521/FREEPDB1"
    assert kwargs["user"] == "dataops"
    assert kwargs["password"] == "s3cret"
    assert kwargs["tcp_connect_timeout"] == 10
    assert secret_store.revealed_refs == ["secret-1"]


def test_connect_dsn_uses_service_name_override() -> None:
    fake = _FakeOracleDb()
    conn_info = _conn_info().model_copy(update={"extra": {"service_name": "ORCLPDB1"}})
    adapter = OracleAdapter(conn_info, cast(SecretStore, _SecretStore("pwd")), oracledb_module=fake)

    adapter.test_connection()

    assert fake.connect_kwargs[0]["dsn"] == "127.0.0.1:1521/ORCLPDB1"


def test_connect_dsn_full_override() -> None:
    fake = _FakeOracleDb()
    conn_info = _conn_info().model_copy(update={"extra": {"dsn": "myhost:1600/CUSTOM.svc"}})
    adapter = OracleAdapter(conn_info, cast(SecretStore, _SecretStore("pwd")), oracledb_module=fake)

    adapter.test_connection()

    assert fake.connect_kwargs[0]["dsn"] == "myhost:1600/CUSTOM.svc"


def test_connect_timeout_respects_extra_override() -> None:
    fake = _FakeOracleDb()
    conn_info = _conn_info().model_copy(update={"extra": {"connect_timeout": 3}})
    adapter = OracleAdapter(conn_info, cast(SecretStore, _SecretStore("pwd")), oracledb_module=fake)

    adapter.test_connection()

    assert fake.connect_kwargs[0]["tcp_connect_timeout"] == 3


def test_execute_select_streams_across_fetchmany_batches_and_sets_arraysize() -> None:
    fake = _FakeOracleDb(default_rows=[(1,), (2,), (3,)])
    adapter = _adapter(fake, fetch_chunk_size=2)

    rows = list(adapter.execute_select("SELECT N FROM T", {}))

    assert rows == [Row(values=[1]), Row(values=[2]), Row(values=[3])]
    cursor = fake.connections[0].cursors[0]
    # 跨 fetchmany 批:2 + 1 + 空批终止;arraysize 对齐 chunk
    assert cursor.fetchmany_sizes == [2, 2, 2]
    assert cursor.arraysize == 2
    assert cursor.closed is True
    assert fake.connections[0].closed is True


def test_statement_timeout_sets_call_timeout_in_milliseconds() -> None:
    fake = _FakeOracleDb()
    adapter = _adapter(fake, statement_timeout_seconds=45)

    list(adapter.execute_select("SELECT 1 FROM dual", {}))

    # Oracle call_timeout 单位毫秒(45s → 45000ms),与 DM callTimeout 同
    assert fake.connections[0].call_timeout == 45000


def test_statement_timeout_zero_disables() -> None:
    fake = _FakeOracleDb()
    adapter = _adapter(fake, statement_timeout_seconds=0)

    list(adapter.execute_select("SELECT 1 FROM dual", {}))

    assert fake.connections[0].call_timeout is None


def test_statement_timeout_degrades_safely_when_set_fails() -> None:
    fake = _FakeOracleDb(call_timeout_raises=True)
    adapter = _adapter(fake, statement_timeout_seconds=45)

    rows = list(adapter.execute_select("SELECT 1 FROM dual", {}))

    assert rows == [Row(values=[1])]


def test_test_connection_pings_dual_and_records_version() -> None:
    fake = _FakeOracleDb()
    adapter = _adapter(fake)

    assert adapter.test_connection() is True
    executed = [cursor.executed_sql for cursor in fake.connections[0].cursors]
    assert any("from dual" in sql.lower() for sql in executed)
    assert adapter.last_server_version == "Oracle 19.0.0.0.0"


def test_test_connection_sanitizes_error_and_never_leaks_password() -> None:
    fake = _FakeOracleDb(
        connect_exc=RuntimeError(
            "ORA-01017 invalid credential password=top-secret "
            "dsn=oracle://user:top-secret@host:1521/svc"
        )
    )
    adapter = _adapter(fake, secret_store=_SecretStore("top-secret"))

    assert adapter.test_connection() is False
    assert adapter.last_connection_error is not None
    assert "top-secret" not in adapter.last_connection_error
    assert "***REDACTED***" in adapter.last_connection_error
    assert "<redacted-url>" in adapter.last_connection_error


def test_execute_error_enriched_with_ora_full_code() -> None:
    err = _make_db_error("ORA-00942", "table or view does not exist", 942)
    fake = _FakeOracleDb(execute_exc=err)
    adapter = _adapter(fake, secret_store=_SecretStore("top-secret"))

    with pytest.raises(OracleAdapterError) as excinfo:
        list(adapter.execute_select("SELECT 1 FROM missing_tbl", {}))

    message = str(excinfo.value)
    assert "ORA-00942" in message
    assert "Oracle execute failed" in message
    assert "top-secret" not in message


def test_fetch_error_enriched_and_cancel_not_swallowed() -> None:
    err = _make_db_error("ORA-03113", "end-of-file on communication channel", 3113)
    fake = _FakeOracleDb(default_rows=[(1,)], fetch_exc=err)
    adapter = _adapter(fake)

    with pytest.raises(OracleAdapterError) as excinfo:
        list(adapter.execute_select("SELECT 1 FROM dual", {}))

    assert "ORA-03113" in str(excinfo.value)
    assert fake.connections[0].closed is True


def test_missing_driver_error_guides_install(monkeypatch: pytest.MonkeyPatch) -> None:
    _block_oracledb_import(monkeypatch)
    adapter = _adapter(None)

    with pytest.raises(AdapterConnectionError) as excinfo:
        list(adapter.execute_select("SELECT 1 FROM dual", {}))

    cause = excinfo.value.__cause__
    assert isinstance(cause, OracleDriverNotInstalledError)
    assert "pip install oracledb" in str(cause)
    assert "thin mode" in str(cause)

    # test_connection 同路径:False + last_connection_error 带指引
    assert adapter.test_connection() is False
    assert adapter.last_connection_error is not None
    assert "pip install oracledb" in adapter.last_connection_error


def test_stream_select_preserves_connection_error_cause() -> None:
    original = RuntimeError("driver connect boom")
    adapter = _adapter(_FakeOracleDb(connect_exc=original))

    with pytest.raises(AdapterConnectionError) as excinfo:
        list(adapter.execute_select("SELECT 1 FROM dual", {}))

    assert excinfo.value.__cause__ is original


def test_soft_cancel_raises_on_safe_point() -> None:
    fake = _FakeOracleDb(default_rows=[(1,), (2,)])
    cancelled = {"flag": False}
    adapter = _adapter(fake, fetch_chunk_size=1, cancel_check=lambda: cancelled["flag"])

    iterator = adapter.execute_select("SELECT N FROM T", {})
    assert next(iterator) == Row(values=[1])
    cancelled["flag"] = True
    with pytest.raises(QueryCancelledError):
        next(iterator)
    # 软取消后连接仍被 finally 释放
    assert fake.connections[0].closed is True


def test_execute_select_emits_columns_with_scale_and_clean_driver_type() -> None:
    description = (
        _FetchInfo("ID", _FakeDbType("DB_TYPE_NUMBER"), scale=0, null_ok=False),
        _FetchInfo("AMOUNT", _FakeDbType("DB_TYPE_NUMBER"), scale=2, null_ok=True),
        _FetchInfo("NAME", _FakeDbType("DB_TYPE_VARCHAR"), scale=None, null_ok=True),
        _FetchInfo("CREATED_AT", _FakeDbType("DB_TYPE_TIMESTAMP_TZ"), scale=6, null_ok=True),
    )
    fake = _FakeOracleDb(default_rows=[(1, 1, "a", None)], default_description=description)
    captured: list[Column] = []
    adapter = _adapter(fake, column_sink=captured.extend)

    list(adapter.execute_select("SELECT ID, AMOUNT, NAME, CREATED_AT FROM T", {}))

    assert captured == [
        # NUMBER scale==0 → INTEGER;driver_type 去 DB_TYPE_ 前缀
        Column(name="ID", type=ColumnType.INTEGER, driver_type="NUMBER", nullable=False),
        # NUMBER scale>0 → DECIMAL
        Column(name="AMOUNT", type=ColumnType.DECIMAL, driver_type="NUMBER", nullable=True),
        Column(name="NAME", type=ColumnType.STRING, driver_type="VARCHAR", nullable=True),
        # TIMESTAMP_TZ 归一到 TIMESTAMP 桶 → DATETIME
        Column(name="CREATED_AT", type=ColumnType.DATETIME, driver_type="TIMESTAMP", nullable=True),
    ]


# ──────────────────── introspection ────────────────────


def test_list_schemas_filters_system_schemas() -> None:
    rows = [("APP",), ("HR",), ("SYS",), ("SYSTEM",), ("XDB",), ("DBSNMP",)]
    fake = _FakeOracleDb(default_rows=rows, default_description=(("name",),))
    adapter = _adapter(fake)

    schemas = adapter.list_schemas()

    assert schemas == [Schema(name="APP"), Schema(name="HR")]
    cursor = fake.connections[0].cursors[0]
    sql = " ".join(cursor.executed_sql.split())
    assert "FROM ALL_USERS" in sql
    assert cursor.executed_params is None


def test_list_tables_parameterized_union_of_tables_and_views() -> None:
    description = (("schema_name",), ("name",), ("table_type",))
    rows = [("APP", "USERS", "TABLE"), ("APP", "V_USERS", "VIEW")]
    fake = _FakeOracleDb(default_rows=rows, default_description=description)
    adapter = _adapter(fake)

    tables = adapter.list_tables("app")

    assert tables == [
        Table(schema_name="APP", name="USERS", table_type="TABLE"),
        Table(schema_name="APP", name="V_USERS", table_type="VIEW"),
    ]
    cursor = fake.connections[0].cursors[0]
    sql = " ".join(cursor.executed_sql.split())
    assert "FROM ALL_TABLES" in sql
    assert "FROM ALL_VIEWS" in sql
    # 命名绑定 + UPPER,不拼字面量
    assert "OWNER = UPPER(:owner)" in sql
    assert cursor.executed_params == {"owner": "app"}


def test_list_columns_maps_rows_with_pk_and_comment() -> None:
    fake = _FakeOracleDb(
        query_map=[
            _Route(
                "all_tab_columns",
                (("name",), ("data_type",), ("nullable",), ("comment",)),
                [
                    ("ID", "NUMBER", "N", None),
                    ("NAME", "VARCHAR2", "Y", "用户名"),
                    ("CREATED_AT", "TIMESTAMP(6)", "N", None),
                    ("PAYLOAD", "BLOB", "Y", None),
                ],
            ),
            _Route("all_constraints", (("name",),), [("ID",)]),
        ]
    )
    adapter = _adapter(fake)

    columns = adapter.list_columns("app", "users")

    assert columns == [
        Column(
            name="ID",
            type=ColumnType.DECIMAL,
            driver_type="NUMBER",
            nullable=False,
            primary_key=True,
        ),
        Column(
            name="NAME",
            type=ColumnType.STRING,
            driver_type="VARCHAR2",
            nullable=True,
            primary_key=False,
            comment="用户名",
        ),
        Column(
            name="CREATED_AT",
            type=ColumnType.DATETIME,
            driver_type="TIMESTAMP(6)",
            nullable=False,
            primary_key=False,
        ),
        Column(
            name="PAYLOAD",
            type=ColumnType.BYTES,
            driver_type="BLOB",
            nullable=True,
            primary_key=False,
        ),
    ]
    # 第一条连接跑 columns 查询,断言参数化 + UPPER
    columns_cursor = fake.connections[0].cursors[0]
    sql = " ".join(columns_cursor.executed_sql.split())
    assert "FROM ALL_TAB_COLUMNS c" in sql
    assert "c.OWNER = UPPER(:owner) AND c.TABLE_NAME = UPPER(:tbl)" in sql
    assert columns_cursor.executed_params == {"owner": "app", "tbl": "users"}


def test_list_indexes_groups_columns_and_parses_uniqueness() -> None:
    fake = _FakeOracleDb(
        query_map=[
            _Route(
                "all_indexes",
                (("name",), ("column_name",), ("uniqueness",), ("column_position",)),
                [
                    ("PK_USERS", "ID", "UNIQUE", 1),
                    ("UX_EMAIL", "EMAIL", "UNIQUE", 1),
                    ("IX_NAME_CREATED", "NAME", "NONUNIQUE", 1),
                    ("IX_NAME_CREATED", "CREATED_AT", "NONUNIQUE", 2),
                ],
            ),
            _Route("all_constraints", (("name",),), [("PK_USERS",)]),
        ]
    )
    adapter = _adapter(fake)

    indexes = adapter.list_indexes("app", "users")

    assert indexes == [
        Index(name="PK_USERS", columns=["ID"], is_unique=True, is_primary=True),
        Index(name="UX_EMAIL", columns=["EMAIL"], is_unique=True, is_primary=False),
        Index(
            name="IX_NAME_CREATED",
            columns=["NAME", "CREATED_AT"],
            is_unique=False,
            is_primary=False,
        ),
    ]
    cursor = fake.connections[0].cursors[0]
    sql = " ".join(cursor.executed_sql.split())
    assert "FROM ALL_INDEXES i" in sql
    assert "JOIN ALL_IND_COLUMNS ic" in sql
    assert "i.TABLE_OWNER = UPPER(:owner) AND i.TABLE_NAME = UPPER(:tbl)" in sql


def test_introspection_rejects_invalid_identifiers_before_connecting() -> None:
    fake = _FakeOracleDb()
    adapter = _adapter(fake)

    for call in (
        lambda: adapter.list_tables("bad schema"),
        lambda: adapter.list_columns("app", "users; DROP TABLE x"),
        lambda: adapter.list_indexes("app'--", "users"),
    ):
        with pytest.raises(ValueError):
            call()
    # 校验在连接之前,零连接开销
    assert fake.connections == []


def test_introspection_query_error_enriched_with_ora_code() -> None:
    err = _make_db_error("ORA-00942", "table or view does not exist", 942)
    fake = _FakeOracleDb(execute_exc=err)
    adapter = _adapter(fake)

    with pytest.raises(OracleAdapterError) as excinfo:
        adapter.list_schemas()

    assert "ORA-00942" in str(excinfo.value)
    assert fake.connections[0].closed is True


# ──────────────────── type mapping units ────────────────────


def test_description_type_mapping_units() -> None:
    # NUMBER scale 判别:scale==0 → INTEGER,scale>0/None → DECIMAL
    assert description_item_to_column_type(_FakeDbType("DB_TYPE_NUMBER"), 0) is ColumnType.INTEGER
    assert description_item_to_column_type(_FakeDbType("DB_TYPE_NUMBER"), 2) is ColumnType.DECIMAL
    assert (
        description_item_to_column_type(_FakeDbType("DB_TYPE_NUMBER"), None) is ColumnType.DECIMAL
    )
    # 其余类型与 scale 无关
    assert description_item_to_column_type(_FakeDbType("DB_TYPE_VARCHAR")) is ColumnType.STRING
    assert description_item_to_column_type(_FakeDbType("DB_TYPE_CLOB")) is ColumnType.STRING
    assert description_item_to_column_type(_FakeDbType("DB_TYPE_BINARY_DOUBLE")) is ColumnType.FLOAT
    assert description_item_to_column_type(_FakeDbType("DB_TYPE_DATE")) is ColumnType.DATE
    assert description_item_to_column_type(_FakeDbType("DB_TYPE_TIMESTAMP")) is ColumnType.DATETIME
    assert (
        description_item_to_column_type(_FakeDbType("DB_TYPE_TIMESTAMP_LTZ")) is ColumnType.DATETIME
    )
    assert description_item_to_column_type(_FakeDbType("DB_TYPE_RAW")) is ColumnType.BYTES
    assert description_item_to_column_type(_FakeDbType("DB_TYPE_BLOB")) is ColumnType.BYTES
    assert description_item_to_column_type(_FakeDbType("DB_TYPE_BOOLEAN")) is ColumnType.BOOLEAN
    assert description_item_to_column_type(_FakeDbType("DB_TYPE_JSON")) is ColumnType.JSON
    # 未知 / None 不臆造
    assert description_item_to_column_type(_FakeDbType("DB_TYPE_INTERVAL_YM")) is ColumnType.UNKNOWN
    assert description_item_to_column_type(None) is ColumnType.UNKNOWN
    # driver_type 去前缀 + 复合名归一
    assert description_type_to_driver_name(_FakeDbType("DB_TYPE_VARCHAR")) == "VARCHAR"
    assert description_type_to_driver_name(_FakeDbType("DB_TYPE_TIMESTAMP_TZ")) == "TIMESTAMP"
    assert description_type_to_driver_name(_FakeDbType("DB_TYPE_LONG_RAW")) == "RAW"
    assert description_type_to_driver_name(object()) is None
    assert description_type_to_driver_name(None) is None


def test_data_type_string_mapping_units() -> None:
    cases: dict[str, ColumnType] = {
        "NUMBER": ColumnType.DECIMAL,
        "NUMBER(10,2)": ColumnType.DECIMAL,
        "FLOAT": ColumnType.FLOAT,
        "BINARY_FLOAT": ColumnType.FLOAT,
        "BINARY_DOUBLE": ColumnType.FLOAT,
        "VARCHAR2": ColumnType.STRING,
        "NVARCHAR2": ColumnType.STRING,
        "CHAR": ColumnType.STRING,
        "NCHAR": ColumnType.STRING,
        "CLOB": ColumnType.STRING,
        "NCLOB": ColumnType.STRING,
        "LONG": ColumnType.STRING,
        "ROWID": ColumnType.STRING,
        "DATE": ColumnType.DATE,
        "TIMESTAMP(6)": ColumnType.DATETIME,
        "TIMESTAMP(6) WITH TIME ZONE": ColumnType.DATETIME,
        "RAW": ColumnType.BYTES,
        "LONG RAW": ColumnType.BYTES,  # 前缀 "LONG" 会误判 STRING → 前置特判归 BYTES
        "BLOB": ColumnType.BYTES,
        "BFILE": ColumnType.BYTES,
        "JSON": ColumnType.JSON,
    }
    for raw, expected in cases.items():
        assert data_type_string_to_column_type(raw) is expected, raw
    # 未知类型 / 非字符串不臆造
    assert data_type_string_to_column_type("INTERVAL YEAR TO MONTH") is ColumnType.UNKNOWN
    assert data_type_string_to_column_type("SDO_GEOMETRY") is ColumnType.UNKNOWN
    assert data_type_string_to_column_type(None) is ColumnType.UNKNOWN


# ──────────────────────────── helpers / fakes ────────────────────────────


def _conn_info() -> DatasourceConnInfo:
    return DatasourceConnInfo(
        host="127.0.0.1",
        port=1521,
        username="dataops",
        database="FREEPDB1",
        password_ref=SecretRef(ref="secret-1", kind=SecretKind.DATASOURCE_PASSWORD),
        db_type=DbType.ORACLE,
    )


def _adapter(
    fake: _FakeOracleDb | None,
    *,
    secret_store: _SecretStore | None = None,
    **kwargs: Any,
) -> OracleAdapter:
    return OracleAdapter(
        _conn_info(),
        cast(SecretStore, secret_store or _SecretStore("pwd")),
        oracledb_module=fake,
        **kwargs,
    )


def _block_oracledb_import(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import_module = importlib.import_module

    def _fake_import(name: str, package: str | None = None) -> Any:
        if name == "oracledb":
            raise ImportError("No module named 'oracledb'")
        return real_import_module(name, package)

    monkeypatch.setattr(importlib, "import_module", _fake_import)


def _make_db_error(full_code: str, message: str, code: int) -> Exception:
    """构造 python-oracledb.DatabaseError 形态:args[0] 是带 full_code/code 的 _Error。"""

    class _OraError:
        def __init__(self) -> None:
            self.full_code = full_code
            self.message = message
            self.code = code

    class _FakeDatabaseError(Exception):
        pass

    return _FakeDatabaseError(_OraError())


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


class _FakeDbType:
    """python-oracledb DbType 形态:.name = "DB_TYPE_NUMBER" 等。"""

    def __init__(self, name: str) -> None:
        self.name = name


class _FetchInfo:
    """python-oracledb FetchInfo 形态(属性访问 .name/.type_code/.scale/.null_ok)。"""

    def __init__(self, name: str, type_code: object, *, scale: object, null_ok: bool) -> None:
        self.name = name
        self.type_code = type_code
        self.scale = scale
        self.null_ok = null_ok


class _Route:
    """按 SQL 子串路由到指定 (description, rows)。"""

    def __init__(
        self,
        needle: str,
        description: tuple[Any, ...],
        rows: list[tuple[Any, ...]],
    ) -> None:
        self.needle = needle
        self.description = description
        self.rows = rows


class _FakeOracleDb:
    """fake oracledb 模块:connect(**kwargs) → thin connection。"""

    def __init__(
        self,
        *,
        default_rows: list[tuple[Any, ...]] | None = None,
        default_description: tuple[Any, ...] | None = None,
        query_map: list[_Route] | None = None,
        connect_exc: Exception | None = None,
        execute_exc: Exception | None = None,
        fetch_exc: Exception | None = None,
        call_timeout_raises: bool = False,
    ) -> None:
        self.connect_kwargs: list[dict[str, Any]] = []
        self.connections: list[_FakeOracleConnection] = []
        self._default_rows = default_rows if default_rows is not None else [(1,)]
        self._default_description = default_description
        self._query_map = query_map or []
        self._connect_exc = connect_exc
        self._execute_exc = execute_exc
        self._fetch_exc = fetch_exc
        self._call_timeout_raises = call_timeout_raises

    def connect(self, **kwargs: Any) -> _FakeOracleConnection:
        if self._connect_exc is not None:
            raise self._connect_exc
        self.connect_kwargs.append(dict(kwargs))
        conn = _FakeOracleConnection(
            default_rows=self._default_rows,
            default_description=self._default_description,
            query_map=self._query_map,
            execute_exc=self._execute_exc,
            fetch_exc=self._fetch_exc,
            call_timeout_raises=self._call_timeout_raises,
        )
        self.connections.append(conn)
        return conn


class _FakeOracleConnection:
    def __init__(
        self,
        *,
        default_rows: list[tuple[Any, ...]],
        default_description: tuple[Any, ...] | None,
        query_map: list[_Route],
        execute_exc: Exception | None,
        fetch_exc: Exception | None,
        call_timeout_raises: bool,
    ) -> None:
        self.closed = False
        self.cursors: list[_FakeOracleCursor] = []
        self._call_timeout: int | None = None
        self._default_rows = default_rows
        self._default_description = default_description
        self._query_map = query_map
        self._execute_exc = execute_exc
        self._fetch_exc = fetch_exc
        self._call_timeout_raises = call_timeout_raises

    @property
    def call_timeout(self) -> int | None:
        return self._call_timeout

    @call_timeout.setter
    def call_timeout(self, value: int) -> None:
        if self._call_timeout_raises:
            raise RuntimeError("call_timeout not supported")
        self._call_timeout = value

    def cursor(self) -> _FakeOracleCursor:
        cursor = _FakeOracleCursor(
            default_rows=self._default_rows,
            default_description=self._default_description,
            query_map=self._query_map,
            execute_exc=self._execute_exc,
            fetch_exc=self._fetch_exc,
        )
        self.cursors.append(cursor)
        return cursor

    def close(self) -> None:
        self.closed = True


class _FakeOracleCursor:
    def __init__(
        self,
        *,
        default_rows: list[tuple[Any, ...]],
        default_description: tuple[Any, ...] | None,
        query_map: list[_Route],
        execute_exc: Exception | None,
        fetch_exc: Exception | None,
    ) -> None:
        self.closed = False
        self.arraysize = 100
        self.executed_sql = ""
        self.executed_params: object = None
        self.fetchmany_sizes: list[int] = []
        self.description: tuple[Any, ...] = (("OK",),)
        self._default_rows = default_rows
        self._default_description = default_description
        self._query_map = query_map
        self._execute_exc = execute_exc
        self._fetch_exc = fetch_exc
        self._rows: list[tuple[Any, ...]] = []
        self._offset = 0

    def execute(self, sql: str, params: object = None) -> None:
        self.executed_sql = sql
        self.executed_params = params
        if self._execute_exc is not None:
            raise self._execute_exc
        normalized = " ".join(sql.lower().split())
        # product_component_version 探活(test_connection 版本查询)
        if "product_component_version" in normalized:
            self.description = (("version",),)
            self._rows = [("19.0.0.0.0",)]
            self._offset = 0
            return
        for route in self._query_map:
            if route.needle in normalized:
                self.description = route.description
                self._rows = list(route.rows)
                self._offset = 0
                return
        self._rows = list(self._default_rows)
        if self._default_description is not None:
            self.description = self._default_description
        else:
            self.description = tuple(("OK",) for _ in (self._rows[0] if self._rows else (1,)))
        self._offset = 0

    def fetchone(self) -> tuple[Any, ...] | None:
        rows = self.fetchmany(1)
        return rows[0] if rows else None

    def fetchmany(self, size: int) -> list[tuple[Any, ...]]:
        if self._fetch_exc is not None:
            raise self._fetch_exc
        self.fetchmany_sizes.append(size)
        batch = self._rows[self._offset : self._offset + size]
        self._offset += len(batch)
        return batch

    def fetchall(self) -> list[tuple[Any, ...]]:
        rows = self._rows[self._offset :]
        self._offset = len(self._rows)
        return rows

    def close(self) -> None:
        self.closed = True
