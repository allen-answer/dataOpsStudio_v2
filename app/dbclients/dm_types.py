"""DM(达梦)driver 类型 → 统一 ColumnType 映射(契约 §3.2 ColumnType)。

DM 方言继承 Oracle(V1_AS_IS §2.3/§2.4),类型体系也基本是 Oracle 风格
(NUMBER / VARCHAR2 / CHAR / DATE / TIMESTAMP / CLOB / BLOB ...),外加 DM 自有
的若干别名(BIGINT / INT / DOUBLE / TEXT 等,DM 兼容多种 SQL 方言)。

两路输入,与 MySQL 同构:

- **SELECT 结果**(`cursor.description[1]`):dmPython type code / type object。
  dmPython 的 description type 形态因版本而异(可能是整数码,也可能是
  type-object,如 `dmPython.STRING`)。`type_code_to_column_type` 优先按
  dmPython 模块上的 DB-API type-object 常量比对,再退整数码;无法识别落 UNKNOWN。
- **all_tab_columns.DATA_TYPE**:字符串,如 `"NUMBER"` / `"VARCHAR2"` /
  `"TIMESTAMP(6)"`。走 `data_type_string_to_column_type`。

★ 无法识别一律 UNKNOWN(不臆造),原始类型由调用方塞进 Column.driver_type。
"""

from __future__ import annotations

import re
from typing import Any

from app.domain.schema import ColumnType

# DATA_TYPE 字符串前缀(取第一个标识符,大写后比对)→ 统一 ColumnType。
# all_tab_columns.DATA_TYPE 形如 "NUMBER" / "VARCHAR2" / "TIMESTAMP(6) WITH TIME ZONE"。
_DATA_TYPE_PREFIX_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_]*)")
_DATA_TYPE_NAME_TO_COLUMN_TYPE: dict[str, ColumnType] = {
    # Oracle/DM 数字:NUMBER 既能整数也能小数,无 scale 信号时保守归 DECIMAL
    "NUMBER": ColumnType.DECIMAL,
    "NUMERIC": ColumnType.DECIMAL,
    "DECIMAL": ColumnType.DECIMAL,
    "DEC": ColumnType.DECIMAL,
    # DM 兼容方言下的明确整数类型
    "INT": ColumnType.INTEGER,
    "INTEGER": ColumnType.INTEGER,
    "BIGINT": ColumnType.INTEGER,
    "SMALLINT": ColumnType.INTEGER,
    "TINYINT": ColumnType.INTEGER,
    "BYTE": ColumnType.INTEGER,
    # 浮点
    "FLOAT": ColumnType.FLOAT,
    "DOUBLE": ColumnType.FLOAT,
    "REAL": ColumnType.FLOAT,
    "BINARY_FLOAT": ColumnType.FLOAT,
    "BINARY_DOUBLE": ColumnType.FLOAT,
    # 布尔(DM 有 BIT/BOOL)
    "BIT": ColumnType.BOOLEAN,
    "BOOL": ColumnType.BOOLEAN,
    "BOOLEAN": ColumnType.BOOLEAN,
    # 时间
    "DATE": ColumnType.DATE,
    "TIME": ColumnType.TIME,
    "DATETIME": ColumnType.DATETIME,
    "TIMESTAMP": ColumnType.DATETIME,
    # 字符串
    "CHAR": ColumnType.STRING,
    "CHARACTER": ColumnType.STRING,
    "VARCHAR": ColumnType.STRING,
    "VARCHAR2": ColumnType.STRING,
    "NCHAR": ColumnType.STRING,
    "NVARCHAR": ColumnType.STRING,
    "NVARCHAR2": ColumnType.STRING,
    "TEXT": ColumnType.STRING,
    "CLOB": ColumnType.STRING,
    "NCLOB": ColumnType.STRING,
    "LONG": ColumnType.STRING,
    # 二进制
    "BINARY": ColumnType.BYTES,
    "VARBINARY": ColumnType.BYTES,
    "BLOB": ColumnType.BYTES,
    "RAW": ColumnType.BYTES,
    "LONGRAW": ColumnType.BYTES,
    "IMAGE": ColumnType.BYTES,
    "BFILE": ColumnType.BYTES,
}

