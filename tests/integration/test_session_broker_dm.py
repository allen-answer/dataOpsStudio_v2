"""Env-gated Session Broker integration checks against the local DM SPIKE target.

The suite is deliberately excluded from CI: DM has no trusted Actions service image.
Set ``DATAOPS_TEST_DM_*`` and run with ``-m dm_integration``; see
``docs/testing/session-broker-integration.md``.  Assertions use only the public
InteractiveConnection / CancelChannel / ErrorClassifier / SessionBroker seams.
"""

from __future__ import annotations

import importlib
import os
import queue
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any, cast

import pytest

from app.broker import AttachRequest, BrokerConfig, MemoryBrokerStore, SessionBroker
from app.dbclients.interactive import (
    ClassifiedError,
    DMCancelChannel,
    DMErrorClassifier,
    DMInteractiveConnection,
    ErrorCategory,
    InteractiveConnectError,
    InteractiveExecuteError,
    ServerCancelSupport,
    StatementRequest,
)
from app.domain.console_session import ConsoleSessionState, ConsoleStatementState
from app.domain.datasource import DatasourceConnInfo, DbType
from app.domain.secret import SecretKind, SecretRef
from app.infrastructure.secretstore.protocol import SecretStore

_HOST = os.environ.get("DATAOPS_TEST_DM_HOST", "127.0.0.1")
_PORT = int(os.environ.get("DATAOPS_TEST_DM_PORT", "5237"))
_USER = os.environ.get("DATAOPS_TEST_DM_USERNAME")
_PASSWORD = os.environ.get("DATAOPS_TEST_DM_PASSWORD")
_SCHEMA = os.environ.get("DATAOPS_TEST_DM_SCHEMA") or _USER or ""
_ALLOW_DDL = os.environ.get("DATAOPS_TEST_DM_ALLOW_DDL") == "1"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.dm_integration,
    pytest.mark.skipif(
        not (_USER and _PASSWORD),
        reason="DM SPIKE credentials require DATAOPS_TEST_DM_USERNAME and _PASSWORD",
    ),
]

_BATCH_TABLE = "DOSV2_A7_BATCH_OBJECT"
_TERMINAL_STATEMENT_STATES = {
    ConsoleStatementState.SUCCEEDED,
    ConsoleStatementState.FAILED,
    ConsoleStatementState.CANCELLED,
    ConsoleStatementState.TIMEOUT,
}


class _EnvSecretStore:
    """Minimal integration seam: the real value remains process-local and is never printed."""

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal_secret(self, ref: SecretRef) -> str:
        assert ref.kind is SecretKind.DATASOURCE_PASSWORD
        return self._value


def _secret_store(value: str | None = None) -> SecretStore:
    return cast(SecretStore, _EnvSecretStore(value if value is not None else (_PASSWORD or "")))


def _conn_info(*, port: int | None = None) -> DatasourceConnInfo:
    return DatasourceConnInfo(
        host=_HOST,
        port=_PORT if port is None else port,
        username=_USER or "",
        # DM's DatasourceConnInfo.database maps to the login schema, not DB_NAME.
        database=_SCHEMA,
        password_ref=SecretRef(ref="env://dm-a7", kind=SecretKind.DATASOURCE_PASSWORD),
        db_type=DbType.DM,
    )


def _raw_connection() -> Any:
    dm = importlib.import_module("dmPython")
    return dm.connect(
        user=_USER,
        password=_PASSWORD,
        server=_HOST,
        port=_PORT,
        schema=_SCHEMA,
        login_timeout=5000,
    )


def _drop_test_table(cursor: Any, name: str) -> None:
    try:
        cursor.execute(f"DROP TABLE {name}")
    except Exception:
        pass


@pytest.fixture(scope="module")
def long_query() -> Iterator[str]:
    """Reuse the one-million-row relation created by the approved DM spike."""
    yield "SELECT COUNT(*) FROM SYSDBA.SPIKE_BIG A, SYSDBA.SPIKE_BIG B"


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition did not become true")


