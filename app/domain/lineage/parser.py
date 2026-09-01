from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, cast

import sqlglot
from pydantic import BaseModel, ConfigDict, Field
from sqlglot import exp
from sqlglot.errors import ParseError, SqlglotError
from sqlglot.lineage import Node, lineage
from sqlglot.optimizer.qualify import qualify

from app.domain.lineage.dialects import normalize_lineage_dialect, register_lineage_dialects
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
from app.domain.lineage.segments import (
    extract_dynamic_sql_segments,
    extract_procedure_segments,
)
from app.domain.lineage.supplemental_edges import build_supplemental_edges
from app.domain.lineage.variables import (
    all_plsql_local_names,
    normalize_template_variables,
    script_variables,
)

LineageSchema = dict[str, dict[str, dict[str, str]]]

# Bound the per-error detail list persisted in lineage_runs.parse_summary (JSONB).
# parse_error_count stays the full count, so len(parse_errors) < parse_error_count
# signals truncation without an extra field.
PARSE_ERRORS_SUMMARY_LIMIT = 50

# Bound iterative CTE / derived-table resolution so a pathological alias cycle
# cannot loop forever.
_RESOLUTION_DEPTH_LIMIT = 8


class LineageParseRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    sql_text: str = Field(min_length=1)
    dialect: str
    metadata_schema: LineageSchema = Field(alias="schema")
    default_schema: str | None = None
    # 宽松模式(批量分析用):引用的表缺元数据缓存时不再判 unsupported_schema,
    # 降级只产出表级边(列级跳过,精度诚实降档,warning 标注)——1.x 就是表级精度。
    lenient: bool = False


@dataclass(frozen=True)
class _StatementContext:
    index: int
    dialect: str
    schema: LineageSchema
    default_schema: str | None
    lenient: bool = False


def analyze_sql_lineage(request: LineageParseRequest) -> LineageReport:
    register_lineage_dialects()
    dialect = normalize_lineage_dialect(request.dialect)
    sql_text = request.sql_text
    # L-3:PL/SQL 深度解析。控制流感知切段 + 动态 SQL 抽取,纯静态(无 DB,R1 满足)。
    # 变量在任何 normalize 之前抽(``${name}`` 模板名不能丢)。
    procedure_segments = extract_procedure_segments(sql_text)
    dynamic_sql_segments = extract_dynamic_sql_segments(sql_text)
    variables = script_variables(sql_text)
    local_names = all_plsql_local_names(sql_text)

    raw_statements, segment_titles = _statements_for_analysis(
        sql_text, dialect, procedure_segments, dynamic_sql_segments
    )
    report = LineageReport(statement_count=len(raw_statements))
    for index, statement in enumerate(raw_statements):
        context = _StatementContext(
            index=index,
            dialect=dialect,
            schema=request.metadata_schema,
            default_schema=request.default_schema,
            lenient=request.lenient,
        )
        _analyze_statement(statement, context, report)

    # L-3:业务标题挂到对应 statement;每段单独试解析回填 parse_status;填充预留
    # envelope 字段;局部变量假表过滤(G6);无法解析的动态 SQL 出 dynamic_sql warning。
    for entry in report.statements:
        title = segment_titles.get(entry["index"])
        if title:
            entry["title"] = title
    _mark_segment_parse_status(procedure_segments, dialect)
    _emit_dynamic_sql_warnings(report, dynamic_sql_segments)
    if local_names:
        _drop_local_name_pseudo_tables(report, local_names)
    # L-3 PR2:游标 / UDF / BULK COLLECT / TRIGGER 补链边(confidence=medium)。在局部
    # 变量假表过滤之后追加,与确定性边按 (source, target) 去重,补链不覆盖确定性边。
    report.graph_edges.extend(build_supplemental_edges(procedure_segments, report.graph_edges))
    report.variables = variables
    report.dynamic_sql_segments = dynamic_sql_segments
    report.dynamic_sql_count = len(dynamic_sql_segments)
    report.procedure_segments = procedure_segments

    report.report = _summary(report)
    report.semantic_lineage = {
        "targets": report.target_summary,
        "edge_count": len(report.graph_edges),
        "column_mapping_count": len(report.insert_mappings),
        "parse_error_count": len(report.parse_errors),
    }
    return report


def _statements_for_analysis(
    sql_text: str,
    dialect: str,
    procedure_segments: list[dict[str, Any]],
    dynamic_sql_segments: list[dict[str, str]],
) -> tuple[list[str], dict[int, str]]:
    """决定送入 ``_analyze_statement`` 的语句列表,并映射 index → 业务标题。

    有 PL/SQL 过程段时,用控制流感知切段的结果(替代裸分号 split)+ 可解析的动态
    SQL 段;否则退回 :func:`_parse_or_split_plsql`(纯 DML 脚本行为不变,回归安全)。
    """
    if not procedure_segments:
        return _parse_or_split_plsql(sql_text, dialect), {}
    statements: list[str] = []
    titles: dict[int, str] = {}
    for segment in procedure_segments:
        title = str(segment.get("preceding_comment") or "")
        if title:
            titles[len(statements)] = title
        statements.append(str(segment["sql"]))
    for dynamic in dynamic_sql_segments:
        # unresolved 占位段是 `-- ...` 注释,无法解析,不送分析(只作 warning)。
        if dynamic.get("confidence") == "unresolved":
            continue
        statements.append(str(dynamic["sql"]))
    return statements, titles


