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
    "compare_result_columns",
    "decode_compare_result_row",
    "empty_bucket_counts",
    "encode_compare_result_row",
]
