from __future__ import annotations

import re


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
