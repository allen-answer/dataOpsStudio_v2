"""Oracle 建连的共享深模块:thick 初始化、DSN、取值处理器。

job 路径的 OracleAdapter 与会话控制台的 OracleInteractiveConnection 都从这里建连。
thick / thin 是**进程级**且只能定一次,两条路径必须共用同一份初始化状态,否则后建连的
一方可能二次调 init_oracle_client,或在 thin 连接之后才切 thick。

R1:oracledb 模块由调用方传入(它们负责 import),本模块不 import 驱动。
R2:密码只作为 connect 的参数经过,不落任何属性;DSN 不含密码。
"""

from __future__ import annotations

import decimal
import os
import threading
from collections.abc import Callable
from typing import Any

from app.domain.datasource import DatasourceConnInfo

# thick mode 开关:指向 Oracle Instant Client 目录(11g 需要;19c 是支持 11.2 的最高客户端)。
CLIENT_DIR_ENV = "DATAOPS_ORACLE_CLIENT_DIR"

# init_oracle_client 每进程只能成功调一次;结果(含失败)缓存,失败时每次连接都如实报错。
_client_init_lock = threading.Lock()
_client_init_done = False
_client_init_error: BaseException | None = None


class OracleClientInitFailure(RuntimeError):
    """thick 初始化失败(已缓存)。`cause` 是 init_oracle_client 抛出的原始异常。"""

    def __init__(self, cause: BaseException) -> None:
        super().__init__("Oracle thick mode initialization failed")
        self.cause = cause


def ensure_client_mode(oracledb: Any) -> None:
    """设了 DATAOPS_ORACLE_CLIENT_DIR 时,本进程首次加载驱动调一次 init_oracle_client。

    必须在本进程第一个 Oracle 连接之前;所有 Oracle 建连都先经这里。失败结果缓存,之后
    每次连接都抛同一个 OracleClientInitFailure,不静默回落 thin(回落会让 11g 报更难懂的
    DPY-3010)。
    """
    global _client_init_done, _client_init_error
    lib_dir = os.environ.get(CLIENT_DIR_ENV, "").strip()
    if not lib_dir:
        return
    with _client_init_lock:
        if not _client_init_done:
            _client_init_done = True
            try:
                oracledb.init_oracle_client(lib_dir=lib_dir)
            except Exception as exc:
                _client_init_error = exc
        if _client_init_error is not None:
            raise OracleClientInitFailure(_client_init_error)


def build_dsn(conn_info: DatasourceConnInfo) -> str:
    """extra["dsn"] 整串覆盖(Easy Connect 或 TNS descriptor;不含密码);
    否则 Easy Connect host:port/service_name,service_name 缺省取 database。
    """
    extra = conn_info.extra
    override = extra.get("dsn")
    if isinstance(override, str) and override.strip():
        return override.strip()
    service_name = extra.get("service_name") or conn_info.database
    return f"{conn_info.host}:{conn_info.port}/{service_name}"


def connect(
    oracledb: Any,
    conn_info: DatasourceConnInfo,
    password: str,
    *,
    connect_timeout_seconds: int,
) -> Any:
    """建连并挂取值处理器。调用方先 `ensure_client_mode`(两条路径都在加载驱动时做)。"""
    kwargs: dict[str, Any] = {
        "user": conn_info.username,
        "password": password,
        "dsn": build_dsn(conn_info),
    }
    connect_timeout = int(conn_info.extra.get("connect_timeout", connect_timeout_seconds))
    if connect_timeout > 0:
        # python-oracledb:tcp_connect_timeout 单位秒(thin / thick 都认)。
        kwargs["tcp_connect_timeout"] = connect_timeout
    conn = oracledb.connect(**kwargs)
    handler = build_output_type_handler(oracledb)
    if handler is not None:
        conn.outputtypehandler = handler
    return conn


def build_output_type_handler(oracledb: Any) -> Callable[[Any, Any], Any] | None:
    """连接级 outputtypehandler:有小数位 / 无约束的 NUMBER → Decimal;LOB → str / bytes。

    `NUMBER(p,0)`(precision>0、scale==0)缺省就取精确 int,不接管。驱动模块缺这些
    DbType 常量时(单测 fake)返回 None,不挂 handler。
    """
    number_type = getattr(oracledb, "DB_TYPE_NUMBER", None)
    lob_targets: dict[Any, Any] = {}
    for source_name, target_name in (
        ("DB_TYPE_CLOB", "DB_TYPE_LONG"),
        ("DB_TYPE_NCLOB", "DB_TYPE_LONG_NVARCHAR"),
        ("DB_TYPE_BLOB", "DB_TYPE_LONG_RAW"),
    ):
        source = getattr(oracledb, source_name, None)
        target = getattr(oracledb, target_name, None)
        if source is not None and target is not None:
            lob_targets[source] = target
    if number_type is None and not lob_targets:
        return None

    def handler(cursor: Any, metadata: Any) -> Any:
        type_code = metadata.type_code
        if number_type is not None and type_code is number_type:
            if metadata.scale == 0 and (metadata.precision or 0) > 0:
                return None
            return cursor.var(decimal.Decimal, arraysize=cursor.arraysize)
        target = lob_targets.get(type_code)
        if target is not None:
            return cursor.var(target, arraysize=cursor.arraysize)
        return None

    return handler


__all__ = [
    "CLIENT_DIR_ENV",
    "OracleClientInitFailure",
    "build_dsn",
    "build_output_type_handler",
    "connect",
    "ensure_client_mode",
]
