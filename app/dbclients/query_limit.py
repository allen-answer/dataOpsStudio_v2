from __future__ import annotations

import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from app.dbclients.sql_guard import validate_readonly_sql
from app.domain.datasource import DbType


class QueryLimitError(ValueError):
    """The query could not be safely rewritten with a database-side row cap."""


_DIALECTS: dict[DbType, str] = {
    DbType.MYSQL: "mysql",
    DbType.POSTGRESQL: "postgres",
    # DM is Oracle-compatible for SELECT pagination. DB2 has no sqlglot dialect;
    # its supported FETCH FIRST syntax matches the Oracle generator for the
    # read-only SELECT subset exposed by this project.
    DbType.DM: "oracle",
    DbType.ORACLE: "oracle",
    DbType.DB2: "oracle",
}
_PYFORMAT_BIND_RE = re.compile(r"%\([A-Za-z_][A-Za-z0-9_]*\)s|%s")
_DOLLAR_QUOTE_RE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")
_BIND_SENTINEL_PREFIX = "__dataops_safe_bind_"


def apply_database_row_limit(sql: str, db_type: DbType, row_limit: int) -> str:
    """Return a parsed, dialect-rendered SELECT capped at ``row_limit`` rows.

    The limit is attached to the top-level query AST. A smaller literal
    LIMIT/FETCH supplied by the user is preserved. PyMySQL/psycopg pyformat
    placeholders are shielded while sqlglot parses and renders the statement,
    then restored byte-for-byte so driver binding remains active.
    """

    if row_limit <= 0:
        raise ValueError("row_limit must be positive")
    try:
        dialect = _DIALECTS[db_type]
    except KeyError as exc:
        raise QueryLimitError("Unsupported database dialect for safe row limiting") from exc

    guarded_sql = validate_readonly_sql(sql)
    shielded_sql, bindings = _shield_pyformat_bindings(guarded_sql)
    try:
        expression = sqlglot.parse_one(shielded_sql, read=dialect)
    except ParseError as exc:
        raise QueryLimitError("SQL could not be safely parsed for database row limiting") from exc
    if not isinstance(expression, exp.Query):
        raise QueryLimitError("Only parsed SELECT/WITH queries can be row limited")

    if _has_nonliteral_top_level_window(expression):
        expression = _outer_page(expression, row_limit=row_limit, page_offset=0)
    else:
        existing_limit = _literal_top_level_limit(expression)
        if existing_limit is None or existing_limit > row_limit:
            expression = expression.limit(row_limit, copy=True)
    rendered = expression.sql(dialect=dialect)
    return _restore_pyformat_bindings(rendered, bindings)


def apply_database_page(
    sql: str,
    db_type: DbType,
    *,
    page_offset: int,
    row_limit: int,
) -> str:
    """Return a parsed query window for one stateless database page.

    ``page_offset`` is relative to the user's original query. Literal
    top-level LIMIT/FETCH and OFFSET clauses remain part of that query's
    semantics: the page window is intersected with the original limit and the
    offsets are added. This lets every continuation job fetch only N+1 rows
    without retaining a database cursor between HTTP requests.
    """

    if page_offset < 0:
        raise ValueError("page_offset must be non-negative")
    if row_limit <= 0:
        raise ValueError("row_limit must be positive")
    dialect, expression, bindings = _parse_query(sql, db_type)
    if _has_nonliteral_top_level_window(expression):
        expression = _outer_page(
            expression,
            row_limit=row_limit,
            page_offset=page_offset,
        )
        rendered = expression.sql(dialect=dialect)
        return _restore_pyformat_bindings(rendered, bindings)

    existing_limit = _literal_top_level_limit(expression)
    existing_offset = _literal_top_level_offset(expression)
    if existing_limit is not None:
        row_limit = min(row_limit, max(0, existing_limit - page_offset))
    if row_limit <= 0:
        raise QueryLimitError("Requested page is beyond the query's literal row limit")
    expression = expression.limit(row_limit, copy=True)
    adjusted_offset = existing_offset + page_offset
    if adjusted_offset > 0:
        expression = expression.offset(adjusted_offset, copy=False)
    rendered = expression.sql(dialect=dialect)
    return _restore_pyformat_bindings(rendered, bindings)


def supports_ordered_pagination(sql: str, db_type: DbType) -> bool:
    """Whether repeated page queries have an explicit top-level ordering.

    Stateless continuation is deliberately unavailable without ORDER BY. A
    page request is a fresh read under the datasource's isolation level, so
    callers must also surface that consistency boundary instead of implying a
    snapshot cursor.
    """

    _, expression, _ = _parse_query(sql, db_type)
    order = expression.args.get("order")
    return (
        isinstance(order, exp.Order)
        and bool(order.expressions)
        and not _has_nonliteral_top_level_window(expression)
    )


