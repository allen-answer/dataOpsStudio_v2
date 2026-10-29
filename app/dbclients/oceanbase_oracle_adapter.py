"""OceanBase Oracle 模式 DatabaseAdapter(Preview)。

**协议是 MySQL 协议,SQL 是 Oracle 方言**(设计稿 §4.3):执行层用 PyMySQL(经
oceanbase_oracle_driver 置 Oracle 模式能力位、接管扩展类型解码、改用 Oracle 转义),
SQL 文本层复用 Oracle 的 ALL_* 目录查询(oracle_catalog,pyformat 渲染)。

POC 结论(OB 4.3.5.4 企业版,经 OBProxy)落到这里的几处:
- 版本:握手里的 server_version 是 OBProxy 的 `5.6.25`,不能据此判断;用 `ob_version()`,
  并用 `ob_compatibility_mode` 核对租户确是 ORACLE 模式。`VERSION()` 在 Oracle 模式不存在。
- 语句超时:`SET ob_query_timeout = <微秒>` 生效(会话缺省 7 天,必须显式设);
  `MAX_EXECUTION_TIME` 不报错但无效果,不用。
- 流式读:结果集大于沿途缓冲、消费方停顿超过服务端 `net_write_timeout`(缺省 60 秒)时
  连接会被断开(客户端读空缓冲后才报 2013)。会话里把它调到不小于游标最长持有时间。
- 执行计划:`EXPLAIN <sql>` 返回单列 `Query Plan` 文本行;`EXPLAIN PLAN FOR` 语法报错、
  DBMS_XPLAN 无 PLAN_TABLE,都不用。EXPLAIN 不写库。
- DDL:`DBMS_METADATA.GET_DDL` 可用,尾部带 OB 专有表选项,原样返回。
- 错误:超时、取消、函数不存在都报 600,区分它们的内部码在消息固定前缀
  `OBE-00600: internal error code, arguments: -<n>` 里;错误摘要里单独列出。

未开:库内哈希下推(只有 DBMS_CRYPTO,约 1 万行/秒,PR-9 决定)、server-side cancel。

R1:PyMySQL 只在 app/dbclients/ import。R2:密码只从 SecretStore 解出后进 password kwarg。
R5:错误摘要统一脱敏,不含密码 / SQL 原文。
"""

from __future__ import annotations

import importlib
import re
import time
from collections.abc import Callable, Iterator, Sequence
from typing import Any

from app.dbclients import oceanbase_oracle_driver as ob_driver
from app.dbclients import oracle_catalog
from app.dbclients.oceanbase_oracle_types import field_to_column_type, field_type_name
from app.dbclients.oracle_types import data_type_string_to_column_type
from app.dbclients.protocol import AdapterConnectionError, DatabaseAdapter
from app.dbclients.query_timing import QueryTimingRecorder
from app.dbclients.sql_guard import SqlGuardError, validate_readonly_sql
from app.dbclients.sql_write import WriteResult, run_write_statement
from app.domain.capabilities import AdapterCapabilities
from app.domain.compare import CompareHashPlan, CompareHashRequest
from app.domain.datasource import DatasourceConnInfo, DbType
from app.domain.plan import PlanNode
from app.domain.schema import Column, Index, Row, Schema, Table
from app.domain.secret import SecretKind
from app.infrastructure.secretstore.protocol import SecretStore

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.$-]+$")
_SCHEMA_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]*$")
_PASSWORD_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(password|passwd|pwd)\s*[:=]\s*['\"]?[^'\",\s)]+['\"]?"
)
_URL_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s'\")]+")
_OB_INTERNAL_CODE_RE = re.compile(r"^OBE-00600: internal error code, arguments: (-\d+)")
_MAX_ERROR_SUMMARY_LENGTH = 240

_PING_SQL = "SELECT 1 AS ok FROM DUAL"
_VERSION_SQL = "SELECT ob_version() FROM DUAL"
_MODE_SQL = "SHOW VARIABLES LIKE 'ob_compatibility_mode'"

