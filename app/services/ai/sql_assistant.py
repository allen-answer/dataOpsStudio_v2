from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError
from sqlglot.optimizer.scope import Scope, traverse_scope

from app.dbclients.sql_guard import SqlGuardError, validate_readonly_sql
from app.domain.ai import AiResponse, ReasoningMode
from app.domain.schema import Column

ValidationState = Literal["passed", "failed", "partial"]
_COMPLEX_WORDS = re.compile(
    r"\b(sum|count|avg|average|group|aggregate|join|subquery|cte|window|rank|union|intersect|except)\b",
    re.IGNORECASE,
)
_REPAIRABLE = {
    "provider_reasoning_only",
    "provider_output_truncated",
    "provider_invalid_response",
    "sql_parse_failed",
    "sql_unknown_table",
    "sql_unknown_column",
}


@dataclass(frozen=True)
class TableSchema:
    schema_name: str
    table_name: str
    columns: tuple[Column, ...]


@dataclass(frozen=True)
class TableCandidate:
    schema_name: str
    table_name: str
    matched_by: tuple[str, ...]


@dataclass(frozen=True)
class SqlValidation:
    ok: bool
    readonly: ValidationState
    tables: ValidationState
    columns: ValidationState
    diagnostic_code: str | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class _RelationColumns:
    names: frozenset[str]
    exhaustive: bool


def classify_reasoning_mode(natural_language: str, tables: list[TableSchema]) -> ReasoningMode:
    if len(tables) != 1 or _COMPLEX_WORDS.search(natural_language):
        return ReasoningMode.ENABLED
    return ReasoningMode.DISABLED


def diagnose_empty_response(response: AiResponse) -> str | None:
    if response.content.strip():
        return None
    if response.finish_reason == "length":
        return "provider_output_truncated"
    if response.reasoning_chars > 0:
        return "provider_reasoning_only"
    return "provider_invalid_response"


def should_repair(diagnostic_code: str, *, attempts: int) -> bool:
    return attempts == 1 and diagnostic_code in _REPAIRABLE


def _editor_table_names(editor_sql: str) -> set[str]:
    if not editor_sql.strip():
        return set()
    try:
        statements = sqlglot.parse(editor_sql)
    except ParseError:
        return set()
    return {
        table.name.casefold()
        for statement in statements
        if statement is not None
        for table in statement.find_all(exp.Table)
        if table.name
    }


def rank_table_candidates(
    natural_language: str,
    editor_sql: str,
    tables: list[TableSchema],
    *,
    limit: int,
) -> list[TableCandidate]:
    words = set(re.findall(r"[\w]+", natural_language.casefold()))
    editor_tables = _editor_table_names(editor_sql)
    scored: list[tuple[int, int, TableCandidate]] = []
    for index, table in enumerate(tables):
        matched: list[str] = []
        score = 0
        table_name = table.table_name.casefold()
        if table_name in editor_tables:
            matched.append("editor_reference")
            score += 100
        if table_name in words or any(word in table_name for word in words):
            matched.append("table_name")
            score += 20
        if any(
            column.name.casefold() in words or any(word in column.name.casefold() for word in words)
            for column in table.columns
        ):
            matched.append("column_name")
            score += 2
        if score:
            scored.append(
                (
                    -score,
                    index,
                    TableCandidate(
                        schema_name=table.schema_name,
                        table_name=table.table_name,
                        matched_by=tuple(matched),
                    ),
                )
            )
    scored.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in scored[:limit]]


def extract_sql(content: str) -> str | None:
    fenced = re.search(r"```(?:sql)?\s*(.*?)```", content, flags=re.IGNORECASE | re.DOTALL)
    candidate = (fenced.group(1) if fenced else content).strip()
    if not candidate:
        return None
    try:
        statements = [item for item in sqlglot.parse(candidate) if item is not None]
    except ParseError:
        return candidate
    return candidate.rstrip(";").strip() if len(statements) == 1 else None


