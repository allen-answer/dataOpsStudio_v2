"""DM(达梦)会话缝实现(设计 §4.1/§4.2/§5.4)。

本机 DM 8 + dmPython 2.5.32 实测结论(本 PR 复跑确认,与 DM-SPIKE 一致):

- `SELECT SESSID()` 直接可用(无需 `FROM DUAL`),返回 int 型 sid;
- 控制连接 `CALL SP_CANCEL_SESSION_OPERATION(?)` 亚毫秒返回,owner 连接立刻
  抛 **-6515**,**取消后 owner 连接照常可复用**(1+1=2 正常);
- 对自己的 sid 调同一存储过程是空转,连接无损 —— 权限自探测因此无害;
- `login_timeout` 单位是**毫秒**,与 `connection_timeout` 是独立两属性。

Q3 双重错误的修法(**只在本新缝内**,既有 `dm_adapter.py` 一行不动):

1. 登录超时接 `login_timeout`(毫秒),不再把"连接秒数 x 1000"塞
   `connection_timeout` —— 后者实测默认 0 且**不是执行超时**(设为 2 时长查询
   跑满 12.7s 未被打断,DM-SPIKE §3);
2. 不再给不存在的 `conn.callTimeout` 赋值再吞异常。DM **无可用的服务端语句
   超时属性**,语句时限唯一机制是 broker 客户端计时 + 取消通道(§4.2),
   `server_statement_timeout=False` 如实声明,不造假保险带。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.dbclients.dm_types import description_item_to_column_type
from app.dbclients.interactive.base import BaseCancelChannel, BaseInteractiveConnection
from app.dbclients.interactive.errors import DMErrorClassifier
from app.dbclients.interactive.protocol import (
    ErrorClassifier,
    InteractiveCapabilities,
)
from app.domain.datasource import DatasourceConnInfo, DbType
from app.domain.schema import Column
from app.infrastructure.secretstore.protocol import SecretStore

# 驱动默认值即 5000ms(约 6s 实到);设计 §4.2 取同值。
DM_DEFAULT_LOGIN_TIMEOUT_MS = 5000

# 官方函数(GPT 报告 §4);spike 用 `v$sessions where curr_sch=USER` 验证同值。
DM_SESSION_MARKER_SQL = "SELECT SESSID()"
DM_PING_SQL = "SELECT 1 FROM dual"

# 硬取消 / 破坏性兜底(§4.1 表)。dmPython paramstyle = qmark。
DM_CANCEL_SQL = "CALL SP_CANCEL_SESSION_OPERATION(?)"
DM_DESTROY_SQL = "CALL SP_CLOSE_SESSION(?)"

DM_INTERACTIVE_CAPABILITIES = InteractiveCapabilities(
    server_cancel=True,  # SP_CANCEL_SESSION_OPERATION 实证可用
    server_statement_timeout=False,  # DM 无对应物 —— 不造(§4.2)
    session_streaming=True,  # execute 不缓冲,fetchmany 分批(DM-SPIKE §1-Q7)
)


class DMInteractiveConnection(BaseInteractiveConnection):
    """DM 持久会话连接。一个 broker lane 独占一条。"""

    db_type = DbType.DM
    capabilities = DM_INTERACTIVE_CAPABILITIES
    _driver_name = "dmPython"
    _ping_sql = DM_PING_SQL
    _session_marker_sql = DM_SESSION_MARKER_SQL

    def __init__(
        self,
        conn_info: DatasourceConnInfo,
        secret_store: SecretStore,
        *,
        classifier: ErrorClassifier | None = None,
        login_timeout_ms: int = DM_DEFAULT_LOGIN_TIMEOUT_MS,
        dm_module: Any | None = None,
    ) -> None:
        super().__init__(
            conn_info,
            secret_store,
            classifier=classifier or DMErrorClassifier(),
            login_timeout_ms=login_timeout_ms,
            driver_module=dm_module,
        )

    def _driver_connect(self) -> Any:
        return _dm_connect(
            self._load_driver(),
            self._conn_info,
            self._reveal_password(),
            self._login_timeout_ms,
        )

    def _describe_columns(self, cursor: Any) -> list[Column]:
        return _description_to_columns(self._load_driver(), getattr(cursor, "description", None))


class DMCancelChannel(BaseCancelChannel):
    """DM 控制连接:SP_CANCEL_SESSION_OPERATION / SP_CLOSE_SESSION。"""

    db_type = DbType.DM
    _driver_name = "dmPython"
    _ping_sql = DM_PING_SQL
    _session_marker_sql = DM_SESSION_MARKER_SQL

    def __init__(
        self,
        conn_info: DatasourceConnInfo,
        secret_store: SecretStore,
        *,
        classifier: ErrorClassifier | None = None,
        login_timeout_ms: int = DM_DEFAULT_LOGIN_TIMEOUT_MS,
        dm_module: Any | None = None,
    ) -> None:
        super().__init__(
            conn_info,
            secret_store,
            classifier=classifier or DMErrorClassifier(),
            login_timeout_ms=login_timeout_ms,
            driver_module=dm_module,
        )

    def _driver_connect(self) -> Any:
        return _dm_connect(
            self._load_driver(),
            self._conn_info,
            self._reveal_password(),
            self._login_timeout_ms,
        )

    def _cancel_command(self, session_marker: str) -> tuple[str, Any]:
        return DM_CANCEL_SQL, (int(session_marker),)

    def _destroy_command(self, session_marker: str) -> tuple[str, Any]:
        return DM_DESTROY_SQL, (int(session_marker),)


def _dm_connect(
    dm_module: Any,
    conn_info: DatasourceConnInfo,
    password: str,
    login_timeout_ms: int,
) -> Any:
    """dmPython.connect —— 会话形态的正确接线。

    只设 `login_timeout`(毫秒)。**不设** `connection_timeout`(它不是执行超时,
    默认 0 即不限),**不碰** `conn.callTimeout`(该属性不存在)。
    """
    kwargs: dict[str, Any] = {
        "user": conn_info.username,
        "password": password,
        "server": conn_info.host,
        "port": conn_info.port,
    }
    if conn_info.database:
        kwargs["schema"] = conn_info.database
    if login_timeout_ms > 0:
        kwargs["login_timeout"] = login_timeout_ms
    return dm_module.connect(**kwargs)


def _description_to_columns(dm_module: Any, description: object) -> list[Column]:
    """cursor.description → list[Column](类型映射复用 `dm_types`,共享深模块)。"""
    if not isinstance(description, Sequence) or isinstance(description, (str, bytes, bytearray)):
        return []
    columns: list[Column] = []
    for item in description:
        if not isinstance(item, Sequence) or isinstance(item, (str, bytes, bytearray)) or not item:
            continue
        type_code = item[1] if len(item) > 1 else None
        scale_value = item[5] if len(item) > 5 else None
        nullable_value = item[6] if len(item) > 6 else None
        columns.append(
            Column(
                name=str(item[0]),
                type=description_item_to_column_type(dm_module, type_code, scale_value),
                driver_type=_driver_type_name(type_code),
                nullable=bool(nullable_value) if nullable_value is not None else True,
                primary_key=False,
            )
        )
    return columns


def _driver_type_name(type_code: object | None) -> str | None:
    if type_code is None:
        return None
    name = getattr(type_code, "__name__", None)
    return str(name) if name else str(type_code)


__all__ = [
    "DM_CANCEL_SQL",
    "DM_DEFAULT_LOGIN_TIMEOUT_MS",
    "DM_DESTROY_SQL",
    "DM_INTERACTIVE_CAPABILITIES",
    "DM_PING_SQL",
    "DM_SESSION_MARKER_SQL",
    "DMCancelChannel",
    "DMInteractiveConnection",
]
