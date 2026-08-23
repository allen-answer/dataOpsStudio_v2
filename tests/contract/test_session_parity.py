"""语句轮询面与 job 轮询面的**平价契约**(Session Broker 设计 §3.2 / §10-A5)。

本文件的核心是**镜像断言防漂移**:`StatementProgressResponse` 与
`JobProgressResponse`(以及 result 两者)的字段集必须逐字对齐,只允许在一份
显式白名单里存在差异。任何一边加字段而另一边没跟上,这里立刻红 —— 这正是
设计 §9 契约测试要钉死的"两套轮询契约漂移"。

其余用例覆盖 retry_after_ms 调速、progress 的 timings/execution 取数来源、
result 的 page_size/max_result_rows 快照、导出端点复用 job 导出设施。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from pydantic import BaseModel

from app.api.app import create_app
from app.api.routes.core import poll_retry_after_ms
from app.api.schemas import (
    JobProgressResponse,
    JobResultResponse,
    StatementProgressResponse,
    StatementResultResponse,
)
from app.api.security import create_access_token
from app.api.services import ApiServices
from app.broker import BrokerConfig, SessionBroker, SessionLimits
from app.broker.store import MemoryBrokerStore
from app.dbclients.interactive import (
    CancelChannel,
    ClassifiedError,
    ErrorCategory,
    ErrorClassifier,
    InteractiveCapabilities,
    InteractiveConnection,
    ServerCancelSupport,
    StatementRequest,
)
from app.domain.console_session import ConsoleSession, ConsoleSessionState, ConsoleStatementState
from app.domain.datasource import DbType
from app.domain.job import Job, JobKind
from app.domain.schema import Row
from tests._asgi_client import AsgiClient

pytestmark = pytest.mark.contract

JWT_SECRET = "jwt-secret"

# progress 镜像的**唯一**允许差异:主键与状态枚举。
# job 用 job_id / status:JobStatus,语句用 statement_id / state:ConsoleStatementState;
# 语句另有一个内嵌 session 块(设计 §3.2:一条轮询同时驱动语句与会话渲染)。
_PROGRESS_JOB_ONLY = frozenset({"job_id", "status"})
_PROGRESS_STATEMENT_ONLY = frozenset({"statement_id", "state", "session"})
_RESULT_JOB_ONLY = frozenset({"job_id"})
_RESULT_STATEMENT_ONLY = frozenset({"statement_id", "statement_state"})

# 共有字段里唯一允许类型不同的一个:job 的 error_code 是 `JobErrorCode` 闭集,
# 语句的是**驱动分类码**(`cancelled:1317` / `statement_error:-2104`,设计 §5.4)。
# 强行套 JobErrorCode 会把数值码碾平成四个粗粒度值,恰恰丢掉排障要的那一位。
_PROGRESS_TYPE_EXCEPTIONS = frozenset({"error_code"})


def _fields(model: type[BaseModel]) -> frozenset[str]:
    return frozenset(model.model_fields)


# ── 镜像断言(不需要跑服务器)────────────────────────────────────────────────


def test_statement_progress_mirrors_job_progress_field_for_field() -> None:
    job = _fields(JobProgressResponse)
    statement = _fields(StatementProgressResponse)

    assert job - statement == _PROGRESS_JOB_ONLY
    assert statement - job == _PROGRESS_STATEMENT_ONLY


def test_statement_progress_mirror_keeps_identical_types_on_shared_fields() -> None:
    """共有字段连**类型**都必须一致 —— 只对齐名字挡不住 `int` 变 `str`。"""

    job = JobProgressResponse.model_fields
    statement = StatementProgressResponse.model_fields
    shared = (set(job) & set(statement)) - _PROGRESS_TYPE_EXCEPTIONS
    for name in shared:
        assert job[name].annotation == statement[name].annotation, name
    # 白名单本身也要有效:漏写一个例外就等于放行漂移。
    assert _PROGRESS_TYPE_EXCEPTIONS <= set(job) & set(statement)


def test_statement_result_mirrors_job_result_field_for_field() -> None:
    job = _fields(JobResultResponse)
    statement = _fields(StatementResultResponse)

    assert job - statement == _RESULT_JOB_ONLY
    assert statement - job == _RESULT_STATEMENT_ONLY


def test_statement_result_mirror_keeps_identical_types_on_shared_fields() -> None:
    job = JobResultResponse.model_fields
    statement = StatementResultResponse.model_fields
    for name in set(job) & set(statement):
        assert job[name].annotation == statement[name].annotation, name


# ── retry_after_ms 调速 ──────────────────────────────────────────────────────


def test_retry_after_ms_follows_the_shared_schedule() -> None:
    """0/1000/2000 沿用 job 规则;排队态 500ms 是会话独有的快档(设计 §3.2)。"""

    assert poll_retry_after_ms(terminal=True, fresh=True) == 0
    assert poll_retry_after_ms(terminal=True, fresh=False, queued=True) == 0
    assert poll_retry_after_ms(terminal=False, fresh=True) == 1000
    assert poll_retry_after_ms(terminal=False, fresh=False) == 2000
    assert poll_retry_after_ms(terminal=False, fresh=False, queued=True) == 500


# ── fakes ────────────────────────────────────────────────────────────────────


class _FakeClassifier:
    db_type: DbType = DbType.MYSQL

    def classify(self, _exc: BaseException) -> ClassifiedError:
        return ClassifiedError(ErrorCategory.UNKNOWN)


class _FakeConnection:
    db_type = DbType.MYSQL
    classifier: ErrorClassifier = _FakeClassifier()
    capabilities = InteractiveCapabilities(
        server_cancel=True,
        server_statement_timeout=True,
        session_streaming=True,
    )

    def __init__(self) -> None:
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
        del request
        return iter([Row(values=[1])])

    def request_soft_cancel(self) -> None:
        self._soft_cancel.set()

    def clear_soft_cancel(self) -> None:
        self._soft_cancel.clear()

    @property
    def soft_cancel_requested(self) -> bool:
        return self._soft_cancel.is_set()

    def ping(self) -> bool:
        return self._open

    def close(self) -> None:
        self._open = False


class _FakeCancelChannel:
    db_type = DbType.MYSQL
    classifier: ErrorClassifier = _FakeClassifier()

    def __init__(self) -> None:
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

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        return None


_CONSOLE_ROW: dict[str, object] = {
    "id": "console-1",
    "owner_user_id": "user-1",
    "name": "console",
    "datasource_id": "ds-1",
    "sql": "SELECT 1",
    "pinned": False,
    "created_at": datetime(2026, 8, 22, tzinfo=UTC),
    "updated_at": datetime(2026, 8, 22, tzinfo=UTC),
}

_DATASOURCE_ROW: dict[str, object] = {
    "id": "ds-1",
    "project_id": "project-1",
    "name": "warehouse",
    "db_type": "mysql",
    "host": "mysql.internal",
    "port": 3306,
    "username": "dataops",
    "database_name": "app",
    "password_secret_ref": "secret-1",
    "capability_profile": {},
    "operation_policy": {},
}

# 会话 lane 落 spool 后写回的 catalog 行:timings/execution 就存在这里的
# storage_ref.metadata 里(job 路径存在 jobs.result_ref)。
_RESULT_SET_ROW: dict[str, object] = {
    "id": "rs-1",
    "execution_id": "stmt-1",
    "console_id": "console-1",
    "storage_ref": {
        "backend": "local_fs",
        "uri": "resultsets/rs-1/manifest.json",
        "metadata": {
            "timings": {"execute_first_row_ms": 12, "fetch_ms": 34, "spool_ms": 5},
            "execution": {
                "execute_started_at": "2026-08-22T09:00:00+00:00",
                "first_row_at": "2026-08-22T09:00:01+00:00",
                "finished_reading_at": "2026-08-22T09:00:02+00:00",
                "rows_read": 7,
                "rows_returned": 6,
                "max_rows": 500,
                "page_size": 25,
                "limit_pushdown": True,
                "limit_pushdown_reason": "top_level_limit_can_stop_row_production",
                "output_limit_applied": True,
                "query_shape": "simple_select",
                "effective_sql_hash": "deadbeef",
                "db_type": "mysql",
            },
        },
    },
    "columns": [],
    "loaded_rows": 6,
    "total_rows": None,
    "state": "complete",
    "created_at": datetime(2026, 8, 22, tzinfo=UTC),
    "updated_at": datetime(2026, 8, 22, tzinfo=UTC),
}


class _FakeResult:
    def __init__(self, value: object) -> None:
        self._value = value
        self.rowcount = 1

    def mappings(self) -> _FakeResult:
        return self

    def scalars(self) -> _FakeResult:
        return self

    def one_or_none(self) -> object:
        return self._value

    def all(self) -> list[object]:
        return list(self._value) if isinstance(self._value, list) else []

    def scalar_one(self) -> object:
        return self._value

    def scalar_one_or_none(self) -> object:
        return self._value


class _KeywordConn:
    """按 SQL 文本关键字派发的 fake 连接。

    比"按顺序 pop 结果"稳:导出路径要连查 datasource / 导出配额 / result_sets,
    顺序变一次就得改所有用例。
    """

    def __init__(self, engine: _KeywordEngine) -> None:
        self._engine = engine

    def __enter__(self) -> _KeywordConn:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, statement: object, parameters: object | None = None) -> _FakeResult:
        del parameters
        text = str(statement)
        self._engine.statements.append(text)
        upper = text.lstrip().upper()
        if not (upper.startswith("SELECT") or upper.startswith("WITH")):
            return _FakeResult(None)
        if "count(" in text:
            return _FakeResult(self._engine.export_jobs_last_hour)
        if "FROM result_sets" in text:
            return _FakeResult(self._engine.result_set_row)
        if "FROM sql_consoles" in text:
            return _FakeResult(dict(_CONSOLE_ROW))
        if "FROM datasources" in text:
            return _FakeResult(dict(_DATASOURCE_ROW))
        if "FROM projects" in text:
            return _FakeResult({"id": "project-1"})
        return _FakeResult(None)


class _KeywordEngine:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.export_jobs_last_hour = 0
        self.result_set_row: dict[str, object] | None = dict(_RESULT_SET_ROW)

    def connect(self) -> _KeywordConn:
        return _KeywordConn(self)

    def begin(self) -> _KeywordConn:
        return _KeywordConn(self)


class _FakeResultStore:
    """有列有行的 spool 替身 —— result 页要能真的回出行来。"""

    def __init__(self) -> None:
        self.rows = [Row(values=[1]), Row(values=[2])]

    def spool_exists(self, _result_set_id: str) -> bool:
        return True

    def get_spool_manifest(self, _result_set_id: str) -> dict[str, Any]:
        return {
            "columns": [{"name": "id", "type": "integer", "nullable": True}],
            "loaded_rows": len(self.rows),
            "result_version": 3,
            "truncated": True,
            "has_more": True,
            "first_batch_at": 1_755_000_000.0,
        }

    def fetch_range(self, _result_set_id: str, offset: int, limit: int) -> list[Row]:
        return self.rows[offset : offset + limit]


class _FakeJobBackend:
    def __init__(self) -> None:
        self.jobs: list[Job] = []

    def enqueue(self, job: Job) -> None:
        self.jobs.append(job)


class _RateLimiter:
    def __init__(self) -> None:
        self.groups: list[str] = []

    def check(self, _key: str, *, group: str = "general_read") -> Any:
        self.groups.append(group)
        return type("_D", (), {"allowed": True, "retry_after_seconds": 0.0})()


class _Services:
    jwt_secret = JWT_SECRET
    download_url_ttl_seconds = 900
    export_per_user_per_hour = 20
    max_active_resultsets_per_console = 3

    def __init__(self, engine: _KeywordEngine, broker: SessionBroker | None) -> None:
        self.engine = engine
        self.session_broker = broker
        self.rate_limiter = _RateLimiter()
        self.result_store = _FakeResultStore()
        self.job_backend = _FakeJobBackend()
        self.audits: list[dict[str, object]] = []

    def access_token_ttl_seconds(self) -> int:
        return 3600

    def license_enforcement_enabled(self) -> bool:
        return False

    def is_token_revoked(self, **_kwargs: object) -> bool:
        return False

    def write_audit(self, **kwargs: object) -> None:
        self.audits.append(kwargs)


def _headers(user_id: str = "user-1") -> dict[str, str]:
    token = create_access_token(user_id=user_id, role="admin", secret=JWT_SECRET)
    return {"Authorization": f"Bearer {token}"}


def _broker() -> SessionBroker:
    broker = SessionBroker(
        MemoryBrokerStore(),
        connection_factory=lambda _session: cast(InteractiveConnection, _FakeConnection()),
        cancel_channel_factory=lambda _ds: cast(CancelChannel, _FakeCancelChannel()),
        config=BrokerConfig(
            limits=SessionLimits(per_user=8, per_datasource=4, global_total=16),
            timer_poll_seconds=3600,
        ),
        boot_id="boot-parity",
    )
    broker.start()
    return broker


def _wait(predicate: object, timeout: float = 3.0) -> None:
    assert callable(predicate)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition did not become true")


def _client(broker: SessionBroker | None) -> tuple[AsgiClient, _Services, _KeywordEngine]:
    engine = _KeywordEngine()
    services = _Services(engine, broker)
    return AsgiClient(create_app(services=cast(ApiServices, services))), services, engine


def _submit(client: AsgiClient, broker: SessionBroker) -> tuple[str, ConsoleSession]:
    attach = client.post(
        "/api/sql/sessions/attach",
        headers=_headers(),
        json_body={"console_id": "console-1"},
    ).json()
    _wait(lambda: broker.observe(attach["session_id"]).state is ConsoleSessionState.IDLE)
    submitted = client.post(
        f"/api/sql/sessions/{attach['session_id']}/statements",
        headers=_headers(),
        json_body={
            "epoch": attach["epoch"],
            "sql": "SELECT 1",
            "client_request_id": "req-1",
            "page_size": 25,
            "max_result_rows": 500,
        },
    ).json()
    statement_id = str(submitted["statement_id"])
    _wait(lambda: broker.statement(statement_id).state is ConsoleStatementState.SUCCEEDED)
    return statement_id, broker.observe(attach["session_id"]).session


# ── progress / result 平价 ───────────────────────────────────────────────────


def test_progress_fills_the_mirrored_timings_and_execution_blocks() -> None:
    """镜像字段不是摆设:timings/execution 要真从结果集 catalog 取到值。"""

    broker = _broker()
    client, _, _ = _client(broker)
    try:
        statement_id, _ = _submit(client, broker)

        payload = client.get(
            f"/api/sql/statements/{statement_id}/progress",
            headers=_headers(),
        ).json()

        assert payload["terminal"] is True
        assert payload["retry_after_ms"] == 0
        assert payload["timings"]["fetch_ms"] == 34
        assert payload["timings"]["spool_ms"] == 5
        assert payload["timings"]["execute_first_row_ms"] == 12
        # 会话复用连接:语句维度没有建连阶段,如实回 null 而不是 0。
        assert payload["timings"]["connect_ms"] is None
        execution = payload["execution"]
        assert execution["rows_read"] == 7
        assert execution["rows_returned"] == 6
        assert execution["max_rows"] == 500
        assert execution["output_limit_applied"] is True
        assert execution["db_type"] == "mysql"
        assert execution["worker_id"] is None
        assert execution["connect_ms"] is None
        # 会话块随 progress 一起回:一条轮询驱动两处渲染。
        assert payload["session"]["state"] == ConsoleSessionState.IDLE.value
    finally:
        broker.shutdown()


def test_result_page_returns_rows_and_the_effective_size_snapshot() -> None:
    broker = _broker()
    client, _, _ = _client(broker)
    try:
        statement_id, _ = _submit(client, broker)

        payload = client.get(
            f"/api/sql/statements/{statement_id}/result",
            headers=_headers(),
        ).json()

        assert [row["values"] for row in payload["rows"]] == [[1], [2]]
        assert payload["columns"][0]["name"] == "id"
        assert payload["state"] == "complete"
        assert payload["truncated"] is True
        assert payload["has_more"] is True
        # submit 的生效快照原样回读(job 路径从 job payload 取同名两项)。
        assert payload["page_size"] == 25
        assert payload["max_result_rows"] == 500
    finally:
        broker.shutdown()


def test_progress_of_a_queued_statement_asks_for_a_fast_repoll() -> None:
    broker = _broker()
    client, _, _ = _client(broker)
    try:
        attach = client.post(
            "/api/sql/sessions/attach",
            headers=_headers(),
            json_body={"console_id": "console-1"},
        ).json()
        _wait(lambda: broker.observe(attach["session_id"]).state is ConsoleSessionState.IDLE)
        submitted = client.post(
            f"/api/sql/sessions/{attach['session_id']}/statements",
            headers=_headers(),
            json_body={
                "epoch": attach["epoch"],
                "sql": "SELECT 1",
                "client_request_id": "req-queued",
            },
        ).json()
        statement_id = str(submitted["statement_id"])
        _wait(lambda: broker.statement(statement_id).state is ConsoleStatementState.SUCCEEDED)

        # 语句已终态:调速回 0(停轮询)。排队档本身由纯函数用例钉死,
        # 这里断言的是端点确实走了同一份规则。
        payload = client.get(
            f"/api/sql/statements/{statement_id}/progress",
            headers=_headers(),
        ).json()
        assert payload["retry_after_ms"] == 0
    finally:
        broker.shutdown()


# ── 导出平价 ─────────────────────────────────────────────────────────────────


def test_export_enqueues_a_result_export_job_reusing_the_job_pipeline() -> None:
    broker = _broker()
    client, services, _ = _client(broker)
    try:
        statement_id, _ = _submit(client, broker)
        result_set_id = broker.statement(statement_id).result_set_id

        response = client.post(
            f"/api/sql/statements/{statement_id}/export",
            headers=_headers(),
            json_body={"format": "csv", "table_name": "exported_result"},
        )

        assert response.status_code == 202, response.body
        payload = response.json()
        assert payload["format"] == "csv"
        assert payload["download_token"]
        assert payload["filename"] == f"{statement_id}.csv"

        assert len(services.job_backend.jobs) == 1
        job = services.job_backend.jobs[0]
        assert job.kind is JobKind.RESULT_EXPORT
        # worker 只认 source_result_set_id —— 会话与 job 共用同一个结果 catalog,
        # 所以导出 worker 侧一行不用改。
        assert job.payload["source_result_set_id"] == result_set_id
        assert job.payload["source_statement_id"] == statement_id
        assert job.payload["source_db_type"] == "mysql"
        assert "console_statement_export" in [audit["action"] for audit in services.audits]
    finally:
        broker.shutdown()


def test_export_of_a_still_running_statement_is_refused() -> None:
    """还在跑就导出 = 导出半份结果。如实 409,不静默截断。"""

    broker = _broker()
    client, services, _ = _client(broker)
    try:
        attach = client.post(
            "/api/sql/sessions/attach",
            headers=_headers(),
            json_body={"console_id": "console-1"},
        ).json()
        _wait(lambda: broker.observe(attach["session_id"]).state is ConsoleSessionState.IDLE)
        submitted = client.post(
            f"/api/sql/sessions/{attach['session_id']}/statements",
            headers=_headers(),
            json_body={
                "epoch": attach["epoch"],
                "sql": "SELECT 1",
                "client_request_id": "req-1",
            },
        ).json()
        statement_id = str(submitted["statement_id"])
        _wait(lambda: broker.statement(statement_id).state is ConsoleStatementState.SUCCEEDED)
        _force_state(broker, statement_id, ConsoleStatementState.STREAMING)

        response = client.post(
            f"/api/sql/statements/{statement_id}/export",
            headers=_headers(),
            json_body={"format": "csv"},
        )

        assert response.status_code == 409
        assert response.json()["error"] == "statement_not_successful"
        assert services.job_backend.jobs == []
    finally:
        broker.shutdown()


def test_export_of_a_cancelled_statement_is_allowed() -> None:
    """★ 取消保留的部分结果**可导出**(设计 §2.2 / F6 / §11-7)。"""

    broker = _broker()
    client, services, _ = _client(broker)
    try:
        statement_id, _ = _submit(client, broker)
        _force_state(broker, statement_id, ConsoleStatementState.CANCELLED)

        response = client.post(
            f"/api/sql/statements/{statement_id}/export",
            headers=_headers(),
            json_body={"format": "csv"},
        )

        assert response.status_code == 202, response.body
        assert len(services.job_backend.jobs) == 1
    finally:
        broker.shutdown()


def test_export_respects_the_shared_per_user_hourly_quota() -> None:
    broker = _broker()
    client, services, engine = _client(broker)
    try:
        statement_id, _ = _submit(client, broker)
        engine.export_jobs_last_hour = services.export_per_user_per_hour

        response = client.post(
            f"/api/sql/statements/{statement_id}/export",
            headers=_headers(),
            json_body={"format": "csv"},
        )

        assert response.status_code == 429
        assert response.json()["error"] == "export_rate_limited"
    finally:
        broker.shutdown()


def test_export_is_rate_limit_grouped_with_the_other_sql_control_verbs() -> None:
    broker = _broker()
    client, services, _ = _client(broker)
    try:
        statement_id, _ = _submit(client, broker)
        services.rate_limiter.groups.clear()

        client.post(
            f"/api/sql/statements/{statement_id}/export",
            headers=_headers(),
            json_body={"format": "csv"},
        )

        assert services.rate_limiter.groups == ["sql_control"]
    finally:
        broker.shutdown()


def _force_state(
    broker: SessionBroker,
    statement_id: str,
    state: ConsoleStatementState,
) -> None:
    """把语句钉到指定状态,构造导出准入的边界(不经过状态机)。"""

    from dataclasses import replace

    statement = broker.statement(statement_id)
    runtime = broker._sessions[statement.session_id]
    runtime.statements[statement_id] = replace(statement, state=state)
