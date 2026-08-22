"""把 `SessionBroker` 装配成 API 进程内的单实例(设计 D1 / §3.1 / §6)。

`app/broker/core.py` 只认协议;本模块负责**方言选型与凭据接线** —— 从
`datasources` 行还原 `DatasourceConnInfo`,再按 db_type 造 owner 连接与控制通道。

R1:驱动 import 仍只发生在 `app/dbclients/` 内(本模块 import 的是会话缝类,
其驱动模块由缝内 `_load_driver()` 惰性加载)。
R2:只传 `SecretRef`;明文口令只在会话缝 connect 内部短暂存在。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.engine import Engine

from app.broker.core import BrokerConfig, SessionBroker
from app.broker.results import SpoolResultStore, SpoolStatementResults
from app.broker.store import PostgresBrokerStore
from app.db.models import datasources
from app.dbclients.interactive import (
    CancelChannel,
    DMCancelChannel,
    DMInteractiveConnection,
    InteractiveConnection,
    InvalidDatasourceError,
    MySQLCancelChannel,
    MySQLInteractiveConnection,
)
from app.domain.console_session import ConsoleSession
from app.domain.datasource import DatasourceConnInfo, DbType, OperationPolicy
from app.domain.secret import SecretKind, SecretRef
from app.infrastructure.secretstore.protocol import SecretStore

# 会话纵切的方言范围线(设计 §6):其余方言整体回退 job 路径,不在此造会话。
SESSION_CAPABLE_DB_TYPES: frozenset[DbType] = frozenset({DbType.MYSQL, DbType.DM})


def build_session_broker(
    engine: Engine,
    secret_store: SecretStore,
    result_store: SpoolResultStore,
    *,
    config: BrokerConfig | None = None,
    max_active_resultsets_per_console: int = 3,
) -> SessionBroker:
    """构造(但不 start)API 进程内唯一的 broker。start/shutdown 归 app lifespan。

    `result_store` 是**必填**:少了它 lane 就不落 spool,语句会"成功但没结果",
    所以这里不给默认值,由调用方明确交出同一个 `LocalFsResultStore` 实例
    (与 job 路径共用 spool 目录,设计 §3.2)。
    """

    def connection_factory(session: ConsoleSession) -> InteractiveConnection:
        conn_info = load_datasource_conn_info(engine, session.datasource_id)
        if conn_info.db_type is DbType.MYSQL:
            return MySQLInteractiveConnection(conn_info, secret_store)
        if conn_info.db_type is DbType.DM:
            return DMInteractiveConnection(conn_info, secret_store)
        raise InvalidDatasourceError(f"db_type has no interactive session: {conn_info.db_type}")

    def cancel_channel_factory(datasource_id: str) -> CancelChannel:
        conn_info = load_datasource_conn_info(engine, datasource_id)
        if conn_info.db_type is DbType.MYSQL:
            return MySQLCancelChannel(conn_info, secret_store)
        if conn_info.db_type is DbType.DM:
            return DMCancelChannel(conn_info, secret_store)
        raise InvalidDatasourceError(f"db_type has no cancel channel: {conn_info.db_type}")

    return SessionBroker(
        PostgresBrokerStore(engine),
        connection_factory=connection_factory,
        cancel_channel_factory=cancel_channel_factory,
        results=SpoolStatementResults(
            engine,
            result_store,
            max_active_resultsets_per_console=max_active_resultsets_per_console,
        ),
        config=config,
    )


def load_datasource_conn_info(engine: Engine, datasource_id: str) -> DatasourceConnInfo:
    """datasources 行 → `DatasourceConnInfo`(口径与 worker 侧一致)。"""

    with engine.connect() as conn:
        row = (
            conn.execute(select(datasources).where(datasources.c.id == datasource_id))
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise LookupError(f"Datasource not found: {datasource_id}")
    return DatasourceConnInfo(
        host=str(row["host"]),
        port=int(row["port"]),
        username=str(row["username"]),
        database=str(row["database_name"]) if row["database_name"] is not None else None,
        password_ref=SecretRef(
            ref=str(row["password_secret_ref"]),
            kind=SecretKind.DATASOURCE_PASSWORD,
        ),
        db_type=DbType(str(row["db_type"])),
        extra=dict(row["capability_profile"] or {}),
        operation_policy=OperationPolicy.model_validate(row["operation_policy"] or {}),
    )


__all__ = [
    "SESSION_CAPABLE_DB_TYPES",
    "build_session_broker",
    "load_datasource_conn_info",
]
