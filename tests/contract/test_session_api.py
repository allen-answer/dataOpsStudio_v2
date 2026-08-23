"""SQL 控制台会话端点契约(Session Broker 设计 §3.1/§3.2/§2.3)。

覆盖:端点全集、409 错误码族与 epoch 栅栏矩阵(M1/M3/M8)、幂等回执、
只读 guard、限流分组归属、审计豁免、回退开关与非会话方言的如实拒绝。

真驱动不参与:broker 走 `MemoryBrokerStore` + fake InteractiveConnection
(与 tests/unit/test_broker_core.py 同形),本文件断言的是 **HTTP 契约**。
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.routing import APIRoute

from app.api.app import create_app
from app.api.routes.sessions import _BROKER_CONFLICT_CODES
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
    InteractiveExecuteError,
    ServerCancelSupport,
    StatementRequest,
)
from app.dbclients.query_limit import apply_database_row_limit
from app.domain.console_session import ConsoleSession, ConsoleSessionState, ConsoleStatementState
from app.domain.datasource import DbType
from app.domain.schema import Row
from tests._asgi_client import AsgiClient

pytestmark = pytest.mark.contract

JWT_SECRET = "jwt-secret"


# ── fakes ────────────────────────────────────────────────────────────────────


class _FakeClassifier:
    db_type: DbType = DbType.MYSQL

    def classify(self, _exc: BaseException) -> ClassifiedError:
        return ClassifiedError(ErrorCategory.UNKNOWN)


class _FakeConnection:
    """一条 fake 会话连接。`block_execute` 让语句停在 streaming 供取消用例观察。"""

    db_type = DbType.MYSQL
    classifier: ErrorClassifier = _FakeClassifier()
    capabilities = InteractiveCapabilities(
        server_cancel=True,
        server_statement_timeout=True,
        session_streaming=True,
    )

    def __init__(self, harness: _DriverHarness, marker: str) -> None:
        self._harness = harness
        self._marker = marker
        self._open = False
        self._soft_cancel = threading.Event()

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def session_marker(self) -> str | None:
        return self._marker if self._open else None

    def open(self) -> str:
        self._open = True
        return self._marker

    def execute(self, request: StatementRequest) -> Iterator[Row]:
        self._harness.executed.append(request.sql)
        self._harness.timeouts.append(request.timeout_seconds)
        self._harness.execute_started.set()
        if self._harness.block_execute:
            assert self._harness.execute_release.wait(3), "test did not release execute"
        if self._soft_cancel.is_set():
            raise InteractiveExecuteError(
                "cancelled",
                ClassifiedError(ErrorCategory.CANCELLED, driver_code=1317, server_confirmed=True),
            )
        return iter([Row(values=[1])])

    def request_soft_cancel(self) -> None:
        self._soft_cancel.set()
        self._harness.execute_release.set()

    def clear_soft_cancel(self) -> None:
        self._soft_cancel.clear()
        self._harness.execute_started.clear()
        self._harness.execute_release.clear()

    @property
    def soft_cancel_requested(self) -> bool:
        return self._soft_cancel.is_set()

    def ping(self) -> bool:
        return self._open

    def close(self) -> None:
        self._open = False
        self._harness.closed.set()
        self._harness.execute_release.set()


class _FakeCancelChannel:
    db_type = DbType.MYSQL
    classifier: ErrorClassifier = _FakeClassifier()

    def __init__(self, harness: _DriverHarness) -> None:
        self._harness = harness
        self._support = ServerCancelSupport.UNKNOWN

    @property
    def support(self) -> ServerCancelSupport:
        return self._support

    def open(self) -> ServerCancelSupport:
        self._support = ServerCancelSupport.AVAILABLE
        return self._support

    def cancel(self, session_marker: str) -> None:
        self._harness.cancelled.append(session_marker)

    def destroy(self, session_marker: str) -> None:
        self._harness.destroyed.append(session_marker)
        self._harness.execute_release.set()

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        return None


class _DriverHarness:
    def __init__(self, *, block_execute: bool = False) -> None:
        self.block_execute = block_execute
        self.executed: list[str] = []
        self.timeouts: list[int] = []
        self.cancelled: list[str] = []
        self.destroyed: list[str] = []
        self.execute_started = threading.Event()
        self.execute_release = threading.Event()
        self.closed = threading.Event()
        self.connections: list[_FakeConnection] = []

    def connection_factory(self, _session: ConsoleSession) -> InteractiveConnection:
        connection = _FakeConnection(self, marker=str(100 + len(self.connections)))
        self.connections.append(connection)
        return connection

    def channel_factory(self, _datasource_id: str) -> CancelChannel:
        return _FakeCancelChannel(self)


class _FakeResult:
    def __init__(self, value: object) -> None:
        self._value = value
        self.rowcount = 1

    def mappings(self) -> _FakeResult:
        return self

    def one_or_none(self) -> object:
        return self._value

    def scalar_one_or_none(self) -> object:
        return None


class _FakeConn:
    def __init__(self, engine: _FakeEngine) -> None:
        self._engine = engine

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, statement: object, parameters: object | None = None) -> _FakeResult:
        del parameters
        text = str(statement).lstrip().upper()
        self._engine.statements.append(str(statement))
        if text.startswith("SELECT") or text.startswith("WITH"):
            return _FakeResult(self._engine.results.pop(0) if self._engine.results else None)
        return _FakeResult(None)


class _FakeEngine:
    def __init__(self, results: list[object] | None = None) -> None:
        self.results: list[object] = list(results or [])
        self.statements: list[str] = []

    def connect(self) -> _FakeConn:
        return _FakeConn(self)

    def begin(self) -> _FakeConn:
        return _FakeConn(self)


class _FakeResultStore:
    def spool_exists(self, _result_set_id: str) -> bool:
        return False

    def get_spool_manifest(self, _result_set_id: str) -> dict[str, Any]:
        return {}

    def fetch_range(self, _result_set_id: str, _offset: int, _limit: int) -> list[Row]:
        return []


class _RateLimiter:
    def check(self, _key: str, *, group: str = "general_read") -> Any:
        self.groups.append(group)
        return type("_D", (), {"allowed": True, "retry_after_seconds": 0.0})()

    def __init__(self) -> None:
        self.groups: list[str] = []


class _Services:
    jwt_secret = JWT_SECRET

    def __init__(self, engine: _FakeEngine, broker: SessionBroker | None) -> None:
        self.engine = engine
        self.session_broker = broker
        self.rate_limiter = _RateLimiter()
        self.result_store = _FakeResultStore()
        self.audits: list[dict[str, object]] = []

    def access_token_ttl_seconds(self) -> int:
        return 3600

    def license_enforcement_enabled(self) -> bool:
        return False

    def is_token_revoked(self, **_kwargs: object) -> bool:
        return False

    def write_audit(self, **kwargs: object) -> None:
        self.audits.append(kwargs)


# ── helpers ──────────────────────────────────────────────────────────────────


def _console_row(datasource_id: str | None = "ds-1") -> dict[str, object]:
    return {
        "id": "console-1",
        "owner_user_id": "user-1",
        "name": "console",
        "datasource_id": datasource_id,
        "sql": "SELECT 1",
        "pinned": False,
        "created_at": datetime(2026, 8, 22, tzinfo=UTC),
        "updated_at": datetime(2026, 8, 22, tzinfo=UTC),
    }


def _datasource_row(db_type: str = "mysql") -> dict[str, object]:
    return {
        "id": "ds-1",
        "project_id": "project-1",
        "name": "warehouse",
        "db_type": db_type,
        "host": "mysql.internal",
        "port": 3306,
        "username": "dataops",
        "database_name": "app",
        "password_secret_ref": "secret-1",
        "capability_profile": {},
        "operation_policy": {},
    }


def _attach_rows(db_type: str = "mysql", datasource_id: str | None = "ds-1") -> list[object]:
    """attach 的读序:console → datasource → project 授权。"""

    return [_console_row(datasource_id), _datasource_row(db_type), {"id": "project-1"}]


def _observe_rows() -> list[object]:
    return [_datasource_row(), {"id": "project-1"}]


def _headers(user_id: str = "user-1") -> dict[str, str]:
    token = create_access_token(user_id=user_id, role="admin", secret=JWT_SECRET)
    return {"Authorization": f"Bearer {token}"}


def _strip_result_set(broker: SessionBroker, statement_id: str) -> None:
    """把语句的 result_set_id 抹掉,模拟"结果集不存在"(spool 写失败/被淘汰)。"""

    statement = broker.statement(statement_id)
    session_id = statement.session_id
    runtime = broker._sessions[session_id]
    runtime.statements[statement_id] = replace(statement, result_set_id=None)


def _broker(harness: _DriverHarness, *, start: bool = True) -> SessionBroker:
    broker = SessionBroker(
        MemoryBrokerStore(),
        connection_factory=harness.connection_factory,
        cancel_channel_factory=harness.channel_factory,
        config=BrokerConfig(
            limits=SessionLimits(per_user=8, per_datasource=4, global_total=16),
            timer_poll_seconds=3600,
        ),
        boot_id="boot-contract",
    )
    if start:
        broker.start()
    return broker


def _client(
    engine: _FakeEngine,
    broker: SessionBroker | None,
) -> tuple[AsgiClient, _Services]:
    services = _Services(engine, broker)
    app = create_app(services=cast(ApiServices, services))
    return AsgiClient(app), services


def _wait(predicate: object, timeout: float = 3.0) -> None:
    assert callable(predicate)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition did not become true")


def _attach(client: AsgiClient) -> dict[str, Any]:
    response = client.post(
        "/api/sql/sessions/attach",
        headers=_headers(),
        json_body={"console_id": "console-1"},
    )
    assert response.status_code == 200, response.body
    return cast(dict[str, Any], response.json())


# ── 端点全集 ─────────────────────────────────────────────────────────────────


def test_session_route_surface_matches_design_section_3_1() -> None:
    app = create_app(services=cast(ApiServices, _Services(_FakeEngine(), None)))
    routes = {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }

    assert ("POST", "/api/sql/sessions/attach") in routes
    assert ("GET", "/api/sql/sessions/{session_id}") in routes
    assert ("POST", "/api/sql/sessions/{session_id}/statements") in routes
    assert ("POST", "/api/sql/sessions/{session_id}/close") in routes
    assert ("GET", "/api/sql/statements/{statement_id}/progress") in routes
    assert ("GET", "/api/sql/statements/{statement_id}/result") in routes
    assert ("POST", "/api/sql/statements/{statement_id}/cancel") in routes
    # A5 平价:导出复用既有导出模块与一次性 token(设计 §3.3)。
    assert ("POST", "/api/sql/statements/{statement_id}/export") in routes


# ── attach / observe ─────────────────────────────────────────────────────────


def test_attach_creates_session_and_reports_capability_bits() -> None:
    harness = _DriverHarness()
    broker = _broker(harness)
    client, services = _client(_FakeEngine(_attach_rows()), broker)
    try:
        payload = _attach(client)

        assert payload["session_id"]
        assert payload["epoch"] == 1
        assert payload["current_epoch"] == 1
        assert payload["db_type"] == "mysql"
        assert payload["server_cancel"] == "available"
        assert payload["state"] in {"connecting", "idle"}
        assert "console_session_attach" in [audit["action"] for audit in services.audits]
    finally:
        broker.shutdown()


def test_attach_bumps_epoch_without_rebuilding_the_live_connection() -> None:
    """M2/M3:活会话再 attach 只 bump epoch,不重建连接、不打断在跑语句。"""

    harness = _DriverHarness()
    broker = _broker(harness)
    engine = _FakeEngine(_attach_rows() + _attach_rows())
    client, _ = _client(engine, broker)
    try:
        first = _attach(client)
        _wait(lambda: broker.observe(first["session_id"]).state is ConsoleSessionState.IDLE)
        second = _attach(client)

        assert second["session_id"] == first["session_id"]
        assert second["epoch"] == 2
        assert len(harness.connections) == 1
    finally:
        broker.shutdown()


def test_observe_always_carries_current_epoch() -> None:
    harness = _DriverHarness()
    broker = _broker(harness)
    engine = _FakeEngine(_attach_rows() + _attach_rows() + _observe_rows())
    client, _ = _client(engine, broker)
    try:
        session_id = _attach(client)["session_id"]
        _attach(client)  # 第二个 tab 接管

        observed = client.get(f"/api/sql/sessions/{session_id}", headers=_headers())

        assert observed.status_code == 200
        assert observed.json()["current_epoch"] == 2
    finally:
        broker.shutdown()


def test_attach_refuses_non_session_dialects_so_frontend_falls_back_to_jobs() -> None:
    harness = _DriverHarness()
    broker = _broker(harness)
    client, _ = _client(_FakeEngine(_attach_rows(db_type="oracle")), broker)
    try:
        response = client.post(
            "/api/sql/sessions/attach",
            headers=_headers(),
            json_body={"console_id": "console-1"},
        )

        assert response.status_code == 409
        assert response.json()["error"] == "console_session_unsupported"
    finally:
        broker.shutdown()


def test_endpoints_refuse_when_console_sessions_are_disabled() -> None:
    client, _ = _client(_FakeEngine(_attach_rows()), None)

    response = client.post(
        "/api/sql/sessions/attach",
        headers=_headers(),
        json_body={"console_id": "console-1"},
    )

    assert response.status_code == 409
    assert response.json()["error"] == "console_session_disabled"


def test_observe_hides_sessions_owned_by_another_user() -> None:
    harness = _DriverHarness()
    broker = _broker(harness)
    client, _ = _client(_FakeEngine(_attach_rows()), broker)
    try:
        session_id = _attach(client)["session_id"]

        response = client.get(f"/api/sql/sessions/{session_id}", headers=_headers(user_id="user-2"))

        assert response.status_code == 404
    finally:
        broker.shutdown()


# ── submit ───────────────────────────────────────────────────────────────────


def test_submit_accepts_statement_and_returns_receipt() -> None:
    harness = _DriverHarness()
    broker = _broker(harness)
    client, _ = _client(_FakeEngine(_attach_rows()), broker)
    try:
        session = _attach(client)
        _wait(lambda: broker.observe(session["session_id"]).state is ConsoleSessionState.IDLE)

        response = client.post(
            f"/api/sql/sessions/{session['session_id']}/statements",
            headers=_headers(),
            json_body={
                "epoch": session["epoch"],
                "sql": "SELECT 1",
                "client_request_id": "req-1",
                "timeout_seconds": 42,
            },
        )

        assert response.status_code == 202
        payload = response.json()
        assert payload["seq"] == 1
        assert payload["deduplicated"] is False
        # A5:结果集在**受理时**分配(job 路径同型),前端拿到回执即可开轮询。
        assert isinstance(payload["result_set_id"], str)
        assert payload["result_set_id"]
        _wait(lambda: harness.timeouts == [42])
    finally:
        broker.shutdown()


def test_resubmitting_same_client_request_id_never_executes_twice() -> None:
    """幂等回执(设计 §3.1):submit 的 HTTP 响应丢失时重试提交请求是安全的。"""

    harness = _DriverHarness()
    broker = _broker(harness)
    client, _ = _client(_FakeEngine(_attach_rows()), broker)
    try:
        session = _attach(client)
        _wait(lambda: broker.observe(session["session_id"]).state is ConsoleSessionState.IDLE)
        body = {
            "epoch": session["epoch"],
            "sql": "SELECT 1",
            "client_request_id": "req-1",
        }
        url = f"/api/sql/sessions/{session['session_id']}/statements"

        first = client.post(url, headers=_headers(), json_body=body)
        second = client.post(url, headers=_headers(), json_body=body)

        assert first.json()["statement_id"] == second.json()["statement_id"]
        assert second.json()["deduplicated"] is True
        expected_sql = apply_database_row_limit("SELECT 1", DbType.MYSQL, 1001)
        _wait(lambda: harness.executed == [expected_sql])
        assert harness.executed == [expected_sql]
    finally:
        broker.shutdown()


def test_submit_keeps_readonly_guard_unchanged() -> None:
    harness = _DriverHarness()
    broker = _broker(harness)
    client, _ = _client(_FakeEngine(_attach_rows()), broker)
    try:
        session = _attach(client)

        response = client.post(
            f"/api/sql/sessions/{session['session_id']}/statements",
            headers=_headers(),
            json_body={
                "epoch": session["epoch"],
                "sql": "DELETE FROM users",
                "client_request_id": "req-1",
            },
        )

        assert response.status_code == 400
        assert response.json()["error"] == "invalid_sql"
        assert harness.executed == []
    finally:
        broker.shutdown()


def test_stale_epoch_submit_is_refused_with_current_epoch() -> None:
    """M1:双 tab 抢同一 console —— 旧 tab 收 409 + current_epoch,会话本体不受影响。"""

    harness = _DriverHarness()
    broker = _broker(harness)
    engine = _FakeEngine(_attach_rows() + _attach_rows())
    client, _ = _client(engine, broker)
    try:
        first = _attach(client)
        _wait(lambda: broker.observe(first["session_id"]).state is ConsoleSessionState.IDLE)
        _attach(client)

        response = client.post(
            f"/api/sql/sessions/{first['session_id']}/statements",
            headers=_headers(),
            json_body={
                "epoch": first["epoch"],
                "sql": "SELECT 1",
                "client_request_id": "req-stale",
            },
        )

        assert response.status_code == 409
        assert response.json()["error"] == "stale_session_epoch"
        assert response.json()["current_epoch"] == 2
        assert harness.executed == []
    finally:
        broker.shutdown()


# ── progress ─────────────────────────────────────────────────────────────────


def test_progress_mirrors_job_progress_shape_and_embeds_session_block() -> None:
    harness = _DriverHarness()
    broker = _broker(harness)
    client, _ = _client(_FakeEngine(_attach_rows()), broker)
    try:
        session = _attach(client)
        _wait(lambda: broker.observe(session["session_id"]).state is ConsoleSessionState.IDLE)
        submitted = client.post(
            f"/api/sql/sessions/{session['session_id']}/statements",
            headers=_headers(),
            json_body={
                "epoch": session["epoch"],
                "sql": "SELECT 1",
                "client_request_id": "req-1",
            },
        ).json()
        statement_id = submitted["statement_id"]
        _wait(lambda: broker.statement(statement_id).state is ConsoleStatementState.SUCCEEDED)

        response = client.get(f"/api/sql/statements/{statement_id}/progress", headers=_headers())

        assert response.status_code == 200
        payload = response.json()
        assert payload["statement_id"] == statement_id
        assert payload["state"] == "succeeded"
        assert payload["terminal"] is True
        assert payload["retry_after_ms"] == 0
        assert payload["session"] == {
            "session_id": session["session_id"],
            "state": "idle",
            "current_epoch": 1,
        }
        # 与 JobProgressResponse 镜像的字段一个不少(镜像断言在 A5 钉死)。
        for key in (
            "result_version",
            "loaded_rows",
            "columns_ready",
            "first_batch_ready",
            "has_new_result",
            "truncated",
            "has_more",
            "timings",
            "execution",
        ):
            assert key in payload
    finally:
        broker.shutdown()


def test_progress_paces_queued_statements_at_500ms() -> None:
    harness = _DriverHarness(block_execute=True)
    broker = _broker(harness)
    client, _ = _client(_FakeEngine(_attach_rows()), broker)
    try:
        session = _attach(client)
        _wait(lambda: broker.observe(session["session_id"]).state is ConsoleSessionState.IDLE)
        url = f"/api/sql/sessions/{session['session_id']}/statements"
        client.post(
            url,
            headers=_headers(),
            json_body={
                "epoch": session["epoch"],
                "sql": "SELECT 1",
                "client_request_id": "req-1",
            },
        )
        harness.execute_started.wait(3)
        queued = client.post(
            url,
            headers=_headers(),
            json_body={
                "epoch": session["epoch"],
                "sql": "SELECT 2",
                "client_request_id": "req-2",
            },
        ).json()

        payload = client.get(
            f"/api/sql/statements/{queued['statement_id']}/progress", headers=_headers()
        ).json()

        assert payload["state"] == "accepted"
        assert payload["retry_after_ms"] == 500
    finally:
        harness.execute_release.set()
        broker.shutdown()


def test_progress_hides_statements_owned_by_another_user() -> None:
    harness = _DriverHarness()
    broker = _broker(harness)
    client, _ = _client(_FakeEngine(_attach_rows()), broker)
    try:
        session = _attach(client)
        _wait(lambda: broker.observe(session["session_id"]).state is ConsoleSessionState.IDLE)
        submitted = client.post(
            f"/api/sql/sessions/{session['session_id']}/statements",
            headers=_headers(),
            json_body={
                "epoch": session["epoch"],
                "sql": "SELECT 1",
                "client_request_id": "req-1",
            },
        ).json()

        response = client.get(
            f"/api/sql/statements/{submitted['statement_id']}/progress",
            headers=_headers(user_id="user-2"),
        )

        assert response.status_code == 404
    finally:
        broker.shutdown()


def test_progress_of_unknown_statement_is_404() -> None:
    harness = _DriverHarness()
    broker = _broker(harness)
    client, _ = _client(_FakeEngine(), broker)
    try:
        response = client.get("/api/sql/statements/missing/progress", headers=_headers())

        assert response.status_code == 404
        assert response.json()["error"] == "not_found"
    finally:
        broker.shutdown()


# ── result ───────────────────────────────────────────────────────────────────


def test_result_page_mirrors_the_job_result_shape() -> None:
    """`GET .../result` 与 `GET /jobs/{id}/result` **同形**(设计 §3.1)。

    本用例的 result_store 是空替身(spool 里没有行):断言的是形状与
    `statement_state` 归属,不是行内容 —— 行内容平价在 `test_session_parity.py`。
    """

    harness = _DriverHarness()
    broker = _broker(harness)
    client, _ = _client(_FakeEngine(_attach_rows()), broker)
    try:
        session = _attach(client)
        _wait(lambda: broker.observe(session["session_id"]).state is ConsoleSessionState.IDLE)
        submitted = client.post(
            f"/api/sql/sessions/{session['session_id']}/statements",
            headers=_headers(),
            json_body={
                "epoch": session["epoch"],
                "sql": "SELECT 1",
                "client_request_id": "req-1",
            },
        ).json()
        statement_id = submitted["statement_id"]
        _wait(lambda: broker.statement(statement_id).state is ConsoleStatementState.SUCCEEDED)

        response = client.get(f"/api/sql/statements/{statement_id}/result", headers=_headers())

        assert response.status_code == 200
        payload = response.json()
        assert payload["statement_id"] == statement_id
        assert payload["statement_state"] == ConsoleStatementState.SUCCEEDED.value
        assert payload["result_set_id"] == submitted["result_set_id"]
        assert payload["rows"] == []
    finally:
        broker.shutdown()


def test_result_is_404_when_the_statement_has_no_result_set() -> None:
    """没有结果集就如实 404,不编一个空结果页(设计 §3.1)。"""

    harness = _DriverHarness()
    broker = _broker(harness)
    client, _ = _client(_FakeEngine(_attach_rows()), broker)
    try:
        session = _attach(client)
        _wait(lambda: broker.observe(session["session_id"]).state is ConsoleSessionState.IDLE)
        submitted = client.post(
            f"/api/sql/sessions/{session['session_id']}/statements",
            headers=_headers(),
            json_body={
                "epoch": session["epoch"],
                "sql": "SELECT 1",
                "client_request_id": "req-1",
            },
        ).json()
        statement_id = submitted["statement_id"]
        _wait(lambda: broker.statement(statement_id).state is ConsoleStatementState.SUCCEEDED)
        _strip_result_set(broker, statement_id)

        response = client.get(f"/api/sql/statements/{statement_id}/result", headers=_headers())

        assert response.status_code == 404
    finally:
        broker.shutdown()


# ── cancel ───────────────────────────────────────────────────────────────────


def test_cancel_dequeues_a_queued_statement() -> None:
    harness = _DriverHarness(block_execute=True)
    broker = _broker(harness)
    client, services = _client(_FakeEngine(_attach_rows()), broker)
    try:
        session = _attach(client)
        _wait(lambda: broker.observe(session["session_id"]).state is ConsoleSessionState.IDLE)
        url = f"/api/sql/sessions/{session['session_id']}/statements"
        client.post(
            url,
            headers=_headers(),
            json_body={
                "epoch": session["epoch"],
                "sql": "SELECT 1",
                "client_request_id": "req-1",
            },
        )
        harness.execute_started.wait(3)
        queued = client.post(
            url,
            headers=_headers(),
            json_body={
                "epoch": session["epoch"],
                "sql": "SELECT 2",
                "client_request_id": "req-2",
            },
        ).json()

        response = client.post(
            f"/api/sql/statements/{queued['statement_id']}/cancel",
            headers=_headers(),
            json_body={"epoch": session["epoch"]},
        )

        assert response.status_code == 200
        assert response.json() == {"accepted": True, "statement_state": "cancelled"}
        assert "console_statement_cancel" in [audit["action"] for audit in services.audits]
    finally:
        harness.execute_release.set()
        broker.shutdown()


def test_stale_epoch_cancel_is_refused() -> None:
    """M8:取消权随 epoch 移交,旧 tab 杀不掉别人正观察的查询。"""

    harness = _DriverHarness(block_execute=True)
    broker = _broker(harness)
    engine = _FakeEngine(_attach_rows() + _attach_rows())
    client, _ = _client(engine, broker)
    try:
        session = _attach(client)
        _wait(lambda: broker.observe(session["session_id"]).state is ConsoleSessionState.IDLE)
        submitted = client.post(
            f"/api/sql/sessions/{session['session_id']}/statements",
            headers=_headers(),
            json_body={
                "epoch": session["epoch"],
                "sql": "SELECT 1",
                "client_request_id": "req-1",
            },
        ).json()
        harness.execute_started.wait(3)
        _attach(client)

        response = client.post(
            f"/api/sql/statements/{submitted['statement_id']}/cancel",
            headers=_headers(),
            json_body={"epoch": session["epoch"]},
        )

        assert response.status_code == 409
        assert response.json()["error"] == "stale_session_epoch"
        assert response.json()["current_epoch"] == 2
        assert harness.cancelled == []
    finally:
        harness.execute_release.set()
        broker.shutdown()


def test_cancelling_a_terminal_statement_reports_not_accepted() -> None:
    harness = _DriverHarness()
    broker = _broker(harness)
    client, _ = _client(_FakeEngine(_attach_rows()), broker)
    try:
        session = _attach(client)
        _wait(lambda: broker.observe(session["session_id"]).state is ConsoleSessionState.IDLE)
        submitted = client.post(
            f"/api/sql/sessions/{session['session_id']}/statements",
            headers=_headers(),
            json_body={
                "epoch": session["epoch"],
                "sql": "SELECT 1",
                "client_request_id": "req-1",
            },
        ).json()
        statement_id = submitted["statement_id"]
        _wait(lambda: broker.statement(statement_id).state is ConsoleStatementState.SUCCEEDED)

        response = client.post(
            f"/api/sql/statements/{statement_id}/cancel",
            headers=_headers(),
            json_body={"epoch": session["epoch"]},
        )

        assert response.status_code == 200
        assert response.json() == {"accepted": False, "statement_state": "succeeded"}
    finally:
        broker.shutdown()


# ── close ────────────────────────────────────────────────────────────────────


def test_close_queues_graceful_shutdown_and_reports_state() -> None:
    harness = _DriverHarness()
    broker = _broker(harness)
    engine = _FakeEngine(_attach_rows() + _observe_rows())
    client, services = _client(engine, broker)
    try:
        session = _attach(client)
        _wait(lambda: broker.observe(session["session_id"]).state is ConsoleSessionState.IDLE)

        response = client.post(
            f"/api/sql/sessions/{session['session_id']}/close",
            headers=_headers(),
            json_body={"epoch": session["epoch"]},
        )

        assert response.status_code == 200
        # close 在 lane mailbox 上排队仲裁(M6):受理时的状态可能还没翻,
        # 契约保证的是"受理成功 + 最终收敛到 closed",不是同步关闭。
        assert response.json()["state"] in {"idle", "closing", "closed"}
        _wait(lambda: broker.observe(session["session_id"]).state is ConsoleSessionState.CLOSED)
        assert broker.observe(session["session_id"]).session.close_reason == "user"
        assert "console_session_close" in [audit["action"] for audit in services.audits]
    finally:
        broker.shutdown()


def test_stale_epoch_close_is_refused() -> None:
    harness = _DriverHarness()
    broker = _broker(harness)
    engine = _FakeEngine(_attach_rows() + _attach_rows() + _observe_rows())
    client, _ = _client(engine, broker)
    try:
        session = _attach(client)
        _wait(lambda: broker.observe(session["session_id"]).state is ConsoleSessionState.IDLE)
        _attach(client)

        response = client.post(
            f"/api/sql/sessions/{session['session_id']}/close",
            headers=_headers(),
            json_body={"epoch": session["epoch"]},
        )

        assert response.status_code == 409
        assert response.json()["error"] == "stale_session_epoch"
        assert broker.observe(session["session_id"]).state is not ConsoleSessionState.CLOSED
    finally:
        broker.shutdown()


# ── 限流分组 / 审计豁免 ──────────────────────────────────────────────────────


def test_rate_limit_groups_split_control_from_observe() -> None:
    """设计 §3.2:变更类归 sql_control(30/min),观察类归 job_read(300/min)。"""

    harness = _DriverHarness()
    broker = _broker(harness)
    engine = _FakeEngine(_attach_rows() + _observe_rows())
    client, services = _client(engine, broker)
    try:
        session = _attach(client)
        _wait(lambda: broker.observe(session["session_id"]).state is ConsoleSessionState.IDLE)
        submitted = client.post(
            f"/api/sql/sessions/{session['session_id']}/statements",
            headers=_headers(),
            json_body={
                "epoch": session["epoch"],
                "sql": "SELECT 1",
                "client_request_id": "req-1",
            },
        ).json()
        statement_id = submitted["statement_id"]
        client.get(f"/api/sql/sessions/{session['session_id']}", headers=_headers())
        client.get(f"/api/sql/statements/{statement_id}/progress", headers=_headers())
        client.post(
            f"/api/sql/statements/{statement_id}/cancel",
            headers=_headers(),
            json_body={"epoch": session["epoch"]},
        )

        assert services.rate_limiter.groups == [
            "sql_control",  # attach
            "sql_control",  # submit
            "job_read",  # observe
            "job_read",  # progress
            "sql_control",  # cancel
        ]
    finally:
        broker.shutdown()


# ── lifespan 装配 ────────────────────────────────────────────────────────────


def test_app_lifespan_starts_broker_and_closes_sessions_with_shutdown_reason() -> None:
    """优雅停机(设计 §5.1 close_reason):进程退出把活会话收成 shutdown,不留悬挂连接。"""

    harness = _DriverHarness()
    broker = _broker(harness, start=False)
    services = _Services(_FakeEngine(_attach_rows()), broker)
    app = create_app(services=cast(ApiServices, services))
    client = AsgiClient(app)

    async def drive() -> str:
        async with app.router.lifespan_context(app):
            session = await client.request_async(
                "POST",
                "/api/sql/sessions/attach",
                headers=_headers(),
                json_body={"console_id": "console-1"},
            )
            session_id = str(session.json()["session_id"])
            _wait(lambda: broker.observe(session_id).state is ConsoleSessionState.IDLE)
        return session_id

    closed_session_id = asyncio.run(drive())

    observation = broker.observe(closed_session_id)
    assert observation.state is ConsoleSessionState.CLOSED
    assert observation.session.close_reason == "shutdown"
    assert harness.closed.is_set()


def test_app_lifespan_is_a_noop_without_a_broker() -> None:
    """回退开关关闭(或 services 替身无此字段)时 lifespan 不得炸,job 路径照常。"""

    app = create_app(services=cast(ApiServices, _Services(_FakeEngine(), None)))

    async def drive() -> int:
        async with app.router.lifespan_context(app):
            response = await AsgiClient(app).request_async("GET", "/healthz")
        return response.status_code

    assert asyncio.run(drive()) == 200


# ── 错误码族防漂移 ───────────────────────────────────────────────────────────


def test_broker_error_codes_all_map_to_an_http_status() -> None:
    """broker 新增一个 code 而路由层没登记 ⇒ 前端会收到 500。这条用例先红。"""

    source = (Path(__file__).resolve().parents[2] / "app" / "broker" / "core.py").read_text(
        encoding="utf-8"
    )
    raised = set(re.findall(r'BrokerError\(\s*"([a-z_]+)"', source))

    assert raised
    assert raised <= _BROKER_CONFLICT_CODES | {"statement_not_found"}


def test_statement_polls_are_exempt_from_generic_request_audit() -> None:
    harness = _DriverHarness()
    broker = _broker(harness)
    client, services = _client(_FakeEngine(_attach_rows()), broker)
    try:
        session = _attach(client)
        _wait(lambda: broker.observe(session["session_id"]).state is ConsoleSessionState.IDLE)
        submitted = client.post(
            f"/api/sql/sessions/{session['session_id']}/statements",
            headers=_headers(),
            json_body={
                "epoch": session["epoch"],
                "sql": "SELECT 1",
                "client_request_id": "req-1",
            },
        ).json()
        statement_id = submitted["statement_id"]
        services.audits.clear()

        client.get(f"/api/sql/statements/{statement_id}/progress", headers=_headers())
        client.get(f"/api/sql/statements/{statement_id}/result", headers=_headers())

        assert services.audits == []
    finally:
        broker.shutdown()
