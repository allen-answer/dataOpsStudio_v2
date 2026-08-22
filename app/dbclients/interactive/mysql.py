"""MySQL 会话缝实现(设计 §4.1/§4.2/§5.4)。

与 job 路径 `mysql_adapter.py` 的三点**刻意差异**(既有 adapter 一行不动):

1. **不设 `read_timeout` / `write_timeout`**。现状按语句超时设 socket 读超时,
   只有"一次性连接"才成立;会话连接上它会误杀长查询(§4.2)。连接死亡改由
   错误分类(2006/2013)兜底;
2. **保险带逐语句同步**(评审修订 R2):`max_execution_time` 在**每条语句
   execute 之前**同步为"本条语句时限 + 30s",时限 0(不限)即写 0 关闭保险带
   —— 避免"不限时"的语句被上一条留下的默认带误杀;
3. 取消是**真取消**:控制连接 `KILL QUERY :id`(硬取消,会话续用),
   `KILL CONNECTION :id` 是破坏性兜底。3024(MAX_EXECUTION_TIME)归 timeout,
   1317(ER_QUERY_INTERRUPTED)归 cancelled,两者都是 server_confirmed。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.dbclients.interactive.base import BaseCancelChannel, BaseInteractiveConnection
from app.dbclients.interactive.errors import MySQLErrorClassifier
from app.dbclients.interactive.protocol import (
    ErrorClassifier,
    InteractiveCapabilities,
)
from app.dbclients.mysql_types import field_type_to_column_type
from app.domain.datasource import DatasourceConnInfo, DbType
from app.domain.schema import Column
from app.infrastructure.secretstore.protocol import SecretStore

MYSQL_DEFAULT_LOGIN_TIMEOUT_MS = 10_000  # 设计 §4.2:connect_timeout = 10s
MYSQL_SESSION_MARKER_SQL = "SELECT CONNECTION_ID()"
MYSQL_PING_SQL = "SELECT 1"

# 硬取消 / 破坏性兜底(§4.1 表)。同账号即有权杀自己的连接。
MYSQL_CANCEL_SQL = "KILL QUERY %s"
MYSQL_DESTROY_SQL = "KILL CONNECTION %s"

MYSQL_STATEMENT_TIMEOUT_SQL = "SET SESSION MAX_EXECUTION_TIME = %s"
# 保险带 = 语句时限 + 余量;客户端计时先到先触,保险带只是兜底(§4.2)。
MYSQL_SAFETY_BELT_MARGIN_SECONDS = 30

MYSQL_INTERACTIVE_CAPABILITIES = InteractiveCapabilities(
    server_cancel=True,  # KILL QUERY
    server_statement_timeout=True,  # max_execution_time(仅 SELECT 生效)
    session_streaming=True,  # SSCursor
)


class MySQLInteractiveConnection(BaseInteractiveConnection):
    """MySQL 持久会话连接。一个 broker lane 独占一条。"""

    db_type = DbType.MYSQL
    capabilities = MYSQL_INTERACTIVE_CAPABILITIES
    _driver_name = "pymysql"
    _ping_sql = MYSQL_PING_SQL
    _session_marker_sql = MYSQL_SESSION_MARKER_SQL

    def __init__(
        self,
        conn_info: DatasourceConnInfo,
        secret_store: SecretStore,
        *,
        classifier: ErrorClassifier | None = None,
        login_timeout_ms: int = MYSQL_DEFAULT_LOGIN_TIMEOUT_MS,
        default_timeout_seconds: int = 600,
        pymysql_module: Any | None = None,
    ) -> None:
        super().__init__(
            conn_info,
            secret_store,
            classifier=classifier or MySQLErrorClassifier(),
            login_timeout_ms=login_timeout_ms,
            driver_module=pymysql_module,
        )
        self._default_timeout_seconds = max(0, default_timeout_seconds)
        self._belt_seconds: int | None = None
        self._belt_supported = True

    @property
    def server_statement_timeout_active(self) -> bool:
        """保险带是否真的装上了。

        老版本 MySQL 没有 `max_execution_time`,SET 会失败。此时**不拖垮会话**
        (§4.2 语句时限的权威机制本就是 broker 客户端计时),但如实置 False,
        绝不让调用方以为服务端还有一道兜底。
        """
        return self._belt_supported

    @property
    def safety_belt_seconds(self) -> int | None:
        """当前会话上生效的保险带秒数(0 = 已关闭,None = 尚未设置)。"""
        return self._belt_seconds

    def _driver_connect(self) -> Any:
        return _mysql_connect(
            self._load_driver(),
            self._conn_info,
            self._reveal_password(),
            self._login_timeout_ms,
        )

    def _after_connect(self, conn: Any) -> None:
        # 建连时按默认语句超时装带;逐语句同步在 execute 前发生(R2)。
        self._sync_server_statement_timeout(conn, self._default_timeout_seconds)

    def _new_cursor(self, conn: Any) -> Any:
        return conn.cursor(self._load_driver().cursors.SSCursor)

    def _sync_server_statement_timeout(self, conn: Any, timeout_seconds: int) -> None:
        if not self._belt_supported:
            return
        belt_seconds = (
            0 if timeout_seconds <= 0 else timeout_seconds + MYSQL_SAFETY_BELT_MARGIN_SECONDS
        )
        if belt_seconds == self._belt_seconds:
            return
        cursor = None
        try:
            cursor = conn.cursor()
            cursor.execute(MYSQL_STATEMENT_TIMEOUT_SQL, (belt_seconds * 1000,))
        except Exception:
            # 服务端不认这个变量 —— 记下"没有保险带"这个事实,别再反复试。
            self._belt_supported = False
            self._belt_seconds = None
            return
        finally:
            _close_quietly(cursor)
        self._belt_seconds = belt_seconds

    def _describe_columns(self, cursor: Any) -> list[Column]:
        return _description_to_columns(self._load_driver(), getattr(cursor, "description", None))


class MySQLCancelChannel(BaseCancelChannel):
    """MySQL 控制连接:KILL QUERY / KILL CONNECTION。"""

    db_type = DbType.MYSQL
    _driver_name = "pymysql"
    _ping_sql = MYSQL_PING_SQL
    _session_marker_sql = MYSQL_SESSION_MARKER_SQL

    def __init__(
        self,
        conn_info: DatasourceConnInfo,
        secret_store: SecretStore,
        *,
        classifier: ErrorClassifier | None = None,
        login_timeout_ms: int = MYSQL_DEFAULT_LOGIN_TIMEOUT_MS,
        pymysql_module: Any | None = None,
    ) -> None:
        super().__init__(
            conn_info,
            secret_store,
            classifier=classifier or MySQLErrorClassifier(),
            login_timeout_ms=login_timeout_ms,
            driver_module=pymysql_module,
        )

    def _driver_connect(self) -> Any:
        return _mysql_connect(
            self._load_driver(),
            self._conn_info,
            self._reveal_password(),
            self._login_timeout_ms,
        )

    def _cancel_command(self, session_marker: str) -> tuple[str, Any]:
        return MYSQL_CANCEL_SQL, (int(session_marker),)

    def _destroy_command(self, session_marker: str) -> tuple[str, Any]:
        return MYSQL_DESTROY_SQL, (int(session_marker),)


def _mysql_connect(
    pymysql_module: Any,
    conn_info: DatasourceConnInfo,
    password: str,
    login_timeout_ms: int,
) -> Any:
    """pymysql.connect —— 会话形态的正确接线(不设 read/write_timeout,§4.2)。"""
    extra = conn_info.extra
    kwargs: dict[str, Any] = {
        "host": conn_info.host,
        "port": conn_info.port,
        "user": conn_info.username,
        "password": password,
        "database": conn_info.database,
        "charset": str(extra.get("charset", "utf8mb4")),
    }
    if login_timeout_ms > 0:
        # PyMySQL connect_timeout 单位是秒(与 DM 的毫秒 login_timeout 不同,
        # 两边都从统一的毫秒入参换算,免得再出一次 Q3 式单位错)。
        kwargs["connect_timeout"] = max(1, round(login_timeout_ms / 1000))
    if "ssl" in extra:
        kwargs["ssl"] = extra["ssl"]
    return pymysql_module.connect(**{k: v for k, v in kwargs.items() if v is not None})


def _description_to_columns(pymysql_module: Any, description: object) -> list[Column]:
    """cursor.description → list[Column](类型映射复用 `mysql_types`,共享深模块)。"""
    if not isinstance(description, Sequence) or isinstance(description, (str, bytes, bytearray)):
        return []
    columns: list[Column] = []
    for item in description:
        if not isinstance(item, Sequence) or isinstance(item, (str, bytes, bytearray)) or not item:
            continue
        type_code = item[1] if len(item) > 1 else None
        nullable_value = item[6] if len(item) > 6 else None
        columns.append(
            Column(
                name=str(item[0]),
                type=field_type_to_column_type(pymysql_module, type_code),
                driver_type=str(type_code) if type_code is not None else None,
                nullable=bool(nullable_value) if nullable_value is not None else True,
                primary_key=False,
            )
        )
    return columns


def _close_quietly(obj: object | None) -> None:
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
    "MYSQL_CANCEL_SQL",
    "MYSQL_DEFAULT_LOGIN_TIMEOUT_MS",
    "MYSQL_DESTROY_SQL",
    "MYSQL_INTERACTIVE_CAPABILITIES",
    "MYSQL_PING_SQL",
    "MYSQL_SAFETY_BELT_MARGIN_SECONDS",
    "MYSQL_SESSION_MARKER_SQL",
    "MYSQL_STATEMENT_TIMEOUT_SQL",
    "MySQLCancelChannel",
    "MySQLInteractiveConnection",
]
