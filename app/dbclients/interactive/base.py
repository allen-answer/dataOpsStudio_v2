"""MySQL / DM 会话缝实现的共享骨架。

方言差异全部落在子类钩子上(建连 kwargs、marker SQL、cursor 类、保险带、
控制命令);会话不变量(软取消 flag、批间检查、cursor 只活在栈上、连接判死)
只在这里写一遍,免得两个方言各飘一套。

R6:cursor 只在 `_stream` 的栈帧里存在,不挂到任何长命对象上。
R2:只持 `SecretRef`,明文只在 `_driver_connect` 内部短暂存在。
R5:本模块不写日志。
"""

from __future__ import annotations

import importlib
import re
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from app.dbclients.interactive.protocol import (
    CancelChannelError,
    ClassifiedError,
    ErrorCategory,
    ErrorClassifier,
    InteractiveCapabilities,
    InteractiveConnectError,
    InteractiveExecuteError,
    ServerCancelSupport,
    SessionNotOpenError,
    SoftCancelledError,
    StatementRequest,
)
from app.domain.datasource import DatasourceConnInfo, DbType
from app.domain.schema import Column, Row
from app.domain.secret import SecretKind
from app.infrastructure.secretstore.protocol import SecretStore

_SESSION_MARKER_RE = re.compile(r"^\d{1,63}$")

_SOFT_CANCEL = ClassifiedError(
    category=ErrorCategory.CANCELLED,
    driver_code=None,
    summary="soft cancel requested",
    # 客户端提前跳出取数循环 ≠ 服务端已停(设计 §4.1)。
    server_confirmed=False,
)


class InvalidDatasourceError(ValueError):
    """DatasourceConnInfo 与该方言的会话实现不匹配。"""