# OB-Oracle 内置账号(POC 在 ALL_USERS 里见到的)+ PUBLIC。
_SYSTEM_SCHEMAS = frozenset({"SYS", "LBACSYS", "ORAAUDITOR", "PUBLIC"})

# 服务端 net_write_timeout 缺省 60 秒;流式连接至少调到这个值以上。
_MIN_NET_WRITE_TIMEOUT_SECONDS = 60
_DDL_UNAVAILABLE_CODE = 31603
_DDL_UNAVAILABLE_MESSAGE = (
    "OceanBase DDL unavailable (ORA-31603): the table or view does not exist, "
    "or this account cannot read DDL of that schema"
)


class ObOracleAdapterError(RuntimeError):
    """OB-Oracle adapter 基础异常。错误信息不得包含密码或 SQL 原文。"""


class QueryCancelledError(ObOracleAdapterError):
    """软取消命中。"""


class QueryTimeoutError(ObOracleAdapterError):
    """cursor 持有超过 cursor_max_hold_seconds。"""


class InvalidDatasourceError(ValueError):
    """DatasourceConnInfo 与 OceanBaseOracleAdapter 不匹配。"""


class ObOracleDdlUnavailableError(ObOracleAdapterError):
    """取不到 DDL:表不存在或无权读他人 schema(ORA-31603)。"""


