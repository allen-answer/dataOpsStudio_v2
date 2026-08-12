from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy.engine import Connection, Engine

from app.domain.job import Job, JobErrorCode, JobKind, JobStatus
from app.domain.resource import ResourceProfile
from app.domain.result import ResultRef
from app.infrastructure.jobbackend.postgres import PostgresJobBackend


class _FakeResult:
    rowcount = 0

    def __init__(self, row: dict[str, Any] | None = None) -> None:
        self._row = row

    def mappings(self) -> _FakeResult:
        return self

    def one_or_none(self) -> dict[str, Any] | None:
        return self._row


class _RecordingConnection:
    def __init__(self, row: dict[str, Any] | None = None) -> None:
        self.executed: list[tuple[Any, dict[str, Any] | None]] = []
        self._row = row

    def execute(
        self,
        statement: Any,
        parameters: dict[str, Any] | None = None,
    ) -> _FakeResult:
        self.executed.append((statement, parameters))
        return _FakeResult(self._row)

    def in_transaction(self) -> bool:
        return True


def _make_job(*, available_at: datetime) -> Job:
    return Job(
        id="job-1",
        kind=JobKind.SQL_QUERY,
        status=JobStatus.PENDING,
        owner_user_id="user-1",
        project_id="project-1",
        priority=0,
        available_at=available_at,
        timeout_seconds=300,
        resource_profile=ResourceProfile(),
        audit_id="audit-1",
    )


def _job_row(*, available_at: datetime, error_code: str | None = None) -> dict[str, Any]:
    return {
        "id": "job-1",
        "kind": JobKind.SQL_QUERY.value,
        "status": JobStatus.PENDING.value,
        "owner_user_id": "user-1",
        "project_id": "project-1",
        "datasource_ids": [],
        "priority": 0,
        "available_at": available_at,
        "timeout_seconds": 300,
        "resource_profile": {},
        "result_ref": None,
        "audit_id": "audit-1",
        "worker_id": None,
        "last_heartbeat": None,
        "started_at": None,
        "finished_at": None,
        "cancel_requested": False,
        "cancel_reason": None,
        "error": None,
        "error_code": error_code,
        "retry_count": 0,
        "payload": {},
        "parent_workflow_run_id": None,
    }


def test_enqueue_persists_available_at() -> None:
    connection = _RecordingConnection()
    backend = PostgresJobBackend(cast(Connection, connection))
    available_at = datetime.now(UTC) + timedelta(hours=1)

    backend.enqueue(_make_job(available_at=available_at))

    insert_statement = connection.executed[0][0]
    assert insert_statement.compile().params["available_at"] == available_at


def test_claim_next_requires_due_time_and_retains_queue_order_and_locking() -> None:
    connection = _RecordingConnection()
    backend = PostgresJobBackend(cast(Connection, connection))

    assert backend.claim_next("worker-1") is None

    claim_sql = " ".join(str(connection.executed[0][0]).split())
    assert "WHERE status = 'pending' AND available_at <= now()" in claim_sql
    assert "ORDER BY priority DESC, created_at ASC" in claim_sql
    assert "FOR UPDATE SKIP LOCKED" in claim_sql


def test_get_job_maps_available_at_from_database_row() -> None:
    available_at = datetime(2026, 7, 12, 4, 0, tzinfo=UTC)
    connection = _RecordingConnection(_job_row(available_at=available_at))
    backend = PostgresJobBackend(cast(Connection, connection))

    job = backend.get_job("job-1")

    assert job is not None
    assert job.available_at == available_at


def test_get_job_maps_error_code_from_database_row() -> None:
    connection = _RecordingConnection(
        _job_row(
            available_at=datetime.now(UTC),
            error_code=JobErrorCode.SQL_FAILED.value,
        )
    )
    backend = PostgresJobBackend(cast(Connection, connection))

    job = backend.get_job("job-1")

    assert job is not None
    assert job.error_code is JobErrorCode.SQL_FAILED


def test_requeue_workflow_run_resets_available_at_to_database_now() -> None:
    connection = _RecordingConnection()
    backend = PostgresJobBackend(
        cast(Connection, connection),
        worker_id="worker-1",
    )

    backend.requeue_workflow_run("job-1")

    update_sql = " ".join(str(connection.executed[0][0]).split())
    assert "available_at=now()" in update_sql


def test_retry_workflow_node_resets_available_at_to_database_now() -> None:
    connection = _RecordingConnection()
    backend = PostgresJobBackend(cast(Connection, connection))

    backend.retry_workflow_node("job-1")

    update_sql = " ".join(str(connection.executed[0][0]).split())
    assert "available_at=now()" in update_sql


def test_cancel_pending_job_is_independent_of_available_at() -> None:
    connection = _RecordingConnection()
    backend = PostgresJobBackend(cast(Connection, connection))

    backend.cancel_pending_job("job-1", "workflow aborted")

    update_sql = " ".join(str(connection.executed[0][0]).split())
    assert "jobs.status" in update_sql
    assert "available_at" not in update_sql


def test_complete_fail_require_constructor_worker_id() -> None:
    backend = PostgresJobBackend(cast(Engine, object()))

    with pytest.raises(RuntimeError, match="complete/fail requires worker_id"):
        backend.complete("job-1", ResultRef(backend="local_fs", uri="spool/job-1"))

    with pytest.raises(RuntimeError, match="complete/fail requires worker_id"):
        backend.fail("job-1", "boom")


def test_update_result_ref_is_scoped_to_running_job_owned_by_worker() -> None:
    connection = _RecordingConnection()
    backend = PostgresJobBackend(cast(Connection, connection), worker_id="worker-1")
    ref = ResultRef(
        backend="local_fs",
        uri="spool/job-1",
        metadata={"execution": {"rows_returned": 100}},
    )

    backend.update_result_ref("job-1", ref)

    statement = connection.executed[0][0]
    sql = " ".join(str(statement).split())
    params = statement.compile().params
    assert "jobs.worker_id" in sql
    assert "jobs.status" in sql
    assert params["worker_id_1"] == "worker-1"
    assert params["status_1"] == JobStatus.RUNNING.value
    assert params["result_ref"]["metadata"]["execution"]["rows_returned"] == 100
