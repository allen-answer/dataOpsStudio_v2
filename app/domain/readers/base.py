"""行读取器统一契约(C-1 文件源对比 PR1,设计稿 §一 / §四.2)。

所有文件源(CSV / Excel / …)适配到同一 **dict-行** 形状喂给对比内核,
下游内核对源类型无感(设计稿 §一.1)。本层为**纯 domain**:只依赖
stdlib / openpyxl,**绝不 import 任何 DB 驱动**(红线 R1)。

- `columns()`:规范化后的表头(去重 uniquify,丢弃空表头列)。
- `iter_rows()`:逐行流式产出 `dict[str, Any]`。
- `fetch_all(max_rows=...)`:物化为 list,超 `max_rows` 硬顶直接 raise(防 OOM)。

表头去重逻辑移植自 1.x `compare_schema.uniquify_columns`(大小写不敏感去重,
重名后缀 `__2/__3`),防 `dict(zip)` 同名列互相覆盖。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from typing import Any


class ReaderError(RuntimeError):
    """行读取器失败基类(文件缺失 / 解码失败 / 超限等)。"""


class MaxRowsExceededError(ReaderError):
    """物化行数超过 `max_rows` 硬顶(移植 1.x 硬顶语义,防大文件 OOM)。"""

    def __init__(self, max_rows: int) -> None:
        super().__init__(f"row count exceeds max_rows limit ({max_rows})")
        self.max_rows = max_rows


def normalize_column_name(name: Any) -> str:
    """表头单元格 → 规范列名:None 归空串,其余转 str 并去首尾空白。"""

    if name is None:
        return ""
    return str(name).strip()


def uniquify_columns(names: Sequence[str]) -> list[str]:
    """大小写不敏感去重:首现原样,重名依次加 `__2` / `__3` 后缀。

    生成的后缀名若又与既有列冲突则继续递增,保证结果全局唯一
    (防 `dict(zip(columns, values))` 静默覆盖)。
    """

    result: list[str] = []
    seen: set[str] = set()  # 已产出列名的小写形式
    counts: dict[str, int] = {}  # 规范名小写 → 出现次数
    for raw in names:
        key = raw.lower()
        counts[key] = counts.get(key, 0) + 1
        candidate = raw if counts[key] == 1 else f"{raw}__{counts[key]}"
        while candidate.lower() in seen:
            counts[key] += 1
            candidate = f"{raw}__{counts[key]}"
        seen.add(candidate.lower())
        result.append(candidate)
    return result


def build_columns(raw_headers: Sequence[Any]) -> tuple[list[str], list[int]]:
    """从原始表头行构造 (列名, 保留列的原始下标)。

    空表头(None / 空白)列被丢弃(移植 1.x Excel "丢未命名列";CSV 对齐同一行为),
    其对应下标不进 `kept_indices`,故数据行取值时自然跳过这些列。
    """

    kept_indices = [idx for idx, cell in enumerate(raw_headers) if normalize_column_name(cell)]
    names = uniquify_columns([normalize_column_name(raw_headers[idx]) for idx in kept_indices])
    return names, kept_indices


def row_to_dict(
    columns: Sequence[str],
    kept_indices: Sequence[int],
    raw_row: Sequence[Any],
) -> dict[str, Any]:
    """按保留下标把原始行映射为 dict;行短于表头的缺失位补 None,超长部分忽略。"""

    return {
        column: (raw_row[idx] if idx < len(raw_row) else None)
        for column, idx in zip(columns, kept_indices, strict=True)
    }


def is_blank_row(values: Sequence[Any]) -> bool:
    """判定"全空行":所有值为 None 或空白串(移植 1.x "跳全空行")。"""

    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and value.strip() == "":
            continue
        return False
    return True


class RowReader(ABC):
    """文件源统一读取契约(设计稿 §四.2 收敛到 2.0 所需最小面)。"""

    @abstractmethod
    def columns(self) -> list[str]:
        """规范化、去重后的列名列表(空表头列已剔除)。"""

    @abstractmethod
    def iter_rows(self) -> Iterator[dict[str, Any]]:
        """逐行流式产出 `{列名: 值}`;跳过全空行。"""

    def fetch_all(self, *, max_rows: int | None = None) -> list[dict[str, Any]]:
        """物化所有行。给定 `max_rows` 时,超出硬顶立即 raise(不静默截断)。"""

        rows: list[dict[str, Any]] = []
        for index, row in enumerate(self.iter_rows()):
            if max_rows is not None and index >= max_rows:
                raise MaxRowsExceededError(max_rows)
            rows.append(row)
        return rows


__all__ = [
    "MaxRowsExceededError",
    "ReaderError",
    "RowReader",
    "build_columns",
    "is_blank_row",
    "normalize_column_name",
    "row_to_dict",
    "uniquify_columns",
]
