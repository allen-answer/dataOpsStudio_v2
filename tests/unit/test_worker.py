from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import BinaryIO, Protocol

from app.dbclients.factory import UnsupportedDbTypeError
from app.dbclients.protocol import AdapterConnectionError
from app.domain.compare import CompareHashExecutionMode, CompareHashPlan
from app.domain.compare_result import decode_compare_result_row
from app.domain.datasource import DatasourceConnInfo, DbType, OperationPolicy
from app.domain.job import Job, JobErrorCode, JobKind, JobStatus
from app.domain.plan import PlanNode
from app.domain.resource import ResourceProfile
from app.domain.result import ResultRef
from app.domain.schema import Column, ColumnType, Row
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


def test_worker_runs_sql_explain_to_spool_and_completes() -> None:
    columns = [
        Column(name="id", type=ColumnType.UNKNOWN),
        Column(name="select_type", type=ColumnType.UNKNOWN),
    ]
    job = _make_job(
        kind=JobKind.SQL_EXPLAIN,
        payload={"sql": "SELECT 1", "result_set_id": "rs-1", "console_id": "console-1"},
    )
    backend = _FakeBackend([job])
    result_store = _FakeResultStore()
    catalog = _FakeResultSetCatalog()
    adapter = _FakeAdapter([], explain_rows=[{"id": 1, "select_type": "SIMPLE"}])
    runner = WorkerRunner(
        backend,
        result_store,
        lambda datasource_id: _conn_info(
            datasource_id,
            operation_policy=OperationPolicy(allow_explain=True),
        ),
        _adapter_factory(adapter),
        WorkerRunnerConfig(worker_id="worker-1", sql_spool_batch_size=10),
        catalog,
    )

    assert runner.run_once() is True

    assert result_store.columns_by_result_set == {"rs-1": columns}
    assert result_store.rows_by_result_set == {"rs-1": [Row(values=[1, "SIMPLE"])]}
    assert backend.completed == [("job-1", ResultRef(backend="local_fs", uri="spool/rs-1"))]
    assert catalog.completed[-1]["loaded_rows"] == 1
    assert catalog.completed[-1]["console_id"] == "console-1"


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
    job = _make_job(kind=JobKind.AI_ASSIST_CALL)
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


