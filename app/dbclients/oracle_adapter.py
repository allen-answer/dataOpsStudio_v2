"""Oracle DatabaseAdapter(Preview 档)。

驱动:python-oracledb **thin mode**(纯 Python,免装 Oracle Instant Client;
cp312 wheel 齐全,uv/pip 可直接装)。方言细节与同族 DM adapter 对齐
(DM 1.x 即 `DmDialect(OracleDialect)`,V1_AS_IS §2.3/§2.4):

- 连接:Easy Connect DSN `host:port/service_name`(service_name 缺省取
  DatasourceConnInfo.database;可由 extra["service_name"] 覆盖,或 extra["dsn"]
  整串覆盖 —— DSN 不含密码,密码始终经 SecretRef 单独走 password kwarg,R2 无虞)。
- statement timeout:连接属性 `Connection.call_timeout`(毫秒,与 DM callTimeout 同),
  设置失败安全降级"不超时"(cursor deadline + 软取消仍兜底)。
- 流式读:普通 cursor + fetchmany(arraysize 对齐 chunk;非 MySQL SSCursor)。
- 标识符:UPPER() 包裹(Oracle 默认大写),introspection SQL 用命名绑定,不拼字面量。
- 列类型:oracledb DbType 对象 → 统一 ColumnType(oracle_types,NUMBER 按 scale 判别)。
- 错误:oracledb.DatabaseError 的 `args[0]` 带 `.full_code`(ORA-xxxxx / DPY-xxxx)/
  `.code`,反查拼进结构化错误信息(脱敏后)。
- 取消:oracledb thin 有 `Connection.cancel()`,但它须由**另一线程**在语句执行
  期间打断;当前 worker 软取消模型是在 fetch 批次间轮询 cancel_check(单线程,
  无独立打断线程),故 server_side_cancel=False、kill_query 恒 False,靠安全点
  软取消 + statement timeout 兜底(与 PG/DB2/DM 一致;真 server-side cancel 接入
  是 Certified 前置项,需另起线程,不在 Preview 范围)。

Preview 范围(能力矩阵照 DB2 Preview 现档):execute_select / stream_rows /
introspection(schemas/tables/columns/indexes,复用 DM 验证过的 Oracle 目录视图
SQL)开;explain(依赖 PLAN_TABLE,真实例相关)/ get_table_ddl(DBMS_METADATA
权限相关)/ compare 仍关,调用方按 capabilities 降级。

R1:oracledb 只允许在 app/dbclients/ import(banned-api 拦别处)。
R2:密码只从 SecretStore 解出后进 password kwarg;DSN / 连接参数不落日志,
错误信息统一走 _sanitize_error_text 脱敏。
R5:连接失败日志不得含密码 / DSN 敏感段(错误摘要经脱敏 helper)。
"""

from __future__ import annotations

import importlib
import re
import time
from collections.abc import Callable, Iterator, Sequence
from typing import Any

