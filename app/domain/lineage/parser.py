from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, cast

import sqlglot
from pydantic import BaseModel, ConfigDict, Field
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError
from sqlglot.lineage import lineage
from sqlglot.optimizer.qualify import qualify

from app.domain.lineage.dialects import register_lineage_dialects
from app.domain.lineage.models import (
    InsertMapping,
    LineageParseError,
    LineageReport,
    LineageWarning,
    TableLineageEdge,
    TargetSummary,
    TransformationKind,
    TransformationSubtype,
)
from app.domain.lineage.plsql import split_plsql_statements

LineageSchema = dict[str, dict[str, dict[str, str]]]

# Bound the per-error detail list persisted in lineage_runs.parse_summary (JSONB).
# parse_error_count stays the full count, so len(parse_errors) < parse_error_count
# signals truncation without an extra field.
PARSE_ERRORS_SUMMARY_LIMIT = 50


class LineageParseRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    sql_text: str = Field(min_length=1)
    dialect: str
    metadata_schema: LineageSchema = Field(alias="schema")
    default_schema: str | None = None


@dataclass(frozen=True)
class _StatementContext:
    index: int
    dialect: str
    schema: LineageSchema
    default_schema: str | None


def analyze_sql_lineage(request: LineageParseRequest) -> LineageReport:
    register_lineage_dialects()
    dialect = _normalize_dialect(request.dialect)
    raw_statements = _parse_or_split_plsql(request.sql_text, dialect)
    report = LineageReport(statement_count=len(raw_statements))
    for index, statement in enumerate(raw_statements):
        context = _StatementContext(
            index=index,
            dialect=dialect,
            schema=request.metadata_schema,
            default_schema=request.default_schema,
        )
        _analyze_statement(statement, context, report)
    report.report = _summary(report)
    report.semantic_lineage = {
        "targets": report.target_summary,
        "edge_count": len(report.graph_edges),
        "column_mapping_count": len(report.insert_mappings),
        "parse_error_count": len(report.parse_errors),
    }
    return report


def schema_from_metadata_cache_rows(rows: Iterable[Mapping[str, Any]]) -> LineageSchema:
    schema: LineageSchema = {}
    for row in rows:
        if row.get("cache_level") != "columns":
            continue
        schema_name = _non_empty_str(row.get("schema_name"))
        table_name = _non_empty_str(row.get("table_name"))
        payload = row.get("payload")
        if not schema_name or not table_name or not isinstance(payload, list):
            continue
        columns: dict[str, str] = {}
        for item in payload:
            if not isinstance(item, Mapping):
                continue
            column_name = _non_empty_str(item.get("name"))
            if not column_name:
                continue
            columns[column_name] = str(item.get("type") or item.get("driver_type") or "unknown")
        if columns:
            schema.setdefault(schema_name, {})[table_name] = columns
    return schema


def _parse_or_split_plsql(sql_text: str, dialect: str) -> list[str]:
    try:
        parsed = sqlglot.parse(sql_text, read=dialect)
    except ParseError:
        parsed = []
    if parsed and not _looks_like_plsql(sql_text):
        return [expression.sql(dialect=dialect) for expression in parsed if expression is not None]
    split = split_plsql_statements(sql_text)
    if split:
        return split
    return [sql_text]


