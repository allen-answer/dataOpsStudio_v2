from __future__ import annotations

from typing import Any, cast

import pytest

from app.dbclients.postgresql_adapter import (
    InvalidDatasourceError,
    PostgresqlAdapter,
    PostgresqlUnsupportedOperationError,
    QueryCancelledError,
)
from app.dbclients.postgresql_types import (
    data_type_string_to_column_type,
    type_code_to_column_type,
    type_code_to_driver_name,
)
from app.dbclients.protocol import AdapterConnectionError
from app.domain.datasource import DatasourceConnInfo, DbType
from app.domain.schema import Column, ColumnType, Index, Row, Schema, Table
from app.domain.secret import HashedRef, RotationReport, SecretKind, SecretRef
from app.infrastructure.secretstore.protocol import SecretStore


def test_requires_postgresql_db_type() -> None:
    conn_info = _conn_info().model_copy(update={"db_type": DbType.MYSQL})
    with pytest.raises(InvalidDatasourceError):
        PostgresqlAdapter(
            conn_info, cast(SecretStore, _SecretStore("pwd")), psycopg_module=_FakePsycopg()
        )


def test_capabilities_enable_execution_and_metadata_not_compare_hash() -> None:
    caps = _adapter(_FakePsycopg()).capabilities
    assert caps.execute_select is True
    assert caps.explain is True
    assert caps.stream_rows is True
    assert caps.list_schemas is True
    assert caps.list_tables is True
    assert caps.list_columns is True
    assert caps.list_indexes is True
    assert caps.server_side_cancel is False
    assert caps.get_table_ddl is False
    assert caps.compare_db_hash is False


def test_execute_select_streams_reveals_secret_and_closes_resources() -> None:
    fake_pg = _FakePsycopg(rows=[(1,), (2,), (3,)])
    secret_store = _SecretStore("pwd")
    adapter = _adapter(fake_pg, secret_store=secret_store, fetch_chunk_size=2)

    rows = list(adapter.execute_select("SELECT n FROM t", {}))

    assert rows == [Row(values=[1]), Row(values=[2]), Row(values=[3])]
    assert secret_store.revealed_refs == ["secret-1"]
    assert fake_pg.connections[0].kwargs["dbname"] == "app"
    assert fake_pg.connections[0].kwargs["autocommit"] is True
    assert fake_pg.connections[0].kwargs["connect_timeout"] == 10
    assert all(cursor.closed for cursor in fake_pg.connections[0].cursors)
    assert fake_pg.connections[0].closed is True


def test_execute_select_emits_columns_with_clean_driver_type() -> None:
    fake_pg = _FakePsycopg(
        rows=[(1, "alice")],
        description=(
            ("id", 23, None, None, None, None, False),
            ("name", 1043, None, None, None, None, True),
        ),
    )
    captured: list[Column] = []
    adapter = _adapter(fake_pg, column_sink=captured.extend)

    list(adapter.execute_select("SELECT id, name FROM users", {}))

    assert captured == [
        Column(name="id", type=ColumnType.INTEGER, driver_type="INT4", nullable=False),
        Column(name="name", type=ColumnType.STRING, driver_type="VARCHAR", nullable=True),
    ]


def test_explain_returns_plan_payload() -> None:
    payload = [{"Plan": {"Node Type": "Result"}}]
    adapter = _adapter(_FakePsycopg(explain_payload=payload))

    plan = adapter.explain("SELECT 1")

    assert plan.operation == "EXPLAIN"
    assert plan.details["rows"] == payload
    cursor = _last_cursor(adapter)
    assert cursor.executed_sql.startswith("EXPLAIN (FORMAT JSON) SELECT 1")


