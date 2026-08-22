"""Session Broker 会话与语句的领域快照(设计 §5.1/§5.2)。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ConsoleSessionState(StrEnum):
    CONNECTING = "connecting"
    IDLE = "idle"
    EXECUTING = "executing"
    CANCELLING = "cancelling"
    CLOSING = "closing"
    CLOSED = "closed"
    SESSION_LOST = "session_lost"
    CONNECT_FAILED = "connect_failed"


ACTIVE_CONSOLE_SESSION_STATES: frozenset[ConsoleSessionState] = frozenset(
    {
        ConsoleSessionState.CONNECTING,
        ConsoleSessionState.IDLE,
        ConsoleSessionState.EXECUTING,
        ConsoleSessionState.CANCELLING,
        ConsoleSessionState.CLOSING,
    }
)


class ServerCancelState(StrEnum):
    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


class ConsoleStatementKind(StrEnum):
    SELECT = "select"
    DML = "dml"
    DDL = "ddl"
    PLSQL = "plsql"
    OTHER = "other"


class ConsoleStatementState(StrEnum):
    ACCEPTED = "accepted"
    EXECUTING = "executing"
    STREAMING = "streaming"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    OUTCOME_UNKNOWN = "outcome_unknown"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class ConsoleSession:
    id: str
    console_id: str
    datasource_id: str
    owner_user_id: str
    epoch: int
    state: ConsoleSessionState
    broker_boot_id: str
    db_session_marker: str | None
    server_cancel: ServerCancelState
    autocommit: bool
    created_at: datetime
    last_activity_at: datetime
    closed_at: datetime | None
    close_reason: str | None
    error_code: str | None


@dataclass(frozen=True)
class ConsoleStatement:
    id: str
    session_id: str
    console_id: str
    datasource_id: str
    owner_user_id: str
    epoch: int
    seq: int
    client_request_id: str
    sql_text: str
    sql_hash: str
    sql_len: int
    statement_kind: ConsoleStatementKind
    is_write: bool
    state: ConsoleStatementState
    cancel_requested: bool
    result_set_id: str | None
    rows_affected: int | None
    error_code: str | None
    error_summary: str | None
    timeout_seconds: int
    script_id: str | None
    script_seq: int | None
    resolved_by: str | None
    resolved_at: datetime | None
    resolution: str | None
    submitted_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True)
class ConsoleStatementEvent:
    id: int
    statement_id: str
    ts: datetime
    event_type: str
    message: str | None
    detail: dict[str, object] | None


__all__ = [
    "ACTIVE_CONSOLE_SESSION_STATES",
    "ConsoleSession",
    "ConsoleSessionState",
    "ConsoleStatement",
    "ConsoleStatementEvent",
    "ConsoleStatementKind",
    "ConsoleStatementState",
    "ServerCancelState",
]
