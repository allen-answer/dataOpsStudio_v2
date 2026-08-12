from __future__ import annotations

import importlib
import re
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from typing import Any

from app.dbclients.postgresql_types import (
    data_type_string_to_column_type,
    type_code_to_column_type,
    type_code_to_driver_name,
)
from app.dbclients.protocol import AdapterConnectionError, DatabaseAdapter
from app.dbclients.query_timing import QueryTimingRecorder
from app.dbclients.sql_guard import SqlGuardError, validate_readonly_sql
from app.domain.capabilities import AdapterCapabilities
from app.domain.compare import CompareHashPlan, CompareHashRequest
from app.domain.datasource import DatasourceConnInfo, DbType
from app.domain.plan import PlanNode
from app.domain.schema import Column, Index, Row, Schema, Table
from app.domain.secret import SecretKind
from app.infrastructure.secretstore.protocol import SecretStore

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.$-]+$")
_PASSWORD_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(password|passwd|pwd)\s*[:=]\s*['\"]?[^'\",\s)]+['\"]?"
)
_URL_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s'\")]+")
_MAX_ERROR_SUMMARY_LENGTH = 240


class PostgresqlAdapterError(RuntimeError):
    """PostgreSQL adapter base error. Messages must not contain secrets or raw SQL."""


class QueryCancelledError(PostgresqlAdapterError):
    """Soft cancel hit at an adapter safe point."""


class QueryTimeoutError(PostgresqlAdapterError):
    """Cursor exceeded cursor_max_hold_seconds."""


class InvalidDatasourceError(ValueError):
    """DatasourceConnInfo does not match PostgresqlAdapter."""


class PostgresqlUnsupportedOperationError(PostgresqlAdapterError, NotImplementedError):
    """Capability is not enabled for the PostgreSQL adapter."""


