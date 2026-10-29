"""Oracle 库内比对哈希下推(规范见 docs/design/compare-normalization-spec.md)。

与 DM 的差别都来自 Oracle 自身:

- **字符集**:现场库是 ZHS16GBK。缺省哨兵 / 分隔符(U+2400 / U+241F)不在 GBK 里,
  写成 VARCHAR2 字面量会变成 `?`;`UTL_I18N.STRING_TO_RAW` 对 NVARCHAR2 实参也会先经库
  字符集转一道(11.2.0.4 实测 U+2400 → EFBC9F)。所以载荷**按十六进制拼接**:固定字面量在
  Python 侧算好 UTF-8 十六进制,列值先 `TO_NCHAR` 再取国家字符集字节,用 `UTL_RAW.CONVERT`
  转到 AL32UTF8,全程不经库字符集。最后 `HEXTORAW` 一次喂给 MD5。
- **`''` 即 NULL**:TRIM 掉全空格后得到 NULL,但客户端是 `''`。NULL 位按规则单独判定
  (不开 empty_as_null 时只看原列是否为 NULL),值为 NULL 时按空串拼接、长度记 0。
- **长度**:`LENGTH` 对 NVARCHAR2 数 UTF-16 码元(😀 记 2),Python `len` 数码点;用 `LENGTHC`。
- **时间截断**:客户端按精度截断小数秒,`CAST(... AS TIMESTAMP(p))` 会四舍五入;
  改为按 FF9 取全精度再 SUBSTR。
- **MD5**:12c+ `STANDARD_HASH`;有授权用 `DBMS_CRYPTO`;11g 最小权限账号只有
  `DBMS_OBFUSCATION_TOOLKIT.MD5`(对 PUBLIC 可执行)。选哪个由 OracleAdapter 探测。

载荷上限:十六进制串受 VARCHAR2 4000 限制,即 UTF-8 载荷 ≤ 2000 字节(MD5 的 RAW 入参在
SQL 里也是 2000)。超长行执行时报 ORA-01489,worker 记日志后整任务退回客户端哈希。
"""

from __future__ import annotations

import hashlib
import re

from app.dbclients.dm_compare import HEX_TO_UINT64_TO_NUMBER, hex_to_uint64_expression
from app.domain.compare import (
    CompareColumn,
    CompareHashExecutionMode,
    CompareHashLevel,
    CompareHashPlan,
    CompareHashRequest,
)
from app.domain.schema import ColumnType

ORACLE_MD5_STANDARD_HASH = "standard_hash"
ORACLE_MD5_DBMS_CRYPTO = "dbms_crypto"
ORACLE_MD5_OBFUSCATION_TOOLKIT = "dbms_obfuscation_toolkit"
# 探测顺序(设计稿 §6.1.1):STANDARD_HASH → DBMS_CRYPTO → DBMS_OBFUSCATION_TOOLKIT → 客户端
ORACLE_MD5_FUNCTIONS = (
    ORACLE_MD5_STANDARD_HASH,
    ORACLE_MD5_DBMS_CRYPTO,
    ORACLE_MD5_OBFUSCATION_TOOLKIT,
)
_MD5_SQL = {
    ORACLE_MD5_STANDARD_HASH: "STANDARD_HASH({raw}, 'MD5')",
    ORACLE_MD5_DBMS_CRYPTO: "DBMS_CRYPTO.HASH({raw}, 2)",
    ORACLE_MD5_OBFUSCATION_TOOLKIT: "DBMS_OBFUSCATION_TOOLKIT.MD5(input => {raw})",
}
DEFAULT_NCHAR_CHARSET = "AL16UTF16"
_NCHAR_CHARSETS = {"AL16UTF16", "UTF8"}

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.$-]+$")
_UTF8_CHARSETS = {"utf8", "utf8mb4", "al32utf8"}
# BOOLEAN 不收:Oracle 没有布尔列,DM 那套 IN (1, 'Y', ...) 混类型比较在 Oracle 上报 ORA-01722。
_SUPPORTED_DB_HASH_TYPES = {
    ColumnType.STRING,
    ColumnType.INTEGER,
    ColumnType.FLOAT,
    ColumnType.DECIMAL,
    ColumnType.DATETIME,
    ColumnType.DATE,
    ColumnType.TIME,
}
# 归 STRING 但进不了 TO_NCHAR 拼接的类型。driver_type 缺失(前端建的比对任务不带)时
# 无从判断,执行时报错,由 worker 退回客户端哈希。
_UNSUPPORTED_STRING_DRIVER_TYPES = {"CLOB", "NCLOB", "LONG", "XMLTYPE"}
# FLOAT 桶里只有这两种是 IEEE 二进制浮点(客户端拿到 float);FLOAT / REAL / DOUBLE PRECISION
# 在 Oracle 里都是 NUMBER(客户端拿到 Decimal)。两者规范化写法不同,driver_type 缺失时分不清。
_BINARY_FLOAT_DRIVER_TYPES = {"BINARY_DOUBLE", "BINARY_FLOAT"}
_NLS_NUMERIC = "'NLS_NUMERIC_CHARACTERS=''.,'''"
_LEADING_WORD_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_]*)")

