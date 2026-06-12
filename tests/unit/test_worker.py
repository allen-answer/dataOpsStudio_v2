from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Protocol

from app.dbclients.protocol import AdapterConnectionError
from app.domain.datasource import DatasourceConnInfo, DbType, OperationPolicy
from app.domain.job import Job, JobErrorCode, JobKind, JobStatus
from app.domain.resource import ResourceProfile
from app.domain.result import ResultRef
from app.domain.schema import Column, ColumnType, Row
from app.domain.secret import SecretKind, SecretRef
from app.worker import UnsupportedDbTypeError, WorkerRunner, WorkerRunnerConfig


def test_worker_runs_sql_query_to_spool_and_completes() -> None:
    job = _make_job(payload={"sql": "SELECT 1", "result_set_id": "rs-1"})
    backend = _FakeBackend([job])
    result_store = _FakeResultStore()
    adapter = _FakeAdapter([Row(values=[1]), Row(values=[2])])
    runner = WorkerRunner(
        backend,
        result_store,
        lambda datasource_id: _conn_info(datasource_id),
        _adapter_factory(adapter),
        WorkerRunnerConfig(worker_id="worker-1", sql_spool_batch_size=1),
    )

    assert runner.run_once() is True

    assert result_store.rows_by_result_set == {"rs-1": [Row(values=[1]), Row(values=[2])]}
    assert backend.completed == [("job-1", ResultRef(backend="local_fs", uri="spool/rs-1"))]
    assert backend.failed == []
    assert backend.cancelled == []


def test_worker_persists_columns_to_spool_and_catalog() -> None:
    columns = [Column(name="r", type=ColumnType.UNKNOWN)]
    job = _make_job(payload={"sql": "SELECT 1 AS r", "result_set_id": "rs-1"})
    backend = _FakeBackend([job])
    result_store = _FakeResultStore()
    catalog = _FakeResultSetCatalog()
    adapter = _FakeAdapter([Row(values=[1]), Row(values=[2])], columns=columns)
    runner = WorkerRunner(
        backend,
        result_store,
        lambda datasource_id: _conn_info(datasource_id),
        _adapter_factory(adapter),
        WorkerRunnerConfig(worker_id="worker-1", sql_spool_batch_size=10),
        catalog,
    )

    assert runner.run_once() is True

    assert result_store.columns_by_result_set == {"rs-1": columns}
    assert catalog.completed == [
        {
            "result_set_id": "rs-1",
            "execution_id": "job-1",
            "storage_ref": ResultRef(backend="local_fs", uri="spool/rs-1"),
            "columns": columns,
            "loaded_rows": 2,
            "truncated": False,
            "console_id": None,
        }
    ]
    assert catalog.streaming[-1]["loaded_rows"] == 2


def test_worker_updates_catalog_while_streaming_batches() -> None:
    job = _make_job(payload={"sql": "SELECT n", "result_set_id": "rs-1", "console_id": "console-1"})
    backend = _FakeBackend([job])
    result_store = _FakeResultStore()
    catalog = _FakeResultSetCatalog()
    adapter = _FakeAdapter([Row(values=[1]), Row(values=[2]), Row(values=[3])])
    runner = WorkerRunner(
        backend,
        result_store,
        lambda datasource_id: _conn_info(datasource_id),
        _adapter_factory(adapter),
        WorkerRunnerConfig(worker_id="worker-1", sql_spool_batch_size=1),
        catalog,
    )

    assert runner.run_once() is True

    assert [entry["loaded_rows"] for entry in catalog.streaming] == [0, 1, 2, 3]
    assert {entry["console_id"] for entry in catalog.streaming} == {"console-1"}
    assert catalog.completed[-1]["loaded_rows"] == 3
    assert catalog.completed[-1]["console_id"] == "console-1"


