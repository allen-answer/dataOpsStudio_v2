"""会话结果集在**真 PG** 上的平价契约(Session Broker 设计 §10-A5)。

这一层只有真库能证:`result_sets` catalog 的写入与 3/console 淘汰、SQL 历史两条
路径合流的 SQL、以及评审修订 **R3** 的端到端形态(活跃会话的结果集不被 worker
spool GC 回收)。CI 的 unit+contract job 带 `DATAOPS_TEST_PG_URL`;本机没设就跳过
(R9:不落 SQLite 兜底)。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete, insert, select, text
from sqlalchemy.engine import Engine

from app.api.app import create_app
from app.api.security import create_access_token
from app.api.services import ApiServices
from app.broker.results import SpoolStatementResults, StatementMetrics, StatementSpool
from app.db.models import (
    console_sessions,
    console_statements,
    datasources,
    jobs,
    metadata,
    result_sets,
    sql_consoles,
)
from app.domain.console_session import (
    ConsoleSessionState,
    ConsoleStatement,
    ConsoleStatementKind,
    ConsoleStatementState,
)
from app.domain.job import JobKind, JobStatus
from app.domain.schema import Column, ColumnType, Row
from app.infrastructure.resultstore.local_fs import LocalFsResultStore
from app.worker import (
    PostgresActiveConsoleResultSets,
    WorkerRunner,
    WorkerRunnerConfig,
)
from tests._asgi_client import AsgiClient

pytestmark = pytest.mark.contract

JWT_SECRET = "jwt-secret"
USER_ID = "u_parity"
PROJECT_ID = "p_parity"
DATASOURCE_ID = "ds_parity"
CONSOLE_ID = "console_parity"


# ── fixtures ─────────────────────────────────────────────────────────────────


def _pg_engine_or_skip() -> Engine:
    url = os.environ.get("DATAOPS_TEST_PG_URL") or os.environ.get("DATAOPS_DATABASE_URL")
    if not url:
        pytest.skip("Session parity PG contract tests require DATAOPS_TEST_PG_URL")
    return create_engine(url)


@pytest.fixture
def engine() -> Iterator[Engine]:
    engine = _pg_engine_or_skip()
    metadata.create_all(engine)
    _seed_fixtures(engine)
    yield engine
    _clear_fixtures(engine)


def _seed_fixtures(engine: Engine) -> None:
    _clear_fixtures(engine)
    now = datetime.now(UTC)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO users (id, username, password_hash)
                VALUES (:id, :username, :password_hash)
                ON CONFLICT (id) DO NOTHING
                """
            ),
            # R10:hex 切片即唯一,不 f-string 拼前缀。
            {"id": USER_ID, "username": uuid4().hex[:16], "password_hash": "not-a-real-hash"},
        )
        conn.execute(
            text(
                """
                INSERT INTO projects (id, name, owner_user_id)
                VALUES (:id, :name, :owner_user_id)
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {"id": PROJECT_ID, "name": uuid4().hex[:16], "owner_user_id": USER_ID},
        )
        conn.execute(
            insert(datasources).values(
                id=DATASOURCE_ID,
                project_id=PROJECT_ID,
                name=uuid4().hex[:16],
                db_type="mysql",
                host="mysql.internal",
                port=3306,
                username="dataops",
                database_name="app",
                password_secret_ref="secret-1",
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            insert(sql_consoles).values(
                id=CONSOLE_ID,
                owner_user_id=USER_ID,
                name="console",
                datasource_id=DATASOURCE_ID,
                sql="SELECT 1",
                pinned=False,
                created_at=now,
                updated_at=now,
            )
        )


def _clear_fixtures(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(delete(console_statements))
        conn.execute(delete(console_sessions))
        conn.execute(delete(result_sets))
        conn.execute(delete(jobs).where(jobs.c.owner_user_id == USER_ID))
        conn.execute(delete(sql_consoles).where(sql_consoles.c.id == CONSOLE_ID))
        conn.execute(delete(datasources).where(datasources.c.id == DATASOURCE_ID))


def _statement(statement_id: str, *, seq: int = 1, sql: str = "SELECT 1") -> ConsoleStatement:
    now = datetime.now(UTC)
    return ConsoleStatement(
        id=statement_id,
        session_id="session-1",
        console_id=CONSOLE_ID,
        datasource_id=DATASOURCE_ID,
        owner_user_id=USER_ID,
        epoch=1,
        seq=seq,
        client_request_id=f"req-{seq}",
        sql_text=sql,
        sql_hash="sha256:" + uuid4().hex,
        sql_len=len(sql),
        statement_kind=ConsoleStatementKind.SELECT,
        is_write=False,
        state=ConsoleStatementState.ACCEPTED,
        cancel_requested=False,
        result_set_id=None,
        rows_affected=None,
        error_code=None,
        error_summary=None,
        timeout_seconds=600,
        script_id=None,
        script_seq=None,
        resolved_by=None,
        resolved_at=None,
        resolution=None,
        submitted_at=now,
        started_at=None,
        finished_at=None,
    )


def test_console_statement_accepts_canonical_tagged_sha256(engine: Engine) -> None:
    canonical_hash = "sha256:" + ("a" * 64)

    result_set_id = _insert_session_with_statement(engine, sql_hash=canonical_hash)

    with engine.connect() as conn:
        stored = conn.execute(
            select(console_statements.c.sql_hash).where(
                console_statements.c.result_set_id == result_set_id
            )
        ).scalar_one()
    assert stored == canonical_hash


# ── result_sets catalog 平价 ─────────────────────────────────────────────────


def test_register_creates_a_streaming_catalog_row_for_the_console(
    engine: Engine,
    tmp_path: Path,
) -> None:
    store = LocalFsResultStore(tmp_path)
    results = SpoolStatementResults(engine, store)

    spool = results.register(_statement("stmt-1"), page_size=25, max_result_rows=500)

    with engine.connect() as conn:
        row = (
            conn.execute(select(result_sets).where(result_sets.c.id == spool.result_set_id))
            .mappings()
            .one()
        )
    assert row["console_id"] == CONSOLE_ID
    assert row["execution_id"] == "stmt-1"
    assert row["state"] == "streaming"
    assert row["loaded_rows"] == 0


def test_register_evicts_beyond_three_active_result_sets_per_console(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """3/console 淘汰与 job 路径同口径(`core.py` `_evict_console_resultsets`)。"""

    store = LocalFsResultStore(tmp_path)
    results = SpoolStatementResults(engine, store, max_active_resultsets_per_console=3)

    spools: list[StatementSpool] = []
    for index in range(4):
        spool = results.register(
            _statement(f"stmt-{index}", seq=index + 1),
            page_size=100,
            max_result_rows=1000,
        )
        store.append_spool(spool.result_set_id, [Row(values=[index])])
        results.publish_streaming(spool, _statement(f"stmt-{index}"), columns=[])
        spools.append(spool)

    with engine.connect() as conn:
        rows = conn.execute(select(result_sets.c.id, result_sets.c.state)).mappings().all()
    states: dict[str, str] = {str(row["id"]): str(row["state"]) for row in rows}
    active = [spool.result_set_id for spool in spools if states[spool.result_set_id] != "closed"]
    assert len(active) == 3
    # 最早的那个被淘汰,刚注册的一定还在 —— 跑着的语句不会把自己淘汰掉。
    assert states[spools[0].result_set_id] == "closed"
    assert spools[3].result_set_id in active
    assert store.spool_exists(spools[0].result_set_id) is False
    assert store.spool_exists(spools[3].result_set_id) is True


def test_finalize_stores_the_mirrored_timings_and_execution_metadata(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """progress 的镜像字段是从这里读出去的(job 路径读 `jobs.result_ref`)。"""

    store = LocalFsResultStore(tmp_path)
    results = SpoolStatementResults(engine, store)
    statement = _statement("stmt-1")
    spool = results.register(statement, page_size=25, max_result_rows=500)
    results.set_columns(spool, [Column(name="id", type=ColumnType.INTEGER)])
    results.append(spool, [Row(values=[1]), Row(values=[2])])

    results.finalize(
        spool,
        statement,
        columns=[Column(name="id", type=ColumnType.INTEGER)],
        metrics=StatementMetrics(
            execute_started_at=datetime(2026, 8, 22, 9, tzinfo=UTC),
            rows_read=3,
            rows_returned=2,
            fetch_ms=34,
            spool_ms=5,
            db_type="mysql",
            output_limit_applied=True,
        ),
    )

    with engine.connect() as conn:
        row = (
            conn.execute(select(result_sets).where(result_sets.c.id == spool.result_set_id))
            .mappings()
            .one()
        )
    assert row["state"] == "complete"
    assert row["loaded_rows"] == 2
    # 截断了就不报 total_rows —— 报一个"看起来完整"的总数就是撒谎。
    assert row["total_rows"] is None
    execution = row["storage_ref"]["metadata"]["execution"]
    assert execution["rows_read"] == 3
    assert execution["rows_returned"] == 2
    assert execution["max_rows"] == 500
    assert execution["page_size"] == 25
    assert row["storage_ref"]["metadata"]["timings"]["spool_ms"] == 5


# ── R3:worker spool GC 与活跃会话 ───────────────────────────────────────────


def test_active_console_result_sets_lists_only_live_sessions(engine: Engine) -> None:
    live = _insert_session_with_statement(engine, state=ConsoleSessionState.EXECUTING)
    closed = _insert_session_with_statement(engine, state=ConsoleSessionState.CLOSED)

    ids = PostgresActiveConsoleResultSets(engine).active_result_set_ids()

    assert live in ids
    assert closed not in ids


def test_worker_gc_does_not_reclaim_an_active_session_result_set(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """★ 评审修订 R3 的端到端形态(设计 §10-A5 验收项)。

    `result_ttl_days=0` 是最狠的一档:任何 mtime 不是"此刻"的结果集都过期。
    活跃会话的结果集必须活下来 —— 用户还在翻页、还能导出;会话关掉之后,
    它就回到普通 TTL 口径,下一轮 GC 照收不误。
    """

    live = _insert_session_with_statement(engine, state=ConsoleSessionState.EXECUTING)
    dead = _insert_session_with_statement(engine, state=ConsoleSessionState.CLOSED)
    store = LocalFsResultStore(tmp_path, result_ttl_days=0)
    for result_set_id in (live, dead):
        store.append_spool(result_set_id, [Row(values=[1])])
        _age_spool(tmp_path, result_set_id)
    runner = _gc_only_runner(engine, store)

    runner.run_once()

    assert store.spool_exists(live) is True
    assert store.fetch_range(live, 0, 10) == [Row(values=[1])]
    assert store.spool_exists(dead) is False


def test_closing_the_session_releases_its_result_set_to_the_collector(
    engine: Engine,
    tmp_path: Path,
) -> None:
    live = _insert_session_with_statement(engine, state=ConsoleSessionState.IDLE)
    store = LocalFsResultStore(tmp_path, result_ttl_days=0)
    store.append_spool(live, [Row(values=[1])])
    _age_spool(tmp_path, live)
    runner = _gc_only_runner(engine, store)
    runner.run_once()
    assert store.spool_exists(live) is True

    with engine.begin() as conn:
        conn.execute(console_sessions.update().values(state=ConsoleSessionState.CLOSED.value))
    _age_spool(tmp_path, live)
    _gc_only_runner(engine, store).run_once()

    assert store.spool_exists(live) is False


# ── SQL 历史合流 ─────────────────────────────────────────────────────────────


def test_sql_history_merges_console_statements_with_job_history(
    engine: Engine,
    tmp_path: Path,
) -> None:
    del tmp_path
    _insert_job(engine, sql="SELECT job_only", sql_hash="sha256:job-only")
    _insert_session_with_statement(
        engine,
        state=ConsoleSessionState.IDLE,
        sql="SELECT session_only",
        sql_hash="sha256:session-only",
    )
    client = _client(engine)

    items = client.get("/api/sql/history", headers=_headers()).json()

    by_hash = {item["sql_hash"]: item for item in items}
    assert by_hash["sha256:job-only"]["source"] == "job"
    assert by_hash["sha256:job-only"]["job_id"]
    assert by_hash["sha256:job-only"]["statement_id"] is None
    session_item = by_hash["sha256:session-only"]
    assert session_item["source"] == "console_statement"
    assert session_item["statement_id"]
    assert session_item["job_id"] is None
    assert session_item["sql"] == "SELECT session_only"
    assert session_item["datasource_name"] is not None


def test_sql_history_dedupes_the_same_sql_across_both_paths(
    engine: Engine,
) -> None:
    """去重口径不变(按 sql_hash):同一条 SQL 只留最近一次,不分路径。"""

    shared = "sha256:shared"
    _insert_job(
        engine,
        sql="SELECT shared",
        sql_hash=shared,
        created_at=datetime.now(UTC) - timedelta(hours=2),
    )
    _insert_session_with_statement(
        engine,
        state=ConsoleSessionState.IDLE,
        sql="SELECT shared",
        sql_hash=shared,
    )
    client = _client(engine)

    items = client.get("/api/sql/history", headers=_headers()).json()

    matching = [item for item in items if item["sql_hash"] == shared]
    assert len(matching) == 1
    # 会话那条更新,合流后留下的是它。
    assert matching[0]["source"] == "console_statement"


def test_sql_history_maps_statement_states_onto_job_status_words(
    engine: Engine,
) -> None:
    """`outcome_unknown` 保守映射到 failed,原值另放 statement_state。"""

    _insert_session_with_statement(
        engine,
        state=ConsoleSessionState.IDLE,
        sql="SELECT unknown",
        sql_hash="sha256:unknown",
        statement_state=ConsoleStatementState.OUTCOME_UNKNOWN,
    )
    client = _client(engine)

    items = client.get("/api/sql/history", headers=_headers()).json()

    item = next(entry for entry in items if entry["sql_hash"] == "sha256:unknown")
    assert item["status"] == JobStatus.FAILED.value
    assert item["statement_state"] == ConsoleStatementState.OUTCOME_UNKNOWN.value


# ── helpers ──────────────────────────────────────────────────────────────────


def _age_spool(root: Path, result_set_id: str) -> None:
    spool_dir = root / "resultsets" / result_set_id
    old = datetime.now(UTC).timestamp() - 10 * 24 * 60 * 60
    os.utime(spool_dir, (old, old))


def _gc_only_runner(engine: Engine, store: LocalFsResultStore) -> WorkerRunner:
    """只为跑 GC 的 runner:队列空,run_once 除了 GC 什么都不做。"""

    return WorkerRunner(
        _EmptyBackend(),
        store,
        _unused_datasource_loader,
        _unused_adapter_factory,
        WorkerRunnerConfig(worker_id="worker-parity", result_gc_interval_seconds=0.001),
        active_console_resultsets=PostgresActiveConsoleResultSets(engine),
    )


class _EmptyBackend:
    def claim_next(self, worker_id: str) -> None:
        del worker_id
        return None

    def reap_stale_running_jobs(
        self,
        heartbeat_timeout_seconds: int,
        *,
        limit: int = 50,
    ) -> object:
        del heartbeat_timeout_seconds, limit
        return None

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"gc-only runner must not touch the backend: {name}")


def _unused_datasource_loader(datasource_id: str) -> Any:
    raise AssertionError(f"gc-only runner must not load datasources: {datasource_id}")


def _unused_adapter_factory(*args: object, **kwargs: object) -> Any:
    del args, kwargs
    raise AssertionError("gc-only runner must not build adapters")


def _insert_session_with_statement(
    engine: Engine,
    *,
    state: ConsoleSessionState,
    sql: str = "SELECT 1",
    sql_hash: str | None = None,
    statement_state: ConsoleStatementState = ConsoleStatementState.SUCCEEDED,
) -> str:
    """建一条会话 + 一条带结果集的语句,返回 result_set_id。"""

    now = datetime.now(UTC)
    session_id = uuid4().hex
    statement_id = uuid4().hex
    result_set_id = uuid4().hex
    with engine.begin() as conn:
        conn.execute(
            insert(result_sets).values(
                id=result_set_id,
                execution_id=statement_id,
                console_id=CONSOLE_ID,
                storage_ref={"backend": "local_fs", "uri": f"resultsets/{result_set_id}"},
                columns=[],
                loaded_rows=1,
                total_rows=1,
                state="complete",
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            insert(console_sessions).values(
                id=session_id,
                console_id=CONSOLE_ID,
                datasource_id=DATASOURCE_ID,
                owner_user_id=USER_ID,
                epoch=1,
                state=state.value,
                broker_boot_id=uuid4().hex,
                db_session_marker="100",
                server_cancel="available",
                autocommit=True,
                created_at=now,
                last_activity_at=now,
            )
        )
        conn.execute(
            insert(console_statements).values(
                id=statement_id,
                session_id=session_id,
                console_id=CONSOLE_ID,
                datasource_id=DATASOURCE_ID,
                owner_user_id=USER_ID,
                epoch=1,
                seq=1,
                client_request_id=uuid4().hex[:16],
                sql_text=sql,
                sql_hash=sql_hash or ("sha256:" + uuid4().hex),
                sql_len=len(sql),
                statement_kind=ConsoleStatementKind.SELECT.value,
                is_write=False,
                state=statement_state.value,
                cancel_requested=False,
                result_set_id=result_set_id,
                timeout_seconds=600,
                submitted_at=now,
                started_at=now,
                finished_at=now,
            )
        )
    return result_set_id


def _insert_job(
    engine: Engine,
    *,
    sql: str,
    sql_hash: str,
    created_at: datetime | None = None,
) -> str:
    job_id = uuid4().hex
    stamp = created_at or datetime.now(UTC)
    with engine.begin() as conn:
        conn.execute(
            insert(jobs).values(
                id=job_id,
                kind=JobKind.SQL_QUERY.value,
                status=JobStatus.SUCCESS.value,
                owner_user_id=USER_ID,
                project_id=PROJECT_ID,
                datasource_ids=[DATASOURCE_ID],
                priority=0,
                timeout_seconds=300,
                resource_profile={},
                audit_id=uuid4().hex,
                payload={"sql": sql, "sql_hash": sql_hash, "datasource_id": DATASOURCE_ID},
                created_at=stamp,
                finished_at=stamp,
            )
        )
    return job_id


class _RateLimiter:
    def check(self, _key: str, *, group: str = "general_read") -> Any:
        del group
        return type("_D", (), {"allowed": True, "retry_after_seconds": 0.0})()


class _Services:
    jwt_secret = JWT_SECRET

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.session_broker = None
        self.rate_limiter = _RateLimiter()

    def access_token_ttl_seconds(self) -> int:
        return 3600

    def license_enforcement_enabled(self) -> bool:
        return False

    def is_token_revoked(self, **_kwargs: object) -> bool:
        return False

    def write_audit(self, **_kwargs: object) -> None:
        return None


def _client(engine: Engine) -> AsgiClient:
    return AsgiClient(create_app(services=cast(ApiServices, _Services(engine))))


def _headers() -> dict[str, str]:
    token = create_access_token(user_id=USER_ID, role="admin", secret=JWT_SECRET)
    return {"Authorization": f"Bearer {token}"}
