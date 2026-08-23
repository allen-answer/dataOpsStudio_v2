from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete, insert, select, update
from sqlalchemy.engine import Engine

from app.db.models import (
    compare_result_inputs,
    console_sessions,
    console_statements,
    datasources,
    jobs,
    metadata,
    projects,
    result_sets,
    sql_consoles,
    users,
)
from app.domain.compare_result import decode_compare_result_row
from app.domain.compare_result_input import (
    ActorProject,
    JobResultOrigin,
    StatementResultOrigin,
)
from app.domain.job import Job, JobKind, JobStatus
from app.domain.resource import ResourceProfile
from app.domain.schema import Column, ColumnType, Row
from app.infrastructure.compare_result_inputs import CompareResultInputs
from app.infrastructure.jobbackend.postgres import PostgresJobBackend
from app.infrastructure.resultstore.local_fs import LocalFsResultStore
from app.worker import WorkerRunner, WorkerRunnerConfig
from tests.unit.test_worker import _FakeCompareRunCatalog

pytestmark = pytest.mark.integration


def test_statement_and_job_results_capture_to_independent_snapshots(tmp_path: Path) -> None:
    engine = _pg_engine_or_skip()
    metadata.create_all(engine)
    ids = _Ids()
    store = LocalFsResultStore(tmp_path)
    columns = [
        Column(name="id", type=ColumnType.INTEGER),
        Column(name="name", type=ColumnType.STRING),
    ]
    for result_set_id, value in (
        (ids.statement_result_set, "statement"),
        (ids.job_result_set, "job"),
    ):
        store.set_spool_columns(result_set_id, columns)
        store.append_spool(result_set_id, [Row(values=[1, value])])
    _seed(engine, ids, store, columns)
    module = CompareResultInputs(engine, store, ttl_days=7)
    scope = ActorProject(actor_id=ids.user, project_id=ids.project)
    try:
        statement_input = module.capture(
            StatementResultOrigin(ids.statement),
            scope=scope,
        )
        job_input = module.capture(JobResultOrigin(ids.job), scope=scope)

        assert store.delete_spool(ids.statement_result_set) is True
        assert store.delete_spool(ids.job_result_set) is True
        assert [
            row
            for batch in module.open(
                statement_input.id,
                scope=scope,
                batch_size=100,
                retain_until=datetime.now(UTC) + timedelta(minutes=5),
            ).batches
            for row in batch
        ] == [Row(values=[1, "statement"])]
        assert [
            row
            for batch in module.open(
                job_input.id,
                scope=scope,
                batch_size=100,
                retain_until=datetime.now(UTC) + timedelta(minutes=5),
            ).batches
            for row in batch
        ] == [Row(values=[1, "job"])]

        bucket_spools = {
            "only_source": str(uuid4()),
            "only_target": str(uuid4()),
            "diff": str(uuid4()),
            "same": str(uuid4()),
        }
        compare_job = Job(
            id=str(uuid4()),
            kind=JobKind.COMPARE_RUN,
            status=JobStatus.PENDING,
            owner_user_id=ids.user,
            project_id=ids.project,
            priority=0,
            timeout_seconds=300,
            resource_profile=ResourceProfile(),
            audit_id=str(uuid4()),
            payload={
                "run_id": str(uuid4()),
                "task_id": str(uuid4()),
                "source_id": None,
                "target_id": None,
                "source_ref": {
                    "kind": "result_snapshot",
                    "input_id": statement_input.id,
                },
                "target_ref": {"kind": "result_snapshot", "input_id": job_input.id},
                "columns": [column.model_dump(mode="json") for column in columns],
                "compare_rules": {"key_columns": ["id"]},
                "run_limits": {"recursive_checksum": False},
                "bucket_result_set_ids": bucket_spools,
            },
        )
        expired_before_claim = datetime.now(UTC)
        with engine.begin() as conn:
            conn.execute(
                update(compare_result_inputs)
                .where(compare_result_inputs.c.id == statement_input.id)
                .values(expires_at=expired_before_claim - timedelta(seconds=1))
            )
        backend = PostgresJobBackend(engine, worker_id="worker-result-snapshot")
        backend.enqueue(compare_job)
        compare_catalog = _FakeCompareRunCatalog()

        def fail_dependency(*args: object, **kwargs: object) -> Any:
            del args, kwargs
            raise AssertionError("result snapshot compare must not load a datasource or adapter")

        runner = WorkerRunner(
            backend,
            store,
            cast(Any, fail_dependency),
            cast(Any, fail_dependency),
            WorkerRunnerConfig(worker_id="worker-result-snapshot"),
            compare_run_catalog=compare_catalog,
            compare_result_inputs=module,
        )

        assert runner.run_once() is True
        with engine.connect() as conn:
            compare_job_row = (
                conn.execute(select(jobs).where(jobs.c.id == compare_job.id)).mappings().one()
            )
            renewed_expiry = conn.execute(
                select(compare_result_inputs.c.expires_at).where(
                    compare_result_inputs.c.id == statement_input.id
                )
            ).scalar_one()
        assert str(compare_job_row["status"]) == "success"
        assert renewed_expiry > expired_before_claim
        bucket_counts = compare_catalog.completed[0]["bucket_counts"]
        assert isinstance(bucket_counts, dict)
        assert bucket_counts["diff"] == 1
        diff_rows = store.fetch_range(bucket_spools["diff"], 0, 10)
        assert decode_compare_result_row(diff_rows[0])["cells"] == [
            {"column": "name", "source": "statement", "target": "job"}
        ]
        with engine.connect() as conn:
            rows = conn.execute(
                select(compare_result_inputs.c.origin_kind).where(
                    compare_result_inputs.c.project_id == ids.project
                )
            ).scalars()
            assert set(rows) == {"statement", "job"}

        cutoff = datetime.now(UTC)
        with engine.begin() as conn:
            conn.execute(
                update(compare_result_inputs)
                .where(compare_result_inputs.c.id == statement_input.id)
                .values(expires_at=cutoff - timedelta(seconds=1))
            )
        barrier = Barrier(2)

        def collect_once() -> int:
            barrier.wait()
            return module.collect_expired(now=cutoff, limit=1).deleted

        with ThreadPoolExecutor(max_workers=2) as pool:
            reports = list(pool.map(lambda _: collect_once(), range(2)))

        assert sum(reports) == 1
        assert store.spool_exists(statement_input.id) is False
        with engine.connect() as conn:
            state = conn.execute(
                select(compare_result_inputs.c.state).where(
                    compare_result_inputs.c.id == statement_input.id
                )
            ).scalar_one()
        assert state == "deleted"
    finally:
        _clear(engine, ids)