def test_worker_fails_if_adapter_emits_columns_twice() -> None:
    job = _make_job(payload={"sql": "SELECT 1 AS r", "result_set_id": "rs-1"})
    backend = _FakeBackend([job])
    adapter = _FakeAdapter(
        [Row(values=[1])],
        columns=[Column(name="r", type=ColumnType.UNKNOWN)],
        emit_columns_twice=True,
    )
    runner = WorkerRunner(
        backend,
        _FakeResultStore(),
        lambda datasource_id: _conn_info(datasource_id),
        _adapter_factory(adapter),
        WorkerRunnerConfig(worker_id="worker-1", sql_spool_batch_size=10),
    )

    assert runner.run_once() is True

    assert backend.completed == []
    assert backend.failed == [("job-1", "sql_failed")]


def test_worker_cancel_safe_point_marks_cancelled() -> None:
    job = _make_job(payload={"sql": "SELECT 1", "result_set_id": "rs-1"})
    backend = _FakeBackend([job])
    backend.cancel_after_checks = 4
    result_store = _FakeResultStore()
    catalog = _FakeResultSetCatalog()
    adapter = _FakeAdapter([Row(values=[1]), Row(values=[2])])
    runner = WorkerRunner(
        backend,
        result_store,
        lambda datasource_id: _conn_info(datasource_id),
        _adapter_factory(adapter),
        WorkerRunnerConfig(
            worker_id="worker-1",
            sql_spool_batch_size=1,
            cancel_check_row_interval=1,
        ),
        catalog,
    )

    assert runner.run_once() is True

    assert result_store.rows_by_result_set == {}
    assert result_store.deleted_spools == ["rs-1"]
    assert catalog.terminal == [{"result_set_id": "rs-1", "state": "closed"}]
    assert backend.cancelled == [("job-1", "cancel requested")]
    assert backend.completed == []


def test_worker_unsupported_kind_fails_job() -> None:
    job = _make_job(kind=JobKind.EXPORT_EXCEL)
    backend = _FakeBackend([job])
    runner = WorkerRunner(
        backend,
        _FakeResultStore(),
        lambda datasource_id: _conn_info(datasource_id),
        _adapter_factory(_FakeAdapter([])),
        WorkerRunnerConfig(worker_id="worker-1"),
    )

    assert runner.run_once() is True

    assert backend.failed == [("job-1", "internal")]


def test_worker_unsupported_db_type_fails_with_precise_message() -> None:
    job = _make_job(payload={"sql": "SELECT 1", "result_set_id": "rs-1"})
    backend = _FakeBackend([job])

    def load_oracle_datasource(datasource_id: str) -> DatasourceConnInfo:
        return _conn_info(datasource_id).model_copy(update={"db_type": DbType.ORACLE})

    def adapter_factory(
        conn_info: DatasourceConnInfo,
        cancel_check: Callable[[], bool],
        column_sink: Callable[[list[Column]], None],
    ) -> _ColumnSinkAdapter:
        raise UnsupportedDbTypeError(f"Unsupported datasource db_type: {conn_info.db_type.value}")

    runner = WorkerRunner(
        backend,
        _FakeResultStore(),
        load_oracle_datasource,
        adapter_factory,
        WorkerRunnerConfig(worker_id="worker-1"),
    )

    assert runner.run_once() is True

    assert backend.completed == []
    assert backend.failed == [("job-1", "unsupported_db_type")]


def test_worker_sql_connection_error_gets_connection_failed_code() -> None:
    job = _make_job(payload={"sql": "SELECT 1", "result_set_id": "rs-1"})
    backend = _FakeBackend([job])
    error_writer = _FakeErrorCodeWriter()

    def adapter_factory(
        conn_info: DatasourceConnInfo,
        cancel_check: Callable[[], bool],
        column_sink: Callable[[list[Column]], None],
    ) -> _ColumnSinkAdapter:
        del conn_info, cancel_check, column_sink
        raise AdapterConnectionError("adapter connection failed")

    runner = WorkerRunner(
        backend,
        _FakeResultStore(),
        lambda datasource_id: _conn_info(datasource_id),
        adapter_factory,
        WorkerRunnerConfig(worker_id="worker-1"),
        job_error_code_writer=error_writer,
    )

    assert runner.run_once() is True

    assert backend.completed == []
    assert backend.failed == [("job-1", "connection_failed")]
    assert error_writer.error_codes == [("job-1", JobErrorCode.CONNECTION_FAILED)]