def _mark_segment_parse_status(procedure_segments: list[dict[str, Any]], dialect: str) -> None:
    """每段单独试 sqlglot 解析,回填 parse_status='parsed' / 'unsupported'。"""
    for segment in procedure_segments:
        try:
            sqlglot.parse_one(normalize_template_variables(str(segment["sql"])), read=dialect)
        except (SqlglotError, RecursionError):
            segment["parse_status"] = "unsupported"
        else:
            segment["parse_status"] = "parsed"


def _emit_dynamic_sql_warnings(
    report: LineageReport, dynamic_sql_segments: list[dict[str, str]]
) -> None:
    """无法静态解析的动态 SQL(unresolved 变量)诚实告警,而非静默丢失。"""
    for dynamic in dynamic_sql_segments:
        if dynamic.get("confidence") != "unresolved":
            continue
        report.warnings.append(
            LineageWarning(
                code="dynamic_sql",
                message=(f"dynamic SQL could not be resolved statically: {dynamic['sql']}"),
            ).model_dump(mode="json")
        )


def _drop_local_name_pseudo_tables(report: LineageReport, local_names: set[str]) -> None:
    """过滤 PL/SQL 局部变量被误当表的污染(G6)。

    ``SELECT col INTO v_row FROM t`` 被 sqlglot 重写成 ``CREATE TABLE v_row AS
    SELECT ...``,``v_row`` 会混进 tables / 边。局部变量名恒为无 schema 限定的裸名,
    真实表(``ods.orders``)有点号,不会误伤。
    """

    def _is_pseudo(name: str) -> bool:
        bare = name.strip().strip('"`[]').lower()
        return "." not in bare and bare in local_names

    report.tables = [t for t in report.tables if not _is_pseudo(str(t.get("name", "")))]
    report.table_roles = [r for r in report.table_roles if not _is_pseudo(str(r.get("table", "")))]
    report.columns = [c for c in report.columns if not _is_pseudo(str(c.get("table", "")))]
    report.graph_edges = [
        e
        for e in report.graph_edges
        if not _is_pseudo(str(e.get("source_table", "")))
        and not _is_pseudo(str(e.get("target_table", "")))
    ]
    report.target_summary = [s for s in report.target_summary if not _is_pseudo(s.table)]
    report.insert_mappings = [
        m
        for m in report.insert_mappings
        if not _is_pseudo(str(m.get("source_table", "")))
        and not _is_pseudo(str(m.get("target_table", "")))
    ]


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
    normalized_sql = normalize_template_variables(sql_text)
    try:
        parsed = sqlglot.parse(normalized_sql, read=dialect)
    except (SqlglotError, RecursionError):
        parsed = []
    if parsed and not _looks_like_plsql(normalized_sql):
        return [expression.sql(dialect=dialect) for expression in parsed if expression is not None]
    split = split_plsql_statements(normalized_sql)
    if split:
        return split
    return [normalized_sql]


def _analyze_statement(
    statement: str,
    context: _StatementContext,
    report: LineageReport,
) -> None:
    statement = normalize_template_variables(statement)
    try:
        expression = cast(exp.Expression, sqlglot.parse_one(statement, read=context.dialect))
    except (SqlglotError, RecursionError) as exc:
        # Oracle 别名前缀 INSERT(`insert into t a (a.col…)`)sqlglot 不认,
        # 仅在直解失败时尝试剥别名重写再解一次(限定爆炸半径)
        rewritten = _rewrite_insert_alias(statement) if isinstance(exc, ParseError) else None
        if rewritten is None:
            _append_parse_error(
                report, context, "parse_error", exc, statement_type=None, statement=statement
            )
            return
        try:
            expression = cast(exp.Expression, sqlglot.parse_one(rewritten, read=context.dialect))
        except (SqlglotError, RecursionError) as rewritten_exc:
            _append_parse_error(
                report,
                context,
                "parse_error",
                rewritten_exc,
                statement_type=None,
                statement=statement,
            )
            return
    statement_type = expression.key.upper()
    report.statements.append({"index": context.index, "type": statement_type})
    # DELETE/TRUNCATE 无列血缘,不需要 metadata/qualify:在 _missing_tables 之前
    # 提前短路,strict + lenient 两路共用同一逻辑,避免语义重复(L-4 全量重刷判据)。
    if isinstance(expression, exp.Delete):
        _analyze_delete(expression, context, report)
        return
    if _is_truncate(expression):
        _analyze_truncate(expression, context, report)
        return
    missing = _missing_tables(expression, context.schema, context.default_schema)
    if missing:
        if context.lenient:
            _analyze_statement_table_level(expression, statement_type, context, report)
            return
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
    except (SqlglotError, RecursionError) as exc:
        if context.lenient:
            _analyze_statement_table_level(expression, statement_type, context, report)
            return
        _append_parse_error(report, context, "qualify_error", exc, statement_type=statement_type)
        return
    if isinstance(qualified, exp.Insert):
        _analyze_insert(qualified, context, report)
    elif isinstance(qualified, exp.Create) and _create_query(qualified) is not None:
        _analyze_ctas(qualified, context, report)
    elif isinstance(qualified, exp.Merge):
        _analyze_merge(qualified, context, report)
    elif isinstance(qualified, exp.Update):
        _analyze_update(qualified, context, report)
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
    query = _query_expression(expression.expression)
    if target_table_expr is None or query is None:
        _append_unsupported(
            report,
            context,
            "unsupported_insert",
            "INSERT statement is not INSERT ... SELECT",
            "INSERT",
        )
        return
    _analyze_query_write(
        report,
        qualified_expression=expression,
        target_table_expr=target_table_expr,
        query=query,
        target_columns=_target_columns(expression, query),
        operation="insert",
        statement_type="INSERT",
        context=context,
    )


