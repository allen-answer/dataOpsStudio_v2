from __future__ import annotations

import os
import threading
from collections import Counter
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.db.models import metadata
from app.domain.job import Job, JobKind, JobStatus
from app.domain.resource import ResourceProfile
from app.domain.result import ResultRef
from app.infrastructure.jobbackend.postgres import PostgresJobBackend

pytestmark = pytest.mark.integration


def test_pg_queue_concurrent_claims_are_disjoint() -> None:
    engine = _pg_engine_or_skip()
    owner_id, project_id = _prepare_db(engine)
    backend = PostgresJobBackend(engine)
    job_ids = [str(uuid4()) for _ in range(25)]
    for index, job_id in enumerate(job_ids):
        backend.enqueue(_make_job(job_id, owner_id, project_id, priority=index % 3))

    claimed: list[str] = []
    errors: list[Exception] = []
    claimed_lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker(worker_index: int) -> None:
        try:
            local_backend = PostgresJobBackend(engine, worker_id=f"worker-{worker_index}")
            barrier.wait()
            while True:
                job = local_backend.claim_next(f"worker-{worker_index}")
                if job is None:
                    return
                with claimed_lock:
                    claimed.append(job.id)
                local_backend.complete(job.id, ResultRef(backend="local_fs", uri=f"rs/{job.id}"))
        except Exception as exc:
            with claimed_lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert sorted(claimed) == sorted(job_ids)
    counts = Counter(claimed)
    assert all(count == 1 for count in counts.values())


def test_future_high_priority_job_is_skipped_for_lower_priority_due_job() -> None:
    engine = _pg_engine_or_skip()
    owner_id, project_id = _prepare_db(engine)
    backend = PostgresJobBackend(engine)
    future = _make_job(
        str(uuid4()),
        owner_id,
        project_id,
        priority=100,
        available_at=datetime.now(UTC) + timedelta(hours=1),
    )
    due = _make_job(
        str(uuid4()),
        owner_id,
        project_id,
        priority=1,
    )
    backend.enqueue(future)
    backend.enqueue(due)

    claimed = backend.claim_next("worker-due")

    assert claimed is not None
    assert claimed.id == due.id
    assert backend.claim_next("worker-future") is None


def test_future_job_becomes_claimable_exactly_once_after_moving_due() -> None:
    engine = _pg_engine_or_skip()
    owner_id, project_id = _prepare_db(engine)
    backend = PostgresJobBackend(engine)
    job = _make_job(
        str(uuid4()),
        owner_id,
        project_id,
        available_at=datetime.now(UTC) + timedelta(hours=1),
    )
    backend.enqueue(job)
    assert backend.claim_next("worker-before-due") is None
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE jobs SET available_at = now() WHERE id = :job_id"),
            {"job_id": job.id},
        )

    claimed: list[str] = []
    errors: list[Exception] = []
    claimed_lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker(worker_index: int) -> None:
        try:
            local_backend = PostgresJobBackend(engine)
            barrier.wait()
            result = local_backend.claim_next(f"due-worker-{worker_index}")
            if result is not None:
                with claimed_lock:
                    claimed.append(result.id)
        except Exception as exc:
            with claimed_lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert not any(thread.is_alive() for thread in threads)
    assert claimed == [job.id]


def test_future_pending_job_can_be_cancelled_without_being_claimed() -> None:
    engine = _pg_engine_or_skip()
    owner_id, project_id = _prepare_db(engine)
    backend = PostgresJobBackend(engine)
    available_at = datetime.now(UTC) + timedelta(hours=1)
    job = _make_job(
        str(uuid4()),
        owner_id,
        project_id,
        available_at=available_at,
    )
    backend.enqueue(job)

    backend.cancel_pending_job(job.id, "workflow aborted")

    cancelled = backend.get_job(job.id)
    assert cancelled is not None
    assert cancelled.status == JobStatus.CANCELLED
    assert cancelled.available_at == available_at
    assert backend.claim_next("worker-cancelled") is None


