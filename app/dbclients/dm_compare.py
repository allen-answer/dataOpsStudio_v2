from __future__ import annotations

import re

from app.domain.compare import (
    CompareColumn,
    CompareHashExecutionMode,
    CompareHashLevel,
    CompareHashPlan,
    CompareHashRequest,
)
from app.domain.schema import ColumnType

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.$-]+$")
_SUPPORTED_DB_HASH_TYPES = {
    ColumnType.STRING,
    ColumnType.INTEGER,
    ColumnType.FLOAT,
    ColumnType.DECIMAL,
    ColumnType.BOOLEAN,
    ColumnType.DATETIME,
    ColumnType.DATE,
    ColumnType.TIME,
}


def build_dm_compare_hash_plan(request: CompareHashRequest) -> CompareHashPlan:
    reasons = _degrade_reasons(request.columns)
    if reasons:
        return CompareHashPlan(
            execution_mode=CompareHashExecutionMode.CLIENT_ROW_HASH,
            degrade_reasons=reasons,
        )

    payload_expr = _row_payload_expression(request)
    hash_hex_expr = (
        f"RAWTOHEX(DBMS_CRYPTO.HASH(UTL_RAW.CAST_TO_RAW({payload_expr}), DBMS_CRYPTO.HASH_MD5))"
    )
    row_hash_expr = f"TO_NUMBER(SUBSTR({hash_hex_expr}, 1, 16), 'XXXXXXXXXXXXXXXX')"
    table_expr = _table_expression(request.table.schema_name, request.table.name)
    where_clause = _where_clause(request)
    if request.level is CompareHashLevel.ROW:
        key_exprs = ", ".join(
            f"{_quote_identifier(column.name)} AS {_quote_identifier(column.name)}"
            for column in request.key_columns
        )
        select_exprs = (
            f'{key_exprs}, {row_hash_expr} AS "__ROW_HASH64"'
            if key_exprs
            else (f'{row_hash_expr} AS "__ROW_HASH64"')
        )
        sql = f"SELECT {select_exprs} FROM {table_expr}{where_clause}"
    else:
        sql = (
            'SELECT COUNT(*) AS "ROW_COUNT", '
            'NVL(SUM("__ROW_HASH64"), 0) AS "AGGREGATE_HASH" '
            f'FROM (SELECT {row_hash_expr} AS "__ROW_HASH64" FROM {table_expr}{where_clause}) '
            "DATAOPS_COMPARE_HASH"
        )
    return CompareHashPlan(
        execution_mode=CompareHashExecutionMode.DB_HASH,
        sql=sql,
        params=_segment_params(request),
        paramstyle="named",
        normalized_payload_expression=payload_expr,
        row_hash_expression=row_hash_expr,
    )


def _degrade_reasons(columns: list[CompareColumn]) -> list[str]:
    reasons: list[str] = []
    for column in columns:
        if column.type not in _SUPPORTED_DB_HASH_TYPES:
            reasons.append(
                f"column {column.name} type {column.type.value} requires client row hash"
            )
    return reasons


def _row_payload_expression(request: CompareHashRequest) -> str:
    rules = request.rules
    normalized_exprs = [_normalized_value_expression(column, request) for column in request.columns]
    null_bits = [f"CASE WHEN ({expr}) IS NULL THEN '1' ELSE '0' END" for expr in normalized_exprs]
    normalized_values = [_coalesced_value(expr, request) for expr in normalized_exprs]
    parts = [*null_bits, *normalized_values]
    separator = _sql_string(rules.field_separator)
    return "(" + f" || {separator} || ".join(parts) + ")"


def _coalesced_value(value_expr: str, request: CompareHashRequest) -> str:
    return f"COALESCE({value_expr}, {_sql_string(request.rules.null_sentinel)})"


def _normalized_value_expression(column: CompareColumn, request: CompareHashRequest) -> str:
    rules = request.rules
    quoted = _quote_identifier(column.name)
    if column.type in {ColumnType.INTEGER, ColumnType.DECIMAL, ColumnType.FLOAT}:
        return (
            f"TO_CHAR(ROUND({quoted}, {rules.numeric_scale}), "
            f"{_sql_string(_dm_number_format(rules.numeric_scale))}, "
            "'NLS_NUMERIC_CHARACTERS=''.,''')"
        )
    if column.type is ColumnType.BOOLEAN:
        return (
            f"CASE WHEN {quoted} IS NULL THEN NULL "
            f"WHEN {quoted} IN (1, '1', 'Y', 'y', 'TRUE', 'true') THEN '1' ELSE '0' END"
        )
    if column.type is ColumnType.DATE:
        return f"TO_CHAR({quoted}, 'YYYY-MM-DD')"
    if column.type is ColumnType.TIME:
        return _dm_time_expr(quoted, rules.timestamp_precision)
    if column.type is ColumnType.DATETIME:
        return _dm_timestamp_expr(quoted, rules.timestamp_precision)

    expr = f"CONVERT(CAST({quoted} AS VARCHAR(4000)), 'UTF8')"
    if rules.trim_strings:
        expr = f"TRIM({expr})"
    if rules.case_insensitive:
        expr = f"LOWER({expr})"
    if rules.empty_as_null:
        expr = f"NULLIF({expr}, '')"
    return expr


def _dm_number_format(scale: int) -> str:
    fractional = "" if scale == 0 else "D" + ("0" * scale)
    return "FM99999999999999999999999999999999999990" + fractional


def _dm_time_expr(expr: str, precision: int) -> str:
    if precision == 0:
        return f"TO_CHAR(CAST({expr} AS TIMESTAMP(0)), 'HH24:MI:SS')"
    return f"TO_CHAR(CAST({expr} AS TIMESTAMP({precision})), 'HH24:MI:SS.FF{precision}')"


def _dm_timestamp_expr(expr: str, precision: int) -> str:
    if precision == 0:
        return f"TO_CHAR(CAST({expr} AS TIMESTAMP(0)), 'YYYY-MM-DD HH24:MI:SS')"
    return f"TO_CHAR(CAST({expr} AS TIMESTAMP({precision})), 'YYYY-MM-DD HH24:MI:SS.FF{precision}')"


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


__all__ = ["build_dm_compare_hash_plan"]
