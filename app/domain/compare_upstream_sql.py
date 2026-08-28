"""定位 SQL 与血缘反推的联动:把差异行主键沿列级血缘上游链逐跳映射成上游表的定位 SQL。

现有 UX-2 C-3(``compare_diff_sql``)只能定位到对比表自身;本模块回答下一个问题——
"这批差异行在**上游源表**里长什么样"。做法是纯谓词映射:主键列顺着列级血缘边找到
上游对应列,把 ``pk IN (...)`` 原样搬到上游表上。**只生成文本,不执行**(与 C-3 同口径)。

★ 纯 domain:零 IO、不 import 数据库驱动(R1)。血缘边由 API 层查好传进来。

设计裁决(设计稿 D4 / 架构画板):

- **逐边可反推性分级**:只有 ``DIRECT`` 边无条件放行;``CAST`` 放行但标风险
  (值表示可能变,多数库隐式转换仍能命中);``EXPRESSION`` / ``AGGREGATION`` /
  ``TRANSFORMATION`` 与所有 ``INDIRECT``(FILTER / JOIN)边判为**不可反推**,链在此终止。
- **inferred 边默认不用**:未经人工确认的 AI 推断边与确定性边不同权重(PRD 14.7);
  ``include_inferred=True`` 才纳入,并标 ``inferred_edge_used`` 风险点。
- **每跳每上游表一条独立 SELECT,不生成跨跳 JOIN**:上游表可能物理在另一套库,
  跨库 JOIN 是一条跑不了的 SQL。字面量已内联,``IN`` 即完备,不需要 EXISTS。
- **复合主键部分可追**:能追到的子集照样生成(结果是**超集**,对定位仍有用),标
  ``partial_key`` 并列出丢失列;子集元组去重(丢列后必然重复)。
- **置信度取路径上各边 confidence 的最小值**(weakest link),不连乘:边 confidence
  量纲混杂(确定性边恒 1.0、inferred 边是启发式打分),连乘会造出"7 跳各 0.95 连乘得 0.70"
  式的假精度。整体 clamp 到 [0.50, 1.00],展示为 50%-100%(设计稿 Q3)。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from app.dbclients.sql_build import quote_identifier, sql_literal
from app.domain.datasource import DbType

__all__ = [
    "CONFIDENCE_CEILING",
    "CONFIDENCE_FLOOR",
    "INVERTIBLE_SUBTYPES",
    "MAX_UPSTREAM_DEPTH",
    "UpstreamEdge",
    "UpstreamHop",
    "build_upstream_hops",
    "dedupe_pk_rows",
    "hop_risks",
    "render_upstream_sql",
    "upstream_header_comment",
]

# 置信度下限:再不确定也不给"0%"这种既不可解释、又暗示"完全没用"的数字(设计稿 Q3)。
CONFIDENCE_FLOOR = 0.50
CONFIDENCE_CEILING = 1.00

# 可反推的列变换:值原样传递(DIRECT)或仅表示形式变化(CAST)。
INVERTIBLE_SUBTYPES = frozenset({"DIRECT", "CAST"})
# 反推链最长跳数:与 C-8 逐跳对比同一量级,防呆而非性能考量。
MAX_UPSTREAM_DEPTH = 5

_BLOCK_REASONS = frozenset({"rejected_edge", "unconfirmed_inferred_edge"})


@dataclass(frozen=True)
class UpstreamEdge:
    """一条列级血缘边(API 层从 lineage_column_edges 投影而来)。"""

    edge_id: str
    run_id: str
    source_table: str
    source_column: str
    target_table: str
    target_column: str
    transformation: str
    transformation_subtype: str
    inference_status: str
    confidence: float


@dataclass(frozen=True)
class UpstreamHop:
    """某一跳、某张上游表的反推结果(可用则带 SQL 所需的列映射)。"""

    depth: int
    table: str
    # 上游列名,与传入 key_columns 一一对应;追不到的位置为 None(partial_key)
    key_columns: list[str | None]
    available: bool
    reason: str | None = None
    warnings: list[str] = field(default_factory=list)
    confidence: float = CONFIDENCE_CEILING
    edges: list[UpstreamEdge] = field(default_factory=list)
    # 该跳断链时,最先挡住的变换子类型(给用户看"为什么到此为止")
    blocked_by: str | None = None

    @property
    def resolved_columns(self) -> list[str]:
        return [column for column in self.key_columns if column is not None]


def _edge_usable(edge: UpstreamEdge, *, include_inferred: bool) -> tuple[bool, str | None]:
    """返回 (可用, 不可用原因)。原因码与 CompareDiffSqlSide.reason 同风格。"""
    if edge.inference_status == "rejected":
        return False, "rejected_edge"
    if edge.inference_status == "inferred" and not include_inferred:
        return False, "unconfirmed_inferred_edge"
    if edge.transformation != "DIRECT":
        # INDIRECT(FILTER / JOIN):谓词影响,不是值传递
        return False, "non_invertible_transformation"
    if edge.transformation_subtype not in INVERTIBLE_SUBTYPES:
        return False, "non_invertible_transformation"
    return True, None


def _clamp(value: float) -> float:
    return max(CONFIDENCE_FLOOR, min(CONFIDENCE_CEILING, float(value)))


def build_upstream_hops(
    *,
    focus_table: str,
    key_columns: Sequence[str],
    edges: Sequence[UpstreamEdge],
    include_inferred: bool = False,
    max_depth: int = MAX_UPSTREAM_DEPTH,
) -> list[UpstreamHop]:
    """沿列级血缘上游逐跳映射主键列,每跳每上游表产出一个 :class:`UpstreamHop`。

    ``edges`` 是候选边全集(API 层已按项目 / 生效 run 过滤);本函数只做纯粹的
    图行走与判定。同一列有多个上游(UNION / 多脚本汇入)时**每张上游表独立成条**,
    不合并——它们是不同的物理表,SQL 也必须分开。
    """
    if not key_columns:
        return []
    depth_limit = max(1, min(max_depth, MAX_UPSTREAM_DEPTH))
    by_target: dict[tuple[str, str], list[UpstreamEdge]] = {}
    for edge in edges:
        by_target.setdefault((edge.target_table, edge.target_column), []).append(edge)

    hops: list[UpstreamHop] = []
    frontier: dict[str, list[str | None]] = {focus_table: [str(col) for col in key_columns]}
    visited: set[str] = {focus_table}

    for depth in range(1, depth_limit + 1):
        next_frontier: dict[str, list[str | None]] = {}
        for table, columns in frontier.items():
            resolved: dict[str, list[str | None]] = {}
            table_edges: dict[str, list[UpstreamEdge]] = {}
            blocked: dict[str, str] = {}
            for index, column in enumerate(columns):
                if column is None:
                    continue
                for edge in by_target.get((table, column), []):
                    usable, reason = _edge_usable(edge, include_inferred=include_inferred)
                    if not usable:
                        if reason is not None:
                            blocked.setdefault(
                                edge.source_table,
                                reason if reason in _BLOCK_REASONS else edge.transformation_subtype,
                            )
                        continue
                    slot = resolved.setdefault(edge.source_table, [None] * len(columns))
                    # 同一位置多条边(同表多路径)取第一条:都是等价定位,不做笛卡尔
                    # 展开(会把 SQL 炸开且无增量信息)。
                    if slot[index] is None:
                        slot[index] = edge.source_column
                        table_edges.setdefault(edge.source_table, []).append(edge)
            for upstream_table, slot in resolved.items():
                hop_edges = table_edges.get(upstream_table, [])
                warnings: list[str] = []
                if any(edge.transformation_subtype == "CAST" for edge in hop_edges):
                    warnings.append("cast_value_mismatch_risk")
                if any(edge.inference_status == "inferred" for edge in hop_edges):
                    warnings.append("inferred_edge_used")
                if any(value is None for value in slot):
                    warnings.append("partial_key")
                hops.append(
                    UpstreamHop(
                        depth=depth,
                        table=upstream_table,
                        key_columns=list(slot),
                        available=True,
                        warnings=warnings,
                        confidence=_clamp(min(edge.confidence for edge in hop_edges)),
                        edges=hop_edges,
                    )
                )
                if upstream_table not in visited:
                    next_frontier[upstream_table] = list(slot)
                    visited.add(upstream_table)
            # 一个可反推上游都没有:把"为什么断在这"如实报出来
            if not resolved:
                for upstream_table, subtype in blocked.items():
                    hops.append(
                        UpstreamHop(
                            depth=depth,
                            table=upstream_table,
                            key_columns=[None] * len(columns),
                            available=False,
                            reason=subtype
                            if subtype in _BLOCK_REASONS
                            else "non_invertible_transformation",
                            blocked_by=subtype,
                        )
                    )
        if not next_frontier:
            break
        frontier = next_frontier
    return hops


def render_upstream_sql(
    db_type: DbType,
    *,
    table: str,
    columns: Sequence[str],
    pk_rows: Sequence[Sequence[object]],
) -> str | None:
    """生成上游表的定位 SQL;列 / 行为空或标识符非法返回 None。

    与 ``compare_diff_sql.build_diff_row_select`` 同一构造口径(方言引用 + 字面量转义),
    区别只在表达式来自血缘边而非 CompareDataRef。
    """
    if not columns or not pk_rows:
        return None
    try:
        quoted_table = quote_identifier(db_type, str(table))
        quoted_cols = [quote_identifier(db_type, str(column)) for column in columns]
    except ValueError:
        return None
    if len(quoted_cols) == 1:
        values = ", ".join(sql_literal(row[0]) for row in pk_rows)
        where = f"{quoted_cols[0]} IN ({values})"
    else:
        tuples = ", ".join(
            "(" + ", ".join(sql_literal(value) for value in row) + ")" for row in pk_rows
        )
        where = f"({', '.join(quoted_cols)}) IN ({tuples})"
    return f"SELECT * FROM {quoted_table} WHERE {where}"


def dedupe_pk_rows(rows: Sequence[Sequence[object]]) -> list[list[object]]:
    """丢列之后必然出现重复元组(partial_key),去重但保持原顺序。"""
    seen: set[tuple[object, ...]] = set()
    deduped: list[list[object]] = []
    for row in rows:
        key = tuple(row)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(list(row))
    return deduped


def hop_risks(hop: UpstreamHop, *, missing_columns: Sequence[str]) -> list[str]:
    """把结构性警告翻译成要用户确认的风险点码(前端按码取 i18n 文案)。

    ``pk_name_stability_assumed`` 固定列出:凡生成即成立(与 C-8"主键跨链稳定"
    同一假设边界),不能因为它总在就省略——它恰恰是最容易被忽略的那条。
    """
    risks: list[str] = []
    for warning in hop.warnings:
        if warning == "partial_key" and missing_columns:
            risks.append(f"partial_key:{','.join(missing_columns)}")
        else:
            risks.append(warning)
    risks.append("pk_name_stability_assumed")
    return risks


def upstream_header_comment(
    hop: UpstreamHop,
    *,
    lineage_run_id: str | None,
    risks: Sequence[str],
) -> str:
    """置信度与风险点写进 SQL 头部注释 —— 用户复制走时一并带着(设计稿 D4)。"""
    lines = [f"-- 血缘溯源 · run {lineage_run_id or 'unknown'} · hop{hop.depth} · {hop.table}"]
    for edge in hop.edges:
        suffix = "  (AI 推断,未确认)" if edge.inference_status == "inferred" else ""
        lines.append(
            f"--   {edge.target_table}.{edge.target_column} <- "
            f"{edge.source_table}.{edge.source_column}  "
            f"{edge.transformation_subtype}  置信度 {round(edge.confidence * 100)}%{suffix}"
        )
    lines.append(
        f"-- 整体置信度 {round(_clamp(hop.confidence) * 100)}%"
        f"(clamp {round(CONFIDENCE_FLOOR * 100)}%-{round(CONFIDENCE_CEILING * 100)}%)"
        f" · 风险点 {len(risks)} 项 · 只读 SELECT · 平台不执行"
    )
    for risk in risks:
        lines.append(f"--   风险:{risk}")
    return "\n".join(lines)
