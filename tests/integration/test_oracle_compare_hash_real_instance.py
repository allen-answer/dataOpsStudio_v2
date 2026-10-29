"""Oracle 库内比对哈希下推的真实例黄金对照(设计稿 §6.1.5)。

库内逐行哈希必须与客户端 compare_row_hash64 一字不差,否则跨库比对 / 降级时静默产出假差异。
数据全部由 `SELECT ... FROM DUAL` 内联视图生成,**只读,不建表**,最小权限账号即可跑。

GH Actions 无 Oracle,本文件不在 CI 跑:缺环境变量时整文件 skip。运行方式:

    export DATAOPS_TEST_ORACLE_HOST=... DATAOPS_TEST_ORACLE_PORT=1521 \
           DATAOPS_TEST_ORACLE_SERVICE=... DATAOPS_TEST_ORACLE_USERNAME=... \
           DATAOPS_TEST_ORACLE_PASSWORD=...(勿写进 shell history)
    # 11g 需 thick 模式:DATAOPS_ORACLE_CLIENT_DIR=<Instant Client 目录>
    uv run pytest tests/integration/test_oracle_compare_hash_real_instance.py -v
"""

from __future__ import annotations

import os
from typing import Any, cast

import pytest

from app.dbclients.oracle_adapter import OracleAdapter
from app.domain.compare import (
    CompareColumn,
    CompareHashLevel,
    CompareHashRequest,
    CompareRules,
    CompareSegment,
    CompareTableRef,
    compare_row_hash64,
)
from app.domain.datasource import DatasourceConnInfo, DbType
from app.domain.schema import ColumnType
from app.domain.secret import SecretKind, SecretRef
from app.infrastructure.secretstore.protocol import SecretStore

_HOST = os.environ.get("DATAOPS_TEST_ORACLE_HOST")
_PORT = int(os.environ.get("DATAOPS_TEST_ORACLE_PORT", "1521"))
_SERVICE = os.environ.get("DATAOPS_TEST_ORACLE_SERVICE")
_USER = os.environ.get("DATAOPS_TEST_ORACLE_USERNAME")
_PASSWORD = os.environ.get("DATAOPS_TEST_ORACLE_PASSWORD")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (_HOST and _SERVICE and _USER and _PASSWORD),
        reason="real Oracle instance required (DATAOPS_TEST_ORACLE_* unset)",
    ),
]

# 每行覆盖一类边界:GBK 外字符(哨兵本身、增补平面)、全空格、CHAR 补齐、
# BINARY_DOUBLE 舍入边界(2.675)、FF9 时间戳截断、带时区时间戳、NULL。
_ROWS = [
    (
        "1",
        "'abc'",
        "UNISTR('a\\2400\\D83D\\DE00')",
        "'ab'",
        "1.0000005",
        "2.675",
        "DATE '2024-01-02'",
        "TIMESTAMP '2024-01-02 03:04:05.123456789'",
        "TIMESTAMP '2024-01-02 03:04:05.5 -03:30'",
    ),
    (
        "2",
        "'  pad  '",
        "UNISTR('  ')",
        "'x'",
        "-2.5",
        "0.1",
        "TO_DATE('2024-12-31 23:59:59', 'YYYY-MM-DD HH24:MI:SS')",
        "TIMESTAMP '2024-01-02 03:04:05.999999999'",
        "TIMESTAMP '2000-02-29 00:00:00 +08:00'",
    ),
    ("3", "'   '", "NULL", "NULL", "12345678901234567.123456", "1e20", "NULL", "NULL", "NULL"),
    (
        "4",
        "NULL",
        "UNISTR('\\241F\\2400')",
        "UNISTR('\\4E2D')",
        "0",
        "-0.5",
        "DATE '1999-01-01'",
        "TIMESTAMP '1999-01-01 00:00:00'",
        "NULL",
    ),
    (
        "5",
        "'quote''s \\ back'",
        "UNISTR('MiXeD Case')",
        "'Aa  '",
        "99.9999995",
        "3.0000000000000004",
        "DATE '2024-02-29'",
        "TIMESTAMP '2024-02-29 01:02:03.4'",
        "TIMESTAMP '2024-02-29 01:02:03.4 +05:45'",
    ),
]
_VIEW = (
    "("
    + " UNION ALL ".join(
        f"SELECT CAST({r[0]} AS NUMBER(10)) id, CAST({r[1]} AS VARCHAR2(40)) s, "
        f"CAST({r[2]} AS NVARCHAR2(40)) n, CAST({r[3]} AS CHAR(5)) c, "
        f"CAST({r[4]} AS NUMBER(30,9)) num, CAST({r[5]} AS BINARY_DOUBLE) bd, "
        f"CAST({r[6]} AS DATE) d, CAST({r[7]} AS TIMESTAMP(9)) ts, "
        f"CAST({r[8]} AS TIMESTAMP(6) WITH TIME ZONE) tz FROM DUAL"
        for r in _ROWS
    )
    + ")"
)
_SOURCE_INDEX = {"S": 0, "N": 1, "C": 2, "NUM": 3, "BD": 4, "D": 5, "TS": 6, "TZ": 7}
_COLUMNS = [
    CompareColumn(name="S", type=ColumnType.STRING),
    CompareColumn(name="N", type=ColumnType.STRING),
    CompareColumn(name="C", type=ColumnType.STRING),
    CompareColumn(name="NUM", type=ColumnType.DECIMAL),
    CompareColumn(name="BD", type=ColumnType.FLOAT, driver_type="BINARY_DOUBLE"),
    CompareColumn(name="D", type=ColumnType.DATETIME),
    CompareColumn(name="TS", type=ColumnType.DATETIME),
    CompareColumn(name="TZ", type=ColumnType.DATETIME),
    CompareColumn(name="D", type=ColumnType.DATE),
    CompareColumn(name="TS", type=ColumnType.TIME),
]
_KEY = [CompareColumn(name="ID", type=ColumnType.INTEGER)]
_PLACEHOLDER = '"GOLDEN"'