def test_metadata_methods_map_postgresql_catalog_rows() -> None:
    fake_pg = _FakePsycopg()
    adapter = _adapter(fake_pg)

    assert adapter.list_schemas() == [Schema(name="public")]
    assert adapter.list_tables("public") == [
        Table(schema_name="public", name="users", table_type="BASE TABLE")
    ]
    assert adapter.list_columns("public", "users") == [
        Column(
            name="id",
            type=ColumnType.INTEGER,
            driver_type="int4",
            nullable=False,
            primary_key=True,
        ),
        Column(
            name="email",
            type=ColumnType.STRING,
            driver_type="varchar",
            nullable=True,
            primary_key=False,
        ),
    ]
    assert adapter.list_indexes("public", "users") == [
        Index(name="users_pkey", columns=["id"], is_unique=True, is_primary=True)
    ]


def test_test_connection_records_server_version() -> None:
    adapter = _adapter(_FakePsycopg())

    assert adapter.test_connection() is True
    assert adapter.last_server_version == "PostgreSQL 16.test"


def test_test_connection_sanitizes_error_on_failure() -> None:
    adapter = _adapter(
        _FailingPsycopg(
            RuntimeError("connect failed password=top-secret postgresql://u:top-secret@db/app")
        ),
        secret_store=_SecretStore("top-secret"),
    )

    assert adapter.test_connection() is False
    assert adapter.last_connection_error is not None
    assert "top-secret" not in adapter.last_connection_error
    assert "***REDACTED***" in adapter.last_connection_error
    assert "<redacted-url>" in adapter.last_connection_error


def test_stream_select_preserves_connection_error_cause() -> None:
    original = RuntimeError("driver connect boom")
    adapter = _adapter(_FailingPsycopg(original))

    with pytest.raises(AdapterConnectionError) as excinfo:
        list(adapter.execute_select("SELECT 1", {}))

    assert excinfo.value.__cause__ is original


def test_soft_cancel_raises_on_safe_point_and_closes_connection() -> None:
    fake_pg = _FakePsycopg(rows=[(1,), (2,)])
    cancelled = {"flag": False}
    adapter = _adapter(fake_pg, fetch_chunk_size=1, cancel_check=lambda: cancelled["flag"])

    iterator = adapter.execute_select("SELECT n FROM t", {})
    assert next(iterator) == Row(values=[1])
    cancelled["flag"] = True
    with pytest.raises(QueryCancelledError):
        next(iterator)
    assert fake_pg.connections[0].closed is True


def test_unsupported_operations_raise_not_implemented() -> None:
    adapter = _adapter(_FakePsycopg())
    with pytest.raises(PostgresqlUnsupportedOperationError) as excinfo:
        adapter.get_table_ddl("public", "users")
    assert isinstance(excinfo.value, NotImplementedError)


def test_type_mapping_units() -> None:
    assert type_code_to_column_type(23) is ColumnType.INTEGER
    assert type_code_to_driver_name(23) == "INT4"
    assert type_code_to_column_type(1700) is ColumnType.DECIMAL
    assert type_code_to_column_type(1184) is ColumnType.DATETIME
    assert type_code_to_column_type(999999) is ColumnType.UNKNOWN
    assert data_type_string_to_column_type("character varying") is ColumnType.STRING
    assert data_type_string_to_column_type("timestamp with time zone") is ColumnType.DATETIME
    assert data_type_string_to_column_type("weirdtype") is ColumnType.UNKNOWN


def _conn_info() -> DatasourceConnInfo:
    return DatasourceConnInfo(
        host="127.0.0.1",
        port=5432,
        username="dataops",
        database="app",
        password_ref=SecretRef(ref="secret-1", kind=SecretKind.DATASOURCE_PASSWORD),
        db_type=DbType.POSTGRESQL,
    )


def _adapter(
    fake_pg: _FakePsycopg,
    *,
    secret_store: _SecretStore | None = None,
    **kwargs: Any,
) -> PostgresqlAdapter:
    return PostgresqlAdapter(
        _conn_info(),
        cast(SecretStore, secret_store or _SecretStore("pwd")),
        psycopg_module=fake_pg,
        **kwargs,
    )