def _analyze_statement(
    statement: str,
    context: _StatementContext,
    report: LineageReport,
) -> None:
    try:
        expression = cast(exp.Expression, sqlglot.parse_one(statement, read=context.dialect))
    except ParseError as exc:
        _append_parse_error(report, context, "parse_error", exc, statement_type=None)
        return
    statement_type = expression.key.upper()
    report.statements.append({"index": context.index, "type": statement_type})
    missing = _missing_tables(expression, context.schema, context.default_schema)
    if missing:
        for table in missing:
            report.parse_errors.append(
                LineageParseError(
                    statement_index=context.index,
                    error_type="unsupported_schema",
                    message=f"metadata cache missing for table {table}",
                    unsupported=True,
                    statement_type=statement_type,
                ).model_dump(mode="json")
            )
        return
    try:
        qualified = qualify(
            expression,
            dialect=context.dialect,
            db=context.default_schema,
            schema=cast(dict[str, object], context.schema),
            expand_stars=True,
            validate_qualify_columns=True,
        )
    except OptimizeError as exc:
        _append_parse_error(report, context, "qualify_error", exc, statement_type=statement_type)
        return
    if isinstance(qualified, exp.Insert):
        _analyze_insert(qualified, context, report)
    else:
        report.warnings.append(
            LineageWarning(
                code="unsupported_statement",
                message=f"{statement_type} is not emitted as lineage edges in W1",
                statement_index=context.index,
            ).model_dump(mode="json")
        )


def _analyze_insert(
    expression: exp.Insert,
    context: _StatementContext,
    report: LineageReport,
) -> None:
    target_table_expr = _target_table(expression)
    select_expr = expression.expression if isinstance(expression.expression, exp.Select) else None
    if target_table_expr is None or select_expr is None:
        report.parse_errors.append(
            LineageParseError(
                statement_index=context.index,
                error_type="unsupported_insert",
                message="INSERT statement is not INSERT ... SELECT",
                unsupported=True,
                statement_type="INSERT",
            ).model_dump(mode="json")
        )
        return
    target_table = _table_name(target_table_expr, context.default_schema)
    report.target_summary.append(
        TargetSummary(table=target_table, operation="insert", statement_index=context.index)
    )
    source_tables = {
        _table_name(table, context.default_schema)
        for table in select_expr.find_all(exp.Table)
        if _table_name(table, context.default_schema) != target_table
    }
    for source_table in sorted(source_tables):
        _append_unique(
            report.graph_edges,
            TableLineageEdge(
                source_table=source_table,
                target_table=target_table,
                statement_index=context.index,
            ).model_dump(mode="json"),
        )
    _append_tables(report, target_table, source_tables, context.index)
    _append_columns(report, select_expr, context.default_schema)
    _append_clause_details(report, select_expr, context.index, context.default_schema)
    target_columns = _target_columns(expression, select_expr)
    for select_item, target_column in zip(select_expr.expressions, target_columns, strict=False):
        _append_output_mappings(
            report,
            qualified_expression=expression,
            select_item=select_item,
            target_table=target_table,
            target_column=target_column,
            context=context,
        )
        _append_indirect_mappings(
            report,
            select_item=select_item,
            select_expr=select_expr,
            target_table=target_table,
            target_column=target_column,
            context=context,
        )


def _append_output_mappings(
    report: LineageReport,
    *,
    qualified_expression: exp.Insert,
    select_item: exp.Expression,
    target_table: str,
    target_column: str,
    context: _StatementContext,
) -> None:
    subtype = _transformation_subtype(select_item)
    try:
        node = lineage(
            select_item.alias_or_name,
            qualified_expression,
            schema=context.schema,
            dialect=context.dialect,
        )
    except Exception as exc:  # sqlglot can raise OptimizeError here too
        _append_parse_error(report, context, "lineage_error", exc, statement_type="INSERT")
        return
    for source_table, source_column in _source_columns_from_node(node, context.default_schema):
        mapping = InsertMapping(
            target_table=target_table,
            target_column=target_column,
            source_table=source_table,
            source_column=source_column,
            statement_index=context.index,
            transformation=TransformationKind.DIRECT,
            transformation_subtype=subtype,
        )
        _append_unique(report.insert_mappings, mapping.model_dump(mode="json"))


