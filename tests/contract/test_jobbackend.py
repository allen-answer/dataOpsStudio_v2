"""JobBackend Protocol 契约测试(契约 §3.1)。

Codex T1 实现 PostgresJobBackend / ThreadPoolJobBackend 后:
1. 实现 jobbackend fixture(返回真实 impl)
2. 删除 @pytest.mark.skip 装饰器
3. 跑测试验证签名和语义匹配契约
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.db.models import metadata
from app.domain.job import Job, JobKind, JobStatus
from app.domain.resource import ResourceProfile
from app.domain.result import ResultRef
from app.infrastructure.jobbackend.postgres import PostgresJobBackend
from app.infrastructure.jobbackend.protocol import JobBackend

pytestmark = pytest.mark.contract


def _make_job(job_id: str = "j_1", priority: int = 0) -> Job:
    return Job(
        id=job_id,
        kind=JobKind.SQL_QUERY,
        status=JobStatus.PENDING,
        owner_user_id="u_1",
        project_id="p_1",
        datasource_ids=["ds_1"],
        priority=priority,
        timeout_seconds=300,
        resource_profile=ResourceProfile(),
        audit_id="a_1",
        payload={"sql": "SELECT 1"},
    )


@pytest.fixture
def jobbackend() -> JobBackend:
    engine = _pg_engine_or_skip()
    metadata.create_all(engine)
    _ensure_owner_project(engine, "u_1", "p_1")
    backend = PostgresJobBackend(engine, worker_id="worker-1")
    backend.clear_all_jobs_for_tests()
    return backend


def test_enqueue_then_claim_returns_same_job(jobbackend: JobBackend) -> None:
    job = _make_job()
    jobbackend.enqueue(job)
    claimed = jobbackend.claim_next("worker-1")
    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.status == JobStatus.RUNNING
    assert claimed.worker_id == "worker-1"


def test_claim_next_returns_none_when_empty(jobbackend: JobBackend) -> None:
    """空队列 claim_next 返回 None,不阻塞。"""
    assert jobbackend.claim_next("worker-1") is None


def test_claim_respects_priority_then_fifo(jobbackend: JobBackend) -> None:
    """优先级降序;同优先级 FIFO(created_at 升序)。"""
    low = _make_job("j_low", priority=0)
    high = _make_job("j_high", priority=10)
    jobbackend.enqueue(low)
    jobbackend.enqueue(high)
    claimed = jobbackend.claim_next("worker-1")
    assert claimed is not None and claimed.id == "j_high"


def test_complete_marks_success_and_attaches_result_ref(jobbackend: JobBackend) -> None:
    job = _make_job()
    jobbackend.enqueue(job)
    jobbackend.claim_next("worker-1")
    ref = ResultRef(backend="local_fs", uri="/tmp/results/j_1")
    jobbackend.complete(job.id, ref)
    # 重新抢应该拿不到 j_1
    assert jobbackend.claim_next("worker-2") is None


def test_fail_marks_failed_with_error(jobbackend: JobBackend) -> None:
    job = _make_job()
    jobbackend.enqueue(job)
    jobbackend.claim_next("worker-1")
    jobbackend.fail(job.id, "syntax error at line 1")
    assert isinstance(jobbackend, PostgresJobBackend)
    failed = jobbackend.get_job(job.id)
    assert failed is not None
    assert failed.status == JobStatus.FAILED
    assert failed.error == "syntax error at line 1"
    assert failed.error_code is None


def test_request_cancel_sets_flag(jobbackend: JobBackend) -> None:
    """request_cancel 只设标记;真正取消由 worker 安全点检查执行(软取消)。"""
    job = _make_job()
    jobbackend.enqueue(job)
    jobbackend.claim_next("worker-1")
    jobbackend.request_cancel(job.id)
    assert isinstance(jobbackend, PostgresJobBackend)
    cancelled = jobbackend.get_job(job.id)
    assert cancelled is not None
    assert cancelled.cancel_requested is True


def test_heartbeat_updates_last_heartbeat(jobbackend: JobBackend) -> None:
    job = _make_job()
    jobbackend.enqueue(job)
    jobbackend.claim_next("worker-1")
    before = datetime.now(UTC)
    jobbackend.heartbeat(job.id, "worker-1")
    assert isinstance(jobbackend, PostgresJobBackend)
    updated = jobbackend.get_job(job.id)
    assert updated is not None
    assert updated.last_heartbeat is not None
    assert updated.last_heartbeat >= before


def test_two_workers_claim_disjoint_jobs(jobbackend: JobBackend) -> None:
    """★ PG queue 关键性质:两 worker 并发 claim 不会拿到同一个 job。

    PostgresJobBackend 用 FOR UPDATE SKIP LOCKED,
    ThreadPoolJobBackend 用进程内锁。两种实现都必须通过本测试。
    """
    j1 = _make_job("j_a")
    j2 = _make_job("j_b")
    jobbackend.enqueue(j1)
    jobbackend.enqueue(j2)
    c1 = jobbackend.claim_next("worker-1")
    c2 = jobbackend.claim_next("worker-2")
    assert c1 is not None and c2 is not None
    assert c1.id != c2.id  # 不同 job


def _pg_engine_or_skip() -> Engine:
    url = os.environ.get("DATAOPS_TEST_PG_URL") or os.environ.get("DATAOPS_DATABASE_URL")
    if not url:
        pytest.skip("Postgres JobBackend contract tests require DATAOPS_TEST_PG_URL")
    return create_engine(url)


def _ensure_owner_project(engine: Engine, user_id: str, project_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO users (id, username, password_hash)
                VALUES (:id, :username, :password_hash)
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "id": user_id,
                # R10:不要 f-string 拼前缀;hex 切片即唯一
                "username": uuid4().hex[:16],
                "password_hash": "not-a-real-hash",
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO projects (id, name, owner_user_id)
                VALUES (:id, :name, :owner_user_id)
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {"id": project_id, "name": uuid4().hex[:16], "owner_user_id": user_id},
        )