class BaseInteractiveConnection:
    """持久会话连接骨架(设计 §1.3-1)。"""

    db_type: DbType
    capabilities: InteractiveCapabilities
    _driver_name: str
    # 探活 SQL(DM 需要 FROM DUAL,MySQL 不需要)。
    _ping_sql: str
    # session_marker 采集 SQL(§4.1 表)。
    _session_marker_sql: str

    def __init__(
        self,
        conn_info: DatasourceConnInfo,
        secret_store: SecretStore,
        *,
        classifier: ErrorClassifier,
        login_timeout_ms: int,
        driver_module: Any | None = None,
    ) -> None:
        if conn_info.db_type is not self.db_type:
            raise InvalidDatasourceError(f"requires DbType.{self.db_type.name}")
        if conn_info.password_ref.kind is not SecretKind.DATASOURCE_PASSWORD:
            raise InvalidDatasourceError("password_ref.kind must be DATASOURCE_PASSWORD")
        if login_timeout_ms < 0:
            raise ValueError("login_timeout_ms must not be negative")

        self._conn_info = conn_info
        self._secret_store = secret_store
        self.classifier = classifier
        self._login_timeout_ms = login_timeout_ms
        self._driver = driver_module
        self._conn: Any | None = None
        self._marker: str | None = None
        self._alive = False
        self._cancel_flag = threading.Event()

    # ── 状态 ────────────────────────────────────────────────────────────────

    @property
    def is_open(self) -> bool:
        return self._conn is not None and self._alive

    @property
    def session_marker(self) -> str | None:
        return self._marker

    @property
    def soft_cancel_requested(self) -> bool:
        return self._cancel_flag.is_set()

    def request_soft_cancel(self) -> None:
        self._cancel_flag.set()

    def clear_soft_cancel(self) -> None:
        self._cancel_flag.clear()

    # ── 生命周期 ────────────────────────────────────────────────────────────

    def open(self) -> str:
        if self._conn is not None:
            raise SessionNotOpenError("connection already open")
        started_at = time.monotonic()
        try:
            conn = self._driver_connect()
        except Exception as exc:
            raise InteractiveConnectError(
                "interactive connect failed",
                self._classify_connect_failure(exc, started_at),
            ) from exc
        self._conn = conn
        self._alive = True
        try:
            self._after_connect(conn)
            marker = self._read_session_marker(conn)
        except Exception as exc:
            self.close()
            raise InteractiveConnectError(
                "interactive session marker capture failed",
                self.classifier.classify(exc),
            ) from exc
        self._marker = marker
        return marker

    def ping(self) -> bool:
        conn = self._conn
        if conn is None:
            return False
        cursor = None
        try:
            cursor = conn.cursor()
            cursor.execute(self._ping_sql)
            cursor.fetchone()
        except Exception:
            self._alive = False
            return False
        finally:
            _safe_close(cursor)
        return True

    def close(self) -> None:
        conn, self._conn = self._conn, None
        self._alive = False
        _safe_close(conn)

    # ── 执行 ────────────────────────────────────────────────────────────────

    def execute(self, request: StatementRequest) -> Iterator[Row]:
        if request.fetch_size <= 0:
            raise ValueError("fetch_size must be positive")
        conn = self._require_open()
        # 栅栏破损兜底:上一条取消的 flag 还挂着就起下一条,等于交出一条
        # "取消不掉"的语句。宁可 fail fast 也不装作能取消(设计 §4.1)。
        self._check_soft_cancel()
        # 评审修订 R2:保险带必须在 execute **之前**同步到本条语句的时限。
        self._sync_server_statement_timeout(conn, request.timeout_seconds)
        cursor = self._new_cursor(conn)
        try:
            self._run(cursor.execute, request.sql, self._driver_params(request))
            self._emit_columns(request, cursor)
        except BaseException:
            _safe_close(cursor)
            raise
        return self._stream(cursor, request)

    def _stream(self, cursor: Any, request: StatementRequest) -> Iterator[Row]:
        try:
            while True:
                # §4.1 阶梯第 1 级:批间软取消检查(沿用现有机制形态)。
                self._check_soft_cancel()
                batch = self._run(cursor.fetchmany, request.fetch_size)
                if not batch:
                    return
                for raw_row in batch:
                    yield _to_row(raw_row)
        finally:
            _safe_close(cursor)

    def _run(self, call: Any, *args: Any) -> Any:
        try:
            return call(*args)
        except Exception as exc:
            classified = self.classifier.classify(exc)
            if classified.connection_aborted:
                self._alive = False
            raise InteractiveExecuteError("interactive statement failed", classified) from exc

    def _check_soft_cancel(self) -> None:
        if self._cancel_flag.is_set():
            raise SoftCancelledError("statement soft cancelled", _SOFT_CANCEL)

    # ── 方言钩子 ────────────────────────────────────────────────────────────

    def _driver_connect(self) -> Any:
        raise NotImplementedError

    def _after_connect(self, conn: Any) -> None:
        """建连后的会话初始化(MySQL 装保险带;DM 无事可做)。"""

    def _new_cursor(self, conn: Any) -> Any:
        return conn.cursor()

    def _driver_params(self, request: StatementRequest) -> Any:
        """按调用方给的形状转交(映射→dict,序列→list),不改 paramstyle。"""
        params = request.params
        if not params:
            return None
        if isinstance(params, Mapping):
            return dict(params)
        return list(params)

    def _sync_server_statement_timeout(self, conn: Any, timeout_seconds: int) -> None:
        """服务端保险带同步。DM 无对应物 —— **不造**(设计 §4.2)。"""

    def _describe_columns(self, cursor: Any) -> list[Column]:
        raise NotImplementedError

    # ── 内部 ────────────────────────────────────────────────────────────────

    def _require_open(self) -> Any:
        conn = self._conn
        if conn is None or not self._alive:
            raise SessionNotOpenError("interactive connection is not open")
        return conn

    def _load_driver(self) -> Any:
        if self._driver is None:
            # R1:驱动 import 只发生在 app/dbclients/ 内(与既有 adapter 同形)。
            self._driver = importlib.import_module(self._driver_name)
        return self._driver

    def _reveal_password(self) -> str:
        return self._secret_store.reveal_secret(self._conn_info.password_ref)

    def _read_session_marker(self, conn: Any) -> str:
        cursor = None
        try:
            cursor = conn.cursor()
            cursor.execute(self._session_marker_sql)
            marker = _first_cell(cursor.fetchone())
        finally:
            _safe_close(cursor)
        if marker is None:
            raise InteractiveConnectError(
                "session marker query returned no value",
                ClassifiedError(category=ErrorCategory.UNKNOWN, summary="empty session marker"),
            )
        return validate_session_marker(str(marker))

    def _emit_columns(self, request: StatementRequest, cursor: Any) -> None:
        if request.column_sink is None:
            return
        request.column_sink(
            [column.model_copy(deep=True) for column in self._describe_columns(cursor)]
        )

    def _classify_connect_failure(self, exc: Exception, started_at: float) -> ClassifiedError:
        classified = self.classifier.classify(exc)
        if classified.category is not ErrorCategory.UNKNOWN or self._login_timeout_ms <= 0:
            return classified
        # 码表没覆盖、又恰好烧满了登录预算 ⇒ 归 login_timeout(设计 §2.1
        # connect_failed 归因)。这是**计时判据**,不是消息文本判据。
        elapsed_ms = (time.monotonic() - started_at) * 1000
        if elapsed_ms < self._login_timeout_ms:
            return classified
        return ClassifiedError(
            category=ErrorCategory.LOGIN_TIMEOUT,
            driver_code=classified.driver_code,
            summary=classified.summary,
        )


