from __future__ import annotations

import importlib
import re
import time
from collections.abc import Callable, Iterator, Sequence
from typing import Any

from app.dbclients.dm_types import (
    data_type_string_to_column_type,
    description_item_to_column_type,
)
from app.dbclients.protocol import AdapterConnectionError, DatabaseAdapter
from app.dbclients.sql_guard import SqlGuardError, validate_readonly_sql
from app.domain.capabilities import AdapterCapabilities
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

# DM 系统 schema(列 schema 时排除,与 V1 dialect 习惯一致)。
_SYSTEM_SCHEMAS = (
    "SYS",
    "SYSDBA",
    "SYSAUDITOR",
    "SYSSSO",
    "SYSSEC",
    "CTISYS",
)


class DMAdapterError(RuntimeError):
    """DM adapter 基础异常。错误信息不得包含密码或 SQL 原文。"""


class QueryCancelledError(DMAdapterError):
    """软取消命中(DM 无 driver-level cancel,靠安全点轮询 + statement timeout)。"""


class QueryTimeoutError(DMAdapterError):
    """cursor 持有超过 cursor_max_hold_seconds。"""


class InvalidDatasourceError(ValueError):
    """DatasourceConnInfo 与 DMAdapter 不匹配。"""


class DMAdapter(DatabaseAdapter):
    """DM(达梦)DatabaseAdapter。

    方言细节移植自 1.x `DmDialect(OracleDialect)`(V1_AS_IS §2.3/§2.4):
    - 标识符:UPPER() 包裹 + 双引号 quote(MySQL 是反引号原样)
    - 连接探活:`SELECT 1 AS ok FROM dual`
    - statement timeout:连接属性 `conn.callTimeout`(毫秒),不是会话 SQL
    - 流式读:普通 cursor + `fetchmany`(不是 MySQL 的 SSCursor)
    - 列元数据:`all_tab_columns`(无 comment join,与 V1 DM 一致)
    - 取消:软取消(server_side_cancel=False),不假设能真 kill
    """

    capabilities = AdapterCapabilities(
        execute_select=True,
        explain=True,
        stream_rows=True,
        server_side_cancel=False,  # DM 无 driver-level cancel(V1_AS_IS §2.8)
        list_schemas=True,
        list_tables=True,
        list_columns=True,
        list_indexes=True,
        get_table_ddl=True,
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
        dm_module: Any | None = None,
        column_sink: Callable[[list[Column]], None] | None = None,
    ) -> None:
        if conn_info.db_type is not DbType.DM:
            raise InvalidDatasourceError("DMAdapter requires DbType.DM")
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
        self._dm = dm_module
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
        # DM 的执行计划语法是 `EXPLAIN FOR <sql>`(裸 `EXPLAIN <sql>` 报 -2002),
        # 直接返回执行计划文本行。
        rows = self._query_dicts(f"EXPLAIN FOR {guarded_sql}", None)
        return PlanNode(operation="EXPLAIN", details={"rows": rows})

    def test_connection(self) -> bool:
        conn = None
        cursor = None
        self._last_server_version = None
        self._last_connection_error = None
        try:
            conn = self._connect()
            cursor = conn.cursor()
            cursor.execute("SELECT 1 AS ok FROM dual")
            cursor.fetchone()
            version = self._fetch_server_version(conn)
            if version:
                self._last_server_version = f"DM {version}"
            return True
        except Exception as exc:
            self._last_connection_error = _connection_error_summary(exc)
            return False
        finally:
            _safe_close(cursor)
            _safe_close(conn)

    def list_schemas(self) -> list[Schema]:
        placeholders = ", ".join(["?"] * len(_SYSTEM_SCHEMAS))
        rows = self._query_dicts(
            f"""
            SELECT USERNAME AS name
            FROM ALL_USERS
            WHERE USERNAME NOT IN ({placeholders})
            ORDER BY USERNAME
            """,
            tuple(_SYSTEM_SCHEMAS),
        )
        return [Schema(name=str(row["name"])) for row in rows]

    def list_tables(self, schema: str) -> list[Table]:
        _validate_identifier(schema)
        rows = self._query_dicts(
            """
            SELECT OWNER AS schema_name, TABLE_NAME AS name, 'TABLE' AS table_type
            FROM ALL_TABLES
            WHERE OWNER = UPPER(?)
            UNION ALL
            SELECT OWNER AS schema_name, VIEW_NAME AS name, 'VIEW' AS table_type
            FROM ALL_VIEWS
            WHERE OWNER = UPPER(?)
            ORDER BY name
            """,
            (schema, schema),
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
                c.COLUMN_NAME AS name,
                c.DATA_TYPE AS data_type,
                c.NULLABLE AS nullable
            FROM ALL_TAB_COLUMNS c
            WHERE c.OWNER = UPPER(?) AND c.TABLE_NAME = UPPER(?)
            ORDER BY c.COLUMN_ID
            """,
            (schema, table),
        )
        primary_keys = self._primary_key_columns(schema, table)
        return [
            Column(
                name=str(row["name"]),
                type=data_type_string_to_column_type(row["data_type"]),
                driver_type=str(row["data_type"]) if row["data_type"] is not None else None,
                # Oracle/DM NULLABLE 编码 'Y'/'N'(V1_AS_IS §2.4)
                nullable=str(row["nullable"]).upper() == "Y",
                primary_key=str(row["name"]) in primary_keys,
            )
            for row in rows
        ]

    def list_indexes(self, schema: str, table: str) -> list[Index]:
        _validate_identifier(schema)
        _validate_identifier(table)
        rows = self._query_dicts(
            """
            SELECT
                i.INDEX_NAME AS name,
                ic.COLUMN_NAME AS column_name,
                i.UNIQUENESS AS uniqueness,
                ic.COLUMN_POSITION AS column_position
            FROM ALL_INDEXES i
            JOIN ALL_IND_COLUMNS ic
              ON ic.INDEX_OWNER = i.OWNER AND ic.INDEX_NAME = i.INDEX_NAME
            WHERE i.TABLE_OWNER = UPPER(?) AND i.TABLE_NAME = UPPER(?)
            ORDER BY i.INDEX_NAME, ic.COLUMN_POSITION
            """,
            (schema, table),
        )
        primary_keys = self._primary_key_index_names(schema, table)
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            name = str(row["name"])
            item = grouped.setdefault(
                name,
                {
                    "columns": [],
                    "is_unique": str(row["uniqueness"]).upper() == "UNIQUE",
                    "is_primary": name in primary_keys,
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
        # DM 兼容 Oracle DBMS_METADATA.GET_DDL(标识符大写 + 参数化,不拼字面量)
        rows = self._query_tuples(
            "SELECT DBMS_METADATA.GET_DDL('TABLE', UPPER(?), UPPER(?)) FROM dual",
            (table, schema),
        )
        if not rows or not rows[0]:
            raise DMAdapterError("DBMS_METADATA.GET_DDL returned no DDL")
        return str(rows[0][0])

    def kill_query(self, connection_id: str) -> bool:
        # DM 无 driver-level cancel(V1_AS_IS §2.8);走软取消,这里恒 False。
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
            # DM 流式读:普通 cursor + fetchmany(不是 MySQL SSCursor),V1_AS_IS §2.7
            cursor = conn.cursor()
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
            # DM 把不带引号的别名(AS name)折叠成大写,description 列名因而是
            # NAME / DATA_TYPE 等;introspection 全程按小写键取值,故此处统一小写化
            # (一处修,list_schemas/tables/columns/indexes/PK 全链路受益)。
            raw_columns = _description_columns(getattr(cursor, "description", None))
            columns = [name.lower() for name in raw_columns]
            return [_row_to_dict(row, columns) for row in rows]
        finally:
            _safe_close(cursor)
            _safe_close(conn)

    def _query_tuples(
        self,
        sql: str,
        params: Sequence[Any] | None = None,
    ) -> list[Sequence[Any]]:
        conn = None
        cursor = None
        try:
            conn = self._connect()
            cursor = conn.cursor()
            cursor.execute(sql, params)
            return [
                tuple(row) if not isinstance(row, dict) else tuple(row.values())
                for row in cursor.fetchall()
            ]
        finally:
            _safe_close(cursor)
            _safe_close(conn)

    def _primary_key_columns(self, schema: str, table: str) -> set[str]:
        rows = self._query_dicts(
            """
            SELECT cc.COLUMN_NAME AS name
            FROM ALL_CONSTRAINTS c
            JOIN ALL_CONS_COLUMNS cc
              ON cc.OWNER = c.OWNER AND cc.CONSTRAINT_NAME = c.CONSTRAINT_NAME
            WHERE c.CONSTRAINT_TYPE = 'P'
              AND c.OWNER = UPPER(?) AND c.TABLE_NAME = UPPER(?)
            """,
            (schema, table),
        )
        return {str(row["name"]) for row in rows}

    def _primary_key_index_names(self, schema: str, table: str) -> set[str]:
        rows = self._query_dicts(
            """
            SELECT c.INDEX_NAME AS name
            FROM ALL_CONSTRAINTS c
            WHERE c.CONSTRAINT_TYPE = 'P'
              AND c.OWNER = UPPER(?) AND c.TABLE_NAME = UPPER(?)
              AND c.INDEX_NAME IS NOT NULL
            """,
            (schema, table),
        )
        return {str(row["name"]) for row in rows}

    def _connect(self) -> Any:
        dm = self._load_dm()
        password = self._secret_store.reveal_secret(self._conn_info.password_ref)
        extra = self._conn_info.extra
        # dmPython.connect 接口(V1_AS_IS §2.4):server / port / user / password,
        # schema 指目标 schema(DatasourceConnInfo.database 映射 schema)。
        kwargs: dict[str, Any] = {
            "user": self._conn_info.username,
            "password": password,
            "server": self._conn_info.host,
            "port": self._conn_info.port,
        }
        if self._conn_info.database:
            kwargs["schema"] = self._conn_info.database
        connect_timeout = int(extra.get("connect_timeout", self._connect_timeout_seconds))
        if connect_timeout > 0:
            # dmPython 用毫秒 login timeout
            kwargs["connection_timeout"] = connect_timeout * 1000
        return dm.connect(**kwargs)

    def _load_dm(self) -> Any:
        if self._dm is None:
            self._dm = importlib.import_module("dmPython")
        return self._dm

    def _apply_statement_timeout(self, conn: Any) -> None:
        if self._statement_timeout_seconds <= 0:
            return
        # Oracle/DM statement timeout 走连接属性 conn.callTimeout(毫秒),V1_AS_IS §2.4。
        try:
            conn.callTimeout = int(self._statement_timeout_seconds * 1000)
        except Exception:
            return

    def _fetch_server_version(self, conn: Any) -> str | None:
        cursor = None
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT BANNER FROM V$VERSION WHERE ROWNUM = 1")
            row = cursor.fetchone()
            version = _first_cell(row)
            return str(version) if version is not None else None
        except Exception:
            return None
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

        DB-API description 项形如 (name, type_code, display_size, internal_size,
        precision, scale, null_ok)。dmPython 的 type_code 是 type-object
        (如 dmPython.NUMBER)→ 统一 ColumnType;NUMBER / DECIMAL 用 scale(idx5)
        区分整数与小数。原始类型字符串化后塞 driver_type 备查。
        """
        if not isinstance(description, Sequence) or isinstance(
            description, (str, bytes, bytearray)
        ):
            return []

        dm = self._load_dm()
        columns: list[Column] = []
        for item in description:
            if (
                not isinstance(item, Sequence)
                or isinstance(item, (str, bytes, bytearray))
                or not item
            ):
                continue
            type_code = item[1] if len(item) > 1 else None
            scale_value = item[5] if len(item) > 5 else None
            nullable_value = item[6] if len(item) > 6 else None
            columns.append(
                Column(
                    name=str(item[0]),
                    type=description_item_to_column_type(dm, type_code, scale_value),
                    driver_type=str(type_code) if type_code is not None else None,
                    nullable=bool(nullable_value) if nullable_value is not None else True,
                    primary_key=False,
                )
            )
        return columns


def _validate_identifier(identifier: str) -> None:
    if not identifier or _IDENTIFIER_RE.fullmatch(identifier) is None:
        raise ValueError("Invalid database identifier")


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
    "DMAdapter",
    "DMAdapterError",
    "InvalidDatasourceError",
    "QueryCancelledError",
    "QueryTimeoutError",
    "SqlGuardError",
]