def test_worker_runs_test_connection_job_and_completes() -> None:
    job = _make_job(kind=JobKind.TEST_CONNECTION, payload={"datasource_id": "ds_1"})
    backend = _FakeBackend([job])
    adapter = _FakeAdapter([])
    runner = WorkerRunner(
        backend,
        _FakeResultStore(),
        lambda datasource_id: _conn_info(datasource_id),
        _adapter_factory(adapter),
        WorkerRunnerConfig(worker_id="worker-1"),
    )

    assert runner.run_once() is True

    assert adapter.test_connection_calls == 1
    assert len(backend.completed) == 1
    completed_job_id, result_ref = backend.completed[0]
    assert completed_job_id == "job-1"
    assert result_ref.backend == "connection_test"
    assert result_ref.uri == "test_connection/job-1"
    assert result_ref.metadata["server_version"] == "MySQL 8.0.test"
    assert isinstance(result_ref.metadata["latency_ms"], int)
    assert backend.failed == []


def test_worker_failed_test_connection_fails_job() -> None:
    job = _make_job(kind=JobKind.TEST_CONNECTION, payload={"datasource_id": "ds_1"})
    backend = _FakeBackend([job])
    adapter = _FakeAdapter(
        [],
        test_connection_ok=False,
        connection_error="OperationalError code=1045 message=access denied",
    )
    runner = WorkerRunner(
        backend,
        _FakeResultStore(),
        lambda datasource_id: _conn_info(datasource_id),
        _adapter_factory(adapter),
        WorkerRunnerConfig(worker_id="worker-1"),
    )

    assert runner.run_once() is True

    assert backend.completed == []
    assert backend.failed == [("job-1", "auth_failed")]


def test_worker_policy_denies_select_before_adapter_runs() -> None:
    job = _make_job(payload={"sql": "SELECT 1", "result_set_id": "rs-1"})
    backend = _FakeBackend([job])
    error_writer = _FakeErrorCodeWriter()
    adapter = _FakeAdapter([Row(values=[1])])

    def load_denied_datasource(datasource_id: str) -> DatasourceConnInfo:
        return _conn_info(datasource_id).model_copy(
            update={"operation_policy": OperationPolicy(allow_select=False)}
        )

    runner = WorkerRunner(
        backend,
        _FakeResultStore(),
        load_denied_datasource,
        _adapter_factory(adapter),
        WorkerRunnerConfig(worker_id="worker-1"),
        job_error_code_writer=error_writer,
    )

    assert runner.run_once() is True

    assert adapter.execute_select_calls == 0
    assert backend.completed == []
    assert backend.failed == [("job-1", "permission_denied")]
    assert error_writer.error_codes == [("job-1", JobErrorCode.PERMISSION_DENIED)]


def test_worker_empty_queue_returns_false() -> None:
    runner = WorkerRunner(
        _FakeBackend([]),
        _FakeResultStore(),
        lambda datasource_id: _conn_info(datasource_id),
        _adapter_factory(_FakeAdapter([])),
        WorkerRunnerConfig(worker_id="worker-1"),
    )

    assert runner.run_once() is False


