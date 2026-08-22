"""Real-MySQL checks for the interactive Session Broker seam.

The existing ``mysql-integration`` Actions job provides MySQL 8 and runs all
``@pytest.mark.integration`` tests, so these cases need no workflow change.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from typing import cast

import pytest

from app.dbclients.interactive import (
    ClassifiedError,
    ErrorCategory,
    InteractiveExecuteError,
    MySQLCancelChannel,
    MySQLInteractiveConnection,
    ServerCancelSupport,
    StatementRequest,
)
from app.domain.datasource import DatasourceConnInfo, DbType
from app.domain.secret import SecretKind, SecretRef
from app.infrastructure.secretstore.protocol import SecretStore

_HOST = os.environ.get("DATAOPS_TEST_MYSQL_HOST")
_PORT = int(os.environ.get("DATAOPS_TEST_MYSQL_PORT", "3306"))
_USER = os.environ.get("DATAOPS_TEST_MYSQL_USER")
_PASSWORD = os.environ.get("DATAOPS_TEST_MYSQL_PASSWORD")
_DATABASE = os.environ.get("DATAOPS_TEST_MYSQL_DATABASE")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        any(value is None for value in (_HOST, _USER, _PASSWORD, _DATABASE)),
        reason="MySQL integration requires DATAOPS_TEST_MYSQL_*",
    ),
]


class _EnvSecretStore:
    def reveal_secret(self, ref: SecretRef) -> str:
        assert ref.kind is SecretKind.DATASOURCE_PASSWORD
        return _PASSWORD or ""


def _secret_store() -> SecretStore:
    return cast(SecretStore, _EnvSecretStore())


def _conn_info() -> DatasourceConnInfo:
    return DatasourceConnInfo(
        host=_HOST or "",
        port=_PORT,
        username=_USER or "",
        database=_DATABASE or "",
        password_ref=SecretRef(ref="env://mysql-a7", kind=SecretKind.DATASOURCE_PASSWORD),
        db_type=DbType.MYSQL,
    )


def test_kill_query_returns_1317_and_owner_connection_is_reusable() -> None:
    marker_ready: queue.Queue[str] = queue.Queue(maxsize=1)
    result_ready: queue.Queue[tuple[ClassifiedError | AssertionError, list[object]]] = queue.Queue(
        maxsize=1
    )

    def run_owner_lane() -> None:
        owner = MySQLInteractiveConnection(_conn_info(), _secret_store(), default_timeout_seconds=0)
        try:
            marker_ready.put(owner.open())
            try:
                list(owner.execute(StatementRequest(sql="SELECT SLEEP(10)", timeout_seconds=0)))
            except InteractiveExecuteError as exc:
                cancelled = exc.classified
            else:
                cancelled = AssertionError("MySQL SLEEP completed before KILL QUERY")
            reused = list(owner.execute(StatementRequest(sql="SELECT 1 + 1", timeout_seconds=0)))
            result_ready.put((cancelled, [row.values[0] for row in reused]))
        finally:
            owner.close()

    owner_thread = threading.Thread(target=run_owner_lane, name="mysql-a7-owner", daemon=True)
    owner_thread.start()
    marker = marker_ready.get(timeout=10)
    channel = MySQLCancelChannel(_conn_info(), _secret_store())
    try:
        assert channel.open() is ServerCancelSupport.AVAILABLE
        time.sleep(0.2)
        channel.cancel(marker)
        classified, reused_values = result_ready.get(timeout=3)
    finally:
        channel.close()
        owner_thread.join(timeout=3)

    assert not owner_thread.is_alive()
    assert isinstance(classified, ClassifiedError)
    assert classified.category is ErrorCategory.CANCELLED
    assert classified.driver_code == 1317
    assert classified.server_confirmed is True
    assert reused_values == [2]
    print("\n[evidence] MySQL KILL QUERY code=1317 reuse=2")


def test_max_execution_time_hint_returns_3024_timeout() -> None:
    owner = MySQLInteractiveConnection(_conn_info(), _secret_store(), default_timeout_seconds=0)
    try:
        owner.open()
        started = time.monotonic()
        with pytest.raises(InteractiveExecuteError) as timeout_error:
            owner.execute(
                StatementRequest(
                    sql="SELECT /*+ MAX_EXECUTION_TIME(100) */ SLEEP(2)",
                    timeout_seconds=0,
                )
            )
        elapsed = time.monotonic() - started
        assert timeout_error.value.classified.category is ErrorCategory.TIMEOUT
        assert timeout_error.value.classified.driver_code == 3024
        assert timeout_error.value.classified.server_confirmed is True
        assert elapsed < 1.0
    finally:
        owner.close()
    print(f"\n[evidence] MySQL max_execution_time code=3024 elapsed={elapsed:.3f}s")


def test_kill_connection_returns_2013_and_marks_owner_dead() -> None:
    marker_ready: queue.Queue[str] = queue.Queue(maxsize=1)
    result_ready: queue.Queue[tuple[ClassifiedError | AssertionError, bool]] = queue.Queue(
        maxsize=1
    )

    def run_owner_lane() -> None:
        owner = MySQLInteractiveConnection(_conn_info(), _secret_store(), default_timeout_seconds=0)
        try:
            marker_ready.put(owner.open())
            try:
                list(owner.execute(StatementRequest(sql="SELECT SLEEP(10)", timeout_seconds=0)))
            except InteractiveExecuteError as exc:
                result_ready.put((exc.classified, owner.is_open))
            else:
                result_ready.put((AssertionError("SLEEP completed before KILL CONNECTION"), True))
        finally:
            owner.close()

    owner_thread = threading.Thread(
        target=run_owner_lane,
        name="mysql-a7-destroy-owner",
        daemon=True,
    )
    owner_thread.start()
    marker = marker_ready.get(timeout=10)
    channel = MySQLCancelChannel(_conn_info(), _secret_store())
    try:
        assert channel.open() is ServerCancelSupport.AVAILABLE
        time.sleep(0.2)
        channel.destroy(marker)
        classified, owner_is_open = result_ready.get(timeout=3)
    finally:
        channel.close()
        owner_thread.join(timeout=3)

    assert not owner_thread.is_alive()
    assert isinstance(classified, ClassifiedError)
    assert classified.category is ErrorCategory.CONNECTION_DEAD
    assert classified.driver_code == 2013
    assert owner_is_open is False
    print("\n[evidence] MySQL KILL CONNECTION code=2013 owner_dead=true")