# dmPython DB-API type-object 常量名 → 统一 ColumnType。
# DB-API 2.0 规定 description[1] 可以是 type-object;dmPython 实测暴露
# NUMBER / DECIMAL / BIGINT / DOUBLE / REAL / STRING / FIXED_STRING /
# LONG_STRING / TIMESTAMP / DATE / TIME / LOB / FIXED_BINARY / LONG_BINARY 等。
#
# ★ NUMBER / DECIMAL 在真实例上无法只凭 type-object 区分整数与小数:
#   `INT` 列报 dmPython.NUMBER、`DECIMAL(10,2)` 报 dmPython.DECIMAL,
#   但二者都可能承载整数或小数。真正的判别信号是 description 的 scale
#   (元组 idx5):scale==0 → INTEGER,scale>0 或 None → DECIMAL。
#   故 NUMBER / DECIMAL 此表先记为 DECIMAL(无 scale 时的保守默认),
#   有 scale 信号时由 description_item_to_column_type 覆写为 INTEGER。
_TYPE_OBJECT_NAME_TO_COLUMN_TYPE: dict[str, ColumnType] = {
    # 数字(scale 可覆写为 INTEGER)
    "NUMBER": ColumnType.DECIMAL,
    "DECIMAL": ColumnType.DECIMAL,
    # 明确整数类型(不受 scale 影响)
    "BIGINT": ColumnType.INTEGER,
    # 浮点
    "DOUBLE": ColumnType.FLOAT,
    "REAL": ColumnType.FLOAT,
    # 字符串
    "STRING": ColumnType.STRING,
    "FIXED_STRING": ColumnType.STRING,
    "LONG_STRING": ColumnType.STRING,
    "CLOB": ColumnType.STRING,
    # 时间
    "DATETIME": ColumnType.DATETIME,
    "TIMESTAMP": ColumnType.DATETIME,
    "DATE": ColumnType.DATE,
    "TIME": ColumnType.TIME,
    # 二进制
    "BINARY": ColumnType.BYTES,
    "FIXED_BINARY": ColumnType.BYTES,
    "LONG_BINARY": ColumnType.BYTES,
    "LOB": ColumnType.BYTES,
    "BLOB": ColumnType.BYTES,
}

# 这些 type-object 承载整数还是小数取决于 description.scale,需 scale 判别。
_SCALE_SENSITIVE_TYPE_NAMES = ("NUMBER", "DECIMAL")


def data_type_string_to_column_type(raw: object) -> ColumnType:
    """all_tab_columns.DATA_TYPE 字符串 → 统一 ColumnType。无法识别落 UNKNOWN。"""
    if not isinstance(raw, str):
        return ColumnType.UNKNOWN
    match = _DATA_TYPE_PREFIX_RE.match(raw)
    if match is None:
        return ColumnType.UNKNOWN
    return _DATA_TYPE_NAME_TO_COLUMN_TYPE.get(match.group(1).upper(), ColumnType.UNKNOWN)


def type_code_to_column_type(dm_module: Any, code: object) -> ColumnType:
    """dmPython description 的 type code / type-object → 统一 ColumnType。

    先按 dmPython 模块上的 DB-API type-object 常量逐个 == 比对(覆盖
    description[1] 为 type-object 的情形);命中即返回。否则,若 code 是
    整数,尝试按 dmPython 上同名整数常量比对。都不中落 UNKNOWN。
    """
    if code is None:
        return ColumnType.UNKNOWN

    if dm_module is not None:
        for name, column_type in _TYPE_OBJECT_NAME_TO_COLUMN_TYPE.items():
            constant = getattr(dm_module, name, None)
            if constant is None:
                continue
            try:
                if code == constant or code is constant:
                    return column_type
            except Exception:  # pragma: no cover - 异常 __eq__ 的极端兜底
                continue

    return ColumnType.UNKNOWN


def description_item_to_column_type(
    dm_module: Any, code: object, scale: object = None
) -> ColumnType:
    """dmPython description 的 (type-object, scale) → 统一 ColumnType。

    在 `type_code_to_column_type` 基础上加 scale 判别:NUMBER / DECIMAL 这类
    既能承载整数也能承载小数的类型,按 description 的 scale 区分 ——
    scale==0 → INTEGER,scale>0 或 scale 缺失(None)→ DECIMAL(保守默认)。
    其余类型(BIGINT / DOUBLE / STRING / TIMESTAMP ...)与 scale 无关,直接映射。
    """
    base = type_code_to_column_type(dm_module, code)
    if base is not ColumnType.DECIMAL:
        return base
    # 仅 scale-敏感类型(NUMBER / DECIMAL)且 scale==0 时改判 INTEGER。
    if not _code_is_scale_sensitive(dm_module, code):
        return base
    scale_int = _as_int(scale)
    if scale_int is not None and scale_int == 0:
        return ColumnType.INTEGER
    return ColumnType.DECIMAL


def _code_is_scale_sensitive(dm_module: Any, code: object) -> bool:
    if dm_module is None:
        return False
    for name in _SCALE_SENSITIVE_TYPE_NAMES:
        constant = getattr(dm_module, name, None)
        if constant is None:
            continue
        try:
            if code == constant or code is constant:
                return True
        except Exception:  # pragma: no cover - 异常 __eq__ 的极端兜底
            continue
    return False


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


__all__ = [
    "data_type_string_to_column_type",
    "description_item_to_column_type",
    "type_code_to_column_type",
]
