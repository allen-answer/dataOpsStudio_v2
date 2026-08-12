"""DB2 DatabaseAdapter(PR-A:连接 / 流式执行 / 超时 / 探活)。

方言细节移植自 1.x 现场实战过的 Db2Dialect + drivers.py(语义逐条对照):
- 连接:最终始终用 `ibm_db_dbi`(DB-API cursor);连接串
  `DATABASE=...;HOSTNAME=...;PORT=...;PROTOCOL=TCPIP;UID=...;PWD=...;`
  + 自动补 `CONNECTTIMEOUT=<秒>`
- statement timeout:`ibm_db.set_option(handle, {SQL_ATTR_QUERY_TIMEOUT: 秒}, 1)`
  (连接级选项,单位**秒**,不换算毫秒);handle 兼容 `.conn_handler`(老版本)
  / `.conn_handle`(新版本);ibm_db import 失败安全降级为"不超时"
- 探活:`select 1 as ok from sysibm.sysdummy1`
- 错误反查:ibm_db 出错时 Python 层只见 "returned a result with an exception
  set" 这类 generic 描述,真 SQLCODE 必须 `ibm_db.stmt_errormsg()` /
  `conn_errormsg()` 反查拼进错误信息(1.x 实战修过的坑)
- 流式:普通 cursor + fetchmany(ibm_db 无 SSCursor 等价)
- DLL 注入:`add_db2_dll_directories()` 必须幂等(1.x 踩过每次连接前缀 PATH
  把 32767 上限撑爆的坑)

PR-A 开 execute_select / stream_rows / test_connection;PR-B 开 introspection
(SYSCAT.SCHEMATA/TABLES/COLUMNS/INDEXES+INDEXCOLUSE,元数据 SQL 全部
UPPER(?) 参数化,系统 schema 按 1.x 实证前缀清单排除);explain / DDL /
compare 仍占位(后续 PR 再开),对应方法 raise Db2UnsupportedOperationError
(NotImplementedError 子类,调用方按 capabilities 先行降级本不应触达)。

R1:ibm_db / ibm_db_dbi 只允许在 app/dbclients/ import(banned-api 拦截别处)。
R2:密码只从 SecretStore 解出后进连接串;连接串不落日志,错误信息统一走
_sanitize_error_text 脱敏(PWD=... 会被 REDACT)。
"""

from __future__ import annotations

import importlib
import os
import re
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

