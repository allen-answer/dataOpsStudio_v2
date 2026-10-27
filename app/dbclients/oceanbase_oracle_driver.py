"""OB-Oracle 驱动接线:PyMySQL + Oracle 模式能力位 + 扩展类型解码 + Oracle 转义。

OceanBase Oracle 租户走 MySQL 协议,但有三处 PyMySQL 原样用不了(设计稿 §4.3,POC 结论):

1. **握手**:不置 capability bit 27(OB_CLIENT_SUPPORT_ORACLE_MODE)登录即报
   `1235 Oracle tenant for current client driver is not supported`。置位后正常。
   社区包 pyoceanbase 也只做了这一位,故不引入它(方案 A)。
2. **字段解码**:PyMySQL 对不在 TEXT_TYPES 里的类型码一律按 ascii 解码,再交给 converter。
   OB 的 NVARCHAR2/NCHAR(207/208)含中文、时间戳/INTERVAL(200 至 205)的二进制值含
   >=0x80 字节时都会 UnicodeDecodeError,且异常发生在读包中途,**连接随即包序号错乱、
   后续语句全部失败**。所以必须接管 `MySQLResult._get_descriptions` 的逐列编码选择,
   只注册 converter 不够。不改 `pymysql.connections.TEXT_TYPES` 这类模块级全局
   (会波及同进程的 MySQL 连接)。
3. **转义**:服务端不报 NO_BACKSLASH_ESCAPES,PyMySQL 缺省把 `'` 转成 `\'`;
   Oracle 方言里反斜杠是普通字符,结果是**所有含单引号的绑定值都报 900 语法错**。
   改为 Oracle 规则(只双写单引号)。

依赖 PyMySQL 私有方法(`MySQLResult._get_descriptions` / `Connection._read_query_result`),
PyMySQL 已在 uv.lock 锁版本;单测按真实 PyMySQL 类快照这条路径。

R1:PyMySQL 只在 app/dbclients/ 内 import(由调用方注入或惰性加载)。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

from app.dbclients.oceanbase_oracle_types import (
    BINARY_DECODED_TYPES,
    DECODERS,
    OB_NUMBER_FLOAT,
    TEXT_DECODED_TYPES,
    decode_number_float,
)

# MySQL 8 把 bit 27 用作 CLIENT_QUERY_ATTRIBUTES;OB 复用它表示“客户端支持 Oracle 模式”。
OB_CLIENT_SUPPORT_ORACLE_MODE = 1 << 27

_DEFAULT_ACCESS_MODE = "proxy"
_ACCESS_MODES = frozenset({"proxy", "direct"})


class ObOracleConfigError(ValueError):
    """OB-Oracle 连接参数不完整或不合法(租户 / 集群 / 访问方式)。"""


def assemble_username(username: str, extra: Mapping[str, Any]) -> str:
    """表单里只填纯用户名,这里按访问方式拼成 OB 登录名(设计稿 §3.4)。

    - proxy(经 OBProxy,缺省):`user@tenant#cluster`
    - direct(直连 observer):`user@tenant`
    username 里已含 `@` 视为拼好的完整串原样使用(兼容存量写法),此时 tenant/cluster 可省。
    """
    if "@" in username:
        return username
    access_mode = str(extra.get("access_mode") or _DEFAULT_ACCESS_MODE).lower()
    if access_mode not in _ACCESS_MODES:
        raise ObOracleConfigError("access_mode must be 'proxy' or 'direct'")
    tenant = str(extra.get("tenant") or "").strip()
    if not tenant:
        raise ObOracleConfigError("OceanBase tenant is required (extra.tenant)")
    if access_mode == "direct":
        return f"{username}@{tenant}"
    cluster = str(extra.get("cluster") or "").strip()
    if not cluster:
        raise ObOracleConfigError(
            "OceanBase cluster is required when connecting through OBProxy (extra.cluster)"
        )
    return f"{username}@{tenant}#{cluster}"


def escape_literal(value: object) -> str:
    """把 Python 值渲染成 Oracle 方言的 SQL 字面量(PyMySQL 客户端插值用)。"""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"HEXTORAW('{bytes(value).hex().upper()}')"
    if isinstance(value, datetime):
        text = value.replace(tzinfo=None).isoformat(sep=" ", timespec="microseconds")
        return f"TO_TIMESTAMP('{text}', 'YYYY-MM-DD HH24:MI:SS.FF6')"
    if isinstance(value, date):
        return f"DATE '{value.isoformat()}'"
    if isinstance(value, (list, tuple, set, frozenset)):
        return "(" + ",".join(escape_literal(item) for item in value) + ")"
    if isinstance(value, (int, float)):
        return repr(value)
    # Decimal 等:str 即合法数字字面量
    return str(value)


def build_connection_class(pymysql: Any) -> Any:
    """基于给定的 pymysql 模块派生 OB-Oracle 专用 Connection 类。"""
    connections = pymysql.connections

    class _ObOracleResult(connections.MySQLResult):  # type: ignore[misc,name-defined]
        def _get_descriptions(self) -> None:
            super()._get_descriptions()
            if not self.connection.use_unicode:
                return
            text_encoding = self.connection.encoding
            for index, field in enumerate(self.fields):
                type_code = field.type_code
                if type_code in BINARY_DECODED_TYPES:
                    self.converters[index] = (None, DECODERS.get(type_code))
                elif type_code in TEXT_DECODED_TYPES:
                    self.converters[index] = (text_encoding, None)
                elif type_code == OB_NUMBER_FLOAT:
                    self.converters[index] = ("ascii", decode_number_float)

    class ObOracleConnection(connections.Connection):  # type: ignore[misc,name-defined]
        result_class = _ObOracleResult

        # 与 PyMySQL Connection._read_query_result 同构,只把结果类换成 _ObOracleResult。
        def _read_query_result(self, unbuffered: bool = False) -> int:
            self._result = None
            result = self.result_class(self)
            if unbuffered:
                result.init_unbuffered_query()
            else:
                result.read()
            self._result = result
            if result.server_status is not None:
                self.server_status = result.server_status
            return int(result.affected_rows or 0)

        def escape(self, obj: object, mapping: object = None) -> str:
            return escape_literal(obj)

        def escape_string(self, s: str) -> str:
            return s.replace("'", "''")

    return ObOracleConnection


def connect(pymysql: Any, **kwargs: Any) -> Any:
    """建 OB-Oracle 连接:置 Oracle 模式能力位,其余参数同 pymysql.connect。"""
    connection_class = build_connection_class(pymysql)
    client_flag = int(kwargs.pop("client_flag", 0)) | OB_CLIENT_SUPPORT_ORACLE_MODE
    return connection_class(client_flag=client_flag, **kwargs)


def field_charsets(cursor: Any) -> list[int | None]:
    """当前结果集各列的 charsetnr(区分 CLOB / BLOB 用;拿不到返回空表)。"""
    result = getattr(cursor, "_result", None)
    fields = getattr(result, "fields", None) or []
    return [getattr(field, "charsetnr", None) for field in fields]


__all__ = [
    "OB_CLIENT_SUPPORT_ORACLE_MODE",
    "ObOracleConfigError",
    "assemble_username",
    "build_connection_class",
    "connect",
    "escape_literal",
    "field_charsets",
]