# 探测载荷覆盖 ASCII、GBK 内字符、GBK 外 BMP 字符(哨兵)与增补平面字符。
_PROBE_TEXT = "a中␀\U0001f600"


def build_oracle_compare_hash_plan(
    request: CompareHashRequest,
    *,
    md5_function: str | None,
    nchar_charset: str = DEFAULT_NCHAR_CHARSET,
) -> CompareHashPlan:
    """构造 Oracle 下推哈希计划;md5_function 为 None 表示探测没找到可用 MD5。"""
    reasons = _degrade_reasons(request, md5_function, nchar_charset)
    if reasons or md5_function is None:
        return CompareHashPlan(
            execution_mode=CompareHashExecutionMode.CLIENT_ROW_HASH,
            degrade_reasons=reasons,
        )

    payload_hex = _row_payload_hex_expression(request, nchar_charset)
    hash_hex_expr = _hash_hex_expression(payload_hex, md5_function)
    hash_hex_alias = '"__HASH_HEX"'
    row_hash_expr = hex_to_uint64_expression(hash_hex_alias, HEX_TO_UINT64_TO_NUMBER)
    table_expr = _table_expression(request.table.schema_name, request.table.name)
    where_clause = _where_clause(request)
    if request.level is CompareHashLevel.ROW:
        key_exprs = ", ".join(
            f"{_quote_identifier(column.name)} AS {_quote_identifier(column.name)}"
            for column in request.key_columns
        )
        hash_source_exprs = (
            f"{key_exprs}, {hash_hex_expr} AS {hash_hex_alias}"
            if key_exprs
            else f"{hash_hex_expr} AS {hash_hex_alias}"
        )
        select_exprs = (
            f'{key_exprs}, {row_hash_expr} AS "__ROW_HASH64"'
            if key_exprs
            else f'{row_hash_expr} AS "__ROW_HASH64"'
        )
        sql = (
            f"SELECT {select_exprs} FROM "
            f"(SELECT {hash_source_exprs} FROM {table_expr}{where_clause}) "
            "DATAOPS_COMPARE_HEX"
        )
    else:
        sql = (
            'SELECT COUNT(*) AS "ROW_COUNT", '
            'NVL(SUM(CAST("__ROW_HASH64" AS DECIMAL(38,0))), '
            'CAST(0 AS DECIMAL(38,0))) AS "AGGREGATE_HASH" '
            f'FROM (SELECT {row_hash_expr} AS "__ROW_HASH64" '
            f"FROM (SELECT {hash_hex_expr} AS {hash_hex_alias} FROM {table_expr}{where_clause}) "
            "DATAOPS_COMPARE_HEX) "
            "DATAOPS_COMPARE_HASH"
        )
    return CompareHashPlan(
        execution_mode=CompareHashExecutionMode.DB_HASH,
        sql=sql,
        params=_segment_params(request),
        paramstyle="named",
        normalized_payload_expression=payload_hex,
        row_hash_expression=row_hash_expr,
    )


def md5_probe_sql(md5_function: str, nchar_charset: str = DEFAULT_NCHAR_CHARSET) -> str:
    """探测 SQL:对固定载荷走完整链路(UNISTR → UTF-8 字节 → MD5 → uint64)。

    结果须等于 `md5_probe_expected()`;报错或不等都说明该函数在此库上不可用。
    """
    literal = "UNISTR(" + _sql_string(_unistr_escape(_PROBE_TEXT)) + ")"
    payload_hex = _nchar_to_utf8_hex(literal, nchar_charset)
    hash_hex = _hash_hex_expression(payload_hex, md5_function)
    return f"SELECT {hex_to_uint64_expression(hash_hex, HEX_TO_UINT64_TO_NUMBER)} FROM DUAL"


def md5_probe_expected() -> int:
    digest = hashlib.md5(_PROBE_TEXT.encode("utf-8"), usedforsecurity=False).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


