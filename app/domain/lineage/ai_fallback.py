"""Lineage AI 兜底解析的纯函数层(设计稿 §2.4 第 7 条,AI Assist 表 §2.7.3)。

职责(不碰 DB / 不碰网络,供 API 路由层组装):
1. 把确定性解析失败的语句(parse_errors.statement_index)映射回 SQL 片段;
2. ★ R-AI / egress:送 AI 前脱敏字面量(字符串 / 数字),SQL 结构 + 表列名
   属 L2(§2.7.3 表格把 Lineage AI 兜底最高 egress 定为 L2;字面量属 L3+,
   必须遮蔽后才能按 L2 出站);
3. 防御式解析 AI 返回的 JSON 血缘边(缺 confidence 给缺省值并 clamp 到 [0,1])。

★ 语句切分逻辑与 parser._parse_or_split_plsql 保持一致(该函数私有且
parser.py 属另一在途 PR 领地,不可改动,故此处按同一规则复制实现)。
"""

from __future__ import annotations

import json
import re
from typing import Any

import sqlglot
from pydantic import BaseModel
from sqlglot.errors import ParseError

from app.domain.lineage.dialects import register_lineage_dialects
from app.domain.lineage.plsql import split_plsql_statements

# AI 返回缺 confidence 时的缺省值(设计稿未定;人工确认前不与确定性边同权重,
# 取中间值,列入报告裁决项)。
DEFAULT_INFERRED_CONFIDENCE = 0.5
# 送 AI 的失败语句数上限(bound token 用量;与 parse_errors 明细上限同数量级)。
MAX_FALLBACK_STATEMENTS = 20
# 单次 AI 响应可接受的边数上限(防御异常膨胀的响应)。
MAX_INFERRED_EDGES = 200

_MAX_TABLE_NAME_LEN = 512
_MAX_COLUMN_NAME_LEN = 256

_STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")
_NUMBER_LITERAL_RE = re.compile(r"(?<![\w$.\"'`])\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")


class InferredTableEdge(BaseModel):
    source_table: str
    target_table: str
    confidence: float = DEFAULT_INFERRED_CONFIDENCE


class InferredColumnEdge(BaseModel):
    source_table: str
    source_column: str
    target_table: str
    target_column: str
    confidence: float = DEFAULT_INFERRED_CONFIDENCE


class InferredLineageEdges(BaseModel):
    table_edges: list[InferredTableEdge] = []
    column_edges: list[InferredColumnEdge] = []


def redact_sql_literals(sql: str) -> str:
    """遮蔽字符串 / 数字字面量,保留 SQL 结构与表列名(L2 出站前置)。"""
    redacted = _STRING_LITERAL_RE.sub("'?'", sql)
    return _NUMBER_LITERAL_RE.sub("0", redacted)


def fallback_statements(
    *,
    sql_text: str,
    dialect: str,
    parse_errors: list[dict[str, Any]],
    limit: int = MAX_FALLBACK_STATEMENTS,
) -> list[dict[str, Any]]:
    """把 parse_errors 指向的语句映射为脱敏后的 AI 输入片段。"""
    indexes = sorted(
        {
            int(error["statement_index"])
            for error in parse_errors
            if isinstance(error, dict)
            and isinstance(error.get("statement_index"), int)
            and not isinstance(error.get("statement_index"), bool)
        }
    )
    if not indexes:
        return []
    statements = _split_statements(sql_text, dialect)
    fragments: list[dict[str, Any]] = []
    for index in indexes:
        if len(fragments) >= limit:
            break
        if 0 <= index < len(statements):
            fragments.append(
                {"statement_index": index, "sql": redact_sql_literals(statements[index])}
            )
    return fragments


def parse_inferred_edges(content: str) -> InferredLineageEdges | None:
    """防御式解析 AI 返回的血缘边 JSON;结构不对返回 None(调用方降级)。"""
    payload = _load_json_object(content)
    if payload is None:
        return None
    table_edges: list[InferredTableEdge] = []
    for item in _as_list(payload.get("table_edges"))[:MAX_INFERRED_EDGES]:
        edge = _table_edge(item)
        if edge is not None and edge not in table_edges:
            table_edges.append(edge)
    column_edges: list[InferredColumnEdge] = []
    for item in _as_list(payload.get("column_edges"))[:MAX_INFERRED_EDGES]:
        column_edge = _column_edge(item)
        if column_edge is not None and column_edge not in column_edges:
            column_edges.append(column_edge)
    return InferredLineageEdges(table_edges=table_edges, column_edges=column_edges)


def _split_statements(sql_text: str, dialect: str) -> list[str]:
    """与 parser._parse_or_split_plsql 同规则的语句切分(见模块 docstring)。"""
    register_lineage_dialects()
    normalized = dialect.lower()
    if normalized == "dameng":
        normalized = "dm"
    try:
        parsed = sqlglot.parse(sql_text, read=normalized)
    except ParseError:
        parsed = []
    if parsed and not _looks_like_plsql(sql_text):
        return [
            expression.sql(dialect=normalized) for expression in parsed if expression is not None
        ]
    split = split_plsql_statements(sql_text)
    if split:
        return split
    return [sql_text]


def _looks_like_plsql(sql_text: str) -> bool:
    upper = sql_text.upper()
    return "CREATE PROCEDURE" in upper or "CREATE OR REPLACE PROCEDURE" in upper


def _load_json_object(content: str) -> dict[str, Any] | None:
    stripped = content.strip()
    for candidate in (stripped, _braced_substring(stripped)):
        if candidate is None:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _braced_substring(content: str) -> str | None:
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end <= start:
        return None
    return content[start : end + 1]


def _as_list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _table_edge(item: object) -> InferredTableEdge | None:
    if not isinstance(item, dict):
        return None
    source = _name(item.get("source_table"), _MAX_TABLE_NAME_LEN)
    target = _name(item.get("target_table"), _MAX_TABLE_NAME_LEN)
    if source is None or target is None or source == target:
        return None
    return InferredTableEdge(
        source_table=source,
        target_table=target,
        confidence=_confidence(item.get("confidence")),
    )


def _column_edge(item: object) -> InferredColumnEdge | None:
    if not isinstance(item, dict):
        return None
    source_table = _name(item.get("source_table"), _MAX_TABLE_NAME_LEN)
    source_column = _name(item.get("source_column"), _MAX_COLUMN_NAME_LEN)
    target_table = _name(item.get("target_table"), _MAX_TABLE_NAME_LEN)
    target_column = _name(item.get("target_column"), _MAX_COLUMN_NAME_LEN)
    if None in (source_table, source_column, target_table, target_column):
        return None
    if (source_table, source_column) == (target_table, target_column):
        return None
    return InferredColumnEdge(
        source_table=str(source_table),
        source_column=str(source_column),
        target_table=str(target_table),
        target_column=str(target_column),
        confidence=_confidence(item.get("confidence")),
    )


def _name(value: object, max_len: int) -> str | None:
    if not isinstance(value, str):
        return None
    name = value.strip()
    if not name or len(name) > max_len:
        return None
    return name


def _confidence(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULT_INFERRED_CONFIDENCE
    return min(1.0, max(0.0, float(value)))


__all__ = [
    "DEFAULT_INFERRED_CONFIDENCE",
    "MAX_FALLBACK_STATEMENTS",
    "MAX_INFERRED_EDGES",
    "InferredColumnEdge",
    "InferredLineageEdges",
    "InferredTableEdge",
    "fallback_statements",
    "parse_inferred_edges",
    "redact_sql_literals",
]