class _Ids:
    def __init__(self) -> None:
        self.user = str(uuid4())
        self.project = str(uuid4())
        self.datasource = str(uuid4())
        self.console = str(uuid4())
        self.session = str(uuid4())
        self.statement = str(uuid4())
        self.statement_result_set = str(uuid4())
        self.job = str(uuid4())
        self.job_result_set = str(uuid4())


def _pg_engine_or_skip() -> Engine:
    url = os.environ.get("DATAOPS_TEST_PG_URL") or os.environ.get("DATAOPS_DATABASE_URL")
    if not url:
        pytest.skip("Compare result input PG test requires DATAOPS_TEST_PG_URL")
    return create_engine(url)


def _seed(
    engine: Engine,
    ids: _Ids,
    store: LocalFsResultStore,
    columns: list[Column],
) -> None:
    now = datetime.now(UTC)
    serialized_columns = [column.model_dump(mode="json") for column in columns]
    with engine.begin() as conn:
        conn.execute(
            insert(users).values(
                id=ids.user,
                username=uuid4().hex[:16],
                password_hash="not-a-real-hash",
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            insert(projects).values(
                id=ids.project,
                name=uuid4().hex[:16],
                owner_user_id=ids.user,
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            insert(datasources).values(
                id=ids.datasource,
                project_id=ids.project,
                name=uuid4().hex[:16],
                db_type="mysql",
                host="db.internal",
                port=3306,
                username="dataops",
                database_name="app",
                password_secret_ref="secret-ref",
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            insert(sql_consoles).values(
                id=ids.console,
                owner_user_id=ids.user,
                datasource_id=ids.datasource,
                name="snapshot source",
                sql="SELECT 1",
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            insert(console_sessions).values(
                id=ids.session,
                console_id=ids.console,
                datasource_id=ids.datasource,
                owner_user_id=ids.user,
                epoch=1,
                state="closed",
                broker_boot_id=str(uuid4()),
                created_at=now,
                last_activity_at=now,
                closed_at=now,
            )
        )
        for result_set_id, execution_id, console_id in (
            (ids.statement_result_set, ids.statement, ids.console),
            (ids.job_result_set, ids.job, None),
        ):
            conn.execute(
                insert(result_sets).values(
                    id=result_set_id,
                    execution_id=execution_id,
                    console_id=console_id,
                    storage_ref=store.spool_ref(result_set_id).model_dump(mode="json"),
                    columns=serialized_columns,
                    loaded_rows=1,
                    total_rows=1,
                    state="complete",
                    created_at=now,
                    updated_at=now,
                )
            )
        conn.execute(
            insert(console_statements).values(
                id=ids.statement,
                session_id=ids.session,
                console_id=ids.console,
                datasource_id=ids.datasource,
                owner_user_id=ids.user,
                epoch=1,
                seq=1,
                client_request_id=uuid4().hex,
                sql_text="SELECT 1",
                sql_hash="sha256:" + ("0" * 64),
                sql_len=8,
                statement_kind="select",
                is_write=False,
                state="succeeded",
                result_set_id=ids.statement_result_set,
                timeout_seconds=300,
                submitted_at=now,
                started_at=now,
                finished_at=now,
            )
        )
        conn.execute(
            insert(jobs).values(
                id=ids.job,
                kind="sql_query",
                status="success",
                owner_user_id=ids.user,
                project_id=ids.project,
                datasource_ids=[ids.datasource],
                priority=0,
                timeout_seconds=300,
                resource_profile={},
                audit_id=str(uuid4()),
                payload={"result_set_id": ids.job_result_set},
                created_at=now,
                started_at=now,
                finished_at=now,
            )
        )


def _clear(engine: Engine, ids: _Ids) -> None:
    with engine.begin() as conn:
        conn.execute(
            delete(compare_result_inputs).where(compare_result_inputs.c.project_id == ids.project)
        )
        conn.execute(delete(console_statements).where(console_statements.c.id == ids.statement))
        conn.execute(delete(console_sessions).where(console_sessions.c.id == ids.session))
        conn.execute(
            delete(result_sets).where(
                result_sets.c.id.in_([ids.statement_result_set, ids.job_result_set])
            )
        )
        conn.execute(delete(jobs).where(jobs.c.project_id == ids.project))
        conn.execute(delete(sql_consoles).where(sql_consoles.c.id == ids.console))
        conn.execute(delete(datasources).where(datasources.c.id == ids.datasource))
        conn.execute(delete(projects).where(projects.c.id == ids.project))
        conn.execute(delete(users).where(users.c.id == ids.user))
