"""DB2(ibm_db_dbi)driver 类型 → 统一 ColumnType 保守映射(契约 §3.2 ColumnType)。

PR-A 范围:只做字符串 / 数值 / 时间 / 二进制 / 布尔 的保守归类,
细化类型映射表(GRAPHIC 家族、DECFLOAT 精度语义等)留 DB2 PR-B。

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
]
