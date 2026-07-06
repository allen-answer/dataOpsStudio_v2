"""C-1 文件源对比 PR1 — readers domain 层单测。

用真实小文件字节:CSV 直接写字符串,Excel 用 openpyxl 现造 .xlsx。
不 mock reader 本体(设计稿 PR1:纯样例文件 → 行/列/sheet/编码/表头)。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from openpyxl import Workbook

from app.domain.readers import (
    CsvReader,
    ExcelReader,
    MaxRowsExceededError,
    ReaderError,
    csv_list_columns,
    excel_list_columns,
    list_sheets,
    uniquify_columns,
)
from app.domain.readers.base import build_columns, is_blank_row, normalize_column_name

# ─────────────────────────── helpers ───────────────────────────


def _write_csv(
    tmp_path: Path, text: str, *, name: str = "data.csv", encoding: str = "utf-8"
) -> Path:
    path = tmp_path / name
    path.write_bytes(text.encode(encoding))
    return path


def _write_xlsx(
    tmp_path: Path, sheets: dict[str, list[list[object]]], *, name: str = "data.xlsx"
) -> Path:
    workbook = Workbook()
    # 删掉默认空 sheet,按传入顺序建。
    default = workbook.active
    workbook.remove(default)
    for sheet_name, rows in sheets.items():
        worksheet = workbook.create_sheet(title=sheet_name)
        for row in rows:
            worksheet.append(row)
    path = tmp_path / name
    workbook.save(path)
    return path


# ─────────────────────── base helpers ───────────────────────


def test_uniquify_columns_case_insensitive_suffix() -> None:
    assert uniquify_columns(["id", "Name", "name", "NAME"]) == ["id", "Name", "name__2", "NAME__3"]


def test_uniquify_columns_avoids_generated_collision() -> None:
    # 生成的 "a__2" 已被真实列占用 → 继续递增。
    assert uniquify_columns(["a", "a__2", "a"]) == ["a", "a__2", "a__3"]


def test_normalize_column_name_handles_none_and_whitespace() -> None:
    assert normalize_column_name(None) == ""
    assert normalize_column_name("  x  ") == "x"
    assert normalize_column_name(42) == "42"


def test_build_columns_drops_blank_headers() -> None:
    names, kept = build_columns(["id", "", None, "  ", "name"])
    assert names == ["id", "name"]
    assert kept == [0, 4]


def test_is_blank_row() -> None:
    assert is_blank_row([None, "", "  "])
    assert not is_blank_row([None, "x"])
    assert not is_blank_row([0])


# ─────────────────────────── CSV ───────────────────────────


def test_csv_basic_comma(tmp_path: Path) -> None:
    path = _write_csv(tmp_path, "id,name\n1,alice\n2,bob\n")
    reader = CsvReader(path)
    assert reader.columns() == ["id", "name"]
    assert reader.fetch_all() == [
        {"id": "1", "name": "alice"},
        {"id": "2", "name": "bob"},
    ]


def test_csv_custom_delimiter_tsv(tmp_path: Path) -> None:
    path = _write_csv(tmp_path, "id\tname\n1\talice\n")
    reader = CsvReader(path, delimiter="\t")
    assert reader.fetch_all() == [{"id": "1", "name": "alice"}]


def test_csv_values_are_strings_and_empty_preserved(tmp_path: Path) -> None:
    path = _write_csv(tmp_path, "a,b\n,x\n")
    reader = CsvReader(path)
    assert reader.fetch_all() == [{"a": "", "b": "x"}]


def test_csv_utf8_sig_strips_bom(tmp_path: Path) -> None:
    path = _write_csv(tmp_path, "id,name\n1,x\n", encoding="utf-8-sig")
    reader = CsvReader(path)  # 默认 utf-8-sig
    assert reader.columns() == ["id", "name"]
    assert reader.fetch_all() == [{"id": "1", "name": "x"}]


def test_csv_gbk_fallback(tmp_path: Path) -> None:
    # 用 GBK 写中文,默认 utf-8-sig 解码失败 → 自动回退 gbk。
    path = _write_csv(tmp_path, "名称,值\n甲,1\n", encoding="gbk")
    reader = CsvReader(path)
    assert reader.columns() == ["名称", "值"]
    assert reader.fetch_all() == [{"名称": "甲", "值": "1"}]
    assert reader.resolved_encoding == "gbk"


def test_csv_explicit_gbk_no_double_fallback(tmp_path: Path) -> None:
    path = _write_csv(tmp_path, "名称,值\n甲,1\n", encoding="gbk")
    reader = CsvReader(path, encoding="gbk")
    assert reader.resolved_encoding == "gbk"
    assert reader.fetch_all() == [{"名称": "甲", "值": "1"}]


def test_csv_undecodable_raises(tmp_path: Path) -> None:
    path = tmp_path / "bad.csv"
    # 0xFF 0xFE 0xFF 既非合法 utf-8 也非合法 gbk 序列。
    path.write_bytes(b"\xff\xfe\xff\n")
    with pytest.raises(ReaderError):
        CsvReader(path).columns()


def test_csv_header_dedup(tmp_path: Path) -> None:
    path = _write_csv(tmp_path, "id,name,name\n1,a,b\n")
    reader = CsvReader(path)
    assert reader.columns() == ["id", "name", "name__2"]
    assert reader.fetch_all() == [{"id": "1", "name": "a", "name__2": "b"}]


def test_csv_header_row_offset_skips_preamble(tmp_path: Path) -> None:
    path = _write_csv(tmp_path, "report title\nid,name\n1,x\n")
    reader = CsvReader(path, header_row=2)
    assert reader.columns() == ["id", "name"]
    assert reader.fetch_all() == [{"id": "1", "name": "x"}]


def test_csv_ragged_rows_pad_and_truncate(tmp_path: Path) -> None:
    path = _write_csv(tmp_path, "a,b,c\n1\n1,2,3,4\n")
    reader = CsvReader(path)
    assert reader.fetch_all() == [
        {"a": "1", "b": None, "c": None},
        {"a": "1", "b": "2", "c": "3"},
    ]


def test_csv_skips_blank_rows(tmp_path: Path) -> None:
    path = _write_csv(tmp_path, "a,b\n1,2\n\n,\n3,4\n")
    reader = CsvReader(path)
    assert reader.fetch_all() == [
        {"a": "1", "b": "2"},
        {"a": "3", "b": "4"},
    ]


def test_csv_empty_file(tmp_path: Path) -> None:
    path = _write_csv(tmp_path, "")
    reader = CsvReader(path)
    assert reader.columns() == []
    assert reader.fetch_all() == []


def test_csv_max_rows_hard_cap(tmp_path: Path) -> None:
    path = _write_csv(tmp_path, "id\n1\n2\n3\n")
    reader = CsvReader(path)
    assert len(reader.fetch_all(max_rows=3)) == 3
    with pytest.raises(MaxRowsExceededError):
        reader.fetch_all(max_rows=2)


def test_csv_list_columns(tmp_path: Path) -> None:
    path = _write_csv(tmp_path, "id;name\n1;x\n")
    assert csv_list_columns(path, delimiter=";") == ["id", "name"]


# ─────────────────────────── Excel ───────────────────────────


def test_excel_basic_first_sheet(tmp_path: Path) -> None:
    path = _write_xlsx(tmp_path, {"S1": [["id", "name"], [1, "alice"], [2, "bob"]]})
    reader = ExcelReader(path)
    assert reader.columns() == ["id", "name"]
    assert reader.fetch_all() == [
        {"id": 1, "name": "alice"},
        {"id": 2, "name": "bob"},
    ]


def test_excel_sheet_selection(tmp_path: Path) -> None:
    path = _write_xlsx(
        tmp_path,
        {
            "First": [["a"], [1]],
            "Second": [["b"], [2]],
        },
    )
    assert list_sheets(path) == ["First", "Second"]
    assert ExcelReader(path).columns() == ["a"]  # 默认第一个
    assert ExcelReader(path, sheet="Second").fetch_all() == [{"b": 2}]


def test_excel_sheet_not_found(tmp_path: Path) -> None:
    path = _write_xlsx(tmp_path, {"S1": [["a"], [1]]})
    with pytest.raises(ReaderError):
        ExcelReader(path, sheet="nope").columns()


def test_excel_header_row_offset(tmp_path: Path) -> None:
    path = _write_xlsx(
        tmp_path,
        {"S1": [["台账标题", None], ["id", "name"], [1, "x"]]},
    )
    reader = ExcelReader(path, header_row=2)
    assert reader.columns() == ["id", "name"]
    assert reader.fetch_all() == [{"id": 1, "name": "x"}]


def test_excel_native_types_preserved(tmp_path: Path) -> None:
    when = dt.datetime(2024, 1, 2, 3, 4, 5)
    path = _write_xlsx(
        tmp_path,
        {"S1": [["i", "f", "d", "b"], [7, 1.5, when, True]]},
    )
    rows = ExcelReader(path).fetch_all()
    assert rows == [{"i": 7, "f": 1.5, "d": when, "b": True}]
    assert isinstance(rows[0]["i"], int)
    assert isinstance(rows[0]["d"], dt.datetime)


def test_excel_drops_unnamed_columns(tmp_path: Path) -> None:
    path = _write_xlsx(
        tmp_path,
        {"S1": [["id", None, "name"], [1, "junk", "x"]]},
    )
    reader = ExcelReader(path)
    assert reader.columns() == ["id", "name"]
    assert reader.fetch_all() == [{"id": 1, "name": "x"}]


def test_excel_skips_blank_rows(tmp_path: Path) -> None:
    path = _write_xlsx(
        tmp_path,
        {"S1": [["a", "b"], [1, 2], [None, None], [3, 4]]},
    )
    assert ExcelReader(path).fetch_all() == [{"a": 1, "b": 2}, {"a": 3, "b": 4}]


def test_excel_header_dedup(tmp_path: Path) -> None:
    path = _write_xlsx(tmp_path, {"S1": [["name", "name"], ["a", "b"]]})
    reader = ExcelReader(path)
    assert reader.columns() == ["name", "name__2"]
    assert reader.fetch_all() == [{"name": "a", "name__2": "b"}]


def test_excel_empty_sheet(tmp_path: Path) -> None:
    path = _write_xlsx(tmp_path, {"S1": []})
    reader = ExcelReader(path)
    assert reader.columns() == []
    assert reader.fetch_all() == []


def test_excel_max_rows_hard_cap(tmp_path: Path) -> None:
    path = _write_xlsx(tmp_path, {"S1": [["id"], [1], [2], [3]]})
    reader = ExcelReader(path)
    assert len(reader.fetch_all(max_rows=3)) == 3
    with pytest.raises(MaxRowsExceededError):
        reader.fetch_all(max_rows=2)


def test_excel_list_columns(tmp_path: Path) -> None:
    path = _write_xlsx(tmp_path, {"S1": [["x", "y"], [1, 2]]})
    assert excel_list_columns(path) == ["x", "y"]


def test_excel_open_failure_raises_reader_error(tmp_path: Path) -> None:
    path = _write_csv(tmp_path, "not an xlsx", name="fake.xlsx")
    with pytest.raises(ReaderError):
        ExcelReader(path).columns()
