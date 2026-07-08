"""Oracle driver / ALL_TAB_COLUMNS 类型 → 统一 ColumnType 映射(契约 §3.2 ColumnType)。

两条入口(与 DM / DB2 types 同构):

1. **SELECT 结果**(`cursor.description[1]`):python-oracledb 的 type_code 是
   `oracledb.DbType` 对象(`.name` 形如 "DB_TYPE_VARCHAR" / "DB_TYPE_NUMBER")。
   走 `description_item_to_column_type`,按 `.name` 去 "DB_TYPE_" 前缀后归类。
2. **ALL_TAB_COLUMNS.DATA_TYPE**:字符串,如 "NUMBER" / "VARCHAR2" /
   "TIMESTAMP(6) WITH TIME ZONE" / "LONG RAW"。走 `data_type_string_to_column_type`。

★ Oracle NUMBER 既能承整数也能承小数,单凭类型无法区分;真正的判别信号是
  description 的 scale(元组 idx5 / FetchInfo.scale):scale==0 → INTEGER,
  scale>0 或缺失 → DECIMAL(保守默认,不丢精度)。

★ Oracle DATE 实际带时分秒,但同族 DM adapter 一律把 DATE 归 DATE(统一 ColumnType
  家族行为对齐),此处沿用 DM 口径归 DATE。

★ 无法识别一律 UNKNOWN(不臆造),原始类型由调用方塞进 Column.driver_type。

driver_type 只放干净类型名(如 "VARCHAR" / "NUMBER"),不透传 DbType 的原始 repr。
"""

from __future__ import annotations

import re

from app.domain.schema import ColumnType

# DATA_TYPE 字符串前缀(取第一个标识符,大写后比对)→ 统一 ColumnType。
# ALL_TAB_COLUMNS.DATA_TYPE 形如 "NUMBER" / "VARCHAR2" / "TIMESTAMP(6) WITH TIME ZONE"。
_DATA_TYPE_PREFIX_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_]*)")

# 干净大写类型名 → 统一 ColumnType(description type-object 与 DATA_TYPE 前缀共用)。
_NAME_TO_COLUMN_TYPE: dict[str, ColumnType] = {
    # 数值 —— NUMBER 无 scale 信号时保守归 DECIMAL(有 scale==0 时上层改判 INTEGER)
    "NUMBER": ColumnType.DECIMAL,
    "NUMERIC": ColumnType.DECIMAL,
    "DECIMAL": ColumnType.DECIMAL,
    "DEC": ColumnType.DECIMAL,
    # 明确整数类型(PL/SQL BINARY_INTEGER / PLS_INTEGER,不受 scale 影响)
    "INT": ColumnType.INTEGER,
    "INTEGER": ColumnType.INTEGER,
    "SMALLINT": ColumnType.INTEGER,
    "BINARY_INTEGER": ColumnType.INTEGER,
    "PLS_INTEGER": ColumnType.INTEGER,
    # 浮点
    "FLOAT": ColumnType.FLOAT,
    "BINARY_FLOAT": ColumnType.FLOAT,
    "BINARY_DOUBLE": ColumnType.FLOAT,
    "REAL": ColumnType.FLOAT,
    "DOUBLE": ColumnType.FLOAT,
    # 布尔(Oracle 23c 原生 BOOLEAN / PL/SQL BOOLEAN)
    "BOOLEAN": ColumnType.BOOLEAN,
    # 时间 —— TIMESTAMP 家族归 DATETIME;DATE 沿用 DM 口径归 DATE
    "TIMESTAMP": ColumnType.DATETIME,
    "DATE": ColumnType.DATE,
    # 字符串
    "VARCHAR": ColumnType.STRING,
    "VARCHAR2": ColumnType.STRING,
    "NVARCHAR": ColumnType.STRING,
    "NVARCHAR2": ColumnType.STRING,
    "CHAR": ColumnType.STRING,
    "NCHAR": ColumnType.STRING,
    "CHARACTER": ColumnType.STRING,
    "CLOB": ColumnType.STRING,
    "NCLOB": ColumnType.STRING,
    "LONG": ColumnType.STRING,  # 遗留 LONG(字符);"LONG RAW" 由前置特判归 BYTES
    "ROWID": ColumnType.STRING,
    "UROWID": ColumnType.STRING,
    "XMLTYPE": ColumnType.STRING,
    # 二进制
    "RAW": ColumnType.BYTES,
    "BLOB": ColumnType.BYTES,
    "BFILE": ColumnType.BYTES,
    # 原生 JSON(Oracle 21c+)
    "JSON": ColumnType.JSON,
}