from app.dbclients.oracle_types import (
    data_type_string_to_column_type,
    description_item_to_column_type,
    description_type_to_driver_name,
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

# Oracle 探活:所有账号都能读 dual。
_PING_SQL = "SELECT 1 AS ok FROM dual"

# Oracle 系统 schema(列 schema 时排除;比 DM 更全,含 12c+ 常见内建账号)。
_SYSTEM_SCHEMAS = frozenset(
    {
        "SYS",
        "SYSTEM",
        "SYSAUX",
        "OUTLN",
        "DBSNMP",
        "APPQOSSYS",
        "AUDSYS",
        "CTXSYS",
        "XDB",
        "WMSYS",
        "MDSYS",
        "ORDSYS",
        "ORDDATA",
        "ORDPLUGINS",
        "OLAPSYS",
        "LBACSYS",
        "DVSYS",
        "DVF",
        "GSMADMIN_INTERNAL",
        "GSMCATUSER",
        "GSMUSER",
        "REMOTE_SCHEDULER_AGENT",
        "SYSBACKUP",
        "SYSDG",
        "SYSKM",
        "SYSRAC",
        "SYS$UMF",
        "ANONYMOUS",
        "XS$NULL",
        "OJVMSYS",
        "SI_INFORMTN_SCHEMA",
        "DBSFWUSER",
        "GGSYS",
    }
)

_DRIVER_INSTALL_HINT = (
    "Oracle driver (oracledb) is not installed. "
    "Install it with `pip install oracledb` or the oracle extra "
    "(`uv sync --extra oracle` / `pip install dataops-studio[oracle]`). "
    "python-oracledb runs in thin mode by default, so no Oracle Instant Client is required."
)


class OracleAdapterError(RuntimeError):
    """Oracle adapter 基础异常。错误信息不得包含密码或 SQL 原文。"""


class QueryCancelledError(OracleAdapterError):
    """软取消命中(单线程安全点轮询 + statement timeout 兜底)。"""


class QueryTimeoutError(OracleAdapterError):
    """cursor 持有超过 cursor_max_hold_seconds。"""


class InvalidDatasourceError(ValueError):
    """DatasourceConnInfo 与 OracleAdapter 不匹配。"""


class OracleDriverNotInstalledError(OracleAdapterError):
    """oracledb 不可 import(optional extra 未安装)。message 含安装指引。"""


class OracleUnsupportedOperationError(OracleAdapterError, NotImplementedError):
    """Preview 未开的能力被调用(调用方应先查 capabilities)。"""


class OracleAdapter(DatabaseAdapter):
    """Oracle DatabaseAdapter(Preview,thin mode)。"""

    capabilities = AdapterCapabilities(
        execute_select=True,
        explain=False,  # 依赖 PLAN_TABLE(EXPLAIN PLAN FOR),真实例相关,Preview 不开
        stream_rows=True,
        server_side_cancel=False,  # thin 有 Connection.cancel(),但需另线程打断(见模块 docstring)
        list_schemas=True,
        list_tables=True,
        list_columns=True,
        list_indexes=True,
        get_table_ddl=False,  # DBMS_METADATA.GET_DDL 权限相关,Preview 不开(DM 已开,Oracle 保守)
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
        oracledb_module: Any | None = None,
        column_sink: Callable[[list[Column]], None] | None = None,
    ) -> None:
        if conn_info.db_type is not DbType.ORACLE:
            raise InvalidDatasourceError("OracleAdapter requires DbType.ORACLE")
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
        self._oracledb = oracledb_module
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

    def execute_select(self, sql: str, params: dict[str, Any]) -> Iterator[Row]:
        guarded_sql = validate_readonly_sql(sql)
        return self._stream_select(guarded_sql, params)

    def stream_rows(self, sql: str) -> Iterator[Row]:
        return self.execute_select(sql, {})

    def explain(self, sql: str) -> PlanNode:
        raise self._unsupported("explain")

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
            version = self._fetch_server_version(conn)
            if version:
                self._last_server_version = f"Oracle {version}"
            return True
        except Exception as exc:
            self._last_connection_error = self._connection_error_summary(exc)
            return False
        finally:
            _safe_close(cursor)
            _safe_close(conn)

    def list_schemas(self) -> list[Schema]:
        # ALL_USERS 全列 + 客户端按系统 schema 清单排除(与 DB2 同款,SQL 里省一串 NOT IN)。
        rows = self._query_dicts(
            """
            SELECT USERNAME AS name
            FROM ALL_USERS
            ORDER BY USERNAME
            """,
            None,
        )
        schemas: list[Schema] = []
        for row in rows:
            name = str(row["name"]).strip()
            if not name or name.upper() in _SYSTEM_SCHEMAS:
                continue
            schemas.append(Schema(name=name))
        return schemas

    def list_tables(self, schema: str) -> list[Table]:
        _validate_identifier(schema)
        # ALL_TABLES UNION ALL_VIEWS(与 DM 一致连视图一起列);命名绑定 :owner 复用一次。
        rows = self._query_dicts(
            """
            SELECT OWNER AS schema_name, TABLE_NAME AS name, 'TABLE' AS table_type
            FROM ALL_TABLES
            WHERE OWNER = UPPER(:owner)
            UNION ALL
            SELECT OWNER AS schema_name, VIEW_NAME AS name, 'VIEW' AS table_type
            FROM ALL_VIEWS
            WHERE OWNER = UPPER(:owner)
            ORDER BY name
            """,
            {"owner": schema},
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
                c.NULLABLE AS nullable,
                cc.COMMENTS AS "COMMENT"
            FROM ALL_TAB_COLUMNS c
            LEFT JOIN ALL_COL_COMMENTS cc
              ON cc.OWNER = c.OWNER
             AND cc.TABLE_NAME = c.TABLE_NAME
             AND cc.COLUMN_NAME = c.COLUMN_NAME
            WHERE c.OWNER = UPPER(:owner) AND c.TABLE_NAME = UPPER(:tbl)
            ORDER BY c.COLUMN_ID
            """,
            {"owner": schema, "tbl": table},
        )
        primary_keys = self._primary_key_columns(schema, table)
        return [
            Column(
                name=str(row["name"]),
                type=data_type_string_to_column_type(row["data_type"]),
                driver_type=str(row["data_type"]) if row["data_type"] is not None else None,
                # Oracle NULLABLE 编码 'Y'/'N'(V1_AS_IS §2.4)
                nullable=str(row["nullable"]).upper() == "Y",
                primary_key=str(row["name"]) in primary_keys,
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
                i.INDEX_NAME AS name,
                ic.COLUMN_NAME AS column_name,
                i.UNIQUENESS AS uniqueness,
                ic.COLUMN_POSITION AS column_position
            FROM ALL_INDEXES i
            JOIN ALL_IND_COLUMNS ic
              ON ic.INDEX_OWNER = i.OWNER AND ic.INDEX_NAME = i.INDEX_NAME
            WHERE i.TABLE_OWNER = UPPER(:owner) AND i.TABLE_NAME = UPPER(:tbl)
            ORDER BY i.INDEX_NAME, ic.COLUMN_POSITION
            """,
            {"owner": schema, "tbl": table},
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
        raise self._unsupported("get_table_ddl")

    def build_compare_hash_query(self, request: CompareHashRequest) -> CompareHashPlan:
        raise self._unsupported("build_compare_hash_query")

    def kill_query(self, connection_id: str) -> bool:
        # thin Connection.cancel() 需另线程打断,当前软取消模型不接;恒 False。
        return False

    def _stream_select(self, sql: str, params: dict[str, Any]) -> Iterator[Row]:
        conn = None
        cursor = None
        started_at = time.monotonic()
        self._query_timing.reset()
        connect_started = time.monotonic()
        try:
            conn = self._connect()
        except Exception as exc:
            # 保留 cause(from exc):worker 的 logger.exception 带 cause 链便于排障,
            # 日志链路有 R5 脱敏 processor 兜底,原始驱动异常不会泄密。
            raise AdapterConnectionError("adapter connection failed") from exc
        self._query_timing.record_connect(_elapsed_ms(connect_started))
        try:
            self._apply_statement_timeout(conn)
            cursor = conn.cursor()
            cursor.arraysize = self._fetch_chunk_size
            try:
                execute_started = time.monotonic()
                cursor.execute(sql, params or None)
                self._query_timing.record_execute(_elapsed_ms(execute_started))
            except Exception as exc:
                raise self._driver_error("Oracle execute failed", exc) from exc
            self._emit_columns(getattr(cursor, "description", None))
            while True:
                self._check_cancel()
                self._check_cursor_deadline(started_at)
                try:
                    fetch_started = time.monotonic()
                    batch = cursor.fetchmany(self._fetch_chunk_size)
                    self._query_timing.record_fetch(_elapsed_ms(fetch_started))
                except Exception as exc:
                    raise self._driver_error("Oracle fetch failed", exc) from exc
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
        """introspection 用的一次性小查询:执行 + fetchall + 行转 dict。

        Oracle 把不带引号的别名(AS name)折叠成大写,description 列名因而是
        NAME / DATA_TYPE 等;introspection 全程按小写键取值,故统一小写化
        (DM adapter 同款处理)。执行错误经 _driver_error 反查 ORA code 增强。
        """
        conn = None
        cursor = None
        try:
            conn = self._connect()
        except Exception as exc:
            raise AdapterConnectionError("adapter connection failed") from exc
        try:
            cursor = conn.cursor()
            try:
                cursor.execute(sql, params or None)
                rows = cursor.fetchall()
            except Exception as exc:
                raise self._driver_error("Oracle introspection query failed", exc) from exc
            raw_columns = _description_columns(getattr(cursor, "description", None))
            columns = [name.lower() for name in raw_columns]
            return [_row_to_dict(row, columns) for row in rows]
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
              AND c.OWNER = UPPER(:owner) AND c.TABLE_NAME = UPPER(:tbl)
            """,
            {"owner": schema, "tbl": table},
        )
        return {str(row["name"]) for row in rows}

    def _primary_key_index_names(self, schema: str, table: str) -> set[str]:
        rows = self._query_dicts(
            """
            SELECT c.INDEX_NAME AS name
            FROM ALL_CONSTRAINTS c
            WHERE c.CONSTRAINT_TYPE = 'P'
              AND c.OWNER = UPPER(:owner) AND c.TABLE_NAME = UPPER(:tbl)
              AND c.INDEX_NAME IS NOT NULL
            """,
            {"owner": schema, "tbl": table},
        )
        return {str(row["name"]) for row in rows}

    def _connect(self) -> Any:
        oracledb = self._load_oracledb()
        password = self._secret_store.reveal_secret(self._conn_info.password_ref)
        dsn = self._build_dsn()
        kwargs: dict[str, Any] = {
            "user": self._conn_info.username,
            "password": password,
            "dsn": dsn,
        }
        connect_timeout = int(
            self._conn_info.extra.get("connect_timeout", self._connect_timeout_seconds)
        )
        if connect_timeout > 0:
            # python-oracledb thin:tcp_connect_timeout 单位秒。
            kwargs["tcp_connect_timeout"] = connect_timeout
        # DSN / kwargs 含主机与 service,但不含密码(密码单独 kwarg);仍不入日志。
        return oracledb.connect(**kwargs)

    def _build_dsn(self) -> str:
        # extra["dsn"] 整串覆盖(Easy Connect 或 TNS descriptor;不含密码,R2 无虞);
        # 否则 Easy Connect host:port/service_name,service_name 缺省取 database。
        extra = self._conn_info.extra
        override = extra.get("dsn")
        if isinstance(override, str) and override.strip():
            return override.strip()
        service_name = extra.get("service_name") or self._conn_info.database
        return f"{self._conn_info.host}:{self._conn_info.port}/{service_name}"

    def _load_oracledb(self) -> Any:
        if self._oracledb is None:
            try:
                self._oracledb = importlib.import_module("oracledb")
            except ImportError as exc:
                raise OracleDriverNotInstalledError(_DRIVER_INSTALL_HINT) from exc
        return self._oracledb

    def _apply_statement_timeout(self, conn: Any) -> None:
        if self._statement_timeout_seconds <= 0:
            return
        # Oracle statement timeout 走连接属性 call_timeout(毫秒,round-trip 上限);
        # 设置失败安全降级"不超时"(cursor deadline + 软取消仍兜底)。
        try:
            conn.call_timeout = int(self._statement_timeout_seconds * 1000)
        except Exception:
            return

    def _fetch_server_version(self, conn: Any) -> str | None:
        # PRODUCT_COMPONENT_VERSION 普通账号可读(V$VERSION 需特权,不用)。
        cursor = None
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT version FROM product_component_version WHERE ROWNUM = 1")
            row = cursor.fetchone()
            version = _first_cell(row)
            return str(version) if version is not None else None
        except Exception:
            return None
        finally:
            _safe_close(cursor)

    def _driver_error(self, message: str, exc: Exception) -> OracleAdapterError:
        """驱动异常 → 带 ORA/DPY code 的 OracleAdapterError(脱敏后)。"""
        parts = [message, self._connection_error_summary(exc)]
        return OracleAdapterError(" | ".join(part for part in parts if part))

    def _connection_error_summary(self, exc: Exception) -> str:
        parts = [type(exc).__name__]
        code = _oracle_error_code(exc)
        if code:
            parts.append(f"code={code}")
        message = _sanitize_error_text(str(exc))
        if message:
            parts.append(f"message={message}")
        return " ".join(parts)

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

        python-oracledb 的 description 项是 FetchInfo(既支持属性访问 .name/
        .type_code/.scale/.null_ok,也支持 DB-API 7 元组索引)。type_code 是
        oracledb.DbType(→ oracle_types 归一 ColumnType);NUMBER 用 scale 区分整/小数。
        driver_type 只放干净类型名,不透传 DbType repr。
        """
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
            scale = _description_scale(item)
            nullable_value = _description_nullable(item)
            columns.append(
                Column(
                    name=name,
                    type=description_item_to_column_type(type_code, scale),
                    driver_type=description_type_to_driver_name(type_code),
                    nullable=bool(nullable_value) if nullable_value is not None else True,
                    primary_key=False,
                )
            )
        return columns

    def _unsupported(self, operation: str) -> OracleUnsupportedOperationError:
        return OracleUnsupportedOperationError(
            f"OracleAdapter does not support {operation} in this build "
            "(Oracle Preview). Check adapter.capabilities before calling."
        )


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


def _description_scale(item: object) -> object:
    value = getattr(item, "scale", None)
    if value is not None:
        return value
    if (
        isinstance(item, Sequence)
        and not isinstance(item, (str, bytes, bytearray))
        and len(item) > 5
    ):
        return item[5]
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
        return {str(key).lower(): value for key, value in row.items()}
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


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _oracle_error_code(exc: Exception) -> str | None:
    """oracledb.DatabaseError 的 args[0] 是 _Error 对象,带 full_code(ORA-xxxxx /
    DPY-xxxx)/ code(int)。反查结构化错误码;拿不到返回 None。
    """
    for arg in getattr(exc, "args", ()):
        full_code = getattr(arg, "full_code", None)
        if isinstance(full_code, str) and full_code:
            return full_code
        code = getattr(arg, "code", None)
        if isinstance(code, int) and code:
            return f"ORA-{code:05d}"
    code = _first_int_arg(exc)
    return str(code) if code is not None else None


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
    "OracleAdapter",
    "OracleAdapterError",
    "OracleDriverNotInstalledError",
    "OracleUnsupportedOperationError",
    "QueryCancelledError",
    "QueryTimeoutError",
    "SqlGuardError",
]
