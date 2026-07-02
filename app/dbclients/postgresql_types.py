from __future__ import annotations

import re

from app.domain.schema import ColumnType

_TYPE_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")

_OID_TO_COLUMN_TYPE: dict[int, ColumnType] = {
    16: ColumnType.BOOLEAN,
    17: ColumnType.BYTES,
    20: ColumnType.INTEGER,
    21: ColumnType.INTEGER,
    23: ColumnType.INTEGER,
    25: ColumnType.STRING,
    700: ColumnType.FLOAT,
    701: ColumnType.FLOAT,
    1042: ColumnType.STRING,
    1043: ColumnType.STRING,
    1082: ColumnType.DATE,
    1083: ColumnType.TIME,
    1114: ColumnType.DATETIME,
    1184: ColumnType.DATETIME,
    1700: ColumnType.DECIMAL,
    2950: ColumnType.STRING,
}

_NAME_TO_COLUMN_TYPE: dict[str, ColumnType] = {
    "BOOLEAN": ColumnType.BOOLEAN,
    "BOOL": ColumnType.BOOLEAN,
    "SMALLINT": ColumnType.INTEGER,
    "INTEGER": ColumnType.INTEGER,
    "INT": ColumnType.INTEGER,
    "INT2": ColumnType.INTEGER,
    "INT4": ColumnType.INTEGER,
    "INT8": ColumnType.INTEGER,
    "BIGINT": ColumnType.INTEGER,
    "SERIAL": ColumnType.INTEGER,
    "BIGSERIAL": ColumnType.INTEGER,
    "NUMERIC": ColumnType.DECIMAL,
    "DECIMAL": ColumnType.DECIMAL,
    "REAL": ColumnType.FLOAT,
    "FLOAT": ColumnType.FLOAT,
    "FLOAT4": ColumnType.FLOAT,
    "FLOAT8": ColumnType.FLOAT,
    "DOUBLE": ColumnType.FLOAT,
    "DATE": ColumnType.DATE,
    "TIME": ColumnType.TIME,
    "TIMETZ": ColumnType.TIME,
    "TIMESTAMP": ColumnType.DATETIME,
    "TIMESTAMPTZ": ColumnType.DATETIME,
    "BYTEA": ColumnType.BYTES,
    "CHAR": ColumnType.STRING,
    "CHARACTER": ColumnType.STRING,
    "VARCHAR": ColumnType.STRING,
    "TEXT": ColumnType.STRING,
    "UUID": ColumnType.STRING,
    "JSON": ColumnType.STRING,
    "JSONB": ColumnType.STRING,
}


def type_code_to_column_type(type_code: object) -> ColumnType:
    if isinstance(type_code, int):
        return _OID_TO_COLUMN_TYPE.get(type_code, ColumnType.UNKNOWN)
    driver_name = type_code_to_driver_name(type_code)
    if driver_name is None:
        return ColumnType.UNKNOWN
    return _NAME_TO_COLUMN_TYPE.get(driver_name, ColumnType.UNKNOWN)


def type_code_to_driver_name(type_code: object) -> str | None:
    if isinstance(type_code, int):
        return _oid_driver_name(type_code)
    if isinstance(type_code, str):
        return _normalize(type_code)
    name = getattr(type_code, "name", None)
    if isinstance(name, str):
        return _normalize(name)
    return None


def data_type_string_to_column_type(data_type: object) -> ColumnType:
    if not isinstance(data_type, str):
        return ColumnType.UNKNOWN
    normalized = _normalize(data_type)
    if normalized is None:
        return ColumnType.UNKNOWN
    return _NAME_TO_COLUMN_TYPE.get(normalized, ColumnType.UNKNOWN)


def _oid_driver_name(oid: int) -> str | None:
    names = {
        16: "BOOL",
        17: "BYTEA",
        20: "INT8",
        21: "INT2",
        23: "INT4",
        25: "TEXT",
        700: "FLOAT4",
        701: "FLOAT8",
        1042: "CHAR",
        1043: "VARCHAR",
        1082: "DATE",
        1083: "TIME",
        1114: "TIMESTAMP",
        1184: "TIMESTAMPTZ",
        1700: "NUMERIC",
        2950: "UUID",
    }
    return names.get(oid)


def _normalize(name: str) -> str | None:
    match = _TYPE_NAME_RE.search(name)
    return match.group(0).upper() if match else None


__all__ = [
    "data_type_string_to_column_type",
    "type_code_to_column_type",
    "type_code_to_driver_name",
]