def build_generation_prompt(
    natural_language: str, *, dialect: str, revision_instruction: str | None = None
) -> str:
    if revision_instruction is None:
        return (
            f"Generate one read-only {dialect} SELECT for this request. "
            "Use only the supplied schema. Return SQL only.\nRequest: "
            f"{natural_language.strip()}"
        )
    return (
        f"Revise the supplied candidate into one read-only {dialect} SELECT. "
        "Use only the supplied schema. Return SQL only.\nRevision: "
        f"{revision_instruction.strip()}"
    )


def build_repair_prompt(candidate_sql: str, diagnostic_code: str, *, dialect: str) -> str:
    return (
        f"Repair this candidate into one read-only {dialect} SELECT "
        "using only the supplied schema. "
        f"Validation code: {diagnostic_code}. Return SQL only.\nCandidate SQL:\n{candidate_sql}"
    )


def _table_match(node: exp.Table, tables: list[TableSchema]) -> TableSchema | None:
    name = node.name.casefold()
    schema = node.db.casefold() if node.db else None
    matches = [
        item
        for item in tables
        if item.table_name.casefold() == name
        and (schema is None or item.schema_name.casefold() == schema)
    ]
    return matches[0] if len(matches) == 1 else None


def _scope_output(scope: Scope) -> _RelationColumns:
    if scope.outer_columns:
        return _RelationColumns(
            names=frozenset(name.casefold() for name in scope.outer_columns),
            exhaustive=False,
        )

    expression = scope.expression
    if isinstance(expression, exp.SetOperation):
        return _RelationColumns(
            names=frozenset(
                name.casefold() for name in expression.named_selects if name and name != "*"
            ),
            exhaustive=False,
        )
    if not isinstance(expression, exp.Select):
        return _RelationColumns(names=frozenset(), exhaustive=False)

    names: set[str] = set()
    exhaustive = True
    for projection in expression.selects:
        name = projection.alias_or_name
        if name and name != "*":
            names.add(name.casefold())

        projected = projection.this if isinstance(projection, exp.Alias) else projection
        if not isinstance(projected, exp.Column) or projected.name == "*":
            exhaustive = False

    return _RelationColumns(names=frozenset(names), exhaustive=exhaustive)


def _physical_relation(table: TableSchema) -> _RelationColumns:
    return _RelationColumns(
        names=frozenset(column.name.casefold() for column in table.columns),
        exhaustive=True,
    )


def _build_scope_relations(
    scopes: list[Scope],
    outputs: dict[int, _RelationColumns],
    tables: list[TableSchema],
) -> dict[int, dict[str, _RelationColumns]] | None:
    relations_by_scope: dict[int, dict[str, _RelationColumns]] = {}
    for scope in scopes:
        relations: dict[str, _RelationColumns] = {}
        for alias, (_, source) in scope.selected_sources.items():
            if isinstance(source, exp.Table):
                matched = _table_match(source, tables)
                if matched is None:
                    return None
                relation = _physical_relation(matched)
            else:
                relation = outputs[id(source)]
            relations[alias.casefold()] = relation
        relations_by_scope[id(scope)] = relations
    return relations_by_scope


def _projection_aliases(scope: Scope) -> frozenset[str]:
    expression = scope.expression
    if not isinstance(expression, exp.Select):
        return frozenset()
    return frozenset(
        projection.alias.casefold() for projection in expression.selects if projection.alias
    )


def _is_top_level_clause(column: exp.Column, clause_type: type[exp.Expr], scope: Scope) -> bool:
    clause = column.find_ancestor(clause_type)
    if clause is None:
        return False
    query = clause.find_ancestor(exp.Select, exp.SetOperation)
    return query is scope.expression


