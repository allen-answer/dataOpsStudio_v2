from __future__ import annotations

import csv
import json
import math
import zipfile
from collections.abc import Iterable
from datetime import date, datetime, time as dt_time
from decimal import Decimal
from itertools import chain
from typing import Any, BinaryIO, Literal, cast
from xml.sax.saxutils import escape

from app.domain.schema import Column, Row

ExportFormat = Literal["csv", "excel", "json", "sql"]

FORMULA_PREFIXES = ("=", "+", "-", "@")


class ExportSizeLimitExceeded(RuntimeError):
    """Export file exceeded the configured byte limit."""


def export_extension(export_format: ExportFormat) -> str:
    return "xlsx" if export_format == "excel" else export_format


def export_content_type(export_format: ExportFormat) -> str:
    if export_format == "csv":
        return "text/csv; charset=utf-8"
    if export_format == "excel":
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if export_format == "json":
        return "application/json; charset=utf-8"
    return "application/sql; charset=utf-8"


def write_result_export(
    *,
    stream: BinaryIO,
    export_format: ExportFormat,
    columns: list[Column],
    rows: Iterable[Row],
    table_name: str,
    db_type: str,
    limit_bytes: int,
) -> int:
    if limit_bytes <= 0:
        raise ValueError("limit_bytes must be positive")
    normalized_columns, normalized_rows = _prepare_columns(columns, rows)
    writer = _LimitedBinaryWriter(stream, limit_bytes)
    if export_format == "csv":
        _write_csv(writer, normalized_columns, normalized_rows)
    elif export_format == "excel":
        _write_xlsx(writer, normalized_columns, normalized_rows)
    elif export_format == "json":
        _write_json(writer, normalized_columns, normalized_rows)
    elif export_format == "sql":
        _write_sql(writer, normalized_columns, normalized_rows, table_name, db_type)
    else:
        raise ValueError(f"unsupported export format: {export_format}")
    return writer.bytes_written


def sanitize_formula_text(value: str) -> str:
    if value.startswith(FORMULA_PREFIXES):
        return "'" + value
    return value


def _prepare_columns(
    columns: list[Column],
    rows: Iterable[Row],
) -> tuple[list[Column], Iterable[Row]]:
    iterator = iter(rows)
    first = next(iterator, None)
    width = len(columns)
    if first is not None:
        width = max(width, len(first.values))
    if width <= 0:
        width = 1
    normalized = list(columns)
    for index in range(len(normalized), width):
        normalized.append(Column(name=f"column_{index + 1}"))
    return normalized, iterator if first is None else chain([first], iterator)


def _write_csv(
    writer: _LimitedBinaryWriter,
    columns: list[Column],
    rows: Iterable[Row],
) -> None:
    csv_writer = csv.writer(_TextSink(writer), lineterminator="\n")
    csv_writer.writerow([column.name for column in columns])
    width = len(columns)
    for row in rows:
        csv_writer.writerow([_csv_value(value) for value in _padded_values(row, width)])


def _write_json(
    writer: _LimitedBinaryWriter,
    columns: list[Column],
    rows: Iterable[Row],
) -> None:
    names = _unique_column_names(columns)
    width = len(names)
    writer.write_text("[")
    first = True
    for row in rows:
        item = {
            name: _json_value(value)
            for name, value in zip(names, _padded_values(row, width), strict=True)
        }
        writer.write_text("" if first else ",")
        writer.write_text(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
        first = False
    writer.write_text("]\n")


def _write_sql(
    writer: _LimitedBinaryWriter,
    columns: list[Column],
    rows: Iterable[Row],
    table_name: str,
    db_type: str,
) -> None:
    quoted_table = _quote_table_name(table_name, db_type)
    quoted_columns = ", ".join(_quote_identifier(column.name, db_type) for column in columns)
    width = len(columns)
    for row in rows:
        values = ", ".join(_sql_literal(value) for value in _padded_values(row, width))
        writer.write_text(f"INSERT INTO {quoted_table} ({quoted_columns}) VALUES ({values});\n")


def _write_xlsx(
    writer: _LimitedBinaryWriter,
    columns: list[Column],
    rows: Iterable[Row],
) -> None:
    with zipfile.ZipFile(cast(Any, writer), mode="w", compression=zipfile.ZIP_DEFLATED) as workbook:
        workbook.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)
        workbook.writestr("_rels/.rels", _RELS_XML)
        workbook.writestr("xl/workbook.xml", _WORKBOOK_XML)
        workbook.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS_XML)
        workbook.writestr("xl/styles.xml", _STYLES_XML)
        with workbook.open("xl/worksheets/sheet1.xml", mode="w") as sheet:
            sheet.write(
                b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                b"<sheetData>"
            )
            _write_xlsx_row(sheet, 1, [column.name for column in columns])
            width = len(columns)
            row_number = 2
            for row in rows:
                _write_xlsx_row(sheet, row_number, _padded_values(row, width))
                row_number += 1
            sheet.write(b"</sheetData></worksheet>")


