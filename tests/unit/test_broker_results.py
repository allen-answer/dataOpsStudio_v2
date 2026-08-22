"""lane 的结果落盘行为(Session Broker 设计 §3.2 spool 平价 / §10-A5)。

真文件系统与真 PG 都不参与:`StatementResults` 是一条协议缝,这里注入记录型
替身,断言的是 **lane 交给结果存储的调用序与内容**:

- 受理即分配 result_set_id(job 路径同型),幂等重发不再分配第二个;
- 列元数据经 `column_sink` 先于首批行落下;
- 落盘批大小 = min(spool_batch_size, page_size);
- 取够 max_result_rows 就停并标 truncated(客户端截断,连接续用);
- **取消/超时保留已落的部分行**(设计 §2.2 / §11-7,与 job 路径刻意不同);
- 终态一定 finalize —— 失败也要封存,不然结果集永远停在 streaming。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from app.broker.core import BrokerConfig, SessionBroker
from app.broker.results import StatementMetrics, StatementSpool
from app.broker.store import AttachRequest, MemoryBrokerStore
from app.dbclients.interactive import (
    CancelChannel,
    ClassifiedError,
    ErrorCategory,
    ErrorClassifier,
    InteractiveCapabilities,
    InteractiveConnection,
    InteractiveExecuteError,
    ServerCancelSupport,
    StatementRequest,
)
from app.domain.console_session import (
    ConsoleSession,
    ConsoleSessionState,
    ConsoleStatement,
    ConsoleStatementState,
)
from app.domain.datasource import DbType
from app.domain.schema import Column, ColumnType, Row

# ── 记录型替身 ────────────────────────────────────────────────────────────────


@dataclass
class RecordingResults:
    """记录 lane 的每一次落盘调用。`register` 返回可预测的 id 便于断言。"""

    appended: list[list[Row]] = field(default_factory=list)
    columns: list[Column] = field(default_factory=list)
    column_calls: int = 0
    truncated: list[str] = field(default_factory=list)
    finalized: list[tuple[ConsoleStatement, StatementMetrics]] = field(default_factory=list)
    streaming_publishes: int = 0
    registered: list[tuple[str, int, int]] = field(default_factory=list)

    def register(
        self,
        statement: ConsoleStatement,
        *,
        page_size: int,
        max_result_rows: int,
    ) -> StatementSpool:
        self.registered.append((statement.id, page_size, max_result_rows))
        return StatementSpool(
            result_set_id=f"rs-{len(self.registered)}",
            page_size=page_size,
            max_result_rows=max_result_rows,
        )

    def set_columns(self, spool: StatementSpool, columns: Sequence[Column]) -> None:
        del spool
        self.column_calls += 1
        self.columns = list(columns)

    def append(self, spool: StatementSpool, rows: Sequence[Row]) -> None:
        del spool
        self.appended.append(list(rows))

    def mark_truncated(self, spool: StatementSpool) -> None:
        self.truncated.append(spool.result_set_id)

    def loaded_rows(self, spool: StatementSpool) -> int:
        del spool
        return sum(len(batch) for batch in self.appended)

    def publish_streaming(
        self,
        spool: StatementSpool,
        statement: ConsoleStatement,
        *,
        columns: Sequence[Column],
    ) -> None:
        del spool, statement, columns
        self.streaming_publishes += 1

    def finalize(
        self,
        spool: StatementSpool,
        statement: ConsoleStatement,
        *,
        columns: Sequence[Column],
        metrics: StatementMetrics,
    ) -> None:
        del spool, columns
        self.finalized.append((statement, metrics))

    @property
    def rows(self) -> list[Row]:
        return [row for batch in self.appended for row in batch]


class FakeClassifier:
    db_type: DbType = DbType.MYSQL

    def classify(self, _exc: BaseException) -> ClassifiedError:
        return ClassifiedError(ErrorCategory.UNKNOWN)


@dataclass
class Harness:
    """产出 `row_count` 行的 fake 连接;`block_after_rows` 让语句停在流式中途。"""

    row_count: int = 3
    columns: list[Column] = field(
        default_factory=lambda: [Column(name="id", type=ColumnType.INTEGER)]
    )
    block_after_rows: int | None = None
    fail_with: ClassifiedError | None = None
    streaming_started: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    fetch_sizes: list[int] = field(default_factory=list)
    connections: list[FakeConnection] = field(default_factory=list)

    def connection_factory(self, _session: ConsoleSession) -> InteractiveConnection:
        connection = FakeConnection(self)
        self.connections.append(connection)
        return connection

    def channel_factory(self, _datasource_id: str) -> CancelChannel:
        return FakeCancelChannel(self)


class FakeConnection:
    db_type = DbType.MYSQL
    classifier: ErrorClassifier = FakeClassifier()
    capabilities = InteractiveCapabilities(
        server_cancel=True,
        server_statement_timeout=True,
        session_streaming=True,
    )

    def __init__(self, harness: Harness) -> None:
        self._harness = harness
        self._open = False
        self._soft_cancel = threading.Event()

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def session_marker(self) -> str | None:
        return "100" if self._open else None

    def open(self) -> str:
        self._open = True
        return "100"

    def execute(self, request: StatementRequest) -> Iterator[Row]:
        self._harness.fetch_sizes.append(request.fetch_size)
        if request.column_sink is not None:
            request.column_sink(list(self._harness.columns))
        return self._rows()

    def _rows(self) -> Iterator[Row]:
        for index in range(self._harness.row_count):
            if (
                self._harness.block_after_rows is not None
                and index == self._harness.block_after_rows
            ):
                self._harness.streaming_started.set()
                assert self._harness.release.wait(3), "test did not release the row stream"
                if self._soft_cancel.is_set():
                    raise InteractiveExecuteError(
                        "cancelled",
                        self._harness.fail_with
                        or ClassifiedError(
                            ErrorCategory.CANCELLED,
                            driver_code=1317,
                            server_confirmed=True,
                        ),
                    )
            yield Row(values=[index])

    def request_soft_cancel(self) -> None:
        self._soft_cancel.set()
        self._harness.release.set()

    def clear_soft_cancel(self) -> None:
        self._soft_cancel.clear()
        self._harness.streaming_started.clear()
        self._harness.release.clear()

    @property
    def soft_cancel_requested(self) -> bool:
        return self._soft_cancel.is_set()

    def ping(self) -> bool:
        return self._open

    def close(self) -> None:
        self._open = False
        self._harness.release.set()


class FakeCancelChannel:
    db_type = DbType.MYSQL
    classifier: ErrorClassifier = FakeClassifier()

    def __init__(self, harness: Harness) -> None:
        self._harness = harness
        self._support = ServerCancelSupport.UNKNOWN

    @property
    def support(self) -> ServerCancelSupport:
        return self._support

    def open(self) -> ServerCancelSupport:
        self._support = ServerCancelSupport.AVAILABLE
        return self._support

    def cancel(self, session_marker: str) -> None:
        del session_marker

    def destroy(self, session_marker: str) -> None:
        del session_marker
        self._harness.release.set()

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        return None


@dataclass
class FakeClock:
    value: datetime = datetime(2026, 8, 22, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.value += timedelta(milliseconds=1)
        return self.value


def _broker(
    harness: Harness,
    results: RecordingResults,
    *,
    spool_batch_size: int = 1000,
) -> tuple[SessionBroker, MemoryBrokerStore]:
    store = MemoryBrokerStore()
    broker = SessionBroker(
        store,
        connection_factory=harness.connection_factory,
        cancel_channel_factory=harness.channel_factory,
        results=results,
        clock=FakeClock(),
        config=BrokerConfig(timer_poll_seconds=3600, spool_batch_size=spool_batch_size),
        boot_id="boot-results",
    )
    broker.start()
    return broker, store


def _request() -> AttachRequest:
    return AttachRequest(console_id="console-1", datasource_id="ds-1", owner_user_id="user-1")


def _wait(predicate: object, timeout: float = 3.0) -> None:
    assert callable(predicate)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition did not become true")


# ── 用例 ──────────────────────────────────────────────────────────────────────


def test_submit_allocates_a_result_set_and_lane_spools_every_row() -> None:
    harness = Harness(row_count=3)
    results = RecordingResults()
    broker, store = _broker(harness, results)
    try:
        session = broker.attach(_request())
        _wait(lambda: broker.observe(session.id).state is ConsoleSessionState.IDLE)
        receipt = broker.submit(session.id, session.epoch, "SELECT 1", "req-1")

        assert receipt.statement.result_set_id == "rs-1"
        _wait(
            lambda: store.statements[receipt.statement.id].state is ConsoleStatementState.SUCCEEDED
        )
        # 受理时就写进持久层:重启后的 observe / 导出都靠这一列找结果集。
        assert store.statements[receipt.statement.id].result_set_id == "rs-1"
        assert [row.values for row in results.rows] == [[0], [1], [2]]
        assert results.columns == harness.columns
        assert len(results.finalized) == 1
    finally:
        broker.shutdown()


def test_columns_land_before_the_first_row_batch() -> None:
    """列元数据先于行 —— 前端 `columns_ready` 早于 `first_batch_ready` 的前提。"""

    harness = Harness(row_count=2)
    results = RecordingResults()
    broker, store = _broker(harness, results)
    try:
        session = broker.attach(_request())
        _wait(lambda: broker.observe(session.id).state is ConsoleSessionState.IDLE)
        receipt = broker.submit(session.id, session.epoch, "SELECT 1", "req-1")
        _wait(
            lambda: store.statements[receipt.statement.id].state is ConsoleStatementState.SUCCEEDED
        )

        assert results.column_calls == 1
        assert results.columns == harness.columns
    finally:
        broker.shutdown()


def test_idempotent_resubmit_reuses_the_same_result_set() -> None:
    """幂等回执不得分配第二个结果集 —— 否则重发一次就漏一份 spool。"""

    harness = Harness(row_count=1)
    results = RecordingResults()
    broker, _store = _broker(harness, results)
    try:
        session = broker.attach(_request())
        _wait(lambda: broker.observe(session.id).state is ConsoleSessionState.IDLE)
        first = broker.submit(session.id, session.epoch, "SELECT 1", "req-1")
        second = broker.submit(session.id, session.epoch, "SELECT 1", "req-1")

        assert second.deduplicated is True
        assert second.statement.result_set_id == first.statement.result_set_id
        assert len(results.registered) == 1
    finally:
        broker.shutdown()


def test_spool_batch_size_is_capped_by_page_size() -> None:
    harness = Harness(row_count=5)
    results = RecordingResults()
    broker, store = _broker(harness, results, spool_batch_size=1000)
    try:
        session = broker.attach(_request())
        _wait(lambda: broker.observe(session.id).state is ConsoleSessionState.IDLE)
        receipt = broker.submit(
            session.id,
            session.epoch,
            "SELECT 1",
            "req-1",
            page_size=2,
            max_result_rows=1000,
        )
        _wait(
            lambda: store.statements[receipt.statement.id].state is ConsoleStatementState.SUCCEEDED
        )

        assert [len(batch) for batch in results.appended] == [2, 2, 1]
        # 驱动侧 fetchmany 也按 page_size 走(多取一行判"还有更多")。
        assert harness.fetch_sizes == [2]
    finally:
        broker.shutdown()


def test_max_result_rows_truncates_client_side_without_killing_the_session() -> None:
    """取够就停 = DataGrip 语义:标 truncated,会话照常回 idle 可续用。"""

    harness = Harness(row_count=10)
    results = RecordingResults()
    broker, store = _broker(harness, results)
    try:
        session = broker.attach(_request())
        _wait(lambda: broker.observe(session.id).state is ConsoleSessionState.IDLE)
        receipt = broker.submit(
            session.id,
            session.epoch,
            "SELECT 1",
            "req-1",
            page_size=100,
            max_result_rows=4,
        )
        _wait(
            lambda: store.statements[receipt.statement.id].state is ConsoleStatementState.SUCCEEDED
        )

        assert len(results.rows) == 4
        assert results.truncated == ["rs-1"]
        _, metrics = results.finalized[0]
        assert metrics.output_limit_applied is True
        # 多读一行只为证明"还有更多",不落盘。
        assert metrics.rows_read == 5
        assert metrics.rows_returned == 4
        assert broker.observe(session.id).state is ConsoleSessionState.IDLE
    finally:
        broker.shutdown()


def test_cancelled_statement_keeps_the_rows_already_spooled() -> None:
    """★ 与 job 路径刻意不同(设计 §11-7):取消**不删** spool。"""

    harness = Harness(row_count=10, block_after_rows=2)
    results = RecordingResults()
    broker, store = _broker(harness, results, spool_batch_size=1)
    try:
        session = broker.attach(_request())
        _wait(lambda: broker.observe(session.id).state is ConsoleSessionState.IDLE)
        receipt = broker.submit(session.id, session.epoch, "SELECT 1", "req-1", page_size=1)
        assert harness.streaming_started.wait(3)

        broker.cancel(receipt.statement.id, session.epoch)
        _wait(
            lambda: store.statements[receipt.statement.id].state is ConsoleStatementState.CANCELLED
        )

        assert [row.values for row in results.rows] == [[0], [1]]
        # 封存(而不是删除)才让"已取消,保留前 N 行"成立。
        assert len(results.finalized) == 1
        assert results.truncated == []
    finally:
        harness.release.set()
        broker.shutdown()


def test_failed_statement_still_finalizes_its_result_set() -> None:
    """失败也要封存:结果集停在 streaming 会让前端永远显示"加载中"。"""

    harness = Harness(
        row_count=10,
        block_after_rows=1,
        fail_with=ClassifiedError(ErrorCategory.STATEMENT_ERROR, driver_code=1064),
    )
    results = RecordingResults()
    broker, store = _broker(harness, results, spool_batch_size=1)
    try:
        session = broker.attach(_request())
        _wait(lambda: broker.observe(session.id).state is ConsoleSessionState.IDLE)
        receipt = broker.submit(session.id, session.epoch, "SELECT 1", "req-1", page_size=1)
        assert harness.streaming_started.wait(3)
        broker.cancel(receipt.statement.id, session.epoch)
        _wait(
            lambda: (
                store.statements[receipt.statement.id].state
                in {ConsoleStatementState.CANCELLED, ConsoleStatementState.FAILED}
            )
        )

        assert len(results.finalized) == 1
    finally:
        harness.release.set()
        broker.shutdown()


class FailingResults(RecordingResults):
    """`register` 必然失败(spool 磁盘满 / PG 短暂不可用,设计 F9/F10)。"""

    def register(
        self,
        statement: ConsoleStatement,
        *,
        page_size: int,
        max_result_rows: int,
    ) -> StatementSpool:
        del statement, page_size, max_result_rows
        raise RuntimeError("result store is unavailable")


def test_statement_fails_closed_when_the_result_set_cannot_be_created() -> None:
    """建不出结果集就**别排队**:排进去它会永远停在 accepted,前端白转圈。

    F9 口径:语句 failed(resultstore_error),会话续用。幂等重发看到的是这条
    失败语句 —— 而不是"什么都没发生"的静默。
    """

    harness = Harness(row_count=1)
    results = FailingResults()
    broker, store = _broker(harness, results)
    try:
        session = broker.attach(_request())
        _wait(lambda: broker.observe(session.id).state is ConsoleSessionState.IDLE)

        with pytest.raises(RuntimeError):
            broker.submit(session.id, session.epoch, "SELECT 1", "req-1")

        statement = next(iter(store.statements.values()))
        assert statement.state is ConsoleStatementState.FAILED
        assert statement.error_code == "resultstore_error"
        assert results.appended == []
        # 会话没被这条语句带倒,还能接下一条。
        assert broker.observe(session.id).state is ConsoleSessionState.IDLE
        replay = broker.submit(session.id, session.epoch, "SELECT 1", "req-1")
        assert replay.deduplicated is True
        assert replay.statement.state is ConsoleStatementState.FAILED
    finally:
        broker.shutdown()