def test_worker_run_once_triggers_result_store_gc_on_interval() -> None:
    result_store = _FakeResultStore()
    runner = WorkerRunner(
        _FakeBackend([]),
        result_store,
        lambda datasource_id: _conn_info(datasource_id),
        _adapter_factory(_FakeAdapter([])),
        WorkerRunnerConfig(worker_id="worker-1"),
    )

    assert runner.run_once() is False
    assert result_store.gc_calls == 1

    assert runner.run_once() is False
    assert result_store.gc_calls == 1


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
        column_sink: Callable[[list[Column]], None],
    ) -> _RangeAdapter:
        return _RangeAdapter(rows).with_column_sink(column_sink)

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
        column_sink: Callable[[list[Column]], None],
    ) -> _RangeAdapter:
        adapter = _RangeAdapter(row_count).with_column_sink(column_sink)
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
        self.columns_by_result_set: dict[str, list[Column]] = {}
        self.deleted_spools: list[str] = []
        self.gc_calls = 0

    def append_spool(self, result_set_id: str, rows: list[Row]) -> None:
        self.rows_by_result_set.setdefault(result_set_id, []).extend(rows)

    def set_spool_columns(self, result_set_id: str, columns: list[Column]) -> None:
        self.columns_by_result_set[result_set_id] = list(columns)

    def get_spool_manifest(self, result_set_id: str) -> dict[str, object]:
        return {
            "columns": [
                column.model_dump() for column in self.columns_by_result_set.get(result_set_id, [])
            ],
            "loaded_rows": len(self.rows_by_result_set.get(result_set_id, [])),
            "truncated": False,
        }

    def spool_ref(self, result_set_id: str) -> ResultRef:
        return ResultRef(backend="local_fs", uri=f"spool/{result_set_id}")

    def delete_spool(self, result_set_id: str) -> bool:
        self.deleted_spools.append(result_set_id)
        existed = (
            result_set_id in self.rows_by_result_set or result_set_id in self.columns_by_result_set
        )
        self.rows_by_result_set.pop(result_set_id, None)
        self.columns_by_result_set.pop(result_set_id, None)
        return existed

    def gc_expired(self) -> int:
        self.gc_calls += 1
        return 0


class _CountingResultStore:
    def __init__(self) -> None:
        self.row_count_by_result_set: dict[str, int] = {}
        self.columns_by_result_set: dict[str, list[Column]] = {}
        self.deleted_spools: list[str] = []
        self.gc_calls = 0

    def append_spool(self, result_set_id: str, rows: list[Row]) -> None:
        self.row_count_by_result_set[result_set_id] = self.row_count_by_result_set.get(
            result_set_id, 0
        ) + len(rows)

    def set_spool_columns(self, result_set_id: str, columns: list[Column]) -> None:
        self.columns_by_result_set[result_set_id] = list(columns)

    def get_spool_manifest(self, result_set_id: str) -> dict[str, object]:
        return {
            "columns": [
                column.model_dump() for column in self.columns_by_result_set.get(result_set_id, [])
            ],
            "loaded_rows": self.row_count_by_result_set.get(result_set_id, 0),
            "truncated": False,
        }

    def spool_ref(self, result_set_id: str) -> ResultRef:
        return ResultRef(backend="local_fs", uri=f"spool/{result_set_id}")

    def delete_spool(self, result_set_id: str) -> bool:
        self.deleted_spools.append(result_set_id)
        existed = (
            result_set_id in self.row_count_by_result_set
            or result_set_id in self.columns_by_result_set
        )
        self.row_count_by_result_set.pop(result_set_id, None)
        self.columns_by_result_set.pop(result_set_id, None)
        return existed

    def gc_expired(self) -> int:
        self.gc_calls += 1
        return 0


class _FakeErrorCodeWriter:
    def __init__(self) -> None:
        self.error_codes: list[tuple[str, JobErrorCode]] = []

    def set_error_code(self, job_id: str, error_code: JobErrorCode) -> None:
        self.error_codes.append((job_id, error_code))