class PostgresqlAdapter(DatabaseAdapter):
    """PostgreSQL DatabaseAdapter for SQL Workspace execution and metadata browsing."""

    capabilities = AdapterCapabilities(
        execute_select=True,
        explain=True,
        stream_rows=True,
        # psycopg3 Connection.cancel_safe() 真取消在飞查询(execute() 阻塞窗口),
        # 由 _stream_select 的看门狗线程在 cancel_check 命中时触发,不再只靠批间软取消。
        server_side_cancel=True,
        list_schemas=True,
        list_tables=True,
        list_columns=True,
        list_indexes=True,
        get_table_ddl=False,
        compare_db_hash=False,
    )

    def __init__(
        self,
        conn_info: DatasourceConnInfo,
        secret_store: SecretStore,
        *,
        cancel_check: Callable[[], bool] | None = None,
        cursor_max_hold_seconds: int = 300,
        statement_timeout_seconds: int = 300,
        fetch_chunk_size: int = 1000,
        connect_timeout_seconds: int = 10,
        cancel_poll_interval_seconds: float = 0.25,
        psycopg_module: Any | None = None,
        column_sink: Callable[[list[Column]], None] | None = None,
    ) -> None:
        if conn_info.db_type is not DbType.POSTGRESQL:
            raise InvalidDatasourceError("PostgresqlAdapter requires DbType.POSTGRESQL")
        if conn_info.password_ref.kind is not SecretKind.DATASOURCE_PASSWORD:
            raise InvalidDatasourceError("password_ref.kind must be DATASOURCE_PASSWORD")
        if fetch_chunk_size <= 0:
            raise ValueError("fetch_chunk_size must be positive")

        self._conn_info = conn_info
        self._secret_store = secret_store
        self._cancel_check = cancel_check
        self._cursor_max_hold_seconds = cursor_max_hold_seconds
        self._statement_timeout_seconds = statement_timeout_seconds
        self._fetch_chunk_size = fetch_chunk_size
        self._connect_timeout_seconds = connect_timeout_seconds
        self._cancel_poll_interval_seconds = cancel_poll_interval_seconds
        self._psycopg = psycopg_module
        self._column_sink = column_sink
        self._last_server_version: str | None = None
        self._last_connection_error: str | None = None
        self._query_timing = QueryTimingRecorder()

    @property
    def last_server_version(self) -> str | None:
        return self._last_server_version

    @property
    def last_connection_error(self) -> str | None:
        return self._last_connection_error

    @property
    def query_timings_ms(self) -> dict[str, int]:
        return self._query_timing.snapshot()

    @property
    def query_execution_metrics(self) -> dict[str, int | str | None]:
        return self._query_timing.execution_snapshot()

    def execute_select(self, sql: str, params: dict[str, Any]) -> Iterator[Row]:
        guarded_sql = validate_readonly_sql(sql)
        return self._stream_select(guarded_sql, params)

    def stream_rows(self, sql: str) -> Iterator[Row]:
        return self.execute_select(sql, {})

    def explain(self, sql: str) -> PlanNode:
        guarded_sql = validate_readonly_sql(sql)
        rows = self._query_tuples(f"EXPLAIN (FORMAT JSON) {guarded_sql}", None)
        payload = rows[0][0] if rows and rows[0] else None
        return PlanNode(operation="EXPLAIN", details={"rows": payload})

    def test_connection(self) -> bool:
        conn = None
        cursor = None
        self._last_server_version = None
        self._last_connection_error = None
        try:
            conn = self._connect()
            cursor = conn.cursor()
            cursor.execute("SELECT version()")
            version = _first_cell(cursor.fetchone())
            if version:
                self._last_server_version = str(version)
            return True
        except Exception as exc:
            self._last_connection_error = _connection_error_summary(exc)
            return False
        finally:
            _safe_close(cursor)
            _safe_close(conn)

    def list_schemas(self) -> list[Schema]:
        rows = self._query_dicts(
            """
            SELECT schema_name AS name
            FROM information_schema.schemata
            WHERE schema_name NOT IN ('information_schema', 'pg_catalog')
              AND schema_name NOT LIKE 'pg_toast%'
              AND schema_name NOT LIKE 'pg_temp%'
            ORDER BY schema_name
            """,
            None,
        )
        return [Schema(name=str(row["name"])) for row in rows]

    def list_tables(self, schema: str) -> list[Table]:
        _validate_identifier(schema)
        rows = self._query_dicts(
            """
            SELECT table_schema AS schema_name, table_name AS name, table_type
            FROM information_schema.tables
            WHERE table_schema = %s
              AND table_type IN ('BASE TABLE', 'VIEW', 'FOREIGN TABLE', 'MATERIALIZED VIEW')
            ORDER BY table_name
            """,
            (schema,),
        )
        return [
            Table(
                schema_name=str(row["schema_name"]),
                name=str(row["name"]),
                table_type=str(row["table_type"]) if row["table_type"] is not None else None,
            )
            for row in rows
        ]

    def list_columns(self, schema: str, table: str) -> list[Column]:
        _validate_identifier(schema)
        _validate_identifier(table)
        rows = self._query_dicts(
            """
            SELECT
                c.column_name AS name,
                c.data_type AS data_type,
                c.udt_name AS driver_type,
                c.is_nullable AS nullable,
                EXISTS (
                    SELECT 1
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage kcu
                      ON tc.constraint_name = kcu.constraint_name
                     AND tc.table_schema = kcu.table_schema
                     AND tc.table_name = kcu.table_name
                    WHERE tc.constraint_type = 'PRIMARY KEY'
                      AND tc.table_schema = c.table_schema
                      AND tc.table_name = c.table_name
                      AND kcu.column_name = c.column_name
                ) AS primary_key
            FROM information_schema.columns c
            WHERE c.table_schema = %s AND c.table_name = %s
            ORDER BY c.ordinal_position
            """,
            (schema, table),
        )
        return [
            Column(
                name=str(row["name"]),
                type=data_type_string_to_column_type(row["data_type"]),
                driver_type=str(row["driver_type"]) if row["driver_type"] is not None else None,
                nullable=str(row["nullable"]).upper() == "YES",
                primary_key=bool(row["primary_key"]),
            )
            for row in rows
        ]

    def list_indexes(self, schema: str, table: str) -> list[Index]:
        _validate_identifier(schema)
        _validate_identifier(table)
        rows = self._query_dicts(
            """
            SELECT
                i.relname AS name,
                COALESCE(a.attname, '<expression>') AS column_name,
                ix.indisunique AS is_unique,
                ix.indisprimary AS is_primary,
                ord.ordinality AS column_order
            FROM pg_class t
            JOIN pg_namespace n ON n.oid = t.relnamespace
            JOIN pg_index ix ON ix.indrelid = t.oid
            JOIN pg_class i ON i.oid = ix.indexrelid
            JOIN unnest(ix.indkey) WITH ORDINALITY AS ord(attnum, ordinality) ON true
            LEFT JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ord.attnum
            WHERE n.nspname = %s AND t.relname = %s
            ORDER BY i.relname, ord.ordinality
            """,
            (schema, table),
        )
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            name = str(row["name"])
            item = grouped.setdefault(
                name,
                {
                    "columns": [],
                    "is_unique": bool(row["is_unique"]),
                    "is_primary": bool(row["is_primary"]),
                },
            )
            item["columns"].append(str(row["column_name"]))
        return [
            Index(
                name=name,
                columns=list(item["columns"]),
                is_unique=bool(item["is_unique"]),
                is_primary=bool(item["is_primary"]),
            )
            for name, item in grouped.items()
        ]

    def get_table_ddl(self, schema: str, table: str) -> str:
        raise self._unsupported("get_table_ddl")

    def build_compare_hash_query(self, request: CompareHashRequest) -> CompareHashPlan:
        raise self._unsupported("build_compare_hash_query")

    def kill_query(self, connection_id: str) -> bool:
        # server_side_cancel=True 的真取消走 _stream_select 内的 cancel_safe() 看门狗
        # (对本 adapter 正在执行的连接生效);按 connection_id 外部 kill 需追踪 backend
        # PID + 独立管理连接,当前无调用方接线,故此入口恒 False。
        return False

    def _stream_select(self, sql: str, params: dict[str, Any]) -> Iterator[Row]:
        conn = None
        cursor = None
        started_at = time.monotonic()
        self._query_timing.reset()
        self._query_timing.mark_connect_started()
        connect_started = time.monotonic()
        try:
            conn = self._connect()
        except Exception as exc:
            raise AdapterConnectionError("adapter connection failed") from exc
        self._query_timing.record_connect(_elapsed_ms(connect_started))
        try:
            self._apply_statement_timeout(conn)
            cursor = conn.cursor()
            self._query_timing.mark_execute_started()
            execute_started = time.monotonic()
            self._execute_with_cancel(conn, cursor, sql, params)
            self._query_timing.record_execute(_elapsed_ms(execute_started))
            self._emit_columns(getattr(cursor, "description", None))
            while True:
                self._check_cancel()
                self._check_cursor_deadline(started_at)
                fetch_started = time.monotonic()
                batch = cursor.fetchmany(self._fetch_chunk_size)
                self._query_timing.record_fetch(_elapsed_ms(fetch_started), row_count=len(batch))
                if not batch:
                    break
                for raw_row in batch:
                    yield _to_row(raw_row)
        finally:
            _safe_close(cursor)
            _safe_close(conn)

    def _execute_with_cancel(
        self, conn: Any, cursor: Any, sql: str, params: dict[str, Any]
    ) -> None:
        """执行查询,并在 execute() 阻塞窗口内提供 driver-level 取消。

        psycopg3 客户端游标把整段等待都压在 cursor.execute()(结果一次性拉回),
        批间 _check_cancel 够不着这里。故起一个看门狗线程轮询 cancel_check,命中即
        调用 conn.cancel_safe()(3.2+;回退 legacy cancel())真取消服务端在飞查询,
        execute() 随后抛错 → 收敛为 QueryCancelledError。只有本 adapter 主动取消
        (cancelled 置位)才映射为 QueryCancelledError,statement_timeout 等其它错
        原样上抛,不改既有语义。
        """
        bound = params or None
        cancel_check = self._cancel_check
        if cancel_check is None:
            cursor.execute(sql, bound)
            return

        stop = threading.Event()
        cancelled = threading.Event()

        def _watch() -> None:
            while not stop.wait(self._cancel_poll_interval_seconds):
                try:
                    requested = cancel_check()
                except Exception:
                    return
                if requested:
                    cancelled.set()
                    _server_side_cancel(conn)
                    return

        watcher = threading.Thread(target=_watch, name="pg-cancel-watch", daemon=True)
        watcher.start()
        try:
            cursor.execute(sql, bound)
        except BaseException as exc:
            if cancelled.is_set():
                raise QueryCancelledError("Query cancelled") from exc
            raise
        finally:
            stop.set()
            watcher.join(timeout=5.0)

    def _query_dicts(
        self,
        sql: str,
        params: Sequence[Any] | dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        conn = None
        cursor = None
        try:
            conn = self._connect()
            self._apply_statement_timeout(conn)
            cursor = conn.cursor()
            cursor.execute(sql, params)
            rows = cursor.fetchall()
            columns = _description_columns(getattr(cursor, "description", None))
            return [_row_to_dict(row, columns) for row in rows]
        finally:
            _safe_close(cursor)
            _safe_close(conn)

    def _query_tuples(
        self,
        sql: str,
        params: Sequence[Any] | dict[str, Any] | None,
    ) -> list[Sequence[Any]]:
        conn = None
        cursor = None
        try:
            conn = self._connect()
            self._apply_statement_timeout(conn)
            cursor = conn.cursor()
            cursor.execute(sql, params)
            return [
                tuple(row) if not isinstance(row, dict) else tuple(row.values())
                for row in cursor.fetchall()
            ]
        finally:
            _safe_close(cursor)
            _safe_close(conn)

    def _connect(self) -> Any:
        psycopg = self._load_psycopg()
        password = self._secret_store.reveal_secret(self._conn_info.password_ref)
        extra = self._conn_info.extra
        kwargs: dict[str, Any] = {
            "host": self._conn_info.host,
            "port": self._conn_info.port,
            "user": self._conn_info.username,
            "password": password,
            "dbname": self._conn_info.database,
            "connect_timeout": int(extra.get("connect_timeout", self._connect_timeout_seconds)),
            "autocommit": True,
        }
        for key in ("sslmode", "target_session_attrs", "application_name", "options"):
            if key in extra:
                kwargs[key] = extra[key]
        return psycopg.connect(**{key: value for key, value in kwargs.items() if value is not None})

    def _load_psycopg(self) -> Any:
        if self._psycopg is None:
            self._psycopg = importlib.import_module("psycopg")
        return self._psycopg

    def _apply_statement_timeout(self, conn: Any) -> None:
        if self._statement_timeout_seconds <= 0:
            return
        # psycopg3 默认服务端绑定(extended protocol),SET 是 utility 语句
        # 不接受绑定参数,"SET statement_timeout = %s" 会直接 SyntaxError;
        # set_config() 是普通函数调用,可参数化(值为 text,单位毫秒)。
        cursor = None
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT set_config('statement_timeout', %s, false)",
                (str(int(self._statement_timeout_seconds * 1000)),),
            )
        except Exception:
            return
        finally:
            _safe_close(cursor)

    def _check_cancel(self) -> None:
        if self._cancel_check is not None and self._cancel_check():
            raise QueryCancelledError("Query cancelled")

    def _check_cursor_deadline(self, started_at: float) -> None:
        if self._cursor_max_hold_seconds <= 0:
            return
        if time.monotonic() - started_at > self._cursor_max_hold_seconds:
            raise QueryTimeoutError("Query cursor hold time exceeded")

    def _emit_columns(self, description: object) -> None:
        if self._column_sink is None:
            return
        columns = self._description_to_columns(description)
        self._column_sink([column.model_copy(deep=True) for column in columns])

    def _description_to_columns(self, description: object) -> list[Column]:
        if not isinstance(description, Sequence) or isinstance(
            description, (str, bytes, bytearray)
        ):
            return []
        columns: list[Column] = []
        for item in description:
            name = _description_name(item)
            if name is None:
                continue
            type_code = _description_type_code(item)
            nullable_value = _description_nullable(item)
            columns.append(
                Column(
                    name=name,
                    type=type_code_to_column_type(type_code),
                    driver_type=type_code_to_driver_name(type_code),
                    nullable=bool(nullable_value) if nullable_value is not None else True,
                    primary_key=False,
                )
            )
        return columns

    def _unsupported(self, operation: str) -> PostgresqlUnsupportedOperationError:
        return PostgresqlUnsupportedOperationError(
            f"PostgresqlAdapter does not support {operation} in this build. "
            "Check adapter.capabilities before calling."
        )