class BaseCancelChannel:
    """每 datasource 一条常驻控制连接(设计 §1.3-2 / §4.1)。"""

    db_type: DbType
    _driver_name: str
    _ping_sql: str
    _session_marker_sql: str

    def __init__(
        self,
        conn_info: DatasourceConnInfo,
        secret_store: SecretStore,
        *,
        classifier: ErrorClassifier,
        login_timeout_ms: int,
        driver_module: Any | None = None,
    ) -> None:
        if conn_info.db_type is not self.db_type:
            raise InvalidDatasourceError(f"requires DbType.{self.db_type.name}")
        if conn_info.password_ref.kind is not SecretKind.DATASOURCE_PASSWORD:
            raise InvalidDatasourceError("password_ref.kind must be DATASOURCE_PASSWORD")

        self._conn_info = conn_info
        self._secret_store = secret_store
        self.classifier = classifier
        self._login_timeout_ms = login_timeout_ms
        self._driver = driver_module
        self._conn: Any | None = None
        self._own_marker: str | None = None
        self._support = ServerCancelSupport.UNKNOWN

    @property
    def support(self) -> ServerCancelSupport:
        return self._support

    @property
    def own_marker(self) -> str | None:
        return self._own_marker

    def open(self) -> ServerCancelSupport:
        try:
            self._connect()
        except Exception as exc:
            self.close()
            raise CancelChannelError(
                "control connection failed", self.classifier.classify(exc)
            ) from exc
        self._support = self._probe()
        return self._support

    def cancel(self, session_marker: str) -> None:
        self._control(self._cancel_command(validate_session_marker(session_marker)))

    def destroy(self, session_marker: str) -> None:
        self._control(self._destroy_command(validate_session_marker(session_marker)))

    def ping(self) -> bool:
        conn = self._conn
        if conn is None:
            return False
        cursor = None
        try:
            cursor = conn.cursor()
            cursor.execute(self._ping_sql)
            cursor.fetchone()
        except Exception:
            return False
        finally:
            _safe_close(cursor)
        return True

    def close(self) -> None:
        conn, self._conn = self._conn, None
        _safe_close(conn)

    # ── 内部 ────────────────────────────────────────────────────────────────

    def _probe(self) -> ServerCancelSupport:
        """对自己的 marker 试调一次硬取消(自会话空转,无害,设计 §4.1)。

        本机实测:DM `CALL SP_CANCEL_SESSION_OPERATION(<自己的 sid>)` 立即返回、
        连接照常可用。探不通就 `DEGRADED` —— 探测的全部意义就是不假装能取消。
        """
        marker = self._own_marker
        if marker is None:
            return ServerCancelSupport.DEGRADED
        try:
            self._execute_control(self._cancel_command(marker))
        except Exception as exc:
            # 自取消可能反手打断探测语句本身(MySQL `KILL QUERY <自己>` 即此情形):
            # 拿到 server_confirmed 的取消码,恰恰是取消可用的最强证据。
            if not self.classifier.classify(exc).server_confirmed:
                return ServerCancelSupport.DEGRADED
        # 自会话空转/自打断之后,控制连接必须仍可用,否则不能宣称 available。
        if not self.ping():
            try:
                self._connect()
            except Exception:
                return ServerCancelSupport.DEGRADED
        return ServerCancelSupport.AVAILABLE

    def _control(self, command: tuple[str, Any]) -> None:
        self._ensure_ready()
        try:
            self._execute_control(command)
        except Exception as exc:
            raise CancelChannelError(
                "control command failed", self.classifier.classify(exc)
            ) from exc

    def _ensure_ready(self) -> None:
        """使用前 ping;失败即用当前凭据重建(天然吸收密钥轮换,设计 §4.1)。"""
        if self._conn is not None and self.ping():
            return
        self.close()
        try:
            self._connect()
        except Exception as exc:
            raise CancelChannelError(
                "control connection unavailable", self.classifier.classify(exc)
            ) from exc

    def _connect(self) -> None:
        conn = self._driver_connect()
        cursor = None
        try:
            cursor = conn.cursor()
            cursor.execute(self._session_marker_sql)
            marker = _first_cell(cursor.fetchone())
        except BaseException:
            # 半开的控制连接比没有连接更坏(ping 会假装它还在)。
            _safe_close(conn)
            raise
        finally:
            _safe_close(cursor)
        self._conn = conn
        self._own_marker = validate_session_marker(str(marker)) if marker is not None else None

    def _execute_control(self, command: tuple[str, Any]) -> None:
        conn = self._conn
        if conn is None:
            raise CancelChannelError("control connection is not open")
        sql, params = command
        cursor = None
        try:
            cursor = conn.cursor()
            cursor.execute(sql, params)
        finally:
            _safe_close(cursor)

    # ── 方言钩子 ────────────────────────────────────────────────────────────

    def _driver_connect(self) -> Any:
        raise NotImplementedError

    def _cancel_command(self, session_marker: str) -> tuple[str, Any]:
        raise NotImplementedError

    def _destroy_command(self, session_marker: str) -> tuple[str, Any]:
        raise NotImplementedError

    def _load_driver(self) -> Any:
        if self._driver is None:
            self._driver = importlib.import_module(self._driver_name)
        return self._driver

    def _reveal_password(self) -> str:
        return self._secret_store.reveal_secret(self._conn_info.password_ref)


def validate_session_marker(session_marker: str) -> str:
    """marker 必须是纯数字(DM sid / MySQL connection id 都是数值)。

    控制命令要把它拼进 SQL 语义里,这里守死格式,不给任何非数值值可乘之机。
    """
    candidate = session_marker.strip()
    if _SESSION_MARKER_RE.fullmatch(candidate) is None:
        raise CancelChannelError("invalid session marker")
    return candidate


def _first_cell(row: object) -> Any:
    if row is None:
        return None
    if isinstance(row, dict):
        return next(iter(row.values()), None)
    if isinstance(row, Sequence) and not isinstance(row, (str, bytes, bytearray)):
        return row[0] if row else None
    return row


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
    "BaseCancelChannel",
    "BaseInteractiveConnection",
    "InvalidDatasourceError",
    "validate_session_marker",
]