def test_worker_runs_compare_run_to_four_bucket_spools() -> None:
    job = _make_job(
        kind=JobKind.COMPARE_RUN,
        payload={
            "run_id": "run-1",
            "task_id": "task-1",
            "source_id": "ds-source",
            "target_id": "ds-target",
            "source_ref": {"kind": "table", "schema_name": "app", "table_name": "src"},
            "target_ref": {"kind": "table", "schema_name": "app", "table_name": "tgt"},
            "columns": [
                {"name": "id", "type": "integer"},
                {"name": "name", "type": "string"},
            ],
            "compare_rules": {"key_columns": ["id"]},
            "run_limits": {"recursive_checksum": False, "persist_same_bucket": False},
            "bucket_result_set_ids": {
                "only_source": "rs-only-source",
                "only_target": "rs-only-target",
                "diff": "rs-diff",
                "same": "rs-same",
            },
        },
    )
    backend = _FakeBackend([job])
    result_store = _FakeResultStore()
    compare_catalog = _FakeCompareRunCatalog()
    adapters = [
        _FakeAdapter(
            [
                Row(values=[1, 1, "same"]),
                Row(values=[2, 2, "left"]),
                Row(values=[3, 3, "old"]),
            ],
            bounds=(1, 3),
        ),
        _FakeAdapter(
            [
                Row(values=[1, 1, "same"]),
                Row(values=[3, 3, "new"]),
                Row(values=[4, 4, "right"]),
            ],
            bounds=(1, 4),
        ),
    ]

    def adapter_factory(
        conn_info: DatasourceConnInfo,
        cancel_check: Callable[[], bool],
        column_sink: Callable[[list[Column]], None],
    ) -> _ColumnSinkAdapter:
        del conn_info, cancel_check, column_sink
        return adapters.pop(0)

    runner = WorkerRunner(
        backend,
        result_store,
        lambda datasource_id: _conn_info(datasource_id),
        adapter_factory,
        WorkerRunnerConfig(worker_id="worker-1"),
        compare_run_catalog=compare_catalog,
    )

    assert runner.run_once() is True

    completed_run = compare_catalog.completed[0]
    assert completed_run["run_id"] == "run-1"
    assert completed_run["bucket_counts"] == {
        "only_source": 1,
        "only_target": 1,
        "diff": 1,
        "same": 1,
    }
    assert completed_run["progress"] == {
        "scanned_segments": 0,
        "skipped_segments": 0,
        "skipped_rows": 0,
        "recursed_segments": 0,
        "row_mode_segments": 1,
        "max_depth_seen": 0,
    }
    diff_profile = completed_run["diff_profile"]
    assert isinstance(diff_profile, dict)
    assert diff_profile["summary"]["diff_rows"] == 1
    assert diff_profile["columns"]["name"]["diff_rate"] == 1.0
    assert diff_profile["missing_key_ranges"]["only_source"] == [
        {"start": 2, "end": 2, "count": 1, "span": 1}
    ]
    assert result_store.rows_by_result_set.get("rs-same", []) == []
    assert decode_compare_result_row(result_store.rows_by_result_set["rs-only-source"][0])[
        "source"
    ] == {"id": 2, "name": "left"}
    assert decode_compare_result_row(result_store.rows_by_result_set["rs-only-target"][0])[
        "target"
    ] == {"id": 4, "name": "right"}
    diff_row = decode_compare_result_row(result_store.rows_by_result_set["rs-diff"][0])
    assert diff_row["pk"] == {"id": 3}
    assert diff_row["cells"] == [{"column": "name", "source": "old", "target": "new"}]
    assert backend.completed[0][0] == "job-1"
    result_ref = backend.completed[0][1]
    assert result_ref.backend == "compare_run"
    assert result_ref.uri == "compare/run-1"
    assert result_ref.metadata["bucket_counts"] == completed_run["bucket_counts"]
    assert result_ref.metadata["diff_profile"] == diff_profile


def test_worker_sample_quick_check_reports_zero_defect_upper_bound() -> None:
    job = _make_job(
        kind=JobKind.COMPARE_RUN,
        payload={
            "run_id": "run-sample",
            "task_id": "task-1",
            "source_id": "ds-source",
            "target_id": "ds-target",
            "source_ref": {"kind": "table", "schema_name": "app", "table_name": "src"},
            "target_ref": {"kind": "table", "schema_name": "app", "table_name": "tgt"},
            "columns": [
                {"name": "id", "type": "integer"},
                {"name": "name", "type": "string"},
            ],
            "compare_rules": {"key_columns": ["id"]},
            "run_limits": {
                "sample_quick_check": True,
                "sample_size": 2,
                "sample_confidence": 0.95,
                "persist_same_bucket": False,
            },
            "bucket_result_set_ids": {
                "only_source": "rs-only-source",
                "only_target": "rs-only-target",
                "diff": "rs-diff",
                "same": "rs-same",
            },
        },
    )
    backend = _FakeBackend([job])
    result_store = _FakeResultStore()
    compare_catalog = _FakeCompareRunCatalog()
    adapters = [
        _FakeAdapter(
            [
                Row(values=[1, 1, "same-1"]),
                Row(values=[2, 2, "same-2"]),
                Row(values=[3, 3, "same-3"]),
            ],
            bounds=(1, 3),
        ),
        _FakeAdapter(
            [
                Row(values=[1, 1, "same-1"]),
                Row(values=[2, 2, "same-2"]),
                Row(values=[3, 3, "same-3"]),
            ],
            bounds=(1, 3),
        ),
    ]

    runner = WorkerRunner(
        backend,
        result_store,
        lambda datasource_id: _conn_info(datasource_id),
        lambda conn_info, cancel_check, column_sink: adapters.pop(0),
        WorkerRunnerConfig(worker_id="worker-1"),
        compare_run_catalog=compare_catalog,
    )

    assert runner.run_once() is True

    sample_result = compare_catalog.completed[0]["sample_result"]
    assert isinstance(sample_result, dict)
    assert sample_result["requested_rows"] == 2
    assert sample_result["sampled_rows"] == 2
    assert sample_result["all_sampled_equal"] is True
    assert sample_result["difference_rate_upper_bound"] is not None
    assert "does not prove full equality" in sample_result["statement"]
    assert result_store.rows_by_result_set.get("rs-same", []) == []