def _write_xlsx_row(sheet: BinaryIO, row_number: int, values: list[object]) -> None:
    sheet.write(f'<row r="{row_number}">'.encode("utf-8"))
    for index, value in enumerate(values, start=1):
        ref = f"{_column_letter(index)}{row_number}"
        sheet.write(_xlsx_cell_xml(ref, value).encode("utf-8"))
    sheet.write(b"</row>")


def _xlsx_cell_xml(ref: str, value: object) -> str:
    if value is None:
        return f'<c r="{ref}"/>'
    if isinstance(value, bool):
        return f'<c r="{ref}" t="b"><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if math.isfinite(number):
            return f'<c r="{ref}"><v>{value}</v></c>'
    text = sanitize_formula_text(_text_value(value))
    return f'<c r="{ref}" t="inlineStr"><is><t>{escape(text)}</t></is></c>'


def _column_letter(index: int) -> str:
    letters = ""
    value = index
    while value:
        value, remainder = divmod(value - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _csv_value(value: object) -> object:
    if isinstance(value, str):
        return sanitize_formula_text(value)
    return value


def _json_value(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, datetime | date | dt_time):
        return value.isoformat()
    return str(value)


def _sql_literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return str(value)
    text = _text_value(value).replace("'", "''")
    return f"'{text}'"


def _text_value(value: object) -> str:
    if isinstance(value, datetime | date | dt_time):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def _padded_values(row: Row, width: int) -> list[object]:
    values: list[object] = list(row.values[:width])
    if len(values) < width:
        values.extend([None] * (width - len(values)))
    return values


def _unique_column_names(columns: list[Column]) -> list[str]:
    counts: dict[str, int] = {}
    names: list[str] = []
    for column in columns:
        base = column.name or "column"
        count = counts.get(base, 0) + 1
        counts[base] = count
        names.append(base if count == 1 else f"{base}_{count}")
    return names


def _quote_table_name(table_name: str, db_type: str) -> str:
    return ".".join(_quote_identifier(part, db_type) for part in table_name.split(".") if part)


def _quote_identifier(identifier: str, db_type: str) -> str:
    if db_type == "mysql":
        return "`" + identifier.replace("`", "``") + "`"
    return '"' + identifier.replace('"', '""') + '"'


class _TextSink:
    def __init__(self, writer: _LimitedBinaryWriter) -> None:
        self._writer = writer

    def write(self, value: str) -> int:
        self._writer.write_text(value)
        return len(value)


class _LimitedBinaryWriter:
    def __init__(self, stream: BinaryIO, limit_bytes: int) -> None:
        self._stream = stream
        self._limit_bytes = limit_bytes
        self.bytes_written = 0

    def write(self, data: bytes) -> int:
        self.bytes_written += len(data)
        if self.bytes_written > self._limit_bytes:
            raise ExportSizeLimitExceeded("export_limit_exceeded")
        return self._stream.write(data)

    def write_text(self, value: str) -> None:
        self.write(value.encode("utf-8"))

    def flush(self) -> None:
        self._stream.flush()

    def tell(self) -> int:
        return self._stream.tell()

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._stream.seek(offset, whence)


_CONTENT_TYPES_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" '
    'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    '<Override PartName="/xl/styles.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
    "</Types>"
)

_RELS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
    'officeDocument" Target="xl/workbook.xml"/>'
    "</Relationships>"
)

_WORKBOOK_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    '<sheets><sheet name="Result" sheetId="1" r:id="rId1"/></sheets>'
    "</workbook>"
)

_WORKBOOK_RELS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
    'Target="worksheets/sheet1.xml"/>'
    '<Relationship Id="rId2" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
    'Target="styles.xml"/>'
    "</Relationships>"
)

_STYLES_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
    '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
    '<borders count="1"><border/></borders>'
    '<cellStyleXfs count="1"><xf/></cellStyleXfs>'
    '<cellXfs count="1"><xf xfId="0"/></cellXfs>'
    "</styleSheet>"
)