def _analyze_ctas(
    expression: exp.Create,
    context: _StatementContext,
    report: LineageReport,
) -> None:
    query = _create_query(expression)
    target = expression.this
    explicit_columns: list[str | None] = []
    if isinstance(target, exp.Schema):
        explicit_columns = [item.name or None for item in target.expressions]
        target = target.this
    if not isinstance(target, exp.Table) or query is None:
        _append_unsupported(
            report,
            context,
            "unsupported_create",
            "CREATE TABLE ... AS without resolvable target table or SELECT",
            "CREATE",
        )
        return
    target_columns = explicit_columns if any(explicit_columns) else _derived_target_columns(query)
    _analyze_query_write(
        report,
        qualified_expression=expression,
        target_table_expr=target,
        query=query,
        target_columns=target_columns,
        operation="create",
        statement_type="CREATE",
        context=context,
    )


def _analyze_query_write(
    report: LineageReport,
    *,
    qualified_expression: exp.Expression,
    target_table_expr: exp.Table,
    query: exp.Select | exp.SetOperation,
    target_columns: list[str | None],
    operation: str,
    statement_type: str,
    context: _StatementContext,
) -> None:
    target_table = _table_name(target_table_expr, context.default_schema)
    report.target_summary.append(
        TargetSummary(table=target_table, operation=operation, statement_index=context.index)
    )
    cte_map = _cte_map(qualified_expression)
    source_tables = {
        _table_name(table, context.default_schema)
        for table in query.find_all(exp.Table)
        if not _is_cte_reference(table, cte_map)
        and _table_name(table, context.default_schema) != target_table
    }
    _append_edges(report, target_table, source_tables, context.index)
    _append_tables(report, target_table, source_tables, context.index)
    branches = _query_branches(query)
    first_branch = branches[0] if branches else None
    output_lineage = _output_lineage_nodes(
        qualified_expression,
        output_names=(item.alias_or_name for item in first_branch.expressions)
        if first_branch is not None
        else (),
        context=context,
    )
    for branch in branches:
        _append_columns(report, branch, context.default_schema, cte_map)
        _append_clause_details(report, branch, context.index, context.default_schema, cte_map)
    if isinstance(query, exp.SetOperation):
        report.unions.append({"statement_index": context.index})
        _append_set_operation_mappings(
            report,
            qualified_expression=qualified_expression,
            branches=branches,
            target_table=target_table,
            target_columns=target_columns,
            context=context,
            statement_type=statement_type,
            cte_map=cte_map,
            output_lineage=output_lineage,
        )
        return
    for select_item, target_column in zip(query.expressions, target_columns, strict=False):
        if not target_column:
            continue
        _append_output_mappings(
            report,
            qualified_expression=qualified_expression,
            select_item=select_item,
            target_table=target_table,
            target_column=target_column,
            context=context,
            statement_type=statement_type,
            output_lineage=output_lineage,
        )
        _append_indirect_mappings(
            report,
            select_item=select_item,
            select_expr=query,
            target_table=target_table,
            target_column=target_column,
            context=context,
            cte_map=cte_map,
        )


def _analyze_merge(
    expression: exp.Merge,
    context: _StatementContext,
    report: LineageReport,
) -> None:
    target_expr = expression.this
    using = expression.args.get("using")
    if not isinstance(target_expr, exp.Table) or not isinstance(using, (exp.Table, exp.Subquery)):
        _append_unsupported(
            report,
            context,
            "unsupported_merge",
            "MERGE without resolvable target or USING source",
            "MERGE",
        )
        return
    target_table = _table_name(target_expr, context.default_schema)
    report.target_summary.append(
        TargetSummary(table=target_table, operation="merge", statement_index=context.index)
    )
    cte_map = _cte_map(expression)
    source_tables = {
        _table_name(table, context.default_schema)
        for table in using.find_all(exp.Table)
        if not _is_cte_reference(table, cte_map)
        and _table_name(table, context.default_schema) != target_table
    }
    _append_edges(report, target_table, source_tables, context.index)
    _append_tables(report, target_table, source_tables, context.index)
    whens = expression.args.get("whens")
    when_list = list(whens.expressions) if isinstance(whens, exp.Whens) else []
    target_columns: set[str] = set()
    for when in when_list:
        then = when.args.get("then")
        if isinstance(then, exp.Update):
            for assignment in then.expressions:
                if isinstance(assignment, exp.EQ) and isinstance(assignment.this, exp.Column):
                    target_columns.add(assignment.this.name)
                    _append_value_mappings(
                        report,
                        value=assignment.expression,
                        scope=expression,
                        target_table=target_table,
                        target_column=assignment.this.name,
                        context=context,
                        cte_map=cte_map,
                    )
                else:
                    _append_unsupported(
                        report,
                        context,
                        "unsupported_merge",
                        "MERGE UPDATE SET assignment is not column = expression",
                        "MERGE",
                    )
        elif isinstance(then, exp.Insert):
            columns = then.this
            values = then.expression
            if isinstance(columns, exp.Tuple) and isinstance(values, exp.Tuple):
                for column_expr, value in zip(
                    columns.expressions, values.expressions, strict=False
                ):
                    if not isinstance(column_expr, exp.Column):
                        continue
                    target_columns.add(column_expr.name)
                    _append_value_mappings(
                        report,
                        value=value,
                        scope=expression,
                        target_table=target_table,
                        target_column=column_expr.name,
                        context=context,
                        cte_map=cte_map,
                    )
            else:
                _append_unsupported(
                    report,
                    context,
                    "unsupported_merge",
                    "MERGE INSERT without explicit column list",
                    "MERGE",
                )
    predicates: list[tuple[exp.Column, TransformationSubtype]] = []
    on_expr = expression.args.get("on")
    if isinstance(on_expr, exp.Expression):
        predicates.extend(
            (column, TransformationSubtype.JOIN) for column in on_expr.find_all(exp.Column)
        )
    for when in when_list:
        condition = when.args.get("condition")
        if isinstance(condition, exp.Expression):
            predicates.extend(
                (column, TransformationSubtype.FILTER) for column in condition.find_all(exp.Column)
            )
    for target_column in sorted(target_columns):
        _append_predicate_mappings(
            report,
            predicates=predicates,
            scope=expression,
            target_table=target_table,
            target_column=target_column,
            context=context,
            cte_map=cte_map,
        )