def test_sp_cancel_probe_is_available_on_spike() -> None:
    channel = DMCancelChannel(_conn_info(), _secret_store())
    try:
        assert channel.open() is ServerCancelSupport.AVAILABLE
        assert channel.ping() is True
    finally:
        channel.close()


def test_sp_cancel_finishes_under_one_second_and_owner_connection_is_reusable(
    long_query: str,
) -> None:
    marker_ready: queue.Queue[str] = queue.Queue(maxsize=1)
    cancel_ready: queue.Queue[ClassifiedError | AssertionError] = queue.Queue(maxsize=1)
    reuse_ready: queue.Queue[list[object]] = queue.Queue(maxsize=1)

    def run_owner_lane() -> None:
        owner = DMInteractiveConnection(_conn_info(), _secret_store())
        try:
            marker_ready.put(owner.open())
            try:
                list(owner.execute(StatementRequest(sql=long_query, timeout_seconds=0)))
            except InteractiveExecuteError as exc:
                cancelled = exc.classified
            else:
                cancelled = AssertionError("DM long query completed before SP_CANCEL")
            cancel_ready.put(cancelled)
            reused = list(
                owner.execute(StatementRequest(sql="SELECT 1 + 1 FROM DUAL", timeout_seconds=0))
            )
            reuse_ready.put([row.values[0] for row in reused])
        finally:
            owner.close()

    owner_thread = threading.Thread(target=run_owner_lane, name="dm-a7-owner", daemon=True)
    owner_thread.start()
    marker = marker_ready.get(timeout=10)

    channel = DMCancelChannel(_conn_info(), _secret_store())
    try:
        assert channel.open() is ServerCancelSupport.AVAILABLE
        time.sleep(0.2)  # let the owner enter its server-side execute call
        started = time.monotonic()
        channel.cancel(marker)
        classified = cancel_ready.get(timeout=3)
        cancel_elapsed = time.monotonic() - started
        reused_values = reuse_ready.get(timeout=3)
    finally:
        channel.close()
        owner_thread.join(timeout=3)

    assert not owner_thread.is_alive()
    assert isinstance(classified, ClassifiedError)
    assert classified.category is ErrorCategory.CANCELLED
    assert classified.driver_code == -6515
    assert classified.server_confirmed is True
    assert cancel_elapsed < 1.0
    assert reused_values == [2]
    print(f"\n[evidence] DM SP_CANCEL code=-6515 elapsed={cancel_elapsed:.3f}s reuse=2")


def test_dm_connection_errors_are_classified_by_numeric_code() -> None:
    wrong_value = f"{(_PASSWORD or '')[::-1]}-a7-invalid"
    wrong_password = DMInteractiveConnection(_conn_info(), _secret_store(wrong_value))
    with pytest.raises(InteractiveConnectError) as auth_error:
        wrong_password.open()
    assert auth_error.value.classified.category is ErrorCategory.AUTH_FAILED
    assert auth_error.value.classified.driver_code == -2501

    unreachable = DMInteractiveConnection(
        _conn_info(port=1),
        _secret_store(),
        login_timeout_ms=1000,
    )
    with pytest.raises(InteractiveConnectError) as host_error:
        unreachable.open()
    assert host_error.value.classified.category is ErrorCategory.HOST_UNREACHABLE
    assert host_error.value.classified.driver_code == -70028
    print("\n[evidence] DM connection classification codes=-2501,-70028")


