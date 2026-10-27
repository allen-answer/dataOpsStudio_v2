"""OceanBase Oracle 模式在 MySQL 协议里的扩展类型码 → 解码器 / 统一 ColumnType。

OB-Oracle 走 MySQL 协议(文本协议),但 Oracle 专有类型用了 MySQL 没有的类型码。字节布局
是 POC 在 OB 4.3.5.4 上逐类型抓包、并与同值的 TO_CHAR(...) 逐项核对得出的(含 0001 年、
9999 年、负时区 -03:30、区域名时区、负 INTERVAL),见设计稿 §4.3.4:

- 202 TIMESTAMP(n):世纪、年、月、日、时、分、秒各 1 字节 + 纳秒 4 字节小端 + 精度 1 字节
- 201 TIMESTAMP WITH LOCAL TIME ZONE:同 202,已换算到会话时区
- 200 TIMESTAMP WITH TIME ZONE:同 202 + 时区时(有符号 1 字节)+ 时区分(1 字节,符号随时)
  + 区域名(长度前缀)+ 缩写(长度前缀)
- 203 RAW:原始字节
- 204 INTERVAL YEAR TO MONTH:符号 1 + 年 4 小端 + 月 1 + 年精度 1
- 205 INTERVAL DAY TO SECOND:符号 1 + 天 4 小端 + 时/分/秒各 1 + 纳秒 4 小端
  + 天精度 1 + 秒精度 1
- 206 FLOAT(b)(Oracle 二进制精度 NUMBER):十进制文本
- 207 / 208 NVARCHAR2 / NCHAR:连接字符集文本

其余沿用 MySQL 码:NUMBER→246(NEWDECIMAL)、DATE→12(DATETIME 文本,带时分秒)、
VARCHAR2/CHAR→253/254、CLOB/BLOB→251(charsetnr 63 为 BLOB)、BINARY_FLOAT/DOUBLE→4/5。

纯函数,不 import 驱动。
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.domain.schema import ColumnType

OB_TIMESTAMP_TZ = 200
OB_TIMESTAMP_LTZ = 201
OB_TIMESTAMP_NANO = 202
OB_RAW = 203
OB_INTERVAL_YM = 204
OB_INTERVAL_DS = 205
OB_NUMBER_FLOAT = 206
OB_NVARCHAR2 = 207
OB_NCHAR = 208

# MySQL 协议里 OB-Oracle 仍在用的标准码(与 pymysql.constants.FIELD_TYPE 数值一致)。
_FLOAT = 4
_DOUBLE = 5
_NULL = 6
_DATETIME = 12
_NEWDECIMAL = 246
_LONG_BLOB = 251
_VAR_STRING = 253
_STRING = 254
_BINARY_CHARSET = 63

# 需要以原始字节解码的类型(值里有 >=0x80 的字节,PyMySQL 的 ascii 解码会崩并毁掉连接)。
BINARY_DECODED_TYPES = frozenset(
    {OB_TIMESTAMP_TZ, OB_TIMESTAMP_LTZ, OB_TIMESTAMP_NANO, OB_RAW, OB_INTERVAL_YM, OB_INTERVAL_DS}
)
# 需要按连接字符集解码的文本类型(PyMySQL 不认识这两个码,缺省按 ascii)。
TEXT_DECODED_TYPES = frozenset({OB_NVARCHAR2, OB_NCHAR})


class ObValueDecodeError(ValueError):
    """扩展类型的原始字节与已知布局不符。"""


def decode_timestamp(raw: bytes) -> datetime:
    """202 / 201:不带时区的时间戳(微秒精度,纳秒余数截断)。"""
    year, month, day, hour, minute, second, nanos = _timestamp_fields(raw)
    return datetime(year, month, day, hour, minute, second, nanos // 1000)


def decode_timestamp_tz(raw: bytes) -> datetime:
    """200:带固定偏移的时间戳。区域名(如 Asia/Shanghai)只用于显示,值按偏移还原。"""
    year, month, day, hour, minute, second, nanos = _timestamp_fields(raw)
    if len(raw) < 14:
        raise ObValueDecodeError("OB TIMESTAMP WITH TIME ZONE value too short")
    tz_hour = struct.unpack("b", raw[12:13])[0]
    tz_minute = raw[13]
    sign = -1 if tz_hour < 0 else 1
    offset = timedelta(hours=tz_hour, minutes=sign * tz_minute)
    return datetime(year, month, day, hour, minute, second, nanos // 1000, tzinfo=timezone(offset))


def decode_interval_ym(raw: bytes) -> str:
    """204:返回与 TO_CHAR 一致的规范文本,如 `+01-02` / `-012-11`。"""
    if len(raw) < 7:
        raise ObValueDecodeError("OB INTERVAL YEAR TO MONTH value too short")
    negative = raw[0] != 0
    years = struct.unpack("<I", raw[1:5])[0]
    months = raw[5]
    precision = raw[6]
    return f"{'-' if negative else '+'}{years:0{precision}d}-{months:02d}"


def decode_interval_ds(raw: bytes) -> timedelta:
    """205:与 python-oracledb 一致,返回 timedelta(纳秒余数截断到微秒)。"""
    if len(raw) < 14:
        raise ObValueDecodeError("OB INTERVAL DAY TO SECOND value too short")
    negative = raw[0] != 0
    days = struct.unpack("<I", raw[1:5])[0]
    hours, minutes, seconds = raw[5], raw[6], raw[7]
    nanos = struct.unpack("<I", raw[8:12])[0]
    value = timedelta(
        days=days, hours=hours, minutes=minutes, seconds=seconds, microseconds=nanos // 1000
    )
    return -value if negative else value


def decode_number_float(text: str) -> Decimal:
    """206:Oracle FLOAT(b) 本质是 NUMBER,按 Decimal 取,不丢精度。"""
    return Decimal(text)


def _timestamp_fields(raw: bytes) -> tuple[int, int, int, int, int, int, int]:
    if len(raw) < 12:
        raise ObValueDecodeError("OB timestamp value too short")
    century, year, month, day, hour, minute, second = raw[:7]
    nanos = struct.unpack("<I", raw[7:11])[0]
    return century * 100 + year, month, day, hour, minute, second, nanos


# 类型码 → 解码器(输入:原始 bytes 或已按连接字符集解出的 str)。
DECODERS: dict[int, Callable[..., object]] = {
    OB_TIMESTAMP_TZ: decode_timestamp_tz,
    OB_TIMESTAMP_LTZ: decode_timestamp,
    OB_TIMESTAMP_NANO: decode_timestamp,
    OB_INTERVAL_YM: decode_interval_ym,
    OB_INTERVAL_DS: decode_interval_ds,
    OB_NUMBER_FLOAT: decode_number_float,
}

_TYPE_NAMES: dict[int, str] = {
    _FLOAT: "BINARY_FLOAT",
    _DOUBLE: "BINARY_DOUBLE",
    _NULL: "NULL",
    _DATETIME: "DATE",
    _NEWDECIMAL: "NUMBER",
    _VAR_STRING: "VARCHAR2",
    _STRING: "CHAR",
    OB_TIMESTAMP_TZ: "TIMESTAMP WITH TIME ZONE",
    OB_TIMESTAMP_LTZ: "TIMESTAMP WITH LOCAL TIME ZONE",
    OB_TIMESTAMP_NANO: "TIMESTAMP",
    OB_RAW: "RAW",
    OB_INTERVAL_YM: "INTERVAL YEAR TO MONTH",
    OB_INTERVAL_DS: "INTERVAL DAY TO SECOND",
    OB_NUMBER_FLOAT: "FLOAT",
    OB_NVARCHAR2: "NVARCHAR2",
    OB_NCHAR: "NCHAR",
}

_COLUMN_TYPES: dict[int, ColumnType] = {
    _FLOAT: ColumnType.FLOAT,
    _DOUBLE: ColumnType.FLOAT,
    _DATETIME: ColumnType.DATETIME,  # Oracle DATE 带时分秒
    _VAR_STRING: ColumnType.STRING,
    _STRING: ColumnType.STRING,
    OB_TIMESTAMP_TZ: ColumnType.DATETIME,
    OB_TIMESTAMP_LTZ: ColumnType.DATETIME,
    OB_TIMESTAMP_NANO: ColumnType.DATETIME,
    OB_RAW: ColumnType.BYTES,
    OB_INTERVAL_YM: ColumnType.STRING,
    OB_INTERVAL_DS: ColumnType.STRING,
    OB_NUMBER_FLOAT: ColumnType.DECIMAL,
    OB_NVARCHAR2: ColumnType.STRING,
    OB_NCHAR: ColumnType.STRING,
}


def field_to_column_type(
    type_code: object, *, scale: object = None, charsetnr: int | None = None
) -> ColumnType:
    """结果集字段 → 统一 ColumnType。NUMBER 按 scale 判整数;251 按 charsetnr 分 CLOB/BLOB。"""
    if not isinstance(type_code, int):
        return ColumnType.UNKNOWN
    if type_code == _NEWDECIMAL:
        return ColumnType.INTEGER if scale == 0 else ColumnType.DECIMAL
    if type_code == _LONG_BLOB:
        return ColumnType.BYTES if charsetnr == _BINARY_CHARSET else ColumnType.STRING
    return _COLUMN_TYPES.get(type_code, ColumnType.UNKNOWN)


def field_type_name(type_code: object, *, charsetnr: int | None = None) -> str | None:
    """结果集字段 → 干净的 Oracle 类型名(放 Column.driver_type);未知码返回 None。"""
    if not isinstance(type_code, int):
        return None
    if type_code == _LONG_BLOB:
        return "BLOB" if charsetnr == _BINARY_CHARSET else "CLOB"
    return _TYPE_NAMES.get(type_code)


__all__ = [
    "BINARY_DECODED_TYPES",
    "DECODERS",
    "OB_INTERVAL_DS",
    "OB_INTERVAL_YM",
    "OB_NCHAR",
    "OB_NUMBER_FLOAT",
    "OB_NVARCHAR2",
    "OB_RAW",
    "OB_TIMESTAMP_LTZ",
    "OB_TIMESTAMP_NANO",
    "OB_TIMESTAMP_TZ",
    "TEXT_DECODED_TYPES",
    "ObValueDecodeError",
    "decode_interval_ds",
    "decode_interval_ym",
    "decode_number_float",
    "decode_timestamp",
    "decode_timestamp_tz",
    "field_to_column_type",
    "field_type_name",
]
