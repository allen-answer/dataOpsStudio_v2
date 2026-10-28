"""写语句(DML / DDL)执行:语句分类、单语句事务、六个适配器的 execute_write。"""

from __future__ import annotations

from typing import Any, cast

import pytest

from app.dbclients.db2_adapter import Db2Adapter
from app.dbclients.dm_adapter import DMAdapter
from app.dbclients.mysql_adapter import MySQLAdapter
from app.dbclients.oceanbase_oracle_adapter import OceanBaseOracleAdapter
from app.dbclients.oracle_adapter import OracleAdapter
from app.dbclients.postgresql_adapter import PostgresqlAdapter
from app.dbclients.protocol import AdapterConnectionError
from app.dbclients.sql_guard import SqlGuardError, SqlStatementKind, classify_executable_sql
from app.dbclients.sql_write import WriteResult, run_write_statement
from app.domain.datasource import DatasourceConnInfo, DbType
from app.domain.secret import SecretKind, SecretRef
from app.infrastructure.secretstore.protocol import SecretStore


@pytest.mark.parametrize(
    ("sql", "kind", "verb", "statement"),
    [
        ("INSERT INTO t VALUES (1);", SqlStatementKind.DML, "insert", "INSERT INTO t VALUES (1)"),
        ("-- note\nupdate t set a = 1 where id = 2", SqlStatementKind.DML, "update", None),
        ("DELETE FROM t WHERE id = 1", SqlStatementKind.DML, "delete", None),
        (
            "MERGE INTO t USING s ON (t.id = s.id) WHEN MATCHED THEN UPDATE SET t.a = s.a",
            SqlStatementKind.DML,
            "merge",
            None,
        ),
        ("CREATE TABLE t (id INT);", SqlStatementKind.DDL, "create", "CREATE TABLE t (id INT)"),
        ("ALTER TABLE t ADD c INT", SqlStatementKind.DDL, "alter", None),
        ("/* drop */ DROP TABLE t", SqlStatementKind.DDL, "drop", "DROP TABLE t"),
        ("TRUNCATE TABLE t", SqlStatementKind.DDL, "truncate", None),
        ("SELECT * FROM t;", SqlStatementKind.QUERY, "select", "SELECT * FROM t;"),
        ("WITH x AS (SELECT 1) SELECT * FROM x", SqlStatementKind.QUERY, "with", None),
    ],
)
def test_classify_executable_sql_accepts_query_dml_and_ddl(
    sql: str, kind: SqlStatementKind, verb: str, statement: str | None
) -> None:
    classified = classify_executable_sql(sql)

    assert classified.kind is kind
    assert classified.verb == verb
    assert classified.is_write is (kind is not SqlStatementKind.QUERY)
    if statement is not None:
        assert classified.sql == statement


@pytest.mark.parametrize(
    "sql",
    [
        "",
        "  -- only a comment",
        "GRANT SELECT ON t TO someone",
        "REVOKE SELECT ON t FROM someone",
        "CALL p()",
        "BEGIN NULL; END;",
        "COMMIT",
        "LOCK TABLE t IN EXCLUSIVE MODE",
        "INSERT INTO t VALUES (1); DELETE FROM t",
        "UPDATE t SET a = 1; DROP TABLE t",
        # 只读链路的既有规则不变:查询里夹写关键字 / FOR UPDATE 仍拒绝
        "WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d",
        "SELECT * FROM t FOR UPDATE",
    ],
)
def test_classify_executable_sql_rejects_everything_else(sql: str) -> None:
    with pytest.raises(SqlGuardError):
        classify_executable_sql(sql)


@pytest.mark.parametrize(
    ("sql", "missing_where"),
    [
        ("UPDATE t SET a = 1", True),
        ("DELETE FROM t", True),
        ("DELETE FROM t -- where id = 1", True),
        ("UPDATE t SET note = 'where'", True),
        ('UPDATE "where" SET a = 1', True),
        ("UPDATE t SET a = 1 WHERE id = 1", False),
        ("delete from t\nwhere id in (1, 2)", False),
        ("INSERT INTO t SELECT * FROM s", False),
        ("TRUNCATE TABLE t", False),
    ],
)
def test_classify_flags_update_and_delete_without_where(sql: str, missing_where: bool) -> None:
    assert classify_executable_sql(sql).missing_where is missing_where


def test_semicolon_inside_literal_is_not_a_second_statement() -> None:
    classified = classify_executable_sql("INSERT INTO t (s) VALUES ('a;b');")

    assert classified.sql == "INSERT INTO t (s) VALUES ('a;b')"


class _Cursor:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn
        self.rowcount: object = conn.rowcount
        self.closed = False

    def execute(self, sql: str, params: object = None) -> None:
        self._conn.executed.append((sql, params))
        if self._conn.execute_exc is not None and not sql.lower().startswith(("set", "select")):
            raise self._conn.execute_exc

    def close(self) -> None:
        self.closed = True


class _Conn:
    def __init__(self, *, rowcount: object = 2, execute_exc: Exception | None = None) -> None:
        self.rowcount = rowcount
        self.execute_exc = execute_exc
        self.executed: list[tuple[str, object]] = []
        self.cursors: list[_Cursor] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.autocommit = True

    def cursor(self, *args: object) -> _Cursor:
        cursor = _Cursor(self)
        self.cursors.append(cursor)
        return cursor

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


def _no_cancel() -> None:
    return None


