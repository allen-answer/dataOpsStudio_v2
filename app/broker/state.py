"""Session Broker 状态机的唯一转移表 (设计 §2)。"""

from __future__ import annotations

from app.domain.console_session import ConsoleSessionState, ConsoleStatementState


class InvalidStateTransition(RuntimeError):
    """调用方试图跨过设计 §2 未定义的状态边。"""


SESSION_TRANSITIONS: frozenset[tuple[ConsoleSessionState, ConsoleSessionState]] = frozenset(
    {
        (ConsoleSessionState.CONNECTING, ConsoleSessionState.IDLE),
        (ConsoleSessionState.CONNECTING, ConsoleSessionState.CONNECT_FAILED),
        (ConsoleSessionState.CONNECTING, ConsoleSessionState.CLOSING),
        (ConsoleSessionState.IDLE, ConsoleSessionState.EXECUTING),
        (ConsoleSessionState.IDLE, ConsoleSessionState.CLOSING),
        (ConsoleSessionState.IDLE, ConsoleSessionState.CLOSED),
        (ConsoleSessionState.IDLE, ConsoleSessionState.SESSION_LOST),
        (ConsoleSessionState.EXECUTING, ConsoleSessionState.IDLE),
        (ConsoleSessionState.EXECUTING, ConsoleSessionState.CANCELLING),
        (ConsoleSessionState.EXECUTING, ConsoleSessionState.CLOSING),
        (ConsoleSessionState.EXECUTING, ConsoleSessionState.SESSION_LOST),
        (ConsoleSessionState.CANCELLING, ConsoleSessionState.IDLE),
        (ConsoleSessionState.CANCELLING, ConsoleSessionState.CLOSING),
        (ConsoleSessionState.CANCELLING, ConsoleSessionState.SESSION_LOST),
        (ConsoleSessionState.CLOSING, ConsoleSessionState.CLOSED),
        (ConsoleSessionState.CLOSING, ConsoleSessionState.SESSION_LOST),
    }
)


STATEMENT_TRANSITIONS: frozenset[tuple[ConsoleStatementState, ConsoleStatementState]] = frozenset(
    {
        (ConsoleStatementState.ACCEPTED, ConsoleStatementState.EXECUTING),
        (ConsoleStatementState.ACCEPTED, ConsoleStatementState.CANCELLED),
        (ConsoleStatementState.ACCEPTED, ConsoleStatementState.SKIPPED),
        # A5:受理后、lane 取出前结果集就建不出来(spool 写失败 / PG 短暂不可用)。
        # 设计 F9 规定这种情况语句归 failed(resultstore_error) 且会话续用 ——
        # §2.2 的图只画到执行期错误,这条边是 F9 在受理期的同一条规则。
        # 没有它,语句只能永远停在 accepted:前端白转圈,比失败更糟。
        (ConsoleStatementState.ACCEPTED, ConsoleStatementState.FAILED),
        (ConsoleStatementState.EXECUTING, ConsoleStatementState.STREAMING),
        (ConsoleStatementState.EXECUTING, ConsoleStatementState.SUCCEEDED),
        (ConsoleStatementState.EXECUTING, ConsoleStatementState.FAILED),
        (ConsoleStatementState.EXECUTING, ConsoleStatementState.CANCELLED),
        (ConsoleStatementState.EXECUTING, ConsoleStatementState.TIMEOUT),
        (ConsoleStatementState.EXECUTING, ConsoleStatementState.OUTCOME_UNKNOWN),
        (ConsoleStatementState.STREAMING, ConsoleStatementState.SUCCEEDED),
        (ConsoleStatementState.STREAMING, ConsoleStatementState.FAILED),
        (ConsoleStatementState.STREAMING, ConsoleStatementState.CANCELLED),
        (ConsoleStatementState.STREAMING, ConsoleStatementState.TIMEOUT),
        (ConsoleStatementState.STREAMING, ConsoleStatementState.OUTCOME_UNKNOWN),
    }
)


def transition_session(
    source: ConsoleSessionState, target: ConsoleSessionState
) -> ConsoleSessionState:
    if source is target:
        return target
    if (source, target) not in SESSION_TRANSITIONS:
        raise InvalidStateTransition(
            f"illegal session transition: {source.value} -> {target.value}"
        )
    return target


def transition_statement(
    source: ConsoleStatementState, target: ConsoleStatementState
) -> ConsoleStatementState:
    if source is target:
        return target
    if (source, target) not in STATEMENT_TRANSITIONS:
        raise InvalidStateTransition(
            f"illegal statement transition: {source.value} -> {target.value}"
        )
    return target


__all__ = [
    "SESSION_TRANSITIONS",
    "STATEMENT_TRANSITIONS",
    "InvalidStateTransition",
    "transition_session",
    "transition_statement",
]
