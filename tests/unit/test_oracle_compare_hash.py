"""Oracle 下推哈希计划单测(不连库)。

数值与客户端 compare_row_hash64 逐行一致的证据来自真库(11.2.0.4,ZHS16GBK):
内联视图覆盖 GBK 外字符 / 增补平面 / 全空格 / CHAR 补齐 / BINARY_DOUBLE 舍入边界 /
FF9 时间戳 / 时区,六组规则全部一致;ALL_OBJECTS 3000 行一致。这里锁住构造上的不变式。
"""

from __future__ import annotations

import hashlib

import pytest

from app.dbclients.oracle_compare import (
    ORACLE_MD5_DBMS_CRYPTO,
    ORACLE_MD5_FUNCTIONS,
    ORACLE_MD5_OBFUSCATION_TOOLKIT,
    ORACLE_MD5_STANDARD_HASH,
    build_oracle_compare_hash_plan,
    md5_probe_expected,
    md5_probe_sql,
)
from app.domain.compare import (
    CompareColumn,
    CompareHashLevel,
    CompareHashRequest,
    CompareRules,
    CompareSegment,
    CompareTableRef,
)
from app.domain.schema import ColumnType

_KEY = CompareColumn(name="ID", type=ColumnType.INTEGER)


def _request(
    *columns: CompareColumn,
    rules: CompareRules | None = None,
    level: CompareHashLevel = CompareHashLevel.AGGREGATE,
    segment: CompareSegment | None = None,
) -> CompareHashRequest:
    return CompareHashRequest(
        table=CompareTableRef(schema_name="dw", name="t"),
        columns=list(columns) or [CompareColumn(name="AMT", type=ColumnType.DECIMAL)],
        key_columns=[_KEY],
        rules=rules or CompareRules(),
        segment=segment,
        level=level,
    )


def _plan_sql(request: CompareHashRequest, md5: str = ORACLE_MD5_OBFUSCATION_TOOLKIT) -> str:
    plan = build_oracle_compare_hash_plan(request, md5_function=md5)
    assert plan.uses_database_hash, plan.degrade_reasons
    assert plan.sql is not None
    return plan.sql


def test_sql_is_pure_ascii_even_with_default_unicode_sentinel() -> None:
    # 缺省哨兵 U+2400 / 分隔符 U+241F 不在 GBK 里;SQL 里一旦出现非 ASCII 字面量,
    # 在 ZHS16GBK 库上会变成 '?',整库哈希静默错位。只允许以 UTF-8 十六进制出现。
    sql = _plan_sql(_request(CompareColumn(name="NAME", type=ColumnType.STRING)))

    assert sql.isascii()
    assert "'E29080'" in sql  # U+2400 的 UTF-8
    assert "'E2909F'" in sql  # U+241F 的 UTF-8


@pytest.mark.parametrize(
    ("md5", "fragment"),
    [
        (ORACLE_MD5_STANDARD_HASH, "STANDARD_HASH(HEXTORAW("),
        (ORACLE_MD5_DBMS_CRYPTO, "DBMS_CRYPTO.HASH(HEXTORAW("),
        (ORACLE_MD5_OBFUSCATION_TOOLKIT, "DBMS_OBFUSCATION_TOOLKIT.MD5(input => HEXTORAW("),
    ],
)
def test_md5_function_is_rendered(md5: str, fragment: str) -> None:
    assert fragment in _plan_sql(_request(), md5)


def test_text_goes_through_national_charset_bytes_not_db_charset() -> None:
    sql = _plan_sql(_request(CompareColumn(name="NAME", type=ColumnType.STRING)))

    # UTL_I18N.STRING_TO_RAW 会先经库字符集(11.2.0.4 实测 U+2400 → EFBC9F),不得使用
    assert "UTL_I18N" not in sql
    assert (
        'UTL_RAW.CONVERT(UTL_RAW.CAST_TO_RAW(TO_NCHAR("NAME")), '
        "'AMERICAN_AMERICA.AL32UTF8', 'AMERICAN_AMERICA.AL16UTF16')"
    ) in sql
    # UTL_RAW.CONVERT(NULL) 报 ORA-06502:值为 NULL 时必须先短路
    assert 'WHEN TO_NCHAR("NAME") IS NULL THEN NULL ELSE RAWTOHEX(UTL_RAW.CONVERT(' in sql


def test_length_counts_code_points_like_python() -> None:
    sql = _plan_sql(_request(CompareColumn(name="NAME", type=ColumnType.STRING)))

    assert "LENGTHC(" in sql
    assert "LENGTH(" not in sql.replace("LENGTHC(", "")


def test_null_bit_tracks_original_column_unless_empty_as_null() -> None:
    column = CompareColumn(name="NAME", type=ColumnType.STRING)

    default_sql = _plan_sql(_request(column, rules=CompareRules(trim_strings=True)))
    empty_sql = _plan_sql(
        _request(column, rules=CompareRules(trim_strings=True, empty_as_null=True))
    )

    # 不开 empty_as_null:全空格 TRIM 成 NULL 仍算非空值(客户端是 ''),NULL 位只看原列
    assert "CASE WHEN \"NAME\" IS NULL THEN '31' ELSE '30' END" in default_sql
    assert "CASE WHEN TRIM(TO_NCHAR(\"NAME\")) IS NULL THEN '31' ELSE '30' END" in empty_sql


