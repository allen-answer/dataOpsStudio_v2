"""错误码表与分类器(设计 §5.4)。

**铁律:只按数值错误码分类,永不按消息文本。** 错误消息语言是生产环境配置项
(DM-SPIKE §4 复核点②);本机实测 dmPython 消息在 Windows 上还会以 GBK 字节
回来变成乱码 —— 文本分类在真机上根本不可靠。

驱动的码藏在哪(本机 dmPython 2.5.32 / PyMySQL 实测):

- dmPython:`exc.args[0]` 是 `dmPython.DmError` 对象,码在 `.code`
  **不是 int 参数** —— 既有 adapter 的 `_first_int_arg` 对 DM 恒取不到码;
- dmPython 建连失败:抛的是 `builtins.SystemError`("returned a result with an
  exception set"),真正的 `DatabaseError` 挂在 `__context__` 上 —— 必须回溯;
- PyMySQL:`OperationalError(errno, message)`,码是第一个 int 参数,部分异常
  另有 `.errno`。

`DmError` 对象在 dmPython 里被复用(多次异常拿到同一地址),所以分类必须
**当场取值**,绝不持有该对象。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from app.dbclients.interactive.protocol import (
    ClassifiedError,
    ErrorCategory,
)
from app.domain.datasource import DbType

_PASSWORD_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(password|passwd|pwd)\s*[:=]\s*['\"]?[^'\",\s)]+['\"]?"
)
_URL_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s'\")]+")
_NUMERIC_RE = re.compile(r"^-?\d+$")
_MAX_ERROR_SUMMARY_LENGTH = 240
_MAX_CAUSE_DEPTH = 5

# ── §5.4 错误码表 ────────────────────────────────────────────────────────────
# 依据: DM 侧 -2501 / -70028 / -6515 / -2104 出自 DM-SPIKE §1-E/§3;
# -2106(无效的表或视图)由本 PR 在同一 DM 8 实例上实测补入,与 -2104(同批新建
# 对象不可见)同属语句级。MySQL 侧为常识码,A7 的 CI 容器用例负责钉死。
#
# ★ DM 的"无效的列名"码**尚未实测**,故意留空:表结构被改后按旧元数据展开 `*`
# 正是这个错误,但没有实例可验的数字不写进码表(写错比缺失更坏 —— 会把别的
# 错误误分类)。缺失的代价仅是该错误落 UNKNOWN 兜底、多一次 ping 往返;
# 错误摘要本身照常带驱动原文与数值码回给用户。拿到实测码后补这一行即可。

DM_ERROR_CODES: Mapping[int, ErrorCategory] = {
    -2501: ErrorCategory.AUTH_FAILED,  # 用户名或密码错误
    -70028: ErrorCategory.HOST_UNREACHABLE,  # 创建 SOCKET 连接失败
    -6515: ErrorCategory.CANCELLED,  # 操作被取消(SP_CANCEL_SESSION_OPERATION)
    -2104: ErrorCategory.STATEMENT_ERROR,  # 无效的表名(同批新建对象不可见)
    -2106: ErrorCategory.STATEMENT_ERROR,  # 无效的表或视图名
}

MYSQL_ERROR_CODES: Mapping[int, ErrorCategory] = {
    1045: ErrorCategory.AUTH_FAILED,  # ER_ACCESS_DENIED_ERROR
    2003: ErrorCategory.HOST_UNREACHABLE,  # CR_CONN_HOST_ERROR
    2005: ErrorCategory.HOST_UNREACHABLE,  # CR_UNKNOWN_HOST
    1317: ErrorCategory.CANCELLED,  # ER_QUERY_INTERRUPTED(KILL QUERY)
    3024: ErrorCategory.TIMEOUT,  # ER_QUERY_TIMEOUT(max_execution_time)
    1054: ErrorCategory.STATEMENT_ERROR,  # ER_BAD_FIELD_ERROR(无效的列名)
    1064: ErrorCategory.STATEMENT_ERROR,  # ER_PARSE_ERROR
    1146: ErrorCategory.STATEMENT_ERROR,  # ER_NO_SUCH_TABLE
    2006: ErrorCategory.CONNECTION_DEAD,  # CR_SERVER_GONE_ERROR
    2013: ErrorCategory.CONNECTION_DEAD,  # CR_SERVER_LOST
}

# server_confirmed 只对"服务器确认的取消/超时"成立(设计 §4.1)。
_SERVER_CONFIRMED_CATEGORIES = frozenset({ErrorCategory.CANCELLED, ErrorCategory.TIMEOUT})

# 连接死亡的类型判据(socket 类异常)—— 与码表互补,同样不看文本。
_CONNECTION_DEAD_TYPES: tuple[type[BaseException], ...] = (
    ConnectionError,
    BrokenPipeError,
    TimeoutError,
    EOFError,
)


class _CodeTableClassifier:
    """按 `db_type` 的码表分类。无状态,可跨 lane 复用。"""

    __slots__ = ("_codes", "db_type")

    def __init__(self, db_type: DbType, codes: Mapping[int, ErrorCategory]) -> None:
        self.db_type = db_type
        self._codes = codes

    def classify(self, exc: BaseException) -> ClassifiedError:
        code = driver_error_code(exc)
        summary = error_summary(exc, code)
        category = self._codes.get(code) if code is not None else None
        if category is None:
            category = _category_from_type(exc)
        return ClassifiedError(
            category=category,
            driver_code=code,
            summary=summary,
            server_confirmed=category in _SERVER_CONFIRMED_CATEGORIES,
        )


class DMErrorClassifier(_CodeTableClassifier):
    """DM 错误分类器(码表 `DM_ERROR_CODES`)。"""

    def __init__(self) -> None:
        super().__init__(DbType.DM, DM_ERROR_CODES)


class MySQLErrorClassifier(_CodeTableClassifier):
    """MySQL 错误分类器(码表 `MYSQL_ERROR_CODES`)。"""

    def __init__(self) -> None:
        super().__init__(DbType.MYSQL, MYSQL_ERROR_CODES)


def _category_from_type(exc: BaseException) -> ErrorCategory:
    """码表未命中时的兜底 —— 只看异常类型,不看文本。

    socket 类异常 → `CONNECTION_DEAD`(§5.4 DM 列"socket 类异常");其余一律
    `UNKNOWN`,由 broker ping 一次再判会话去留(不猜、不假装会话可续用)。
    """
    for candidate in _cause_chain(exc):
        if isinstance(candidate, _CONNECTION_DEAD_TYPES):
            return ErrorCategory.CONNECTION_DEAD
    return ErrorCategory.UNKNOWN


def driver_error_code(exc: BaseException) -> int | None:
    """从驱动异常里挖出数值错误码,挖不到返回 None。

    覆盖三种真实形态(见模块 docstring):dmPython 的 `args[0].code`、
    PyMySQL 的 int 首参 / `.errno`、以及建连期挂在 `__context__` 上的真异常。
    """
    for candidate in _cause_chain(exc):
        code = _code_of(candidate)
        if code is not None:
            return code
    return None


def error_summary(exc: BaseException, code: int | None = None) -> str:
    """脱敏摘要(形态复用既有 `_connection_error_summary`),供 error_summary 列。

    只拼 `类型 + code + 脱敏消息`;消息**仅供人读**,分类一律不看它。
    """
    root = _root_driver_exception(exc)
    parts = [type(root).__name__]
    if code is None:
        code = driver_error_code(exc)
    if code is not None:
        parts.append(f"code={code}")
    message = sanitize_error_text(_message_of(root))
    if message:
        parts.append(f"message={message}")
    return " ".join(parts)


def sanitize_error_text(text: str) -> str:
    """抹掉 password= 赋值与 URL,压空白,截断(与既有 adapter 同口径)。"""
    sanitized = _PASSWORD_ASSIGNMENT_RE.sub(r"\1=***REDACTED***", text)
    sanitized = _URL_RE.sub("<redacted-url>", sanitized)
    sanitized = " ".join(sanitized.split())
    if len(sanitized) > _MAX_ERROR_SUMMARY_LENGTH:
        return f"{sanitized[:_MAX_ERROR_SUMMARY_LENGTH]}..."
    return sanitized


def _cause_chain(exc: BaseException) -> list[BaseException]:
    """异常本身 + `__cause__` / `__context__` 链(有界深度,防环)。

    dmPython 建连失败把真 `DatabaseError` 放在 `SystemError.__context__` 上,
    不回溯就永远拿不到 -2501 / -70028。
    """
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and len(chain) < _MAX_CAUSE_DEPTH:
        if id(current) in seen:
            break
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _root_driver_exception(exc: BaseException) -> BaseException:
    """摘要优先取链上第一个带码的异常(建连期真异常),否则取异常本身。"""
    for candidate in _cause_chain(exc):
        if _code_of(candidate) is not None:
            return candidate
    return exc


def _code_of(exc: BaseException) -> int | None:
    errno = getattr(exc, "errno", None)
    if isinstance(errno, int) and not isinstance(errno, bool):
        return errno
    for arg in getattr(exc, "args", ()):
        code = _code_of_arg(arg)
        if code is not None:
            return code
    return None


def _code_of_arg(arg: Any) -> int | None:
    if isinstance(arg, bool):
        return None
    if isinstance(arg, int):
        return arg
    if isinstance(arg, str):
        # 纯数字串仍是码(某些驱动这么传),不是"按消息文本分类"。
        return int(arg) if _NUMERIC_RE.fullmatch(arg.strip()) else None
    # dmPython.DmError 对象:码在 .code 上。当场取值,绝不持有该对象(驱动复用它)。
    code = getattr(arg, "code", None)
    if isinstance(code, int) and not isinstance(code, bool):
        return code
    return None


def _message_of(exc: BaseException) -> str:
    for arg in getattr(exc, "args", ()):
        message = getattr(arg, "message", None)
        if isinstance(message, str) and message:
            return message
    return str(exc)


__all__ = [
    "DM_ERROR_CODES",
    "MYSQL_ERROR_CODES",
    "DMErrorClassifier",
    "MySQLErrorClassifier",
    "driver_error_code",
    "error_summary",
    "sanitize_error_text",
]