def _last_cursor(adapter: PostgresqlAdapter) -> _FakeCursor:
    fake_pg = adapter._psycopg
    return cast(_FakePsycopg, fake_pg).connections[-1].cursors[-1]


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


class _FakePsycopg:
    def __init__(
        self,
        *,
        rows: list[tuple[Any, ...]] | None = None,
        description: tuple[tuple[Any, ...], ...] | None = None,
        explain_payload: object | None = None,
    ) -> None:
        self.connections: list[_FakeConnection] = []
        self._rows = rows if rows is not None else [(1,)]
        self._description = description
        self._explain_payload = explain_payload

    def connect(self, **kwargs: Any) -> _FakeConnection:
        conn = _FakeConnection(
            kwargs,
            rows=self._rows,
            description=self._description,
            explain_payload=self._explain_payload,
        )
        self.connections.append(conn)
        return conn


class _FailingPsycopg(_FakePsycopg):
    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self._exc = exc

    def connect(self, **kwargs: Any) -> _FakeConnection:
        raise self._exc


class _FakeConnection:
    def __init__(
        self,
        kwargs: dict[str, Any],
        *,
        rows: list[tuple[Any, ...]],
        description: tuple[tuple[Any, ...], ...] | None,
        explain_payload: object | None,
    ) -> None:
        self.kwargs = kwargs
        self.closed = False
        self.cursors: list[_FakeCursor] = []
        self._rows = rows
        self._description = description
        self._explain_payload = explain_payload

    def cursor(self) -> _FakeCursor:
        cursor = _FakeCursor(
            rows=self._rows,
            description=self._description,
            explain_payload=self._explain_payload,
        )
        self.cursors.append(cursor)
        return cursor

    def close(self) -> None:
        self.closed = True


class _FakeCursor:
    def __init__(
        self,
        *,
        rows: list[tuple[Any, ...]],
        description: tuple[tuple[Any, ...], ...] | None,
        explain_payload: object | None,
    ) -> None:
        self.closed = False
        self.executed_sql = ""
        self.description: tuple[tuple[Any, ...], ...] = description or (("n", 23),)
        self._configured_rows = rows
        self._configured_description = description
        self._explain_payload = explain_payload
        self._rows: list[tuple[Any, ...]] = []
        self._offset = 0

    def execute(self, sql: str, params: object = None) -> None:
        del params
        self.executed_sql = sql
        normalized = " ".join(sql.lower().split())
        if normalized.startswith("set statement_timeout"):
            self.description = (("ok",),)
            self._rows = []
        elif normalized.startswith("select version()"):
            self.description = (("version", 25),)
            self._rows = [("PostgreSQL 16.test",)]
        elif normalized.startswith("explain"):
            self.description = (("QUERY PLAN",),)
            self._rows = [(self._explain_payload or [{"Plan": {"Node Type": "Result"}}],)]
        elif "information_schema.schemata" in normalized:
            self.description = (("name",),)
            self._rows = [("public",)]
        elif "information_schema.tables" in normalized:
            self.description = (("schema_name",), ("name",), ("table_type",))
            self._rows = [("public", "users", "BASE TABLE")]
        elif "information_schema.columns" in normalized:
            self.description = (
                ("name",),
                ("data_type",),
                ("driver_type",),
                ("nullable",),
                ("table_comment",),
                ("primary_key",),
            )
            self._rows = [
                ("id", "integer", "int4", "NO", None, True),
                ("email", "character varying", "varchar", "YES", None, False),
            ]
        elif "pg_index" in normalized:
            self.description = (
                ("name",),
                ("column_name",),
                ("is_unique",),
                ("is_primary",),
                ("column_order",),
            )
            self._rows = [("users_pkey", "id", True, True, 1)]
        else:
            self.description = self._configured_description or (("n", 23),)
            self._rows = list(self._configured_rows)
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