def _append_indirect_mappings(
    report: LineageReport,
    *,
    select_item: exp.Expression,
    select_expr: exp.Select,
    target_table: str,
    target_column: str,
    context: _StatementContext,
) -> None:
    output_columns = {_column_key(column) for column in select_item.find_all(exp.Column)}
    for source_column, subtype in _predicate_columns(select_expr):
        if _column_key(source_column) in output_columns:
            continue
        source_table = _source_table_for_column(source_column, select_expr, context.default_schema)
        mapping = InsertMapping(
            target_table=target_table,
            target_column=target_column,
            source_table=source_table,
            source_column=source_column.name,
            statement_index=context.index,
            transformation=TransformationKind.INDIRECT,
            transformation_subtype=subtype,
        )
        _append_unique(report.insert_mappings, mapping.model_dump(mode="json"))


def _source_columns_from_node(node: Any, default_schema: str | None) -> set[tuple[str, str]]:
    sources: set[tuple[str, str]] = set()
    for current in node.walk():
        expression = getattr(current, "expression", None)
        if isinstance(expression, exp.Table):
            column = _last_identifier(str(getattr(current, "name", "")))
            if column:
                sources.add((_table_name(expression, default_schema), column))
    return sources


def _predicate_columns(select_expr: exp.Select) -> list[tuple[exp.Column, TransformationSubtype]]:
    columns: list[tuple[exp.Column, TransformationSubtype]] = []
    where_expr = select_expr.args.get("where")
    if isinstance(where_expr, exp.Expression):
        columns.extend(
            (column, TransformationSubtype.FILTER) for column in where_expr.find_all(exp.Column)
        )
    for join_expr in select_expr.args.get("joins") or []:
        on_expr = join_expr.args.get("on")
        if isinstance(on_expr, exp.Expression):
            columns.extend(
                (column, TransformationSubtype.JOIN) for column in on_expr.find_all(exp.Column)
            )
    return columns


def _append_clause_details(
    report: LineageReport,
    select_expr: exp.Select,
    statement_index: int,
    default_schema: str | None,
) -> None:
    for join_expr in select_expr.args.get("joins") or []:
        on_expr = join_expr.args.get("on")
        report.joins.append(
            {
                "statement_index": statement_index,
                "tables": sorted(
                    {_table_name(table, default_schema) for table in join_expr.find_all(exp.Table)}
                ),
                "columns": sorted({column.name for column in on_expr.find_all(exp.Column)})
                if isinstance(on_expr, exp.Expression)
                else [],
            }
        )
    where_expr = select_expr.args.get("where")
    if isinstance(where_expr, exp.Expression):
        report.filters.append(
            {
                "statement_index": statement_index,
                "columns": sorted({column.name for column in where_expr.find_all(exp.Column)}),
            }
        )
    group_expr = select_expr.args.get("group")
    if isinstance(group_expr, exp.Expression):
        report.group_by.append(
            {
                "statement_index": statement_index,
                "columns": sorted({column.name for column in group_expr.find_all(exp.Column)}),
            }
        )
    if select_expr.find(exp.Union):
        report.unions.append({"statement_index": statement_index})


def _append_tables(
    report: LineageReport,
    target_table: str,
    source_tables: set[str],
    statement_index: int,
) -> None:
    _append_unique(report.tables, {"name": target_table, "role": "target"})
    report.table_roles.append(
        {"table": target_table, "role": "target", "statement_index": statement_index}
    )
    for table in sorted(source_tables):
        _append_unique(report.tables, {"name": table, "role": "source"})
        report.table_roles.append(
            {"table": table, "role": "source", "statement_index": statement_index}
        )


def _append_columns(
    report: LineageReport,
    select_expr: exp.Select,
    default_schema: str | None,
) -> None:
    for table in select_expr.find_all(exp.Table):
        table_name = _table_name(table, default_schema)
        for column in select_expr.find_all(exp.Column):
            if column.table and column.table != table.alias_or_name:
                continue
            _append_unique(report.columns, {"table": table_name, "name": column.name})


def _target_table(expression: exp.Insert) -> exp.Table | None:
    if isinstance(expression.this, exp.Schema):
        table = expression.this.this
        return table if isinstance(table, exp.Table) else None
    return expression.this if isinstance(expression.this, exp.Table) else None


