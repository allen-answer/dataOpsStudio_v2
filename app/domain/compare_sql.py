"""SQL compare projection aliases shared by API preview and Worker execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from app.domain.datasource import DbType


class CompareSqlProjectionError(ValueError):
    """A compare SQL projection cannot be inspected or safely normalized."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class CompareSqlProjection:
    name: str
    generated: bool
    projection_index: int
    expression: str | None = None


@dataclass(frozen=True)
class CompareSqlPlan:
    sql: str
    projections: tuple[CompareSqlProjection, ...]
    rewritten: bool


_DIALECTS: dict[DbType, str | None] = {
    DbType.MYSQL: "mysql",
    DbType.DM: "oracle",
    DbType.ORACLE: "oracle",
    DbType.POSTGRESQL: "postgres",
    DbType.DB2: None,
}


def inspect_compare_sql(sql: str) -> tuple[CompareSqlProjection, ...]:
    """Return deterministic output metadata without persisting rewritten SQL."""

    return _build_plan(sql, dialect=None).projections


def normalize_compare_sql(sql: str, db_type: DbType) -> CompareSqlPlan:
    """Alias unlabelled computed projections with collision-free ``RESULT_n`` names."""

    return _build_plan(sql, dialect=_DIALECTS[db_type])


def legacy_generated_aliases(
    columns: list[str],
    projections: tuple[CompareSqlProjection, ...],
) -> dict[str, str]:
    """Map only proven driver ordinal names to their generated stable aliases."""

    repairs: dict[str, str] = {}
    for projection in projections:
        index = projection.projection_index - 1
        if not projection.generated or index >= len(columns):
            continue
        configured_name = columns[index]
        if configured_name == str(projection.projection_index):
            repairs[configured_name] = projection.name
    return repairs


def _build_plan(sql: str, *, dialect: str | None) -> CompareSqlPlan:
    if not sql.strip():
        raise CompareSqlProjectionError("invalid_sql")
    try:
        statement = sqlglot.parse_one(sql, read=dialect)
    except (ParseError, ValueError) as exc:
        raise CompareSqlProjectionError("invalid_sql") from exc
    if isinstance(statement, exp.SetOperation):
        raise CompareSqlProjectionError("explicit_alias_required")
    if not isinstance(statement, exp.Select):
        raise CompareSqlProjectionError("invalid_sql")

    selections = list(statement.expressions)
    occupied = {
        name.casefold()
        for selection in selections
        if (name := _existing_projection_name(selection)) is not None
    }
    next_generated = 1
    rewritten = False
    normalized: list[exp.Expression] = []
    projections: list[CompareSqlProjection] = []

    for index, selection in enumerate(selections, start=1):
        existing_name = _existing_projection_name(selection)
        if existing_name is not None:
            normalized.append(selection)
            projections.append(
                CompareSqlProjection(
                    name=existing_name,
                    generated=False,
                    projection_index=index,
                )
            )
            continue

        while f"result_{next_generated}".casefold() in occupied:
            next_generated += 1
        generated_name = f"RESULT_{next_generated}"
        next_generated += 1
        occupied.add(generated_name.casefold())
        expression_sql = selection.sql(dialect=dialect, pretty=True)
        normalized.append(
            cast(exp.Expression, exp.alias_(selection.copy(), generated_name, quoted=False))
        )
        projections.append(
            CompareSqlProjection(
                name=generated_name,
                generated=True,
                projection_index=index,
                expression=expression_sql,
            )
        )
        rewritten = True

    if rewritten:
        statement.set("expressions", normalized)
    normalized_sql = statement.sql(dialect=dialect)
    return CompareSqlPlan(
        sql=normalized_sql,
        projections=tuple(projections),
        rewritten=rewritten,
    )


def _existing_projection_name(selection: exp.Expression) -> str | None:
    if isinstance(selection, exp.Alias):
        return selection.alias
    if isinstance(selection, exp.Star):
        return "*"
    if isinstance(selection, exp.Column):
        if isinstance(selection.this, exp.Star):
            return "*"
        return selection.name
    return None
