from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from sqlalchemy import RowMapping, delete, insert, select, text, update
from sqlalchemy.engine import Connection, Engine

from app.db.models import job_events, jobs
from app.domain.job import Job, JobStatus
from app.domain.resource import ResourceProfile
from app.domain.result import ResultRef
from app.infrastructure.jobbackend.protocol import JobBackend

T = TypeVar("T")


class JobBackendError(RuntimeError):
    """PostgresJobBackend 基础异常。"""


@dataclass(frozen=True)
class ReapReport:
    requeued: int = 0
    failed: int = 0


class PostgresJobBackend(JobBackend):
    """PostgreSQL-backed JobBackend using FOR UPDATE SKIP LOCKED."""

    def __init__(
        self,
        bind: Engine | Connection,
        *,
        worker_id: str | None = None,
        job_default_max_retries: int = 0,
    ) -> None:
        if job_default_max_retries < 0:
            raise ValueError("job_default_max_retries must be non-negative")
        self._bind = bind
        self._worker_id = worker_id
        self._job_default_max_retries = job_default_max_retries

    def enqueue(self, job: Job) -> None:
        def op(conn: Connection) -> None:
            conn.execute(
                insert(jobs).values(
                    id=job.id,
                    kind=job.kind.value,
                    status=JobStatus.PENDING.value,
                    owner_user_id=job.owner_user_id,
                    project_id=job.project_id,
                    datasource_ids=job.datasource_ids,
                    priority=job.priority,
                    available_at=job.available_at,
                    timeout_seconds=job.timeout_seconds,
                    resource_profile=job.resource_profile.model_dump(),
                    result_ref=_result_ref_to_json(job.result_ref),
                    audit_id=job.audit_id,
                    worker_id=None,
                    last_heartbeat=None,
                    started_at=None,
                    finished_at=None,
                    cancel_requested=job.cancel_requested,
                    cancel_reason=job.cancel_reason,
                    error=job.error,
                    retry_count=job.retry_count,
                    payload=job.payload,
                    parent_workflow_run_id=job.parent_workflow_run_id,
                )
            )
            self._write_event(conn, job.id, "enqueued", None)

        self._write(op)

    def claim_next(self, worker_id: str) -> Job | None:
        claim_sql = text(
            """
            UPDATE jobs
            SET status = 'running',
                worker_id = :worker_id,
                started_at = now(),
                last_heartbeat = now()
            WHERE id = (
                SELECT id
                FROM jobs
                WHERE status = 'pending'
                  AND available_at <= now()
                ORDER BY priority DESC, created_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING *
            """
        )

        def op(conn: Connection) -> Job | None:
            row = conn.execute(claim_sql, {"worker_id": worker_id}).mappings().one_or_none()
            if row is None:
                return None
            job = _job_from_row(row)
            self._write_event(conn, job.id, "claimed", f"worker={worker_id}")
            return job

        return self._write(op)

    def complete(self, job_id: str, result_ref: ResultRef) -> None:
        worker_id = self._require_complete_fail_worker_id()

        def op(conn: Connection) -> None:
            result = conn.execute(
                update(jobs)
                .where(jobs.c.id == job_id)
                .where(jobs.c.worker_id == worker_id)
                .where(jobs.c.status == JobStatus.RUNNING.value)
                .values(
                    status=JobStatus.SUCCESS.value,
                    result_ref=_result_ref_to_json(result_ref),
                    finished_at=text("now()"),
                    error=None,
                )
            )
            if result.rowcount:
                self._write_event(conn, job_id, "completed", None)

        self._write(op)

    def fail(self, job_id: str, error: str) -> None:
        worker_id = self._require_complete_fail_worker_id()

        def op(conn: Connection) -> None:
            result = conn.execute(
                update(jobs)
                .where(jobs.c.id == job_id)
                .where(jobs.c.worker_id == worker_id)
                .where(jobs.c.status == JobStatus.RUNNING.value)
                .values(status=JobStatus.FAILED.value, error=error, finished_at=text("now()"))
            )
            if result.rowcount:
                self._write_event(conn, job_id, "failed", error)

        self._write(op)

    def request_cancel(self, job_id: str) -> None:
        def op(conn: Connection) -> None:
            conn.execute(
                update(jobs)
                .where(jobs.c.id == job_id)
                .values(cancel_requested=True, cancel_reason="requested")
            )
            self._write_event(conn, job_id, "cancel_requested", None)

        self._write(op)

    def heartbeat(self, job_id: str, worker_id: str) -> None:
        def op(conn: Connection) -> None:
            conn.execute(
                update(jobs)
                .where(jobs.c.id == job_id)
                .where(jobs.c.worker_id == worker_id)
                .where(jobs.c.status == JobStatus.RUNNING.value)
                .values(last_heartbeat=text("now()"))
            )

        self._write(op)

    def mark_cancelled(self, job_id: str, reason: str = "cancelled") -> None:
        worker_id = self._require_worker_id("mark_cancelled")

        def op(conn: Connection) -> None:
            result = conn.execute(
                update(jobs)
                .where(jobs.c.id == job_id)
                .where(jobs.c.worker_id == worker_id)
                .where(jobs.c.status == JobStatus.RUNNING.value)
                .values(
                    status=JobStatus.CANCELLED.value,
                    cancel_requested=True,
                    cancel_reason=reason,
                    finished_at=text("now()"),
                )
            )
            if result.rowcount:
                self._write_event(conn, job_id, "cancelled", reason)

        self._write(op)

    def is_cancel_requested(self, job_id: str) -> bool:
        job = self.get_job(job_id)
        return bool(job and job.cancel_requested)

    # ── workflow_run 编排(2.4.0 PR-4;ADR-0009)────────────────────────────
    # workflow_run job 以「推进器」方式运行:claim → 推进 DAG 一步 → 让位
    # (requeue 回 pending),不阻塞占用 worker 槽位等子 job(单 worker 串行
    # 模型下阻塞等待会死锁:占住唯一槽位的 run 永远等不到子 job 被消费)。

    def list_jobs_by_parent(self, parent_workflow_run_id: str) -> list[Job]:
        def op(conn: Connection) -> list[Job]:
            rows = (
                conn.execute(
                    select(jobs)
                    .where(jobs.c.parent_workflow_run_id == parent_workflow_run_id)
                    .order_by(jobs.c.created_at.asc(), jobs.c.id.asc())
                )
                .mappings()
                .all()
            )
            return [_job_from_row(row) for row in rows]

        return self._read(op)

    def requeue_workflow_run(self, job_id: str) -> None:
        """workflow_run 推进一步后让位:running → pending(高频,不写事件)。"""
        worker_id = self._require_worker_id("requeue_workflow_run")

        def op(conn: Connection) -> None:
            conn.execute(
                update(jobs)
                .where(jobs.c.id == job_id)
                .where(jobs.c.worker_id == worker_id)
                .where(jobs.c.status == JobStatus.RUNNING.value)
                .values(
                    status=JobStatus.PENDING.value,
                    worker_id=None,
                    started_at=None,
                    last_heartbeat=None,
                    available_at=text("now()"),
                )
            )

        self._write(op)

    def retry_workflow_node(self, job_id: str) -> None:
        """节点子 job 失败后按 RetryPolicy 重排:failed/timeout → pending,retry_count+1。"""

        def op(conn: Connection) -> None:
            result = conn.execute(
                update(jobs)
                .where(jobs.c.id == job_id)
                .where(jobs.c.status.in_([JobStatus.FAILED.value, JobStatus.TIMEOUT.value]))
                .values(
                    status=JobStatus.PENDING.value,
                    worker_id=None,
                    started_at=None,
                    last_heartbeat=None,
                    finished_at=None,
                    error=None,
                    error_code=None,
                    available_at=text("now()"),
                    retry_count=jobs.c.retry_count + 1,
                )
            )
            if result.rowcount:
                self._write_event(conn, job_id, "workflow_node_retry", None)

        self._write(op)

    def cancel_pending_job(self, job_id: str, reason: str = "cancelled") -> None:
        """直接取消尚未被 claim 的 pending job(abort / run 取消传播用)。"""

        def op(conn: Connection) -> None:
            result = conn.execute(
                update(jobs)
                .where(jobs.c.id == job_id)
                .where(jobs.c.status == JobStatus.PENDING.value)
                .values(
                    status=JobStatus.CANCELLED.value,
                    cancel_requested=True,
                    cancel_reason=reason,
                    finished_at=text("now()"),
                )
            )
            if result.rowcount:
                self._write_event(conn, job_id, "cancelled", reason)

        self._write(op)

    def get_job(self, job_id: str) -> Job | None:
        def op(conn: Connection) -> Job | None:
            row = conn.execute(select(jobs).where(jobs.c.id == job_id)).mappings().one_or_none()
            return _job_from_row(row) if row is not None else None

        return self._read(op)

    def reap_stale_running_jobs(
        self,
        heartbeat_timeout_seconds: int,
        *,
        limit: int = 100,
    ) -> ReapReport:
        if heartbeat_timeout_seconds <= 0:
            raise ValueError("heartbeat_timeout_seconds must be positive")
        if limit <= 0:
            raise ValueError("limit must be positive")

        # workflow_run 推进器幂等可续(每步 claim → 计划 → 让位),worker 崩溃
        # 后 requeue 即恢复编排;走默认 fail 路径会永久杀死在途 run 并留下
        # 无人编排的孤儿子 job,因此无条件 requeue、不占重试预算。
        resumed = self._requeue_stale_workflow_runs(heartbeat_timeout_seconds, limit)
        remaining_limit = max(limit - resumed, 0)
        if self._job_default_max_retries > 0 and remaining_limit > 0:
            requeued = self._requeue_stale_jobs(heartbeat_timeout_seconds, remaining_limit)
            remaining_limit = max(remaining_limit - requeued, 0)
        else:
            requeued = 0
        failed = (
            self._fail_stale_jobs(heartbeat_timeout_seconds, remaining_limit)
            if remaining_limit > 0
            else 0
        )
        return ReapReport(requeued=resumed + requeued, failed=failed)

    def clear_all_jobs_for_tests(self) -> None:
        def op(conn: Connection) -> None:
            conn.execute(delete(job_events))
            conn.execute(delete(jobs))

        self._write(op)

    def _requeue_stale_workflow_runs(self, heartbeat_timeout_seconds: int, limit: int) -> int:
        """stale 的 workflow_run 无条件恢复(pending),不计 retry_count。"""
        sql = text(
            """
            WITH stale AS (
                SELECT id
                FROM jobs
                WHERE status = 'running'
                  AND kind = 'workflow_run'
                  AND last_heartbeat < now() - (:timeout_seconds * interval '1 second')
                ORDER BY last_heartbeat ASC
                FOR UPDATE SKIP LOCKED
                LIMIT :limit
            )
            UPDATE jobs AS j
            SET status = 'pending',
                worker_id = NULL,
                started_at = NULL,
                last_heartbeat = NULL
            FROM stale
            WHERE j.id = stale.id
            RETURNING j.id
            """
        )

        def op(conn: Connection) -> int:
            result = conn.execute(
                sql,
                {"timeout_seconds": heartbeat_timeout_seconds, "limit": limit},
            )
            job_ids = [str(row[0]) for row in result.fetchall()]
            for job_id in job_ids:
                self._write_event(conn, job_id, "workflow_run_resumed_after_stale_heartbeat", None)
            return len(job_ids)

        return self._write(op)

    def _requeue_stale_jobs(self, heartbeat_timeout_seconds: int, limit: int) -> int:
        sql = text(
            """
            WITH stale AS (
                SELECT id
                FROM jobs
                WHERE status = 'running'
                  AND kind <> 'workflow_run'
                  AND last_heartbeat < now() - (:timeout_seconds * interval '1 second')
                  AND retry_count < :max_retries
                ORDER BY last_heartbeat ASC
                FOR UPDATE SKIP LOCKED
                LIMIT :limit
            )
            UPDATE jobs AS j
            SET status = 'pending',
                worker_id = NULL,
                started_at = NULL,
                last_heartbeat = NULL,
                retry_count = j.retry_count + 1,
                error = NULL
            FROM stale
            WHERE j.id = stale.id
            RETURNING j.id
            """
        )

        def op(conn: Connection) -> int:
            result = conn.execute(
                sql,
                {
                    "timeout_seconds": heartbeat_timeout_seconds,
                    "max_retries": self._job_default_max_retries,
                    "limit": limit,
                },
            )
            job_ids = [str(row[0]) for row in result.fetchall()]
            for job_id in job_ids:
                self._write_event(conn, job_id, "requeued_after_stale_heartbeat", None)
            return len(job_ids)

        return self._write(op)

    def _fail_stale_jobs(self, heartbeat_timeout_seconds: int, limit: int) -> int:
        sql = text(
            """
            WITH stale AS (
                SELECT id
                FROM jobs
                WHERE status = 'running'
                  AND kind <> 'workflow_run'
                  AND last_heartbeat < now() - (:timeout_seconds * interval '1 second')
                ORDER BY last_heartbeat ASC
                FOR UPDATE SKIP LOCKED
                LIMIT :limit
            )
            UPDATE jobs AS j
            SET status = 'failed',
                error = 'worker heartbeat timed out',
                finished_at = now()
            FROM stale
            WHERE j.id = stale.id
            RETURNING j.id
            """
        )

        def op(conn: Connection) -> int:
            result = conn.execute(
                sql,
                {"timeout_seconds": heartbeat_timeout_seconds, "limit": limit},
            )
            job_ids = [str(row[0]) for row in result.fetchall()]
            for job_id in job_ids:
                self._write_event(conn, job_id, "failed_after_stale_heartbeat", None)
            return len(job_ids)

        return self._write(op)

    def _require_complete_fail_worker_id(self) -> str:
        if self._worker_id is None:
            raise RuntimeError("complete/fail requires worker_id")
        return self._worker_id

    def _require_worker_id(self, operation: str) -> str:
        if self._worker_id is None:
            raise RuntimeError(f"{operation} requires worker_id")
        return self._worker_id

    def _write_event(
        self,
        conn: Connection,
        job_id: str,
        event_type: str,
        message: str | None,
    ) -> None:
        conn.execute(
            insert(job_events).values(job_id=job_id, event_type=event_type, message=message)
        )

    def _read(self, op: Callable[[Connection], T]) -> T:
        if isinstance(self._bind, Engine):
            with self._bind.connect() as conn:
                return op(conn)
        return op(self._bind)

    def _write(self, op: Callable[[Connection], T]) -> T:
        if isinstance(self._bind, Engine):
            with self._bind.begin() as conn:
                return op(conn)
        if self._bind.in_transaction():
            return op(self._bind)
        with self._bind.begin():
            return op(self._bind)


