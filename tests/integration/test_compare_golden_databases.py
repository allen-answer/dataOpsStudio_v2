"""Compare golden consistency test against real MySQL + DM instances.

This test is env-gated because CI does not provide a licensed DM service. It must run in
an environment with both datasource backends configured. The test never prints connection
details or credentials.
"""

from __future__ import annotations

import importlib
import os
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from typing import Any, cast

import pytest

from app.dbclients.dm_adapter import DMAdapter
from app.dbclients.mysql_adapter import MySQLAdapter
from app.domain.compare import (
    CompareColumn,
    CompareHashLevel,
    CompareHashPlan,
    CompareHashRequest,
    CompareRules,
    CompareTableRef,
    compare_row_hash64,
)
from app.domain.datasource import DatasourceConnInfo, DbType
from app.domain.schema import ColumnType
from app.domain.secret import HashedRef, RotationReport, SecretKind, SecretRef
from app.infrastructure.secretstore.protocol import SecretStore

pytestmark = pytest.mark.integration

_MYSQL_TABLE = "dosv2_compare_golden_mysql"
_DM_TABLE = "DOSV2_COMPARE_GOLDEN_DM"
_DM_POC_TABLE = "DOSV2_COMPARE_HASH_POC"
_DM_POC_ROWS = 100_000


def test_compare_golden_row_and_aggregate_hashes_match_mysql_and_dm() -> None:
    mysql_env = _mysql_env_or_skip()
    dm_env = _dm_env_or_skip()
    rules = _rules()
    columns = _columns()

    with _seed_mysql_table(mysql_env), _seed_dm_table(dm_env):
        mysql_adapter = MySQLAdapter(
            _mysql_conn_info(mysql_env),
            cast(SecretStore, _EnvSecretStore(mysql_env["password"])),
        )
        dm_adapter = DMAdapter(
            _dm_conn_info(dm_env),
            cast(SecretStore, _EnvSecretStore(dm_env["password"])),
        )
        mysql_row_plan = mysql_adapter.build_compare_hash_query(
            _request(
                table=CompareTableRef(schema_name=mysql_env["database"], name=_MYSQL_TABLE),
                columns=columns,
                rules=rules,
                level=CompareHashLevel.ROW,
            )
        )
        dm_row_plan = dm_adapter.build_compare_hash_query(
            _request(
                table=CompareTableRef(schema_name=dm_env["database"], name=_DM_TABLE),
                columns=columns,
                rules=rules,
                level=CompareHashLevel.ROW,
            )
        )
        mysql_aggregate_plan = mysql_adapter.build_compare_hash_query(
            _request(
                table=CompareTableRef(schema_name=mysql_env["database"], name=_MYSQL_TABLE),
                columns=columns,
                rules=rules,
                level=CompareHashLevel.AGGREGATE,
            )
        )
        dm_aggregate_plan = dm_adapter.build_compare_hash_query(
            _request(
                table=CompareTableRef(schema_name=dm_env["database"], name=_DM_TABLE),
                columns=columns,
                rules=rules,
                level=CompareHashLevel.AGGREGATE,
            )
        )

        mysql_row_hashes = _mysql_rows(mysql_env, mysql_row_plan)
        dm_row_hashes = _dm_rows(dm_env, dm_row_plan)
        expected = _expected_row_hashes(columns, rules)

        assert mysql_row_hashes == expected
        assert dm_row_hashes == expected
        assert _mysql_aggregate(mysql_env, mysql_aggregate_plan) == _dm_aggregate(
            dm_env,
            dm_aggregate_plan,
        )
        assert _mysql_aggregate(mysql_env, mysql_aggregate_plan) == (
            len(expected),
            sum(expected.values()),
        )


@pytest.mark.slow
def test_dm_dbms_crypto_hash_poc_100k_rows() -> None:
    dm_env = _dm_env_or_skip()
    rules = _rules()
    columns = _columns()

    with _seed_dm_poc_table(dm_env, _DM_POC_ROWS):
        dm_adapter = DMAdapter(
            _dm_conn_info(dm_env),
            cast(SecretStore, _EnvSecretStore(dm_env["password"])),
        )
        aggregate_plan = dm_adapter.build_compare_hash_query(
            _request(
                table=CompareTableRef(schema_name=dm_env["database"], name=_DM_POC_TABLE),
                columns=columns,
                rules=rules,
                level=CompareHashLevel.AGGREGATE,
            )
        )
        started_at = time.perf_counter()
        row_count, aggregate_hash = _dm_aggregate(dm_env, aggregate_plan)
        elapsed_seconds = time.perf_counter() - started_at

    assert row_count == _DM_POC_ROWS
    assert aggregate_hash > 0
    print(
        "\n[evidence] DM DBMS_CRYPTO.HASH compare aggregate "
        f"rows={row_count} elapsed_seconds={elapsed_seconds:.3f} mode=db_hash"
    )


