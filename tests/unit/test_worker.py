from __future__ import annotations

from collections.abc import Iterable

from app.domain.datasource import DatasourceConnInfo, DbType
from app.domain.job import Job, JobKind, JobStatus
from app.domain.resource import ResourceProfile
from app.domain.result import ResultRef
from app.domain.schema import Row
from app.domain.secret import SecretKind, SecretRef
from app.worker import WorkerRunner, WorkerRunnerConfig


def test_worker_runs_sql_query_to_spool_and_completes() -> None:
    job = _make_job(payload={"sql": "SELECT 1", "result_set_id": "rs-1"})
    backend = _FakeBackend([job])
    result_store = _FakeResultStore()
    adapter = _FakeAdapter([Row(values=[1]), Row(values=[2])])
    runner = WorkerRunner(
        backend,
        result_store,
        lambda datasource_id: _conn_info(datasource_id),
        lambda conn_info, cancel_check: adapter,
        WorkerRunnerConfig(worker_id="worker-1", sql_spool_batch_size=1),
    )

    assert runner.run_once() is True

    assert result_store.rows_by_result_set == {"rs-1": [Row(values=[1]), Row(values=[2])]}
    assert backend.completed == [("job-1", ResultRef(backend="local_fs", uri="spool/rs-1"))]
    assert backend.failed == []
    assert backend.cancelled == []


def test_worker_cancel_safe_point_marks_cancelled() -> None:
    job = _make_job(payload={"sql": "SELECT 1", "result_set_id": "rs-1"})
    backend = _FakeBackend([job])
    backend.cancel_after_checks = 4
    result_store = _FakeResultStore()
    adapter = _FakeAdapter([Row(values=[1]), Row(values=[2])])
    runner = WorkerRunner(
        backend,
        result_store,
        lambda datasource_id: _conn_info(datasource_id),
        lambda conn_info, cancel_check: adapter,
        WorkerRunnerConfig(worker_id="worker-1", sql_spool_batch_size=1),
    )

    assert runner.run_once() is True

    assert result_store.rows_by_result_set == {"rs-1": [Row(values=[1])]}
    assert backend.cancelled == [("job-1", "cancel requested")]
    assert backend.completed == []


def test_worker_unsupported_kind_fails_job() -> None:
    job = _make_job(kind=JobKind.EXPORT_EXCEL)
    backend = _FakeBackend([job])
    runner = WorkerRunner(
        backend,
        _FakeResultStore(),
        lambda datasource_id: _conn_info(datasource_id),
        lambda conn_info, cancel_check: _FakeAdapter([]),
        WorkerRunnerConfig(worker_id="worker-1"),
    )

    assert runner.run_once() is True

    assert backend.failed
    assert backend.failed[0][0] == "job-1"
    assert "Unsupported job kind" in backend.failed[0][1]


def test_worker_empty_queue_returns_false() -> None:
    runner = WorkerRunner(
        _FakeBackend([]),
        _FakeResultStore(),
        lambda datasource_id: _conn_info(datasource_id),
        lambda conn_info, cancel_check: _FakeAdapter([]),
        WorkerRunnerConfig(worker_id="worker-1"),
    )

    assert runner.run_once() is False


class _FakeBackend:
    def __init__(self, jobs: list[Job]) -> None:
        self._jobs = jobs
        self.completed: list[tuple[str, ResultRef]] = []
        self.failed: list[tuple[str, str]] = []
        self.cancelled: list[tuple[str, str]] = []
        self.heartbeats: list[tuple[str, str]] = []
        self.cancel_after_checks: int | None = None
        self._cancel_checks = 0

    def claim_next(self, worker_id: str) -> Job | None:
        if not self._jobs:
            return None
        job = self._jobs.pop(0)
        return job.model_copy(update={"status": JobStatus.RUNNING, "worker_id": worker_id})

    def complete(self, job_id: str, result_ref: ResultRef) -> None:
        self.completed.append((job_id, result_ref))

    def fail(self, job_id: str, error: str) -> None:
        self.failed.append((job_id, error))

    def heartbeat(self, job_id: str, worker_id: str) -> None:
        self.heartbeats.append((job_id, worker_id))

    def mark_cancelled(self, job_id: str, reason: str = "cancelled") -> None:
        self.cancelled.append((job_id, reason))

    def is_cancel_requested(self, job_id: str) -> bool:
        self._cancel_checks += 1
        return (
            self.cancel_after_checks is not None and self._cancel_checks >= self.cancel_after_checks
        )

    def reap_stale_running_jobs(
        self,
        heartbeat_timeout_seconds: int,
        *,
        limit: int = 100,
    ) -> object:
        return object()


class _FakeResultStore:
    def __init__(self) -> None:
        self.rows_by_result_set: dict[str, list[Row]] = {}

    def append_spool(self, result_set_id: str, rows: list[Row]) -> None:
        self.rows_by_result_set.setdefault(result_set_id, []).extend(rows)

    def spool_ref(self, result_set_id: str) -> ResultRef:
        return ResultRef(backend="local_fs", uri=f"spool/{result_set_id}")


class _FakeAdapter:
    def __init__(self, rows: list[Row]) -> None:
        self._rows = rows

    def execute_select(self, sql: str, params: dict[str, object]) -> Iterable[Row]:
        yield from self._rows


def _make_job(
    *,
    kind: JobKind = JobKind.SQL_QUERY,
    payload: dict[str, object] | None = None,
) -> Job:
    return Job(
        id="job-1",
        kind=kind,
        status=JobStatus.PENDING,
        owner_user_id="u_1",
        project_id="p_1",
        datasource_ids=["ds_1"],
        priority=0,
        timeout_seconds=300,
        resource_profile=ResourceProfile(),
        audit_id="audit-1",
        payload=payload or {"sql": "SELECT 1"},
    )


def _conn_info(datasource_id: str) -> DatasourceConnInfo:
    return DatasourceConnInfo(
        host="127.0.0.1",
        port=3306,
        username="dataops",
        database="app",
        password_ref=SecretRef(ref="secret-1", kind=SecretKind.DATASOURCE_PASSWORD),
        db_type=DbType.MYSQL,
    )
