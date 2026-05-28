from __future__ import annotations

from typing import Protocol

from app.domain.job import Job
from app.domain.result import ResultRef


class JobBackend(Protocol):
    """统一 Job 队列后端(契约 §3.1)。

    2.0.0 实现:
    - PostgresJobBackend(默认,FOR UPDATE SKIP LOCKED 抢任务)
    - ThreadPoolJobBackend(dev / 极简 portable 单进程)

    后置:RedisRqJobBackend(hosted scale-out)。

    所有方法签名 = 契约 §3.1 字面拷贝,实现类必须匹配。要改签名,先改契约。
    """

    def enqueue(self, job: Job) -> None:
        raise NotImplementedError

    def claim_next(self, worker_id: str) -> Job | None:
        """抢下一个 pending job。PG 实现走 FOR UPDATE SKIP LOCKED。"""
        raise NotImplementedError

    def complete(self, job_id: str, result_ref: ResultRef) -> None:
        raise NotImplementedError

    def fail(self, job_id: str, error: str) -> None:
        raise NotImplementedError

    def request_cancel(self, job_id: str) -> None:
        """软取消标记。真实 kill 见 DatabaseAdapter.kill_query(多为软取消)。"""
        raise NotImplementedError

    def heartbeat(self, job_id: str, worker_id: str) -> None:
        """worker 定期更新 jobs.last_heartbeat;超时由调度器扫描标 failed/重排。"""
        raise NotImplementedError
