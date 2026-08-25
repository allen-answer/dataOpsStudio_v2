from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from app.broker.core import (
    BrokerConfig,
    BrokerError,
    SessionBroker,
    SessionLimits,
)
from app.broker.store import AttachRequest, MemoryBrokerStore
from app.dbclients.interactive import (
    CancelChannel,
    CancelChannelError,
    ClassifiedError,
    ErrorCategory,
    ErrorClassifier,
    InteractiveCapabilities,
    InteractiveConnection,
    InteractiveExecuteError,
    ServerCancelSupport,
    StatementRequest,
)
from app.dbclients.interactive.errors import MySQLErrorClassifier
from app.dbclients.query_limit import apply_database_row_limit
from app.domain.console_session import ConsoleSession, ConsoleSessionState, ConsoleStatementState
from app.domain.datasource import DbType
from app.domain.schema import Row
from tests.unit._interactive_fakes import mysql_error


@dataclass
class FakeClock:
    value: datetime = datetime(2026, 8, 22, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


@dataclass
class DriverHarness:
    db_type: DbType = DbType.MYSQL
    block_execute: bool = False
    cancel_causes_confirmation: bool = True
    block_cancel_return: bool = False
    # 置上后 execute 抛缝的标准执行错;测试可中途换掉它,逐条语句给不同分类。
    execute_error: ClassifiedError | None = None
    execute_started: threading.Event = field(default_factory=threading.Event)
    execute_release: threading.Event = field(default_factory=threading.Event)
    cancel_started: threading.Event = field(default_factory=threading.Event)
    cancel_release: threading.Event = field(default_factory=threading.Event)
    destroy_called: threading.Event = field(default_factory=threading.Event)
    calls: list[tuple[str, str]] = field(default_factory=list)
    connections: list[FakeInteractiveConnection] = field(default_factory=list)
    channels: list[FakeCancelChannel] = field(default_factory=list)

    def connection_factory(self, _session: ConsoleSession) -> InteractiveConnection:
        connection = FakeInteractiveConnection(self, marker=str(100 + len(self.connections)))
        self.connections.append(connection)
        return connection

    def channel_factory(self, _datasource_id: str) -> CancelChannel:
        channel = FakeCancelChannel(self)
        self.channels.append(channel)
        return channel


class FakeClassifier:
    db_type: DbType = DbType.MYSQL

    def classify(self, _exc: BaseException) -> ClassifiedError:
        return ClassifiedError(ErrorCategory.UNKNOWN)


class FakeInteractiveConnection:
    db_type: DbType
    classifier: ErrorClassifier = FakeClassifier()
    capabilities = InteractiveCapabilities(
        server_cancel=True,
        server_statement_timeout=True,
        session_streaming=True,
    )

    def __init__(self, harness: DriverHarness, marker: str) -> None:
        self.harness = harness
        self.db_type = harness.db_type
        self._marker = marker
        self._open = False
        self._soft_cancel = threading.Event()
        self.thread_ids: list[int] = []
        self.executed_sql: list[str] = []

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def session_marker(self) -> str | None:
        return self._marker if self._open else None

    def open(self) -> str:
        self.thread_ids.append(threading.get_ident())
        self._open = True
        return self._marker

    def execute(self, request: StatementRequest) -> Iterator[Row]:
        self.thread_ids.append(threading.get_ident())
        self.executed_sql.append(request.sql)
        self.harness.calls.append(("execute", request.sql))
        self.harness.execute_started.set()
        if self.harness.block_execute:
            assert self.harness.execute_release.wait(3), "test did not release execute"
        if self.harness.execute_error is not None:
            # 缝在 `_run` 里就是这么抛的:消息恒为该常量,真原因只在 classified 里。
            raise InteractiveExecuteError(
                "interactive statement failed", self.harness.execute_error
            )
        if self._soft_cancel.is_set() and self.harness.cancel_causes_confirmation:
            raise InteractiveExecuteError(
                "cancelled",
                ClassifiedError(
                    ErrorCategory.CANCELLED,
                    driver_code=1317,
                    server_confirmed=True,
                ),
            )
        return iter([Row(values=[1])])

    def request_soft_cancel(self) -> None:
        self._soft_cancel.set()
        if self.harness.cancel_causes_confirmation:
            self.harness.execute_release.set()

    def clear_soft_cancel(self) -> None:
        self._soft_cancel.clear()
        self.harness.execute_started.clear()
        self.harness.execute_release.clear()

    @property
    def soft_cancel_requested(self) -> bool:
        return self._soft_cancel.is_set()

    def ping(self) -> bool:
        return self._open

    def close(self) -> None:
        self.thread_ids.append(threading.get_ident())
        self._open = False
        self.harness.execute_release.set()


class FakeCancelChannel:
    db_type = DbType.MYSQL
    classifier: ErrorClassifier = FakeClassifier()

    def __init__(self, harness: DriverHarness) -> None:
        self.harness = harness
        self._support = ServerCancelSupport.UNKNOWN
        self.thread_ids: list[int] = []

    @property
    def support(self) -> ServerCancelSupport:
        return self._support

    def open(self) -> ServerCancelSupport:
        self.thread_ids.append(threading.get_ident())
        self._support = ServerCancelSupport.AVAILABLE
        return self._support

    def cancel(self, session_marker: str) -> None:
        self.thread_ids.append(threading.get_ident())
        self.harness.calls.append(("cancel", session_marker))
        self.harness.cancel_started.set()
        if self.harness.block_cancel_return:
            assert self.harness.cancel_release.wait(3), "test did not release cancel"

    def destroy(self, session_marker: str) -> None:
        self.thread_ids.append(threading.get_ident())
        self.harness.calls.append(("destroy", session_marker))
        self.harness.destroy_called.set()
        self.harness.execute_release.set()

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        self.thread_ids.append(threading.get_ident())


class RecordingLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, object]]] = []

    def info(self, event: str, **kwargs: object) -> None:
        self.records.append((event, kwargs))


def _config(**overrides: object) -> BrokerConfig:
    values: dict[str, object] = {
        "limits": SessionLimits(per_user=8, per_datasource=4, global_total=16),
        "mailbox_size": 20,
        "idle_timeout_seconds": 1800,
        "cancel_grace_seconds": 5,
        "timer_poll_seconds": 3600,
    }
    values.update(overrides)
    return BrokerConfig(**values)  # type: ignore[arg-type]


def _broker(
    harness: DriverHarness | None = None,
    *,
    clock: FakeClock | None = None,
    config: BrokerConfig | None = None,
    logger: RecordingLogger | None = None,
) -> tuple[SessionBroker, MemoryBrokerStore, DriverHarness]:
    harness = harness or DriverHarness()
    store = MemoryBrokerStore()
    broker = SessionBroker(
        store,
        connection_factory=harness.connection_factory,
        cancel_channel_factory=harness.channel_factory,
        clock=clock or FakeClock(),
        config=config or _config(),
        logger=logger,
        boot_id="boot-test",
    )
    broker.start()
    return broker, store, harness


def _request(
    console: str = "console-1", datasource: str = "ds-1", user: str = "user-1"
) -> AttachRequest:
    return AttachRequest(console_id=console, datasource_id=datasource, owner_user_id=user)


def _wait(predicate: object, timeout: float = 3.0) -> None:
    assert callable(predicate)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition did not become true")


def _limited(sql: str, db_type: DbType = DbType.MYSQL, limit: int = 1001) -> str:
    return apply_database_row_limit(sql, db_type, limit)


def test_lane_owns_one_connection_and_executes_statements_serially() -> None:
    broker, store, harness = _broker()
    try:
        session = broker.attach(_request())
        _wait(lambda: broker.observe(session.id).state is ConsoleSessionState.IDLE)
        first = broker.submit(session.id, session.epoch, "SELECT 1", "request-1")
        second = broker.submit(session.id, session.epoch, "SELECT 2", "request-2")
        _wait(
            lambda: store.statements[second.statement.id].state is ConsoleStatementState.SUCCEEDED
        )

        assert [call for call in harness.calls if call[0] == "execute"] == [
            ("execute", _limited("SELECT 1")),
            ("execute", _limited("SELECT 2")),
        ]
        assert len(harness.connections) == 1
        assert len(set(harness.connections[0].thread_ids)) == 1
        assert store.statements[first.statement.id].state is ConsoleStatementState.SUCCEEDED
    finally:
        broker.shutdown()


def test_dm_session_pushes_max_result_rows_into_execute_sql() -> None:
    harness = DriverHarness(db_type=DbType.DM)
    broker, store, _ = _broker(harness)
    try:
        session = broker.attach(_request())
        _wait(lambda: broker.observe(session.id).state is ConsoleSessionState.IDLE)
        receipt = broker.submit(
            session.id,
            session.epoch,
            "SELECT * FROM SJCS.ACC_FUNDACCOUNT",
            "request-limited",
            max_result_rows=1000,
        )
        _wait(
            lambda: store.statements[receipt.statement.id].state is ConsoleStatementState.SUCCEEDED
        )

        assert harness.connections[0].executed_sql == [
            "SELECT * FROM SJCS.ACC_FUNDACCOUNT FETCH FIRST 1001 ROWS ONLY"
        ]
    finally:
        broker.shutdown()


def test_failed_statement_reports_the_driver_summary_not_the_seam_constant() -> None:
    """回归:`error_summary` 曾恒为缝里写死的 "interactive statement failed"。

    用户改了表结构、控制台按旧元数据展开 `*`,数据库报的是"无效的列名",而这一
    条常量把真原因整个吞掉 —— 全系统再没有第二处留有它。摘要必须来自
    `classified.summary`(protocol.py 给该字段写明的用途),常量只做兜底。
    """
    real_summary = "OperationalError code=1054 message=Unknown column 'legacy_col' in 'field list'"
    harness = DriverHarness(
        execute_error=ClassifiedError(
            ErrorCategory.STATEMENT_ERROR,
            driver_code=1054,
            summary=real_summary,
        )
    )
    broker, store, _ = _broker(harness)
    try:
        session = broker.attach(_request())
        _wait(lambda: broker.observe(session.id).state is ConsoleSessionState.IDLE)
        receipt = broker.submit(
            session.id, session.epoch, "SELECT legacy_col FROM t", "request-bad-column"
        )
        _wait(lambda: store.statements[receipt.statement.id].state is ConsoleStatementState.FAILED)

        failed = store.statements[receipt.statement.id]
        assert failed.error_summary == real_summary
        assert "interactive statement failed" not in (failed.error_summary or "")
        # 分类码一并落库,前端拿它当排查线索(sessions.ts raw_error_code)。
        assert failed.error_code == "statement_error:1054"

        # 摘要为空时(分类器没造出摘要)仍回落到缝的常量,不写出空错误。
        harness.execute_error = ClassifiedError(ErrorCategory.STATEMENT_ERROR, driver_code=1064)
        second = broker.submit(session.id, session.epoch, "SELECT 2", "request-no-summary")
        _wait(lambda: store.statements[second.statement.id].state is ConsoleStatementState.FAILED)
        assert store.statements[second.statement.id].error_summary == "interactive statement failed"
    finally:
        broker.shutdown()


def test_failed_statement_summary_never_carries_a_password() -> None:
    """R5:摘要现在带驱动原文,必须仍然脱敏 —— 密码在到达 broker 前就已抹掉。"""
    classified = MySQLErrorClassifier().classify(
        mysql_error(1045, "Access denied for password='hunter2' at mysql://db:3306/x")
    )
    harness = DriverHarness(execute_error=classified)
    logger = RecordingLogger()
    broker, store, _ = _broker(harness, logger=logger)
    try:
        session = broker.attach(_request())
        _wait(lambda: broker.observe(session.id).state is ConsoleSessionState.IDLE)
        receipt = broker.submit(session.id, session.epoch, "SELECT 1", "request-secret")
        _wait(lambda: store.statements[receipt.statement.id].state is ConsoleStatementState.FAILED)

        summary = store.statements[receipt.statement.id].error_summary or ""
        assert "hunter2" not in summary
        assert "***REDACTED***" in summary
        assert "<redacted-url>" in summary
        assert "hunter2" not in repr(logger.records)
    finally:
        broker.shutdown()


def test_m1_m2_m3_and_m8_epoch_fencing_and_live_reattach() -> None:
    harness = DriverHarness(block_execute=True)
    broker, _store, _ = _broker(harness)
    try:
        first = broker.attach(_request())
        _wait(lambda: broker.observe(first.id).state is ConsoleSessionState.IDLE)
        running = broker.submit(first.id, first.epoch, "SELECT slow", "request-1")
        assert harness.execute_started.wait(1)

        takeover = broker.attach(_request())
        assert takeover.id == first.id
        assert takeover.epoch == first.epoch + 1
        assert broker.observe(first.id).current_statement_id == running.statement.id
        assert len(harness.connections) == 1

        with pytest.raises(BrokerError) as stale_submit:
            broker.submit(first.id, first.epoch, "SELECT stale", "request-stale")
        assert (stale_submit.value.code, stale_submit.value.current_epoch) == (
            "stale_session_epoch",
            takeover.epoch,
        )
        with pytest.raises(BrokerError) as stale_cancel:
            broker.cancel(running.statement.id, first.epoch)
        assert stale_cancel.value.code == "stale_session_epoch"
        assert broker.cancel(running.statement.id, takeover.epoch).accepted is True
    finally:
        harness.execute_release.set()
        harness.cancel_release.set()
        broker.shutdown()


