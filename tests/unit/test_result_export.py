from __future__ import annotations

import io
import zipfile

import pytest

from app.domain.schema import Column, ColumnType, Row
from app.infrastructure.result_export import ExportSizeLimitExceeded, write_result_export


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
