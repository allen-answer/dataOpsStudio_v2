from __future__ import annotations

import re

_DML_START_RE = re.compile(r"^\s*(INSERT|UPDATE|MERGE|DELETE|SELECT)\b", re.IGNORECASE)


def split_plsql_statements(sql_text: str) -> list[str]:
    body = _procedure_body(sql_text)
    statements = _split_semicolon_statements(body)
    return [statement for statement in statements if _DML_START_RE.match(statement)]


def _procedure_body(sql_text: str) -> str:
    text = _strip_line_comments(sql_text).strip()
    begin_match = re.search(r"\bBEGIN\b", text, flags=re.IGNORECASE)
    if begin_match:
        text = text[begin_match.end() :]
    end_match = re.search(r"\bEND\b\s*[A-Za-z0-9_$#]*\s*;?\s*/?\s*$", text, flags=re.IGNORECASE)
    if end_match:
        text = text[: end_match.start()]
    return text


def _strip_line_comments(sql_text: str) -> str:
    return "\n".join(line.split("--", 1)[0] for line in sql_text.splitlines())


def _split_semicolon_statements(sql_text: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(sql_text):
        char = sql_text[index]
        current.append(char)
        if quote is not None:
            if char == quote:
                next_char = sql_text[index + 1] if index + 1 < len(sql_text) else ""
                if next_char == quote:
                    current.append(next_char)
                    index += 1
                else:
                    quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char == ";":
            statement = "".join(current).strip().rstrip(";").strip()
            if statement:
                statements.append(statement)
            current = []
        index += 1
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements
