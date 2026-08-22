from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from app.broker.store import AttachRequest, MemoryBrokerStore
from app.domain.console_session import (
    ConsoleSessionState,
    ConsoleStatementState,
)

NOW = datetime(2026, 8, 22, 9, 0, tzinfo=UTC)


def _attach(store: MemoryBrokerStore, *, reuse_session_id: str | None = None):
    return store.attach_session(
        AttachRequest(console_id="console-1", datasource_id="ds-1", owner_user_id="user-1"),
        session_id="session-1",
        broker_boot_id="boot-new",
        now=NOW,
        reuse_session_id=reuse_session_id,
    )


def test_attach_epoch_is_monotonic_and_reattach_reuses_the_live_session() -> None:
    store = MemoryBrokerStore()
    first = _attach(store)
    second = _attach(store, reuse_session_id=first.id)

    assert (first.epoch, second.epoch) == (1, 2)
    assert first.id == second.id
    assert len(store.sessions) == 1


def test_statement_receipt_is_idempotent_per_session_request_id() -> None:
    store = MemoryBrokerStore()
    session = _attach(store)
    first, first_deduplicated = store.create_statement(
        session,
        statement_id="statement-1",
        client_request_id="request-1",
        sql="SELECT 1",
        sql_hash="sha256:one",
        timeout_seconds=600,
        now=NOW,
    )
    replay, replay_deduplicated = store.create_statement(
        session,
        statement_id="statement-2",
        client_request_id="request-1",
        sql="SELECT should never persist",
        sql_hash="sha256:two",
        timeout_seconds=1,
        now=NOW,
    )

    assert first_deduplicated is False
    assert replay_deduplicated is True
    assert replay == first
    assert len(store.statements) == 1
    assert store.get_session(session.id) == session
    assert store.get_statement(first.id) == first


def test_startup_sweep_marks_sessions_lost_and_splits_read_from_write() -> None:
    store = MemoryBrokerStore()
    session = _attach(store)
    store.update_session(replace(session, state=ConsoleSessionState.EXECUTING))
    read, _ = store.create_statement(
        session,
        statement_id="read",
        client_request_id="read-request",
        sql="SELECT 1",
        sql_hash="sha256:read",
        timeout_seconds=600,
        now=NOW,
    )
    write, _ = store.create_statement(
        session,
        statement_id="write",
        client_request_id="write-request",
        sql="UPDATE t SET n=1",
        sql_hash="sha256:write",
        timeout_seconds=600,
        now=NOW,
        is_write=True,
    )
    store.update_statement(replace(read, state=ConsoleStatementState.STREAMING))
    store.update_statement(replace(write, state=ConsoleStatementState.EXECUTING))

    report = store.startup_sweep(current_boot_id="boot-newer", now=NOW)

    assert report.sessions_lost == 1
    assert report.read_failed == 1
    assert report.write_outcome_unknown == 1
    assert store.sessions[session.id].state is ConsoleSessionState.SESSION_LOST
    assert store.sessions[session.id].close_reason == "broker_restart"
    assert store.statements[read.id].state is ConsoleStatementState.FAILED
    assert store.statements[write.id].state is ConsoleStatementState.OUTCOME_UNKNOWN


def test_startup_sweep_ignores_rows_owned_by_the_current_boot() -> None:
    store = MemoryBrokerStore()
    session = _attach(store)
    assert store.startup_sweep(current_boot_id=session.broker_boot_id, now=NOW).sessions_lost == 0
    assert store.sessions[session.id].state is ConsoleSessionState.CONNECTING
