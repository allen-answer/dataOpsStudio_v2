"""会话缝(InteractiveConnection / CancelChannel)单测 —— fake driver,不连真库。

覆盖 A2 的验收面:登录超时接线(Q3 修法)、session_marker 采集、软取消、
错误分类接线、MySQL 保险带逐语句同步(评审修订 R2)、SP_CANCEL 自探测与降级。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, cast

import pytest

from app.dbclients.interactive import (
    DM_CANCEL_SQL,
    DM_DEFAULT_LOGIN_TIMEOUT_MS,
    DM_DESTROY_SQL,
    MYSQL_SAFETY_BELT_MARGIN_SECONDS,
    CancelChannel,
    CancelChannelError,
    DMCancelChannel,
    DMInteractiveConnection,
    ErrorCategory,
    InteractiveConnectError,
    InteractiveConnection,
    InteractiveExecuteError,
    InvalidDatasourceError,
    MySQLCancelChannel,
    MySQLInteractiveConnection,
    ServerCancelSupport,
    SessionNotOpenError,
    SoftCancelledError,
    StatementRequest,
)
from app.domain.datasource import DbType
from app.domain.schema import Column, ColumnType, Row
from app.infrastructure.secretstore.protocol import SecretStore
from tests.unit._interactive_fakes import (
    FakeConnection,
    FakeDM,
    FakePyMySQL,
    SecretStoreStub,
    SSCursorMarker,
    conn_info,
    dm_connect_error,
    dm_error,
    mysql_error,
)


def _dm(
    fake: FakeDM | None = None,
    *,
    store: SecretStoreStub | None = None,
    **kwargs: Any,
) -> tuple[DMInteractiveConnection, FakeDM]:
    fake = fake or FakeDM()
    conn = DMInteractiveConnection(
        conn_info(DbType.DM),
        cast(SecretStore, store or SecretStoreStub()),
        dm_module=fake,
        **kwargs,
    )
    return conn, fake


def _mysql(
    fake: FakePyMySQL | None = None,
    **kwargs: Any,
) -> tuple[MySQLInteractiveConnection, FakePyMySQL]:
    fake = fake or FakePyMySQL()
    conn = MySQLInteractiveConnection(
        conn_info(DbType.MYSQL),
        cast(SecretStore, SecretStoreStub()),
        pymysql_module=fake,
        **kwargs,
    )
    return conn, fake


def _select() -> StatementRequest:
    return StatementRequest(sql="SELECT n FROM t", fetch_size=2)


# ── 构造与能力矩阵 ──────────────────────────────────────────────────────────


def test_dialect_and_secret_kind_are_enforced() -> None:
    with pytest.raises(InvalidDatasourceError):
        DMInteractiveConnection(
            conn_info(DbType.MYSQL), cast(SecretStore, SecretStoreStub()), dm_module=FakeDM()
        )
    with pytest.raises(InvalidDatasourceError):
        MySQLInteractiveConnection(
            conn_info(DbType.DM), cast(SecretStore, SecretStoreStub()), pymysql_module=FakePyMySQL()
        )


def test_capabilities_match_the_spike_evidence() -> None:
    """§5.4 能力矩阵:DM 无服务端语句超时属性,不能声明有。"""
    dm_caps = DMInteractiveConnection.capabilities
    assert (dm_caps.server_cancel, dm_caps.server_statement_timeout, dm_caps.session_streaming) == (
        True,
        False,
        True,
    )
    my_caps = MySQLInteractiveConnection.capabilities
    assert (my_caps.server_cancel, my_caps.server_statement_timeout, my_caps.session_streaming) == (
        True,
        True,
        True,
    )


def test_implementations_satisfy_the_protocols() -> None:
    conn, _ = _dm()
    channel = DMCancelChannel(
        conn_info(DbType.DM), cast(SecretStore, SecretStoreStub()), dm_module=FakeDM()
    )
    assert isinstance(conn, InteractiveConnection)
    assert isinstance(channel, CancelChannel)


# ── 登录超时接线(Q3 修法)────────────────────────────────────────────────


def test_dm_login_timeout_is_wired_to_login_timeout_in_milliseconds() -> None:
    """Q3 修法①:接 `login_timeout`(毫秒),不再是"连接秒数 x 1000"塞错属性。"""
    conn, fake = _dm()
    conn.open()
    kwargs = fake.connect_kwargs[0]
    assert kwargs["login_timeout"] == DM_DEFAULT_LOGIN_TIMEOUT_MS == 5000
    assert "connection_timeout" not in kwargs


def test_dm_never_touches_call_timeout_or_fabricates_a_server_timeout() -> None:
    """Q3 修法②:不给不存在的 `callTimeout` 赋值;DM 侧不发任何超时 SQL。"""
    conn, fake = _dm()
    conn.open()
    list(conn.execute(StatementRequest(sql="SELECT n FROM t", timeout_seconds=42)))
    driver_conn = fake.connections[0]
    assert not hasattr(driver_conn, "callTimeout")
    assert not any("timeout" in sql for sql in driver_conn.statements)


def test_mysql_login_timeout_converts_milliseconds_to_seconds() -> None:
    conn, fake = _mysql(login_timeout_ms=10_000)
    conn.open()
    assert fake.connect_kwargs[0]["connect_timeout"] == 10


def test_mysql_session_connection_sets_no_socket_read_write_timeout() -> None:
    """§4.2:会话连接上设 read_timeout 会误杀长查询 —— job 路径的做法不能照搬。"""
    conn, fake = _mysql()
    conn.open()
    kwargs = fake.connect_kwargs[0]
    assert "read_timeout" not in kwargs
    assert "write_timeout" not in kwargs


def test_connect_failure_is_classified_from_the_driver_code() -> None:
    conn, _ = _dm(FakeDM(connect_error=dm_connect_error(-2501)))
    with pytest.raises(InteractiveConnectError) as excinfo:
        conn.open()
    assert excinfo.value.classified.category is ErrorCategory.AUTH_FAILED
    assert excinfo.value.classified.driver_code == -2501
    assert conn.is_open is False
    assert conn.session_marker is None


def test_unmapped_connect_failure_that_burns_the_login_budget_is_login_timeout() -> None:
    """码表不认、又恰好烧满登录预算 ⇒ login_timeout(计时判据,非文本判据)。"""
    fake = FakeDM(connect_error=RuntimeError("no code here"), connect_delay_seconds=0.02)
    conn, _ = _dm(fake, login_timeout_ms=5)
    with pytest.raises(InteractiveConnectError) as excinfo:
        conn.open()
    assert excinfo.value.classified.category is ErrorCategory.LOGIN_TIMEOUT


def test_unmapped_connect_failure_inside_the_budget_stays_unknown() -> None:
    conn, _ = _dm(FakeDM(connect_error=RuntimeError("no code here")), login_timeout_ms=600_000)
    with pytest.raises(InteractiveConnectError) as excinfo:
        conn.open()
    assert excinfo.value.classified.category is ErrorCategory.UNKNOWN


# ── session_marker 采集 ─────────────────────────────────────────────────────


def test_dm_marker_comes_from_sessid() -> None:
    conn, fake = _dm()
    marker = conn.open()
    assert marker == "142"
    assert conn.session_marker == "142"
    assert fake.connections[0].statements[0] == "select sessid()"


def test_mysql_marker_comes_from_connection_id() -> None:
    conn, fake = _mysql()
    marker = conn.open()
    assert marker == "99"
    assert "select connection_id()" in fake.connections[0].statements


def test_marker_failure_closes_the_connection_and_reports_connect_failed() -> None:
    fake = FakeDM()
    conn, _ = _dm(fake)
    original_connect = fake.connect

    def connect_with_broken_marker(**kwargs: Any) -> FakeConnection:
        driver_conn = original_connect(**kwargs)
        driver_conn.errors["sessid"] = dm_error(-6001)
        return driver_conn

    fake.connect = connect_with_broken_marker  # type: ignore[method-assign]
    with pytest.raises(InteractiveConnectError):
        conn.open()
    assert conn.is_open is False
    assert fake.connections[0].closed is True


def test_open_twice_is_rejected() -> None:
    conn, _ = _dm()
    conn.open()
    with pytest.raises(SessionNotOpenError):
        conn.open()


def test_execute_before_open_is_rejected() -> None:
    conn, _ = _dm()
    with pytest.raises(SessionNotOpenError):
        conn.execute(_select())


# ── 流式执行与会话续用 ──────────────────────────────────────────────────────


def test_execute_streams_in_batches_and_keeps_the_session_open() -> None:
    conn, fake = _dm()
    conn.open()
    rows = list(conn.execute(_select()))
    assert rows == [Row(values=[1]), Row(values=[2]), Row(values=[3])]
    driver_conn = fake.connections[0]
    # 末尾那次空批是"取完了"的判据,不是多余调用。
    assert driver_conn.cursors[-1].fetchmany_sizes == [2, 2, 2]
    # 语句 cursor 关闭,但**连接留着** —— 会话式执行的全部意义。
    assert all(cursor.closed for cursor in driver_conn.cursors)
    assert driver_conn.closed is False
    assert conn.is_open is True


def test_two_statements_reuse_one_connection() -> None:
    conn, fake = _dm()
    conn.open()
    list(conn.execute(_select()))
    list(conn.execute(_select()))
    assert len(fake.connections) == 1
    # 建连只 reveal 一次密钥(P5:会话复用天然减少 reveal 次数)。
    assert len(fake.connect_kwargs) == 1


def test_secret_is_revealed_only_at_connect_time() -> None:
    store = SecretStoreStub()
    conn, _ = _dm(store=store)
    conn.open()
    list(conn.execute(_select()))
    assert store.revealed_refs == ["secret-1"]


def test_mysql_uses_a_streaming_cursor() -> None:
    conn, fake = _mysql()
    conn.open()
    list(conn.execute(_select()))
    assert SSCursorMarker in fake.connections[0].cursor_classes


def test_column_sink_receives_mapped_columns() -> None:
    captured: list[Column] = []
    conn, _ = _mysql()
    conn.open()
    list(conn.execute(StatementRequest(sql="SELECT n FROM t", column_sink=captured.extend)))
    assert [column.name for column in captured] == ["N"]
    assert captured[0].type is ColumnType.INTEGER


def test_fetch_size_must_be_positive() -> None:
    conn, _ = _dm()
    conn.open()
    with pytest.raises(ValueError):
        conn.execute(StatementRequest(sql="SELECT 1", fetch_size=0))


# ── 软取消(阶梯第 1 级)────────────────────────────────────────────────────


def test_soft_cancel_between_batches_stops_streaming_without_claiming_confirmation() -> None:
    conn, _ = _dm()
    conn.open()
    stream = conn.execute(StatementRequest(sql="SELECT n FROM t", fetch_size=1))
    assert next(stream) == Row(values=[1])
    conn.request_soft_cancel()
    # 检查点在**批之间**:当前批发完即停,不等整条语句跑完。
    with pytest.raises(SoftCancelledError) as excinfo:
        next(stream)
    classified = excinfo.value.classified
    assert classified.category is ErrorCategory.CANCELLED
    # 客户端跳出取数循环 ≠ 服务端已停。绝不冒充 server_confirmed。
    assert classified.server_confirmed is False


def test_pending_soft_cancel_blocks_the_next_statement_instead_of_running_it_uncancellable() -> (
    None
):
    conn, fake = _dm()
    conn.open()
    conn.request_soft_cancel()
    with pytest.raises(SoftCancelledError):
        conn.execute(_select())
    # execute 根本没发出去。
    assert not any("from t" in sql for sql in fake.connections[0].statements)


def test_clearing_the_flag_lets_the_next_statement_run() -> None:
    conn, _ = _dm()
    conn.open()
    conn.request_soft_cancel()
    requested = conn.soft_cancel_requested
    conn.clear_soft_cancel()
    cleared = conn.soft_cancel_requested
    assert (requested, cleared) == (True, False)
    assert list(conn.execute(_select())) == [Row(values=[1]), Row(values=[2]), Row(values=[3])]


# ── 执行期错误分类接线 ──────────────────────────────────────────────────────


def test_server_confirmed_cancel_keeps_the_session_usable() -> None:
    """spike 实证:DM 取消后 owner 连接可续用(cancelling → idle 通路的根据)。"""
    conn, fake = _dm()
    conn.open()
    fake.connections[0].errors["from t"] = dm_error(-6515)
    with pytest.raises(InteractiveExecuteError) as excinfo:
        list(conn.execute(_select()))
    classified = excinfo.value.classified
    assert classified.category is ErrorCategory.CANCELLED
    assert classified.server_confirmed is True
    assert classified.connection_aborted is False
    assert conn.is_open is True


def test_statement_error_keeps_the_session_usable() -> None:
    conn, fake = _dm()
    conn.open()
    fake.connections[0].errors["from t"] = dm_error(-2106)
    with pytest.raises(InteractiveExecuteError) as excinfo:
        list(conn.execute(_select()))
    assert excinfo.value.classified.category is ErrorCategory.STATEMENT_ERROR
    assert conn.is_open is True


def test_connection_dead_marks_the_session_lost() -> None:
    conn, fake = _mysql()
    conn.open()
    fake.connections[0].errors["from t"] = mysql_error(2013)
    with pytest.raises(InteractiveExecuteError) as excinfo:
        list(conn.execute(_select()))
    assert excinfo.value.classified.connection_aborted is True
    assert conn.is_open is False


def test_error_during_fetch_is_classified_and_closes_the_cursor() -> None:
    conn, fake = _mysql()
    conn.open()
    stream = conn.execute(_select())
    fake.connections[0].cursors[-1].fetch_errors[2] = mysql_error(1317)
    assert next(stream) == Row(values=[1])
    next(stream)
    with pytest.raises(InteractiveExecuteError) as excinfo:
        next(stream)
    assert excinfo.value.classified.category is ErrorCategory.CANCELLED
    assert fake.connections[0].cursors[-1].closed is True


def test_failed_execute_closes_its_cursor() -> None:
    conn, fake = _dm()
    conn.open()
    fake.connections[0].errors["from t"] = dm_error(-2106)
    with pytest.raises(InteractiveExecuteError):
        conn.execute(_select())
    assert all(cursor.closed for cursor in fake.connections[0].cursors)


def test_ping_reports_and_records_a_dead_connection() -> None:
    conn, fake = _mysql()
    conn.open()
    assert conn.ping() is True
    fake.connections[0].errors["select 1"] = mysql_error(2006)
    assert conn.ping() is False
    assert conn.is_open is False


def test_close_is_idempotent() -> None:
    conn, fake = _dm()
    conn.open()
    conn.close()
    conn.close()
    assert fake.connections[0].closed is True
    assert conn.is_open is False


# ── MySQL 服务端保险带逐语句同步(评审修订 R2)──────────────────────────────


def _belt_statements(driver_conn: FakeConnection) -> list[str]:
    return [sql for sql in driver_conn.statements if "max_execution_time" in sql]


def _belt_values(driver_conn: FakeConnection) -> list[Any]:
    return [
        cursor.params[index]
        for cursor in driver_conn.cursors
        for index, sql in enumerate(cursor.executed)
        if "max_execution_time" in sql
    ]


def test_belt_is_armed_at_connect_with_the_default_timeout_plus_margin() -> None:
    conn, fake = _mysql(default_timeout_seconds=600)
    conn.open()
    assert _belt_values(fake.connections[0]) == [((600 + MYSQL_SAFETY_BELT_MARGIN_SECONDS) * 1000,)]
    assert conn.safety_belt_seconds == 630


def test_belt_is_resynced_before_execute_when_the_statement_timeout_differs() -> None:
    """R2 的核心:同步必须发生在 **execute 之前**,顺序也要对。"""
    conn, fake = _mysql(default_timeout_seconds=600)
    conn.open()
    list(conn.execute(StatementRequest(sql="SELECT n FROM t", timeout_seconds=5)))
    statements = fake.connections[0].statements
    belt_index = max(i for i, sql in enumerate(statements) if "max_execution_time" in sql)
    execute_index = max(i for i, sql in enumerate(statements) if "from t" in sql)
    assert belt_index < execute_index
    assert _belt_values(fake.connections[0])[-1] == ((5 + MYSQL_SAFETY_BELT_MARGIN_SECONDS) * 1000,)


def test_unlimited_statement_disarms_the_belt() -> None:
    """0 = 不限:必须写 0 关掉保险带,否则"不限时"的语句被默认带误杀。"""
    conn, fake = _mysql(default_timeout_seconds=600)
    conn.open()
    list(conn.execute(StatementRequest(sql="SELECT n FROM t", timeout_seconds=0)))
    assert _belt_values(fake.connections[0])[-1] == (0,)
    assert conn.safety_belt_seconds == 0


def test_belt_is_not_resent_when_the_timeout_is_unchanged() -> None:
    conn, fake = _mysql(default_timeout_seconds=600)
    conn.open()
    list(conn.execute(StatementRequest(sql="SELECT n FROM t", timeout_seconds=600)))
    list(conn.execute(StatementRequest(sql="SELECT n FROM t", timeout_seconds=600)))
    assert len(_belt_statements(fake.connections[0])) == 1


def test_belt_switches_back_from_unlimited_to_bounded() -> None:
    conn, fake = _mysql(default_timeout_seconds=600)
    conn.open()
    list(conn.execute(StatementRequest(sql="SELECT n FROM t", timeout_seconds=0)))
    list(conn.execute(StatementRequest(sql="SELECT n FROM t", timeout_seconds=10)))
    assert _belt_values(fake.connections[0])[-2:] == [(0,), ((10 + 30) * 1000,)]


def test_missing_server_variable_is_reported_not_hidden() -> None:
    """老 MySQL 没有 max_execution_time:语句照跑,但如实说"没有保险带"。"""
    fake = FakePyMySQL()
    conn, _ = _mysql(fake)
    original_connect = fake.connect

    def connect_without_belt(**kwargs: Any) -> FakeConnection:
        driver_conn = original_connect(**kwargs)
        driver_conn.errors["max_execution_time"] = mysql_error(1193, "unknown system variable")
        return driver_conn

    fake.connect = connect_without_belt  # type: ignore[method-assign]
    conn.open()
    assert conn.server_statement_timeout_active is False
    assert conn.safety_belt_seconds is None
    assert list(conn.execute(_select())) == [Row(values=[1]), Row(values=[2]), Row(values=[3])]


def test_dm_never_sends_a_belt_statement() -> None:
    conn, fake = _dm()
    conn.open()
    list(conn.execute(StatementRequest(sql="SELECT n FROM t", timeout_seconds=7)))
    assert _belt_statements(fake.connections[0]) == []


# ── CancelChannel:自探测、硬取消、破坏性兜底 ───────────────────────────────


def _dm_channel(fake: FakeDM | None = None) -> tuple[DMCancelChannel, FakeDM]:
    fake = fake or FakeDM()
    channel = DMCancelChannel(
        conn_info(DbType.DM), cast(SecretStore, SecretStoreStub()), dm_module=fake
    )
    return channel, fake


def _mysql_channel(fake: FakePyMySQL | None = None) -> tuple[MySQLCancelChannel, FakePyMySQL]:
    fake = fake or FakePyMySQL()
    channel = MySQLCancelChannel(
        conn_info(DbType.MYSQL), cast(SecretStore, SecretStoreStub()), pymysql_module=fake
    )
    return channel, fake


def test_probe_calls_sp_cancel_on_its_own_sid_and_reports_available() -> None:
    channel, fake = _dm_channel()
    assert channel.support is ServerCancelSupport.UNKNOWN
    assert channel.open() is ServerCancelSupport.AVAILABLE
    driver_conn = fake.connections[0]
    assert "call sp_cancel_session_operation(?)" in driver_conn.statements
    # 探测打在**自己的** sid 上(自会话空转,无害)。
    probe_params = [
        cursor.params[index]
        for cursor in driver_conn.cursors
        for index, sql in enumerate(cursor.executed)
        if "sp_cancel_session_operation" in sql
    ]
    assert probe_params == [(142,)]


def test_probe_permission_failure_degrades_instead_of_pretending() -> None:
    """生产受限账号无 SP_CANCEL 权限(DM-SPIKE §4①)—— 如实 degraded。"""
    fake = FakeDM()
    channel, _ = _dm_channel(fake)
    original_connect = fake.connect

    def connect_without_cancel_privilege(**kwargs: Any) -> FakeConnection:
        driver_conn = original_connect(**kwargs)
        driver_conn.errors["sp_cancel_session_operation"] = dm_error(-5567, "no privilege")
        return driver_conn

    fake.connect = connect_without_cancel_privilege  # type: ignore[method-assign]
    assert channel.open() is ServerCancelSupport.DEGRADED
    assert channel.support is ServerCancelSupport.DEGRADED


def test_probe_treats_a_self_interrupt_as_proof_that_cancel_works() -> None:
    """MySQL `KILL QUERY <自己>` 可能反手打断探测语句:1317 恰恰是能力证据。"""
    fake = FakePyMySQL()
    channel, _ = _mysql_channel(fake)
    original_connect = fake.connect

    def connect_with_self_interrupt(**kwargs: Any) -> FakeConnection:
        driver_conn = original_connect(**kwargs)
        driver_conn.errors["kill query"] = mysql_error(1317)
        return driver_conn

    fake.connect = connect_with_self_interrupt  # type: ignore[method-assign]
    assert channel.open() is ServerCancelSupport.AVAILABLE


def test_cancel_and_destroy_use_the_documented_commands() -> None:
    channel, fake = _dm_channel()
    channel.open()
    channel.cancel("777")
    channel.destroy("777")
    statements = fake.connections[0].statements
    assert DM_CANCEL_SQL.lower() in statements
    assert DM_DESTROY_SQL.lower() in statements


def test_mysql_cancel_and_destroy_use_kill_query_and_kill_connection() -> None:
    channel, fake = _mysql_channel()
    channel.open()
    channel.cancel("777")
    channel.destroy("777")
    statements = fake.connections[0].statements
    assert "kill query %s" in statements
    assert "kill connection %s" in statements


@pytest.mark.parametrize("marker", ["1; DROP TABLE t", "abc", "", "-5", "1 OR 1=1"])
def test_non_numeric_markers_are_refused_before_any_sql_is_issued(marker: str) -> None:
    channel, fake = _dm_channel()
    channel.open()
    before = len(fake.connections[0].statements)
    with pytest.raises(CancelChannelError):
        channel.cancel(marker)
    assert len(fake.connections[0].statements) == before


def test_control_connection_is_rebuilt_when_the_ping_fails() -> None:
    """使用前 ping,失败即用当前凭据重建(天然吸收密钥轮换,§4.1)。"""
    channel, fake = _dm_channel()
    channel.open()
    fake.connections[0].errors["select 1"] = dm_error(-70028)
    channel.cancel("777")
    assert len(fake.connections) == 2
    assert fake.connections[0].closed is True
    assert DM_CANCEL_SQL.lower() in fake.connections[1].statements


def test_probe_result_survives_a_rebuild() -> None:
    """探测结果缓存于控制 lane 生命周期(§4.1),重建不重探。"""
    channel, fake = _dm_channel()
    channel.open()
    fake.connections[0].errors["select 1"] = dm_error(-70028)
    channel.cancel("777")
    assert channel.support is ServerCancelSupport.AVAILABLE
    assert "sp_cancel_session_operation" not in " ".join(fake.connections[1].statements[:1])


def test_unavailable_control_connection_is_reported_not_swallowed() -> None:
    """控制连接建不起来 ⇒ 报错让 broker 升级阶梯,绝不静默当作"已取消"。"""
    channel, fake = _dm_channel()
    channel.open()
    fake.connections[0].errors["select 1"] = dm_error(-70028)
    fake.connect_error = dm_connect_error(-70028)
    with pytest.raises(CancelChannelError) as excinfo:
        channel.cancel("777")
    assert excinfo.value.classified.category is ErrorCategory.HOST_UNREACHABLE


def test_control_command_failure_is_classified() -> None:
    channel, fake = _dm_channel()
    channel.open()
    fake.connections[0].errors["sp_close_session"] = dm_error(-5567)
    with pytest.raises(CancelChannelError) as excinfo:
        channel.destroy("777")
    assert excinfo.value.classified.driver_code == -5567


def test_open_failure_reports_a_cancel_channel_error() -> None:
    channel, _ = _dm_channel(FakeDM(connect_error=dm_connect_error(-2501)))
    with pytest.raises(CancelChannelError) as excinfo:
        channel.open()
    assert excinfo.value.classified.category is ErrorCategory.AUTH_FAILED


# ── 边界纪律:不碰既有 adapter、不写日志 ────────────────────────────────────


def _seam_sources() -> dict[str, str]:
    package = Path(__file__).resolve().parents[2] / "app" / "dbclients" / "interactive"
    return {path.name: path.read_text(encoding="utf-8") for path in package.glob("*.py")}


def _imported_modules(source: str) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_seam_never_imports_the_job_path_adapters() -> None:
    """A2 的硬约束:不 import 不修改既有 adapter(共享深模块,不共享连接)。"""
    for name, source in _seam_sources().items():
        for module in _imported_modules(source):
            assert not module.endswith("_adapter"), f"{name} import 了 {module}"


def test_seam_shares_only_the_type_mapping_modules() -> None:
    sources = _seam_sources()
    assert "from app.dbclients.dm_types import" in sources["dm.py"]
    assert "from app.dbclients.mysql_types import" in sources["mysql.py"]


def test_seam_writes_no_logs() -> None:
    """R5:脱敏纪律靠 broker 的事件通道,缝里一行日志都不写(也就无从泄密)。"""
    for name, source in _seam_sources().items():
        assert "import logging" not in source, name
        assert "structlog" not in source, name