def test_worker_runs_result_export_from_spool_and_completes() -> None:
    job = _make_job(
        kind=JobKind.RESULT_EXPORT,
        payload={
            "source_result_set_id": "rs-source",
            "format": "csv",
            "filename": "result.csv",
            "table_name": "exported_result",
            "source_db_type": "mysql",
        },
    )
    backend = _FakeBackend([job])
    result_store = _FakeResultStore()
    result_store.columns_by_result_set["rs-source"] = [
        Column(name="formula", type=ColumnType.STRING),
        Column(name="n", type=ColumnType.INTEGER),
    ]
    result_store.rows_by_result_set["rs-source"] = [Row(values=["=SUM(A1:A2)", 7])]
    runner = WorkerRunner(
        backend,
        result_store,
        lambda datasource_id: _conn_info(datasource_id),
        _adapter_factory(_FakeAdapter([])),
        WorkerRunnerConfig(worker_id="worker-1"),
    )

    assert runner.run_once() is True

    assert backend.completed == [
        (
            "job-1",
            ResultRef(
                backend="local_fs",
                uri="exports/job-1/result.csv",
                metadata={
                    "format": "csv",
                    "filename": "result.csv",
                    "bytes": len(b"formula,n\n'=SUM(A1:A2),7\n"),
                    "source_result_set_id": "rs-source",
                },
            ),
        )
    ]
    assert result_store.export_artifacts[("job-1", "result.csv")] == (
        b"formula,n\n'=SUM(A1:A2),7\n"
    )
    assert backend.failed == []


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
        self.export_artifacts: dict[tuple[str, str], bytes] = {}
        self.deleted_spools: list[str] = []
        self.gc_calls = 0

    def fetch_range(self, result_set_id: str, offset: int, limit: int) -> list[Row]:
        return list(self.rows_by_result_set.get(result_set_id, [])[offset : offset + limit])

    def append_spool(self, result_set_id: str, rows: list[Row]) -> None:
        self.rows_by_result_set.setdefault(result_set_id, []).extend(rows)

    def put_export_artifact(self, export_id: str, name: str, stream: BinaryIO) -> ResultRef:
        data = stream.read()
        self.export_artifacts[(export_id, name)] = data
        return ResultRef(backend="local_fs", uri=f"exports/{export_id}/{name}")

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

    def fetch_range(self, result_set_id: str, offset: int, limit: int) -> list[Row]:
        del result_set_id, offset, limit
        return []

    def append_spool(self, result_set_id: str, rows: list[Row]) -> None:
        self.row_count_by_result_set[result_set_id] = self.row_count_by_result_set.get(
            result_set_id, 0
        ) + len(rows)

    def set_spool_columns(self, result_set_id: str, columns: list[Column]) -> None:
        self.columns_by_result_set[result_set_id] = list(columns)

    def put_export_artifact(self, export_id: str, name: str, stream: BinaryIO) -> ResultRef:
        del stream
        return ResultRef(backend="local_fs", uri=f"exports/{export_id}/{name}")

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
        bounds: tuple[int, int] | None = None,
        test_connection_ok: bool = True,
        connection_error: str | None = None,
        columns: list[Column] | None = None,
        emit_columns_twice: bool = False,
        explain_rows: list[dict[str, object]] | None = None,
    ) -> None:
        self._rows = rows
        self._bounds = bounds
        self._test_connection_ok = test_connection_ok
        self.last_connection_error = connection_error
        self._columns = columns or [Column(name="n", type=ColumnType.UNKNOWN)]
        self._emit_columns_twice = emit_columns_twice
        self._explain_rows = explain_rows or [{"operation": "EXPLAIN"}]
        self._column_sink: Callable[[list[Column]], None] | None = None
        self.test_connection_calls = 0
        self.execute_select_calls = 0
        self.explain_calls = 0
        self.last_server_version = "MySQL 8.0.test"

    def with_column_sink(self, column_sink: Callable[[list[Column]], None]) -> _FakeAdapter:
        self._column_sink = column_sink
        return self

    def execute_select(self, sql: str, params: dict[str, object]) -> Iterable[Row]:
        self.execute_select_calls += 1
        if "MIN(" in sql.upper() and self._bounds is not None:
            yield Row(values=[self._bounds[0], self._bounds[1]])
            return
        if self._column_sink is not None:
            self._column_sink(self._columns)
            if self._emit_columns_twice:
                self._column_sink(self._columns)
        rows = _filter_fake_compare_rows(sql, self._rows)
        yield from rows

    def explain(self, sql: str) -> PlanNode:
        self.explain_calls += 1
        return PlanNode(operation="EXPLAIN", details={"rows": self._explain_rows})

    def test_connection(self) -> bool:
        self.test_connection_calls += 1
        return self._test_connection_ok

    def build_compare_hash_query(self, request: object) -> CompareHashPlan:
        del request
        return CompareHashPlan(execution_mode=CompareHashExecutionMode.CLIENT_ROW_HASH)


