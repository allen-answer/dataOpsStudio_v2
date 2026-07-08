"""AI Copilot C2 —— Compare 残余列映射学习纯逻辑(设计稿 §2.7.4,egress L2 + 可选 L4)。

★ 本模块是 **纯 domain**:不碰 DB / 网络 / gateway,只做残余列拆分 / 历史聚合 /
上下文组装 / prompt 构造 / 响应解析。这样截断策略、prompt 形状、样本投影可离线
单测,route 层只负责取缓存 / 拉样本 / 调 gateway。

egress 分层(§2.7.5):
- L2:残余列 schema(表名/列名/类型/可空/主键/注释)+ 历史映射决策(源→目标名 + 次数)。
  **绝不含行值**。
- L4:样本行值——高风险,route 层只在 include_samples 且 gateway 配置允许 L4 时
  才拉取并作为独立 L4 ContextItem 送出;本模块只提供投影/截断(project_sample_rows),
  不决定是否发送。

C2 是"读历史"型(设计稿 §2.7.4 排期依据):历史为空时 **如实告知 AI 无历史**,
不臆造(build_map_suggest_prompt 的 has_history 分支)。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.domain.schema import Column

# 截断上限:控制 prompt token + egress 面。宁可少送也绝不越界。
MAX_RESIDUAL_COLUMNS = 80
MAX_HISTORY_PAIRS = 40
MAX_SAMPLE_ROWS = 20  # 设计稿 §2.7.4:≤20 行样本
MAX_SAMPLE_CELL_LEN = 120  # 单元格值截断,减少意外过量出站


def split_residual_columns(
    source_columns: list[Column],
    target_columns: list[Column],
    confirmed_mappings: dict[str, str],
) -> tuple[list[Column], list[Column], bool]:
    """从两侧全列中剔除已确认映射列,得到"残余未映射列"(设计稿 §2.7.4)。

    比较大小写不敏感(不同库标识符大小写策略不同)。返回
    (residual_source, residual_target, truncated);任一侧超 MAX_RESIDUAL_COLUMNS
    则截断并置 truncated=True。C2 只管规则推断后的残余列,不抢规则的活。
    """
    mapped_sources = {k.lower() for k in confirmed_mappings}
    mapped_targets = {v.lower() for v in confirmed_mappings.values()}
    residual_source = [c for c in source_columns if c.name.lower() not in mapped_sources]
    residual_target = [c for c in target_columns if c.name.lower() not in mapped_targets]
    truncated = (
        len(residual_source) > MAX_RESIDUAL_COLUMNS or len(residual_target) > MAX_RESIDUAL_COLUMNS
    )
    return (
        residual_source[:MAX_RESIDUAL_COLUMNS],
        residual_target[:MAX_RESIDUAL_COLUMNS],
        truncated,
    )


def column_schema_payload(columns: list[Column]) -> list[dict[str, Any]]:
    """把残余列压成 L2 结构 JSON。★ 只取结构字段,物理上无从取到行值。"""
    return [
        {
            "name": col.name,
            "type": col.type.value,
            "nullable": col.nullable,
            "primary_key": col.primary_key,
            "comment": col.comment,
        }
        for col in columns
    ]


def aggregate_mapping_history(task_mappings: list[dict[str, str]]) -> list[dict[str, Any]]:
    """从既有 compare 任务的已确认 column_mappings 聚合历史映射决策(L2)。

    输入:各任务 compare_rules.column_mappings(源列名 → 目标列名)的列表。
    输出:去重后的 (source_column, target_column, times_confirmed) 列表,按确认次数
    降序,截断到 MAX_HISTORY_PAIRS。为空时返回 [](route 层据此如实告知 AI 无历史)。
    """
    counter: dict[tuple[str, str], int] = {}
    for mapping in task_mappings:
        for src, tgt in mapping.items():
            if not src or not tgt:
                continue
            counter[(src, tgt)] = counter.get((src, tgt), 0) + 1
    pairs = sorted(
        counter.items(),
        key=lambda entry: (-entry[1], entry[0][0].lower(), entry[0][1].lower()),
    )
    return [
        {"source_column": src, "target_column": tgt, "times_confirmed": count}
        for (src, tgt), count in pairs[:MAX_HISTORY_PAIRS]
    ]


def project_sample_rows(
    columns: list[str],
    rows: list[list[Any]],
    residual_names: list[str],
    *,
    max_rows: int = MAX_SAMPLE_ROWS,
    max_cell_len: int = MAX_SAMPLE_CELL_LEN,
) -> dict[str, Any]:
    """把一侧样本裁到仅残余列 + ≤max_rows 行 + 单元格截断(L4 投影)。

    只保留残余列(减少 L4 出站面),不含主键/已映射列;行数与单元格长度都封顶。
    纯投影,不决定是否发送(那是 route 层按 egress 配置的事)。
    """
    residual_lower = {name.lower() for name in residual_names}
    keep_idx = [idx for idx, name in enumerate(columns) if name.lower() in residual_lower]
    keep_cols = [columns[idx] for idx in keep_idx]
    out_rows: list[list[Any]] = []
    for row in rows[:max_rows]:
        out_rows.append(
            [_truncate_cell(row[idx], max_cell_len) if idx < len(row) else None for idx in keep_idx]
        )
    return {"columns": keep_cols, "rows": out_rows}


def _truncate_cell(value: Any, max_len: int) -> Any:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= max_len else text[:max_len]


def build_map_suggest_prompt(
    *,
    source_dialect: str,
    target_dialect: str,
    has_history: bool,
    include_samples: bool,
) -> str:
    """C2 生成指令。约束:只映射给定残余列、不臆造列、历史为空时不假装有历史。"""
    if has_history:
        history_line = (
            "Historical confirmed mappings from prior compare tasks are provided; "
            "weigh them as prior decisions but do not blindly copy."
        )
    else:
        extra = " and sample values" if include_samples else ""
        history_line = (
            "No historical mapping decisions are available. Rely on column names, types"
            f"{extra} only, and do not fabricate history."
        )
    samples_line = (
        "Sample row values (<=20 rows per side) are provided to help match columns by data shape."
        if include_samples
        else "No sample values are provided; use column names and types only."
    )
    return (
        "You map residual (not-yet-mapped) columns between a source and a target "
        "database table for a data compare task. "
        f"Source dialect: {source_dialect}. Target dialect: {target_dialect}. "
        "Only propose mappings between the residual source columns and residual target "
        "columns given in the context. Never invent columns that are not listed. "
        "Each source column maps to at most one target column. "
        f"{history_line} {samples_line} "
        "Return strict JSON only, no prose: "
        '{"suggestions": [{"source_column": string, "target_column": string, '
        '"confidence": number, "rationale": string}]}. '
        "confidence is between 0 and 1. Return an empty list when no confident mapping exists."
    )


@dataclass(frozen=True)
class MappingSuggestion:
    source_column: str
    target_column: str
    confidence: float
    rationale: str | None


def parse_map_suggestions(
    content: str,
    *,
    valid_sources: set[str],
    valid_targets: set[str],
) -> list[MappingSuggestion]:
    """宽松解析 provider 响应为映射建议列表。

    - 容忍 ```json 围栏 / 前后散文;取第一个可解析的 JSON 数组/对象。
    - 只接受 source_column ∈ 残余源列 且 target_column ∈ 残余目标列 的建议
      (对齐 lineage 兜底"绝不臆造未出现的列"),按源列去重。
    - 完全解析不出(如 MockProvider 回 "ok")→ 返回 [](route 层 ok=True 空建议)。
    """
    data = _loose_json(content)
    if isinstance(data, dict):
        data = data.get("suggestions")
    if not isinstance(data, list):
        return []
    valid_src = {name.lower() for name in valid_sources}
    valid_tgt = {name.lower() for name in valid_targets}
    out: list[MappingSuggestion] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        src = item.get("source_column")
        tgt = item.get("target_column")
        if not isinstance(src, str) or not isinstance(tgt, str):
            continue
        if src.lower() not in valid_src or tgt.lower() not in valid_tgt:
            continue  # 绝不臆造未在残余列中的列
        if src.lower() in seen:
            continue
        seen.add(src.lower())
        confidence = _coerce_confidence(item.get("confidence"))
        rationale = item.get("rationale") or item.get("reason")
        out.append(
            MappingSuggestion(
                source_column=src,
                target_column=tgt,
                confidence=confidence,
                rationale=rationale if isinstance(rationale, str) and rationale else None,
            )
        )
    return out


def _coerce_confidence(raw: Any) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return round(max(0.0, min(1.0, value)), 3)


_JSON_FENCE = re.compile(r"```(?:json)?\s*(?P<body>.+?)```", re.IGNORECASE | re.DOTALL)


def _loose_json(content: str) -> Any:
    text = content.strip()
    fence = _JSON_FENCE.search(text)
    if fence is not None:
        text = fence.group("body").strip()
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass
    for open_char, close_char in (("[", "]"), ("{", "}")):
        start = text.find(open_char)
        end = text.rfind(close_char)
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except (ValueError, TypeError):
                continue
    return None