# python-oracledb DbType.name 去前缀后落到 _NAME_TO_COLUMN_TYPE 的规范化别名:
# 复合名(带 _TZ / _LTZ / LONG_RAW / LONG_NVARCHAR 等)先在此归一到基础桶。
_NORMALIZED_ALIASES: dict[str, str] = {
    "TIMESTAMP_TZ": "TIMESTAMP",
    "TIMESTAMP_LTZ": "TIMESTAMP",
    "LONG_NVARCHAR": "LONG",
    "LONG_VARCHAR": "LONG",
    "LONG_RAW": "RAW",
}

# NUMBER 这类既能承整数也能承小数的类型,需 description.scale 判别。
_SCALE_SENSITIVE_NAMES = ("NUMBER", "NUMERIC", "DECIMAL", "DEC")


def data_type_string_to_column_type(raw: object) -> ColumnType:
    """ALL_TAB_COLUMNS.DATA_TYPE 字符串 → 统一 ColumnType。无法识别落 UNKNOWN。"""
    if not isinstance(raw, str):
        return ColumnType.UNKNOWN
    upper = raw.strip().upper()
    # "LONG RAW" 前缀是 "LONG"(→STRING),但它其实是二进制 → 前置特判归 BYTES。
    if upper.startswith("LONG RAW"):
        return ColumnType.BYTES
    match = _DATA_TYPE_PREFIX_RE.match(raw)
    if match is None:
        return ColumnType.UNKNOWN
    return _NAME_TO_COLUMN_TYPE.get(match.group(1).upper(), ColumnType.UNKNOWN)


def description_item_to_column_type(type_code: object, scale: object = None) -> ColumnType:
    """description 的 (type-object, scale) → 统一 ColumnType。

    NUMBER / DECIMAL 系按 scale 区分:scale==0 → INTEGER,scale>0 或缺失(None)
    → DECIMAL(保守默认)。其余类型与 scale 无关,直接映射。无法识别落 UNKNOWN。
    """
    name = _normalized_type_name(type_code)
    if name is None:
        return ColumnType.UNKNOWN
    base = _NAME_TO_COLUMN_TYPE.get(name, ColumnType.UNKNOWN)
    if base is ColumnType.DECIMAL and name in _SCALE_SENSITIVE_NAMES:
        scale_int = _as_int(scale)
        if scale_int is not None and scale_int == 0:
            return ColumnType.INTEGER
    return base


def description_type_to_driver_name(type_code: object) -> str | None:
    """description 的 type-object → 干净大写类型名(去 "DB_TYPE_" 前缀)。

    拿不到干净名返回 None(宁缺毋滥,不透传 DbType 的原始 repr)。
    """
    return _normalized_type_name(type_code)


def _normalized_type_name(type_code: object) -> str | None:
    """oracledb DbType(或其 .name 字符串)→ 去 "DB_TYPE_" 前缀 + 复合名归一的大写名。"""
    if type_code is None:
        return None
    raw_name = type_code if isinstance(type_code, str) else getattr(type_code, "name", None)
    if not isinstance(raw_name, str):
        return None
    upper = raw_name.strip().upper()
    if not upper:
        return None
    if upper.startswith("DB_TYPE_"):
        upper = upper[len("DB_TYPE_") :]
    return _NORMALIZED_ALIASES.get(upper, upper)


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


__all__ = [
    "data_type_string_to_column_type",
    "description_item_to_column_type",
    "description_type_to_driver_name",
]