@pytest.mark.skipif(
    not _ALLOW_DDL,
    reason="set DATAOPS_TEST_DM_ALLOW_DDL=1 for the self-owned -2104 batch test",
)
def test_dm_same_batch_new_object_error_is_minus_2104() -> None:
    cleanup = _raw_connection()
    cleanup_cursor = cleanup.cursor()
    owner = DMInteractiveConnection(_conn_info(), _secret_store())
    try:
        _drop_test_table(cleanup_cursor, _BATCH_TABLE)
        cleanup.commit()
        owner.open()
        batch = f"CREATE TABLE {_BATCH_TABLE} (ID INT); INSERT INTO {_BATCH_TABLE} (ID) VALUES (1)"
        with pytest.raises(InteractiveExecuteError) as compile_error:
            owner.execute(StatementRequest(sql=batch, timeout_seconds=0))
        assert compile_error.value.classified.category is ErrorCategory.STATEMENT_ERROR
        assert compile_error.value.classified.driver_code == -2104
    finally:
        owner.close()
        _drop_test_table(cleanup_cursor, _BATCH_TABLE)
        cleanup.commit()
        cleanup_cursor.close()
        cleanup.close()
    print("\n[evidence] DM same-batch classification code=-2104")


def test_broker_timeout_runs_cancel_ladder_and_preserves_session(long_query: str) -> None:
    store = MemoryBrokerStore()
    broker = SessionBroker(
        store,
        connection_factory=lambda _session: DMInteractiveConnection(_conn_info(), _secret_store()),
        cancel_channel_factory=lambda _datasource_id: DMCancelChannel(
            _conn_info(), _secret_store()
        ),
        config=BrokerConfig(
            cancel_grace_seconds=2,
            timer_poll_seconds=0.02,
            idle_timeout_seconds=60,
        ),
        boot_id="dm-a7-integration",
    )
    broker.start()
    try:
        session = broker.attach(
            AttachRequest(
                console_id="dm-a7-console",
                datasource_id="dm-a7-datasource",
                owner_user_id="dm-a7-user",
            )
        )
        _wait_until(lambda: broker.observe(session.id).state is ConsoleSessionState.IDLE)
        receipt = broker.submit(
            session.id,
            session.epoch,
            long_query,
            "dm-a7-timeout",
            timeout_seconds=1,
        )
        _wait_until(
            lambda: broker.statement(receipt.statement.id).state in _TERMINAL_STATEMENT_STATES,
            timeout=5,
        )
        timed_out = broker.statement(receipt.statement.id)
        assert timed_out.state is ConsoleStatementState.TIMEOUT
        # Hard cancel can interrupt execute (-6515), or the soft flag can win at
        # the first fetch boundary. Both are honest timeout-ladder outcomes.
        assert timed_out.error_code in {"cancelled", "cancelled:-6515"}
        _wait_until(lambda: broker.observe(session.id).state is ConsoleSessionState.IDLE)

        event_types = [
            event.event_type for event in store.events if event.statement_id == receipt.statement.id
        ]
        assert event_types.index("cancel_requested") < event_types.index("cancel_dispatched")
        assert "cancel_escalated" not in event_types

        reuse = broker.submit(
            session.id,
            session.epoch,
            "SELECT 1 + 1 FROM DUAL",
            "dm-a7-timeout-reuse",
            timeout_seconds=2,
        )
        _wait_until(
            lambda: broker.statement(reuse.statement.id).state in _TERMINAL_STATEMENT_STATES
        )
        assert broker.statement(reuse.statement.id).state is ConsoleStatementState.SUCCEEDED
    finally:
        broker.shutdown()
    print("\n[evidence] DM timeout ladder=deadline->SP_CANCEL->timeout; session reused")


def test_dm_error_classifier_pins_all_required_numeric_codes() -> None:
    classifier = DMErrorClassifier()

    class DmCode:
        def __init__(self, code: int) -> None:
            self.code = code

    assert classifier.classify(Exception(DmCode(-6515))).category is ErrorCategory.CANCELLED
    assert classifier.classify(Exception(DmCode(-2501))).category is ErrorCategory.AUTH_FAILED
    assert classifier.classify(Exception(DmCode(-70028))).category is ErrorCategory.HOST_UNREACHABLE
    assert classifier.classify(Exception(DmCode(-2104))).category is ErrorCategory.STATEMENT_ERROR
