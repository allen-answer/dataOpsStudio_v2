"""JobBackend Protocol 契约测试(契约 §3.1)。

Codex T1 实现 PostgresJobBackend / ThreadPoolJobBackend 后:
1. 实现 jobbackend fixture(返回真实 impl)
2. 删除 @pytest.mark.skip 装饰器
3. 跑测试验证签名和语义匹配契约
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from app.db.models import metadata
from app.domain.job import Job, JobKind, JobStatus
from app.domain.resource import ResourceProfile
from app.domain.result import ResultRef
from app.infrastructure.jobbackend.postgres import PostgresJobBackend
from app.infrastructure.jobbackend.protocol import JobBackend

pytestmark = pytest.mark.contract


def _make_job(
    job_id: str = "j_1",
    priority: int = 0,
    *,
    available_at: datetime | None = None,
) -> Job:
    job = Job(
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
    if available_at is None:
        return job
    return job.model_copy(update={"available_at": available_at})


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


def test_claim_skips_future_high_priority_job_for_due_job(
    jobbackend: JobBackend,
) -> None:
    future = _make_job(
        "j_future",
        priority=100,
        available_at=datetime.now(UTC) + timedelta(hours=1),
    )
    due = _make_job("j_due", priority=1)
    jobbackend.enqueue(future)
    jobbackend.enqueue(due)

    claimed = jobbackend.claim_next("worker-1")

    assert claimed is not None
    assert claimed.id == "j_due"
    assert jobbackend.claim_next("worker-2") is None


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


def test_workflow_run_requeue_yields_running_back_to_pending(
    jobbackend: JobBackend,
) -> None:
    """workflow_run 推进器让位:running → pending,可被再次 claim。"""
    assert isinstance(jobbackend, PostgresJobBackend)
    job = _make_job("j_wf")
    jobbackend.enqueue(job)
    jobbackend.claim_next("worker-1")
    future = datetime.now(UTC) + timedelta(hours=1)
    with _pg_engine_or_skip().begin() as conn:
        conn.execute(
            text("UPDATE jobs SET available_at = :available_at WHERE id = :job_id"),
            {"available_at": future, "job_id": job.id},
        )
    jobbackend.requeue_workflow_run("j_wf")
    requeued = jobbackend.get_job("j_wf")
    assert requeued is not None
    assert requeued.status == JobStatus.PENDING
    assert requeued.worker_id is None
    # PostgreSQL writes and evaluates due times with its own clock.  Compare
    # against the previous future value, then let claim_next prove it is due.
    assert requeued.available_at < future
    reclaimed = jobbackend.claim_next("worker-1")
    assert reclaimed is not None and reclaimed.id == "j_wf"


def test_retry_workflow_node_requeues_failed_and_increments_retry_count(
    jobbackend: JobBackend,
) -> None:
    assert isinstance(jobbackend, PostgresJobBackend)
    job = _make_job("j_node")
    jobbackend.enqueue(job)
    jobbackend.claim_next("worker-1")
    jobbackend.fail("j_node", "boom")
    future = datetime.now(UTC) + timedelta(hours=1)
    with _pg_engine_or_skip().begin() as conn:
        conn.execute(
            text("UPDATE jobs SET available_at = :available_at WHERE id = :job_id"),
            {"available_at": future, "job_id": job.id},
        )
    jobbackend.retry_workflow_node("j_node")
    retried = jobbackend.get_job("j_node")
    assert retried is not None
    assert retried.status == JobStatus.PENDING
    assert retried.retry_count == 1
    assert retried.error is None
    assert retried.available_at < future
    # 非 failed/timeout 状态不重排(幂等防护)
    reclaimed = jobbackend.claim_next("worker-1")
    assert reclaimed is not None and reclaimed.id == "j_node"
    jobbackend.retry_workflow_node("j_node")
    running = jobbackend.get_job("j_node")
    assert running is not None
    assert running.status == JobStatus.RUNNING
    assert running.retry_count == 1


def test_cancel_pending_job_marks_terminal_without_claim(
    jobbackend: JobBackend,
) -> None:
    assert isinstance(jobbackend, PostgresJobBackend)
    available_at = datetime.now(UTC) + timedelta(hours=1)
    job = _make_job("j_pending", available_at=available_at)
    jobbackend.enqueue(job)
    jobbackend.cancel_pending_job("j_pending", "workflow aborted")
    cancelled = jobbackend.get_job("j_pending")
    assert cancelled is not None
    assert cancelled.status == JobStatus.CANCELLED
    assert cancelled.cancel_reason == "workflow aborted"
    assert cancelled.available_at == available_at
    assert jobbackend.claim_next("worker-1") is None


def test_list_jobs_by_parent_returns_only_children_in_created_order(
    jobbackend: JobBackend,
) -> None:
    assert isinstance(jobbackend, PostgresJobBackend)
    run_job = _make_job("j_run")
    jobbackend.enqueue(run_job)
    child_a = _make_job("j_child_a").model_copy(update={"parent_workflow_run_id": "j_run"})
    child_b = _make_job("j_child_b").model_copy(update={"parent_workflow_run_id": "j_run"})
    unrelated = _make_job("j_other")
    jobbackend.enqueue(child_a)
    jobbackend.enqueue(child_b)
    jobbackend.enqueue(unrelated)
    children = jobbackend.list_jobs_by_parent("j_run")
    assert [child.id for child in children] == ["j_child_a", "j_child_b"]
    assert all(child.parent_workflow_run_id == "j_run" for child in children)


def test_reap_requeues_stale_workflow_run_and_fails_stale_normal_job(
    jobbackend: JobBackend,
) -> None:
    """worker 崩溃后:stale workflow_run 恢复 pending(推进器幂等可续),普通 job 仍 fail。"""
    assert isinstance(jobbackend, PostgresJobBackend)
    engine = _pg_engine_or_skip()
    run_job = _make_job("j_wf_stale").model_copy(update={"kind": JobKind.WORKFLOW_RUN})
    normal_job = _make_job("j_sql_stale")
    jobbackend.enqueue(run_job)
    jobbackend.enqueue(normal_job)
    assert jobbackend.claim_next("worker-1") is not None
    assert jobbackend.claim_next("worker-1") is not None
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE jobs SET last_heartbeat = now() - interval '1 hour' "
                "WHERE id IN ('j_wf_stale', 'j_sql_stale')"
            )
        )

    report = jobbackend.reap_stale_running_jobs(60)

    assert getattr(report, "requeued", None) == 1
    assert getattr(report, "failed", None) == 1
    resumed = jobbackend.get_job("j_wf_stale")
    assert resumed is not None
    assert resumed.status == JobStatus.PENDING
    assert resumed.retry_count == 0  # 恢复不占重试预算
    assert resumed.worker_id is None
    failed = jobbackend.get_job("j_sql_stale")
    assert failed is not None
    assert failed.status == JobStatus.FAILED


def test_duplicate_workflow_node_child_rejected_by_unique_index(
    jobbackend: JobBackend,
) -> None:
    """uq_jobs_workflow_node_per_run:同 run 同节点第二个子 job 被唯一索引拒绝。"""
    assert isinstance(jobbackend, PostgresJobBackend)
    run_job = _make_job("j_run_uq")
    jobbackend.enqueue(run_job)
    child = _make_job("j_uq_child_1").model_copy(
        update={
            "parent_workflow_run_id": "j_run_uq",
            "payload": {"sql": "SELECT 1", "workflow_node_id": "n1"},
        }
    )
    duplicate = _make_job("j_uq_child_2").model_copy(
        update={
            "parent_workflow_run_id": "j_run_uq",
            "payload": {"sql": "SELECT 1", "workflow_node_id": "n1"},
        }
    )
    jobbackend.enqueue(child)
    with pytest.raises(IntegrityError):
        jobbackend.enqueue(duplicate)
    # 不同节点不受影响
    other_node = _make_job("j_uq_child_3").model_copy(
        update={
            "parent_workflow_run_id": "j_run_uq",
            "payload": {"sql": "SELECT 1", "workflow_node_id": "n2"},
        }
    )
    jobbackend.enqueue(other_node)
    children = jobbackend.list_jobs_by_parent("j_run_uq")
    assert [c.id for c in children] == ["j_uq_child_1", "j_uq_child_3"]


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