def _parse_query(sql: str, db_type: DbType) -> tuple[str, exp.Query, list[tuple[str, str]]]:
    try:
        dialect = _DIALECTS[db_type]
    except KeyError as exc:
        raise QueryLimitError("Unsupported database dialect for safe row limiting") from exc

    guarded_sql = validate_readonly_sql(sql)
    shielded_sql, bindings = _shield_pyformat_bindings(guarded_sql)
    try:
        expression = sqlglot.parse_one(shielded_sql, read=dialect)
    except ParseError as exc:
        raise QueryLimitError("SQL could not be safely parsed for database row limiting") from exc
    if not isinstance(expression, exp.Query):
        raise QueryLimitError("Only parsed SELECT/WITH queries can be row limited")
    return dialect, expression, bindings


def _literal_top_level_limit(expression: exp.Query) -> int | None:
    limit = expression.args.get("limit")
    if isinstance(limit, exp.Limit):
        count = limit.expression
    elif isinstance(limit, exp.Fetch):
        count = limit.args.get("count")
    else:
        return None
    if not isinstance(count, exp.Literal) or count.is_string:
        return None
    try:
        value = int(count.this)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _literal_top_level_offset(expression: exp.Query) -> int:
    offset = expression.args.get("offset")
    if not isinstance(offset, exp.Offset):
        return 0
    count = offset.expression
    if not isinstance(count, exp.Literal) or count.is_string:
        raise QueryLimitError("Parameterized top-level OFFSET cannot be safely paginated")
    try:
        value = int(count.this)
    except (TypeError, ValueError) as exc:
        raise QueryLimitError("Top-level OFFSET must be a non-negative integer") from exc
    if value < 0:
        raise QueryLimitError("Top-level OFFSET must be a non-negative integer")
    return value


def _has_nonliteral_top_level_window(expression: exp.Query) -> bool:
    limit = expression.args.get("limit")
    if isinstance(limit, exp.Limit):
        limit_count = limit.expression
    elif isinstance(limit, exp.Fetch):
        limit_count = limit.args.get("count")
    else:
        limit_count = None
    if limit_count is not None and not (
        isinstance(limit_count, exp.Literal) and not limit_count.is_string
    ):
        return True

    offset = expression.args.get("offset")
    if not isinstance(offset, exp.Offset):
        return False
    offset_count = offset.expression
    return not (isinstance(offset_count, exp.Literal) and not offset_count.is_string)


def _outer_page(expression: exp.Query, *, row_limit: int, page_offset: int) -> exp.Query:
    outer = exp.select("*").from_(expression.subquery(alias="_dataops_page"))
    outer = outer.limit(row_limit, copy=False)
    if page_offset > 0:
        outer = outer.offset(page_offset, copy=False)
    return outer


def _shield_pyformat_bindings(sql: str) -> tuple[str, list[tuple[str, str]]]:
    bindings: list[tuple[str, str]] = []
    output: list[str] = []
    index = 0
    while index < len(sql):
        quoted_end = _quoted_or_comment_end(sql, index)
        if quoted_end is not None:
            output.append(sql[index:quoted_end])
            index = quoted_end
            continue
        binding = _PYFORMAT_BIND_RE.match(sql, index)
        if binding is not None:
            sentinel = f"{_BIND_SENTINEL_PREFIX}{len(bindings)}__"
            bindings.append((sentinel, binding.group(0)))
            output.append(sentinel)
            index = binding.end()
            continue
        output.append(sql[index])
        index += 1
    return "".join(output), bindings


def _quoted_or_comment_end(sql: str, start: int) -> int | None:
    if sql.startswith("--", start) or sql.startswith("#", start):
        newline = sql.find("\n", start)
        return len(sql) if newline < 0 else newline + 1
    if sql.startswith("/*", start):
        closing = sql.find("*/", start + 2)
        return len(sql) if closing < 0 else closing + 2

    dollar_quote = _DOLLAR_QUOTE_RE.match(sql, start)
    if dollar_quote is not None:
        delimiter = dollar_quote.group(0)
        closing = sql.find(delimiter, dollar_quote.end())
        return len(sql) if closing < 0 else closing + len(delimiter)

    quote = sql[start]
    if quote not in {"'", '"', "`"}:
        return None
    index = start + 1
    while index < len(sql):
        if sql[index] == "\\":
            index += 2
            continue
        if sql[index] != quote:
            index += 1
            continue
        if index + 1 < len(sql) and sql[index + 1] == quote:
            index += 2
            continue
        return index + 1
    return len(sql)


def _restore_pyformat_bindings(sql: str, bindings: list[tuple[str, str]]) -> str:
    restored = sql
    for sentinel, binding in bindings:
        if sentinel not in restored:
            raise QueryLimitError("SQL parameter binding was not preserved during row limiting")
        restored = restored.replace(sentinel, binding)
    return restored


__all__ = [
    "QueryLimitError",
    "apply_database_page",
    "apply_database_row_limit",
    "supports_ordered_pagination",
]