@pytest.mark.parametrize(("precision", "length"), [(0, 19), (3, 23), (6, 26)])
def test_datetime_truncates_fraction_instead_of_rounding(precision: int, length: int) -> None:
    sql = _plan_sql(
        _request(
            CompareColumn(name="TS", type=ColumnType.DATETIME),
            rules=CompareRules(timestamp_precision=precision),
        )
    )

    assert (
        f"SUBSTR(TO_CHAR(CAST(\"TS\" AS TIMESTAMP(9)), 'YYYY-MM-DD HH24:MI:SS.FF9'), 1, {length})"
        in sql
    )


def test_binary_double_uses_shortest_round_trip_digits() -> None:
    sql = _plan_sql(
        _request(CompareColumn(name="BD", type=ColumnType.FLOAT, driver_type="BINARY_DOUBLE"))
    )

    for digits in (15, 16, 17):
        assert "'FM9." + "9" * (digits - 1) + "EEEE'" in sql
    assert 'CAST("BD" AS BINARY_DOUBLE)' in sql


def test_number_based_float_rounds_the_column_directly() -> None:
    sql = _plan_sql(_request(CompareColumn(name="F", type=ColumnType.FLOAT, driver_type="FLOAT")))

    assert 'ROUND("F", 6)' in sql
    assert "EEEE" not in sql


@pytest.mark.parametrize(
    ("column", "reason"),
    [
        (CompareColumn(name="B", type=ColumnType.BYTES), "type bytes"),
        (CompareColumn(name="FLAG", type=ColumnType.BOOLEAN), "type boolean"),
        (CompareColumn(name="DOC", type=ColumnType.STRING, driver_type="CLOB"), "driver type CLOB"),
        (CompareColumn(name="L", type=ColumnType.STRING, driver_type="LONG"), "driver type LONG"),
        (CompareColumn(name="F", type=ColumnType.FLOAT), "without driver_type"),
    ],
)
def test_unsupported_columns_degrade_to_client_hash(column: CompareColumn, reason: str) -> None:
    plan = build_oracle_compare_hash_plan(
        _request(column), md5_function=ORACLE_MD5_OBFUSCATION_TOOLKIT
    )

    assert plan.uses_database_hash is False
    assert plan.sql is None
    assert any(reason in item for item in plan.degrade_reasons)


def test_missing_md5_function_degrades() -> None:
    plan = build_oracle_compare_hash_plan(_request(), md5_function=None)

    assert plan.uses_database_hash is False
    assert any("no usable MD5" in item for item in plan.degrade_reasons)


def test_non_utf8_normalization_charset_degrades() -> None:
    plan = build_oracle_compare_hash_plan(
        _request(rules=CompareRules(charset="gbk", collation="gbk_bin")),
        md5_function=ORACLE_MD5_STANDARD_HASH,
    )

    assert any("UTF-8 normalization charset" in item for item in plan.degrade_reasons)


def test_unknown_md5_function_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        build_oracle_compare_hash_plan(_request(), md5_function="sha1")


def test_aggregate_plan_binds_segment_and_sums_in_decimal() -> None:
    segment = CompareSegment(key_column="ID", start=10, end=20)
    plan = build_oracle_compare_hash_plan(
        _request(segment=segment), md5_function=ORACLE_MD5_STANDARD_HASH
    )

    assert plan.params == {"segment_start": 10, "segment_end": 20}
    assert plan.paramstyle == "named"
    assert plan.sql is not None
    assert 'FROM "DW"."T" WHERE "ID" >= :segment_start AND "ID" < :segment_end' in plan.sql
    assert 'NVL(SUM(CAST("__ROW_HASH64" AS DECIMAL(38,0)))' in plan.sql


def test_row_plan_selects_keys_and_row_hash() -> None:
    sql = _plan_sql(_request(level=CompareHashLevel.ROW))

    assert sql.startswith('SELECT "ID" AS "ID", ')
    assert 'AS "__ROW_HASH64" FROM (SELECT "ID" AS "ID", ' in sql


@pytest.mark.parametrize("md5", ORACLE_MD5_FUNCTIONS)
def test_probe_sql_is_ascii_and_covers_non_gbk_characters(md5: str) -> None:
    sql = md5_probe_sql(md5)

    assert sql.isascii()
    # U+2400(GBK 外)与 U+1F600(增补平面,UTF-16 代理对)都在探测载荷里
    assert "\\2400" in sql and "\\D83D\\DE00" in sql


def test_probe_expected_value_uses_compare_row_hash_semantics() -> None:
    # 与 compare_row_hash64 同一口径:UTF-8 字节的 md5 前 8 字节,大端无符号
    digest = hashlib.md5("a中␀\U0001f600".encode(), usedforsecurity=False).digest()
    assert md5_probe_expected() == int.from_bytes(digest[:8], "big", signed=False)