def _server_side_cancel(conn: object) -> bool:
    """尽力对在飞查询做 driver-level 取消。

    优先 psycopg3.2+ 的 Connection.cancel_safe()(另开一条连接下发 cancel 请求,
    线程安全);不可用时回退 legacy 阻塞式 cancel()。两者都失败则返回 False。
    """
    for method_name in ("cancel_safe", "cancel"):
        method = getattr(conn, method_name, None)
        if callable(method):
            try:
                method()
                return True
            except Exception:
                continue
    return False


def _validate_identifier(identifier: str) -> None:
    if not identifier or _IDENTIFIER_RE.fullmatch(identifier) is None:
        raise ValueError("Invalid database identifier")


def _description_name(item: object) -> str | None:
    value = getattr(item, "name", None)
    if value is not None:
        return str(value)
    if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)) and item:
        return str(item[0])
    return None


def _description_type_code(item: object) -> object:
    value = getattr(item, "type_code", None)
    if value is not None:
        return value
    if (
        isinstance(item, Sequence)
        and not isinstance(item, (str, bytes, bytearray))
        and len(item) > 1
    ):
        return item[1]
    return None


def _description_nullable(item: object) -> object:
    value = getattr(item, "null_ok", None)
    if value is not None:
        return value
    if (
        isinstance(item, Sequence)
        and not isinstance(item, (str, bytes, bytearray))
        and len(item) > 6
    ):
        return item[6]
    return None


