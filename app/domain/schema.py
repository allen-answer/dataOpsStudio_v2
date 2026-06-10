from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict


class Row(BaseModel):
    """Database row(positional,顺序匹配 Column 列表)。

    values 是 Any list —— DB 类型因驱动而异,adapter 内做转换,
    domain 层保持开放(避免 MySQL/Oracle/DM 类型差异固化进 domain)。
    """

    values: list[Any]


class ColumnType(StrEnum):
    """跨方言统一列类型(契约 §3.2 ColumnType)。

    各 adapter 把 driver-specific 类型码(PyMySQL FIELD_TYPE 整数 /
    dmPython type code 等)映射到这 11 个统一值,前端按此一套语义
    染色 / cast / format,不再做 N 方言条件分支。原始 driver 类型字符串
    放 Column.driver_type 备查。

    无法识别的类型码一律落 UNKNOWN(不臆造,留 driver_type 供人工核对)。
    """

    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    DATETIME = "datetime"
    DATE = "date"
    TIME = "time"
    BYTES = "bytes"
    JSON = "json"
    UNKNOWN = "unknown"


class Column(BaseModel):
    name: str
    type: ColumnType = ColumnType.UNKNOWN  # 跨方言统一枚举(前端按此染色)
    driver_type: str | None = None  # 原始 driver 类型,如 "VARCHAR(64)" / "NUMBER(10,2)"
    nullable: bool = True
    primary_key: bool = False


class Schema(BaseModel):
    """逻辑 schema —— 在 MySQL 等价于"database",在 Oracle/DM/PG 等价于"schema"。

    注:字段名用 `name` 而非 `schema`,避免与 Pydantic BaseModel 内部
    `model_schema` 概念混淆;调用方写 `Schema(name="HR")`。
    """

    model_config = ConfigDict(frozen=True)

    name: str


class Table(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_name: str
    name: str
    table_type: str | None = None  # "TABLE" / "VIEW" / 等,adapter 决定


class Index(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    columns: list[str]
    is_unique: bool = False
    is_primary: bool = False
