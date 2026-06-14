from __future__ import annotations

import importlib
import re
import time
from collections.abc import Callable, Iterator, Sequence
from typing import Any

from app.dbclients.mysql_compare import build_mysql_compare_hash_plan
from app.dbclients.mysql_types import (
    column_type_string_to_column_type,
    field_type_to_column_type,
)
from app.dbclients.protocol import AdapterConnectionError, DatabaseAdapter
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


class MySQLAdapterError(RuntimeError):
    """MySQL adapter 基础异常。错误信息不得包含密码或 SQL 原文。"""


class QueryCancelledError(MySQLAdapterError):
    """软取消命中。"""


class QueryTimeoutError(MySQLAdapterError):
    """cursor 持有超过 cursor_max_hold_seconds。"""


class InvalidDatasourceError(ValueError):
    """DatasourceConnInfo 与 MySQLAdapter 不匹配。"""


class MySQLAdapter(DatabaseAdapter):
    """MySQL DatabaseAdapter,使用 PyMySQL SSCursor 流式读取。"""

    capabilities = AdapterCapabilities(
        execute_select=True,
        explain=True,
        stream_rows=True,
        server_side_cancel=False,
        list_schemas=True,
        list_tables=True,
        list_columns=True,
        list_indexes=True,
        get_table_ddl=True,
        compare_db_hash=True,
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
        pymysql_module: Any | None = None,
        column_sink: Callable[[list[Column]], None] | None = None,
    ) -> None:
        if conn_info.db_type is not DbType.MYSQL:
            raise InvalidDatasourceError("MySQLAdapter requires DbType.MYSQL")
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
        self._pymysql = pymysql_module
        self._column_sink = column_sink
        self._last_server_version: str | None = None
        self._last_connection_error: str | None = None

    @property
    def last_server_version(self) -> str | None:
        return self._last_server_version

    @property
    def last_connection_error(self) -> str | None:
        return self._last_connection_error

    def execute_select(self, sql: str, params: dict[str, Any]) -> Iterator[Row]:
        guarded_sql = validate_readonly_sql(sql)
        return self._stream_select(guarded_sql, params)

    def stream_rows(self, sql: str) -> Iterator[Row]:
        return self.execute_select(sql, {})

    def explain(self, sql: str) -> PlanNode:
        guarded_sql = validate_readonly_sql(sql)
        rows = self._query_dicts(f"EXPLAIN {guarded_sql}", None)
        return PlanNode(operation="EXPLAIN", details={"rows": rows})

    def test_connection(self) -> bool:
        conn = None
        cursor = None
        self._last_server_version = None
        self._last_connection_error = None
        try:
            conn = self._connect()
            cursor = conn.cursor()
            cursor.execute("SELECT VERSION()")
            version = _first_cell(cursor.fetchone())
            if version:
                self._last_server_version = f"MySQL {version}"
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
            SELECT SCHEMA_NAME AS name
            FROM information_schema.SCHEMATA
            WHERE SCHEMA_NAME NOT IN ('information_schema', 'mysql', 'performance_schema', 'sys')
            ORDER BY SCHEMA_NAME
            """,
            None,
        )
        return [Schema(name=str(row["name"])) for row in rows]

    def list_tables(self, schema: str) -> list[Table]:
        _validate_identifier(schema)
        rows = self._query_dicts(
            """
            SELECT TABLE_SCHEMA AS schema_name, TABLE_NAME AS name, TABLE_TYPE AS table_type
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = %s
            ORDER BY TABLE_NAME
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
                COLUMN_NAME AS name,
                COLUMN_TYPE AS type,
                IS_NULLABLE AS nullable,
                COLUMN_KEY AS column_key,
                COLUMN_COMMENT AS comment
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
            ORDER BY ORDINAL_POSITION
            """,
            (schema, table),
        )
        return [
            Column(
                name=str(row["name"]),
                type=column_type_string_to_column_type(row["type"]),
                driver_type=str(row["type"]) if row["type"] is not None else None,
                nullable=str(row["nullable"]).upper() == "YES",
                primary_key=str(row["column_key"]).upper() == "PRI",
                comment=_optional_str(row.get("comment")),
            )
            for row in rows
        ]

    def list_indexes(self, schema: str, table: str) -> list[Index]:
        _validate_identifier(schema)
        _validate_identifier(table)
        rows = self._query_dicts(
            """
            SELECT
                INDEX_NAME AS name,
                COLUMN_NAME AS column_name,
                NON_UNIQUE AS non_unique,
                SEQ_IN_INDEX AS seq_in_index
            FROM information_schema.STATISTICS
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
            ORDER BY INDEX_NAME, SEQ_IN_INDEX
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
                    "is_unique": int(row["non_unique"]) == 0,
                    "is_primary": name.upper() == "PRIMARY",
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
        _validate_identifier(schema)
        _validate_identifier(table)
        table_name = f"{_quote_identifier(schema)}.{_quote_identifier(table)}"
        rows = self._query_tuples(f"SHOW CREATE TABLE {table_name}")
        if not rows or len(rows[0]) < 2:
            raise MySQLAdapterError("SHOW CREATE TABLE returned no DDL")
        return str(rows[0][1])

    def build_compare_hash_query(self, request: CompareHashRequest) -> CompareHashPlan:
        return build_mysql_compare_hash_plan(request)

    def kill_query(self, connection_id: str) -> bool:
        return False

    def _stream_select(self, sql: str, params: dict[str, Any]) -> Iterator[Row]:
        conn = None
        cursor = None
        started_at = time.monotonic()
        try:
            conn = self._connect()
        except Exception as exc:
            # 保留 cause(from exc 而非 from None):worker 的 logger.exception 会带 cause
            # 链便于排障,且日志链路有 R5 脱敏 processor 兜底,原始驱动异常不会泄密。
            raise AdapterConnectionError("adapter connection failed") from exc
        try:
            self._apply_statement_timeout(conn)
            cursor = conn.cursor(self._sscursor_class())
            cursor.execute(sql, params or None)
            self._emit_columns(getattr(cursor, "description", None))
            while True:
                self._check_cancel()
                self._check_cursor_deadline(started_at)
                batch = cursor.fetchmany(self._fetch_chunk_size)
                if not batch:
                    break
                for raw_row in batch:
                    yield _to_row(raw_row)
        finally:
            _safe_close(cursor)
            _safe_close(conn)

    def _query_dicts(
        self,
        sql: str,
        params: Sequence[Any] | dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        conn = None
        cursor = None
        try:
            conn = self._connect()
            cursor = conn.cursor()
            cursor.execute(sql, params)
            rows = cursor.fetchall()
            columns = _description_columns(getattr(cursor, "description", None))
            return [_row_to_dict(row, columns) for row in rows]
        finally:
            _safe_close(cursor)
            _safe_close(conn)

    def _query_tuples(self, sql: str) -> list[Sequence[Any]]:
        conn = None
        cursor = None
        try:
            conn = self._connect()
            cursor = conn.cursor()
            cursor.execute(sql)
            return [
                tuple(row) if not isinstance(row, dict) else tuple(row.values())
                for row in cursor.fetchall()
            ]
        finally:
            _safe_close(cursor)
            _safe_close(conn)

    def _connect(self) -> Any:
        pymysql = self._load_pymysql()
        password = self._secret_store.reveal_secret(self._conn_info.password_ref)
        extra = self._conn_info.extra
        kwargs: dict[str, Any] = {
            "host": self._conn_info.host,
            "port": self._conn_info.port,
            "user": self._conn_info.username,
            "password": password,
            "database": self._conn_info.database,
            "charset": str(extra.get("charset", "utf8mb4")),
            "connect_timeout": int(extra.get("connect_timeout", self._connect_timeout_seconds)),
            "read_timeout": int(extra.get("read_timeout", self._statement_timeout_seconds or 0))
            or None,
            "write_timeout": int(extra.get("write_timeout", self._statement_timeout_seconds or 0))
            or None,
        }
        if "ssl" in extra:
            kwargs["ssl"] = extra["ssl"]
        return pymysql.connect(**{k: v for k, v in kwargs.items() if v is not None})

    def _load_pymysql(self) -> Any:
        if self._pymysql is None:
            self._pymysql = importlib.import_module("pymysql")
        return self._pymysql

    def _sscursor_class(self) -> Any:
        return self._load_pymysql().cursors.SSCursor

    def _apply_statement_timeout(self, conn: Any) -> None:
        if self._statement_timeout_seconds <= 0:
            return
        cursor = None
        try:
            millis = int(self._statement_timeout_seconds * 1000)
            cursor = conn.cursor()
            cursor.execute("SET SESSION MAX_EXECUTION_TIME = %s", (millis,))
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
        """cursor.description → list[Column]。

        description[1] 是 PyMySQL FIELD_TYPE 整数码 → 统一 ColumnType;
        原始整数码字符串化后塞 driver_type 备查。
        """
        if not isinstance(description, Sequence) or isinstance(
            description, (str, bytes, bytearray)
        ):
            return []

        pymysql = self._load_pymysql()
        columns: list[Column] = []
        for item in description:
            if (
                not isinstance(item, Sequence)
                or isinstance(item, (str, bytes, bytearray))
                or not item
            ):
                continue
            type_code = item[1] if len(item) > 1 else None
            nullable_value = item[6] if len(item) > 6 else None
            columns.append(
                Column(
                    name=str(item[0]),
                    type=field_type_to_column_type(pymysql, type_code),
                    driver_type=str(type_code) if type_code is not None else None,
                    nullable=bool(nullable_value) if nullable_value is not None else True,
                    primary_key=False,
                )
            )
        return columns


def _validate_identifier(identifier: str) -> None:
    if not identifier or _IDENTIFIER_RE.fullmatch(identifier) is None:
        raise ValueError("Invalid database identifier")


def _quote_identifier(identifier: str) -> str:
    _validate_identifier(identifier)
    return ".".join(f"`{part}`" for part in identifier.split("."))


def _description_columns(description: object) -> list[str]:
    if not isinstance(description, Sequence) or isinstance(description, (str, bytes, bytearray)):
        return []
    columns: list[str] = []
    for item in description:
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)) and item:
            columns.append(str(item[0]))
    return columns


def _first_cell(row: object) -> object | None:
    if row is None:
        return None
    if isinstance(row, dict):
        return next(iter(row.values()), None)
    if isinstance(row, Sequence) and not isinstance(row, (str, bytes, bytearray)):
        return row[0] if row else None
    return row


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


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


__all__ = [
    "InvalidDatasourceError",
    "MySQLAdapter",
    "MySQLAdapterError",
    "QueryCancelledError",
    "QueryTimeoutError",
    "SqlGuardError",
]
