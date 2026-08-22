"""Broker 持久化端口及 PostgreSQL 实现。

内存注册表负责运行时线程/连接所有权; 本模块把 epoch、状态、幂等回执和事件流
写入 PG, 作为重启恢复与审计锚点 (设计 §5.1/§5.2)。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from threading import RLock
from typing import Protocol, TypeVar

from sqlalchemy import case, func, insert, select, update
from sqlalchemy.engine import Connection, Engine, RowMapping

from app.db.models import (
    console_sessions,
    console_statement_events,
    console_statements,
    sql_consoles,
)
from app.domain.console_session import (
    ACTIVE_CONSOLE_SESSION_STATES,
    ConsoleSession,
    ConsoleSessionState,
    ConsoleStatement,
    ConsoleStatementEvent,
    ConsoleStatementKind,
    ConsoleStatementState,
    ServerCancelState,
)

T = TypeVar("T")
_IN_FLIGHT_STATEMENT_STATES = (
    ConsoleStatementState.EXECUTING,
    ConsoleStatementState.STREAMING,
)


class BrokerStoreError(RuntimeError):
    """Broker 元数据无法按契约读写。"""


@dataclass(frozen=True, slots=True)
class AttachRequest:
    console_id: str
    datasource_id: str
    owner_user_id: str


@dataclass(frozen=True, slots=True)
class SweepReport:
    sessions_lost: int = 0
    read_failed: int = 0
    write_outcome_unknown: int = 0


class BrokerStore(Protocol):
    def attach_session(
        self,
        request: AttachRequest,
        *,
        session_id: str,
        broker_boot_id: str,
        now: datetime,
        reuse_session_id: str | None = None,
    ) -> ConsoleSession: ...

    def update_session(self, session: ConsoleSession) -> None: ...

    def get_session(self, session_id: str) -> ConsoleSession | None: ...

    def create_statement(
        self,
        session: ConsoleSession,
        *,
        statement_id: str,
        client_request_id: str,
        sql: str,
        sql_hash: str,
        timeout_seconds: int,
        now: datetime,
        is_write: bool = False,
    ) -> tuple[ConsoleStatement, bool]: ...

    def update_statement(self, statement: ConsoleStatement) -> None: ...

    def get_statement(self, statement_id: str) -> ConsoleStatement | None: ...

    def add_event(
        self,
        statement_id: str,
        event_type: str,
        *,
        now: datetime,
        message: str | None = None,
        detail: dict[str, object] | None = None,
    ) -> None: ...

    def startup_sweep(self, *, current_boot_id: str, now: datetime) -> SweepReport: ...


class MemoryBrokerStore(BrokerStore):
    """无真库单测用持久化替身; 语义与 PG 实现一致。"""

    def __init__(self) -> None:
        self.sessions: dict[str, ConsoleSession] = {}
        self.statements: dict[str, ConsoleStatement] = {}
        self.events: list[ConsoleStatementEvent] = []
        self._epochs: dict[str, int] = {}
        self._lock = RLock()

    def attach_session(
        self,
        request: AttachRequest,
        *,
        session_id: str,
        broker_boot_id: str,
        now: datetime,
        reuse_session_id: str | None = None,
    ) -> ConsoleSession:
        with self._lock:
            epoch = self._epochs.get(request.console_id, 0) + 1
            self._epochs[request.console_id] = epoch
            if reuse_session_id is not None:
                existing = self.sessions.get(reuse_session_id)
                if existing is None:
                    raise BrokerStoreError("live session is missing from persistence")
                attached = replace(existing, epoch=epoch, last_activity_at=now)
            else:
                attached = ConsoleSession(
                    id=session_id,
                    console_id=request.console_id,
                    datasource_id=request.datasource_id,
                    owner_user_id=request.owner_user_id,
                    epoch=epoch,
                    state=ConsoleSessionState.CONNECTING,
                    broker_boot_id=broker_boot_id,
                    db_session_marker=None,
                    server_cancel=ServerCancelState.UNKNOWN,
                    autocommit=True,
                    created_at=now,
                    last_activity_at=now,
                    closed_at=None,
                    close_reason=None,
                    error_code=None,
                )
            self.sessions[attached.id] = attached
            return attached

    def update_session(self, session: ConsoleSession) -> None:
        with self._lock:
            if session.id not in self.sessions:
                raise BrokerStoreError("session does not exist")
            self.sessions[session.id] = session

    def get_session(self, session_id: str) -> ConsoleSession | None:
        with self._lock:
            return self.sessions.get(session_id)

    def create_statement(
        self,
        session: ConsoleSession,
        *,
        statement_id: str,
        client_request_id: str,
        sql: str,
        sql_hash: str,
        timeout_seconds: int,
        now: datetime,
        is_write: bool = False,
    ) -> tuple[ConsoleStatement, bool]:
        with self._lock:
            for existing in self.statements.values():
                if (
                    existing.session_id == session.id
                    and existing.client_request_id == client_request_id
                ):
                    return existing, True
            seq = 1 + max(
                (
                    statement.seq
                    for statement in self.statements.values()
                    if statement.session_id == session.id
                ),
                default=0,
            )
            statement = _new_statement(
                session,
                statement_id=statement_id,
                seq=seq,
                client_request_id=client_request_id,
                sql=sql,
                sql_hash=sql_hash,
                timeout_seconds=timeout_seconds,
                now=now,
                is_write=is_write,
            )
            self.statements[statement.id] = statement
            return statement, False

    def update_statement(self, statement: ConsoleStatement) -> None:
        with self._lock:
            if statement.id not in self.statements:
                raise BrokerStoreError("statement does not exist")
            self.statements[statement.id] = statement

    def get_statement(self, statement_id: str) -> ConsoleStatement | None:
        with self._lock:
            return self.statements.get(statement_id)

    def add_event(
        self,
        statement_id: str,
        event_type: str,
        *,
        now: datetime,
        message: str | None = None,
        detail: dict[str, object] | None = None,
    ) -> None:
        with self._lock:
            self.events.append(
                ConsoleStatementEvent(
                    id=len(self.events) + 1,
                    statement_id=statement_id,
                    ts=now,
                    event_type=event_type,
                    message=message,
                    detail=detail,
                )
            )

    def startup_sweep(self, *, current_boot_id: str, now: datetime) -> SweepReport:
        sessions_lost = 0
        read_failed = 0
        write_outcome_unknown = 0
        with self._lock:
            stale_session_ids: set[str] = set()
            for session_id, session in tuple(self.sessions.items()):
                if (
                    session.broker_boot_id == current_boot_id
                    or session.state not in ACTIVE_CONSOLE_SESSION_STATES
                ):
                    continue
                stale_session_ids.add(session_id)
                sessions_lost += 1
                self.sessions[session_id] = replace(
                    session,
                    state=ConsoleSessionState.SESSION_LOST,
                    closed_at=now,
                    close_reason="broker_restart",
                    error_code="broker_restart",
                )
            for statement_id, statement in tuple(self.statements.items()):
                if (
                    statement.session_id not in stale_session_ids
                    or statement.state not in _IN_FLIGHT_STATEMENT_STATES
                ):
                    continue
                if statement.is_write:
                    state = ConsoleStatementState.OUTCOME_UNKNOWN
                    write_outcome_unknown += 1
                else:
                    state = ConsoleStatementState.FAILED
                    read_failed += 1
                self.statements[statement_id] = replace(
                    statement,
                    state=state,
                    error_code="broker_restart",
                    error_summary="broker restarted before statement outcome was observed",
                    finished_at=now,
                )
        return SweepReport(
            sessions_lost=sessions_lost,
            read_failed=read_failed,
            write_outcome_unknown=write_outcome_unknown,
        )


class PostgresBrokerStore(BrokerStore):
    """SQLAlchemy Core 的 PG 持久化实现; 不持连接、不跨事务共享 cursor。"""

    def __init__(self, bind: Engine | Connection) -> None:
        self._bind = bind

    def attach_session(
        self,
        request: AttachRequest,
        *,
        session_id: str,
        broker_boot_id: str,
        now: datetime,
        reuse_session_id: str | None = None,
    ) -> ConsoleSession:
        def op(conn: Connection) -> ConsoleSession:
            epoch_row = (
                conn.execute(
                    update(sql_consoles)
                    .where(sql_consoles.c.id == request.console_id)
                    .where(sql_consoles.c.datasource_id == request.datasource_id)
                    .where(sql_consoles.c.owner_user_id == request.owner_user_id)
                    .values(session_epoch=sql_consoles.c.session_epoch + 1)
                    .returning(sql_consoles.c.session_epoch)
                )
                .mappings()
                .one_or_none()
            )
            if epoch_row is None:
                raise BrokerStoreError("console does not exist or ownership changed")
            epoch = int(epoch_row["session_epoch"])
            if reuse_session_id is not None:
                row = (
                    conn.execute(
                        update(console_sessions)
                        .where(console_sessions.c.id == reuse_session_id)
                        .where(console_sessions.c.state.in_(_active_state_values()))
                        .values(epoch=epoch, last_activity_at=now)
                        .returning(*console_sessions.c)
                    )
                    .mappings()
                    .one_or_none()
                )
                if row is None:
                    raise BrokerStoreError("live session is missing from persistence")
                return _session_from_row(row)
            values = {
                "id": session_id,
                "console_id": request.console_id,
                "datasource_id": request.datasource_id,
                "owner_user_id": request.owner_user_id,
                "epoch": epoch,
                "state": ConsoleSessionState.CONNECTING.value,
                "broker_boot_id": broker_boot_id,
                "db_session_marker": None,
                "server_cancel": ServerCancelState.UNKNOWN.value,
                "autocommit": True,
                "created_at": now,
                "last_activity_at": now,
                "closed_at": None,
                "close_reason": None,
                "error_code": None,
            }
            row = (
                conn.execute(
                    insert(console_sessions).values(**values).returning(*console_sessions.c)
                )
                .mappings()
                .one()
            )
            return _session_from_row(row)

        return self._write(op)

    def update_session(self, session: ConsoleSession) -> None:
        def op(conn: Connection) -> None:
            result = conn.execute(
                update(console_sessions)
                .where(console_sessions.c.id == session.id)
                .values(**_session_values(session))
            )
            if result.rowcount != 1:
                raise BrokerStoreError("session does not exist")

        self._write(op)

    def get_session(self, session_id: str) -> ConsoleSession | None:
        def op(conn: Connection) -> ConsoleSession | None:
            row = (
                conn.execute(select(console_sessions).where(console_sessions.c.id == session_id))
                .mappings()
                .one_or_none()
            )
            return _session_from_row(row) if row is not None else None

        return self._read(op)

    def create_statement(
        self,
        session: ConsoleSession,
        *,
        statement_id: str,
        client_request_id: str,
        sql: str,
        sql_hash: str,
        timeout_seconds: int,
        now: datetime,
        is_write: bool = False,
    ) -> tuple[ConsoleStatement, bool]:
        def op(conn: Connection) -> tuple[ConsoleStatement, bool]:
            conn.execute(
                select(console_sessions.c.id)
                .where(console_sessions.c.id == session.id)
                .with_for_update()
            ).one()
            existing = (
                conn.execute(
                    select(console_statements).where(
                        console_statements.c.session_id == session.id,
                        console_statements.c.client_request_id == client_request_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                return _statement_from_row(existing), True
            seq = (
                int(
                    conn.execute(
                        select(func.coalesce(func.max(console_statements.c.seq), 0)).where(
                            console_statements.c.session_id == session.id
                        )
                    ).scalar_one()
                )
                + 1
            )
            statement = _new_statement(
                session,
                statement_id=statement_id,
                seq=seq,
                client_request_id=client_request_id,
                sql=sql,
                sql_hash=sql_hash,
                timeout_seconds=timeout_seconds,
                now=now,
                is_write=is_write,
            )
            row = (
                conn.execute(
                    insert(console_statements)
                    .values(**_statement_values(statement))
                    .returning(*console_statements.c)
                )
                .mappings()
                .one()
            )
            return _statement_from_row(row), False

        return self._write(op)

    def update_statement(self, statement: ConsoleStatement) -> None:
        def op(conn: Connection) -> None:
            result = conn.execute(
                update(console_statements)
                .where(console_statements.c.id == statement.id)
                .values(**_statement_values(statement))
            )
            if result.rowcount != 1:
                raise BrokerStoreError("statement does not exist")

        self._write(op)

    def get_statement(self, statement_id: str) -> ConsoleStatement | None:
        def op(conn: Connection) -> ConsoleStatement | None:
            row = (
                conn.execute(
                    select(console_statements).where(console_statements.c.id == statement_id)
                )
                .mappings()
                .one_or_none()
            )
            return _statement_from_row(row) if row is not None else None

        return self._read(op)

    def add_event(
        self,
        statement_id: str,
        event_type: str,
        *,
        now: datetime,
        message: str | None = None,
        detail: dict[str, object] | None = None,
    ) -> None:
        def op(conn: Connection) -> None:
            conn.execute(
                insert(console_statement_events).values(
                    statement_id=statement_id,
                    ts=now,
                    event_type=event_type,
                    message=message,
                    detail=detail,
                )
            )

        self._write(op)

    def startup_sweep(self, *, current_boot_id: str, now: datetime) -> SweepReport:
        def op(conn: Connection) -> SweepReport:
            stale_rows = (
                conn.execute(
                    select(console_sessions.c.id)
                    .where(console_sessions.c.broker_boot_id != current_boot_id)
                    .where(console_sessions.c.state.in_(_active_state_values()))
                    .with_for_update()
                )
                .mappings()
                .all()
            )
            stale_ids = [str(row["id"]) for row in stale_rows]
            if not stale_ids:
                return SweepReport()
            conn.execute(
                update(console_sessions)
                .where(console_sessions.c.id.in_(stale_ids))
                .values(
                    state=ConsoleSessionState.SESSION_LOST.value,
                    closed_at=now,
                    close_reason="broker_restart",
                    error_code="broker_restart",
                )
            )
            changed = (
                conn.execute(
                    update(console_statements)
                    .where(console_statements.c.session_id.in_(stale_ids))
                    .where(
                        console_statements.c.state.in_(
                            [state.value for state in _IN_FLIGHT_STATEMENT_STATES]
                        )
                    )
                    .values(
                        state=case(
                            (console_statements.c.is_write.is_(True), "outcome_unknown"),
                            else_="failed",
                        ),
                        error_code="broker_restart",
                        error_summary="broker restarted before statement outcome was observed",
                        finished_at=now,
                    )
                    .returning(console_statements.c.id, console_statements.c.is_write)
                )
                .mappings()
                .all()
            )
            read_failed = sum(not bool(row["is_write"]) for row in changed)
            write_unknown = len(changed) - read_failed
            return SweepReport(len(stale_ids), read_failed, write_unknown)

        return self._write(op)

    def _write(self, op: Callable[[Connection], T]) -> T:
        if isinstance(self._bind, Engine):
            with self._bind.begin() as conn:
                return op(conn)
        if self._bind.in_transaction():
            return op(self._bind)
        with self._bind.begin():
            return op(self._bind)

    def _read(self, op: Callable[[Connection], T]) -> T:
        if isinstance(self._bind, Engine):
            with self._bind.connect() as conn:
                return op(conn)
        return op(self._bind)


def _new_statement(
    session: ConsoleSession,
    *,
    statement_id: str,
    seq: int,
    client_request_id: str,
    sql: str,
    sql_hash: str,
    timeout_seconds: int,
    now: datetime,
    is_write: bool,
) -> ConsoleStatement:
    return ConsoleStatement(
        id=statement_id,
        session_id=session.id,
        console_id=session.console_id,
        datasource_id=session.datasource_id,
        owner_user_id=session.owner_user_id,
        epoch=session.epoch,
        seq=seq,
        client_request_id=client_request_id,
        sql_text=sql,
        sql_hash=sql_hash,
        sql_len=len(sql),
        statement_kind=ConsoleStatementKind.OTHER if is_write else ConsoleStatementKind.SELECT,
        is_write=is_write,
        state=ConsoleStatementState.ACCEPTED,
        cancel_requested=False,
        result_set_id=None,
        rows_affected=None,
        error_code=None,
        error_summary=None,
        timeout_seconds=timeout_seconds,
        script_id=None,
        script_seq=None,
        resolved_by=None,
        resolved_at=None,
        resolution=None,
        submitted_at=now,
        started_at=None,
        finished_at=None,
    )


def _active_state_values() -> list[str]:
    return [state.value for state in ACTIVE_CONSOLE_SESSION_STATES]


def _session_values(session: ConsoleSession) -> dict[str, object]:
    return {
        "console_id": session.console_id,
        "datasource_id": session.datasource_id,
        "owner_user_id": session.owner_user_id,
        "epoch": session.epoch,
        "state": session.state.value,
        "broker_boot_id": session.broker_boot_id,
        "db_session_marker": session.db_session_marker,
        "server_cancel": session.server_cancel.value,
        "autocommit": session.autocommit,
        "created_at": session.created_at,
        "last_activity_at": session.last_activity_at,
        "closed_at": session.closed_at,
        "close_reason": session.close_reason,
        "error_code": session.error_code,
    }


def _statement_values(statement: ConsoleStatement) -> dict[str, object]:
    return {
        "session_id": statement.session_id,
        "console_id": statement.console_id,
        "datasource_id": statement.datasource_id,
        "owner_user_id": statement.owner_user_id,
        "epoch": statement.epoch,
        "seq": statement.seq,
        "client_request_id": statement.client_request_id,
        "sql_text": statement.sql_text,
        "sql_hash": statement.sql_hash,
        "sql_len": statement.sql_len,
        "statement_kind": statement.statement_kind.value,
        "is_write": statement.is_write,
        "state": statement.state.value,
        "cancel_requested": statement.cancel_requested,
        "result_set_id": statement.result_set_id,
        "rows_affected": statement.rows_affected,
        "error_code": statement.error_code,
        "error_summary": statement.error_summary,
        "timeout_seconds": statement.timeout_seconds,
        "script_id": statement.script_id,
        "script_seq": statement.script_seq,
        "resolved_by": statement.resolved_by,
        "resolved_at": statement.resolved_at,
        "resolution": statement.resolution,
        "submitted_at": statement.submitted_at,
        "started_at": statement.started_at,
        "finished_at": statement.finished_at,
    }


def _session_from_row(row: RowMapping) -> ConsoleSession:
    return ConsoleSession(
        id=str(row["id"]),
        console_id=str(row["console_id"]),
        datasource_id=str(row["datasource_id"]),
        owner_user_id=str(row["owner_user_id"]),
        epoch=int(row["epoch"]),
        state=ConsoleSessionState(str(row["state"])),
        broker_boot_id=str(row["broker_boot_id"]),
        db_session_marker=_optional_str(row["db_session_marker"]),
        server_cancel=ServerCancelState(str(row["server_cancel"])),
        autocommit=bool(row["autocommit"]),
        created_at=row["created_at"],
        last_activity_at=row["last_activity_at"],
        closed_at=row["closed_at"],
        close_reason=_optional_str(row["close_reason"]),
        error_code=_optional_str(row["error_code"]),
    )


def _statement_from_row(row: RowMapping) -> ConsoleStatement:
    return ConsoleStatement(
        id=str(row["id"]),
        session_id=str(row["session_id"]),
        console_id=str(row["console_id"]),
        datasource_id=str(row["datasource_id"]),
        owner_user_id=str(row["owner_user_id"]),
        epoch=int(row["epoch"]),
        seq=int(row["seq"]),
        client_request_id=str(row["client_request_id"]),
        sql_text=str(row["sql_text"]),
        sql_hash=str(row["sql_hash"]),
        sql_len=int(row["sql_len"]),
        statement_kind=ConsoleStatementKind(str(row["statement_kind"])),
        is_write=bool(row["is_write"]),
        state=ConsoleStatementState(str(row["state"])),
        cancel_requested=bool(row["cancel_requested"]),
        result_set_id=_optional_str(row["result_set_id"]),
        rows_affected=int(row["rows_affected"]) if row["rows_affected"] is not None else None,
        error_code=_optional_str(row["error_code"]),
        error_summary=_optional_str(row["error_summary"]),
        timeout_seconds=int(row["timeout_seconds"]),
        script_id=_optional_str(row["script_id"]),
        script_seq=int(row["script_seq"]) if row["script_seq"] is not None else None,
        resolved_by=_optional_str(row["resolved_by"]),
        resolved_at=row["resolved_at"],
        resolution=_optional_str(row["resolution"]),
        submitted_at=row["submitted_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def _optional_str(value: object) -> str | None:
    return str(value) if value is not None else None


__all__ = [
    "AttachRequest",
    "BrokerStore",
    "BrokerStoreError",
    "MemoryBrokerStore",
    "PostgresBrokerStore",
    "SweepReport",
]
