"""C-1 文件源对比 PR2 — CompareDataRef kind="file" 校验单测。

只对 pydantic 契约做断言(不连 DB / 不起 API)。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.schemas import CompareDataRef, CompareTaskCreateRequest


def test_file_ref_csv_minimal_valid() -> None:
    ref = CompareDataRef(kind="file", upload_id="up-1", file_format="csv")
    assert ref.kind == "file"
    assert ref.upload_id == "up-1"
    assert ref.file_format == "csv"
    assert ref.header_row == 1  # 默认 1-indexed 表头
    assert ref.sheet is None
    assert ref.encoding is None
    assert ref.delimiter is None


def test_file_ref_excel_with_sheet_and_header() -> None:
    ref = CompareDataRef(
        kind="file",
        upload_id="up-2",
        file_format="excel",
        sheet="Sheet2",
        header_row=3,
    )
    assert ref.file_format == "excel"
    assert ref.sheet == "Sheet2"
    assert ref.header_row == 3


def test_file_ref_csv_with_delimiter_and_encoding() -> None:
    ref = CompareDataRef(
        kind="file",
        upload_id="up-3",
        file_format="csv",
        delimiter=";",
        encoding="gbk",
    )
    assert ref.delimiter == ";"
    assert ref.encoding == "gbk"


def test_file_ref_requires_upload_id() -> None:
    with pytest.raises(ValidationError, match="file ref requires upload_id"):
        CompareDataRef(kind="file", file_format="csv")


def test_file_ref_requires_file_format() -> None:
    with pytest.raises(ValidationError, match="file ref requires file_format"):
        CompareDataRef(kind="file", upload_id="up-1")


def test_file_ref_rejects_unknown_format() -> None:
    # Parquet 归次版,当前只接受 csv|excel。故意传非法值验证 pydantic 运行时拒绝;
    # file_format 的 Literal 静态也不含 parquet,mypy(app tests)会报 arg-type,
    # 这里正是要测这个被拒的值 → 显式 ignore。
    with pytest.raises(ValidationError):
        CompareDataRef(kind="file", upload_id="up-1", file_format="parquet")  # type: ignore[arg-type]


def test_file_ref_rejects_header_row_below_one() -> None:
    with pytest.raises(ValidationError):
        CompareDataRef(kind="file", upload_id="up-1", file_format="csv", header_row=0)


def test_table_ref_still_requires_table_name() -> None:
    with pytest.raises(ValidationError, match="table ref requires table_name"):
        CompareDataRef(kind="table")


def test_sql_ref_still_requires_sql() -> None:
    with pytest.raises(ValidationError, match="sql ref requires sql"):
        CompareDataRef(kind="sql")


def test_table_ref_ignores_unused_file_fields() -> None:
    ref = CompareDataRef(kind="table", table_name="orders")
    assert ref.upload_id is None
    assert ref.file_format is None


# ── CompareTaskCreateRequest:文件侧 datasource 可选校验(C-1 PR3)────────────


def _file_ref() -> CompareDataRef:
    return CompareDataRef(kind="file", upload_id="up-1", file_format="csv")


def _table_ref() -> CompareDataRef:
    return CompareDataRef(kind="table", table_name="orders")


def test_task_create_file_source_side_omits_source_id() -> None:
    body = CompareTaskCreateRequest(
        project_id="p1",
        name="file vs db",
        target_id="ds-target",
        source_ref=_file_ref(),
        target_ref=_table_ref(),
    )
    assert body.source_id is None
    assert body.target_id == "ds-target"


def test_task_create_both_file_sides_need_no_datasource() -> None:
    body = CompareTaskCreateRequest(
        project_id="p1",
        name="file vs file",
        source_ref=_file_ref(),
        target_ref=CompareDataRef(kind="file", upload_id="up-2", file_format="excel"),
    )
    assert body.source_id is None
    assert body.target_id is None


def test_task_create_table_side_requires_datasource() -> None:
    with pytest.raises(ValidationError, match="source ref kind=table requires source_id"):
        CompareTaskCreateRequest(
            project_id="p1",
            name="missing source id",
            target_id="ds-target",
            source_ref=_table_ref(),
            target_ref=_table_ref(),
        )


def test_task_create_sql_target_side_requires_datasource() -> None:
    with pytest.raises(ValidationError, match="target ref kind=sql requires target_id"):
        CompareTaskCreateRequest(
            project_id="p1",
            name="missing target id",
            source_id="ds-source",
            source_ref=_table_ref(),
            target_ref=CompareDataRef(kind="sql", sql="SELECT 1"),
        )