def _description_columns(description: object) -> list[str]:
    if not isinstance(description, Sequence) or isinstance(description, (str, bytes, bytearray)):
        return []
    columns: list[str] = []
    for item in description:
        name = _description_name(item)
        if name is not None:
            columns.append(name)
    return columns


def _row_to_dict(row: object, columns: list[str]) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    if not isinstance(row, Sequence) or isinstance(row, (str, bytes, bytearray)):
        return {}
    return {columns[index]: value for index, value in enumerate(row) if index < len(columns)}


def _to_row(raw_row: object) -> Row:
    if isinstance(raw_row, dict):
        return Row(values=list(raw_row.values()))
    if isinstance(raw_row, Sequence) and not isinstance(raw_row, (str, bytes, bytearray)):
        return Row(values=list(raw_row))
    return Row(values=[raw_row])


def _first_cell(row: object) -> object | None:
    if row is None:
        return None
    if isinstance(row, dict):
        return next(iter(row.values()), None)
    if isinstance(row, Sequence) and not isinstance(row, (str, bytes, bytearray)):
        return row[0] if row else None
    return row


def _connection_error_summary(exc: Exception) -> str:
    parts = [type(exc).__name__]
    code = _first_int_arg(exc)
    if code is not None:
        parts.append(f"code={code}")
    message = _sanitize_error_text(str(exc))
    if message:
        parts.append(f"message={message}")
    return " ".join(parts)


def _first_int_arg(exc: Exception) -> int | None:
    for arg in getattr(exc, "args", ()):
        if isinstance(arg, int):
            return arg
    return None


def _sanitize_error_text(text: str) -> str:
    sanitized = _PASSWORD_ASSIGNMENT_RE.sub(r"\1=***REDACTED***", text)
    sanitized = _URL_RE.sub("<redacted-url>", sanitized)
    sanitized = " ".join(sanitized.split())
    if len(sanitized) > _MAX_ERROR_SUMMARY_LENGTH:
        return f"{sanitized[:_MAX_ERROR_SUMMARY_LENGTH]}..."
    return sanitized


def _safe_close(obj: object | None) -> None:
    if obj is None:
        return
    close = getattr(obj, "close", None)
    if close is None:
        return
    try:
        close()
    except Exception:
        return


def _elapsed_ms(started_at: float) -> int:
    return max(0, round((time.monotonic() - started_at) * 1000))


__all__ = [
    "InvalidDatasourceError",
    "PostgresqlAdapter",
    "PostgresqlAdapterError",
    "PostgresqlUnsupportedOperationError",
    "QueryCancelledError",
    "QueryTimeoutError",
    "SqlGuardError",
]
