from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class SqlGuardError(ValueError):
    """只读 SQL 校验失败。错误信息不得包含 SQL 原文。"""


_FIRST_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_FOR_UPDATE_RE = re.compile(r"\bfor\s+update\b", re.IGNORECASE)
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_FORBIDDEN_TOKENS = frozenset(
    {
        "alter",
        "call",
        "create",
        "delete",
        "drop",
        "execute",
        "grant",
        "insert",
        "lock",
        "merge",
        "replace",
        "revoke",
        "truncate",
        "update",
    }
)


def validate_readonly_sql(sql: str) -> str:
    """移植 1.x 只读 SQL guard:只允许 SELECT / WITH 单语句。"""

    stripped = _strip_leading_comments(sql).strip()
    if not stripped:
        raise SqlGuardError("SQL is empty")

    first = _FIRST_TOKEN_RE.match(stripped)
    if first is None or first.group(0).lower() not in {"select", "with"}:
        raise SqlGuardError("Only SELECT/WITH statements are allowed")

    statements = _split_semicolon_statements(stripped)
    if len(statements) > 1:
        raise SqlGuardError("Multiple SQL statements are not allowed")

    if _FOR_UPDATE_RE.search(stripped):
        raise SqlGuardError("SELECT ... FOR UPDATE is not allowed")

    tokens = {token.group(0).lower() for token in _TOKEN_RE.finditer(stripped)}
    forbidden = sorted(tokens & _FORBIDDEN_TOKENS)
    if forbidden:
        raise SqlGuardError("SQL contains forbidden write/control tokens")

    return stripped


class SqlStatementKind(StrEnum):
    """SQL 工作台可执行语句的分类。QUERY 走只读查询链路,DML/DDL 走写执行链路。"""

    QUERY = "query"
    DML = "dml"
    DDL = "ddl"


# 写语句白名单:按首关键字判定。GRANT/REVOKE/CALL/EXECUTE/LOCK/事务控制等仍拒绝。
_DML_VERBS = frozenset({"insert", "update", "delete", "merge"})
_DDL_VERBS = frozenset({"create", "alter", "drop", "truncate"})
_WHERE_RE = re.compile(r"\bwhere\b", re.IGNORECASE)


@dataclass(frozen=True)
class ClassifiedSql:
    """`classify_executable_sql` 的结果。

    sql:        单条语句,已去掉首部注释与末尾分号(Oracle 驱动遇末尾分号报 ORA-00911)。
    verb:       首关键字小写(select / with / insert / update / ... / truncate)。
    missing_where: UPDATE / DELETE 不带 WHERE(整表改 / 删),供前端标红、审计留痕。
    """

    sql: str
    kind: SqlStatementKind
    verb: str
    missing_where: bool = False

    @property
    def is_write(self) -> bool:
        return self.kind is not SqlStatementKind.QUERY


def classify_executable_sql(sql: str) -> ClassifiedSql:
    """SQL 工作台执行入口的校验:SELECT/WITH 走只读 guard,DML/DDL 单语句放行。

    写语句是否真能执行由调用方再按数据源 operation_policy.allow_dml 判定;
    这里只做语句形态校验。错误信息同样不得包含 SQL 原文。
    """

    stripped = _strip_leading_comments(sql).strip()
    if not stripped:
        raise SqlGuardError("SQL is empty")
    first = _FIRST_TOKEN_RE.match(stripped)
    verb = first.group(0).lower() if first is not None else ""
    if verb in {"select", "with"}:
        return ClassifiedSql(sql=validate_readonly_sql(sql), kind=SqlStatementKind.QUERY, verb=verb)
    if verb in _DML_VERBS:
        kind = SqlStatementKind.DML
    elif verb in _DDL_VERBS:
        kind = SqlStatementKind.DDL
    else:
        raise SqlGuardError(
            "Only SELECT/WITH/INSERT/UPDATE/DELETE/MERGE/CREATE/ALTER/DROP/TRUNCATE "
            "statements are allowed"
        )
    statements = _split_semicolon_statements(stripped)
    if len(statements) != 1:
        raise SqlGuardError("Multiple SQL statements are not allowed")
    statement = statements[0]
    missing_where = verb in {"update", "delete"} and not _WHERE_RE.search(
        _blank_literals_and_comments(statement)
    )
    return ClassifiedSql(sql=statement, kind=kind, verb=verb, missing_where=missing_where)


def _blank_literals_and_comments(sql: str) -> str:
    """把字符串字面量、引号标识符与注释替换成空格,只留语句骨架供关键字判定。"""

    out: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(sql):
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < len(sql) else ""
        if quote is not None:
            if ch == quote and not (quote != "`" and _is_escaped(sql, i)):
                quote = None
            out.append(" ")
            i += 1
            continue
        if ch == "-" and nxt == "-":
            newline = sql.find("\n", i)
            end = len(sql) if newline < 0 else newline
            out.append(" " * (end - i))
            i = end
            continue
        if ch == "/" and nxt == "*":
            close = sql.find("*/", i + 2)
            end = len(sql) if close < 0 else close + 2
            out.append(" " * (end - i))
            i = end
            continue
        if ch in {"'", '"', "`"}:
            quote = ch
            out.append(" ")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _strip_leading_comments(sql: str) -> str:
    remaining = sql.lstrip()
    while True:
        if remaining.startswith("--"):
            newline = remaining.find("\n")
            if newline < 0:
                return ""
            remaining = remaining[newline + 1 :].lstrip()
            continue
        if remaining.startswith("/*"):
            end = remaining.find("*/")
            if end < 0:
                raise SqlGuardError("Unclosed SQL block comment")
            remaining = remaining[end + 2 :].lstrip()
            continue
        return remaining


def _split_semicolon_statements(sql: str) -> list[str]:
    statements: list[str] = []
    start = 0
    in_single = False
    in_double = False
    in_backtick = False
    in_line_comment = False
    in_block_comment = False
    i = 0

    while i < len(sql):
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < len(sql) else ""

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue

        if not in_single and not in_double and not in_backtick:
            if ch == "-" and nxt == "-":
                in_line_comment = True
                i += 2
                continue
            if ch == "/" and nxt == "*":
                in_block_comment = True
                i += 2
                continue
            if ch == ";":
                part = sql[start:i].strip()
                if part:
                    statements.append(part)
                start = i + 1
                i += 1
                continue

        if ch == "'" and not in_double and not in_backtick:
            in_single = not in_single if not _is_escaped(sql, i) else in_single
        elif ch == '"' and not in_single and not in_backtick:
            in_double = not in_double if not _is_escaped(sql, i) else in_double
        elif ch == "`" and not in_single and not in_double:
            in_backtick = not in_backtick
        i += 1

    tail = sql[start:].strip()
    if tail:
        statements.append(tail)
    return statements


def _is_escaped(sql: str, index: int) -> bool:
    backslashes = 0
    i = index - 1
    while i >= 0 and sql[i] == "\\":
        backslashes += 1
        i -= 1
    return backslashes % 2 == 1
