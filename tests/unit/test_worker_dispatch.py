"""worker db_type → adapter 分发单测(build_database_adapter)。

Certified:MySQL + DM;DB2 是 Preview(PR-A 起有 adapter,不再抛
UnsupportedDbTypeError)。其余方言(Oracle / PostgreSQL)仍无 adapter →
UnsupportedDbTypeError。
"""

from __future__ import annotations

from typing import cast

import pytest

from app.dbclients.db2_adapter import Db2Adapter
from app.dbclients.dm_adapter import DMAdapter
from app.dbclients.factory import UnsupportedDbTypeError, build_database_adapter
from app.dbclients.mysql_adapter import MySQLAdapter
from app.domain.datasource import DatasourceConnInfo, DbType
from app.domain.secret import HashedRef, RotationReport, SecretKind, SecretRef
from app.infrastructure.secretstore.protocol import SecretStore


def test_dispatch_mysql_builds_mysql_adapter() -> None:
    adapter = build_database_adapter(_conn_info(DbType.MYSQL), cast(SecretStore, _SecretStore()))
    assert isinstance(adapter, MySQLAdapter)


def test_dispatch_dm_builds_dm_adapter_not_unsupported() -> None:
    # 核心验收:DM 数据源不再抛 UnsupportedDbTypeError
    adapter = build_database_adapter(_conn_info(DbType.DM), cast(SecretStore, _SecretStore()))
    assert isinstance(adapter, DMAdapter)


def test_dispatch_db2_builds_db2_adapter_not_unsupported() -> None:
    # DB2 PR-A 核心验收:DB2 数据源不再抛 UnsupportedDbTypeError,
    # 且 capabilities 按 Preview 范围声明(select/流式开,introspection 关)。
    adapter = build_database_adapter(_conn_info(DbType.DB2), cast(SecretStore, _SecretStore()))
    assert isinstance(adapter, Db2Adapter)
    assert adapter.capabilities.execute_select is True
    assert adapter.capabilities.list_schemas is False


@pytest.mark.parametrize("db_type", [DbType.ORACLE, DbType.POSTGRESQL])
def test_dispatch_uncertified_db_types_still_unsupported(db_type: DbType) -> None:
    with pytest.raises(UnsupportedDbTypeError) as exc_info:
        build_database_adapter(_conn_info(db_type), cast(SecretStore, _SecretStore()))
    assert db_type.value in str(exc_info.value)


def test_dispatch_passes_cancel_and_column_sink_through() -> None:
    sink_calls: list[object] = []
    adapter = build_database_adapter(
        _conn_info(DbType.DM),
        cast(SecretStore, _SecretStore()),
        cancel_check=lambda: True,
        column_sink=sink_calls.append,
        cursor_max_hold_seconds=42,
        statement_timeout_seconds=7,
    )
    assert isinstance(adapter, DMAdapter)


def _conn_info(db_type: DbType) -> DatasourceConnInfo:
    port = 3306 if db_type is DbType.MYSQL else 5236
    return DatasourceConnInfo(
        host="127.0.0.1",
        port=port,
        username="dataops",
        database="app",
        password_ref=SecretRef(ref="secret-1", kind=SecretKind.DATASOURCE_PASSWORD),
        db_type=db_type,
    )


class _SecretStore:
    def reveal_secret(self, ref: SecretRef) -> str:
        return "pwd"

    def store_secret(self, plaintext: str, kind: SecretKind) -> SecretRef:
        raise NotImplementedError

    def rotate_secret(self, ref: SecretRef, new_plaintext: str) -> SecretRef:
        raise NotImplementedError

    def delete_secret(self, ref: SecretRef) -> None:
        raise NotImplementedError

    def hash_password(self, plaintext: str) -> HashedRef:
        raise NotImplementedError

    def verify_password(self, plaintext: str, ref: HashedRef) -> bool:
        raise NotImplementedError

    def rotate_master_key(self, new_key: bytes) -> RotationReport:
        raise NotImplementedError
