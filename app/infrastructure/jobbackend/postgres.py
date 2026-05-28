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
        self._claimed_worker_ids: dict[str, str] = {}

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
            self._claimed_worker_ids[job.id] = worker_id
            self._write_event(conn, job.id, "claimed", f"worker={worker_id}")
            return job

        return self._write(op)

    def complete(self, job_id: str, result_ref: ResultRef) -> None:
        worker_id = self._worker_id_for(job_id)
        if worker_id is None:
            return

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
                self._claimed_worker_ids.pop(job_id, None)
                self._write_event(conn, job_id, "completed", None)

        self._write(op)

    def fail(self, job_id: str, error: str) -> None:
        worker_id = self._worker_id_for(job_id)
        if worker_id is None:
            return

        def op(conn: Connection) -> None:
            result = conn.execute(
                update(jobs)
                .where(jobs.c.id == job_id)
                .where(jobs.c.worker_id == worker_id)
                .where(jobs.c.status == JobStatus.RUNNING.value)
                .values(status=JobStatus.FAILED.value, error=error, finished_at=text("now()"))
            )
            if result.rowcount:
                self._claimed_worker_ids.pop(job_id, None)
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
        worker_id = self._worker_id_for(job_id)
        if worker_id is None:
            return

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
                self._claimed_worker_ids.pop(job_id, None)
                self._write_event(conn, job_id, "cancelled", reason)

        self._write(op)

    def is_cancel_requested(self, job_id: str) -> bool:
        job = self.get_job(job_id)
        return bool(job and job.cancel_requested)

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

        if self._job_default_max_retries > 0:
            requeued = self._requeue_stale_jobs(heartbeat_timeout_seconds, limit)
            remaining_limit = max(limit - requeued, 0)
        else:
            requeued = 0
            remaining_limit = limit
        failed = (
            self._fail_stale_jobs(heartbeat_timeout_seconds, remaining_limit)
            if remaining_limit > 0
            else 0
        )
        return ReapReport(requeued=requeued, failed=failed)

    def clear_all_jobs_for_tests(self) -> None:
        def op(conn: Connection) -> None:
            conn.execute(delete(job_events))
            conn.execute(delete(jobs))

        self._write(op)

    def _requeue_stale_jobs(self, heartbeat_timeout_seconds: int, limit: int) -> int:
        sql = text(
            """
            WITH stale AS (
                SELECT id
                FROM jobs
                WHERE status = 'running'
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

    def _worker_id_for(self, job_id: str) -> str | None:
        return self._worker_id or self._claimed_worker_ids.get(job_id)

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
        retry_count=int(row["retry_count"]),
        payload=dict(row["payload"] or {}),
        parent_workflow_run_id=_optional_str(row["parent_workflow_run_id"]),
    )


def _result_ref_to_json(result_ref: ResultRef | None) -> dict[str, Any] | None:
    return result_ref.model_dump() if result_ref is not None else None


def _optional_str(value: object) -> str | None:
    return str(value) if value is not None else None


__all__ = ["JobBackendError", "PostgresJobBackend", "ReapReport"]
