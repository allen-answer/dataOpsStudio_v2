from __future__ import annotations

from collections.abc import Callable

from app.dbclients.db2_adapter import Db2Adapter
from app.dbclients.dm_adapter import DMAdapter
from app.dbclients.mysql_adapter import MySQLAdapter
from app.dbclients.oracle_adapter import OracleAdapter
from app.dbclients.postgresql_adapter import PostgresqlAdapter
from app.dbclients.protocol import DatabaseAdapter
from app.domain.datasource import DatasourceConnInfo, DbType
from app.domain.schema import Column
from app.infrastructure.secretstore.protocol import SecretStore


class UnsupportedDbTypeError(RuntimeError):
    """Datasource db_type has no certified adapter in this build."""


def build_database_adapter(
    conn_info: DatasourceConnInfo,
    secret_store: SecretStore,
    *,
    cancel_check: Callable[[], bool] | None = None,
    column_sink: Callable[[list[Column]], None] | None = None,
    cursor_max_hold_seconds: int = 300,
    statement_timeout_seconds: int = 300,
    connect_timeout_seconds: int = 10,
) -> DatabaseAdapter:
    """db_type -> DatabaseAdapter dispatch.

    Certified backends: MySQL + DM;Preview: DB2 + Oracle;New: PostgreSQL(待真实例
    验证后升 Certified)。Other datasource types are explicit structured
    unsupported errors instead of best-effort driver guesses.
    """

    if conn_info.db_type is DbType.MYSQL:
        return MySQLAdapter(
            conn_info,
            secret_store,
            cancel_check=cancel_check,
            column_sink=column_sink,
            cursor_max_hold_seconds=cursor_max_hold_seconds,
            statement_timeout_seconds=statement_timeout_seconds,
            connect_timeout_seconds=connect_timeout_seconds,
        )
    if conn_info.db_type is DbType.DM:
        return DMAdapter(
            conn_info,
            secret_store,
            cancel_check=cancel_check,
            column_sink=column_sink,
            cursor_max_hold_seconds=cursor_max_hold_seconds,
            statement_timeout_seconds=statement_timeout_seconds,
            connect_timeout_seconds=connect_timeout_seconds,
        )
    if conn_info.db_type is DbType.POSTGRESQL:
        return PostgresqlAdapter(
            conn_info,
            secret_store,
            cancel_check=cancel_check,
            column_sink=column_sink,
            cursor_max_hold_seconds=cursor_max_hold_seconds,
            statement_timeout_seconds=statement_timeout_seconds,
            connect_timeout_seconds=connect_timeout_seconds,
        )
    if conn_info.db_type is DbType.ORACLE:
        return OracleAdapter(
            conn_info,
            secret_store,
            cancel_check=cancel_check,
            column_sink=column_sink,
            cursor_max_hold_seconds=cursor_max_hold_seconds,
            statement_timeout_seconds=statement_timeout_seconds,
            connect_timeout_seconds=connect_timeout_seconds,
        )
    if conn_info.db_type is DbType.DB2:
        return Db2Adapter(
            conn_info,
            secret_store,
            cancel_check=cancel_check,
            column_sink=column_sink,
            cursor_max_hold_seconds=cursor_max_hold_seconds,
            statement_timeout_seconds=statement_timeout_seconds,
            connect_timeout_seconds=connect_timeout_seconds,
        )
    raise UnsupportedDbTypeError(f"Unsupported datasource db_type: {conn_info.db_type.value}")