def test_run_write_statement_commits_and_reports_rowcount() -> None:
    conn = _Conn(rowcount=5)

    result = run_write_statement(conn, "DELETE FROM t", {}, check_cancel=_no_cancel)

    assert result == WriteResult(affected_rows=5)
    # 无参数不传 params:pyformat 驱动不会把 SQL 里的 % 当占位符
    assert conn.executed == [("DELETE FROM t", None)]
    assert (conn.commits, conn.rollbacks) == (1, 0)
    assert conn.cursors[0].closed is True


def test_run_write_statement_passes_bind_params() -> None:
    conn = _Conn()

    run_write_statement(conn, "DELETE FROM t WHERE id = %(id)s", {"id": 1}, check_cancel=_no_cancel)

    assert conn.executed == [("DELETE FROM t WHERE id = %(id)s", {"id": 1})]


@pytest.mark.parametrize("rowcount", [-1, None, True, "3"])
def test_run_write_statement_hides_unknown_rowcount(rowcount: object) -> None:
    result = run_write_statement(
        _Conn(rowcount=rowcount), "CREATE TABLE t (id INT)", {}, check_cancel=_no_cancel
    )

    assert result.affected_rows is None


def test_run_write_statement_rolls_back_on_error() -> None:
    conn = _Conn(execute_exc=RuntimeError("constraint violated"))

    with pytest.raises(RuntimeError):
        run_write_statement(conn, "INSERT INTO t VALUES (1)", {}, check_cancel=_no_cancel)

    assert (conn.commits, conn.rollbacks) == (0, 1)
    assert conn.cursors[0].closed is True


def test_run_write_statement_rolls_back_when_cancelled_before_commit() -> None:
    conn = _Conn()

    def cancelled() -> None:
        raise RuntimeError("Query cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        run_write_statement(conn, "UPDATE t SET a = 1", {}, check_cancel=cancelled)

    assert (conn.commits, conn.rollbacks) == (0, 1)


class _SecretStore:
    def reveal_secret(self, ref: SecretRef) -> str:
        return "pwd"


_ADAPTERS: list[tuple[type[Any], DbType, int]] = [
    (MySQLAdapter, DbType.MYSQL, 3306),
    (PostgresqlAdapter, DbType.POSTGRESQL, 5432),
    (DMAdapter, DbType.DM, 5236),
    (OracleAdapter, DbType.ORACLE, 1521),
    (OceanBaseOracleAdapter, DbType.OCEANBASE_ORACLE, 2883),
    (Db2Adapter, DbType.DB2, 50000),
]


def _build(adapter_cls: type[Any], db_type: DbType, port: int) -> Any:
    extra: dict[str, Any] = {"tenant": "app_tenant"} if db_type is DbType.OCEANBASE_ORACLE else {}
    conn_info = DatasourceConnInfo(
        host="127.0.0.1",
        port=port,
        username="app_user",
        database="APP",
        password_ref=SecretRef(ref="secret-1", kind=SecretKind.DATASOURCE_PASSWORD),
        db_type=db_type,
        extra=extra,
    )
    return adapter_cls(conn_info, cast(SecretStore, _SecretStore()), statement_timeout_seconds=0)


@pytest.mark.parametrize(("adapter_cls", "db_type", "port"), _ADAPTERS)
def test_every_adapter_executes_write_in_one_committed_transaction(
    adapter_cls: type[Any], db_type: DbType, port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _build(adapter_cls, db_type, port)
    conn = _Conn(rowcount=4)
    monkeypatch.setattr(adapter, "_connect", lambda: conn)

    assert adapter.capabilities.execute_write is True
    result = adapter.execute_write("UPDATE t SET a = 1 WHERE id = 1", {})

    assert result == WriteResult(affected_rows=4)
    assert ("UPDATE t SET a = 1 WHERE id = 1", None) in conn.executed
    assert (conn.commits, conn.rollbacks) == (1, 0)
    assert conn.closed is True


@pytest.mark.parametrize(("adapter_cls", "db_type", "port"), _ADAPTERS)
def test_every_adapter_rolls_back_a_failed_write(
    adapter_cls: type[Any], db_type: DbType, port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _build(adapter_cls, db_type, port)
    conn = _Conn(execute_exc=RuntimeError("ORA-00001: unique constraint violated"))
    monkeypatch.setattr(adapter, "_connect", lambda: conn)

    with pytest.raises(Exception):  # noqa: B017 - 各适配器包装成各自的驱动错误类型
        adapter.execute_write("INSERT INTO t VALUES (1)", {})

    assert (conn.commits, conn.rollbacks) == (0, 1)
    assert conn.closed is True


@pytest.mark.parametrize(("adapter_cls", "db_type", "port"), _ADAPTERS)
def test_every_adapter_reports_connection_failure_for_writes(
    adapter_cls: type[Any], db_type: DbType, port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _build(adapter_cls, db_type, port)

    def refuse() -> Any:
        raise OSError("connection refused")

    monkeypatch.setattr(adapter, "_connect", refuse)

    with pytest.raises(AdapterConnectionError):
        adapter.execute_write("DELETE FROM t WHERE id = 1", {})


def test_postgresql_write_turns_off_autocommit_so_commit_is_real(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _build(PostgresqlAdapter, DbType.POSTGRESQL, 5432)
    conn = _Conn()
    monkeypatch.setattr(adapter, "_connect", lambda: conn)

    adapter.execute_write("DELETE FROM t WHERE id = 1", {})

    assert conn.autocommit is False