def test_m4_concurrent_attach_allocates_distinct_epochs_but_one_session() -> None:
    broker, _store, _harness = _broker()
    try:
        barrier = threading.Barrier(3)
        attached: list[tuple[str, int]] = []

        def attach() -> None:
            barrier.wait()
            session = broker.attach(_request())
            attached.append((session.id, session.epoch))

        threads = [threading.Thread(target=attach) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(2)

        assert len({session_id for session_id, _epoch in attached}) == 1
        assert sorted(epoch for _session_id, epoch in attached) == [1, 2]
        assert len(broker.active_sessions()) == 1
    finally:
        broker.shutdown()


def test_m5_new_broker_observes_the_session_lost_snapshot_after_startup_sweep() -> None:
    store = MemoryBrokerStore()
    stale = store.attach_session(
        _request(),
        session_id="stale-session",
        broker_boot_id="old-boot",
        now=FakeClock()(),
    )
    broker = SessionBroker(
        store,
        connection_factory=DriverHarness().connection_factory,
        cancel_channel_factory=DriverHarness().channel_factory,
        clock=FakeClock(),
        config=_config(),
        boot_id="new-boot",
    )
    try:
        report = broker.start()
        observed = broker.observe(stale.id)
        assert report.sessions_lost == 1
        assert observed.state is ConsoleSessionState.SESSION_LOST
        assert observed.session.close_reason == "broker_restart"
    finally:
        broker.shutdown()


def test_m6_close_wins_against_later_submit() -> None:
    broker, _store, _harness = _broker()
    try:
        session = broker.attach(_request())
        _wait(lambda: broker.observe(session.id).state is ConsoleSessionState.IDLE)
        broker.close(session.id, session.epoch)
        with pytest.raises(BrokerError, match="session_not_active"):
            broker.submit(session.id, session.epoch, "SELECT too late", "request-late")
        _wait(lambda: broker.observe(session.id).state is ConsoleSessionState.CLOSED)
    finally:
        broker.shutdown()


def test_m6_submit_wins_when_it_reaches_the_mailbox_before_close() -> None:
    harness = DriverHarness(block_execute=True)
    broker, store, _ = _broker(harness)
    try:
        session = broker.attach(_request())
        _wait(lambda: broker.observe(session.id).state is ConsoleSessionState.IDLE)
        receipt = broker.submit(session.id, session.epoch, "SELECT first", "request-first")
        assert harness.execute_started.wait(1)
        broker.close(session.id, session.epoch)
        harness.execute_release.set()
        _wait(lambda: broker.observe(session.id).state is ConsoleSessionState.CLOSED)
        assert store.statements[receipt.statement.id].state in (
            ConsoleStatementState.CANCELLED,
            ConsoleStatementState.SUCCEEDED,
        )
        assert harness.connections[0].executed_sql == [_limited("SELECT first")]
    finally:
        harness.execute_release.set()
        broker.shutdown()


def test_r1_cancel_fence_blocks_next_execute_until_control_lane_returns() -> None:
    harness = DriverHarness(
        block_execute=True,
        cancel_causes_confirmation=False,
        block_cancel_return=True,
    )
    broker, store, _ = _broker(harness)
    try:
        session = broker.attach(_request())
        _wait(lambda: broker.observe(session.id).state is ConsoleSessionState.IDLE)
        first = broker.submit(session.id, session.epoch, "SELECT first", "request-1")
        assert harness.execute_started.wait(1)
        second = broker.submit(session.id, session.epoch, "SELECT second", "request-2")
        broker.cancel(first.statement.id, session.epoch)
        assert harness.cancel_started.wait(1)

        harness.execute_release.set()
        _wait(lambda: store.statements[first.statement.id].state is ConsoleStatementState.SUCCEEDED)
        time.sleep(0.03)
        assert harness.connections[0].executed_sql == [_limited("SELECT first")]

        harness.block_execute = False
        harness.cancel_release.set()
        _wait(
            lambda: store.statements[second.statement.id].state is ConsoleStatementState.SUCCEEDED
        )
        assert harness.connections[0].executed_sql == [
            _limited("SELECT first"),
            _limited("SELECT second"),
        ]
        assert len(set(harness.channels[0].thread_ids)) == 1
    finally:
        harness.execute_release.set()
        harness.cancel_release.set()
        broker.shutdown()


def test_idempotent_replay_returns_the_original_receipt_and_executes_once() -> None:
    broker, store, harness = _broker()
    try:
        session = broker.attach(_request())
        _wait(lambda: broker.observe(session.id).state is ConsoleSessionState.IDLE)
        first = broker.submit(session.id, session.epoch, "SELECT 1", "same-request")
        replay = broker.submit(session.id, session.epoch, "SELECT changed", "same-request")
        _wait(lambda: store.statements[first.statement.id].state is ConsoleStatementState.SUCCEEDED)

        assert replay.deduplicated is True
        assert replay.statement.id == first.statement.id
        assert harness.connections[0].executed_sql == [_limited("SELECT 1")]
    finally:
        broker.shutdown()


def test_fake_clock_timeout_walks_soft_hard_grace_destroy_in_order() -> None:
    clock = FakeClock()
    harness = DriverHarness(block_execute=True, cancel_causes_confirmation=False)
    broker, store, _ = _broker(harness, clock=clock)
    try:
        session = broker.attach(_request())
        _wait(lambda: broker.observe(session.id).state is ConsoleSessionState.IDLE)
        receipt = broker.submit(
            session.id,
            session.epoch,
            "SELECT slow",
            "request-timeout",
            timeout_seconds=2,
        )
        assert harness.execute_started.wait(1)

        clock.advance(2)
        broker.run_timers_once()
        assert harness.cancel_started.wait(1)
        assert harness.connections[0].soft_cancel_requested is True

        clock.advance(5)
        broker.run_timers_once()
        assert harness.destroy_called.wait(1)
        event_types = [
            event.event_type for event in store.events if event.statement_id == receipt.statement.id
        ]
        assert event_types.index("cancel_requested") < event_types.index("cancel_dispatched")
        assert event_types.index("cancel_dispatched") < event_types.index("cancel_escalated")
    finally:
        harness.execute_release.set()
        broker.shutdown()


def test_session_guards_and_mailbox_bound_are_enforced() -> None:
    limit_config = _config(limits=SessionLimits(per_user=1, per_datasource=1, global_total=1))
    broker, _store, _harness = _broker(config=limit_config)
    try:
        broker.attach(_request())
        with pytest.raises(BrokerError) as excinfo:
            broker.attach(_request(console="console-2", datasource="ds-2"))
        assert excinfo.value.code == "session_limit_reached"
    finally:
        broker.shutdown()


@pytest.mark.parametrize(
    "second_request",
    [
        _request(console="console-2", datasource="ds-2", user="user-1"),
        _request(console="console-2", datasource="ds-1", user="user-2"),
    ],
)
def test_each_session_limit_dimension_is_enforced(second_request: AttachRequest) -> None:
    broker, _store, _harness = _broker(
        config=_config(limits=SessionLimits(per_user=1, per_datasource=1, global_total=8))
    )
    try:
        broker.attach(_request())
        with pytest.raises(BrokerError) as excinfo:
            broker.attach(second_request)
        assert excinfo.value.code == "session_limit_reached"
    finally:
        broker.shutdown()


def test_mailbox_bound_rejects_one_more_than_the_configured_queue_size() -> None:
    harness = DriverHarness(block_execute=True)
    broker, _store, _ = _broker(harness, config=_config(mailbox_size=2))
    try:
        session = broker.attach(_request())
        _wait(lambda: broker.observe(session.id).state is ConsoleSessionState.IDLE)
        broker.submit(session.id, session.epoch, "SELECT running", "running")
        assert harness.execute_started.wait(1)
        broker.submit(session.id, session.epoch, "SELECT queued 1", "queued-1")
        broker.submit(session.id, session.epoch, "SELECT queued 2", "queued-2")
        with pytest.raises(BrokerError) as full:
            broker.submit(session.id, session.epoch, "SELECT rejected", "queued-3")
        assert full.value.code == "session_mailbox_full"
    finally:
        harness.execute_release.set()
        broker.shutdown()


def test_r5_broker_logs_only_the_hash_and_length_never_sql_text() -> None:
    logger = RecordingLogger()
    broker, store, _harness = _broker(logger=logger)
    secret_sql = "SELECT 'do-not-log-this-literal'"
    try:
        session = broker.attach(_request())
        _wait(lambda: broker.observe(session.id).state is ConsoleSessionState.IDLE)
        receipt = broker.submit(session.id, session.epoch, secret_sql, "request-1")
        _wait(
            lambda: store.statements[receipt.statement.id].state is ConsoleStatementState.SUCCEEDED
        )
        rendered = repr(logger.records)
        assert secret_sql not in rendered
        assert "do-not-log-this-literal" not in rendered
        assert any(
            "sql_hash" in values and "sql_len" in values for _event, values in logger.records
        )
        assert all("sql" not in values for _event, values in logger.records)
    finally:
        broker.shutdown()


def test_cancel_channel_failure_is_not_reported_as_confirmed() -> None:
    class BrokenCancelChannel(FakeCancelChannel):
        def cancel(self, session_marker: str) -> None:
            super().cancel(session_marker)
            raise CancelChannelError("unavailable")

    harness = DriverHarness(block_execute=True, cancel_causes_confirmation=False)
    harness.channel_factory = lambda _datasource_id: BrokenCancelChannel(harness)  # type: ignore[method-assign]
    broker, store, _ = _broker(harness)
    try:
        session = broker.attach(_request())
        _wait(lambda: broker.observe(session.id).state is ConsoleSessionState.IDLE)
        receipt = broker.submit(session.id, session.epoch, "SELECT slow", "request-1")
        assert harness.execute_started.wait(1)
        broker.cancel(receipt.statement.id, session.epoch)
        _wait(
            lambda: any(
                event.event_type == "cancel_failed"
                for event in store.events
                if event.statement_id == receipt.statement.id
            )
        )
        assert store.statements[receipt.statement.id].state is not ConsoleStatementState.CANCELLED
    finally:
        harness.execute_release.set()
        broker.shutdown()
