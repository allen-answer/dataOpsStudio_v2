from __future__ import annotations

import json
from typing import Any

from app.domain.compare import CompareDiffBucket
from app.domain.schema import Column, ColumnType, Row

COMPARE_BUCKETS: tuple[str, ...] = (
    CompareDiffBucket.ONLY_SOURCE.value,
    CompareDiffBucket.ONLY_TARGET.value,
    CompareDiffBucket.DIFF.value,
    CompareDiffBucket.SAME.value,
)


def empty_bucket_counts() -> dict[str, int]:
    return {bucket: 0 for bucket in COMPARE_BUCKETS}


def compare_result_columns() -> list[Column]:
    return [
        Column(name="pk", type=ColumnType.JSON),
        Column(name="source", type=ColumnType.JSON),
        Column(name="target", type=ColumnType.JSON),
        Column(name="cells", type=ColumnType.JSON),
    ]


def encode_compare_result_row(
    *,
    pk: dict[str, Any],
    source: dict[str, Any] | None,
    target: dict[str, Any] | None,
    cells: list[dict[str, Any]],
) -> Row:
    return Row(
        values=[
            _json_dumps(pk),
            _json_dumps(source),
            _json_dumps(target),
            _json_dumps(cells),
        ]
    )


def decode_compare_result_row(row: Row) -> dict[str, Any]:
    values = list(row.values)
    if len(values) != 4:
        raise ValueError("compare result row must have exactly four values")
    return {
        "pk": _json_loads(values[0]) or {},
        "source": _json_loads(values[1]),
        "target": _json_loads(values[2]),
        "cells": _json_loads(values[3]) or [],
    }


def compare_export_schema(decoded: dict[str, Any]) -> tuple[list[str], list[str]]:
    """从一条解码后的 compare 结果行推断导出列 schema。

    整个 run 的 key 列 / 值列在四桶间一致(compare_run 固定 key/value 列),
    故任意非空桶首行即可得到全 run 的 schema(空桶据此仍能产出正确表头)。
    """
    pk = decoded.get("pk") or {}
    source = decoded.get("source") or {}
    target = decoded.get("target") or {}
    key_columns = list(pk.keys())
    # 并集(保序):只出现在某一侧的列也不漏;diff/same 两侧同集时等价。
    value_columns = list(dict.fromkeys([*source.keys(), *target.keys()]))
    return key_columns, value_columns


def compare_export_columns(
    bucket: str,
    key_columns: list[str],
    value_columns: list[str],
) -> list[Column]:
    """构造某桶导出 sheet 的表头(解码业务列,非原始 4 列 JSON blob)。

    diff 桶:每个值列拆成 `<col> (source)` / `<col> (target)` 并排,与前端"上下分裂"
    展示同口径(两侧值都可见);其余桶单列。
    """
    columns = [Column(name=name) for name in key_columns]
    if bucket == CompareDiffBucket.DIFF.value:
        for name in value_columns:
            columns.append(Column(name=f"{name} (source)"))
            columns.append(Column(name=f"{name} (target)"))
    else:
        columns.extend(Column(name=name) for name in value_columns)
    return columns


def compare_export_row(
    bucket: str,
    decoded: dict[str, Any],
    key_columns: list[str],
    value_columns: list[str],
) -> Row:
    """把一条解码行摊平成导出行值,列序与 `compare_export_columns` 对齐。

    - only_source / same:取 source 侧值(same 两侧相等)
    - only_target:取 target 侧值
    - diff:每列输出 source、target 两格并排
    """
    pk = decoded.get("pk") or {}
    source = decoded.get("source") or {}
    target = decoded.get("target") or {}
    values: list[object] = [_export_scalar(pk.get(name)) for name in key_columns]
    if bucket == CompareDiffBucket.DIFF.value:
        for name in value_columns:
            values.append(_export_scalar(source.get(name)))
            values.append(_export_scalar(target.get(name)))
    elif bucket == CompareDiffBucket.ONLY_TARGET.value:
        values.extend(_export_scalar(target.get(name)) for name in value_columns)
    else:  # only_source / same:优先 source,空则回退 target
        side = source if source else target
        values.extend(_export_scalar(side.get(name)) for name in value_columns)
    return Row(values=values)


def _export_scalar(value: Any) -> object:
    """保留标量(数值列在 xlsx 里仍是数值),复杂值(嵌套 dict/list)压成紧凑 JSON 串。

    公式注入防御由导出写入层(`_xlsx_cell_xml` → `sanitize_formula_text`)统一兜底,
    此处不重复转义。
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return _json_dumps(value)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _json_loads(value: object) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return json.loads(value)
    return value


__all__ = [
    "COMPARE_BUCKETS",
    "compare_export_columns",
    "compare_export_row",
    "compare_export_schema",
    "compare_result_columns",
    "decode_compare_result_row",
    "empty_bucket_counts",
    "encode_compare_result_row",
]
