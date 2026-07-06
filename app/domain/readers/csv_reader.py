"""CSV / TSV 行读取器(C-1 PR1)。

- stdlib `csv`,**不引 pandas**(移植 1.x 取舍)。
- 编码:默认 `utf-8-sig`(自动剥 BOM);解码失败自动回退 `gbk`
  (国内手工台账常见 GB 系编码,设计稿 §一.2 GBK 回退)。
- `delimiter` / `quotechar` / `header_row`(1-indexed)可配。
- 值全为 str(空串保留;是否当 None 由对比内核 `CompareRules.empty_as_null` 决定)。
- 逐行流式;行长不齐由 `row_to_dict` 补 None / 截尾。

纯 domain,零 DB 依赖(R1)。
"""

from __future__ import annotations

import codecs
import csv
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from app.domain.readers.base import (
    ReaderError,
    RowReader,
    build_columns,
    is_blank_row,
    row_to_dict,
)

# 解码回退顺序里的 GB 系编码(已是 GB 系则不再追加回退)。
_GB_ENCODINGS = frozenset({"gbk", "gb2312", "gb18030", "gb1030"})
_DECODE_CHUNK = 65536


class CsvReader(RowReader):
    def __init__(
        self,
        path: str | Path,
        *,
        encoding: str = "utf-8-sig",
        delimiter: str = ",",
        quotechar: str = '"',
        header_row: int = 1,
    ) -> None:
        if header_row < 1:
            raise ValueError("header_row must be >= 1 (1-indexed)")
        self._path = Path(path)
        self._encoding = encoding
        self._delimiter = delimiter
        self._quotechar = quotechar
        self._header_row = header_row
        self._resolved_encoding: str | None = None

    @property
    def resolved_encoding(self) -> str:
        """实际生效的编码(触发一次探测;供预览层回显 detected_encoding)。"""

        if self._resolved_encoding is None:
            self._resolved_encoding = self._pick_encoding()
        return self._resolved_encoding

    def _pick_encoding(self) -> str:
        """分块增量解码探测:优先配置编码,失败回退 gbk。内存有界。"""

        candidates = [self._encoding]
        if self._encoding.lower().replace("-", "") not in _GB_ENCODINGS:
            candidates.append("gbk")
        last_error: UnicodeDecodeError | None = None
        for candidate in candidates:
            decoder = codecs.getincrementaldecoder(candidate)()
            try:
                with self._path.open("rb") as handle:
                    while True:
                        chunk = handle.read(_DECODE_CHUNK)
                        if not chunk:
                            break
                        decoder.decode(chunk)
                    decoder.decode(b"", final=True)
                return candidate
            except UnicodeDecodeError as error:
                last_error = error
                continue
        raise ReaderError(f"could not decode CSV file with encodings {candidates}") from last_error

    def _rows(self) -> Iterator[list[str]]:
        with self._path.open("r", encoding=self.resolved_encoding, newline="") as handle:
            reader = csv.reader(handle, delimiter=self._delimiter, quotechar=self._quotechar)
            yield from reader

    def columns(self) -> list[str]:
        names, _ = self._header()
        return names

    def _header(self) -> tuple[list[str], list[int]]:
        for index, raw in enumerate(self._rows(), start=1):
            if index == self._header_row:
                return build_columns(raw)
        # 空文件或 header_row 超出实际行数 → 无表头。
        return [], []

    def iter_rows(self) -> Iterator[dict[str, Any]]:
        columns, kept_indices = self._header()
        if not columns:
            return
        for index, raw in enumerate(self._rows(), start=1):
            if index <= self._header_row:
                continue
            if is_blank_row(raw):
                continue
            yield row_to_dict(columns, kept_indices, raw)


def list_columns(
    path: str | Path,
    *,
    encoding: str = "utf-8-sig",
    delimiter: str = ",",
    header_row: int = 1,
) -> list[str]:
    """给"选主键 / 列映射"UI 用:只读表头返回列名。"""

    return CsvReader(
        path,
        encoding=encoding,
        delimiter=delimiter,
        header_row=header_row,
    ).columns()


__all__ = ["CsvReader", "list_columns"]