class _FakeAdapter:
    def __init__(
        self,
        rows: list[Row],
        *,
        test_connection_ok: bool = True,
        connection_error: str | None = None,
        columns: list[Column] | None = None,
        emit_columns_twice: bool = False,
    ) -> None:
        self._rows = rows
        self._test_connection_ok = test_connection_ok
        self.last_connection_error = connection_error
        self._columns = columns or [Column(name="n", type=ColumnType.UNKNOWN)]
        self._emit_columns_twice = emit_columns_twice
        self._column_sink: Callable[[list[Column]], None] | None = None
        self.test_connection_calls = 0
        self.execute_select_calls = 0
        self.last_server_version = "MySQL 8.0.test"

    def with_column_sink(self, column_sink: Callable[[list[Column]], None]) -> _FakeAdapter:
        self._column_sink = column_sink
        return self

    def execute_select(self, sql: str, params: dict[str, object]) -> Iterable[Row]:
        self.execute_select_calls += 1
        if self._column_sink is not None:
            self._column_sink(self._columns)
            if self._emit_columns_twice:
                self._column_sink(self._columns)
        yield from self._rows

    def test_connection(self) -> bool:
        self.test_connection_calls += 1
        return self._test_connection_ok


class _RangeAdapter:
    def __init__(self, row_count: int) -> None:
        self._row_count = row_count
        self.rows_yielded = 0
        self._column_sink: Callable[[list[Column]], None] | None = None

    def with_column_sink(self, column_sink: Callable[[list[Column]], None]) -> _RangeAdapter:
        self._column_sink = column_sink
        return self

    def execute_select(self, sql: str, params: dict[str, object]) -> Iterable[Row]:
        if self._column_sink is not None:
            self._column_sink([Column(name="n", type=ColumnType.UNKNOWN)])
        for index in range(self._row_count):
            self.rows_yielded += 1
            yield Row(values=[index])

    def test_connection(self) -> bool:
        return True


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


class _ColumnSinkAdapter(Protocol):
    def execute_select(self, sql: str, params: dict[str, object]) -> Iterable[Row]: ...

    def test_connection(self) -> bool: ...

    def with_column_sink(
        self,
        column_sink: Callable[[list[Column]], None],
    ) -> _ColumnSinkAdapter: ...


def _adapter_factory(
    adapter: _ColumnSinkAdapter,
) -> Callable[
    [DatasourceConnInfo, Callable[[], bool], Callable[[list[Column]], None]],
    _ColumnSinkAdapter,
]:
    def factory(
        conn_info: DatasourceConnInfo,
        cancel_check: Callable[[], bool],
        column_sink: Callable[[list[Column]], None],
    ) -> _ColumnSinkAdapter:
        return adapter.with_column_sink(column_sink)

    return factory


class _FakeResultSetCatalog:
    def __init__(self) -> None:
        self.streaming: list[dict[str, object]] = []
        self.completed: list[dict[str, object]] = []
        self.terminal: list[dict[str, object]] = []

    def write_streaming(
        self,
        *,
        result_set_id: str,
        execution_id: str,
        storage_ref: ResultRef,
        columns: list[Column],
        loaded_rows: int,
        console_id: str | None,
    ) -> None:
        self.streaming.append(
            {
                "result_set_id": result_set_id,
                "execution_id": execution_id,
                "storage_ref": storage_ref,
                "columns": list(columns),
                "loaded_rows": loaded_rows,
                "console_id": console_id,
            }
        )

    def write_complete(
        self,
        *,
        result_set_id: str,
        execution_id: str,
        storage_ref: ResultRef,
        columns: list[Column],
        loaded_rows: int,
        truncated: bool,
        console_id: str | None,
    ) -> None:
        self.completed.append(
            {
                "result_set_id": result_set_id,
                "execution_id": execution_id,
                "storage_ref": storage_ref,
                "columns": list(columns),
                "loaded_rows": loaded_rows,
                "truncated": truncated,
                "console_id": console_id,
            }
        )

    def write_terminal(self, *, result_set_id: str, state: str) -> None:
        self.terminal.append({"result_set_id": result_set_id, "state": state})