def _target_columns(expression: exp.Insert, select_expr: exp.Select) -> list[str]:
    if isinstance(expression.this, exp.Schema):
        columns = [item.name for item in expression.this.expressions if item.name]
        if columns:
            return columns
    return [item.alias_or_name for item in select_expr.expressions]


def _transformation_subtype(expression: exp.Expression) -> TransformationSubtype:
    if isinstance(expression, exp.Alias):
        expression = expression.this
    if isinstance(expression, exp.Column):
        return TransformationSubtype.DIRECT
    if expression.find(exp.AggFunc):
        return TransformationSubtype.AGGREGATION
    if expression.find(exp.Cast):
        return TransformationSubtype.CAST
    if any(isinstance(item, exp.Func) for item in expression.walk()):
        return TransformationSubtype.TRANSFORMATION
    return TransformationSubtype.EXPRESSION


def _source_table_for_column(
    column: exp.Column,
    select_expr: exp.Select,
    default_schema: str | None,
) -> str:
    table_alias = column.table
    for table in select_expr.find_all(exp.Table):
        if not table_alias or table.alias_or_name == table_alias:
            return _table_name(table, default_schema)
    return table_alias or ""


def _missing_tables(
    expression: exp.Expression,
    schema: LineageSchema,
    default_schema: str | None,
) -> list[str]:
    missing: list[str] = []
    for table in expression.find_all(exp.Table):
        if not _table_in_schema(table, schema, default_schema):
            missing.append(_table_name(table, default_schema))
    return sorted(set(missing))


def _table_in_schema(
    table: exp.Table,
    schema: LineageSchema,
    default_schema: str | None,
) -> bool:
    schema_name = table.db or default_schema or ""
    if not schema_name:
        return any(_case_get(tables, table.name) is not None for tables in schema.values())
    tables = _case_get(schema, schema_name)
    return isinstance(tables, Mapping) and _case_get(tables, table.name) is not None


def _table_name(table: exp.Table, default_schema: str | None) -> str:
    schema_name = table.db or default_schema
    if schema_name:
        return f"{schema_name}.{table.name}"
    return table.name


def _column_key(column: exp.Column) -> tuple[str, str]:
    return (column.table or "", column.name)


def _last_identifier(name: str) -> str:
    return name.replace('"', "").replace("`", "").split(".")[-1]


def _append_unique(items: list[dict[str, Any]], item: dict[str, Any]) -> None:
    if item not in items:
        items.append(item)


def _case_get(mapping: Mapping[str, Any], key: str) -> Any:
    if key in mapping:
        return mapping[key]
    lowered = key.lower()
    for item_key, value in mapping.items():
        if item_key.lower() == lowered:
            return value
    return None


def _append_parse_error(
    report: LineageReport,
    context: _StatementContext,
    error_type: str,
    exc: Exception,
    *,
    statement_type: str | None,
) -> None:
    report.parse_errors.append(
        LineageParseError(
            statement_index=context.index,
            error_type=error_type,
            message=type(exc).__name__,
            unsupported=True,
            statement_type=statement_type,
        ).model_dump(mode="json")
    )


def _summary(report: LineageReport) -> dict[str, Any]:
    return {
        "statement_count": report.statement_count,
        "table_edge_count": len(report.graph_edges),
        "column_mapping_count": len(report.insert_mappings),
        "parse_error_count": len(report.parse_errors),
        "parse_errors": report.parse_errors[:PARSE_ERRORS_SUMMARY_LIMIT],
    }


def _looks_like_plsql(sql_text: str) -> bool:
    upper = sql_text.upper()
    return "CREATE PROCEDURE" in upper or "CREATE OR REPLACE PROCEDURE" in upper


def _normalize_dialect(dialect: str) -> str:
    normalized = dialect.lower()
    if normalized in {"dameng"}:
        return "dm"
    if normalized not in {"mysql", "oracle", "dm"}:
        raise ValueError("lineage dialect must be mysql, oracle, or dm")
    return normalized


def _non_empty_str(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None
