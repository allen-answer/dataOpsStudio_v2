from __future__ import annotations

import csv
import json
import math
import zipfile
from collections.abc import Callable, Iterable
from datetime import date, datetime
from datetime import time as dt_time
from decimal import Decimal
from itertools import chain
from typing import IO, Any, BinaryIO, Literal, cast
from xml.sax.saxutils import escape

from app.domain.schema import Column, Row

ExportFormat = Literal["csv", "excel", "json", "sql"]
XlsxCellStyleResolver = Callable[[str, int, int, list[object]], int | None]

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


def write_xlsx_workbook(
    *,
    stream: BinaryIO,
    sheets: Iterable[tuple[str, list[Column], Iterable[Row]]],
    limit_bytes: int,
    compare_highlight: bool = False,
) -> int:
    if limit_bytes <= 0:
        raise ValueError("limit_bytes must be positive")
    normalized_sheets = []
    for name, columns, rows in sheets:
        normalized_columns, normalized_rows = _prepare_columns(columns, rows)
        normalized_sheets.append((_safe_sheet_name(name), normalized_columns, normalized_rows))
    if not normalized_sheets:
        normalized_sheets.append(("Result", [Column(name="value")], []))
    writer = _LimitedBinaryWriter(stream, limit_bytes)
    style_resolver = _compare_cell_style if compare_highlight else None
    _write_xlsx_sheets(writer, normalized_sheets, cell_style_resolver=style_resolver)
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
    _write_xlsx_sheets(writer, [("Result", columns, rows)])


def _write_xlsx_sheets(
    writer: _LimitedBinaryWriter,
    sheets: list[tuple[str, list[Column], Iterable[Row]]],
    *,
    cell_style_resolver: XlsxCellStyleResolver | None = None,
) -> None:
    with zipfile.ZipFile(cast(Any, writer), mode="w", compression=zipfile.ZIP_DEFLATED) as workbook:
        workbook.writestr("[Content_Types].xml", _content_types_xml(len(sheets)))
        workbook.writestr("_rels/.rels", _RELS_XML)
        workbook.writestr("xl/workbook.xml", _workbook_xml([name for name, _, _ in sheets]))
        workbook.writestr("xl/_rels/workbook.xml.rels", _workbook_rels_xml(len(sheets)))
        workbook.writestr("xl/styles.xml", _STYLES_XML)
        for sheet_index, (sheet_name, columns, rows) in enumerate(sheets, start=1):
            with workbook.open(f"xl/worksheets/sheet{sheet_index}.xml", mode="w") as sheet:
                sheet.write(
                    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                    b"<sheetData>"
                )
                _write_xlsx_row(
                    sheet,
                    1,
                    [column.name for column in columns],
                    sheet_name=sheet_name,
                    cell_style_resolver=cell_style_resolver,
                )
                width = len(columns)
                row_number = 2
                for row in rows:
                    _write_xlsx_row(
                        sheet,
                        row_number,
                        _padded_values(row, width),
                        sheet_name=sheet_name,
                        cell_style_resolver=cell_style_resolver,
                    )
                    row_number += 1
                sheet.write(b"</sheetData></worksheet>")


def _content_types_xml(sheet_count: int) -> str:
    sheet_overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for index in range(1, sheet_count + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        f"{sheet_overrides}"
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        "</Types>"
    )


def _workbook_xml(sheet_names: list[str]) -> str:
    sheets_xml = "".join(
        f'<sheet name="{_xml_attr(name)}" sheetId="{index}" r:id="rId{index}"/>'
        for index, name in enumerate(sheet_names, start=1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{sheets_xml}</sheets>"
        "</workbook>"
    )


def _workbook_rels_xml(sheet_count: int) -> str:
    sheet_rels = "".join(
        f'<Relationship Id="rId{index}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        f'Target="worksheets/sheet{index}.xml"/>'
        for index in range(1, sheet_count + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{sheet_rels}"
        f'<Relationship Id="rId{sheet_count + 1}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
        "</Relationships>"
    )


def _safe_sheet_name(name: str) -> str:
    cleaned = "".join("_" if char in "[]:*?/\\" else char for char in name.strip())
    return (cleaned or "Sheet")[:31]


def _xml_attr(value: str) -> str:
    return escape(value, {'"': "&quot;", "'": "&apos;"})


def _write_xlsx_row(
    sheet: IO[bytes],
    row_number: int,
    values: list[object],
    *,
    sheet_name: str = "Result",
    cell_style_resolver: XlsxCellStyleResolver | None = None,
) -> None:
    sheet.write(f'<row r="{row_number}">'.encode())
    for index, value in enumerate(values, start=1):
        ref = f"{_column_letter(index)}{row_number}"
        style = (
            cell_style_resolver(sheet_name, row_number, index, values)
            if cell_style_resolver is not None
            else None
        )
        sheet.write(_xlsx_cell_xml(ref, value, style=style).encode("utf-8"))
    sheet.write(b"</row>")


def _xlsx_cell_xml(ref: str, value: object, *, style: int | None = None) -> str:
    style_attr = f' s="{style}"' if style is not None else ""
    if value is None:
        return f'<c r="{ref}"{style_attr}/>'
    if isinstance(value, bool):
        return f'<c r="{ref}"{style_attr} t="b"><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if math.isfinite(number):
            return f'<c r="{ref}"{style_attr}><v>{value}</v></c>'
    text = sanitize_formula_text(_text_value(value))
    return f'<c r="{ref}"{style_attr} t="inlineStr"><is><t>{escape(text)}</t></is></c>'


def _compare_cell_style(
    sheet_name: str,
    row_number: int,
    column_index: int,
    values: list[object],
) -> int | None:
    if row_number <= 1:
        return None
    if sheet_name == "only_source":
        return 1
    if sheet_name == "only_target":
        return 2
    if sheet_name != "diff":
        return None
    key_width = _compare_diff_key_width(values)
    if column_index <= key_width:
        return None
    relative = column_index - key_width - 1
    pair_start = key_width + (relative // 2) * 2
    if pair_start + 1 >= len(values):
        return None
    return 3 if values[pair_start] != values[pair_start + 1] else None


def _compare_diff_key_width(values: list[object]) -> int:
    for width in range(1, len(values) + 1):
        tail_width = len(values) - width
        if tail_width > 0 and tail_width % 2 == 0:
            return width
    return 0


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
    '<fills count="5">'
    '<fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FFFFE2E2"/>'
    '<bgColor indexed="64"/></patternFill></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FFDCFCE7"/>'
    '<bgColor indexed="64"/></patternFill></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FFFFF3C7"/>'
    '<bgColor indexed="64"/></patternFill></fill>'
    '</fills>'
    '<borders count="1"><border/></borders>'
    '<cellStyleXfs count="1"><xf/></cellStyleXfs>'
    '<cellXfs count="4">'
    '<xf xfId="0"/>'
    '<xf xfId="0" fillId="2" applyFill="1"/>'
    '<xf xfId="0" fillId="3" applyFill="1"/>'
    '<xf xfId="0" fillId="4" applyFill="1"/>'
    '</cellXfs>'
    "</styleSheet>"
)
