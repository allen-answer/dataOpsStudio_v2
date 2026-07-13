from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

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


def _has_column(table: TableSchema, name: str) -> bool:
    lowered = name.casefold()
    return any(column.name.casefold() == lowered for column in table.columns)


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

    statement = statements[0]
    cte_names = {
        cte.alias_or_name.casefold() for cte in statement.find_all(exp.CTE) if cte.alias_or_name
    }
    cte_columns: dict[str, set[str]] = {}
    for cte in statement.find_all(exp.CTE):
        select_query = cte.this.find(exp.Select) if cte.this is not None else None
        if not cte.alias_or_name or not isinstance(select_query, exp.Select):
            continue
        cte_columns[cte.alias_or_name.casefold()] = {
            select.alias_or_name.casefold()
            for select in select_query.selects
            if select.alias_or_name
        }
    alias_tables: dict[str, TableSchema] = {}
    derived_aliases = set(cte_names)
    derived_columns = dict(cte_columns)
    for node in statement.find_all(exp.Table):
        if node.name.casefold() in cte_names:
            alias = node.alias_or_name.casefold()
            derived_aliases.add(alias)
            derived_columns[alias] = cte_columns[node.name.casefold()]
            continue
        matched = _table_match(node, tables)
        if matched is None:
            return SqlValidation(
                ok=False,
                readonly="passed",
                tables="failed",
                columns="failed",
                diagnostic_code="sql_unknown_table",
            )
        alias_tables[node.alias_or_name.casefold()] = matched
        alias_tables[node.name.casefold()] = matched

    projected_aliases = (
        {select.alias.casefold() for select in statement.selects if select.alias}
        if isinstance(statement, exp.Select)
        else set()
    )
    partial = False
    for column in statement.find_all(exp.Column):
        if column.name == "*":
            continue
        qualifier = column.table.casefold() if column.table else ""
        if qualifier:
            if qualifier in derived_aliases:
                if column.name.casefold() not in derived_columns.get(qualifier, set()):
                    return SqlValidation(
                        ok=False,
                        readonly="passed",
                        tables="passed",
                        columns="failed",
                        diagnostic_code="sql_unknown_column",
                    )
                continue
            table = alias_tables.get(qualifier)
            if table is None or not _has_column(table, column.name):
                return SqlValidation(
                    ok=False,
                    readonly="passed",
                    tables="passed",
                    columns="failed",
                    diagnostic_code="sql_unknown_column",
                )
            continue
        matches = [table for table in tables if _has_column(table, column.name)]
        if not matches and column.name.casefold() not in projected_aliases:
            return SqlValidation(
                ok=False,
                readonly="passed",
                tables="passed",
                columns="failed",
                diagnostic_code="sql_unknown_column",
            )
        if len(matches) != 1:
            partial = True

    return SqlValidation(
        ok=True,
        readonly="passed",
        tables="passed",
        columns="partial" if partial else "passed",
        warnings=("sql_column_validation_partial",) if partial else (),
    )