def _analyze_update(
    expression: exp.Update,
    context: _StatementContext,
    report: LineageReport,
) -> None:
    target_expr = expression.this
    if not isinstance(target_expr, exp.Table):
        _append_unsupported(
            report,
            context,
            "unsupported_update",
            "UPDATE without resolvable target table",
            "UPDATE",
        )
        return
    target_table = _table_name(target_expr, context.default_schema)
    report.target_summary.append(
        TargetSummary(table=target_table, operation="update", statement_index=context.index)
    )
    cte_map = _cte_map(expression)
    source_tables = {
        _table_name(table, context.default_schema)
        for table in expression.find_all(exp.Table)
        if not _is_cte_reference(table, cte_map)
        and _table_name(table, context.default_schema) != target_table
    }
    _append_edges(report, target_table, source_tables, context.index)
    _append_tables(report, target_table, source_tables, context.index)
    predicates: list[tuple[exp.Column, TransformationSubtype]] = []
    where_expr = expression.args.get("where")
    if isinstance(where_expr, exp.Expression):
        predicates.extend(
            (column, TransformationSubtype.FILTER) for column in where_expr.find_all(exp.Column)
        )
    # MySQL multi-table UPDATE nests JOINs inside the target table expression.
    for join_expr in target_expr.args.get("joins") or []:
        on_expr = join_expr.args.get("on")
        if isinstance(on_expr, exp.Expression):
            predicates.extend(
                (column, TransformationSubtype.JOIN) for column in on_expr.find_all(exp.Column)
            )
    for assignment in expression.expressions:
        if not (isinstance(assignment, exp.EQ) and isinstance(assignment.this, exp.Column)):
            _append_unsupported(
                report,
                context,
                "unsupported_update",
                "UPDATE SET assignment is not column = expression",
                "UPDATE",
            )
            continue
        lhs = assignment.this
        set_target_table = target_table
        if lhs.table:
            resolved_target = _resolve_column_source(
                lhs, expression, cte_map, context.default_schema
            )
            if resolved_target is not None:
                set_target_table = resolved_target[0]
        _append_value_mappings(
            report,
            value=assignment.expression,
            scope=expression,
            target_table=set_target_table,
            target_column=lhs.name,
            context=context,
            cte_map=cte_map,
        )
        _append_predicate_mappings(
            report,
            predicates=predicates,
            scope=expression,
            target_table=set_target_table,
            target_column=lhs.name,
            context=context,
            cte_map=cte_map,
        )


def _analyze_delete(
    expression: exp.Delete,
    context: _StatementContext,
    report: LineageReport,
) -> None:
    """DELETE:只落 target_summary,不产血缘边(无 source)。

    has_where=False 的 DELETE = 全表清空,供 L-4 判定全量重刷(DELETE+INSERT)。
    """
    target_expr = expression.this
    if not isinstance(target_expr, exp.Table):
        _append_unsupported_statement_warning(report, context, "DELETE")
        return
    has_where = expression.args.get("where") is not None
    report.target_summary.append(
        TargetSummary(
            table=_table_name(target_expr, context.default_schema),
            operation="delete",
            has_where=has_where,
            statement_index=context.index,
        )
    )


def _analyze_truncate(
    expression: exp.Expression,
    context: _StatementContext,
    report: LineageReport,
) -> None:
    """TRUNCATE:每个目标表一条 target_summary(operation=truncate),无血缘边。

    sqlglot 30.x 将 `TRUNCATE TABLE foo` 解析为 exp.TruncateTable,表在
    .expressions 列表(可多表);兜底 exp.Command 形态用 find_all(Table)。
    取不到表则退回 warning,不崩。
    """
    if isinstance(expression, exp.TruncateTable):
        tables = [t for t in expression.expressions if isinstance(t, exp.Table)]
    else:
        tables = list(expression.find_all(exp.Table))
    if not tables:
        _append_unsupported_statement_warning(report, context, "TRUNCATE")
        return
    for table in tables:
        report.target_summary.append(
            TargetSummary(
                table=_table_name(table, context.default_schema),
                operation="truncate",
                has_where=None,
                statement_index=context.index,
            )
        )


def _is_truncate(expression: exp.Expression) -> bool:
    if isinstance(expression, exp.TruncateTable):
        return True
    # 兜底:某些方言/形态下 TRUNCATE 落 exp.Command(表在原始命令串里)。
    return isinstance(expression, exp.Command) and str(expression.this or "").upper() == "TRUNCATE"