def _degrade_reasons(
    request: CompareHashRequest,
    md5_function: str | None,
    nchar_charset: str,
) -> list[str]:
    reasons: list[str] = []
    if md5_function is None:
        reasons.append(
            "oracle compare db hash found no usable MD5 function "
            "(STANDARD_HASH / DBMS_CRYPTO / DBMS_OBFUSCATION_TOOLKIT)"
        )
    elif md5_function not in _MD5_SQL:
        raise ValueError(f"unknown oracle md5 function: {md5_function}")
    if nchar_charset not in _NCHAR_CHARSETS:
        reasons.append(f"oracle national charset {nchar_charset} is not supported for db hash")
    if not request.columns:
        reasons.append("oracle compare db hash requires at least one hash column")
    for column in request.columns:
        if column.type not in _SUPPORTED_DB_HASH_TYPES:
            reasons.append(
                f"column {column.name} type {column.type.value} requires client row hash"
            )
        elif _driver_base_type(column) in _UNSUPPORTED_STRING_DRIVER_TYPES:
            reasons.append(
                f"column {column.name} driver type {_driver_base_type(column)} "
                "requires client row hash"
            )
        elif column.type is ColumnType.FLOAT and column.driver_type is None:
            reasons.append(
                f"column {column.name} float type without driver_type "
                "(NUMBER vs BINARY_DOUBLE) requires client row hash"
            )
    charset = request.rules.charset.replace("-", "").replace("_", "").lower()
    if charset not in _UTF8_CHARSETS:
        reasons.append("oracle compare db hash requires a UTF-8 normalization charset")
    return reasons


def _driver_base_type(column: CompareColumn) -> str | None:
    if column.driver_type is None:
        return None
    match = _LEADING_WORD_RE.match(column.driver_type)
    return match.group(1).upper() if match else None


def _row_payload_hex_expression(request: CompareHashRequest, nchar_charset: str) -> str:
    """规范化载荷的 UTF-8 十六进制串:NULL 位、(长度, 值)对,按分隔符连接。"""
    rules = request.rules
    separator = _utf8_hex_literal(rules.field_separator)
    sentinel_hex = _utf8_hex_literal(rules.null_sentinel)
    sentinel_length_hex = _utf8_hex_literal(str(len(rules.null_sentinel)))
    null_bits: list[str] = []
    value_parts: list[str] = []
    for column in request.columns:
        null_cond, value_expr, is_text = _normalized_column(column, request)
        null_bits.append(f"CASE WHEN {null_cond} THEN '31' ELSE '30' END")
        # UTL_RAW.CONVERT(NULL) 报 ORA-06502,值为 NULL(Oracle 的空串)时不进转换,
        # 拼接时 NULL 即空串,长度记 0。
        value_hex = (
            _nchar_to_utf8_hex(value_expr, nchar_charset) if is_text else _ascii_hex(value_expr)
        )
        length_hex = _ascii_hex(f"TO_CHAR(NVL(LENGTHC({value_expr}), 0))")
        value_parts.append(
            f"CASE WHEN {null_cond} THEN {sentinel_length_hex} ELSE {length_hex} END"
        )
        value_parts.append(
            f"CASE WHEN {null_cond} THEN {sentinel_hex} "
            f"WHEN {value_expr} IS NULL THEN NULL ELSE {value_hex} END"
        )
    return "(" + f" || {separator} || ".join([*null_bits, *value_parts]) + ")"


def _normalized_column(column: CompareColumn, request: CompareHashRequest) -> tuple[str, str, bool]:
    """返回 (NULL 位条件, 规范化值表达式, 值是否需按国家字符集转码)。

    非文本列的规范化值只含 ASCII,按库字符集原样取字节即可。
    """
    rules = request.rules
    quoted = _quote_identifier(column.name)
    null_cond = f"{quoted} IS NULL"
    if column.type in {ColumnType.INTEGER, ColumnType.DECIMAL, ColumnType.FLOAT}:
        number = (
            _shortest_round_trip_number(f"CAST({quoted} AS BINARY_DOUBLE)")
            if _driver_base_type(column) in _BINARY_FLOAT_DRIVER_TYPES
            else quoted
        )
        value = (
            f"TO_CHAR(ROUND({number}, {rules.numeric_scale}), "
            f"{_sql_string(_number_format(rules.numeric_scale))}, {_NLS_NUMERIC})"
        )
        return null_cond, value, False
    if column.type is ColumnType.DATE:
        return null_cond, f"TO_CHAR({quoted}, 'YYYY-MM-DD')", False
    if column.type is ColumnType.TIME:
        return (
            null_cond,
            _truncated_fraction(quoted, "HH24:MI:SS", 8, rules.timestamp_precision),
            False,
        )
    if column.type is ColumnType.DATETIME:
        return (
            null_cond,
            _truncated_fraction(quoted, "YYYY-MM-DD HH24:MI:SS", 19, rules.timestamp_precision),
            False,
        )

    value = f"TO_NCHAR({quoted})"
    if rules.trim_strings:
        value = f"TRIM({value})"
    if rules.case_insensitive:
        value = f"LOWER({value})"
    if rules.empty_as_null:
        # 客户端去空格后为 '' 即 NULL;Oracle 里 TRIM 出的空串本身就是 NULL
        null_cond = f"{value} IS NULL"
    return null_cond, value, True