def _job_from_row(row: RowMapping) -> Job:
    result_ref_payload = row["result_ref"]
    return Job(
        id=str(row["id"]),
        kind=row["kind"],
        status=row["status"],
        owner_user_id=str(row["owner_user_id"]),
        project_id=str(row["project_id"]),
        datasource_ids=list(row["datasource_ids"] or []),
        priority=int(row["priority"]),
        available_at=row["available_at"],
        timeout_seconds=int(row["timeout_seconds"]),
        resource_profile=ResourceProfile.model_validate(row["resource_profile"] or {}),
        result_ref=ResultRef.model_validate(result_ref_payload) if result_ref_payload else None,
        audit_id=str(row["audit_id"]),
        worker_id=str(row["worker_id"]) if row["worker_id"] is not None else None,
        last_heartbeat=row["last_heartbeat"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        cancel_requested=bool(row["cancel_requested"]),
        cancel_reason=str(row["cancel_reason"]) if row["cancel_reason"] is not None else None,
        error=str(row["error"]) if row["error"] is not None else None,
        error_code=row["error_code"],
        retry_count=int(row["retry_count"]),
        payload=dict(row["payload"] or {}),
        parent_workflow_run_id=_optional_str(row["parent_workflow_run_id"]),
    )


def _result_ref_to_json(result_ref: ResultRef | None) -> dict[str, Any] | None:
    return result_ref.model_dump() if result_ref is not None else None


def _optional_str(value: object) -> str | None:
    return str(value) if value is not None else None


__all__ = ["JobBackendError", "PostgresJobBackend", "ReapReport"]