class OceanBaseOracleAdapter(DatabaseAdapter):
    """OceanBase Oracle 模式 DatabaseAdapter(Preview,PyMySQL SSCursor 流式)。"""

    capabilities = AdapterCapabilities(
        execute_select=True,
        execute_write=True,
        explain=True,  # OB 的 EXPLAIN <sql> 不写库
        stream_rows=True,
        server_side_cancel=False,  # KILL QUERY 已验证可用,接入在会话缝(PR-9)
        list_schemas=True,
        list_tables=True,
        list_columns=True,
        list_indexes=True,
        get_table_ddl=True,
        compare_db_hash=False,  # 只有 DBMS_CRYPTO 且慢,PR-9 决定
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
        if conn_info.db_type is not DbType.OCEANBASE_ORACLE:
            raise InvalidDatasourceError("OceanBaseOracleAdapter requires DbType.OCEANBASE_ORACLE")
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

    def execute_write(self, sql: str, params: dict[str, Any]) -> WriteResult:
        """单条 DML / DDL,一条语句一个事务。

        调用方(worker)已过 `classify_executable_sql` 并按 operation_policy.allow_dml 放行;
        这里不再做只读校验。
        """
        self._query_timing.reset()
        self._query_timing.mark_connect_started()
        connect_started = time.monotonic()
        try:
            conn = self._connect()
        except Exception as exc:
            raise AdapterConnectionError("adapter connection failed") from exc
        self._query_timing.record_connect(_elapsed_ms(connect_started))
        try:
            self._query_timing.mark_execute_started()
            execute_started = time.monotonic()
            result = run_write_statement(
                conn,
                sql,
                params,
                check_cancel=self._check_cancel,
                execute=self._execute_write_cursor,
            )
            self._query_timing.record_execute(_elapsed_ms(execute_started))
            return result
        finally:
            _safe_close(conn)

    def _execute_write_cursor(self, cursor: Any, sql: str, params: dict[str, Any] | None) -> None:
        try:
            if params is None:
                cursor.execute(sql)
            else:
                cursor.execute(sql, params)
        except Exception as exc:
            raise _driver_error("OceanBase execute failed", exc) from exc

    def explain(self, sql: str) -> PlanNode:
        guarded_sql = validate_readonly_sql(sql)
        rows = self._query_rows(f"EXPLAIN {guarded_sql}", None, lowercase_keys=False)
        return PlanNode(operation="EXPLAIN", details={"rows": rows})

    def test_connection(self) -> bool:
        conn = None
        cursor = None
        self._last_server_version = None
        self._last_connection_error = None
        try:
            conn = self._connect()
            cursor = conn.cursor()
            cursor.execute(_PING_SQL)
            cursor.fetchone()
            cursor.execute(_MODE_SQL)
            mode = _second_cell(cursor.fetchone())
            if mode is not None and str(mode).upper() != "ORACLE":
                self._last_connection_error = (
                    f"OceanBase tenant is in {str(mode).upper()} mode, not ORACLE; "
                    "use the OceanBase (MySQL mode) datasource type instead"
                )
                return False
            cursor.execute(_VERSION_SQL)
            version = _first_cell(cursor.fetchone())
            if version:
                self._last_server_version = f"OceanBase {version} (Oracle mode)"
            return True
        except Exception as exc:
            self._last_connection_error = _connection_error_summary(exc)
            return False
        finally:
            _safe_close(cursor)
            _safe_close(conn)

    def list_schemas(self) -> list[Schema]:
        rows = self._query_rows(oracle_catalog.LIST_SCHEMAS.render("pyformat"), None)
        schemas: list[Schema] = []
        for row in rows:
            name = str(row["name"]).strip()
            if not name or name.upper() in _SYSTEM_SCHEMAS:
                continue
            schemas.append(Schema(name=name))
        return schemas

    def list_tables(self, schema: str) -> list[Table]:
        _validate_identifier(schema)
        rows = self._query_rows(oracle_catalog.LIST_TABLES.render("pyformat"), {"owner": schema})
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
        params = {"owner": schema, "tbl": table}
        rows = self._query_rows(oracle_catalog.LIST_COLUMNS.render("pyformat"), params)
        primary_keys = {
            str(row["name"])
            for row in self._query_rows(
                oracle_catalog.PRIMARY_KEY_COLUMNS.render("pyformat"), params
            )
        }
        return [
            Column(
                name=str(row["name"]),
                type=data_type_string_to_column_type(row["data_type"]),
                driver_type=str(row["data_type"]) if row["data_type"] is not None else None,
                nullable=str(row["nullable"]).upper() == "Y",
                primary_key=str(row["name"]) in primary_keys,
                comment=_optional_str(row.get("comment")),
            )
            for row in rows
        ]

    def list_indexes(self, schema: str, table: str) -> list[Index]:
        _validate_identifier(schema)
        _validate_identifier(table)
        params = {"owner": schema, "tbl": table}
        rows = self._query_rows(oracle_catalog.LIST_INDEXES.render("pyformat"), params)
        primary_index_names = {
            str(row["name"])
            for row in self._query_rows(
                oracle_catalog.PRIMARY_KEY_INDEX_NAMES.render("pyformat"), params
            )
        }
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            name = str(row["name"])
            item = grouped.setdefault(
                name,
                {
                    "columns": [],
                    "is_unique": str(row["uniqueness"]).upper() == "UNIQUE",
                    "is_primary": name in primary_index_names,
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
        try:
            rows = self._query_rows(
                oracle_catalog.TABLE_DDL.render("pyformat"), {"tbl": table, "owner": schema}
            )
        except ObOracleAdapterError as exc:
            if _first_int_arg(exc.__cause__) == _DDL_UNAVAILABLE_CODE:
                raise ObOracleDdlUnavailableError(_DDL_UNAVAILABLE_MESSAGE) from exc.__cause__
            raise
        if not rows:
            # ALL_OBJECTS 里没有这张表或视图:不存在或当前账号看不见,与 ORA-31603 同类
            raise ObOracleDdlUnavailableError(_DDL_UNAVAILABLE_MESSAGE)
        ddl = next(iter(rows[0].values()), None)
        if not ddl:
            raise ObOracleAdapterError("DBMS_METADATA.GET_DDL returned no DDL")
        return str(ddl).strip()

    def build_compare_hash_query(self, request: CompareHashRequest) -> CompareHashPlan:
        raise ObOracleAdapterError(
            "OceanBaseOracleAdapter has no database-side compare hash in this build "
            "(check adapter.capabilities.compare_db_hash before calling)"
        )

    def kill_query(self, connection_id: str) -> bool:
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
            cursor = conn.cursor(self._load_pymysql().cursors.SSCursor)
            try:
                self._query_timing.mark_execute_started()
                execute_started = time.monotonic()
                cursor.execute(sql, params or None)
                self._query_timing.record_execute(_elapsed_ms(execute_started))
            except Exception as exc:
                raise _driver_error("OceanBase execute failed", exc) from exc
            self._emit_columns(cursor)
            while True:
                self._check_cancel()
                self._check_cursor_deadline(started_at)
                try:
                    fetch_started = time.monotonic()
                    batch = cursor.fetchmany(self._fetch_chunk_size)
                    self._query_timing.record_fetch(
                        _elapsed_ms(fetch_started), row_count=len(batch)
                    )
                except Exception as exc:
                    raise _driver_error("OceanBase fetch failed", exc) from exc
                if not batch:
                    break
                for raw_row in batch:
                    yield _to_row(raw_row)
        finally:
            _safe_close(cursor)
            _safe_close(conn)

    def _query_rows(
        self,
        sql: str,
        params: dict[str, Any] | None,
        *,
        lowercase_keys: bool = True,
    ) -> list[dict[str, Any]]:
        """一次性小查询。Oracle 把不带引号的别名折叠成大写,introspection 按小写键取值。"""
        conn = None
        cursor = None
        try:
            conn = self._connect()
        except Exception as exc:
            raise AdapterConnectionError("adapter connection failed") from exc
        try:
            cursor = conn.cursor()
            try:
                cursor.execute(sql, params)
                rows = cursor.fetchall()
            except Exception as exc:
                raise _driver_error("OceanBase query failed", exc) from exc
            columns = _description_columns(getattr(cursor, "description", None))
            if lowercase_keys:
                columns = [name.lower() for name in columns]
            return [_row_to_dict(row, columns) for row in rows]
        finally:
            _safe_close(cursor)
            _safe_close(conn)

    def _connect(self) -> Any:
        pymysql = self._load_pymysql()
        extra = self._conn_info.extra
        user = ob_driver.assemble_username(self._conn_info.username, extra)
        password = self._secret_store.reveal_secret(self._conn_info.password_ref)
        io_timeout = int(extra.get("read_timeout", self._statement_timeout_seconds or 0)) or None
        kwargs: dict[str, Any] = {
            "host": self._conn_info.host,
            "port": self._conn_info.port,
            "user": user,
            "password": password,
            # 库字符集可能是 ZHS16GBK;连接固定 utf8mb4,服务端按 UTF-8 返回(POC 实测)。
            "charset": str(extra.get("charset", "utf8mb4")),
            "connect_timeout": int(extra.get("connect_timeout", self._connect_timeout_seconds)),
            "read_timeout": io_timeout,
            "write_timeout": io_timeout,
        }
        conn = ob_driver.connect(pymysql, **{k: v for k, v in kwargs.items() if v is not None})
        try:
            self._prepare_session(conn)
        except Exception:
            _safe_close(conn)
            raise
        return conn

    def _prepare_session(self, conn: Any) -> None:
        statements: list[str] = []
        if self._statement_timeout_seconds > 0:
            statements.append(
                f"SET ob_query_timeout = {int(self._statement_timeout_seconds * 1_000_000)}"
            )
        net_write_timeout = max(
            _MIN_NET_WRITE_TIMEOUT_SECONDS,
            self._cursor_max_hold_seconds,
            self._statement_timeout_seconds,
        )
        statements.append(f"SET net_write_timeout = {int(net_write_timeout)}")
        schema = self._conn_info.database
        if schema:
            if _SCHEMA_NAME_RE.fullmatch(schema) is None:
                raise InvalidDatasourceError("Invalid OceanBase schema name")
            statements.append(f'ALTER SESSION SET CURRENT_SCHEMA = "{schema.upper()}"')
        cursor = conn.cursor()
        try:
            for statement in statements:
                cursor.execute(statement)
        finally:
            _safe_close(cursor)

    def _load_pymysql(self) -> Any:
        if self._pymysql is None:
            self._pymysql = importlib.import_module("pymysql")
        return self._pymysql

    def _check_cancel(self) -> None:
        if self._cancel_check is not None and self._cancel_check():
            raise QueryCancelledError("Query cancelled")

    def _check_cursor_deadline(self, started_at: float) -> None:
        if self._cursor_max_hold_seconds <= 0:
            return
        if time.monotonic() - started_at > self._cursor_max_hold_seconds:
            raise QueryTimeoutError("Query cursor hold time exceeded")

    def _emit_columns(self, cursor: Any) -> None:
        if self._column_sink is None:
            return
        columns = _description_to_columns(
            getattr(cursor, "description", None), ob_driver.field_charsets(cursor)
        )
        self._column_sink([column.model_copy(deep=True) for column in columns])


def _description_to_columns(description: object, charsets: list[int | None]) -> list[Column]:
    """cursor.description(+ 各列 charsetnr)→ list[Column]。"""
    if not isinstance(description, Sequence) or isinstance(description, (str, bytes, bytearray)):
        return []
    columns: list[Column] = []
    for index, item in enumerate(description):
        if not isinstance(item, Sequence) or isinstance(item, (str, bytes, bytearray)) or not item:
            continue
        type_code = item[1] if len(item) > 1 else None
        scale = item[5] if len(item) > 5 else None
        nullable_value = item[6] if len(item) > 6 else None
        charsetnr = charsets[index] if index < len(charsets) else None
        columns.append(
            Column(
                name=str(item[0]),
                type=field_to_column_type(type_code, scale=scale, charsetnr=charsetnr),
                driver_type=field_type_name(type_code, charsetnr=charsetnr),
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


def _second_cell(row: object) -> object | None:
    """SHOW VARIABLES 的 (VARIABLE_NAME, VALUE) 取 VALUE。"""
    if isinstance(row, Sequence) and not isinstance(row, (str, bytes, bytearray)):
        return row[1] if len(row) > 1 else None
    return None


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _driver_error(message: str, exc: Exception) -> ObOracleAdapterError:
    return ObOracleAdapterError(f"{message} | {_connection_error_summary(exc)}")


def _connection_error_summary(exc: Exception) -> str:
    parts = [type(exc).__name__]
    code = _first_int_arg(exc)
    if code is not None:
        parts.append(f"code={code}")
    message = _first_str_arg(exc) or str(exc)
    internal = _OB_INTERNAL_CODE_RE.match(message.strip())
    if internal is not None:
        # 600 是超时 / 取消 / 函数不存在等的统一外层码;内部码才说明是哪种(-4012 超时、
        # -5065 取消、-5055 不存在)。
        parts.append(f"ob_internal_code={internal.group(1)}")
    sanitized = _sanitize_error_text(message)
    if sanitized:
        parts.append(f"message={sanitized}")
    return " ".join(parts)


def _first_int_arg(exc: BaseException | None) -> int | None:
    for arg in getattr(exc, "args", ()):
        if isinstance(arg, int):
            return arg
    return None


def _first_str_arg(exc: Exception) -> str | None:
    for arg in getattr(exc, "args", ()):
        if isinstance(arg, str):
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


def _elapsed_ms(started_at: float) -> int:
    return max(0, round((time.monotonic() - started_at) * 1000))


__all__ = [
    "InvalidDatasourceError",
    "ObOracleAdapterError",
    "ObOracleDdlUnavailableError",
    "OceanBaseOracleAdapter",
    "QueryCancelledError",
    "QueryTimeoutError",
    "SqlGuardError",
]