def _append_unsupported_statement_warning(
    report: LineageReport,
    context: _StatementContext,
    statement_type: str,
) -> None:
    report.warnings.append(
        LineageWarning(
            code="unsupported_statement",
            message=f"{statement_type} target table is not resolvable",
            statement_index=context.index,
        ).model_dump(mode="json")
    )


def _append_output_mappings(
    report: LineageReport,
    *,
    qualified_expression: exp.Expression,
    select_item: exp.Expression,
    target_table: str,
    target_column: str,
    context: _StatementContext,
    statement_type: str,
    output_lineage: Mapping[str, Node] | None,
) -> None:
    subtype = _transformation_subtype(select_item)
    node = output_lineage.get(select_item.alias_or_name) if output_lineage is not None else None
    if node is None:
        try:
            node = lineage(
                select_item.alias_or_name,
                qualified_expression,
                schema=context.schema,
                dialect=context.dialect,
            )
        except (SqlglotError, RecursionError) as exc:
            _append_parse_error(
                report, context, "lineage_error", exc, statement_type=statement_type
            )
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


def _append_set_operation_mappings(
    report: LineageReport,
    *,
    qualified_expression: exp.Expression,
    branches: list[exp.Select],
    target_table: str,
    target_columns: list[str | None],
    context: _StatementContext,
    statement_type: str,
    cte_map: Mapping[str, exp.Expression],
    output_lineage: Mapping[str, Node] | None,
) -> None:
    if not branches:
        return
    for item_index, target_column in enumerate(target_columns):
        if not target_column or item_index >= len(branches[0].expressions):
            continue
        first_item = branches[0].expressions[item_index]
        node = output_lineage.get(first_item.alias_or_name) if output_lineage is not None else None
        if node is None:
            try:
                node = lineage(
                    first_item.alias_or_name,
                    qualified_expression,
                    schema=context.schema,
                    dialect=context.dialect,
                )
            except (SqlglotError, RecursionError) as exc:
                _append_parse_error(
                    report, context, "lineage_error", exc, statement_type=statement_type
                )
                return
        branch_items = [
            branch.expressions[item_index]
            for branch in branches
            if item_index < len(branch.expressions)
        ]
        downstream = list(node.downstream)
        if len(downstream) == len(branch_items):
            per_branch = list(zip(branch_items, downstream, strict=False))
        else:
            # Cannot align lineage sub-nodes with branches; fall back to the
            # merged node and a shared subtype when all branches agree.
            subtypes = {_transformation_subtype(item) for item in branch_items}
            merged_item = branch_items[0] if len(subtypes) == 1 else None
            per_branch = [(merged_item, node)]
        for item, branch_node in per_branch:
            subtype = (
                _transformation_subtype(item)
                if item is not None
                else TransformationSubtype.EXPRESSION
            )
            for source_table, source_column in _source_columns_from_node(
                branch_node, context.default_schema
            ):
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
        for branch in branches:
            if item_index < len(branch.expressions):
                _append_indirect_mappings(
                    report,
                    select_item=branch.expressions[item_index],
                    select_expr=branch,
                    target_table=target_table,
                    target_column=target_column,
                    context=context,
                    cte_map=cte_map,
                )


def _output_lineage_nodes(
    qualified_expression: exp.Expression,
    *,
    output_names: Iterable[str],
    context: _StatementContext,
) -> dict[str, Node] | None:
    names = list(output_names)
    if not names or len(names) != len(set(names)):
        return None
    try:
        # The report consumes only lineage leaf tables/columns, not each node's
        # display-only SELECT source, so trimming would add per-output deep copies.
        nodes = lineage(
            None,
            qualified_expression,
            schema=context.schema,
            dialect=context.dialect,
            trim_selects=False,
        )
    except (SqlglotError, RecursionError, AttributeError, TypeError, ValueError):
        # Preserve the existing per-column error degradation below if sqlglot
        # cannot build all output lineages as one batch.
        return None
    if not isinstance(nodes, dict) or any(name not in nodes for name in names):
        return None
    return nodes


def _append_indirect_mappings(
    report: LineageReport,
    *,
    select_item: exp.Expression,
    select_expr: exp.Select,
    target_table: str,
    target_column: str,
    context: _StatementContext,
    cte_map: Mapping[str, exp.Expression],
) -> None:
    output_columns = {_column_key(column) for column in select_item.find_all(exp.Column)}
    for source_column, subtype in _predicate_columns(select_expr):
        if _column_key(source_column) in output_columns:
            continue
        resolved = _resolve_column_source(
            source_column, select_expr, cte_map, context.default_schema
        )
        if resolved is None:
            continue
        source_table, source_name, _ = resolved
        mapping = InsertMapping(
            target_table=target_table,
            target_column=target_column,
            source_table=source_table,
            source_column=source_name,
            statement_index=context.index,
            transformation=TransformationKind.INDIRECT,
            transformation_subtype=subtype,
        )
        _append_unique(report.insert_mappings, mapping.model_dump(mode="json"))