class _EnvSecretStore:
    def reveal_secret(self, ref: SecretRef) -> str:
        assert ref.kind is SecretKind.DATASOURCE_PASSWORD
        return _PASSWORD or ""


@pytest.fixture(scope="module")
def adapter() -> OracleAdapter:
    info = DatasourceConnInfo(
        host=_HOST or "",
        port=_PORT,
        username=_USER or "",
        database=_SERVICE,
        password_ref=SecretRef(ref="env", kind=SecretKind.DATASOURCE_PASSWORD),
        db_type=DbType.ORACLE,
    )
    return OracleAdapter(info, cast(SecretStore, _EnvSecretStore()))


@pytest.fixture(scope="module")
def source_rows(adapter: OracleAdapter) -> dict[Any, tuple[Any, ...]]:
    sql = f"SELECT ID, S, N, C, NUM, BD, D, TS, TZ FROM {_VIEW} ORDER BY ID"
    return {row.values[0]: tuple(row.values[1:]) for row in adapter.execute_select(sql, {})}


def _client_hash(values: tuple[Any, ...], rules: CompareRules) -> int:
    return compare_row_hash64(
        _COLUMNS, [values[_SOURCE_INDEX[column.name]] for column in _COLUMNS], rules
    )


@pytest.mark.parametrize(
    "rules",
    [
        CompareRules(),
        CompareRules(trim_strings=True, case_insensitive=True, empty_as_null=True),
        CompareRules(trim_strings=True),
        CompareRules(numeric_scale=0, timestamp_precision=0),
        CompareRules(numeric_scale=2, timestamp_precision=3),
        CompareRules(null_sentinel="<NULL>", field_separator="|"),
    ],
    ids=["default", "trim-case-empty", "trim", "scale0-prec0", "scale2-prec3", "ascii-separators"],
)
def test_row_hash_matches_client_hash(
    adapter: OracleAdapter, source_rows: dict[Any, tuple[Any, ...]], rules: CompareRules
) -> None:
    request = CompareHashRequest(
        table=CompareTableRef(name="GOLDEN"),
        columns=_COLUMNS,
        key_columns=_KEY,
        rules=rules,
        level=CompareHashLevel.ROW,
    )
    plan = adapter.build_compare_hash_query(request)
    assert plan.uses_database_hash, plan.degrade_reasons
    assert plan.sql is not None

    rows = adapter.execute_select(plan.sql.replace(_PLACEHOLDER, _VIEW), plan.params)
    db_hashes = {row.values[0]: int(row.values[1]) for row in rows}

    expected = {key: _client_hash(values, rules) for key, values in source_rows.items()}
    assert db_hashes == expected


def test_segment_aggregate_matches_client_sum(
    adapter: OracleAdapter, source_rows: dict[Any, tuple[Any, ...]]
) -> None:
    rules = CompareRules()
    request = CompareHashRequest(
        table=CompareTableRef(name="GOLDEN"),
        columns=_COLUMNS,
        key_columns=_KEY,
        rules=rules,
        segment=CompareSegment(key_column="ID", start=2, end=5),
        level=CompareHashLevel.AGGREGATE,
    )
    plan = adapter.build_compare_hash_query(request)
    assert plan.sql is not None

    row = next(iter(adapter.execute_select(plan.sql.replace(_PLACEHOLDER, _VIEW), plan.params)))

    in_segment = [values for key, values in source_rows.items() if 2 <= int(key) < 5]
    assert int(row.values[0]) == len(in_segment)
    assert int(row.values[1]) == sum(_client_hash(values, rules) for values in in_segment)