class _RedactedEnv(dict[str, str]):
    def __repr__(self) -> str:
        keys = ", ".join(sorted(self.keys()))
        return f"_RedactedEnv(keys=[{keys}])"


def _mysql_env_or_skip() -> _RedactedEnv:
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


def _dm_env_or_skip() -> _RedactedEnv:
    values = _env_or_skip(
        {
            "host": "DATAOPS_TEST_DM_HOST",
            "port": "DATAOPS_TEST_DM_PORT",
            "user": "DATAOPS_TEST_DM_USERNAME",
            "password": "DATAOPS_TEST_DM_PASSWORD",
            "database": "DATAOPS_TEST_DM_DATABASE",
        },
        "DM",
    )
    return values


def _env_or_skip(mapping: dict[str, str], label: str) -> _RedactedEnv:
    values = {name: os.environ.get(env_name) for name, env_name in mapping.items()}
    missing = [mapping[name] for name, value in values.items() if not value]
    if missing:
        pytest.skip(f"{label} integration env not set: {', '.join(missing)}")
    return _RedactedEnv({name: str(value) for name, value in values.items()})


def _mysql_conn_info(env: _RedactedEnv) -> DatasourceConnInfo:
    return DatasourceConnInfo(
        host=env["host"],
        port=int(env["port"]),
        username=env["user"],
        database=env["database"],
        password_ref=SecretRef(
            ref="env://mysql-compare-golden",
            kind=SecretKind.DATASOURCE_PASSWORD,
        ),
        db_type=DbType.MYSQL,
    )


def _dm_conn_info(env: _RedactedEnv) -> DatasourceConnInfo:
    return DatasourceConnInfo(
        host=env["host"],
        port=int(env["port"]),
        username=env["user"],
        database=env["database"],
        password_ref=SecretRef(ref="env://dm-compare-golden", kind=SecretKind.DATASOURCE_PASSWORD),
        db_type=DbType.DM,
    )


def _request(
    *,
    table: CompareTableRef,
    columns: list[CompareColumn],
    rules: CompareRules,
    level: CompareHashLevel,
) -> CompareHashRequest:
    return CompareHashRequest(
        table=table,
        columns=columns,
        key_columns=[CompareColumn(name="id", type=ColumnType.INTEGER)],
        rules=rules,
        level=level,
    )


def _columns() -> list[CompareColumn]:
    return [
        CompareColumn(name="id", type=ColumnType.INTEGER),
        CompareColumn(name="amount", type=ColumnType.DECIMAL),
        CompareColumn(name="updated_at", type=ColumnType.DATETIME),
        CompareColumn(name="name", type=ColumnType.STRING),
        CompareColumn(name="memo", type=ColumnType.STRING),
    ]


def _rules() -> CompareRules:
    return CompareRules(
        trim_strings=True,
        case_insensitive=True,
        empty_as_null=True,
        numeric_scale=4,
        timestamp_precision=6,
    )


def _expected_row_hashes(
    columns: Sequence[CompareColumn],
    rules: CompareRules,
) -> dict[int, int]:
    logical_rows = {
        1: [1, Decimal("12.34"), datetime(2026, 6, 14, 12, 13, 14, 123456), "张三", None],
        2: [2, Decimal("0.1"), datetime(2026, 6, 14, 0, 0, 0, 1), "alpha", None],
        3: [3, Decimal("-5"), datetime(2026, 6, 14, 23, 59, 59, 999999), "mixed", "note"],
    }
    return {key: compare_row_hash64(columns, values, rules) for key, values in logical_rows.items()}


