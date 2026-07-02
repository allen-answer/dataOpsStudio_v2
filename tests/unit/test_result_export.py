from __future__ import annotations

import io
import zipfile

import pytest

from app.domain.schema import Column, ColumnType, Row
from app.infrastructure.result_export import (
    ExportSizeLimitExceeded,
    write_result_export,
    write_xlsx_workbook,
)


def test_csv_export_prefixes_formula_like_cells() -> None:
    stream = io.BytesIO()

    write_result_export(
        stream=stream,
        export_format="csv",
        columns=[Column(name="value", type=ColumnType.STRING)],
        rows=[Row(values=["=cmd"]), Row(values=["+sum"]), Row(values=["-1"]), Row(values=["@x"])],
        table_name="exported_result",
        db_type="mysql",
        limit_bytes=1024,
    )

    assert stream.getvalue().decode("utf-8") == "value\n'=cmd\n'+sum\n'-1\n'@x\n"


def test_xlsx_export_prefixes_formula_like_cells() -> None:
    stream = io.BytesIO()

    write_result_export(
        stream=stream,
        export_format="excel",
        columns=[Column(name="value", type=ColumnType.STRING)],
        rows=[Row(values=["=cmd"])],
        table_name="exported_result",
        db_type="mysql",
        limit_bytes=4096,
    )

    with zipfile.ZipFile(io.BytesIO(stream.getvalue())) as workbook:
        sheet_xml = workbook.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert "'=cmd" in sheet_xml


def test_xlsx_workbook_writes_multiple_sheets_with_formula_defense() -> None:
    stream = io.BytesIO()

    write_xlsx_workbook(
        stream=stream,
        sheets=[
            ("only_source", [Column(name="value", type=ColumnType.STRING)], [Row(values=["=cmd"])]),
            ("diff", [Column(name="value", type=ColumnType.STRING)], [Row(values=["safe"])]),
        ],
        limit_bytes=8192,
    )

    with zipfile.ZipFile(io.BytesIO(stream.getvalue())) as workbook:
        workbook_xml = workbook.read("xl/workbook.xml").decode("utf-8")
        sheet1_xml = workbook.read("xl/worksheets/sheet1.xml").decode("utf-8")
        sheet2_xml = workbook.read("xl/worksheets/sheet2.xml").decode("utf-8")
    assert 'name="only_source"' in workbook_xml
    assert 'name="diff"' in workbook_xml
    assert "'=cmd" in sheet1_xml
    assert "safe" in sheet2_xml


def test_sql_export_writes_quoted_insert_statements() -> None:
    stream = io.BytesIO()

    write_result_export(
        stream=stream,
        export_format="sql",
        columns=[Column(name="select", type=ColumnType.STRING)],
        rows=[Row(values=["O'Reilly"])],
        table_name="app.users",
        db_type="dm",
        limit_bytes=1024,
    )

    assert stream.getvalue().decode("utf-8") == (
        'INSERT INTO "app"."users" ("select") VALUES (\'O\'\'Reilly\');\n'
    )


def test_export_size_limit_is_enforced() -> None:
    stream = io.BytesIO()

    with pytest.raises(ExportSizeLimitExceeded):
        write_result_export(
            stream=stream,
            export_format="json",
            columns=[Column(name="value", type=ColumnType.STRING)],
            rows=[Row(values=["x" * 32])],
            table_name="exported_result",
            db_type="mysql",
            limit_bytes=8,
        )
