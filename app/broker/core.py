"""Session Broker 运行时: 注册表、owner/control lanes、timer 与 fencing。

驱动调用只发生在 ``app/dbclients/interactive`` 对象各自的 owner 线程; broker
只依赖协议, 不 import 任一数据库驱动 (R1)。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from queue import Empty, Full, Queue
from threading import Condition, Event, Lock, RLock, Thread
from typing import Protocol, cast
from uuid import uuid4

import structlog

from app.broker.results import (
    NullStatementResults,
    StatementMetrics,
    StatementResults,
    StatementSpool,
)
from app.broker.state import transition_session, transition_statement
from app.broker.store import AttachRequest, BrokerStore, SweepReport
from app.dbclients.interactive import (
    CancelChannel,
    CancelChannelError,
    ErrorCategory,
    InteractiveConnectError,
    InteractiveConnection,
    InteractiveExecuteError,
    ServerCancelSupport,
    SoftCancelledError,
    StatementRequest,
)
from app.dbclients.query_limit import analyze_database_row_limit, apply_database_row_limit
from app.domain.console_session import (
    ACTIVE_CONSOLE_SESSION_STATES,
    ConsoleSession,
    ConsoleSessionState,
    ConsoleStatement,
    ConsoleStatementState,
    ServerCancelState,
)
from app.domain.schema import Column, Row

Clock = Callable[[], datetime]
ConnectionFactory = Callable[[ConsoleSession], InteractiveConnection]
CancelChannelFactory = Callable[[str], CancelChannel]


class BrokerLogger(Protocol):
    def info(self, event: str, **kwargs: object) -> object: ...


class BrokerError(RuntimeError):
    """A4 可直接映射到 HTTP error code 的 broker 领域错误。"""

    def __init__(self, code: str, *, current_epoch: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.current_epoch = current_epoch


@dataclass(frozen=True, slots=True)
class SessionLimits:
    per_user: int = 8
    per_datasource: int = 4
    global_total: int = 16

    def __post_init__(self) -> None:
        if min(self.per_user, self.per_datasource, self.global_total) <= 0:
            raise ValueError("session limits must be positive")


@dataclass(frozen=True, slots=True)
class BrokerConfig:
    limits: SessionLimits = field(default_factory=SessionLimits)
    mailbox_size: int = 20
    idle_timeout_seconds: int = 1800
    cancel_grace_seconds: float = 5.0
    timer_poll_seconds: float = 0.25
    # 落 spool 的批大小上限(与 worker 的 sql_spool_batch_size 同口径);
    # 实际批大小 = min(本值, 语句 page_size)。
    spool_batch_size: int = 1000

    def __post_init__(self) -> None:
        if self.mailbox_size <= 0:
            raise ValueError("mailbox_size must be positive")
        if self.spool_batch_size <= 0:
            raise ValueError("spool_batch_size must be positive")
        if self.idle_timeout_seconds <= 0:
            raise ValueError("idle_timeout_seconds must be positive")
        if self.cancel_grace_seconds < 0:
            raise ValueError("cancel_grace_seconds must not be negative")
        if self.timer_poll_seconds <= 0:
            raise ValueError("timer_poll_seconds must be positive")


@dataclass(frozen=True, slots=True)
class SessionObservation:
    session: ConsoleSession
    current_statement_id: str | None
    idle_deadline: datetime | None

    @property
    def state(self) -> ConsoleSessionState:
        return self.session.state

    @property
    def current_epoch(self) -> int:
        return self.session.epoch


@dataclass(frozen=True, slots=True)
class SubmitReceipt:
    statement: ConsoleStatement
    deduplicated: bool


@dataclass(frozen=True, slots=True)
class CancelReceipt:
    accepted: bool
    statement_state: ConsoleStatementState


@dataclass(frozen=True, slots=True)
class _LaneCommand:
    kind: str
    statement_id: str | None = None
    close_reason: str | None = None


@dataclass(frozen=True, slots=True)
class _ControlCommand:
    kind: str
    runtime: _SessionRuntime


@dataclass(slots=True)
class _SessionRuntime:
    session: ConsoleSession
    connection: InteractiveConnection
    mailbox: Queue[_LaneCommand]
    condition: Condition
    statements: dict[str, ConsoleStatement] = field(default_factory=dict)
    requests: dict[str, str] = field(default_factory=dict)
    # statement_id → 结果集句柄(submit 时分配,lane 执行期落 spool)。
    spools: dict[str, StatementSpool] = field(default_factory=dict)
    accepting: bool = True
    pending_count: int = 0
    current_statement_id: str | None = None
    statement_deadline: datetime | None = None
    idle_deadline: datetime | None = None
    lane_thread: Thread | None = None
    cancel_in_flight: bool = False
    cancel_statement_id: str | None = None
    cancel_reason: str | None = None
    cancel_control_done: bool = False
    cancel_statement_done: bool = False
    cancel_confirmed: bool = False
    cancel_grace_deadline: datetime | None = None
    cancel_destroy_sent: bool = False
    close_reason_pending: str | None = None


class SessionBroker:
    """API 进程内、单实例的交互会话所有者 (设计 D1)。"""

    def __init__(
        self,
        store: BrokerStore,
        *,
        connection_factory: ConnectionFactory,
        cancel_channel_factory: CancelChannelFactory,
        results: StatementResults | None = None,
        config: BrokerConfig | None = None,
        clock: Clock | None = None,
        boot_id: str | None = None,
        logger: BrokerLogger | None = None,
    ) -> None:
        self._store = store
        self._connection_factory = connection_factory
        self._cancel_channel_factory = cancel_channel_factory
        # 缺省是不落盘替身:状态机单测不需要真 spool。生产装配在
        # `app/broker/wiring.py` 注入 `SpoolStatementResults`。
        self._results = results or NullStatementResults()
        self._config = config or BrokerConfig()
        self._clock = clock or _utc_now
        self._boot_id = boot_id or str(uuid4())
        self._logger = logger or cast(BrokerLogger, structlog.get_logger(__name__))
        self._lock = RLock()
        self._attach_lock = Lock()
        self._sessions: dict[str, _SessionRuntime] = {}
        self._console_sessions: dict[str, str] = {}
        self._statement_sessions: dict[str, str] = {}
        self._controls: dict[str, _ControlLane] = {}
        self._started = False
        self._shutdown = False
        self._timer_stop = Event()
        self._timer_thread: Thread | None = None
        self._sweep_report = SweepReport()

    @property
    def boot_id(self) -> str:
        return self._boot_id

    @property
    def sweep_report(self) -> SweepReport:
        return self._sweep_report

    def start(self) -> SweepReport:
        with self._lock:
            if self._started:
                return self._sweep_report
            if self._shutdown:
                raise RuntimeError("broker has been shut down")
            now = self._clock()
            self._sweep_report = self._store.startup_sweep(current_boot_id=self._boot_id, now=now)
            self._started = True
            self._timer_thread = Thread(
                target=self._timer_loop,
                name="session-broker-timer",
                daemon=True,
            )
            self._timer_thread.start()
            return self._sweep_report

    def attach(self, request: AttachRequest) -> ConsoleSession:
        # attach 包含一次 PG epoch 分配与可能的 lane 创建; 独立锁让并发调用的返回值
        # 也保持各自拿到的 epoch 快照 (M4), 而不只是最终持久态正确。
        with self._attach_lock:
            return self._attach_serial(request)

    def _attach_serial(self, request: AttachRequest) -> ConsoleSession:
        self._require_started()
        new_runtime: _SessionRuntime | None = None
        with self._lock:
            existing_id = self._console_sessions.get(request.console_id)
            existing = self._sessions.get(existing_id) if existing_id is not None else None
            if existing is not None and existing.session.state in ACTIVE_CONSOLE_SESSION_STATES:
                attached = self._store.attach_session(
                    request,
                    session_id=existing.session.id,
                    broker_boot_id=self._boot_id,
                    now=self._clock(),
                    reuse_session_id=existing.session.id,
                )
                existing.session = attached
                existing.idle_deadline = self._idle_deadline(attached.last_activity_at)
                return attached

            self._enforce_session_limits(request)
            now = self._clock()
            session = self._store.attach_session(
                request,
                session_id=str(uuid4()),
                broker_boot_id=self._boot_id,
                now=now,
            )
            new_runtime = _SessionRuntime(
                session=session,
                connection=self._connection_factory(session),
                mailbox=Queue(maxsize=self._config.mailbox_size),
                condition=Condition(self._lock),
                idle_deadline=self._idle_deadline(now),
            )
            self._sessions[session.id] = new_runtime
            self._console_sessions[session.console_id] = session.id

        control = self._ensure_control_lane(request.datasource_id)
        support = control.wait_ready()
        with self._lock:
            if new_runtime.session.state is ConsoleSessionState.CONNECTING:
                new_runtime.session = replace(
                    new_runtime.session,
                    server_cancel=ServerCancelState(support.value),
                )
                self._store.update_session(new_runtime.session)
            thread = Thread(
                target=self._lane_loop,
                args=(new_runtime,),
                name=f"session-lane-{new_runtime.session.id}",
                daemon=True,
            )
            new_runtime.lane_thread = thread
            thread.start()
            return new_runtime.session

    def observe(self, session_id: str) -> SessionObservation:
        with self._lock:
            runtime = self._sessions.get(session_id)
            if runtime is None:
                persisted = self._store.get_session(session_id)
                if persisted is None:
                    raise BrokerError("session_lost")
                return SessionObservation(
                    session=persisted,
                    current_statement_id=None,
                    idle_deadline=None,
                )
            return SessionObservation(
                session=runtime.session,
                current_statement_id=runtime.current_statement_id,
                idle_deadline=runtime.idle_deadline,
            )

    def statement(self, statement_id: str) -> ConsoleStatement:
        """读取内存中的最新语句快照, 重启后回退 PG 审计锚点。"""
        with self._lock:
            session_id = self._statement_sessions.get(statement_id)
            if session_id is not None:
                return self._sessions[session_id].statements[statement_id]
            persisted = self._store.get_statement(statement_id)
            if persisted is None:
                raise BrokerError("statement_not_found")
            return persisted

    def active_sessions(self) -> list[ConsoleSession]:
        with self._lock:
            return [
                runtime.session
                for runtime in self._sessions.values()
                if runtime.session.state in ACTIVE_CONSOLE_SESSION_STATES
            ]

    def submit(
        self,
        session_id: str,
        epoch: int,
        sql: str,
        client_request_id: str,
        *,
        timeout_seconds: int = 600,
        page_size: int = 100,
        max_result_rows: int = 1000,
    ) -> SubmitReceipt:
        if not client_request_id or len(client_request_id) > 64:
            raise ValueError("client_request_id must contain 1..64 characters")
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must not be negative")
        if page_size <= 0 or max_result_rows <= 0:
            raise ValueError("page_size and max_result_rows must be positive")
        with self._lock:
            runtime = self._require_runtime(session_id)
            self._require_epoch(runtime, epoch)
            replay_id = runtime.requests.get(client_request_id)
            if replay_id is not None:
                return SubmitReceipt(runtime.statements[replay_id], deduplicated=True)
            if not runtime.accepting or runtime.session.state not in ACTIVE_CONSOLE_SESSION_STATES:
                raise BrokerError("session_not_active", current_epoch=runtime.session.epoch)
            if runtime.pending_count >= self._config.mailbox_size:
                raise BrokerError("session_mailbox_full", current_epoch=runtime.session.epoch)
            now = self._clock()
            digest = f"sha256:{sha256(sql.encode('utf-8')).hexdigest()}"
            statement, deduplicated = self._store.create_statement(
                runtime.session,
                statement_id=str(uuid4()),
                client_request_id=client_request_id,
                sql=sql,
                sql_hash=digest,
                timeout_seconds=timeout_seconds,
                now=now,
            )
            runtime.statements[statement.id] = statement
            runtime.requests[client_request_id] = statement.id
            self._statement_sessions[statement.id] = runtime.session.id
            if deduplicated:
                return SubmitReceipt(statement, deduplicated=True)
            # 结果集在**受理时**分配(job 路径同型):前端拿到 submit 回执即可
            # 开始按 result_set_id 轮询,不必等 lane 排到这条语句。
            try:
                spool = self._results.register(
                    statement,
                    page_size=page_size,
                    max_result_rows=max_result_rows,
                )
            except Exception:
                # 结果集建不出来就**别把语句排进 mailbox** —— 否则它会永远停在
                # accepted,前端轮到天荒地老。按 F9 记 failed(resultstore_error):
                # 会话续用,幂等重发看到的是这条失败语句而不是"什么都没发生"。
                self._set_statement_state(
                    runtime,
                    statement,
                    ConsoleStatementState.FAILED,
                    now=now,
                    error_code="resultstore_error",
                    error_summary="statement result set could not be created",
                )
                self._record_event(runtime, runtime.statements[statement.id], "terminal", now=now)
                raise
            runtime.spools[statement.id] = spool
            statement = replace(statement, result_set_id=spool.result_set_id)
            runtime.statements[statement.id] = statement
            self._store.update_statement(statement)
            try:
                runtime.mailbox.put_nowait(_LaneCommand("execute", statement_id=statement.id))
            except Full as exc:  # pending_count 与 Queue 必须同锁同向; 命中即内部不变量坏。
                raise RuntimeError("broker mailbox accounting diverged") from exc
            runtime.pending_count += 1
            self._record_event(runtime, statement, "submitted", now=now)
            return SubmitReceipt(statement, deduplicated=False)

    def cancel(self, statement_id: str, epoch: int) -> CancelReceipt:
        with self._lock:
            runtime = self._runtime_for_statement(statement_id)
            self._require_epoch(runtime, epoch)
            statement = runtime.statements[statement_id]
            if statement.state is ConsoleStatementState.ACCEPTED:
                statement = self._set_statement_state(
                    runtime,
                    statement,
                    ConsoleStatementState.CANCELLED,
                    now=self._clock(),
                    cancel_requested=True,
                )
                self._record_event(runtime, statement, "cancel_requested", now=self._clock())
                return CancelReceipt(True, statement.state)
            if runtime.current_statement_id == statement_id and statement.state in (
                ConsoleStatementState.EXECUTING,
                ConsoleStatementState.STREAMING,
            ):
                self._begin_cancel_locked(runtime, statement, reason="user")
                return CancelReceipt(True, runtime.statements[statement_id].state)
            if statement.state in _TERMINAL_STATEMENT_STATES:
                return CancelReceipt(False, statement.state)
            raise BrokerError("statement_not_cancellable", current_epoch=runtime.session.epoch)

    def close(self, session_id: str, epoch: int, *, reason: str = "user") -> None:
        with self._lock:
            runtime = self._require_runtime(session_id)
            self._require_epoch(runtime, epoch)
            self._queue_close_locked(runtime, reason=reason, cancel_current=True)

    def run_timers_once(self) -> None:
        """执行一轮 timer 决策; fake clock 单测可确定地逐级推进。"""
        now = self._clock()
        with self._lock:
            for runtime in tuple(self._sessions.values()):
                if (
                    runtime.current_statement_id is not None
                    and runtime.statement_deadline is not None
                    and now >= runtime.statement_deadline
                    and not runtime.cancel_in_flight
                ):
                    statement = runtime.statements[runtime.current_statement_id]
                    self._begin_cancel_locked(runtime, statement, reason="timeout")
                if (
                    runtime.cancel_in_flight
                    and not runtime.cancel_destroy_sent
                    and runtime.cancel_grace_deadline is not None
                    and now >= runtime.cancel_grace_deadline
                ):
                    runtime.cancel_destroy_sent = True
                    control = self._controls.get(runtime.session.datasource_id)
                    if control is not None:
                        control.submit(_ControlCommand("destroy", runtime))
                if (
                    runtime.accepting
                    and runtime.session.state is ConsoleSessionState.IDLE
                    and runtime.idle_deadline is not None
                    and now >= runtime.idle_deadline
                ):
                    self._queue_close_locked(runtime, reason="idle_timeout", cancel_current=False)

    def shutdown(self) -> None:
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            self._timer_stop.set()
            for runtime in self._sessions.values():
                if runtime.session.state in ACTIVE_CONSOLE_SESSION_STATES:
                    self._queue_close_locked(runtime, reason="shutdown", cancel_current=True)
            lane_threads = [
                runtime.lane_thread
                for runtime in self._sessions.values()
                if runtime.lane_thread is not None
            ]
        for thread in lane_threads:
            thread.join(timeout=5)
        with self._lock:
            controls = list(self._controls.values())
        for control in controls:
            control.stop()
        timer_thread = self._timer_thread
        if timer_thread is not None:
            timer_thread.join(timeout=2)

    # ── lane ──────────────────────────────────────────────────────────────

    def _lane_loop(self, runtime: _SessionRuntime) -> None:
        try:
            marker = runtime.connection.open()
        except InteractiveConnectError as exc:
            with self._lock:
                now = self._clock()
                runtime.accepting = False
                runtime.session = replace(
                    runtime.session,
                    state=transition_session(
                        runtime.session.state, ConsoleSessionState.CONNECT_FAILED
                    ),
                    error_code=exc.classified.error_code,
                    closed_at=now,
                    close_reason="connect_failed",
                    last_activity_at=now,
                )
                self._store.update_session(runtime.session)
                runtime.condition.notify_all()
            return
        except Exception:
            with self._lock:
                now = self._clock()
                runtime.accepting = False
                runtime.session = replace(
                    runtime.session,
                    state=transition_session(
                        runtime.session.state, ConsoleSessionState.CONNECT_FAILED
                    ),
                    error_code="connect_failed",
                    closed_at=now,
                    close_reason="connect_failed",
                    last_activity_at=now,
                )
                self._store.update_session(runtime.session)
                runtime.condition.notify_all()
            return

        with self._lock:
            now = self._clock()
            runtime.session = replace(
                runtime.session,
                state=transition_session(runtime.session.state, ConsoleSessionState.IDLE),
                db_session_marker=marker,
                last_activity_at=now,
            )
            runtime.idle_deadline = self._idle_deadline(now)
            self._store.update_session(runtime.session)
            runtime.condition.notify_all()

        while True:
            command = runtime.mailbox.get()
            if command.kind == "execute":
                with self._lock:
                    runtime.pending_count -= 1
                if command.statement_id is not None:
                    self._execute_statement(runtime, command.statement_id)
                if self._close_if_pending_and_drained(runtime):
                    return
                continue
            if command.kind == "close":
                self._close_on_lane(runtime, command.close_reason or "user")
                return

    def _execute_statement(self, runtime: _SessionRuntime, statement_id: str) -> None:
        with self._lock:
            while (
                runtime.cancel_in_flight and runtime.session.state in ACTIVE_CONSOLE_SESSION_STATES
            ):
                runtime.condition.wait(timeout=0.25)
            statement = runtime.statements[statement_id]
            if statement.state is not ConsoleStatementState.ACCEPTED:
                return
            if not runtime.accepting and runtime.session.state not in (
                ConsoleSessionState.IDLE,
                ConsoleSessionState.EXECUTING,
            ):
                return
            now = self._clock()
            runtime.connection.clear_soft_cancel()
            runtime.current_statement_id = statement.id
            runtime.statement_deadline = (
                now + timedelta(seconds=statement.timeout_seconds)
                if statement.timeout_seconds > 0
                else None
            )
            runtime.session = replace(
                runtime.session,
                state=transition_session(runtime.session.state, ConsoleSessionState.EXECUTING),
                last_activity_at=now,
            )
            runtime.statements[statement.id] = replace(
                statement,
                state=transition_statement(statement.state, ConsoleStatementState.EXECUTING),
                started_at=now,
            )
            self._store.update_session(runtime.session)
            self._store.update_statement(runtime.statements[statement.id])
            self._record_event(runtime, runtime.statements[statement.id], "started", now=now)

        terminal_state: ConsoleStatementState | None = None
        error_code: str | None = None
        error_summary: str | None = None
        connection_lost = False
        spool = runtime.spools.get(statement.id)
        columns: list[Column] = []
        execution_sql = statement.sql_text
        metrics = StatementMetrics(
            execute_started_at=now,
            db_type=runtime.connection.db_type.value,
            effective_sql_hash=sha256(execution_sql.encode("utf-8")).hexdigest(),
        )
        try:
            if spool is not None:
                execution_sql = apply_database_row_limit(
                    statement.sql_text,
                    runtime.connection.db_type,
                    spool.max_result_rows + 1,
                )
                limit_analysis = analyze_database_row_limit(
                    statement.sql_text,
                    runtime.connection.db_type,
                )
                metrics.effective_sql_hash = sha256(execution_sql.encode("utf-8")).hexdigest()
                metrics.query_shape = limit_analysis.query_shape
                metrics.limit_pushdown = limit_analysis.limit_pushdown
                metrics.limit_pushdown_reason = limit_analysis.limit_pushdown_reason

            def capture_columns(emitted: list[Column]) -> None:
                columns.clear()
                columns.extend(emitted)
                if spool is not None:
                    self._results.set_columns(spool, emitted)

            stream = runtime.connection.execute(
                StatementRequest(
                    sql=execution_sql,
                    fetch_size=self._fetch_size(spool),
                    timeout_seconds=statement.timeout_seconds,
                    column_sink=capture_columns,
                )
            )
            with self._lock:
                current = runtime.statements[statement.id]
                if current.state is ConsoleStatementState.EXECUTING:
                    runtime.statements[statement.id] = replace(
                        current,
                        state=transition_statement(current.state, ConsoleStatementState.STREAMING),
                    )
                    self._store.update_statement(runtime.statements[statement.id])
            self._drain_to_spool(statement, stream, spool, columns, metrics)
            terminal_state = ConsoleStatementState.SUCCEEDED
        except (InteractiveExecuteError, SoftCancelledError) as exc:
            classified = exc.classified
            error_code = classified.error_code
            # `str(exc)` 是缝里写死的常量("interactive statement failed"),对用户
            # 零信息量;真实原因(脱敏后的驱动原文 + 数值码)在 `classified.summary`
            # 里,那正是 protocol.py 给该字段写明的用途 —— 供 `error_summary` 列。
            # 常量只在 summary 为空(分类器没造出摘要)时兜底。
            error_summary = classified.summary or str(exc)
            if classified.connection_aborted:
                connection_lost = True
                terminal_state = (
                    ConsoleStatementState.OUTCOME_UNKNOWN
                    if statement.is_write
                    else ConsoleStatementState.FAILED
                )
            elif classified.category is ErrorCategory.TIMEOUT:
                terminal_state = ConsoleStatementState.TIMEOUT
            elif classified.category is ErrorCategory.CANCELLED:
                with self._lock:
                    reason = runtime.cancel_reason
                    if classified.server_confirmed:
                        runtime.cancel_confirmed = True
                terminal_state = (
                    ConsoleStatementState.TIMEOUT
                    if reason == "timeout"
                    else ConsoleStatementState.CANCELLED
                )
            elif classified.category is ErrorCategory.UNKNOWN and not runtime.connection.ping():
                connection_lost = True
                terminal_state = (
                    ConsoleStatementState.OUTCOME_UNKNOWN
                    if statement.is_write
                    else ConsoleStatementState.FAILED
                )
            else:
                terminal_state = ConsoleStatementState.FAILED
        except Exception:
            terminal_state = ConsoleStatementState.FAILED
            error_code = "broker_execution_error"
            error_summary = "statement execution failed"

        if metrics.finished_reading_at is None:
            metrics.finished_reading_at = self._clock()

        # 先封存结果集,**再**把语句翻到终态:反过来会开一个"progress 已报
        # terminal、结果集还停在 streaming"的窗口,前端读到的就是自相矛盾的一页。
        # 锁外做(一次 PG 写 + 一次 manifest 读),别让别的会话 attach/observe 陪等。
        # 终态是 cancelled/timeout 也照样封存 —— 已落的行保持可读可导出
        # (设计 §2.2 / §11-7,与 job 路径"取消即删 spool"刻意不同)。
        if spool is not None:
            try:
                self._results.finalize(
                    spool,
                    runtime.statements[statement.id],
                    columns=columns,
                    metrics=metrics,
                )
            except Exception:
                self._logger.info(
                    "broker statement result finalize failed",
                    statement_id=statement.id,
                    sql_hash=statement.sql_hash,
                )

        with self._lock:
            now = self._clock()
            current = runtime.statements[statement.id]
            if current.state not in _TERMINAL_STATEMENT_STATES and terminal_state is not None:
                self._set_statement_state(
                    runtime,
                    current,
                    terminal_state,
                    now=now,
                    error_code=error_code,
                    error_summary=error_summary,
                )
                self._record_event(runtime, runtime.statements[statement.id], "terminal", now=now)
            if connection_lost and runtime.session.state is not ConsoleSessionState.SESSION_LOST:
                self._lose_session_locked(runtime, reason="connection_dead", now=now)
            if runtime.current_statement_id == statement.id:
                runtime.current_statement_id = None
                runtime.statement_deadline = None
            self._settle_cancel_fence_locked(runtime, statement.id, now=now)
            if not runtime.cancel_in_flight and runtime.session.state in (
                ConsoleSessionState.EXECUTING,
                ConsoleSessionState.CANCELLING,
            ):
                runtime.session = replace(
                    runtime.session,
                    state=transition_session(runtime.session.state, ConsoleSessionState.IDLE),
                    last_activity_at=now,
                )
                runtime.idle_deadline = self._idle_deadline(now)
                self._store.update_session(runtime.session)
            runtime.condition.notify_all()
            lost = runtime.session.state is ConsoleSessionState.SESSION_LOST
        if lost:
            runtime.connection.close()

    def _fetch_size(self, spool: StatementSpool | None) -> int:
        """驱动侧 fetchmany 批大小。多取一行用于判"还有更多"(job 路径同型)。"""

        if spool is None:
            return self._config.spool_batch_size
        return max(1, min(spool.page_size, spool.max_result_rows + 1))

    def _drain_to_spool(
        self,
        statement: ConsoleStatement,
        stream: Iterator[Row],
        spool: StatementSpool | None,
        columns: list[Column],
        metrics: StatementMetrics,
    ) -> None:
        """取数循环:批量落 spool,取够 max_result_rows 就停并标 truncated。

        数据库端已对输出应用 max_result_rows+1 窗口;这里仍保留客户端截断,
        用多出的 1 行判断 has_more,并作为驱动/方言差异下的安全兜底。会话与
        游标随后照常续用。异常(取消/超时/连接死亡)由调用方分类;本函数只保证
        **已落的行不丢**:每批 append 后 catalog 同步一次。
        """

        batch: list[Row] = []
        batch_size = (
            self._config.spool_batch_size
            if spool is None
            else max(1, min(self._config.spool_batch_size, spool.page_size))
        )
        limit = None if spool is None else spool.max_result_rows
        drain_started_at = time.monotonic()
        try:
            for row in stream:
                if metrics.first_row_at is None:
                    metrics.first_row_at = self._clock()
                    metrics.execute_to_first_row_ms = _delta_ms(
                        metrics.execute_started_at, metrics.first_row_at
                    )
                metrics.rows_read += 1
                if limit is not None and metrics.rows_returned >= limit:
                    # 多读到的这一行只用来证明"还有更多",不落盘。
                    metrics.output_limit_applied = True
                    break
                metrics.rows_returned += 1
                batch.append(row)
                if len(batch) >= batch_size:
                    metrics.spool_ms += self._flush_batch(statement, spool, columns, batch)
                    batch = []
        finally:
            metrics.finished_reading_at = self._clock()
            if batch:
                metrics.spool_ms += self._flush_batch(statement, spool, columns, batch)
            if spool is not None and metrics.output_limit_applied:
                self._results.mark_truncated(spool)
            drain_ms = int((time.monotonic() - drain_started_at) * 1000)
            metrics.fetch_ms = max(0, drain_ms - metrics.spool_ms)

    def _flush_batch(
        self,
        statement: ConsoleStatement,
        spool: StatementSpool | None,
        columns: list[Column],
        batch: list[Row],
    ) -> int:
        """落一批并同步 catalog,返回本批耗时 ms(喂 `timings.spool_ms`)。

        **不取 broker 锁**:这是每批都走的热路径,让别的会话 attach/observe 陪着
        排队没有意义。空闲回收也不会误伤 —— 语句在跑时会话是 executing,
        idle 回收只看 idle 态(`run_timers_once`)。
        """

        if spool is None:
            return 0
        started_at = time.monotonic()
        self._results.append(spool, batch)
        self._results.publish_streaming(spool, statement, columns=columns)
        return int((time.monotonic() - started_at) * 1000)

    def _close_on_lane(self, runtime: _SessionRuntime, reason: str) -> None:
        with self._lock:
            while (
                runtime.cancel_in_flight and runtime.session.state in ACTIVE_CONSOLE_SESSION_STATES
            ):
                runtime.condition.wait(timeout=0.25)
            if runtime.session.state in (
                ConsoleSessionState.CLOSED,
                ConsoleSessionState.SESSION_LOST,
                ConsoleSessionState.CONNECT_FAILED,
            ):
                return
            now = self._clock()
            if runtime.session.state is not ConsoleSessionState.CLOSING:
                runtime.session = replace(
                    runtime.session,
                    state=transition_session(runtime.session.state, ConsoleSessionState.CLOSING),
                    last_activity_at=now,
                )
                self._store.update_session(runtime.session)
        runtime.connection.close()
        with self._lock:
            now = self._clock()
            runtime.session = replace(
                runtime.session,
                state=transition_session(runtime.session.state, ConsoleSessionState.CLOSED),
                closed_at=now,
                close_reason=reason,
                last_activity_at=now,
            )
            runtime.idle_deadline = None
            self._store.update_session(runtime.session)
            runtime.condition.notify_all()

    # ── cancel/control ────────────────────────────────────────────────────

    def _begin_cancel_locked(
        self,
        runtime: _SessionRuntime,
        statement: ConsoleStatement,
        *,
        reason: str,
    ) -> None:
        if runtime.cancel_in_flight:
            return
        now = self._clock()
        runtime.cancel_in_flight = True
        runtime.cancel_statement_id = statement.id
        runtime.cancel_reason = reason
        runtime.cancel_control_done = False
        runtime.cancel_statement_done = False
        runtime.cancel_confirmed = False
        runtime.cancel_destroy_sent = False
        runtime.cancel_grace_deadline = now + timedelta(seconds=self._config.cancel_grace_seconds)
        runtime.connection.request_soft_cancel()
        runtime.statements[statement.id] = replace(statement, cancel_requested=True)
        self._store.update_statement(runtime.statements[statement.id])
        if runtime.session.state is ConsoleSessionState.EXECUTING:
            runtime.session = replace(
                runtime.session,
                state=transition_session(runtime.session.state, ConsoleSessionState.CANCELLING),
                last_activity_at=now,
            )
            self._store.update_session(runtime.session)
        self._record_event(runtime, runtime.statements[statement.id], "cancel_requested", now=now)
        control = self._controls.get(runtime.session.datasource_id)
        if control is None:
            raise RuntimeError("control lane is missing for an active session")
        control.submit(_ControlCommand("cancel", runtime))

    def _on_control_cancel_done(
        self, runtime: _SessionRuntime, error: CancelChannelError | None
    ) -> None:
        with self._lock:
            statement_id = runtime.cancel_statement_id
            if statement_id is None:
                return
            statement = runtime.statements[statement_id]
            runtime.cancel_control_done = True
            event_type = "cancel_failed" if error is not None else "cancel_dispatched"
            self._record_event(runtime, statement, event_type, now=self._clock())
            self._settle_cancel_fence_locked(runtime, statement_id, now=self._clock())
            runtime.condition.notify_all()

    def _on_control_destroy_done(
        self, runtime: _SessionRuntime, error: CancelChannelError | None
    ) -> None:
        with self._lock:
            statement_id = runtime.cancel_statement_id
            if statement_id is None:
                return
            statement = runtime.statements[statement_id]
            self._record_event(
                runtime,
                statement,
                "destroy_failed" if error is not None else "cancel_escalated",
                now=self._clock(),
            )
            now = self._clock()
            current = runtime.statements[statement_id]
            if current.state not in _TERMINAL_STATEMENT_STATES:
                terminal = (
                    ConsoleStatementState.OUTCOME_UNKNOWN
                    if current.is_write
                    else ConsoleStatementState.FAILED
                )
                self._set_statement_state(
                    runtime,
                    current,
                    terminal,
                    now=now,
                    error_code="connection_aborted",
                    error_summary="cancel escalation destroyed the session",
                )
            self._lose_session_locked(runtime, reason="cancel_escalated", now=now)
            runtime.current_statement_id = None
            runtime.statement_deadline = None
            runtime.condition.notify_all()

    def _settle_cancel_fence_locked(
        self, runtime: _SessionRuntime, statement_id: str, *, now: datetime
    ) -> None:
        if not runtime.cancel_in_flight or runtime.cancel_statement_id != statement_id:
            return
        statement = runtime.statements[statement_id]
        runtime.cancel_statement_done = statement.state in _TERMINAL_STATEMENT_STATES
        if not runtime.cancel_confirmed and not (
            runtime.cancel_control_done and runtime.cancel_statement_done
        ):
            return
        runtime.cancel_in_flight = False
        runtime.cancel_statement_id = None
        runtime.cancel_reason = None
        runtime.cancel_grace_deadline = None
        runtime.cancel_destroy_sent = False
        runtime.connection.clear_soft_cancel()
        if runtime.session.state is ConsoleSessionState.CANCELLING:
            runtime.session = replace(
                runtime.session,
                state=transition_session(runtime.session.state, ConsoleSessionState.IDLE),
                last_activity_at=now,
            )
            runtime.idle_deadline = self._idle_deadline(now)
            self._store.update_session(runtime.session)
        runtime.condition.notify_all()

    # ── shared state helpers ──────────────────────────────────────────────

    def _set_statement_state(
        self,
        runtime: _SessionRuntime,
        statement: ConsoleStatement,
        state: ConsoleStatementState,
        *,
        now: datetime,
        cancel_requested: bool | None = None,
        error_code: str | None = None,
        error_summary: str | None = None,
    ) -> ConsoleStatement:
        updated = replace(
            statement,
            state=transition_statement(statement.state, state),
            cancel_requested=(
                statement.cancel_requested if cancel_requested is None else cancel_requested
            ),
            error_code=error_code,
            error_summary=error_summary,
            finished_at=now if state in _TERMINAL_STATEMENT_STATES else statement.finished_at,
        )
        runtime.statements[statement.id] = updated
        self._store.update_statement(updated)
        return updated

    def _lose_session_locked(self, runtime: _SessionRuntime, *, reason: str, now: datetime) -> None:
        if runtime.session.state is ConsoleSessionState.SESSION_LOST:
            return
        runtime.accepting = False
        runtime.cancel_in_flight = False
        runtime.session = replace(
            runtime.session,
            state=transition_session(runtime.session.state, ConsoleSessionState.SESSION_LOST),
            closed_at=now,
            close_reason=reason,
            error_code=reason,
            last_activity_at=now,
        )
        runtime.idle_deadline = None
        self._store.update_session(runtime.session)

    def _record_event(
        self,
        runtime: _SessionRuntime,
        statement: ConsoleStatement,
        event_type: str,
        *,
        now: datetime,
    ) -> None:
        self._store.add_event(statement.id, event_type, now=now)
        self._logger.info(
            "broker_statement_event",
            event_type=event_type,
            session_id=runtime.session.id,
            statement_id=statement.id,
            sql_hash=statement.sql_hash,
            sql_len=statement.sql_len,
        )

    def _queue_close_locked(
        self, runtime: _SessionRuntime, *, reason: str, cancel_current: bool
    ) -> None:
        if not runtime.accepting:
            return
        runtime.accepting = False
        if cancel_current and runtime.current_statement_id is not None:
            statement = runtime.statements[runtime.current_statement_id]
            if statement.state in (
                ConsoleStatementState.EXECUTING,
                ConsoleStatementState.STREAMING,
            ):
                self._begin_cancel_locked(runtime, statement, reason="close")
        if reason == "shutdown":
            self._cancel_queued_locked(runtime, event_type="cancelled_for_shutdown")
        try:
            runtime.mailbox.put_nowait(_LaneCommand("close", close_reason=reason))
        except Full:
            # close 是控制命令, 不能被有界用户 mailbox 饿死。保留一个 lane 侧
            # drain 后标志, 最后一条已入队命令 (包括已取消的占位命令) 出队后关闭。
            runtime.close_reason_pending = reason

    def _cancel_queued_locked(self, runtime: _SessionRuntime, *, event_type: str) -> None:
        accepted = [
            statement
            for statement in runtime.statements.values()
            if statement.state is ConsoleStatementState.ACCEPTED
        ]
        for statement in accepted:
            updated = self._set_statement_state(
                runtime,
                statement,
                ConsoleStatementState.CANCELLED,
                now=self._clock(),
                cancel_requested=True,
            )
            self._record_event(runtime, updated, event_type, now=self._clock())

    def _close_if_pending_and_drained(self, runtime: _SessionRuntime) -> bool:
        with self._lock:
            reason = runtime.close_reason_pending
            if reason is None or not runtime.mailbox.empty():
                return False
            runtime.close_reason_pending = None
        self._close_on_lane(runtime, reason)
        return True

    def _enforce_session_limits(self, request: AttachRequest) -> None:
        active = [
            runtime.session
            for runtime in self._sessions.values()
            if runtime.session.state in ACTIVE_CONSOLE_SESSION_STATES
        ]
        limits = self._config.limits
        if (
            len(active) >= limits.global_total
            or sum(session.owner_user_id == request.owner_user_id for session in active)
            >= limits.per_user
            or sum(session.datasource_id == request.datasource_id for session in active)
            >= limits.per_datasource
        ):
            raise BrokerError("session_limit_reached")

    def _runtime_for_statement(self, statement_id: str) -> _SessionRuntime:
        session_id = self._statement_sessions.get(statement_id)
        if session_id is None:
            raise BrokerError("statement_not_cancellable")
        return self._require_runtime(session_id)

    def _require_runtime(self, session_id: str) -> _SessionRuntime:
        runtime = self._sessions.get(session_id)
        if runtime is None:
            raise BrokerError("session_lost")
        return runtime

    def _require_epoch(self, runtime: _SessionRuntime, epoch: int) -> None:
        if epoch != runtime.session.epoch:
            raise BrokerError("stale_session_epoch", current_epoch=runtime.session.epoch)

    def _require_started(self) -> None:
        if not self._started or self._shutdown:
            raise RuntimeError("broker is not running")

    def _idle_deadline(self, last_activity_at: datetime) -> datetime:
        return last_activity_at + timedelta(seconds=self._config.idle_timeout_seconds)

    # ── control/timer lifecycle ──────────────────────────────────────────

    def _ensure_control_lane(self, datasource_id: str) -> _ControlLane:
        with self._lock:
            control = self._controls.get(datasource_id)
            if control is None:
                control = _ControlLane(
                    datasource_id,
                    self._cancel_channel_factory(datasource_id),
                    on_cancel_done=self._on_control_cancel_done,
                    on_destroy_done=self._on_control_destroy_done,
                )
                self._controls[datasource_id] = control
        control.start()
        return control

    def _timer_loop(self) -> None:
        while not self._timer_stop.wait(self._config.timer_poll_seconds):
            self.run_timers_once()


class _ControlLane:
    def __init__(
        self,
        datasource_id: str,
        channel: CancelChannel,
        *,
        on_cancel_done: Callable[[_SessionRuntime, CancelChannelError | None], None],
        on_destroy_done: Callable[[_SessionRuntime, CancelChannelError | None], None],
    ) -> None:
        self.datasource_id = datasource_id
        self._channel = channel
        self._on_cancel_done = on_cancel_done
        self._on_destroy_done = on_destroy_done
        self._mailbox: Queue[_ControlCommand | None] = Queue()
        self._ready = Event()
        self._start_lock = Lock()
        self._thread: Thread | None = None
        self._support = ServerCancelSupport.UNKNOWN

    def start(self) -> None:
        with self._start_lock:
            if self._thread is not None:
                return
            self._thread = Thread(
                target=self._loop,
                name=f"session-control-{self.datasource_id}",
                daemon=True,
            )
            self._thread.start()

    def wait_ready(self) -> ServerCancelSupport:
        if not self._ready.wait(timeout=5):
            raise RuntimeError("control lane did not start")
        return self._support

    def submit(self, command: _ControlCommand) -> None:
        self._mailbox.put_nowait(command)

    def stop(self) -> None:
        self._mailbox.put_nowait(None)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)

    def _loop(self) -> None:
        try:
            self._support = self._channel.open()
        except CancelChannelError:
            self._support = ServerCancelSupport.DEGRADED
        finally:
            self._ready.set()
        while True:
            try:
                command = self._mailbox.get(timeout=0.25)
            except Empty:
                continue
            if command is None:
                self._channel.close()
                return
            if command.kind == "cancel":
                error = (
                    self._run_control(self._channel.cancel, command.runtime)
                    if self._support is ServerCancelSupport.AVAILABLE
                    else CancelChannelError("server cancel is degraded")
                )
                self._on_cancel_done(command.runtime, error)
            elif command.kind == "destroy":
                error = self._run_control(self._channel.destroy, command.runtime)
                self._on_destroy_done(command.runtime, error)

    def _run_control(
        self,
        operation: Callable[[str], None],
        runtime: _SessionRuntime,
    ) -> CancelChannelError | None:
        marker = runtime.session.db_session_marker
        if marker is None:
            return CancelChannelError("session marker is unavailable")
        try:
            operation(marker)
        except CancelChannelError as exc:
            return exc
        return None


_TERMINAL_STATEMENT_STATES = frozenset(
    {
        ConsoleStatementState.SUCCEEDED,
        ConsoleStatementState.FAILED,
        ConsoleStatementState.CANCELLED,
        ConsoleStatementState.TIMEOUT,
        ConsoleStatementState.OUTCOME_UNKNOWN,
        ConsoleStatementState.SKIPPED,
    }
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _delta_ms(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return max(0, int((end - start).total_seconds() * 1000))


__all__ = [
    "BrokerConfig",
    "BrokerError",
    "CancelReceipt",
    "SessionBroker",
    "SessionLimits",
    "SessionObservation",
    "SubmitReceipt",
]
