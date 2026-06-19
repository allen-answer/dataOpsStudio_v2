"""Env-gated lineage checks against real MySQL and DM instances.

These tests intentionally skip without DATAOPS_TEST_* variables. They do not print
connection details or credentials. Connection failures preserve the original exception
chain while redacting details.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest
import sqlglot
from sqlglot import exp

from app.domain.lineage import LineageParseRequest, analyze_sql_lineage

pytestmark = pytest.mark.integration

_MYSQL_SRC = "dosv2_lineage_src"
_MYSQL_DIM = "dosv2_lineage_dim"
_MYSQL_TGT = "dosv2_lineage_tgt"
_DM_SRC = "DOSV2_LINEAGE_SRC"
_DM_DIM = "DOSV2_LINEAGE_DIM"
_DM_TGT = "DOSV2_LINEAGE_TGT"


def test_mysql_lineage_uses_real_schema_for_column_ownership() -> None:
    env = _mysql_env_or_skip()
    with _seed_mysql_lineage_tables(env):
        schema = _mysql_schema(env, [_MYSQL_SRC, _MYSQL_DIM, _MYSQL_TGT])
        sql = (
            f"INSERT INTO `{env['database']}`.`{_MYSQL_TGT}` "
            "(`id`, `amount`, `customer_name`) "
            f"SELECT * FROM `{env['database']}`.`{_MYSQL_SRC}`"
        )
        report = analyze_sql_lineage(
            LineageParseRequest(
                sql_text=sql,
                dialect="mysql",
                schema=schema,
                default_schema=env["database"],
            )
        )

    assert report.parse_errors == []
    assert _has_mapping(report.insert_mappings, "id", f"{env['database']}.{_MYSQL_SRC}", "id")
    assert _has_mapping(
        report.insert_mappings,
        "amount",
        f"{env['database']}.{_MYSQL_SRC}",
        "amount",
    )
    assert _has_mapping(
        report.insert_mappings,
        "customer_name",
        f"{env['database']}.{_MYSQL_SRC}",
        "customer_name",
    )
    assert _naive_select_star_is_unexpanded(sql, "mysql") is True


def test_dm_lineage_dialect_parses_real_dm_dml_with_schema() -> None:
    env = _dm_env_or_skip()
    with _seed_dm_lineage_tables(env):
        schema = _dm_schema(env, [_DM_SRC, _DM_DIM, _DM_TGT])
        sql = (
            f"INSERT INTO {env['database']}.{_DM_TGT} (ID, AMOUNT, CUSTOMER_NAME) "
            f"SELECT S.ID, S.AMOUNT, D.NAME "
            f"FROM {env['database']}.{_DM_SRC} S "
            f"JOIN {env['database']}.{_DM_DIM} D ON S.CUSTOMER_ID = D.ID"
        )
        report = analyze_sql_lineage(
            LineageParseRequest(
                sql_text=sql,
                dialect="dm",
                schema=schema,
                default_schema=env["database"],
            )
        )

    assert report.parse_errors == []
    assert _has_mapping(report.insert_mappings, "ID", f"{env['database']}.{_DM_SRC}", "ID")
    assert _has_mapping(
        report.insert_mappings,
        "AMOUNT",
        f"{env['database']}.{_DM_SRC}",
        "AMOUNT",
    )
    assert _has_mapping(
        report.insert_mappings,
        "CUSTOMER_NAME",
        f"{env['database']}.{_DM_DIM}",
        "NAME",
    )


@contextmanager
def _seed_mysql_lineage_tables(env: dict[str, str]) -> Iterator[None]:
    pymysql = importlib.import_module("pymysql")
    conn = _connect_mysql(pymysql, env)
    cursor = conn.cursor()
    try:
        for table in (_MYSQL_TGT, _MYSQL_DIM, _MYSQL_SRC):
            cursor.execute(f"DROP TABLE IF EXISTS `{table}`")
        cursor.execute(
            f"CREATE TABLE `{_MYSQL_SRC}` ("
            "`id` INT PRIMARY KEY, "
            "`amount` DECIMAL(18,2), "
            "`customer_name` VARCHAR(128))"
        )
        cursor.execute(f"CREATE TABLE `{_MYSQL_DIM}` (`id` INT PRIMARY KEY, `name` VARCHAR(128))")
        cursor.execute(
            f"CREATE TABLE `{_MYSQL_TGT}` ("
            "`id` INT, `amount` DECIMAL(18,2), `customer_name` VARCHAR(128))"
        )
        conn.commit()
        yield
    finally:
        try:
            for table in (_MYSQL_TGT, _MYSQL_DIM, _MYSQL_SRC):
                cursor.execute(f"DROP TABLE IF EXISTS `{table}`")
            conn.commit()
        finally:
            cursor.close()
            conn.close()


@contextmanager
def _seed_dm_lineage_tables(env: dict[str, str]) -> Iterator[None]:
    dm = importlib.import_module("dmPython")
    conn = _connect_dm(dm, env)
    cursor = conn.cursor()
    try:
        for table in (_DM_TGT, _DM_DIM, _DM_SRC):
            _drop_dm_table(cursor, table)
        cursor.execute(
            f"CREATE TABLE {_DM_SRC} (ID INT PRIMARY KEY, AMOUNT DECIMAL(18,2), CUSTOMER_ID INT)"
        )
        cursor.execute(f"CREATE TABLE {_DM_DIM} (ID INT PRIMARY KEY, NAME VARCHAR(128))")
        cursor.execute(
            f"CREATE TABLE {_DM_TGT} (ID INT, AMOUNT DECIMAL(18,2), CUSTOMER_NAME VARCHAR(128))"
        )
        conn.commit()
        yield
    finally:
        try:
            for table in (_DM_TGT, _DM_DIM, _DM_SRC):
                _drop_dm_table(cursor, table)
            conn.commit()
        finally:
            cursor.close()
            conn.close()


def _mysql_schema(env: dict[str, str], tables: list[str]) -> dict[str, dict[str, dict[str, str]]]:
    pymysql = importlib.import_module("pymysql")
    conn = _connect_mysql(pymysql, env)
    cursor = conn.cursor()
    try:
        placeholders = ", ".join(["%s"] * len(tables))
        cursor.execute(
            "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE "
            "FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME IN (" + placeholders + ") "
            "ORDER BY TABLE_NAME, ORDINAL_POSITION",
            [env["database"], *tables],
        )
        return _schema_from_rows(env["database"], cursor.fetchall())
    finally:
        cursor.close()
        conn.close()


def _dm_schema(env: dict[str, str], tables: list[str]) -> dict[str, dict[str, dict[str, str]]]:
    dm = importlib.import_module("dmPython")
    conn = _connect_dm(dm, env)
    cursor = conn.cursor()
    try:
        placeholders = ", ".join(["?"] * len(tables))
        cursor.execute(
            "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE "
            "FROM USER_TAB_COLUMNS "
            "WHERE TABLE_NAME IN (" + placeholders + ") "
            "ORDER BY TABLE_NAME, COLUMN_ID",
            tables,
        )
        return _schema_from_rows(env["database"], cursor.fetchall())
    finally:
        cursor.close()
        conn.close()


def _schema_from_rows(
    schema_name: str,
    rows: list[tuple[object, object, object]],
) -> dict[str, dict[str, dict[str, str]]]:
    schema: dict[str, dict[str, dict[str, str]]] = {schema_name: {}}
    for table_name, column_name, data_type in rows:
        schema[schema_name].setdefault(str(table_name), {})[str(column_name)] = str(data_type)
    return schema


def _has_mapping(
    mappings: list[dict[str, object]],
    target_column: str,
    source_table: str,
    source_column: str,
) -> bool:
    return any(
        mapping["target_column"] == target_column
        and mapping["source_table"] == source_table
        and mapping["source_column"] == source_column
        for mapping in mappings
    )


def _naive_select_star_is_unexpanded(sql: str, dialect: str) -> bool:
    expression = sqlglot.parse_one(sql, read=dialect)
    select = expression.find(exp.Select)
    return bool(select and select.find(exp.Star))


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


def _env_or_skip(mapping: dict[str, str], label: str) -> _RedactedEnv:
    values = {name: os.environ.get(env_name) for name, env_name in mapping.items()}
    missing = [mapping[name] for name, value in values.items() if not value]
    if missing:
        pytest.skip(f"{label} integration env not set: {', '.join(missing)}")
    return _RedactedEnv({name: str(value) for name, value in values.items()})


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
    except Exception as exc:
        raise RuntimeError(
            f"MySQL lineage connection failed: {type(exc).__name__}; details redacted"
        ) from exc


def _connect_dm(dm: Any, env: dict[str, str]) -> Any:
    try:
        return dm.connect(
            user=env["user"],
            password=env["password"],
            server=env["host"],
            port=int(env["port"]),
            schema=env["database"],
        )
    except Exception as exc:
        raise RuntimeError(
            f"DM lineage connection failed: {type(exc).__name__}; details redacted"
        ) from exc


def _ignore_error(fn: Callable[[], object]) -> None:
    try:
        fn()
    except Exception:
        pass


def _drop_dm_table(cursor: Any, table: str) -> None:
    _ignore_error(lambda: cursor.execute(f"DROP TABLE {table}"))