def _projection_alias_allowed(column: exp.Column, scope: Scope, dialect: str) -> bool:
    if column.name.casefold() not in _projection_aliases(scope):
        return False

    order = column.find_ancestor(exp.Order)
    if (
        order is not None
        and order.find_ancestor(exp.Window, exp.WithinGroup) is None
        and _is_top_level_clause(column, exp.Order, scope)
    ):
        return True

    normalized_dialect = dialect.casefold()
    if normalized_dialect == "mysql":
        return _is_top_level_clause(column, exp.Group, scope) or _is_top_level_clause(
            column, exp.Having, scope
        )
    if normalized_dialect in {"bigquery", "duckdb", "snowflake"}:
        return _is_top_level_clause(column, exp.Qualify, scope)
    return False


def _resolve_column(
    column: exp.Column,
    scope: Scope,
    relations_by_scope: dict[int, dict[str, _RelationColumns]],
    *,
    dialect: str,
) -> ValidationState:
    relations = relations_by_scope[id(scope)]
    name = column.name.casefold()
    qualifier = column.table.casefold() if column.table else ""

    if qualifier:
        relation = relations.get(qualifier)
        if relation is not None:
            if not relation.exhaustive:
                return "partial"
            return "passed" if name in relation.names else "failed"
        if scope.can_be_correlated and scope.parent is not None:
            return _resolve_column(
                column,
                scope.parent,
                relations_by_scope,
                dialect=dialect,
            )
        return "failed"

    definite_matches = sum(
        relation.exhaustive and name in relation.names for relation in relations.values()
    )
    has_uncertain_source = any(not relation.exhaustive for relation in relations.values())
    if definite_matches:
        return "partial" if definite_matches != 1 or has_uncertain_source else "passed"
    if has_uncertain_source:
        return "partial"
    if _projection_alias_allowed(column, scope, dialect):
        return "passed"
    if scope.can_be_correlated and scope.parent is not None:
        return _resolve_column(
            column,
            scope.parent,
            relations_by_scope,
            dialect=dialect,
        )
    return "failed"


def validate_generated_sql(sql: str, *, dialect: str, tables: list[TableSchema]) -> SqlValidation:
    try:
        guarded = validate_readonly_sql(sql)
    except SqlGuardError:
        return SqlValidation(
            ok=False,
            readonly="failed",
            tables="failed",
            columns="failed",
            diagnostic_code="sql_not_readonly",
        )
    try:
        statements = [
            statement for statement in sqlglot.parse(guarded, read=dialect) if statement is not None
        ]
    except ParseError:
        return SqlValidation(
            ok=False,
            readonly="passed",
            tables="failed",
            columns="failed",
            diagnostic_code="sql_parse_failed",
        )
    if len(statements) != 1:
        return SqlValidation(
            ok=False,
            readonly="passed",
            tables="failed",
            columns="failed",
            diagnostic_code="sql_parse_failed",
        )

    scopes = traverse_scope(statements[0])
    outputs = {id(scope): _scope_output(scope) for scope in scopes}
    try:
        relations_by_scope = _build_scope_relations(scopes, outputs, tables)
    except OptimizeError:
        return SqlValidation(
            ok=False,
            readonly="passed",
            tables="failed",
            columns="failed",
            diagnostic_code="sql_parse_failed",
        )
    if relations_by_scope is None:
        return SqlValidation(
            ok=False,
            readonly="passed",
            tables="failed",
            columns="failed",
            diagnostic_code="sql_unknown_table",
        )

    partial = False
    for scope in scopes:
        for column in scope.find_all(exp.Column):
            if column.name == "*":
                continue
            resolution = _resolve_column(
                column,
                scope,
                relations_by_scope,
                dialect=dialect,
            )
            if resolution == "failed":
                return SqlValidation(
                    ok=False,
                    readonly="passed",
                    tables="passed",
                    columns="failed",
                    diagnostic_code="sql_unknown_column",
                )
            partial = partial or resolution == "partial"

    return SqlValidation(
        ok=True,
        readonly="passed",
        tables="passed",
        columns="partial" if partial else "passed",
        warnings=("sql_column_validation_partial",) if partial else (),
    )
