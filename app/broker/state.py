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
