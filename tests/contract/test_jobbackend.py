"""JobBackend Protocol 契约测试(契约 §3.1)。

Codex T1 实现 PostgresJobBackend / ThreadPoolJobBackend 后:
1. 实现 jobbackend fixture(返回真实 impl)
2. 删除 @pytest.mark.skip 装饰器
3. 跑测试验证签名和语义匹配契约
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.domain.job import Job, JobKind, JobStatus
from app.domain.resource import ResourceProfile
from app.domain.result import ResultRef
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
    """JobBackend impl —— Codex T1 后改成返回真实实例(PG 或 ThreadPool)。"""
    pytest.skip("Codex T1(infrastructure/jobbackend/)实现后启用")


@pytest.mark.skip(reason="Codex T1 implements JobBackend")
def test_enqueue_then_claim_returns_same_job(jobbackend: JobBackend) -> None:
    job = _make_job()
    jobbackend.enqueue(job)
    claimed = jobbackend.claim_next("worker-1")
    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.status == JobStatus.RUNNING
    assert claimed.worker_id == "worker-1"


@pytest.mark.skip(reason="Codex T1 implements JobBackend")
def test_claim_next_returns_none_when_empty(jobbackend: JobBackend) -> None:
    """空队列 claim_next 返回 None,不阻塞。"""
    assert jobbackend.claim_next("worker-1") is None


@pytest.mark.skip(reason="Codex T1 implements JobBackend")
def test_claim_respects_priority_then_fifo(jobbackend: JobBackend) -> None:
    """优先级降序;同优先级 FIFO(created_at 升序)。"""
    low = _make_job("j_low", priority=0)
    high = _make_job("j_high", priority=10)
    jobbackend.enqueue(low)
    jobbackend.enqueue(high)
    claimed = jobbackend.claim_next("worker-1")
    assert claimed is not None and claimed.id == "j_high"


@pytest.mark.skip(reason="Codex T1 implements JobBackend")
def test_complete_marks_success_and_attaches_result_ref(jobbackend: JobBackend) -> None:
    job = _make_job()
    jobbackend.enqueue(job)
    jobbackend.claim_next("worker-1")
    ref = ResultRef(backend="local_fs", uri="/tmp/results/j_1")
    jobbackend.complete(job.id, ref)
    # 重新抢应该拿不到 j_1
    assert jobbackend.claim_next("worker-2") is None


@pytest.mark.skip(reason="Codex T1 implements JobBackend")
def test_fail_marks_failed_with_error(jobbackend: JobBackend) -> None:
    job = _make_job()
    jobbackend.enqueue(job)
    jobbackend.claim_next("worker-1")
    jobbackend.fail(job.id, "syntax error at line 1")


@pytest.mark.skip(reason="Codex T1 implements JobBackend")
def test_request_cancel_sets_flag(jobbackend: JobBackend) -> None:
    """request_cancel 只设标记;真正取消由 worker 安全点检查执行(软取消)。"""
    job = _make_job()
    jobbackend.enqueue(job)
    jobbackend.claim_next("worker-1")
    jobbackend.request_cancel(job.id)


@pytest.mark.skip(reason="Codex T1 implements JobBackend")
def test_heartbeat_updates_last_heartbeat(jobbackend: JobBackend) -> None:
    job = _make_job()
    jobbackend.enqueue(job)
    jobbackend.claim_next("worker-1")
    _ = datetime.now(UTC)  # 用作时间锚点;Codex 实现后改成真实断言 heartbeat > _
    jobbackend.heartbeat(job.id, "worker-1")
    # 重新查 job(实现细节由 backend 定),验证 last_heartbeat 推进


@pytest.mark.skip(reason="Codex T1 implements JobBackend(★ FOR UPDATE SKIP LOCKED 关键性)")
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