def _shortest_round_trip_number(binary_double: str) -> str:
    """BINARY_DOUBLE → 与 Python repr 相同的最短往返十进制(NUMBER)。

    客户端按 Decimal(str(v)) 舍入,str 取最短往返表示(2.675);Oracle 直接转 NUMBER 得到
    二进制精确值 2.67499…,ROUND 到 2 位就差一分。依次试 15、16、17 位有效数字,取第一个
    转回 BINARY_DOUBLE 仍相等的;比最短表示更长的正确舍入只是补零,数值相同。
    """
    candidates = [_significant_digits(binary_double, digits) for digits in (15, 16)]
    whens = " ".join(
        f"WHEN CAST({candidate} AS BINARY_DOUBLE) = {binary_double} THEN {candidate}"
        for candidate in candidates
    )
    return f"CASE {whens} ELSE {_significant_digits(binary_double, 17)} END"


def _significant_digits(binary_double: str, digits: int) -> str:
    model = _sql_string("FM9." + "9" * (digits - 1) + "EEEE")
    return f"TO_NUMBER(TO_CHAR({binary_double}, {model}, {_NLS_NUMERIC}), {model}, {_NLS_NUMERIC})"


def _truncated_fraction(expr: str, base_format: str, base_length: int, precision: int) -> str:
    """按 FF9 取全精度后截断到 precision 位,与客户端 _append_fraction 的截断一致。"""
    length = base_length if precision == 0 else base_length + 1 + precision
    return f"SUBSTR(TO_CHAR(CAST({expr} AS TIMESTAMP(9)), '{base_format}.FF9'), 1, {length})"


def _number_format(scale: int) -> str:
    fractional = "" if scale == 0 else "D" + ("0" * scale)
    return "FM99999999999999999999999999999999999990" + fractional


def _nchar_to_utf8_hex(expr: str, nchar_charset: str) -> str:
    """NVARCHAR2 表达式 → 其 UTF-8 字节的十六进制串,不经库字符集。"""
    return (
        f"RAWTOHEX(UTL_RAW.CONVERT(UTL_RAW.CAST_TO_RAW({expr}), "
        f"'AMERICAN_AMERICA.AL32UTF8', 'AMERICAN_AMERICA.{nchar_charset}'))"
    )


def _ascii_hex(expr: str) -> str:
    """纯 ASCII 的 VARCHAR2 表达式 → 十六进制串(ASCII 在库字符集与 UTF-8 下字节相同)。"""
    return f"RAWTOHEX(UTL_RAW.CAST_TO_RAW({expr}))"


def _hash_hex_expression(payload_hex: str, md5_function: str) -> str:
    raw = f"HEXTORAW({payload_hex})"
    return f"RAWTOHEX({_MD5_SQL[md5_function].format(raw=raw)})"


def _utf8_hex_literal(text: str) -> str:
    # 空串在 Oracle 里是 NULL,拼接时等同空串,与客户端一致
    return _sql_string(text.encode("utf-8").hex().upper())


def _unistr_escape(text: str) -> str:
    units = text.encode("utf-16-be")
    return "".join(
        "\\" + units[index : index + 2].hex().upper() for index in range(0, len(units), 2)
    )


def _where_clause(request: CompareHashRequest) -> str:
    segment = request.segment
    if segment is None:
        return ""
    key = _quote_identifier(segment.key_column)
    return f" WHERE {key} >= :segment_start AND {key} < :segment_end"


def _segment_params(request: CompareHashRequest) -> dict[str, object]:
    segment = request.segment
    if segment is None:
        return {}
    return {"segment_start": segment.start, "segment_end": segment.end}


def _table_expression(schema_name: str | None, table_name: str) -> str:
    if schema_name:
        return f"{_quote_identifier(schema_name)}.{_quote_identifier(table_name)}"
    return _quote_identifier(table_name)


def _quote_identifier(identifier: str) -> str:
    if not identifier or _IDENTIFIER_RE.fullmatch(identifier) is None:
        raise ValueError("Invalid database identifier")
    quoted_parts = []
    for part in identifier.split("."):
        escaped = part.replace('"', '""').upper()
        quoted_parts.append(f'"{escaped}"')
    return ".".join(quoted_parts)


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


__all__ = [
    "DEFAULT_NCHAR_CHARSET",
    "ORACLE_MD5_DBMS_CRYPTO",
    "ORACLE_MD5_FUNCTIONS",
    "ORACLE_MD5_OBFUSCATION_TOOLKIT",
    "ORACLE_MD5_STANDARD_HASH",
    "build_oracle_compare_hash_plan",
    "md5_probe_expected",
    "md5_probe_sql",
]
