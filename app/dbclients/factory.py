from __future__ import annotations

from collections.abc import Callable

from app.dbclients.db2_adapter import Db2Adapter
from app.dbclients.dm_adapter import DMAdapter
from app.dbclients.mysql_adapter import MySQLAdapter
from app.dbclients.oceanbase_oracle_adapter import OceanBaseOracleAdapter
from app.dbclients.oracle_adapter import OracleAdapter
from app.dbclients.postgresql_adapter import PostgresqlAdapter
from app.dbclients.protocol import DatabaseAdapter
from app.domain.datasource import DatasourceConnInfo, DbType
from app.domain.schema import Column
from app.infrastructure.secretstore.protocol import SecretStore


class UnsupportedDbTypeError(RuntimeError):
    """Datasource db_type has no certified adapter in this build."""


# db_type -> 适配器类。各适配器构造签名一致(conn_info, secret_store, **运行参数);
# 新方言在这里登记一行即可。
_ADAPTER_CLASSES: dict[DbType, Callable[..., DatabaseAdapter]] = {
    DbType.MYSQL: MySQLAdapter,
    DbType.DM: DMAdapter,
    DbType.POSTGRESQL: PostgresqlAdapter,
    DbType.ORACLE: OracleAdapter,
    DbType.DB2: Db2Adapter,
    DbType.OCEANBASE_ORACLE: OceanBaseOracleAdapter,
}


def adapter_class_for(db_type: DbType) -> Callable[..., DatabaseAdapter] | None:
    """db_type 对应的适配器类(只读其类属性 capabilities 用,不实例化、不连库)。"""
    return _ADAPTER_CLASSES.get(db_type)


def build_database_adapter(
    conn_info: DatasourceConnInfo,
    secret_store: SecretStore,
    *,
    cancel_check: Callable[[], bool] | None = None,
    column_sink: Callable[[list[Column]], None] | None = None,
    cursor_max_hold_seconds: int = 300,
    statement_timeout_seconds: int = 300,
    fetch_chunk_size: int = 1000,
    connect_timeout_seconds: int = 10,
) -> DatabaseAdapter:
    """db_type -> DatabaseAdapter dispatch.

    Certified backends: MySQL + DM;Preview: DB2 + Oracle;New: PostgreSQL(待真实例
    验证后升 Certified)。Other datasource types are explicit structured
    unsupported errors instead of best-effort driver guesses.
    """

    adapter_class = _ADAPTER_CLASSES.get(conn_info.db_type)
    if adapter_class is None:
        raise UnsupportedDbTypeError(f"Unsupported datasource db_type: {conn_info.db_type.value}")
    return adapter_class(
        conn_info,
        secret_store,
        cancel_check=cancel_check,
        column_sink=column_sink,
        cursor_max_hold_seconds=cursor_max_hold_seconds,
        statement_timeout_seconds=statement_timeout_seconds,
        fetch_chunk_size=fetch_chunk_size,
        connect_timeout_seconds=connect_timeout_seconds,
    )
