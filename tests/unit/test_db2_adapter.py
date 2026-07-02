"""DB2 adapter 单测(PR-A:连接/流式/超时/探活/错误反查/DLL 幂等)。

DB2 无可用的 GitHub Actions service container(ibm_db + clidriver 体积大且
license 受限),与 DM 同款做法:fake ibm_db_dbi / ibm_db 模块注入做单测 +
契约测试,不依赖真 DB2 实例。语义对照对象是 1.x 现场跑过的 Db2Dialect。
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Any, cast

import pytest

from app.dbclients.db2_adapter import (
    Db2Adapter,
    Db2AdapterError,
    Db2DriverNotInstalledError,
    Db2UnsupportedOperationError,
    InvalidDatasourceError,
    QueryCancelledError,
    add_db2_dll_directories,
)
from app.dbclients.db2_types import (
    description_type_to_column_type,
    description_type_to_driver_name,
    syscat_typename_to_column_type,
    syscat_typename_to_driver_name,
)
from app.dbclients.protocol import AdapterConnectionError
from app.domain.datasource import DatasourceConnInfo, DbType
from app.domain.schema import Column, ColumnType, Index, Row, Schema, Table
from app.domain.secret import HashedRef, RotationReport, SecretKind, SecretRef
from app.infrastructure.secretstore.protocol import SecretStore


def test_requires_db2_db_type() -> None:
    conn_info = _conn_info().model_copy(update={"db_type": DbType.MYSQL})
    with pytest.raises(InvalidDatasourceError):
        Db2Adapter(
            conn_info,
            cast(SecretStore, _SecretStore("pwd")),
            ibm_db_dbi_module=_FakeIbmDbDbi(),
            ibm_db_module=_FakeIbmDb(),
        )


def test_capabilities_declare_preview_scope() -> None:
    caps = _adapter(_FakeIbmDbDbi(), _FakeIbmDb()).capabilities
    assert caps.execute_select is True
    assert caps.stream_rows is True
    # ibm_db 无 driver-level cancel(V1_AS_IS §2.8)
    assert caps.server_side_cancel is False
    # PR-B 开了 SYSCAT introspection 全套
    assert caps.list_schemas is True
    assert caps.list_tables is True
    assert caps.list_columns is True
    assert caps.list_indexes is True
    # 仍占位:explain(EXPLAIN tables 依赖)/ DDL(无稳妥只读途径)/ compare(PR-C)
    assert caps.explain is False
    assert caps.get_table_ddl is False
    assert caps.compare_db_hash is False


def test_kill_query_is_soft_cancel_false() -> None:
    assert _adapter(_FakeIbmDbDbi(), _FakeIbmDb()).kill_query("any-conn-id") is False


def test_unsupported_operations_raise_not_implemented() -> None:
    adapter = _adapter(_FakeIbmDbDbi(), _FakeIbmDb())
    for call in (
        lambda: adapter.explain("SELECT 1"),
        lambda: adapter.get_table_ddl("S", "T"),
    ):
        with pytest.raises(Db2UnsupportedOperationError) as excinfo:
            call()
        # 调用方按 capabilities 降级的契约形态:NotImplementedError 家族
        assert isinstance(excinfo.value, NotImplementedError)


def test_connect_builds_conn_str_with_connecttimeout() -> None:
    fake_dbi = _FakeIbmDbDbi()
    secret_store = _SecretStore("s3cret")
    adapter = _adapter(fake_dbi, _FakeIbmDb(), secret_store=secret_store)

    list(adapter.execute_select("SELECT 1 FROM SYSIBM.SYSDUMMY1", {}))

    # 1.x db2.py:87-102 同款连接串 + 自动补 CONNECTTIMEOUT(秒)
    assert fake_dbi.conn_strs == [
        "DATABASE=SAMPLE;HOSTNAME=127.0.0.1;PORT=50000;PROTOCOL=TCPIP;"
        "UID=dataops;PWD=s3cret;CONNECTTIMEOUT=10;"
    ]
    # 最终始终走 ibm_db_dbi 的 DB-API 形态:connect(conn_str, "", "")
    assert fake_dbi.connect_args == [("", "")]
    assert secret_store.revealed_refs == ["secret-1"]


def test_connect_timeout_respects_extra_override() -> None:
    fake_dbi = _FakeIbmDbDbi()
    conn_info = _conn_info().model_copy(update={"extra": {"connect_timeout": 3}})
    adapter = Db2Adapter(
        conn_info,
        cast(SecretStore, _SecretStore("pwd")),
        ibm_db_dbi_module=fake_dbi,
        ibm_db_module=_FakeIbmDb(),
    )

    adapter.test_connection()

    assert fake_dbi.conn_strs[0].endswith("CONNECTTIMEOUT=3;")


def test_execute_select_streams_across_fetchmany_batches_and_closes_resources() -> None:
    fake_dbi = _FakeIbmDbDbi(rows=[(1,), (2,), (3,)])
    adapter = _adapter(fake_dbi, _FakeIbmDb(), fetch_chunk_size=2)

    rows = list(adapter.execute_select("SELECT N FROM T", {}))

    assert rows == [Row(values=[1]), Row(values=[2]), Row(values=[3])]
    cursor = fake_dbi.connections[0].cursors[0]
    # 跨 fetchmany 批:2 + 1 + 空批终止
    assert cursor.fetchmany_sizes == [2, 2, 2]
    assert cursor.closed is True
    assert fake_dbi.connections[0].closed is True


def test_statement_timeout_uses_ibm_db_set_option_in_seconds() -> None:
    fake_dbi = _FakeIbmDbDbi()
    fake_ibm_db = _FakeIbmDb()
    adapter = _adapter(fake_dbi, fake_ibm_db, statement_timeout_seconds=45)

    list(adapter.execute_select("SELECT 1 FROM SYSIBM.SYSDUMMY1", {}))

    # 1.x db2.py:62-85 同款调用形状:连接级(scope=1)+ 单位秒(45,不是 45000)
    handle, options, scope = fake_ibm_db.set_option_calls[0]
    assert handle is fake_dbi.connections[0].conn_handler
    assert options == {_FakeIbmDb.SQL_ATTR_QUERY_TIMEOUT: 45}
    assert scope == 1


def test_statement_timeout_handle_falls_back_to_conn_handle_attr() -> None:
    # 新版本 ibm_db_dbi 暴露 .conn_handle(老版本 .conn_handler),两个名都要认
    fake_dbi = _FakeIbmDbDbi(handle_attr="conn_handle")
    fake_ibm_db = _FakeIbmDb()
    adapter = _adapter(fake_dbi, fake_ibm_db, statement_timeout_seconds=7)

    list(adapter.execute_select("SELECT 1 FROM SYSIBM.SYSDUMMY1", {}))

    handle, _options, _scope = fake_ibm_db.set_option_calls[0]
    assert handle is fake_dbi.connections[0].conn_handle


def test_statement_timeout_degrades_safely_when_set_option_fails() -> None:
    fake_ibm_db = _FakeIbmDb(set_option_exc=RuntimeError("boom"))
    adapter = _adapter(_FakeIbmDbDbi(), fake_ibm_db, statement_timeout_seconds=45)

    rows = list(adapter.execute_select("SELECT 1 FROM SYSIBM.SYSDUMMY1", {}))

    assert rows == [Row(values=[1])]


def test_statement_timeout_degrades_safely_without_ibm_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ibm_db import 失败 → 安全降级为"不超时"(1.x 同语义),查询主路径不受影响
    _block_ibm_db_imports(monkeypatch, block_dbi=False)
    adapter = _adapter(_FakeIbmDbDbi(), None, statement_timeout_seconds=45)

    rows = list(adapter.execute_select("SELECT 1 FROM SYSIBM.SYSDUMMY1", {}))

    assert rows == [Row(values=[1])]


def test_test_connection_pings_sysdummy1_and_records_version() -> None:
    fake_dbi = _FakeIbmDbDbi()
    adapter = _adapter(fake_dbi, _FakeIbmDb())

    assert adapter.test_connection() is True
    executed = [cursor.executed_sql for cursor in fake_dbi.connections[0].cursors]
    assert "select 1 as ok from sysibm.sysdummy1" in executed
    assert adapter.last_server_version == "DB2 11.5.9"


def test_test_connection_sanitizes_error_and_never_leaks_password() -> None:
    fake_dbi = _FakeIbmDbDbi(
        connect_exc=RuntimeError(
            "SQL30082N connect failed PWD=top-secret db2://user:top-secret@host:50000/SAMPLE"
        )
    )
    adapter = _adapter(fake_dbi, _FakeIbmDb(), secret_store=_SecretStore("top-secret"))

    assert adapter.test_connection() is False
    assert adapter.last_connection_error is not None
    assert "top-secret" not in adapter.last_connection_error
    assert "***REDACTED***" in adapter.last_connection_error
    assert "<redacted-url>" in adapter.last_connection_error


def test_driver_error_enriched_with_stmt_and_conn_errormsg() -> None:
    # ibm_db 报错时 Python 层只见 generic 描述,必须反查 stmt_errormsg/conn_errormsg
    # 拿真 SQLCODE(1.x factory.py:503-547 实战修过的坑)
    fake_dbi = _FakeIbmDbDbi(execute_exc=RuntimeError("returned a result with an exception set"))
    fake_ibm_db = _FakeIbmDb(
        stmt_errormsg='SQLCODE=-204 SQLSTATE=42704 "APP.NO_SUCH_TABLE" is undefined',
        conn_errormsg="SQLCODE=-30081",
    )
    adapter = _adapter(fake_dbi, fake_ibm_db, secret_store=_SecretStore("top-secret"))

    with pytest.raises(Db2AdapterError) as excinfo:
        list(adapter.execute_select("SELECT 1 FROM SYSIBM.SYSDUMMY1", {}))

    message = str(excinfo.value)
    assert "SQLCODE=-204" in message
    assert "SQLCODE=-30081" in message
    assert "returned a result with an exception set" in message
    assert "top-secret" not in message


def test_fetch_error_also_enriched_and_cancel_not_swallowed() -> None:
    fake_dbi = _FakeIbmDbDbi(fetch_exc=RuntimeError("fetchmany exception set"))
    fake_ibm_db = _FakeIbmDb(stmt_errormsg="SQLCODE=-1224")
    adapter = _adapter(fake_dbi, fake_ibm_db)

    with pytest.raises(Db2AdapterError) as excinfo:
        list(adapter.execute_select("SELECT 1 FROM SYSIBM.SYSDUMMY1", {}))

    assert "SQLCODE=-1224" in str(excinfo.value)
    assert fake_dbi.connections[0].closed is True


def test_missing_driver_error_guides_install(monkeypatch: pytest.MonkeyPatch) -> None:
    _block_ibm_db_imports(monkeypatch, block_dbi=True)
    adapter = _adapter(None, None)

    with pytest.raises(AdapterConnectionError) as excinfo:
        list(adapter.execute_select("SELECT 1 FROM SYSIBM.SYSDUMMY1", {}))

    cause = excinfo.value.__cause__
    assert isinstance(cause, Db2DriverNotInstalledError)
    assert "pip install ibm_db" in str(cause)
    assert "clidriver" in str(cause)

    # test_connection 同路径:False + last_connection_error 带指引
    assert adapter.test_connection() is False
    assert adapter.last_connection_error is not None
    assert "pip install ibm_db" in adapter.last_connection_error


def test_stream_select_preserves_connection_error_cause() -> None:
    original = RuntimeError("driver connect boom")
    adapter = _adapter(_FakeIbmDbDbi(connect_exc=original), _FakeIbmDb())

    with pytest.raises(AdapterConnectionError) as excinfo:
        list(adapter.execute_select("SELECT 1 FROM SYSIBM.SYSDUMMY1", {}))

    assert excinfo.value.__cause__ is original


def test_soft_cancel_raises_on_safe_point() -> None:
    fake_dbi = _FakeIbmDbDbi(rows=[(1,), (2,)])
    cancelled = {"flag": False}
    adapter = _adapter(
        fake_dbi,
        _FakeIbmDb(),
        fetch_chunk_size=1,
        cancel_check=lambda: cancelled["flag"],
    )

    iterator = adapter.execute_select("SELECT N FROM T", {})
    assert next(iterator) == Row(values=[1])
    cancelled["flag"] = True
    with pytest.raises(QueryCancelledError):
        next(iterator)
    # 软取消后连接仍被 finally 释放
    assert fake_dbi.connections[0].closed is True


def test_execute_select_emits_columns_with_clean_driver_type() -> None:
    description = (
        ("ID", "int", None, None, None, None, 0),
        ("NAME", frozenset({"CHAR", "VARCHAR", "CLOB"}), None, None, None, None, 1),
    )
    fake_dbi = _FakeIbmDbDbi(rows=[(1, "a")], description=description)
    captured: list[Column] = []
    adapter = _adapter(fake_dbi, _FakeIbmDb(), column_sink=captured.extend)

    list(adapter.execute_select("SELECT ID, NAME FROM T", {}))

    assert captured == [
        Column(name="ID", type=ColumnType.INTEGER, driver_type="INT", nullable=False),
        # 类型名集合取映射表最先命中的干净名(CLOB 在 CHAR 前),确定性即可
        Column(name="NAME", type=ColumnType.STRING, driver_type="CLOB", nullable=True),
    ]
    # driver_type 必须是干净类型名,不许透传 frozenset/type-object 的 repr
    assert all("frozenset" not in (c.driver_type or "") for c in captured)


def test_description_type_mapping_units() -> None:
    # 字符串形态(ibm_db.field_type 小写直出)
    assert description_type_to_column_type("string") is ColumnType.STRING
    assert description_type_to_column_type("bigint") is ColumnType.INTEGER
    assert description_type_to_column_type("decimal") is ColumnType.DECIMAL
    assert description_type_to_column_type("decfloat") is ColumnType.DECIMAL
    assert description_type_to_column_type("real") is ColumnType.FLOAT
    assert description_type_to_column_type("timestamp") is ColumnType.DATETIME
    assert description_type_to_column_type("date") is ColumnType.DATE
    assert description_type_to_column_type("time") is ColumnType.TIME
    assert description_type_to_column_type("blob") is ColumnType.BYTES
    assert description_type_to_column_type("xml") is ColumnType.STRING
    assert description_type_to_column_type("weird") is ColumnType.UNKNOWN
    assert description_type_to_column_type(None) is ColumnType.UNKNOWN
    # type-object 形态(frozenset 子类):歧义数值集合保守归 DECIMAL,
    # 时间集合保守归 DATETIME
    number = frozenset({"SMALLINT", "INTEGER", "BIGINT", "DECIMAL", "REAL"})
    assert description_type_to_column_type(number) is ColumnType.DECIMAL
    datetime_set = frozenset({"DATE", "TIME", "TIMESTAMP"})
    assert description_type_to_column_type(datetime_set) is ColumnType.DATETIME
    # driver_type 干净名
    assert description_type_to_driver_name("VARCHAR(64)") == "VARCHAR"
    assert description_type_to_driver_name(number) == "DECIMAL"
    assert description_type_to_driver_name(object()) is None


# ──────────────────── PR-B:SYSCAT introspection ────────────────────


def test_list_schemas_uses_syscat_and_filters_system_prefixes() -> None:
    rows = [
        ("APPDATA ",),  # CHAR 定长右补空格 → strip
        ("DB2GSE",),
        ("ERRSCHEMA",),
        ("NULLID",),
        ("SQLJ",),
        ("ST_INFORMTN_SCHEMA",),
        ("SYSCAT",),
        ("SYSIBM",),
    ]
    fake_dbi = _FakeIbmDbDbi(rows=rows, description=(("NAME",),))
    adapter = _adapter(fake_dbi, _FakeIbmDb())

    schemas = adapter.list_schemas()

    # 1.x metadata.py:37 实证前缀清单:SYS/DB2/ERR/NULLID/SQLJ/ST_ 全排除
    assert schemas == [Schema(name="APPDATA")]
    cursor = fake_dbi.connections[0].cursors[0]
    sql = " ".join(cursor.executed_sql.split())
    assert "FROM SYSCAT.SCHEMATA" in sql
    assert "ORDER BY SCHEMANAME" in sql
    assert cursor.executed_params is None
    assert cursor.closed is True
    assert fake_dbi.connections[0].closed is True


def test_list_tables_parameterized_upper_and_maps_type_labels() -> None:
    description = (("SCHEMA_NAME",), ("NAME",), ("TABLE_TYPE",))
    rows = [("APPDATA", "USERS", "T"), ("APPDATA", "V_USERS", "V")]
    fake_dbi = _FakeIbmDbDbi(rows=rows, description=description)
    adapter = _adapter(fake_dbi, _FakeIbmDb())

    tables = adapter.list_tables("appdata")

    assert tables == [
        Table(schema_name="APPDATA", name="USERS", table_type="TABLE"),
        Table(schema_name="APPDATA", name="V_USERS", table_type="VIEW"),
    ]
    cursor = fake_dbi.connections[0].cursors[0]
    sql = " ".join(cursor.executed_sql.split())
    assert "FROM SYSCAT.TABLES" in sql
    # 参数化 + UPPER,不字符串拼接用户输入;与 DM 一致连视图一起列
    assert "TABSCHEMA = UPPER(?)" in sql
    assert "TYPE IN ('T', 'V')" in sql
    assert cursor.executed_params == ("appdata",)


def test_list_columns_maps_syscat_rows_to_domain_columns() -> None:
    description = (("NAME",), ("DATA_TYPE",), ("NULLABLE",), ("KEYSEQ",), ("COMMENT",))
    rows = [
        ("ID", "BIGINT", "N", 1, None),
        ("NAME", "VARCHAR", "Y", None, "用户名"),
        ("CREATED_AT", "TIMESTAMP", "N", 0, None),  # KEYSEQ=0 也算非主键
        ("PAYLOAD", "BLOB", "Y", None, None),
    ]
    fake_dbi = _FakeIbmDbDbi(rows=rows, description=description)
    adapter = _adapter(fake_dbi, _FakeIbmDb())

    columns = adapter.list_columns("appdata", "users")

    assert columns == [
        Column(
            name="ID",
            type=ColumnType.INTEGER,
            driver_type="BIGINT",
            nullable=False,
            primary_key=True,
        ),
        Column(
            name="NAME",
            type=ColumnType.STRING,
            driver_type="VARCHAR",
            nullable=True,
            primary_key=False,
            comment="用户名",
        ),
        Column(
            name="CREATED_AT",
            type=ColumnType.DATETIME,
            driver_type="TIMESTAMP",
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
    cursor = fake_dbi.connections[0].cursors[0]
    sql = " ".join(cursor.executed_sql.split())
    assert "FROM SYSCAT.COLUMNS" in sql
    assert "TABSCHEMA = UPPER(?) AND TABNAME = UPPER(?)" in sql
    assert "ORDER BY COLNO" in sql
    assert cursor.executed_params == ("appdata", "users")


def test_list_indexes_groups_columns_and_parses_uniquerule() -> None:
    description = (("NAME",), ("UNIQUERULE",), ("COLUMN_NAME",), ("COLUMN_POSITION",))
    rows = [
        ("PK_USERS", "P", "ID", 1),
        ("UX_EMAIL", "U", "EMAIL", 1),
        ("IX_NAME_CREATED", "D", "NAME", 1),
        ("IX_NAME_CREATED", "D", "CREATED_AT", 2),
    ]
    fake_dbi = _FakeIbmDbDbi(rows=rows, description=description)
    adapter = _adapter(fake_dbi, _FakeIbmDb())

    indexes = adapter.list_indexes("appdata", "users")

    # UNIQUERULE:'P' 主键(也算 unique)/ 'U' unique / 'D' 普通
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
    cursor = fake_dbi.connections[0].cursors[0]
    sql = " ".join(cursor.executed_sql.split())
    assert "FROM SYSCAT.INDEXES i" in sql
    assert "JOIN SYSCAT.INDEXCOLUSE c" in sql
    # INCLUDE 列(COLORDER='I')不是 key 列,必须被 SQL 侧排除
    assert "AND c.COLORDER <> 'I'" in sql
    assert "i.TABSCHEMA = UPPER(?) AND i.TABNAME = UPPER(?)" in sql
    assert "ORDER BY i.INDNAME, c.COLSEQ" in sql
    assert cursor.executed_params == ("appdata", "users")


def test_introspection_rejects_invalid_identifiers_before_connecting() -> None:
    fake_dbi = _FakeIbmDbDbi()
    adapter = _adapter(fake_dbi, _FakeIbmDb())

    for call in (
        lambda: adapter.list_tables("bad schema"),
        lambda: adapter.list_columns("app", "users; DROP TABLE x"),
        lambda: adapter.list_indexes("app'--", "users"),
    ):
        with pytest.raises(ValueError):
            call()
    # 校验在连接之前,零连接开销
    assert fake_dbi.connections == []


def test_introspection_query_error_enriched_with_sqlcode() -> None:
    fake_dbi = _FakeIbmDbDbi(execute_exc=RuntimeError("exception set"))
    fake_ibm_db = _FakeIbmDb(stmt_errormsg="SQLCODE=-551 no SELECT privilege on SYSCAT.SCHEMATA")
    adapter = _adapter(fake_dbi, fake_ibm_db)

    with pytest.raises(Db2AdapterError) as excinfo:
        adapter.list_schemas()

    assert "SQLCODE=-551" in str(excinfo.value)
    assert fake_dbi.connections[0].closed is True


def test_syscat_typename_mapping_full_sweep() -> None:
    cases: dict[str, ColumnType] = {
        # 字符串系(含 GRAPHIC 家族与大对象)
        "CHAR": ColumnType.STRING,
        "CHARACTER": ColumnType.STRING,
        "VARCHAR": ColumnType.STRING,
        "LONG VARCHAR": ColumnType.STRING,
        "CLOB": ColumnType.STRING,
        "GRAPHIC": ColumnType.STRING,
        "VARGRAPHIC": ColumnType.STRING,
        "LONG VARGRAPHIC": ColumnType.STRING,
        "DBCLOB": ColumnType.STRING,
        "XML": ColumnType.STRING,
        # 数值系
        "SMALLINT": ColumnType.INTEGER,
        "INTEGER": ColumnType.INTEGER,
        "BIGINT": ColumnType.INTEGER,
        "DECIMAL": ColumnType.DECIMAL,
        "NUMERIC": ColumnType.DECIMAL,
        "DECFLOAT": ColumnType.DECIMAL,
        "REAL": ColumnType.FLOAT,
        "FLOAT": ColumnType.FLOAT,
        "DOUBLE": ColumnType.FLOAT,
        "DOUBLE PRECISION": ColumnType.FLOAT,
        # 布尔 / 时间 / 二进制
        "BOOLEAN": ColumnType.BOOLEAN,
        "DATE": ColumnType.DATE,
        "TIME": ColumnType.TIME,
        "TIMESTAMP": ColumnType.DATETIME,
        "BLOB": ColumnType.BYTES,
        "BINARY": ColumnType.BYTES,
        "VARBINARY": ColumnType.BYTES,
    }
    for typename, expected in cases.items():
        assert syscat_typename_to_column_type(typename) is expected, typename
    # CHAR 定长右补空格 / 小写输入统一归一
    assert syscat_typename_to_column_type("VARCHAR   ") is ColumnType.STRING
    assert syscat_typename_to_column_type("timestamp") is ColumnType.DATETIME
    # 带修饰的复合名按首词回退
    assert syscat_typename_to_column_type("TIMESTAMP WITH TIME ZONE") is ColumnType.DATETIME
    # distinct UDT / 未知类型不臆造
    assert syscat_typename_to_column_type("MY_MONEY_TYPE") is ColumnType.UNKNOWN
    assert syscat_typename_to_column_type(None) is ColumnType.UNKNOWN
    # driver_type 干净大写名(空白折叠),拿不到返回 None
    assert syscat_typename_to_driver_name(" long  varchar ") == "LONG VARCHAR"
    assert syscat_typename_to_driver_name("VARCHAR ") == "VARCHAR"
    assert syscat_typename_to_driver_name(None) is None
    assert syscat_typename_to_driver_name("   ") is None


def test_add_db2_dll_directories_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # 1.x 踩过的坑:每次连接前缀 PATH 不去重,撑爆 Windows 32767 上限。
    # 反复调用后 PATH 中该目录必须只出现一次(大小写不敏感)。
    bin_dir = tmp_path / "clidriver" / "bin"
    bin_dir.mkdir(parents=True)
    monkeypatch.setenv("IBM_DB_HOME", str(tmp_path / "clidriver"))
    monkeypatch.setenv("PATH", r"C:\Windows\system32")

    first = add_db2_dll_directories()
    second = add_db2_dll_directories()

    assert str(bin_dir) in first
    assert str(bin_dir) in second
    occurrences = [
        part
        for part in os.environ["PATH"].split(os.pathsep)
        if part.lower() == str(bin_dir).lower()
    ]
    assert len(occurrences) == 1


# ──────────────────────────── helpers / fakes ────────────────────────────


def _conn_info() -> DatasourceConnInfo:
    return DatasourceConnInfo(
        host="127.0.0.1",
        port=50000,
        username="dataops",
        database="SAMPLE",
        password_ref=SecretRef(ref="secret-1", kind=SecretKind.DATASOURCE_PASSWORD),
        db_type=DbType.DB2,
    )


def _adapter(
    fake_dbi: _FakeIbmDbDbi | None,
    fake_ibm_db: _FakeIbmDb | None,
    *,
    secret_store: _SecretStore | None = None,
    **kwargs: Any,
) -> Db2Adapter:
    return Db2Adapter(
        _conn_info(),
        cast(SecretStore, secret_store or _SecretStore("pwd")),
        ibm_db_dbi_module=fake_dbi,
        ibm_db_module=fake_ibm_db,
        **kwargs,
    )


def _block_ibm_db_imports(monkeypatch: pytest.MonkeyPatch, *, block_dbi: bool) -> None:
    real_import_module = importlib.import_module

    def _fake_import(name: str, package: str | None = None) -> Any:
        if name == "ibm_db" or (block_dbi and name == "ibm_db_dbi"):
            raise ImportError(f"No module named {name!r}")
        return real_import_module(name, package)

    monkeypatch.setattr(importlib, "import_module", _fake_import)


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


class _ServerInfo:
    DBMS_NAME = "DB2/LINUXX8664"
    DBMS_VER = "11.5.9"


class _FakeIbmDb:
    """fake ibm_db C 模块:set_option / stmt_errormsg / conn_errormsg / server_info。"""

    SQL_ATTR_QUERY_TIMEOUT = 0

    def __init__(
        self,
        *,
        set_option_exc: Exception | None = None,
        stmt_errormsg: str = "",
        conn_errormsg: str = "",
    ) -> None:
        self.set_option_calls: list[tuple[Any, dict[Any, Any], int]] = []
        self._set_option_exc = set_option_exc
        self._stmt_errormsg = stmt_errormsg
        self._conn_errormsg = conn_errormsg

    def set_option(self, handle: Any, options: dict[Any, Any], scope: int) -> None:
        if self._set_option_exc is not None:
            raise self._set_option_exc
        self.set_option_calls.append((handle, dict(options), scope))

    def stmt_errormsg(self) -> str:
        return self._stmt_errormsg

    def conn_errormsg(self) -> str:
        return self._conn_errormsg

    def server_info(self, handle: Any) -> _ServerInfo:
        return _ServerInfo()


class _FakeIbmDbDbi:
    """fake ibm_db_dbi(DB-API wrapper):connect(conn_str, user, password)。"""

    def __init__(
        self,
        *,
        rows: list[tuple[Any, ...]] | None = None,
        description: tuple[tuple[Any, ...], ...] | None = None,
        connect_exc: Exception | None = None,
        execute_exc: Exception | None = None,
        fetch_exc: Exception | None = None,
        handle_attr: str = "conn_handler",
    ) -> None:
        self.conn_strs: list[str] = []
        self.connect_args: list[tuple[str, str]] = []
        self.connections: list[_FakeDb2Connection] = []
        self._rows = rows if rows is not None else [(1,)]
        self._description = description
        self._connect_exc = connect_exc
        self._execute_exc = execute_exc
        self._fetch_exc = fetch_exc
        self._handle_attr = handle_attr

    def connect(self, conn_str: str, user: str = "", password: str = "") -> _FakeDb2Connection:
        if self._connect_exc is not None:
            raise self._connect_exc
        self.conn_strs.append(conn_str)
        self.connect_args.append((user, password))
        conn = _FakeDb2Connection(
            rows=self._rows,
            description=self._description,
            execute_exc=self._execute_exc,
            fetch_exc=self._fetch_exc,
            handle_attr=self._handle_attr,
        )
        self.connections.append(conn)
        return conn


class _FakeDb2Connection:
    def __init__(
        self,
        *,
        rows: list[tuple[Any, ...]],
        description: tuple[tuple[Any, ...], ...] | None,
        execute_exc: Exception | None,
        fetch_exc: Exception | None,
        handle_attr: str,
    ) -> None:
        self.closed = False
        self.cursors: list[_FakeDb2Cursor] = []
        self._rows = rows
        self._description = description
        self._execute_exc = execute_exc
        self._fetch_exc = fetch_exc
        # 只暴露指定的 handle 属性名(conn_handler 老版本 / conn_handle 新版本);
        # 未选中的留 None(adapter 的 getattr(..., None) or getattr(..., None) 会跳过)
        self.conn_handler: Any | None = None
        self.conn_handle: Any | None = None
        setattr(self, handle_attr, object())

    def cursor(self) -> _FakeDb2Cursor:
        cursor = _FakeDb2Cursor(
            rows=self._rows,
            description=self._description,
            execute_exc=self._execute_exc,
            fetch_exc=self._fetch_exc,
        )
        self.cursors.append(cursor)
        return cursor

    def close(self) -> None:
        self.closed = True


class _FakeDb2Cursor:
    def __init__(
        self,
        *,
        rows: list[tuple[Any, ...]],
        description: tuple[tuple[Any, ...], ...] | None,
        execute_exc: Exception | None,
        fetch_exc: Exception | None,
    ) -> None:
        self.closed = False
        self.executed_sql = ""
        self.executed_params: object = None
        self.fetchmany_sizes: list[int] = []
        self.description: tuple[tuple[Any, ...], ...] = (("OK",),)
        self._configured_rows = rows
        self._configured_description = description
        self._execute_exc = execute_exc
        self._fetch_exc = fetch_exc
        self._rows: list[tuple[Any, ...]] = []
        self._offset = 0

    def execute(self, sql: str, params: object = None) -> None:
        self.executed_sql = sql
        self.executed_params = params
        if self._execute_exc is not None:
            raise self._execute_exc
        self._rows = list(self._configured_rows)
        if self._configured_description is not None:
            self.description = self._configured_description
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
