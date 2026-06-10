"""MySQL driver 类型 → 统一 ColumnType 映射(契约 §3.2 ColumnType)。

两路输入,因为 MySQL 在两处暴露列类型的形态不同:

- **SELECT 结果**(`cursor.description[1]`):PyMySQL FIELD_TYPE 整数常量
  (MySQL 协议 field type code,稳定值)。走 `field_type_to_column_type`。
- **information_schema.COLUMNS.COLUMN_TYPE**:字符串,如 `"bigint(20) unsigned"` /
  `"varchar(64)"` / `"decimal(10,2)"`。走 `column_type_string_to_column_type`。

映射用 PyMySQL 自带的 FIELD_TYPE 常量名解析(不硬编魔数),无法识别落 UNKNOWN。
原始 driver 类型(整数码字符串 / COLUMN_TYPE 串)由调用方塞进 Column.driver_type 备查。
"""

from __future__ import annotations

import re
from typing import Any

from app.domain.schema import ColumnType

# FIELD_TYPE 常量名 → 统一 ColumnType。
# 用名字而非魔数:运行时从 pymysql.constants.FIELD_TYPE 取整数值,
# 防驱动小版本偶发改码 + 让映射自解释(MySQL 协议码本身稳定,这里求稳)。
_FIELD_TYPE_NAME_TO_COLUMN_TYPE: dict[str, ColumnType] = {
    # 整数
    "TINY": ColumnType.INTEGER,
    "SHORT": ColumnType.INTEGER,
    "LONG": ColumnType.INTEGER,
    "INT24": ColumnType.INTEGER,
    "LONGLONG": ColumnType.INTEGER,
    "YEAR": ColumnType.INTEGER,
    # 浮点
    "FLOAT": ColumnType.FLOAT,
    "DOUBLE": ColumnType.FLOAT,
    # 定点
    "DECIMAL": ColumnType.DECIMAL,
    "NEWDECIMAL": ColumnType.DECIMAL,
    # 时间
    "TIMESTAMP": ColumnType.DATETIME,
    "DATETIME": ColumnType.DATETIME,
    "TIMESTAMP2": ColumnType.DATETIME,
    "DATETIME2": ColumnType.DATETIME,
    "DATE": ColumnType.DATE,
    "NEWDATE": ColumnType.DATE,
    "TIME": ColumnType.TIME,
    "TIME2": ColumnType.TIME,
    # 二进制 / BLOB
    "TINY_BLOB": ColumnType.BYTES,
    "MEDIUM_BLOB": ColumnType.BYTES,
    "LONG_BLOB": ColumnType.BYTES,
    "BLOB": ColumnType.BYTES,
    # JSON
    "JSON": ColumnType.JSON,
    # 字符串 / 其它文本类
    "VARCHAR": ColumnType.STRING,
    "VAR_STRING": ColumnType.STRING,
    "STRING": ColumnType.STRING,
    "ENUM": ColumnType.STRING,
    "SET": ColumnType.STRING,
    # BIT / GEOMETRY 等无对应统一语义 → 由 caller 落 UNKNOWN
}

# COLUMN_TYPE 字符串前缀(取第一个标识符,小写)→ 统一 ColumnType。
# COLUMN_TYPE 形如 "bigint(20) unsigned" / "varchar(64)" / "decimal(10,2)" /
# "tinyint(1)"(MySQL 习惯当 boolean,但无可靠信号,保守归 INTEGER)。
_COLUMN_TYPE_PREFIX_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_]*)")
_COLUMN_TYPE_NAME_TO_COLUMN_TYPE: dict[str, ColumnType] = {
    # 整数
    "tinyint": ColumnType.INTEGER,
    "smallint": ColumnType.INTEGER,
    "mediumint": ColumnType.INTEGER,
    "int": ColumnType.INTEGER,
    "integer": ColumnType.INTEGER,
    "bigint": ColumnType.INTEGER,
    "year": ColumnType.INTEGER,
    "bit": ColumnType.INTEGER,
    # 浮点
    "float": ColumnType.FLOAT,
    "double": ColumnType.FLOAT,
    "real": ColumnType.FLOAT,
    # 定点
    "decimal": ColumnType.DECIMAL,
    "dec": ColumnType.DECIMAL,
    "numeric": ColumnType.DECIMAL,
    "fixed": ColumnType.DECIMAL,
    # 布尔(MySQL 别名,服务器会展开成 tinyint(1),但若驱动直报 bool 则识别)
    "bool": ColumnType.BOOLEAN,
    "boolean": ColumnType.BOOLEAN,
    # 时间
    "datetime": ColumnType.DATETIME,
    "timestamp": ColumnType.DATETIME,
    "date": ColumnType.DATE,
    "time": ColumnType.TIME,
    # 二进制 / BLOB
    "binary": ColumnType.BYTES,
    "varbinary": ColumnType.BYTES,
    "tinyblob": ColumnType.BYTES,
    "blob": ColumnType.BYTES,
    "mediumblob": ColumnType.BYTES,
    "longblob": ColumnType.BYTES,
    # JSON
    "json": ColumnType.JSON,
    # 字符串
    "char": ColumnType.STRING,
    "varchar": ColumnType.STRING,
    "tinytext": ColumnType.STRING,
    "text": ColumnType.STRING,
    "mediumtext": ColumnType.STRING,
    "longtext": ColumnType.STRING,
    "enum": ColumnType.STRING,
    "set": ColumnType.STRING,
}


def field_type_to_column_type(pymysql_module: Any, code: object) -> ColumnType:
    """PyMySQL FIELD_TYPE 整数码 → 统一 ColumnType。无法识别落 UNKNOWN。"""
    if not isinstance(code, int):
        return ColumnType.UNKNOWN
    mapping = _field_type_code_map(pymysql_module)
    return mapping.get(code, ColumnType.UNKNOWN)


def column_type_string_to_column_type(raw: object) -> ColumnType:
    """information_schema COLUMN_TYPE 字符串 → 统一 ColumnType。无法识别落 UNKNOWN。"""
    if not isinstance(raw, str):
        return ColumnType.UNKNOWN
    match = _COLUMN_TYPE_PREFIX_RE.match(raw)
    if match is None:
        return ColumnType.UNKNOWN
    return _COLUMN_TYPE_NAME_TO_COLUMN_TYPE.get(match.group(1).lower(), ColumnType.UNKNOWN)


def _field_type_code_map(pymysql_module: Any) -> dict[int, ColumnType]:
    """从 pymysql.constants.FIELD_TYPE 解析常量名 → 整数码,组装 int→ColumnType。

    驱动缺某常量名时跳过(向后兼容老/新驱动),不抛。
    """
    field_type = getattr(getattr(pymysql_module, "constants", None), "FIELD_TYPE", None)
    if field_type is None:  # pragma: no cover - 驱动结构异常的极端兜底
        return {}
    resolved: dict[int, ColumnType] = {}
    for name, column_type in _FIELD_TYPE_NAME_TO_COLUMN_TYPE.items():
        code = getattr(field_type, name, None)
        if isinstance(code, int):
            resolved[code] = column_type
    return resolved


__all__ = [
    "column_type_string_to_column_type",
    "field_type_to_column_type",
]
