"""Oracle 会话缝 + SessionBroker 的真实例验证(设计稿 DB-oracle-oceanbase-adaptation §5)。

只执行 SELECT,不建表、不调 destroy(KILL SESSION 是破坏性命令,且最小权限账号无权),
CONNECT 角色即可跑。GH Actions 无 Oracle,缺环境变量时整文件 skip。运行方式:

    export DATAOPS_TEST_ORACLE_HOST=... DATAOPS_TEST_ORACLE_PORT=1521 \
           DATAOPS_TEST_ORACLE_SERVICE=... DATAOPS_TEST_ORACLE_USERNAME=... \
           DATAOPS_TEST_ORACLE_PASSWORD=...(勿写进 shell history)
    # 11g 需 thick 模式:DATAOPS_ORACLE_CLIENT_DIR=<Instant Client 目录>
    uv run pytest tests/integration/test_session_broker_oracle.py -v
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterator
from typing import cast

import pytest

from app.broker.core import BrokerConfig, SessionBroker
from app.broker.store import AttachRequest, MemoryBrokerStore
from app.dbclients.interactive import (
    ErrorCategory,
    InteractiveExecuteError,
    OracleCancelChannel,
    OracleInteractiveConnection,
    ServerCancelSupport,
    StatementRequest,
)
from app.dbclients.query_limit import apply_database_row_limit
from app.domain.console_session import ConsoleSessionState, ConsoleStatementState
from app.domain.datasource import DatasourceConnInfo, DbType
from app.domain.schema import Column
from app.domain.secret import SecretKind, SecretRef
from app.infrastructure.secretstore.protocol import SecretStore

_HOST = os.environ.get("DATAOPS_TEST_ORACLE_HOST")
_PORT = int(os.environ.get("DATAOPS_TEST_ORACLE_PORT", "1521"))
_SERVICE = os.environ.get("DATAOPS_TEST_ORACLE_SERVICE")
_USER = os.environ.get("DATAOPS_TEST_ORACLE_USERNAME")
_PASSWORD = os.environ.get("DATAOPS_TEST_ORACLE_PASSWORD")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (_HOST and _SERVICE and _USER and _PASSWORD),
        reason="real Oracle instance required (DATAOPS_TEST_ORACLE_* unset)",
    ),
]

# 字典视图笛卡尔积:只读、人人可查、足够慢,几秒内一定还在执行。
_SLOW_SQL = "SELECT COUNT(*) FROM all_objects a, all_objects b"


class _EnvSecretStore:
    def reveal_secret(self, ref: SecretRef) -> str:
        assert ref.kind is SecretKind.DATASOURCE_PASSWORD
        return _PASSWORD or ""


def _conn_info() -> DatasourceConnInfo:
    return DatasourceConnInfo(
        host=_HOST or "",
        port=_PORT,
        username=_USER or "",
        database=_SERVICE,
        password_ref=SecretRef(ref="env", kind=SecretKind.DATASOURCE_PASSWORD),
        db_type=DbType.ORACLE,
    )


_SECRETS = cast(SecretStore, _EnvSecretStore())


@pytest.fixture
def connection() -> Iterator[OracleInteractiveConnection]:
    conn = OracleInteractiveConnection(_conn_info(), _SECRETS)
    conn.open()
    try:
        yield conn
    finally:
        conn.close()


def _wait(predicate: Callable[[], bool], timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition did not become true")


def test_marker_is_sid_and_serial(connection: OracleInteractiveConnection) -> None:
    marker = connection.session_marker
    assert marker is not None
    sid, _, serial = marker.partition(",")
    assert sid.isdigit() and serial.isdigit()


def test_broker_row_limit_runs_on_this_server_version(
    connection: OracleInteractiveConnection,
) -> None:
    # broker 的安全限行渲染 FETCH FIRST;11g 上由会话缝改写为 ROWNUM
    sql = apply_database_row_limit(
        "SELECT owner, object_name FROM all_objects ORDER BY object_id", DbType.ORACLE, 6
    )
    columns: list[Column] = []

    rows = list(connection.execute(StatementRequest(sql=sql, column_sink=columns.extend)))

    assert len(rows) == 6
    assert [column.name for column in columns] == ["OWNER", "OBJECT_NAME"]


def test_offset_paging_hides_the_row_number_column(
    connection: OracleInteractiveConnection,
) -> None:
    columns: list[Column] = []
    sql = (
        "SELECT object_id FROM all_objects ORDER BY object_id OFFSET 3 ROWS FETCH NEXT 2 ROWS ONLY"
    )

    rows = list(connection.execute(StatementRequest(sql=sql, column_sink=columns.extend)))

    assert len(rows) == 2
    assert all(len(row.values) == 1 for row in rows)
    assert [column.name for column in columns] == ["OBJECT_ID"]


def test_local_cancel_is_server_confirmed_and_session_survives(
    connection: OracleInteractiveConnection,
) -> None:
    timer = threading.Timer(2.0, connection.local_cancel)
    timer.start()
    try:
        with pytest.raises(InteractiveExecuteError) as excinfo:
            list(connection.execute(StatementRequest(sql=_SLOW_SQL)))
    finally:
        timer.cancel()

    assert excinfo.value.classified.category is ErrorCategory.CANCELLED
    assert excinfo.value.classified.server_confirmed is True
    assert excinfo.value.classified.driver_code == 1013
    rows = list(connection.execute(StatementRequest(sql="SELECT 1 + 1 FROM DUAL")))
    assert int(rows[0].values[0]) == 2


def test_cancel_channel_reports_available() -> None:
    channel = OracleCancelChannel(_conn_info(), _SECRETS)
    try:
        assert channel.open() is ServerCancelSupport.AVAILABLE
    finally:
        channel.close()


def test_broker_cancel_goes_through_local_cancel_and_session_stays_usable() -> None:
    store = MemoryBrokerStore()
    broker = SessionBroker(
        store,
        connection_factory=lambda _session: OracleInteractiveConnection(_conn_info(), _SECRETS),
        cancel_channel_factory=lambda _datasource: OracleCancelChannel(_conn_info(), _SECRETS),
        config=BrokerConfig(timer_poll_seconds=0.2),
    )
    broker.start()
    try:
        session = broker.attach(
            AttachRequest(console_id="console-1", datasource_id="ds-1", owner_user_id="user-1")
        )
        _wait(lambda: broker.observe(session.id).state is ConsoleSessionState.IDLE)
        slow = broker.submit(session.id, session.epoch, _SLOW_SQL, "request-1")
        _wait(lambda: store.statements[slow.statement.id].state is ConsoleStatementState.EXECUTING)
        time.sleep(1)

        broker.cancel(slow.statement.id, session.epoch)
        _wait(lambda: store.statements[slow.statement.id].state is ConsoleStatementState.CANCELLED)

        assert store.statements[slow.statement.id].error_code == "cancelled:1013"
        events = [e.event_type for e in store.events if e.statement_id == slow.statement.id]
        assert "cancel_dispatched" in events
        _wait(lambda: broker.observe(session.id).state is ConsoleSessionState.IDLE)
        follow_up = broker.submit(session.id, session.epoch, "SELECT 1 FROM DUAL", "request-2")
        _wait(
            lambda: (
                store.statements[follow_up.statement.id].state is ConsoleStatementState.SUCCEEDED
            )
        )
    finally:
        broker.shutdown()