@contextmanager
def _seed_mysql_table(env: dict[str, str]) -> Iterator[None]:
    pymysql = importlib.import_module("pymysql")
    conn = _connect_mysql(pymysql, env)
    cursor = conn.cursor()
    try:
        cursor.execute(f"DROP TABLE IF EXISTS `{_MYSQL_TABLE}`")
        cursor.execute(
            f"CREATE TABLE `{_MYSQL_TABLE}` ("
            "`id` INT PRIMARY KEY, "
            "`amount` DECIMAL(18,6), "
            "`updated_at` DATETIME(6), "
            "`name` VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin, "
            "`memo` VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NULL)"
        )
        cursor.executemany(
            f"INSERT INTO `{_MYSQL_TABLE}` VALUES (%s, %s, %s, %s, %s)",
            [
                (1, "12.3400", "2026-06-14 12:13:14.123456", "  张三  ", None),
                (2, "0.100000", "2026-06-14 00:00:00.000001", "Alpha", ""),
                (3, "-5.0000", "2026-06-14 23:59:59.999999", " MIXED ", " Note "),
            ],
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
def _seed_dm_table(env: dict[str, str]) -> Iterator[None]:
    dm = importlib.import_module("dmPython")
    conn = _connect_dm(dm, env)
    cursor = conn.cursor()
    try:
        _ignore_error(lambda: cursor.execute(f"DROP TABLE {_DM_TABLE}"))
        cursor.execute(
            f"CREATE TABLE {_DM_TABLE} ("
            "ID INT PRIMARY KEY, "
            "AMOUNT DECIMAL(18,6), "
            "UPDATED_AT TIMESTAMP(6), "
            "NAME VARCHAR(128), "
            "MEMO VARCHAR(128) NULL)"
        )
        cursor.executemany(
            f"INSERT INTO {_DM_TABLE} VALUES (?, ?, ?, ?, ?)",
            [
                (1, "12.34", "2026-06-14 12:13:14.123456", "张三", None),
                (2, "0.10", "2026-06-14 00:00:00.000001", "alpha", None),
                (3, "-5.000000", "2026-06-14 23:59:59.999999", "mixed", "note"),
            ],
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


@contextmanager
def _seed_dm_poc_table(env: dict[str, str], row_count: int) -> Iterator[None]:
    dm = importlib.import_module("dmPython")
    conn = _connect_dm(dm, env)
    cursor = conn.cursor()
    try:
        _ignore_error(lambda: cursor.execute(f"DROP TABLE {_DM_POC_TABLE}"))
        cursor.execute(
            f"CREATE TABLE {_DM_POC_TABLE} ("
            "ID INT PRIMARY KEY, "
            "AMOUNT DECIMAL(18,6), "
            "UPDATED_AT TIMESTAMP(6), "
            "NAME VARCHAR(128), "
            "MEMO VARCHAR(128) NULL)"
        )
        batch_size = 1000
        for start in range(1, row_count + 1, batch_size):
            end = min(row_count + 1, start + batch_size)
            cursor.executemany(
                f"INSERT INTO {_DM_POC_TABLE} VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        idx,
                        f"{idx / 100:.6f}",
                        "2026-06-14 12:13:14.123456",
                        f"row-{idx % 997}",
                        None if idx % 10 == 0 else f"memo-{idx % 17}",
                    )
                    for idx in range(start, end)
                ],
            )
        conn.commit()
        yield
    finally:
        try:
            _ignore_error(lambda: cursor.execute(f"DROP TABLE {_DM_POC_TABLE}"))
            conn.commit()
        finally:
            cursor.close()
            conn.close()


def _mysql_rows(env: dict[str, str], plan: CompareHashPlan) -> dict[int, int]:
    pymysql = importlib.import_module("pymysql")
    conn = _connect_mysql(pymysql, env)
    cursor = conn.cursor()
    try:
        assert plan.sql is not None
        cursor.execute(plan.sql, plan.params)
        return {int(row[0]): int(row[1]) for row in cursor.fetchall()}
    finally:
        cursor.close()
        conn.close()


def _dm_rows(env: dict[str, str], plan: CompareHashPlan) -> dict[int, int]:
    dm = importlib.import_module("dmPython")
    conn = _connect_dm(dm, env)
    cursor = conn.cursor()
    try:
        assert plan.sql is not None
        cursor.execute(plan.sql)
        return {int(row[0]): int(row[1]) for row in cursor.fetchall()}
    finally:
        cursor.close()
        conn.close()


def _mysql_aggregate(env: dict[str, str], plan: CompareHashPlan) -> tuple[int, int]:
    pymysql = importlib.import_module("pymysql")
    conn = _connect_mysql(pymysql, env)
    cursor = conn.cursor()
    try:
        assert plan.sql is not None
        cursor.execute(plan.sql, plan.params)
        row = cursor.fetchone()
        return int(row[0]), int(row[1])
    finally:
        cursor.close()
        conn.close()


def _dm_aggregate(env: dict[str, str], plan: CompareHashPlan) -> tuple[int, int]:
    dm = importlib.import_module("dmPython")
    conn = _connect_dm(dm, env)
    cursor = conn.cursor()
    try:
        assert plan.sql is not None
        cursor.execute(plan.sql)
        row = cursor.fetchone()
        return int(row[0]), int(row[1])
    finally:
        cursor.close()
        conn.close()


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
        raise RuntimeError("MySQL compare golden connection failed; details redacted") from None


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
        raise RuntimeError("DM compare golden connection failed; details redacted") from None


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
