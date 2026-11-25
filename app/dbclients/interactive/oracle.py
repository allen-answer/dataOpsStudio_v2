"""Oracle 会话缝实现(设计稿 DB-oracle-oceanbase-adaptation §5)。

11.2.0.4 + Instant Client 19.29(thick)、仅 CONNECT 角色的账号实测:

- **硬取消走进程内**:control 线程调 `Connection.cancel()`,执行中的长查询约 2 秒内抛
  ORA-01013,**连接照常可续用**。不要任何权限。`ALTER SYSTEM CANCEL SQL` 要 18c+ 且要
  ALTER SYSTEM 权限,不用;
- **marker = "sid,serial#"**:`V$SESSION` 对该账号不可读(ORA-00942),
  `DBMS_DEBUG_JDWP.CURRENT_SESSION_ID / _SERIAL` 可用。取不到时退回只有 sid,
  这时破坏性兜底无从寻址,如实报 CancelChannelError;
- **破坏性兜底** `ALTER SYSTEM KILL SESSION 'sid,serial#' IMMEDIATE` 需 ALTER SYSTEM
  权限,该账号没有。没有就在执行时失败,broker 记 destroy_failed;
- **语句时限**无服务端属性;`call_timeout`(每次往返的上限,毫秒)作保险带,逐语句同步为
  「时限 + 30 秒」。超时报 DPY-4024(`.code` 为 0,分类按 full_code),连接仍可用;
- **11g 不认 FETCH FIRST**:broker 的安全限行按 Oracle 方言渲染出 FETCH FIRST,
  连到 12c 以前的库时在这里改写成 ROWNUM(与 job 路径共用 oracle_legacy_paging)。

控制通道的 `support` 恒报 AVAILABLE:硬取消不经控制连接、不需权限,前端不应提示
「取消将断开会话」。
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable, Iterator, Sequence
from typing import Any

from app.dbclients import oracle_connect
from app.dbclients.interactive.base import BaseCancelChannel, BaseInteractiveConnection
from app.dbclients.interactive.errors import OracleErrorClassifier
from app.dbclients.interactive.protocol import (
    CancelChannelError,
    ErrorClassifier,
    InteractiveCapabilities,
    ServerCancelSupport,
    StatementRequest,
)
from app.dbclients.oracle_legacy_paging import rewrite_for_pre12c, server_major_version
from app.dbclients.oracle_types import (
    description_item_to_column_type,
    description_type_to_driver_name,
)
from app.domain.datasource import DatasourceConnInfo, DbType
from app.domain.schema import Column, Row
from app.infrastructure.secretstore.protocol import SecretStore

ORACLE_DEFAULT_LOGIN_TIMEOUT_MS = 10_000
ORACLE_PING_SQL = "SELECT 1 FROM DUAL"
ORACLE_SESSION_MARKER_SQL = (
    "SELECT DBMS_DEBUG_JDWP.CURRENT_SESSION_ID || ',' || "
    "DBMS_DEBUG_JDWP.CURRENT_SESSION_SERIAL FROM DUAL"
)
ORACLE_SID_SQL = "SELECT SYS_CONTEXT('USERENV', 'SID') FROM DUAL"
# ALTER SYSTEM 不能绑定变量;marker 已按 _ORACLE_MARKER_RE 守死为纯数字。
ORACLE_DESTROY_SQL = "ALTER SYSTEM KILL SESSION '{sid},{serial}' IMMEDIATE"
# 保险带 = 语句时限 + 余量;broker 客户端计时先到先触,保险带只是兜底。
ORACLE_SAFETY_BELT_MARGIN_SECONDS = 30
# FETCH FIRST / OFFSET 自 12c 起支持。
_FETCH_FIRST_MIN_MAJOR = 12

_ORACLE_MARKER_RE = re.compile(r"^\d{1,10}(,\d{1,10})?$")

ORACLE_INTERACTIVE_CAPABILITIES = InteractiveCapabilities(
    server_cancel=True,  # 进程内 Connection.cancel(),不需权限
    server_statement_timeout=False,  # 无服务端语句超时;call_timeout 只是客户端往返上限
    session_streaming=True,  # fetchmany 分批
)


class OracleInteractiveConnection(BaseInteractiveConnection):
    """Oracle 持久会话连接。一个 broker lane 独占一条。"""

    db_type = DbType.ORACLE
    capabilities = ORACLE_INTERACTIVE_CAPABILITIES
    _driver_name = "oracledb"
    _ping_sql = ORACLE_PING_SQL
    _session_marker_sql = ORACLE_SESSION_MARKER_SQL
    _session_marker_re = _ORACLE_MARKER_RE

    def __init__(
        self,
        conn_info: DatasourceConnInfo,
        secret_store: SecretStore,
        *,
        classifier: ErrorClassifier | None = None,
        login_timeout_ms: int = ORACLE_DEFAULT_LOGIN_TIMEOUT_MS,
        oracledb_module: Any | None = None,
    ) -> None:
        super().__init__(
            conn_info,
            secret_store,
            classifier=classifier or OracleErrorClassifier(),
            login_timeout_ms=login_timeout_ms,
            driver_module=oracledb_module,
        )
        self._belt_ms: int | None = None

    def local_cancel(self) -> bool:
        conn = self._conn
        if conn is None:
            return False
        try:
            conn.cancel()
        except Exception:
            return False
        return True

    def execute(self, request: StatementRequest) -> Iterator[Row]:
        conn = self._require_open()
        major = server_major_version(getattr(conn, "version", None))
        if major is None or major >= _FETCH_FIRST_MIN_MAJOR:
            return super().execute(request)
        rewrite = rewrite_for_pre12c(request.sql)
        if not rewrite.drop_trailing_column:
            return super().execute(dataclasses.replace(request, sql=rewrite.sql))
        # 带 OFFSET 的改写多出一列行号(固定在最后),交出列与行之前去掉。
        sink = request.column_sink
        rows = super().execute(
            dataclasses.replace(
                request,
                sql=rewrite.sql,
                column_sink=_without_last_column(sink) if sink is not None else None,
            )
        )
        return _without_last_value(rows)

    def _driver_connect(self) -> Any:
        oracledb = self._load_driver()
        oracle_connect.ensure_client_mode(oracledb)
        return oracle_connect.connect(
            oracledb,
            self._conn_info,
            self._reveal_password(),
            connect_timeout_seconds=_ceil_seconds(self._login_timeout_ms),
        )

    def _read_session_marker(self, conn: Any) -> str:
        try:
            return _query_marker(conn, self._session_marker_sql, self._session_marker_re)
        except Exception:
            # DBMS_DEBUG_JDWP 被收回执行权时退回只有 sid:硬取消不受影响,只是兜底无从寻址。
            return _query_marker(conn, ORACLE_SID_SQL, self._session_marker_re)

    def _sync_server_statement_timeout(self, conn: Any, timeout_seconds: int) -> None:
        belt_ms = (
            0
            if timeout_seconds <= 0
            else (timeout_seconds + ORACLE_SAFETY_BELT_MARGIN_SECONDS) * 1000
        )
        if belt_ms == self._belt_ms:
            return
        conn.call_timeout = belt_ms
        self._belt_ms = belt_ms

    def _describe_columns(self, cursor: Any) -> list[Column]:
        return _description_to_columns(getattr(cursor, "description", None))


class OracleCancelChannel(BaseCancelChannel):
    """Oracle 控制连接:只负责破坏性兜底 KILL SESSION;硬取消在会话连接上进程内完成。"""

    db_type = DbType.ORACLE
    _driver_name = "oracledb"
    _ping_sql = ORACLE_PING_SQL
    _session_marker_sql = ORACLE_SID_SQL
    _session_marker_re = _ORACLE_MARKER_RE

    def __init__(
        self,
        conn_info: DatasourceConnInfo,
        secret_store: SecretStore,
        *,
        classifier: ErrorClassifier | None = None,
        login_timeout_ms: int = ORACLE_DEFAULT_LOGIN_TIMEOUT_MS,
        oracledb_module: Any | None = None,
    ) -> None:
        super().__init__(
            conn_info,
            secret_store,
            classifier=classifier or OracleErrorClassifier(),
            login_timeout_ms=login_timeout_ms,
            driver_module=oracledb_module,
        )

    def _driver_connect(self) -> Any:
        oracledb = self._load_driver()
        oracle_connect.ensure_client_mode(oracledb)
        return oracle_connect.connect(
            oracledb,
            self._conn_info,
            self._reveal_password(),
            connect_timeout_seconds=_ceil_seconds(self._login_timeout_ms),
        )

    def _probe(self) -> ServerCancelSupport:
        # 硬取消是会话连接上的 Connection.cancel(),不经控制连接、不需权限;
        # 控制连接建得起来(open 已验证)即可宣称 AVAILABLE。
        return ServerCancelSupport.AVAILABLE

    def _cancel_command(self, session_marker: str) -> tuple[str, Any]:
        raise CancelChannelError(
            "oracle cancels in-process via Connection.cancel(); "
            "the control connection has no cancel command"
        )

    def _destroy_command(self, session_marker: str) -> tuple[str, Any]:
        sid, _, serial = session_marker.partition(",")
        if not serial:
            raise CancelChannelError("oracle session marker has no serial#; cannot kill session")
        return ORACLE_DESTROY_SQL.format(sid=int(sid), serial=int(serial)), None


def _query_marker(conn: Any, sql: str, pattern: re.Pattern[str]) -> str:
    cursor = conn.cursor()
    try:
        cursor.execute(sql)
        row = cursor.fetchone()
    finally:
        cursor.close()
    value = row[0] if row else None
    if value is None or pattern.fullmatch(str(value).strip()) is None:
        raise ValueError("oracle session marker query returned an unexpected value")
    return str(value).strip()


def _ceil_seconds(milliseconds: int) -> int:
    return -(-milliseconds // 1000)


def _without_last_column(
    sink: Callable[[list[Column]], None],
) -> Callable[[list[Column]], None]:
    def emit(columns: list[Column]) -> None:
        sink(columns[:-1])

    return emit


def _without_last_value(rows: Iterator[Row]) -> Iterator[Row]:
    try:
        for row in rows:
            yield Row(values=row.values[:-1])
    finally:
        close = getattr(rows, "close", None)
        if close is not None:
            close()


def _description_to_columns(description: object) -> list[Column]:
    """cursor.description → list[Column](类型映射复用 oracle_types,共享深模块)。"""
    if not isinstance(description, Sequence) or isinstance(description, (str, bytes, bytearray)):
        return []
    columns: list[Column] = []
    for item in description:
        name = getattr(item, "name", None)
        if name is None and isinstance(item, Sequence) and item:
            name = item[0]
        if name is None:
            continue
        type_code = getattr(item, "type_code", None)
        if type_code is None and isinstance(item, Sequence) and len(item) > 1:
            type_code = item[1]
        scale = getattr(item, "scale", None)
        if scale is None and isinstance(item, Sequence) and len(item) > 5:
            scale = item[5]
        null_ok = getattr(item, "null_ok", None)
        if null_ok is None and isinstance(item, Sequence) and len(item) > 6:
            null_ok = item[6]
        columns.append(
            Column(
                name=str(name),
                type=description_item_to_column_type(type_code, scale),
                driver_type=description_type_to_driver_name(type_code),
                nullable=bool(null_ok) if null_ok is not None else True,
                primary_key=False,
            )
        )
    return columns


__all__ = [
    "ORACLE_DEFAULT_LOGIN_TIMEOUT_MS",
    "ORACLE_DESTROY_SQL",
    "ORACLE_INTERACTIVE_CAPABILITIES",
    "ORACLE_PING_SQL",
    "ORACLE_SAFETY_BELT_MARGIN_SECONDS",
    "ORACLE_SESSION_MARKER_SQL",
    "OracleCancelChannel",
    "OracleInteractiveConnection",
]
