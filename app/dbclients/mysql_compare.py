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


def build_mysql_compare_hash_plan(request: CompareHashRequest) -> CompareHashPlan:
    reasons = _degrade_reasons(request.columns)
    if reasons:
        return CompareHashPlan(
            execution_mode=CompareHashExecutionMode.CLIENT_ROW_HASH,
            degrade_reasons=reasons,
        )

    payload_expr = _row_payload_expression(request)
    row_hash_expr = f"CAST(CONV(SUBSTR(MD5({payload_expr}), 1, 16), 16, 10) AS DECIMAL(20,0))"
    table_expr = _table_expression(request.table.schema_name, request.table.name)
    where_clause = _where_clause(request)
    if request.level is CompareHashLevel.ROW:
        key_exprs = ", ".join(
            f"{_quote_identifier(column.name)} AS {_quote_identifier(column.name)}"
            for column in request.key_columns
        )
        select_exprs = (
            f"{key_exprs}, {row_hash_expr} AS __row_hash64"
            if key_exprs
            else (f"{row_hash_expr} AS __row_hash64")
        )
        sql = f"SELECT {select_exprs} FROM {table_expr}{where_clause}"
    else:
        sql = (
            "SELECT COUNT(*) AS row_count, "
            "COALESCE(SUM(__row_hash64), 0) AS aggregate_hash "
            f"FROM (SELECT {row_hash_expr} AS __row_hash64 FROM {table_expr}{where_clause}) "
            "dataops_compare_hash"
        )
    return CompareHashPlan(
        execution_mode=CompareHashExecutionMode.DB_HASH,
        sql=sql,
        params=_segment_params(request),
        paramstyle="pyformat",
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
    null_bits = [f"IF(({expr}) IS NULL, '1', '0')" for expr in normalized_exprs]
    normalized_values = [_coalesced_value(expr, request) for expr in normalized_exprs]
    parts = [*null_bits, *normalized_values]
    separator = _sql_string(rules.field_separator)
    return f"CONCAT_WS({separator}, {', '.join(parts)})"


def _coalesced_value(value_expr: str, request: CompareHashRequest) -> str:
    return f"COALESCE({value_expr}, {_sql_string(request.rules.null_sentinel)})"


def _normalized_value_expression(column: CompareColumn, request: CompareHashRequest) -> str:
    rules = request.rules
    quoted = _quote_identifier(column.name)
    if column.type in {ColumnType.INTEGER, ColumnType.DECIMAL}:
        return f"CAST(CAST({quoted} AS DECIMAL(65,{rules.numeric_scale})) AS CHAR)"
    if column.type is ColumnType.FLOAT:
        return (
            f"CAST(CAST(ROUND({quoted}, {rules.numeric_scale}) "
            f"AS DECIMAL(65,{rules.numeric_scale})) AS CHAR)"
        )
    if column.type is ColumnType.BOOLEAN:
        return f"CASE WHEN {quoted} IS NULL THEN NULL WHEN {quoted} THEN '1' ELSE '0' END"
    if column.type is ColumnType.DATE:
        return f"DATE_FORMAT({quoted}, '%Y-%m-%d')"
    if column.type is ColumnType.TIME:
        return _truncate_fraction(
            f"DATE_FORMAT({quoted}, '%H:%i:%s.%f')",
            rules.timestamp_precision,
        )
    if column.type is ColumnType.DATETIME:
        return _truncate_fraction(
            f"DATE_FORMAT({quoted}, '%Y-%m-%d %H:%i:%s.%f')",
            rules.timestamp_precision,
        )

    expr = f"CAST({quoted} AS CHAR CHARACTER SET {rules.charset}) COLLATE {rules.collation}"
    if rules.trim_strings:
        expr = f"TRIM({expr})"
    if rules.case_insensitive:
        expr = f"LOWER({expr})"
    if rules.empty_as_null:
        expr = f"NULLIF({expr}, '')"
    return expr


def _truncate_fraction(expr: str, precision: int) -> str:
    if precision >= 6:
        return expr
    if precision == 0:
        return f"SUBSTR({expr}, 1, CHAR_LENGTH({expr}) - 7)"
    return f"SUBSTR({expr}, 1, CHAR_LENGTH({expr}) - {6 - precision})"


def _where_clause(request: CompareHashRequest) -> str:
    segment = request.segment
    if segment is None:
        return ""
    key = _quote_identifier(segment.key_column)
    return f" WHERE {key} >= %(segment_start)s AND {key} < %(segment_end)s"


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
    return ".".join(f"`{part.replace('`', '``')}`" for part in identifier.split("."))


def _sql_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


__all__ = ["build_mysql_compare_hash_plan"]