from app.dbclients.db2_types import (
    description_type_to_column_type,
    description_type_to_driver_name,
    syscat_typename_to_column_type,
    syscat_typename_to_driver_name,
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

_PING_SQL = "select 1 as ok from sysibm.sysdummy1"

# DB2 系统 schema 前缀(列 schema 时排除;1.x metadata.py:37 现场实证过的清单)。
_SYSTEM_SCHEMA_PREFIXES = ("SYS", "DB2", "ERR", "NULLID", "SQLJ", "ST_")

# SYSCAT.TABLES.TYPE 单字母 → 与 DM adapter 一致的 table_type 标签。
_TABLE_TYPE_LABELS = {"T": "TABLE", "V": "VIEW"}

_DRIVER_INSTALL_HINT = (
    "DB2 driver (ibm_db/ibm_db_dbi) is not installed. "
    "Install it with `pip install ibm_db` or the db2 extra "
    "(`uv sync --extra db2` / `pip install dataops-studio[db2]`), "
    "and make sure the IBM clidriver is available "
    "(bundled with the ibm_db wheel, or point IBM_DB_HOME / DB2CLI_HOME at it)."
)

# add_db2_dll_directories 已注册过的目录(小写),保证 os.add_dll_directory
# 级别的幂等(PATH 级幂等靠成员检查)。
_REGISTERED_DLL_DIRS: set[str] = set()

# clidriver 根目录下需要注入的子目录(1.x drivers.py:107-146 同款)。
_DLL_SUBDIRS = ("bin", "bin/amd64.VC12.CRT", "bin/amd64.VC14.CRT")


class Db2AdapterError(RuntimeError):
    """DB2 adapter 基础异常。错误信息不得包含密码或 SQL 原文。"""


class QueryCancelledError(Db2AdapterError):
    """软取消命中(DB2 无 driver-level cancel,靠安全点轮询 + statement timeout)。"""


class QueryTimeoutError(Db2AdapterError):
    """cursor 持有超过 cursor_max_hold_seconds。"""


class InvalidDatasourceError(ValueError):
    """DatasourceConnInfo 与 Db2Adapter 不匹配。"""


class Db2DriverNotInstalledError(Db2AdapterError):
    """ibm_db_dbi 不可 import(optional extra 未安装)。message 含安装指引。"""


class Db2UnsupportedOperationError(Db2AdapterError, NotImplementedError):
    """PR-A 未开的能力被调用(调用方应先查 capabilities)。"""


def add_db2_dll_directories() -> list[str]:
    """把 DB2 clidriver 的 native DLL 目录注入 Windows DLL 搜索路径。

    候选根:IBM_DB_HOME / DB2CLI_HOME 环境变量 + site-packages/clidriver
    (pip 装 ibm_db wheel 自带)+ 1.x 时代的默认安装位置。每个根注入
    bin、bin/amd64.VC12.CRT、bin/amd64.VC14.CRT。

    ★ 必须幂等:此函数在每次加载驱动前都会被调,1.x 踩过不去重导致 PATH
    重复前缀膨胀、撞 Windows 32767 字符上限直接 ValueError 的坑 ——
    PATH 成员大小写不敏感去重(Windows 路径不区分大小写),
    os.add_dll_directory 用模块级已注册集合去重。
    """
    candidates: list[Path] = []
    for env_name in ("IBM_DB_HOME", "DB2CLI_HOME"):
        env_value = os.environ.get(env_name)
        if env_value:
            candidates.append(Path(env_value))
    for site_packages in _site_packages_dirs():
        candidates.append(site_packages / "clidriver")
    candidates.append(Path.home() / "AppData/Local/Programs/Python/clidriver")

    added: list[str] = []
    for root in candidates:
        for subdir in _DLL_SUBDIRS:
            directory = root / subdir
            if not directory.is_dir():
                continue
            path = str(directory)
            current_path = os.environ.get("PATH", "")
            existing = {part.lower() for part in current_path.split(os.pathsep) if part}
            if path.lower() not in existing:
                os.environ["PATH"] = path + os.pathsep + current_path
            if path.lower() not in _REGISTERED_DLL_DIRS and hasattr(os, "add_dll_directory"):
                try:
                    os.add_dll_directory(path)
                except OSError:
                    pass
            _REGISTERED_DLL_DIRS.add(path.lower())
            added.append(path)
    return added


def _site_packages_dirs() -> list[Path]:
    out: list[Path] = []
    for entry in sys.path:
        if not entry:
            continue
        path = Path(entry)
        if path.name == "site-packages" and path.is_dir():
            out.append(path)
    return out


class Db2Adapter(DatabaseAdapter):
    """DB2 DatabaseAdapter(Preview,PR-A 范围)。"""

    capabilities = AdapterCapabilities(
        execute_select=True,
        explain=False,  # 后续 PR 再开(EXPLAIN tables 依赖,1.x 有安全降级先例)
        stream_rows=True,
        server_side_cancel=False,  # ibm_db 无 driver-level cancel(V1_AS_IS §2.8)
        list_schemas=True,  # PR-B:SYSCAT.SCHEMATA
        list_tables=True,  # PR-B:SYSCAT.TABLES(TYPE IN ('T','V'),与 DM 同列视图)
        list_columns=True,  # PR-B:SYSCAT.COLUMNS(KEYSEQ 判 PK)
        list_indexes=True,  # PR-B:SYSCAT.INDEXES + INDEXCOLUSE(UNIQUERULE 解析)
        get_table_ddl=False,  # DB2 无单条 SELECT 出 DDL 的途径(见 get_table_ddl 注释)
        compare_db_hash=False,  # PR-C
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
        ibm_db_dbi_module: Any | None = None,
        ibm_db_module: Any | None = None,
        column_sink: Callable[[list[Column]], None] | None = None,
    ) -> None:
        if conn_info.db_type is not DbType.DB2:
            raise InvalidDatasourceError("Db2Adapter requires DbType.DB2")
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
        self._ibm_db_dbi = ibm_db_dbi_module
        self._ibm_db = ibm_db_module
        self._ibm_db_load_attempted = ibm_db_module is not None
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
                self._last_server_version = f"DB2 {version}"
            return True
        except Exception as exc:
            summary = _connection_error_summary(exc)
            detail = self._driver_error_detail()
            self._last_connection_error = f"{summary} {detail}".strip()
            return False
        finally:
            _safe_close(cursor)
            _safe_close(conn)

    def list_schemas(self) -> list[Schema]:
        # SYSCAT.SCHEMATA(1.x metadata.py:54 同款);系统 schema 客户端按
        # 前缀排除(1.x _is_system_schema 同款,SQL 里省一串 NOT LIKE)。
        rows = self._query_dicts(
            """
            SELECT SCHEMANAME AS name
            FROM SYSCAT.SCHEMATA
            ORDER BY SCHEMANAME
            """
        )
        schemas: list[Schema] = []
        for row in rows:
            # SYSCAT 老版本 CHAR 定长列右补空格 → strip
            name = str(row["name"]).strip()
            if not name or name.upper().startswith(_SYSTEM_SCHEMA_PREFIXES):
                continue
            schemas.append(Schema(name=name))
        return schemas

    def list_tables(self, schema: str) -> list[Table]:
        _validate_identifier(schema)
        # SYSCAT.TABLES(1.x metadata.py:104 同款,TYPE='T');DM adapter 也列
        # 视图(ALL_VIEWS UNION),行为对齐加 TYPE='V'(单表 IN 即可,不必 UNION)。
        rows = self._query_dicts(
            """
            SELECT TABSCHEMA AS schema_name, TABNAME AS name, TYPE AS table_type
            FROM SYSCAT.TABLES
            WHERE TABSCHEMA = UPPER(?) AND TYPE IN ('T', 'V')
            ORDER BY TABNAME
            """,
            (schema,),
        )
        tables: list[Table] = []
        for row in rows:
            raw_type = str(row["table_type"]).strip().upper() if row["table_type"] else ""
            tables.append(
                Table(
                    schema_name=str(row["schema_name"]).strip(),
                    name=str(row["name"]).strip(),
                    table_type=_TABLE_TYPE_LABELS.get(raw_type, raw_type or None),
                )
            )
        return tables

    def list_columns(self, schema: str, table: str) -> list[Column]:
        _validate_identifier(schema)
        _validate_identifier(table)
        # SYSCAT.COLUMNS(弃用 1.x SYSIBM.SYSCOLUMNS 那套 —— 标着"待验证",
        # SYSCAT 是 LUW 官方 catalog 口径)。PK 用 KEYSEQ(列在主键内的序号,
        # 非主键列为 NULL/0,IBM SYSCAT.COLUMNS 文档),单表免 KEYCOLUSE join。
        rows = self._query_dicts(
            """
            SELECT
                COLNAME AS name,
                TYPENAME AS data_type,
                NULLS AS nullable,
                KEYSEQ AS keyseq,
                REMARKS AS "COMMENT"
            FROM SYSCAT.COLUMNS
            WHERE TABSCHEMA = UPPER(?) AND TABNAME = UPPER(?)
            ORDER BY COLNO
            """,
            (schema, table),
        )
        return [
            Column(
                name=str(row["name"]).strip(),
                type=syscat_typename_to_column_type(row["data_type"]),
                driver_type=syscat_typename_to_driver_name(row["data_type"]),
                # SYSCAT.COLUMNS.NULLS 编码 'Y'/'N'
                nullable=str(row["nullable"]).strip().upper() == "Y",
                primary_key=_keyseq_marks_primary(row.get("keyseq")),
                comment=_optional_str(row.get("comment")),
            )
            for row in rows
        ]

    def list_indexes(self, schema: str, table: str) -> list[Index]:
        _validate_identifier(schema)
        _validate_identifier(table)
        # SYSCAT.INDEXES + SYSCAT.INDEXCOLUSE join(1.x datasource_introspect
        # .py:293-328 同款,改参数化);UNIQUERULE:'P' 主键 / 'U' unique /
        # 'D' duplicates(普通索引),P 也算 unique。
        # COLORDER='I' 是 unique index 的 INCLUDE 列,不属于 key 列,排除。
        rows = self._query_dicts(
            """
            SELECT
                i.INDNAME AS name,
                i.UNIQUERULE AS uniquerule,
                c.COLNAME AS column_name,
                c.COLSEQ AS column_position
            FROM SYSCAT.INDEXES i
            JOIN SYSCAT.INDEXCOLUSE c
              ON c.INDSCHEMA = i.INDSCHEMA AND c.INDNAME = i.INDNAME
             AND c.COLORDER <> 'I'
            WHERE i.TABSCHEMA = UPPER(?) AND i.TABNAME = UPPER(?)
            ORDER BY i.INDNAME, c.COLSEQ
            """,
            (schema, table),
        )
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            name = str(row["name"]).strip()
            rule = str(row["uniquerule"]).strip().upper()
            item = grouped.setdefault(
                name,
                {
                    "columns": [],
                    "is_unique": rule in ("P", "U"),
                    "is_primary": rule == "P",
                },
            )
            item["columns"].append(str(row["column_name"]).strip())
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
        # 保持 capability=False:DB2 无 SHOW CREATE TABLE,也无 DM 那种
        # DBMS_METADATA.GET_DDL 单条 SELECT 等价物。官方途径是 db2look(外部
        # 工具)或 SYSPROC.DB2LK_GENERATE_DDL(CALL 存储过程 + 写 SYSTOOLS 表
        # + 高权限,只读 adapter 不合适);从 SYSCAT 手拼 DDL 易失真(默认值/
        # 分区/表空间/标识列),两条路都不稳妥 → 不做,调用方按 capabilities 降级。
        raise self._unsupported("get_table_ddl")

    def build_compare_hash_query(self, request: CompareHashRequest) -> CompareHashPlan:
        raise self._unsupported("build_compare_hash_query")

    def kill_query(self, connection_id: str) -> bool:
        # ibm_db 无 driver-level cancel(V1_AS_IS §2.8);走软取消,这里恒 False。
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
            # 保留 cause(from exc 而非 from None):worker 的 logger.exception 会带 cause
            # 链便于排障,且日志链路有 R5 脱敏 processor 兜底,原始驱动异常不会泄密。
            raise AdapterConnectionError("adapter connection failed") from exc
        self._query_timing.record_connect(_elapsed_ms(connect_started))
        try:
            self._apply_statement_timeout(conn)
            # DB2 流式读:普通 cursor + fetchmany(ibm_db 无 SSCursor 等价)
            cursor = conn.cursor()
            try:
                execute_started = time.monotonic()
                cursor.execute(sql, params or None)
                self._query_timing.record_execute(_elapsed_ms(execute_started))
            except Exception as exc:
                raise self._driver_error("DB2 execute failed", exc) from exc
            self._emit_columns(getattr(cursor, "description", None))
            while True:
                self._check_cancel()
                self._check_cursor_deadline(started_at)
                try:
                    fetch_started = time.monotonic()
                    batch = cursor.fetchmany(self._fetch_chunk_size)
                    self._query_timing.record_fetch(_elapsed_ms(fetch_started))
                except Exception as exc:
                    raise self._driver_error("DB2 fetch failed", exc) from exc
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
        params: Sequence[Any] | None = None,
    ) -> list[dict[str, Any]]:
        """introspection 用的一次性小查询:执行 + fetchall + 行转 dict。

        DB2 把不带引号的别名(AS name)折叠成大写,description 列名因而是
        NAME / SCHEMA_NAME 等;introspection 全程按小写键取值,故统一小写化
        (DM adapter 同款处理)。执行错误经 _driver_error 反查 SQLCODE 增强。
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
                cursor.execute(sql, params)
                rows = cursor.fetchall()
            except Exception as exc:
                raise self._driver_error("DB2 introspection query failed", exc) from exc
            raw_columns = _description_columns(getattr(cursor, "description", None))
            columns = [name.lower() for name in raw_columns]
            return [_row_to_dict(row, columns) for row in rows]
        finally:
            _safe_close(cursor)
            _safe_close(conn)

    def _connect(self) -> Any:
        driver = self._load_ibm_db_dbi()
        password = self._secret_store.reveal_secret(self._conn_info.password_ref)
        conn_str = self._build_conn_str(password)
        # 连接串含 PWD,禁止入日志 / 异常 message(错误信息统一走脱敏 helper)
        return driver.connect(conn_str, "", "")

    def _build_conn_str(self, password: str) -> str:
        # 1.x db2.py:87-102 同款连接串;CONNECTTIMEOUT 单位秒。
        # 不支持 extra["conn_str"] 整串覆盖(1.x 有,但整串含 PWD 会绕过
        # SecretStore 落进 datasources.extra,R2/R8 风险,2.0 不搬)。
        conn_str = (
            f"DATABASE={self._conn_info.database};"
            f"HOSTNAME={self._conn_info.host};"
            f"PORT={self._conn_info.port};"
            "PROTOCOL=TCPIP;"
            f"UID={self._conn_info.username};"
            f"PWD={password};"
        )
        connect_timeout = int(
            self._conn_info.extra.get("connect_timeout", self._connect_timeout_seconds)
        )
        if connect_timeout > 0:
            conn_str += f"CONNECTTIMEOUT={connect_timeout};"
        return conn_str

    def _load_ibm_db_dbi(self) -> Any:
        if self._ibm_db_dbi is None:
            add_db2_dll_directories()
            try:
                self._ibm_db_dbi = importlib.import_module("ibm_db_dbi")
            except ImportError as exc:
                raise Db2DriverNotInstalledError(_DRIVER_INSTALL_HINT) from exc
        return self._ibm_db_dbi

    def _load_ibm_db(self) -> Any | None:
        """ibm_db C 模块(set_option / errormsg 反查用)。import 失败安全降级 None。"""
        if not self._ibm_db_load_attempted:
            self._ibm_db_load_attempted = True
            add_db2_dll_directories()
            try:
                self._ibm_db = importlib.import_module("ibm_db")
            except ImportError:
                self._ibm_db = None
        return self._ibm_db

    def _apply_statement_timeout(self, conn: Any) -> None:
        if self._statement_timeout_seconds <= 0:
            return
        # 1.x db2.py:62-85 同款:连接级 SQL_ATTR_QUERY_TIMEOUT,单位秒(不换算);
        # handle 兼容 .conn_handler(老版本)/ .conn_handle(新版本);
        # ibm_db 不可用 / set_option 失败 → 安全降级为"不超时"
        # (cursor deadline + 软取消仍兜底)。
        ibm_db = self._load_ibm_db()
        if ibm_db is None:
            return
        handle = getattr(conn, "conn_handler", None) or getattr(conn, "conn_handle", None)
        if handle is None:
            return
        try:
            ibm_db.set_option(
                handle,
                {ibm_db.SQL_ATTR_QUERY_TIMEOUT: int(self._statement_timeout_seconds)},
                1,  # 1 = connection-level option,影响该连接所有后续 execute
            )
        except Exception:
            return

    def _fetch_server_version(self, conn: Any) -> str | None:
        ibm_db = self._load_ibm_db()
        if ibm_db is None:
            return None
        handle = getattr(conn, "conn_handler", None) or getattr(conn, "conn_handle", None)
        if handle is None:
            return None
        try:
            info = ibm_db.server_info(handle)
            version = getattr(info, "DBMS_VER", None)
            return str(version) if version else None
        except Exception:
            return None

    def _driver_error(self, message: str, exc: Exception) -> Db2AdapterError:
        """驱动异常 → 带 SQLCODE 反查明细的 Db2AdapterError(脱敏后)。

        ibm_db 报错时 Python 层常只见 "returned a result with an exception set",
        真 SQLCODE / SQLSTATE 要从 ibm_db.stmt_errormsg() / conn_errormsg() 挖。
        """
        parts = [message, _connection_error_summary(exc)]
        detail = self._driver_error_detail()
        if detail:
            parts.append(detail)
        return Db2AdapterError(" | ".join(part for part in parts if part))

    def _driver_error_detail(self) -> str:
        ibm_db = self._load_ibm_db()
        if ibm_db is None:
            return ""
        parts: list[str] = []
        for label, attr in (("stmt_errormsg", "stmt_errormsg"), ("conn_errormsg", "conn_errormsg")):
            getter = getattr(ibm_db, attr, None)
            if getter is None:
                continue
            try:
                raw = getter()
            except Exception:
                continue
            if raw:
                parts.append(f"{label}={_sanitize_error_text(str(raw))}")
        return " | ".join(parts)

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

        ibm_db_dbi 的 description[1] 是类型名字符串或 DB-API type-object
        (frozenset 子类)→ 保守归一 ColumnType(db2_types);driver_type 只放
        干净类型名,不透传原始 repr。
        """
        if not isinstance(description, Sequence) or isinstance(
            description, (str, bytes, bytearray)
        ):
            return []

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
                    type=description_type_to_column_type(type_code),
                    driver_type=description_type_to_driver_name(type_code),
                    nullable=bool(nullable_value) if nullable_value is not None else True,
                    primary_key=False,
                )
            )
        return columns

    def _unsupported(self, operation: str) -> Db2UnsupportedOperationError:
        return Db2UnsupportedOperationError(
            f"Db2Adapter does not support {operation} in this build "
            "(DB2 Preview). Check adapter.capabilities before calling."
        )


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


def _row_to_dict(row: object, columns: list[str]) -> dict[str, Any]:
    if isinstance(row, dict):
        return {str(key).lower(): value for key, value in row.items()}
    if not isinstance(row, Sequence) or isinstance(row, (str, bytes, bytearray)):
        return {}
    return {columns[index]: value for index, value in enumerate(row) if index < len(columns)}


def _keyseq_marks_primary(value: object) -> bool:
    """SYSCAT.COLUMNS.KEYSEQ:主键内序号(>0);NULL/0 = 非主键列。"""
    if value is None:
        return False
    try:
        return int(str(value)) > 0
    except (TypeError, ValueError):
        return False


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
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
    "Db2Adapter",
    "Db2AdapterError",
    "Db2DriverNotInstalledError",
    "Db2UnsupportedOperationError",
    "InvalidDatasourceError",
    "QueryCancelledError",
    "QueryTimeoutError",
    "SqlGuardError",
    "add_db2_dll_directories",
]
