from __future__ import annotations

from collections.abc import Callable, Iterable

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
        WorkerRunnerConfig(
            worker_id="worker-1",
            sql_spool_batch_size=1,
            cancel_check_row_interval=1,
        ),
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


def test_cancel_check_throttled_to_every_n_rows() -> None:
    """F3: per-row cancel check is throttled to every 5000 rows."""

    rows = 50_000
    row_interval = 5000
    batch_size = 1000
    flush_per_batch = 1
    expected_max = (rows // row_interval) + (rows // batch_size) * flush_per_batch + 5
    job = _make_job(payload={"sql": "SELECT many", "result_set_id": "rs-many"})
    backend = _FakeBackend([job])
    result_store = _CountingResultStore()

    def adapter_factory(
        conn_info: DatasourceConnInfo,
        cancel_check: Callable[[], bool],
    ) -> _RangeAdapter:
        return _RangeAdapter(rows)

    runner = WorkerRunner(
        backend,
        result_store,
        lambda datasource_id: _conn_info(datasource_id),
        adapter_factory,
        WorkerRunnerConfig(
            worker_id="worker-1",
            sql_spool_batch_size=batch_size,
            cancel_check_row_interval=row_interval,
        ),
    )

    assert runner.run_once() is True

    assert result_store.row_count_by_result_set == {"rs-many": rows}
    assert backend.cancel_check_count <= expected_max, (
        f"F3 节流回归:每行检查重新出现?got {backend.cancel_check_count} > 上限 {expected_max}"
    )
    assert backend.cancel_check_count >= rows // row_interval, (
        f"F3 节流太狠:got {backend.cancel_check_count} < 下限 {rows // row_interval}"
    )
    assert backend.completed == [("job-1", ResultRef(backend="local_fs", uri="spool/rs-many"))]


def test_worker_cancel_still_stops_within_interval() -> None:
    row_count = 20_000
    job = _make_job(payload={"sql": "SELECT many", "result_set_id": "rs-cancel"})
    backend = _FakeBackend([job])
    backend.cancel_after_checks = 2
    result_store = _CountingResultStore()
    adapter_holder: dict[str, _RangeAdapter] = {}

    def adapter_factory(
        conn_info: DatasourceConnInfo,
        cancel_check: Callable[[], bool],
    ) -> _RangeAdapter:
        adapter = _RangeAdapter(row_count)
        adapter_holder["adapter"] = adapter
        return adapter

    runner = WorkerRunner(
        backend,
        result_store,
        lambda datasource_id: _conn_info(datasource_id),
        adapter_factory,
        WorkerRunnerConfig(
            worker_id="worker-1",
            sql_spool_batch_size=10_000,
            cancel_check_row_interval=5000,
        ),
    )

    assert runner.run_once() is True

    assert adapter_holder["adapter"].rows_yielded <= 5000
    assert backend.cancelled == [("job-1", "cancel requested")]
    assert backend.completed == []


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

    @property
    def cancel_check_count(self) -> int:
        return self._cancel_checks


class _FakeResultStore:
    def __init__(self) -> None:
        self.rows_by_result_set: dict[str, list[Row]] = {}

    def append_spool(self, result_set_id: str, rows: list[Row]) -> None:
        self.rows_by_result_set.setdefault(result_set_id, []).extend(rows)

    def spool_ref(self, result_set_id: str) -> ResultRef:
        return ResultRef(backend="local_fs", uri=f"spool/{result_set_id}")


class _CountingResultStore:
    def __init__(self) -> None:
        self.row_count_by_result_set: dict[str, int] = {}

    def append_spool(self, result_set_id: str, rows: list[Row]) -> None:
        self.row_count_by_result_set[result_set_id] = self.row_count_by_result_set.get(
            result_set_id, 0
        ) + len(rows)

    def spool_ref(self, result_set_id: str) -> ResultRef:
        return ResultRef(backend="local_fs", uri=f"spool/{result_set_id}")


class _FakeAdapter:
    def __init__(self, rows: list[Row]) -> None:
        self._rows = rows

    def execute_select(self, sql: str, params: dict[str, object]) -> Iterable[Row]:
        yield from self._rows


class _RangeAdapter:
    def __init__(self, row_count: int) -> None:
        self._row_count = row_count
        self.rows_yielded = 0

    def execute_select(self, sql: str, params: dict[str, object]) -> Iterable[Row]:
        for index in range(self._row_count):
            self.rows_yielded += 1
            yield Row(values=[index])


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
