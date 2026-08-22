"""会话式(交互)连接缝的协议与值对象(Session Broker 设计 §1.3/§4/§5.4)。

**这一层是什么**:Session Broker 的 lane 线程持有的"持久会话连接"、control lane
持有的"每 datasource 一条控制连接",以及把驱动异常翻译成分类码的错误分类器。

**这一层不是什么**:不是 `DatabaseAdapter` 的替代。job 路径(每 job 新建连接、
用完即弃、纯软取消)的既有 adapter **一行不改** —— 设计 §1.2"共享深模块,不共享
连接":共享类型映射与错误码口径,不共享连接与执行路径。

编排职责边界(本缝只提供原语,策略在 `app/broker/`):

- 取消阶梯(软 flag → 硬取消 → 宽限期 → destroy → 放弃连接)由 broker 走,
  本缝提供 `request_soft_cancel()` / `CancelChannel.cancel()` / `.destroy()`;
- 语句时限由 broker 客户端计时(§4.2 —— DM 无服务端语句超时属性),本缝只负责
  MySQL 服务端保险带 `max_execution_time` 的逐语句同步(评审修订 R2);
- 取消栅栏(R1:在途取消未回报期间不起下一条语句)是 broker 的 lane 不变量;
- 只读校验(`validate_readonly_sql`)由 API/broker 层施加,本缝不重复施加 ——
  写阶段(阶段 B)同一条连接要执行 DML/DDL,把只读策略焊进缝里等于埋返工。

R5:本模块及实现**不写日志**;事件由 broker 落 `console_statement_events`
(仅 sql_hash + 长度)。R2:只持 `SecretRef`,明文只在 connect 内部短暂存在。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from app.domain.datasource import DbType
from app.domain.schema import Column, Row


class ErrorCategory(StrEnum):
    """错误分类(设计 §4.1 三分 + §5.4 码表的连接期归因)。

    **只按数值错误码分类,永不按消息文本** —— 错误消息语言是生产环境配置项
    (依据: DM-SPIKE §4 复核点②;本机实测 dmPython 的消息在 Windows 上还会以
    GBK 字节回来变成乱码,文本分类在真机上根本不可靠)。
    """

    AUTH_FAILED = "auth_failed"
    HOST_UNREACHABLE = "host_unreachable"
    LOGIN_TIMEOUT = "login_timeout"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    STATEMENT_ERROR = "statement_error"
    CONNECTION_DEAD = "connection_dead"
    # 码表未覆盖。调用方不得据此宣告会话可续用:broker 必须 ping 一次再决定
    # 语句归 failed 还是会话归 session_lost(设计 §4.1"任何一步失败都如实上报")。
    UNKNOWN = "unknown"


class ServerCancelSupport(StrEnum):
    """`console_sessions.server_cancel` 的取值(设计 §5.1)。"""

    AVAILABLE = "available"
    # 探测失败(典型:生产受限账号无 SP_CANCEL 权限,DM-SPIKE §4①)。取消降级为
    # "软取消 + destroy 兜底",attach 响应如实携带,前端明示"取消将断开会话"。
    DEGRADED = "degraded"
    # 尚未探测(控制连接未建立)。
    UNKNOWN = "unknown"


class InteractiveCapabilities(BaseModel):
    """会话缝的能力矩阵(设计 §5.4)。

    ★ 与 `AdapterCapabilities` **独立**:后者的 `server_side_cancel=False`
    描述的是 job 路径软取消现状,不改也不复用。
    """

    # 控制连接可发硬取消(DM SP_CANCEL_SESSION_OPERATION / MySQL KILL QUERY)。
    # 这是**方言能力**;某个 datasource 的实际权限见 ServerCancelSupport。
    server_cancel: bool

    # 服务端语句超时属性可用(MySQL max_execution_time=True;DM=False ——
    # connection_timeout 实证不是执行超时,DM-SPIKE §3)。
    server_statement_timeout: bool

    # execute 不全量缓冲、fetchmany 分批(DM/MySQL 均 True,DM-SPIKE §1-Q7)。
    session_streaming: bool


@dataclass(frozen=True, slots=True)
class ClassifiedError:
    """驱动异常的分类结果。字段只来自数值码与异常类型,不含 SQL 原文与密码。"""

    category: ErrorCategory
    driver_code: int | None = None
    # 脱敏摘要(形态复用既有 `_connection_error_summary`),供
    # `console_statements.error_summary`。
    summary: str = ""
    # 服务器确认的取消/超时(DM -6515 / MySQL 1317、3024)。
    # **软取消命中恒为 False** —— 客户端提前跳出 fetch 循环不等于服务端已停,
    # 绝不允许把它当作 server_confirmed 报给用户(设计 §4.1)。
    server_confirmed: bool = False

    @property
    def connection_aborted(self) -> bool:
        """连接死亡 ⇒ 会话进 session_lost(设计 §2.1)。"""
        return self.category is ErrorCategory.CONNECTION_DEAD

    @property
    def error_code(self) -> str:
        """`console_statements.error_code` / `console_sessions.error_code`(String(64))。"""
        if self.driver_code is None:
            return self.category.value
        return f"{self.category.value}:{self.driver_code}"


@dataclass(frozen=True, slots=True)
class StatementRequest:
    """一条语句的执行请求。broker lane 逐条构造。"""

    sql: str
    # 绑定参数按方言 paramstyle 给形状:DM 是 qmark(`?` + 序列),
    # PyMySQL 是 `%s` / `%(name)s`(序列或映射)。缝原样转交,不擅自换形状 ——
    # 换了就等于在这里悄悄假设一种 paramstyle。只读片当前不传参。
    params: Mapping[str, Any] | Sequence[Any] | None = None
    fetch_size: int = 1000
    # 生效的语句时限快照(0 = 不限,DataGrip 语义)。**权威计时在 broker 侧**
    # (§4.2);传进来只为 MySQL 服务端保险带 max_execution_time = timeout+30s
    # 的逐语句同步(评审修订 R2)。DM 无对应物,忽略。
    timeout_seconds: int = 600
    # execute 返回后、首批 fetch 前回调一次列元数据(形态沿用既有 adapter)。
    column_sink: Callable[[list[Column]], None] | None = field(default=None, compare=False)


class InteractiveError(RuntimeError):
    """会话缝异常基类。消息不得包含密码或 SQL 原文。"""

    def __init__(self, message: str, classified: ClassifiedError | None = None) -> None:
        super().__init__(message)
        self.classified = classified or ClassifiedError(category=ErrorCategory.UNKNOWN)


class InteractiveConnectError(InteractiveError):
    """建连或 session_marker 采集失败 ⇒ 会话进 connect_failed(设计 §2.1)。"""


class InteractiveExecuteError(InteractiveError):
    """execute / fetch 期间的驱动错误。归属看 `classified`。"""


class SoftCancelledError(InteractiveExecuteError):
    """软取消 flag 命中(批间检查)。`classified.server_confirmed` 恒 False。"""


class SessionNotOpenError(InteractiveError):
    """连接未 open,或已 close / 已判死。"""


class CancelChannelError(InteractiveError):
    """控制通道不可用或控制命令失败。

    broker 据此升级取消阶梯(§4.1 第 3/4 级)或如实报 `cancel_failed` ——
    **绝不静默当作已取消**。
    """


@runtime_checkable
class ErrorClassifier(Protocol):
    """驱动异常 → `ClassifiedError`(§5.4 码表)。实现必须无状态、可复用。"""

    db_type: DbType

    def classify(self, exc: BaseException) -> ClassifiedError:
        """只读数值码与异常类型;**不得**读消息文本做判定。"""
        raise NotImplementedError


@runtime_checkable
class InteractiveConnection(Protocol):
    """一条持久会话连接 = 一个 broker lane 独占(threadsafety=1,设计 §1.3-1)。

    **线程约束**:除 `request_soft_cancel()` / `clear_soft_cancel()`(供 timer /
    control 线程置位的原子 flag)外,所有方法只允许在持有它的 lane 线程上调用。
    """

    capabilities: InteractiveCapabilities
    db_type: DbType
    classifier: ErrorClassifier

    @property
    def is_open(self) -> bool:
        raise NotImplementedError

    @property
    def session_marker(self) -> str | None:
        """DM sid(`SELECT SESSID()`)/ MySQL `CONNECTION_ID()` —— 数值标识,非敏感。

        取消通道靠它定位服务端会话:open 失败即无 marker,即无法硬取消。
        """
        raise NotImplementedError

    def open(self) -> str:
        """建连 + 采集 session_marker,返回 marker。失败抛 `InteractiveConnectError`。"""
        raise NotImplementedError

    def execute(self, request: StatementRequest) -> Iterator[Row]:
        """执行并流式返回行。

        调用即刻同步服务端保险带并发出 execute(生成器只包住取数循环),
        使 R2 的"execute 前同步"与 execute 期错误的抛出都发生在 lane 调用点上。
        """
        raise NotImplementedError

    def request_soft_cancel(self) -> None:
        """置软取消 flag(§4.1 阶梯第 1 级)。跨线程安全。"""
        raise NotImplementedError

    def clear_soft_cancel(self) -> None:
        """清 flag。broker 在启动下一条语句前调用(取消栅栏解除后)。"""
        raise NotImplementedError

    @property
    def soft_cancel_requested(self) -> bool:
        raise NotImplementedError

    def ping(self) -> bool:
        """探活。UNKNOWN 分类后 broker 用它判会话是否还能续用。"""
        raise NotImplementedError

    def close(self) -> None:
        """best-effort 关闭;重复调用无害。"""
        raise NotImplementedError


@runtime_checkable
class CancelChannel(Protocol):
    """每 datasource 一条常驻控制连接(设计 §1.3-2)。

    control lane 线程独占,命令串行。`cancel` / `destroy` 用**会话 marker** 寻址,
    因为 SP_CANCEL / KILL QUERY 是会话级原语(不认语句号)—— 这正是取消栅栏
    R1 存在的理由。
    """

    db_type: DbType
    classifier: ErrorClassifier

    @property
    def support(self) -> ServerCancelSupport:
        """探测结果,缓存于本控制通道生命周期(设计 §4.1)。"""
        raise NotImplementedError

    def open(self) -> ServerCancelSupport:
        """建连 + 对**自己的 marker** 试调一次硬取消(自会话空转,无害)。

        权限/调用失败 → `DEGRADED`(如实降级,不假装能取消)。
        建连本身失败抛 `CancelChannelError`。
        """
        raise NotImplementedError

    def cancel(self, session_marker: str) -> None:
        """硬取消目标会话的当前操作(阶梯第 2 级)。失败抛 `CancelChannelError`。

        返回**不代表** server_confirmed —— 确认判据是 owner lane 抛出
        `category=CANCELLED and server_confirmed` 的错误(§4.1)。
        """
        raise NotImplementedError

    def destroy(self, session_marker: str) -> None:
        """破坏性兜底(阶梯第 3 级):DM SP_CLOSE_SESSION / MySQL KILL CONNECTION。

        成功即意味着目标会话 → session_lost。
        """
        raise NotImplementedError

    def ping(self) -> bool:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


__all__ = [
    "CancelChannel",
    "CancelChannelError",
    "ClassifiedError",
    "ErrorCategory",
    "ErrorClassifier",
    "InteractiveCapabilities",
    "InteractiveConnectError",
    "InteractiveConnection",
    "InteractiveError",
    "InteractiveExecuteError",
    "ServerCancelSupport",
    "SessionNotOpenError",
    "SoftCancelledError",
    "StatementRequest",
]
