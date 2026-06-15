"""Env-gated compare inference metadata test against real MySQL + DM.

The test uses only DATAOPS_TEST_* environment variables and never prints connection details.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, cast

import pytest

from app.dbclients.dm_adapter import DMAdapter
from app.dbclients.mysql_adapter import MySQLAdapter
from app.domain.compare_infer import infer_compare_draft
from app.domain.datasource import DatasourceConnInfo, DbType
from app.domain.secret import HashedRef, RotationReport, SecretKind, SecretRef
from app.infrastructure.secretstore.protocol import SecretStore

pytestmark = pytest.mark.integration

_MYSQL_TABLE = "dosv2_compare_infer_src"
_DM_TABLE = "DOSV2_COMPARE_INFER_TGT"


def test_compare_infer_detects_real_primary_keys_and_column_pairs() -> None:
    mysql_env = _mysql_env_or_skip()
    dm_env = _dm_env_or_skip()

    with _seed_mysql(mysql_env), _seed_dm(dm_env):
        mysql_adapter = MySQLAdapter(
            _conn_info("mysql", mysql_env),
            cast(SecretStore, _EnvSecretStore(mysql_env["password"])),
        )
        dm_adapter = DMAdapter(
            _conn_info("dm", dm_env),
            cast(SecretStore, _EnvSecretStore(dm_env["password"])),
        )
        try:
            draft = infer_compare_draft(
                source_columns=mysql_adapter.list_columns(mysql_env["database"], _MYSQL_TABLE),
                target_columns=dm_adapter.list_columns(dm_env["database"], _DM_TABLE),
                source_indexes=mysql_adapter.list_indexes(mysql_env["database"], _MYSQL_TABLE),
                target_indexes=dm_adapter.list_indexes(dm_env["database"], _DM_TABLE),
            )
        except Exception:
            raise RuntimeError("Compare infer metadata probe failed; details redacted") from None

    assert draft.needs_manual_pk is False
    assert draft.pk_candidates[0].source_columns == ["id"]
    assert draft.pk_candidates[0].target_columns == ["ID"]
    assert draft.compare_rules.key_columns == ["id"]
    assert draft.compare_rules.column_mappings["id"] == "ID"
    assert draft.compare_rules.column_mappings["user_name"] == "USER_NAME"
    assert draft.compare_rules.column_mappings["amount"] == "AMOUNT"


@contextmanager
def _seed_mysql(env: dict[str, str]) -> Iterator[None]:
    pymysql = importlib.import_module("pymysql")
    conn = _connect_mysql(pymysql, env)
    cursor = conn.cursor()
    try:
        cursor.execute(f"DROP TABLE IF EXISTS `{_MYSQL_TABLE}`")
        cursor.execute(
            f"CREATE TABLE `{_MYSQL_TABLE}` ("
            "`id` INT PRIMARY KEY, "
            "`user_name` VARCHAR(64), "
            "`amount` DECIMAL(12,2)"
            ")"
        )
        conn.commit()
        yield
    finally:
        try:
            cursor.execute(f"DROP TABLE IF EXISTS `{_MYSQL_TABLE}`")
            conn.commit()
        finally:
            cursor.close()
            conn.close()


@contextmanager
def _seed_dm(env: dict[str, str]) -> Iterator[None]:
    dm = importlib.import_module("dmPython")
    conn = _connect_dm(dm, env)
    cursor = conn.cursor()
    try:
        _ignore_error(lambda: cursor.execute(f"DROP TABLE {_DM_TABLE}"))
        cursor.execute(
            f"CREATE TABLE {_DM_TABLE} ("
            "ID INT PRIMARY KEY, "
            "USER_NAME VARCHAR(64), "
            "AMOUNT DECIMAL(12,2)"
            ")"
        )
        conn.commit()
        yield
    finally:
        try:
            _ignore_error(lambda: cursor.execute(f"DROP TABLE {_DM_TABLE}"))
            conn.commit()
        finally:
            cursor.close()
            conn.close()


def _conn_info(kind: str, env: dict[str, str]) -> DatasourceConnInfo:
    return DatasourceConnInfo(
        host=env["host"],
        port=int(env["port"]),
        username=env["user"],
        database=env["database"],
        password_ref=SecretRef(
            ref=f"env://{kind}-compare-infer",
            kind=SecretKind.DATASOURCE_PASSWORD,
        ),
        db_type=DbType.MYSQL if kind == "mysql" else DbType.DM,
    )


def _mysql_env_or_skip() -> dict[str, str]:
    return _env_or_skip(
        {
            "host": "DATAOPS_TEST_MYSQL_HOST",
            "port": "DATAOPS_TEST_MYSQL_PORT",
            "user": "DATAOPS_TEST_MYSQL_USER",
            "password": "DATAOPS_TEST_MYSQL_PASSWORD",
            "database": "DATAOPS_TEST_MYSQL_DATABASE",
        },
        "MySQL",
    )


def _dm_env_or_skip() -> dict[str, str]:
    return _env_or_skip(
        {
            "host": "DATAOPS_TEST_DM_HOST",
            "port": "DATAOPS_TEST_DM_PORT",
            "user": "DATAOPS_TEST_DM_USERNAME",
            "password": "DATAOPS_TEST_DM_PASSWORD",
            "database": "DATAOPS_TEST_DM_DATABASE",
        },
        "DM",
    )


def _env_or_skip(mapping: dict[str, str], label: str) -> dict[str, str]:
    values = {name: os.environ.get(env_name) for name, env_name in mapping.items()}
    missing = [mapping[name] for name, value in values.items() if not value]
    if missing:
        pytest.skip(f"{label} integration env not set: {', '.join(missing)}")
    return {name: str(value) for name, value in values.items()}


def _connect_mysql(pymysql: Any, env: dict[str, str]) -> Any:
    try:
        return pymysql.connect(
            host=env["host"],
            port=int(env["port"]),
            user=env["user"],
            password=env["password"],
            database=env["database"],
            charset="utf8mb4",
            connect_timeout=10,
        )
    except Exception:
        raise RuntimeError("MySQL compare infer connection failed; details redacted") from None


def _connect_dm(dm: Any, env: dict[str, str]) -> Any:
    try:
        return dm.connect(
            user=env["user"],
            password=env["password"],
            server=env["host"],
            port=int(env["port"]),
            schema=env["database"],
        )
    except Exception:
        raise RuntimeError("DM compare infer connection failed; details redacted") from None


def _ignore_error(fn: Callable[[], object]) -> None:
    try:
        fn()
    except Exception:
        return


class _EnvSecretStore:
    def __init__(self, password: str) -> None:
        self._password = password

    def reveal_secret(self, ref: SecretRef) -> str:
        assert ref.kind is SecretKind.DATASOURCE_PASSWORD
        return self._password

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