def _append_value_mappings(
    report: LineageReport,
    *,
    value: exp.Expression,
    scope: exp.Expression,
    target_table: str,
    target_column: str,
    context: _StatementContext,
    cte_map: Mapping[str, exp.Expression],
) -> None:
    value_subtype = _transformation_subtype(value)
    predicate_pairs: list[tuple[exp.Column, TransformationSubtype]] = []
    excluded: set[int] = set()
    for subquery in value.find_all(exp.Subquery):
        inner = subquery.this
        if not isinstance(inner, exp.Select):
            continue
        pairs = _predicate_columns(inner)
        predicate_pairs.extend(pairs)
        excluded.update(id(column) for column, _ in pairs)
        group_expr = inner.args.get("group")
        if isinstance(group_expr, exp.Expression):
            excluded.update(id(column) for column in group_expr.find_all(exp.Column))
    for column in value.find_all(exp.Column):
        if id(column) in excluded:
            continue
        resolved = _resolve_column_source(column, scope, cte_map, context.default_schema)
        if resolved is None:
            continue
        source_table, source_name, resolved_subtype = resolved
        if source_table == target_table:
            continue
        subtype = (
            value_subtype
            if value_subtype is not TransformationSubtype.DIRECT
            else (resolved_subtype or value_subtype)
        )
        mapping = InsertMapping(
            target_table=target_table,
            target_column=target_column,
            source_table=source_table,
            source_column=source_name,
            statement_index=context.index,
            transformation=TransformationKind.DIRECT,
            transformation_subtype=subtype,
        )
        _append_unique(report.insert_mappings, mapping.model_dump(mode="json"))
    _append_predicate_mappings(
        report,
        predicates=predicate_pairs,
        scope=scope,
        target_table=target_table,
        target_column=target_column,
        context=context,
        cte_map=cte_map,
    )