def test_reaper_requeues_stale_running_jobs_once_under_concurrency() -> None:
    engine = _pg_engine_or_skip()
    owner_id, project_id = _prepare_db(engine)
    backend = PostgresJobBackend(engine, job_default_max_retries=1)
    job_ids = [str(uuid4()) for _ in range(12)]
    for job_id in job_ids:
        backend.enqueue(_make_job(job_id, owner_id, project_id))
        claimed = backend.claim_next("dead-worker")
        assert claimed is not None

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE jobs
                SET last_heartbeat = now() - interval '10 minutes'
                WHERE id = ANY(:job_ids)
                """
            ),
            {"job_ids": job_ids},
        )

    reports = []
    errors: list[Exception] = []
    reports_lock = threading.Lock()
    barrier = threading.Barrier(5)

    def reaper() -> None:
        try:
            local_backend = PostgresJobBackend(engine, job_default_max_retries=1)
            barrier.wait()
            report = local_backend.reap_stale_running_jobs(heartbeat_timeout_seconds=1, limit=50)
            with reports_lock:
                reports.append(report)
        except Exception as exc:
            with reports_lock:
                errors.append(exc)

    threads = [threading.Thread(target=reaper) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert sum(report.requeued for report in reports) == len(job_ids)
    assert sum(report.failed for report in reports) == 0

    with engine.connect() as conn:
        rows = (
            conn.execute(
                text("SELECT status, retry_count FROM jobs WHERE id = ANY(:job_ids)"),
                {"job_ids": job_ids},
            )
            .mappings()
            .all()
        )

    assert len(rows) == len(job_ids)
    assert {row["status"] for row in rows} == {JobStatus.PENDING.value}
    assert {row["retry_count"] for row in rows} == {1}


def test_reaper_marks_stale_job_failed_when_retry_budget_exhausted() -> None:
    engine = _pg_engine_or_skip()
    owner_id, project_id = _prepare_db(engine)
    backend = PostgresJobBackend(engine, job_default_max_retries=0)
    job = _make_job(str(uuid4()), owner_id, project_id)
    backend.enqueue(job)
    assert backend.claim_next("dead-worker") is not None
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE jobs
                SET last_heartbeat = now() - interval '10 minutes'
                WHERE id = :job_id
                """
            ),
            {"job_id": job.id},
        )

    report = backend.reap_stale_running_jobs(heartbeat_timeout_seconds=1)

    assert report.requeued == 0
    assert report.failed == 1
    updated = backend.get_job(job.id)
    assert updated is not None
    assert updated.status == JobStatus.FAILED


def test_late_complete_after_requeue_is_ignored_by_worker_id_condition() -> None:
    engine = _pg_engine_or_skip()
    owner_id, project_id = _prepare_db(engine)
    dead_worker_backend = PostgresJobBackend(engine, worker_id="dead-worker")
    reaper_backend = PostgresJobBackend(engine, job_default_max_retries=1)
    new_worker_backend = PostgresJobBackend(engine, worker_id="new-worker")
    job = _make_job(str(uuid4()), owner_id, project_id)
    dead_worker_backend.enqueue(job)
    assert dead_worker_backend.claim_next("dead-worker") is not None
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE jobs
                SET last_heartbeat = now() - interval '10 minutes'
                WHERE id = :job_id
                """
            ),
            {"job_id": job.id},
        )

    report = reaper_backend.reap_stale_running_jobs(heartbeat_timeout_seconds=1)

    assert report.requeued == 1
    dead_worker_backend.complete(job.id, ResultRef(backend="local_fs", uri="stale-result"))
    dead_worker_backend.fail(job.id, "stale failure")
    requeued = new_worker_backend.get_job(job.id)
    assert requeued is not None
    assert requeued.status == JobStatus.PENDING
    assert requeued.result_ref is None
    assert requeued.error is None

    claimed = new_worker_backend.claim_next("new-worker")
    assert claimed is not None
    new_worker_backend.complete(job.id, ResultRef(backend="local_fs", uri="fresh-result"))
    completed = new_worker_backend.get_job(job.id)
    assert completed is not None
    assert completed.status == JobStatus.SUCCESS
    assert completed.result_ref == ResultRef(backend="local_fs", uri="fresh-result")


def _pg_engine_or_skip() -> Engine:
    url = os.environ.get("DATAOPS_TEST_PG_URL") or os.environ.get("DATAOPS_DATABASE_URL")
    if not url:
        pytest.skip("Postgres integration tests require DATAOPS_TEST_PG_URL")
    return create_engine(url, pool_size=10, max_overflow=20)


def _prepare_db(engine: Engine) -> tuple[str, str]:
    metadata.create_all(engine)
    owner_id = str(uuid4())
    project_id = str(uuid4())
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM job_events"))
        conn.execute(text("DELETE FROM jobs"))
        conn.execute(
            text(
                """
                INSERT INTO users (id, username, password_hash)
                VALUES (:id, :username, :password_hash)
                """
            ),
            {
                "id": owner_id,
                # R10:不要 f-string 拼前缀;hex 即唯一
                "username": uuid4().hex,
                "password_hash": "not-a-real-hash",
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO projects (id, name, owner_user_id)
                VALUES (:id, :name, :owner_user_id)
                """
            ),
            {"id": project_id, "name": uuid4().hex, "owner_user_id": owner_id},
        )
    return owner_id, project_id


def _make_job(
    job_id: str,
    owner_user_id: str,
    project_id: str,
    *,
    priority: int = 0,
    available_at: datetime | None = None,
) -> Job:
    job = Job(
        id=job_id,
        kind=JobKind.SQL_QUERY,
        status=JobStatus.PENDING,
        owner_user_id=owner_user_id,
        project_id=project_id,
        datasource_ids=["ds_1"],
        priority=priority,
        timeout_seconds=300,
        resource_profile=ResourceProfile(),
        audit_id=str(uuid4()),
        payload={"sql": "SELECT 1"},
    )
    if available_at is None:
        return job
    return job.model_copy(update={"available_at": available_at})
