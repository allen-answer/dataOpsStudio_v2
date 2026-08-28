"""AI 组装断链跳的上游定位 SQL(受约束单轮管道,设计稿 Q5 形态 A)。

确定性反推(``compare_upstream_sql``)在遇到 EXPRESSION / AGGREGATION 这类不可反推的
变换时只能停下并说"断在这"。本模块让 AI 接手**且仅接手这一件事**:读血缘边结构与
已存档的 SQL 原文,组装出该跳的上游查询模板。

★ 纯 domain:零 IO、不碰 gateway(R1)。上下文组装 / prompt / 防御式解析 / 校验都是
纯函数,可离线单测;route 层只负责取数、调 gateway、回填字面量。

**架构基石 —— L4 永不出网**:差异行主键值属样本数据(L4,``app/domain/ai.py``),
``ai_configs.max_auto_egress_level`` 的 CHECK 约束把自动出网封在 L3,架构上就不可能
把行值喂给模型。因此:

- 送进 prompt 的只有**血缘边结构 + schema 结构(L2)** 与 **已存档 SQL 原文(L3)**;
  本模块的函数签名里根本没有主键值这个入参,物理上取不到(照抄 ``ai_copilot`` 的构造)。
- 模型产出的是**带 ``{{PK_TUPLES}}`` 占位符的模板**,字面量由 route 层用
  ``sql_literal`` 本地回填 —— 模型既没见过值,也拼不出值。

**不信任模型输出**:回填之前先过 ``validate_ai_sql``——必须是单条只读 SELECT、
出现的标识符必须落在血缘图给出的白名单内、必须含占位符。任一条不满足就整体判失败,
**绝不输出半成品 SQL**。

**AI 自报置信度不采信为独立指标**:模型自评分无校准。自报值只作为**下调系数**乘到
确定性边的 min confidence 上(``ai_confidence`` <= 1),再 clamp 到 [0.50, 1.00]——
即 AI 只能让置信度更低,不能给自己加分。
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from app.domain.compare_upstream_sql import (
    CONFIDENCE_CEILING,
    CONFIDENCE_FLOOR,
    UpstreamEdge,
)

__all__ = [
    "PK_PLACEHOLDER",
    "AiUpstreamResult",
    "build_ai_upstream_prompt",
    "build_lineage_context_payload",
    "combine_confidence",
    "fill_pk_placeholder",
    "parse_ai_upstream_response",
    "validate_ai_sql",
]

# 模型必须原样保留的主键占位符 —— 它见不到真实值,也不允许自己编。
PK_PLACEHOLDER = "{{PK_TUPLES}}"

# 上下文截断上限:控制 prompt token 与出网面。宁可少送,绝不送样本数据。
MAX_CONTEXT_EDGES = 40
MAX_SQL_TEXT_CHARS = 8000

_SQL_FENCE = re.compile(r"```(?:sql)?\s*(?P<sql>.+?)```", re.IGNORECASE | re.DOTALL)
_CONFIDENCE_RE = re.compile(r"confidence\s*[:=]\s*(?P<value>[01](?:\.\d+)?)", re.IGNORECASE)


@dataclass(frozen=True)
class AiUpstreamResult:
    """防御式解析出的模型产出(尚未校验、尚未回填字面量)。"""

    sql_template: str
    ai_confidence: float
    explanation: str | None


def build_lineage_context_payload(
    *,
    focus_table: str,
    blocked_table: str,
    key_columns: Sequence[str],
    edges: Sequence[UpstreamEdge],
    max_edges: int = MAX_CONTEXT_EDGES,
) -> tuple[dict[str, object], bool]:
    """把血缘边压成 L2 结构 JSON(表名 / 列名 / 变换类型 / 置信度)。

    返回 (payload, truncated)。**只取结构字段**,函数签名里没有行值这个入参 ——
    与 ``ai_copilot.build_schema_context`` 同一构造纪律。
    """
    truncated = len(edges) > max_edges
    kept = list(edges)[:max_edges]
    payload: dict[str, object] = {
        "focus_table": focus_table,
        "blocked_upstream_table": blocked_table,
        "key_columns": [str(col) for col in key_columns],
        "column_edges": [
            {
                "source": f"{edge.source_table}.{edge.source_column}",
                "target": f"{edge.target_table}.{edge.target_column}",
                "transformation": edge.transformation_subtype,
                "inference_status": edge.inference_status,
                "confidence": round(float(edge.confidence), 4),
            }
            for edge in kept
        ],
    }
    return payload, truncated


def build_ai_upstream_prompt(
    *,
    dialect: str,
    lineage_payload: dict[str, object],
    archived_sql: str | None,
    max_sql_chars: int = MAX_SQL_TEXT_CHARS,
) -> tuple[str, str | None, bool]:
    """构造单轮 prompt。返回 (prompt, 截断后的 SQL 原文, 是否截断)。

    SQL 原文是 L3(业务过滤条件可见),单独返回让 route 层按 L3 挂进 AiContext ——
    出网级别标注必须与内容一致,不能囫囵按 L2 报。
    """
    truncated = False
    sql_text = archived_sql
    if sql_text is not None and len(sql_text) > max_sql_chars:
        sql_text = sql_text[:max_sql_chars]
        truncated = True

    prompt = (
        "You are a data lineage assistant. A row-locating query must be pushed one hop "
        "upstream, but the column lineage edge for that hop is a non-invertible "
        "transformation, so it cannot be mapped mechanically.\n\n"
        "Given the lineage edges (JSON) and, when available, the archived ETL SQL that "
        "produced the downstream table, write ONE read-only "
        f"{dialect} SELECT against the upstream table that returns the rows "
        "corresponding to a given set of downstream primary keys.\n\n"
        "Hard requirements:\n"
        f"1. Put the literal token {PK_PLACEHOLDER} exactly where the downstream primary "
        "key tuples belong (typically the right-hand side of an IN predicate). NEVER "
        "invent, guess or write any key values - you have not been given any.\n"
        "2. Emit exactly one statement, and it MUST be a SELECT. No DDL, no DML, no CTE "
        "that writes, no multiple statements.\n"
        "3. Use only table and column names that appear in the lineage JSON.\n"
        "4. Return the SQL inside a ```sql fenced block, then one line "
        "'confidence: <0..1>' stating how sure you are that the mapping is correct, then "
        "at most two sentences explaining the transformation you inverted.\n\n"
        f"Lineage edges:\n{json.dumps(lineage_payload, ensure_ascii=False, sort_keys=True)}\n"
    )
    if sql_text:
        prompt += f"\nArchived ETL SQL (may be truncated):\n{sql_text}\n"
    return prompt, sql_text, truncated


def parse_ai_upstream_response(content: str) -> AiUpstreamResult | None:
    """宽松解析模型回包;拿不到 SQL 围栏就返回 None(判失败,不猜)。"""
    text = content.strip()
    match = _SQL_FENCE.search(text)
    if match is None:
        return None
    sql_template = match.group("sql").strip()
    if not sql_template:
        return None
    remainder = (text[: match.start()] + " " + text[match.end() :]).strip()
    confidence_match = _CONFIDENCE_RE.search(remainder)
    # 模型没报置信度时按下限处理 —— 缺省不等于自信。
    ai_confidence = float(confidence_match.group("value")) if confidence_match else CONFIDENCE_FLOOR
    explanation = _CONFIDENCE_RE.sub("", remainder).strip() or None
    return AiUpstreamResult(
        sql_template=sql_template,
        ai_confidence=max(0.0, min(1.0, ai_confidence)),
        explanation=explanation,
    )


def validate_ai_sql(
    sql_template: str, *, dialect: str, allowed_identifiers: set[str]
) -> str | None:
    """校验模型产出;通过返回 None,否则返回失败原因码。

    四道闸:含占位符 / 能解析 / 单条且是 SELECT / 标识符不越出血缘图。
    任一不过整体判失败 —— 半成品 SQL 比没有更危险。
    """
    if PK_PLACEHOLDER not in sql_template:
        return "missing_pk_placeholder"
    # 占位符不是合法 SQL 记号,校验前换成一个中性字面量再解析。
    probe = sql_template.replace(PK_PLACEHOLDER, "NULL")
    try:
        statements = sqlglot.parse(probe, read=_sqlglot_dialect(dialect))
    except Exception:
        return "unparsable_ai_sql"
    parsed = [stmt for stmt in statements if stmt is not None]
    if len(parsed) != 1:
        return "not_single_statement"
    statement = parsed[0]
    if not isinstance(statement, exp.Select):
        return "not_read_only_select"
    lowered = {name.lower() for name in allowed_identifiers}
    for table in statement.find_all(exp.Table):
        name = table.sql(comments=False).replace('"', "").replace("`", "").strip()
        if name.lower().split(" as ")[0].strip() not in lowered:
            return "identifier_out_of_lineage"
    for column in statement.find_all(exp.Column):
        if column.name and column.name.lower() not in lowered:
            return "identifier_out_of_lineage"
    return None


def fill_pk_placeholder(sql_template: str, *, pk_tuples_sql: str) -> str:
    """把占位符换成本地拼好的字面量元组 —— 值从头到尾没离开过本机。"""
    return sql_template.replace(PK_PLACEHOLDER, pk_tuples_sql)


def combine_confidence(deterministic_min: float, ai_confidence: float) -> float:
    """整体置信度 = 确定性边最小值乘以 AI 自报系数,clamp 到 [0.50, 1.00]。

    自报值只能**下调**(系数 <= 1),模型不能给自己加分 —— LLM 自评分无校准,
    与确定性 confidence 混排会造成假同构。
    """
    factor = max(0.0, min(1.0, float(ai_confidence)))
    return max(CONFIDENCE_FLOOR, min(CONFIDENCE_CEILING, float(deterministic_min) * factor))


def _sqlglot_dialect(dialect: str) -> str | None:
    """方言映射到 sqlglot;未知方言用通用解析器(校验宁松勿崩)。"""
    mapping = {"mysql": "mysql", "postgresql": "postgres", "dm": "oracle", "oracle": "oracle"}
    return mapping.get(dialect.lower())