def _append_predicate_mappings(
    report: LineageReport,
    *,
    predicates: list[tuple[exp.Column, TransformationSubtype]],
    scope: exp.Expression,
    target_table: str,
    target_column: str,
    context: _StatementContext,
    cte_map: Mapping[str, exp.Expression],
) -> None:
    for column, subtype in predicates:
        resolved = _resolve_column_source(column, scope, cte_map, context.default_schema)
        if resolved is None:
            continue
        source_table, source_name, _ = resolved
        if source_table == target_table:
            continue
        mapping = InsertMapping(
            target_table=target_table,
            target_column=target_column,
            source_table=source_table,
            source_column=source_name,
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
    cte_map: Mapping[str, exp.Expression],
) -> None:
    for join_expr in select_expr.args.get("joins") or []:
        on_expr = join_expr.args.get("on")
        report.joins.append(
            {
                "statement_index": statement_index,
                "tables": sorted(
                    {
                        _table_name(table, default_schema)
                        for table in join_expr.find_all(exp.Table)
                        if not _is_cte_reference(table, cte_map)
                    }
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


def _append_edges(
    report: LineageReport,
    target_table: str,
    source_tables: set[str],
    statement_index: int,
) -> None:
    for source_table in sorted(source_tables):
        _append_unique(
            report.graph_edges,
            TableLineageEdge(
                source_table=source_table,
                target_table=target_table,
                statement_index=statement_index,
            ).model_dump(mode="json"),
        )


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
    cte_map: Mapping[str, exp.Expression],
) -> None:
    for table in select_expr.find_all(exp.Table):
        if _is_cte_reference(table, cte_map):
            continue
        table_name = _table_name(table, default_schema)
        for column in select_expr.find_all(exp.Column):
            if column.table and column.table != table.alias_or_name:
                continue
            _append_unique(report.columns, {"table": table_name, "name": column.name})


def _analyze_statement_table_level(
    expression: exp.Expression,
    statement_type: str,
    context: _StatementContext,
    report: LineageReport,
) -> None:
    """宽松模式降级路径:不 qualify,直接从 AST 提取表级边(列级跳过)。

    触发条件 = 引用表缺元数据缓存 / qualify 失败。精度 = 1.x 表级水平;
    每条语句追加 lenient_table_level warning,报告端可据此标注降档。
    """
    target_expr: exp.Table | None = None
    operation = statement_type.lower()
    if isinstance(expression, exp.Insert):
        target_expr = _target_table(expression)
        operation = "insert"
    elif isinstance(expression, exp.Create):
        raw_target = expression.this
        if isinstance(raw_target, exp.Schema):
            raw_target = raw_target.this
        if isinstance(raw_target, exp.Table) and _create_query(expression) is not None:
            target_expr = raw_target
            operation = "create"
    elif isinstance(expression, exp.Merge):
        target_expr = expression.this if isinstance(expression.this, exp.Table) else None
        operation = "merge"
    elif isinstance(expression, exp.Update):
        target_expr = expression.this if isinstance(expression.this, exp.Table) else None
        operation = "update"
    if target_expr is None:
        report.warnings.append(
            LineageWarning(
                code="unsupported_statement",
                message=f"{statement_type} is not emitted as lineage edges in W1",
                statement_index=context.index,
            ).model_dump(mode="json")
        )
        return
    target_table = _table_name(target_expr, context.default_schema)
    cte_map = _cte_map(expression)
    source_tables = {
        _table_name(table, context.default_schema)
        for table in expression.find_all(exp.Table)
        if not _is_cte_reference(table, cte_map)
        and id(table) != id(target_expr)
        and _table_name(table, context.default_schema) != target_table
    }
    report.target_summary.append(
        TargetSummary(table=target_table, operation=operation, statement_index=context.index)
    )
    _append_edges(report, target_table, source_tables, context.index)
    _append_tables(report, target_table, source_tables, context.index)
    report.warnings.append(
        LineageWarning(
            code="lenient_table_level",
            message=(
                "metadata cache unavailable; emitted table-level edges only "
                f"for {target_table} (column lineage skipped)"
            ),
            statement_index=context.index,
        ).model_dump(mode="json")
    )


_INSERT_ALIAS_RE = re.compile(
    r"^(\s*insert\s+into\s+[\w.$\"]+)\s+([a-zA-Z_]\w*)\s*\(",
    re.IGNORECASE,
)


def _rewrite_insert_alias(statement: str) -> str | None:
    """剥 Oracle 别名前缀 INSERT:`insert into t a (a.c1, a.c2…)` → 标准形。

    仅在常规解析失败后调用;别名前缀替换严格限定在目标列清单括号段内,
    避免误伤 SELECT 侧同名别名。无匹配返回 None。
    """
    match = _INSERT_ALIAS_RE.match(statement)
    if match is None:
        return None
    alias = match.group(2)
    open_paren = match.end() - 1
    depth = 0
    close_paren = -1
    for position in range(open_paren, len(statement)):
        char = statement[position]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                close_paren = position
                break
    if close_paren < 0:
        return None
    column_list = statement[open_paren : close_paren + 1]
    stripped = re.sub(rf"(?<![\w.]){re.escape(alias)}\.", "", column_list)
    return match.group(1) + " " + stripped + statement[close_paren + 1 :]


def _target_table(expression: exp.Insert) -> exp.Table | None:
    if isinstance(expression.this, exp.Schema):
        table = expression.this.this
        return table if isinstance(table, exp.Table) else None
    return expression.this if isinstance(expression.this, exp.Table) else None


def _query_expression(
    expression: exp.Expression | None,
) -> exp.Select | exp.SetOperation | None:
    if isinstance(expression, exp.Subquery):
        expression = expression.this
    if isinstance(expression, (exp.Select, exp.SetOperation)):
        return expression
    return None


def _create_query(expression: exp.Create) -> exp.Select | exp.SetOperation | None:
    if str(expression.args.get("kind") or "").upper() != "TABLE":
        return None
    return _query_expression(expression.expression)


def _query_branches(query: exp.Expression | None) -> list[exp.Select]:
    if isinstance(query, exp.SetOperation):
        return _query_branches(query.this) + _query_branches(query.expression)
    if isinstance(query, exp.Subquery):
        return _query_branches(query.this)
    return [query] if isinstance(query, exp.Select) else []


def _target_columns(
    expression: exp.Insert,
    query: exp.Select | exp.SetOperation,
) -> list[str | None]:
    if isinstance(expression.this, exp.Schema):
        columns = [item.name for item in expression.this.expressions if item.name]
        if columns:
            return list(columns)
    return _derived_target_columns(query)


def _derived_target_columns(query: exp.Select | exp.SetOperation) -> list[str | None]:
    branches = _query_branches(query)
    if not branches:
        return []
    columns: list[str | None] = []
    for item in branches[0].expressions:
        name = item.alias_or_name
        # qualify() auto-aliases unaliased expressions as _col_N; those names do
        # not exist on the target table, so the mapping target is underivable.
        columns.append(name if name and not name.startswith("_col_") else None)
    return columns


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


def _cte_map(expression: exp.Expression) -> dict[str, exp.Expression]:
    return {
        cte.alias_or_name.lower(): cte.this
        for cte in expression.find_all(exp.CTE)
        if cte.alias_or_name
    }


def _is_cte_reference(table: exp.Table, cte_map: Mapping[str, Any]) -> bool:
    return not table.db and table.name.lower() in cte_map


def _resolve_column_source(
    column: exp.Column,
    scope: exp.Expression,
    cte_map: Mapping[str, exp.Expression],
    default_schema: str | None,
) -> tuple[str, str, TransformationSubtype | None] | None:
    """Resolve a column to its physical (table, column), traversing CTEs and
    derived tables (e.g. MERGE USING subqueries). Returns the strongest
    non-DIRECT transformation subtype seen on traversed projections, if any."""
    name = column.name
    alias = column.table
    subtype: TransformationSubtype | None = None
    current = scope
    for _ in range(_RESOLUTION_DEPTH_LIMIT):
        relation = _relation_for_alias(current, alias)
        if relation is None:
            return None
        if isinstance(relation, exp.Table) and not _is_cte_reference(relation, cte_map):
            return (_table_name(relation, default_schema), name, subtype)
        inner_query = (
            relation.this if isinstance(relation, exp.Subquery) else cte_map[relation.name.lower()]
        )
        branches = _query_branches(inner_query)
        if not branches:
            return None
        item = _projection_named(branches[0], name)
        if item is None:
            return None
        item_subtype = _transformation_subtype(item)
        if subtype is None and item_subtype is not TransformationSubtype.DIRECT:
            subtype = item_subtype
        inner_columns = list(item.find_all(exp.Column))
        if not inner_columns:
            return None
        column = inner_columns[0]
        name = column.name
        alias = column.table
        current = branches[0]
    return None


def _relation_for_alias(
    scope: exp.Expression,
    alias: str,
) -> exp.Table | exp.Subquery | None:
    relations: list[exp.Table | exp.Subquery] = []
    for candidate in scope.find_all(exp.Table, exp.Subquery):
        if isinstance(candidate, exp.Table) or (
            isinstance(candidate, exp.Subquery) and candidate.alias_or_name
        ):
            relations.append(candidate)
    if not alias:
        for node in relations:
            if isinstance(node, exp.Table):
                return node
        return None
    for node in relations:
        if node.alias_or_name == alias:
            return node
    lowered = alias.lower()
    for node in relations:
        if node.alias_or_name.lower() == lowered:
            return node
    return None


def _projection_named(select_expr: exp.Select, name: str) -> exp.Expression | None:
    lowered = name.lower()
    for item in select_expr.expressions:
        if (
            isinstance(item, exp.Expression)
            and item.alias_or_name
            and item.alias_or_name.lower() == lowered
        ):
            return item
    return None


def _missing_tables(
    expression: exp.Expression,
    schema: LineageSchema,
    default_schema: str | None,
) -> list[str]:
    cte_map = _cte_map(expression)
    exempt: set[int] = set()
    if isinstance(expression, exp.Create):
        # A CTAS target is a new table that legitimately has no metadata cache.
        target = expression.this
        if isinstance(target, exp.Schema):
            target = target.this
        if isinstance(target, exp.Table):
            exempt.add(id(target))
    missing: list[str] = []
    for table in expression.find_all(exp.Table):
        if id(table) in exempt or _is_cte_reference(table, cte_map):
            continue
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


# 一段文本要被当作 SQL 看待,至少得出现一个 DML/DDL 关键字。都没有的多半是
# 说明文档 / 任务导出件 / 分隔线,报 sqlglot 的 token 级错误对用户毫无意义。
_SQL_KEYWORD_RE = re.compile(
    r"\b(select|insert|update|delete|merge|create|alter|drop|truncate|with|call|grant)\b",
    re.IGNORECASE,
)
# 纯符号分隔线(=== / --- / *** …):sqlglot 会把 `====` 读成 `==` 运算符然后报
# "Required keyword: 'expression' missing for EQ",对用户是纯噪音。
_SEPARATOR_LINE_RE = re.compile(r"^\s*([=\-*_#~+])\1{5,}\s*$", re.MULTILINE)


def looks_like_sql(statement: str) -> bool:
    """去掉注释后是否还剩 SQL 关键字。用于把"根本不是 SQL"与"SQL 写错了"分开。"""
    body = re.sub(r"--[^\n]*", " ", statement)
    body = re.sub(r"/\*.*?\*/", " ", body, flags=re.DOTALL)
    return bool(_SQL_KEYWORD_RE.search(body))


def _append_parse_error(
    report: LineageReport,
    context: _StatementContext,
    error_type: str,
    exc: Exception,
    *,
    statement_type: str | None,
    statement: str | None = None,
) -> None:
    """把解析失败落成诊断。

    ★ 不是所有失败都值得把 sqlglot 的 token 级细节甩给用户:当这段文本压根不含
    SQL 关键字(整段是注释头 + `====` 分隔线的任务导出件是最常见的一种),报
    "Required keyword: 'expression' missing for EQ (line 7, col 4, token='==')"
    只会让人以为是解析器坏了。这种归为 ``not_sql`` 并说人话,原始细节仍保留在
    ``detail`` 里供排查。
    """
    raw = _parse_error_message(exc) if isinstance(exc, ParseError) else type(exc).__name__
    message = raw
    detail: str | None = None
    if statement is not None and not looks_like_sql(statement):
        error_type = "not_sql"
        hint = (
            "(疑似任务导出文档:整段由注释与分隔线组成)"
            if _SEPARATOR_LINE_RE.search(statement)
            else "(整段未出现 SQL 关键字)"
        )
        message = f"这段内容不是可解析的 SQL,已跳过{hint}"
        detail = raw
    report.parse_errors.append(
        LineageParseError(
            statement_index=context.index,
            error_type=error_type,
            message=message,
            detail=detail,
            unsupported=True,
            statement_type=statement_type,
        ).model_dump(mode="json")
    )


def _parse_error_message(exc: ParseError) -> str:
    """保留 sqlglot 的结构化位置,但不把整段 SQL/context 写进持久化诊断。"""
    details: list[str] = []
    for error in exc.errors[:3]:
        description = " ".join(str(error.get("description") or "parse failed").split())[:300]
        location: list[str] = []
        if isinstance(error.get("line"), int):
            location.append(f"line {error['line']}")
        if isinstance(error.get("col"), int):
            location.append(f"col {error['col']}")
        highlight = " ".join(str(error.get("highlight") or "").split())[:80]
        if highlight:
            location.append(f"token={highlight!r}")
        details.append(f"{description} ({', '.join(location)})" if location else description)
    return ("ParseError: " + "; ".join(details))[:1000] if details else "ParseError"


def _append_unsupported(
    report: LineageReport,
    context: _StatementContext,
    error_type: str,
    message: str,
    statement_type: str,
) -> None:
    report.parse_errors.append(
        LineageParseError(
            statement_index=context.index,
            error_type=error_type,
            message=message,
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


_RE_LOOKS_LIKE_PLSQL = re.compile(
    r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:DEFINER\s*=\s*\S+\s+)?"
    r"(?:PROCEDURE|FUNCTION|PACKAGE\s+BODY|TRIGGER)\b"
    r"|^\s*DECLARE\b",
    flags=re.IGNORECASE | re.MULTILINE,
)


def _looks_like_plsql(sql_text: str) -> bool:
    # L-3:覆盖 PROCEDURE / FUNCTION / PACKAGE BODY / TRIGGER / 匿名 DECLARE 块 ——
    # 这些形态都要走控制流感知切段,不能拿 sqlglot 顶层解析的整块结果。
    return _RE_LOOKS_LIKE_PLSQL.search(sql_text) is not None


def _non_empty_str(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None
