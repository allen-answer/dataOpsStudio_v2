"""L-7 血缘报告 AI 解读:构造送往 AI 的结构化画像(纯函数,无 DB / 无网络)。

差距矩阵 L-7:对整份血缘报告做 AI 解读(风险点 / 刷新模式解释 / 建议)。**严格只增
不改** —— 本模块只把已有的确定性报告(L-4 语义视图 + LineageReport 统计)投影成一份
**只含结构与统计**的画像,绝不产生新的确定性结论。

出站边界(设计稿 §2.7 egress 分级,对齐 Compare `profile_for_ai` / Lineage 兜底先例):
- 只出**表名 / 列名 / 角色 / 刷新方式 / 计数 / 人话观察 / 风险**(schema 级,L2)。
- **绝不出行值、绝不出 SQL 原文全文**;parse_errors 只出计数,不出可能夹带 SQL 片段的
  原始报错文本。
"""

from __future__ import annotations

from typing import Any

from app.domain.lineage.models import LineageReport, SemanticView

# 送 AI 的画像上限,约束 token 用量(与兜底 MAX_* 同思路)。
MAX_TARGETS = 60
MAX_TABLE_ROLES = 120
MAX_EDGES = 200
MAX_OBSERVATIONS = 40
MAX_RISKS = 40
MAX_SOURCE_TABLES = 30

__all__ = [
    "MAX_EDGES",
    "MAX_OBSERVATIONS",
    "MAX_RISKS",
    "MAX_TABLE_ROLES",
    "MAX_TARGETS",
    "build_enrichment_profile",
]


def _table_edges(report: LineageReport) -> list[dict[str, str]]:
    """从 graph_edges 抽取表级边(source→target),只保留表名(L2)。"""
    edges: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for edge in report.graph_edges:
        source = edge.get("source_table")
        target = edge.get("target_table")
        if not isinstance(source, str) or not isinstance(target, str):
            continue
        if not source or not target:
            continue
        key = (source, target)
        if key in seen:
            continue
        seen.add(key)
        item: dict[str, str] = {"source_table": source, "target_table": target}
        edge_type = edge.get("edge_type")
        if isinstance(edge_type, str) and edge_type:
            item["edge_type"] = edge_type
        edges.append(item)
        if len(edges) >= MAX_EDGES:
            break
    return edges


def build_enrichment_profile(view: SemanticView, report: LineageReport) -> dict[str, Any]:
    """把语义视图 + 报告统计投影成 L2 安全的 AI 输入画像(纯结构与统计)。

    输出只含表名 / 角色 / 刷新方式 / 计数 / 人话观察 / 风险 —— 无行值、无 SQL 原文。
    """
    targets = [
        {
            "table": target.table,
            "primary_role": target.primary_role,
            "roles": list(target.roles),
            "refresh_mode": target.refresh_mode,
            "counts": dict(target.counts),
            "source_tables": list(target.source_tables[:MAX_SOURCE_TABLES]),
        }
        for target in view.targets[:MAX_TARGETS]
    ]
    table_roles = [
        {
            "table": role.table,
            "primary_role": role.primary_role,
            "roles": list(role.roles),
        }
        for role in view.table_roles[:MAX_TABLE_ROLES]
    ]
    risks = [
        {"level": risk.level, "type": risk.type, "message": risk.message}
        for risk in view.risks[:MAX_RISKS]
    ]
    return {
        "stats": {
            "statement_count": report.statement_count,
            "table_edge_count": len(report.graph_edges),
            "column_mapping_count": len(report.insert_mappings),
            "parse_error_count": len(report.parse_errors),
            "dynamic_sql_count": report.dynamic_sql_count,
        },
        "targets": targets,
        "table_roles": table_roles,
        "edges": _table_edges(report),
        "observations": list(view.observations[:MAX_OBSERVATIONS]),
        "risks": risks,
    }
