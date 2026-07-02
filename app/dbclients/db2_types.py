"""DB2 driver / SYSCAT 类型 → 统一 ColumnType 映射(契约 §3.2 ColumnType)。

两条入口:
1. `description_type_to_*`(PR-A):cursor.description[1](字符串或
   DB-API type-object)→ 保守归类,查询结果列用;
2. `syscat_typename_to_*`(PR-B):SYSCAT.COLUMNS.TYPENAME(内置类型名
   字符串,如 "VARCHAR" / "LONG VARCHAR" / "DECFLOAT")→ 全量映射,
   introspection(list_columns)用。

ibm_db_dbi 的 `cursor.description[1]` 形态因版本而异:
- 可能是小写类型名字符串(`ibm_db.field_type` 直出,如 "string" / "int" /
  "timestamp");
- 也可能是 DB-API type-object(`ibm_db_dbi.STRING` 等,frozenset 子类,
  成员是若干大写类型名,另带 `col_types` 属性)。

两种形态都归一成大写类型名集合再查表。**driver_type 必须是干净类型名**
(如 "VARCHAR" / "BIGINT"),不许透传 frozenset/type-object 的原始 repr
(DM adapter 当年踩过 driver_type 塞 repr 的坑)。

★ 无法识别一律 UNKNOWN(不臆造)。
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from app.domain.schema import ColumnType

_TYPE_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")

# 大写类型名 → 统一 ColumnType。
# ★ 顺序即匹配优先级:ibm_db_dbi 的 type-object 是"类型名集合"
# (如 NUMBER ⊇ {SMALLINT, INTEGER, DECIMAL, ...}),一个集合可能命中多个桶,
# 按保守原则把 DECIMAL 系排最前(数值歧义时归 DECIMAL 不丢精度),
# TIMESTAMP/DATETIME 排在 DATE/TIME 前(时间歧义时归 DATETIME 不丢时分秒)。
_NAME_TO_COLUMN_TYPE: dict[str, ColumnType] = {
    # 数值 —— DECIMAL 系优先(保守)
    "DECIMAL": ColumnType.DECIMAL,
    "NUMERIC": ColumnType.DECIMAL,
    "DEC": ColumnType.DECIMAL,
    "DECFLOAT": ColumnType.DECIMAL,
    "NUMBER": ColumnType.DECIMAL,
    "DOUBLE": ColumnType.FLOAT,
    "FLOAT": ColumnType.FLOAT,
    "REAL": ColumnType.FLOAT,
    "BIGINT": ColumnType.INTEGER,
    "INTEGER": ColumnType.INTEGER,
    "INT": ColumnType.INTEGER,
    "SMALLINT": ColumnType.INTEGER,
    # 布尔
    "BOOLEAN": ColumnType.BOOLEAN,
    # 时间 —— TIMESTAMP/DATETIME 优先(保守)
    "TIMESTAMP": ColumnType.DATETIME,
    "DATETIME": ColumnType.DATETIME,
    "DATE": ColumnType.DATE,
    "TIME": ColumnType.TIME,
    # 二进制
    "BLOB": ColumnType.BYTES,
    "BINARY": ColumnType.BYTES,
    "VARBINARY": ColumnType.BYTES,
    # 字符串(含 XML/CLOB 大对象与 GRAPHIC 家族;"LONG VARCHAR" 归一后是 LONG)
    "XML": ColumnType.STRING,
    "CLOB": ColumnType.STRING,
    "DBCLOB": ColumnType.STRING,
    "CHAR": ColumnType.STRING,
    "CHARACTER": ColumnType.STRING,
    "VARCHAR": ColumnType.STRING,
    "LONGVARCHAR": ColumnType.STRING,
    "LONG": ColumnType.STRING,
    "GRAPHIC": ColumnType.STRING,
    "VARGRAPHIC": ColumnType.STRING,
    "LONGVARGRAPHIC": ColumnType.STRING,
    "STRING": ColumnType.STRING,
    "TEXT": ColumnType.STRING,
}


# SYSCAT.COLUMNS.TYPENAME(DB2 LUW 内置类型全量,IBM SYSCAT.DATATYPES 文档口径)
# → 统一 ColumnType(DB2 PR-B introspection 用)。
# TYPENAME 是干净的大写类型名字符串;带空格的复合名("LONG VARCHAR" /
# "DOUBLE PRECISION")按全名精确匹配,精确不中再按首词回退
# ("TIMESTAMP WITH TIME ZONE" 之类带修饰的名字归 TIMESTAMP 桶)。
_SYSCAT_TYPENAME_TO_COLUMN_TYPE: dict[str, ColumnType] = {
    # 字符串系(含 GRAPHIC 双字节家族与 CLOB/DBCLOB/XML 大对象)
    "CHAR": ColumnType.STRING,
    "CHARACTER": ColumnType.STRING,
    "VARCHAR": ColumnType.STRING,
    "LONG VARCHAR": ColumnType.STRING,
    "CLOB": ColumnType.STRING,
    "GRAPHIC": ColumnType.STRING,
    "VARGRAPHIC": ColumnType.STRING,
    "LONG VARGRAPHIC": ColumnType.STRING,
    "DBCLOB": ColumnType.STRING,
    "NCHAR": ColumnType.STRING,
    "NVARCHAR": ColumnType.STRING,
    "NCLOB": ColumnType.STRING,
    "XML": ColumnType.STRING,
    # 数值系
    "SMALLINT": ColumnType.INTEGER,
    "INTEGER": ColumnType.INTEGER,
    "INT": ColumnType.INTEGER,
    "BIGINT": ColumnType.INTEGER,
    "DECIMAL": ColumnType.DECIMAL,
    "DEC": ColumnType.DECIMAL,
    "NUMERIC": ColumnType.DECIMAL,
    "DECFLOAT": ColumnType.DECIMAL,
    "REAL": ColumnType.FLOAT,
    "FLOAT": ColumnType.FLOAT,
    "DOUBLE": ColumnType.FLOAT,
    "DOUBLE PRECISION": ColumnType.FLOAT,
    # 布尔(DB2 11.1+)
    "BOOLEAN": ColumnType.BOOLEAN,
    # 时间系
    "DATE": ColumnType.DATE,
    "TIME": ColumnType.TIME,
    "TIMESTAMP": ColumnType.DATETIME,
    # 二进制系(BINARY/VARBINARY 是 DB2 11.1+;BLOB 全版本)
    "BLOB": ColumnType.BYTES,
    "BINARY": ColumnType.BYTES,
    "VARBINARY": ColumnType.BYTES,
}


def syscat_typename_to_column_type(typename: object) -> ColumnType:
    """SYSCAT.COLUMNS.TYPENAME → 统一 ColumnType。

    distinct UDT(TYPENAME 是用户类型名)等无法识别的一律 UNKNOWN(不臆造,
    driver_type 保留原名供人工核对)。
    """
    normalized = syscat_typename_to_driver_name(typename)
    if normalized is None:
        return ColumnType.UNKNOWN
    mapped = _SYSCAT_TYPENAME_TO_COLUMN_TYPE.get(normalized)
    if mapped is not None:
        return mapped
    # 带修饰的复合名回退按首词归类("TIMESTAMP WITH TIME ZONE" → TIMESTAMP)
    head = normalized.split(" ", 1)[0]
    return _SYSCAT_TYPENAME_TO_COLUMN_TYPE.get(head, ColumnType.UNKNOWN)


def syscat_typename_to_driver_name(typename: object) -> str | None:
    """SYSCAT.COLUMNS.TYPENAME → 干净大写类型名(空白折叠 + strip + upper)。

    SYSCAT 老版本 CHAR 定长列右补空格,这里统一归一;拿不到非空名返回 None
    (driver_type 宁缺毋滥,不塞原始 repr —— DM 踩过的坑)。
    """
    if typename is None:
        return None
    text = " ".join(str(typename).split()).upper()
    return text or None


def description_type_to_column_type(type_code: object) -> ColumnType:
    """description[1](字符串或 type-object)→ 统一 ColumnType。无法识别落 UNKNOWN。"""
    names = _normalized_names(type_code)
    for known_name, column_type in _NAME_TO_COLUMN_TYPE.items():
        if known_name in names:
            return column_type
    return ColumnType.UNKNOWN


def description_type_to_driver_name(type_code: object) -> str | None:
    """description[1] → 干净的 driver 类型名(如 "VARCHAR"),不透传原始 repr。

    单一字符串取其首个标识符大写;类型名集合取映射表中最先命中的名字;
    拿不到干净名一律 None(宁缺毋滥,不塞 frozenset repr)。
    """
    candidates = _candidate_names(type_code)
    if len(candidates) == 1:
        return _normalize(candidates[0])
    names = _normalized_names(type_code)
    for known_name in _NAME_TO_COLUMN_TYPE:
        if known_name in names:
            return known_name
    return None


def _candidate_names(type_code: object) -> list[str]:
    if isinstance(type_code, str):
        return [type_code]
    # ibm_db_dbi.DBAPITypeObject 是 frozenset 子类(成员为类型名字符串)
    if isinstance(type_code, (frozenset, set, tuple, list)):
        return sorted(item for item in type_code if isinstance(item, str))
    col_types = getattr(type_code, "col_types", None)
    if isinstance(col_types, Iterable) and not isinstance(col_types, (str, bytes)):
        return sorted(item for item in col_types if isinstance(item, str))
    name = getattr(type_code, "name", None)
    if isinstance(name, str):
        return [name]
    return []


def _normalized_names(type_code: object) -> set[str]:
    names: set[str] = set()
    for candidate in _candidate_names(type_code):
        normalized = _normalize(candidate)
        if normalized is not None:
            names.add(normalized)
    return names


def _normalize(name: str) -> str | None:
    match = _TYPE_NAME_RE.search(name)
    return match.group(0).upper() if match else None


__all__ = [
    "description_type_to_column_type",
    "description_type_to_driver_name",
    "syscat_typename_to_column_type",
    "syscat_typename_to_driver_name",
]