def _filter_fake_compare_rows(sql: str, rows: list[Row]) -> list[Row]:
    filtered = list(rows)
    greater_equal = re.search(r">=\s*(-?\d+)", sql)
    less_than = re.search(r"<\s*(-?\d+)", sql)
    if greater_equal is not None:
        start = int(greater_equal.group(1))
        filtered = [
            row
            for row in filtered
            if row.values and isinstance(row.values[0], int) and row.values[0] >= start
        ]
    if less_than is not None:
        end = int(less_than.group(1))
        filtered = [
            row
            for row in filtered
            if row.values and isinstance(row.values[0], int) and row.values[0] < end
        ]
    if "FETCH FIRST 1 ROWS ONLY" in sql.upper() or " LIMIT 1" in sql.upper():
        filtered = filtered[:1]
    return filtered


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

    def explain(self, sql: str) -> PlanNode:
        return PlanNode(operation="EXPLAIN", details={"rows": [{"rows": self._row_count}]})

    def test_connection(self) -> bool:
        return True

    def build_compare_hash_query(self, request: object) -> CompareHashPlan:
        del request
        return CompareHashPlan(execution_mode=CompareHashExecutionMode.CLIENT_ROW_HASH)


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


def _conn_info(
    datasource_id: str,
    *,
    operation_policy: OperationPolicy | None = None,
) -> DatasourceConnInfo:
    return DatasourceConnInfo(
        host="127.0.0.1",
        port=3306,
        username="dataops",
        database="app",
        password_ref=SecretRef(ref="secret-1", kind=SecretKind.DATASOURCE_PASSWORD),
        db_type=DbType.MYSQL,
        operation_policy=operation_policy or OperationPolicy(),
    )


class _ColumnSinkAdapter(Protocol):
    def execute_select(self, sql: str, params: dict[str, object]) -> Iterable[Row]: ...

    def explain(self, sql: str) -> PlanNode: ...

    def test_connection(self) -> bool: ...

    def build_compare_hash_query(self, request: object) -> CompareHashPlan: ...

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


class _FakeCompareRunCatalog:
    def __init__(self) -> None:
        self.running: list[dict[str, object]] = []
        self.completed: list[dict[str, object]] = []
        self.terminal: list[dict[str, object]] = []

    def write_running(self, *, run_id: str, bucket_spools: dict[str, str]) -> None:
        self.running.append({"run_id": run_id, "bucket_spools": dict(bucket_spools)})

    def write_complete(
        self,
        *,
        run_id: str,
        bucket_counts: dict[str, int],
        progress: dict[str, int],
        diff_profile: dict[str, object],
        sample_result: dict[str, object] | None,
    ) -> None:
        self.completed.append(
            {
                "run_id": run_id,
                "bucket_counts": dict(bucket_counts),
                "progress": dict(progress),
                "diff_profile": dict(diff_profile),
                "sample_result": sample_result,
            }
        )

    def write_terminal(self, *, run_id: str, status: str) -> None:
        self.terminal.append({"run_id": run_id, "status": status})
