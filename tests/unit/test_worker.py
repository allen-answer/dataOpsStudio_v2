from __future__ import annotations

import io
import json
import re
import threading
import time
import zipfile
from collections.abc import Callable, Iterable
from typing import Any, BinaryIO, Protocol

from sqlalchemy.exc import IntegrityError

from app.dbclients.factory import UnsupportedDbTypeError
from app.dbclients.protocol import AdapterConnectionError
from app.domain.compare import CompareHashExecutionMode, CompareHashPlan
from app.domain.compare_result import (
    compare_result_columns,
    decode_compare_result_row,
    encode_compare_result_row,
)
from app.domain.datasource import DatasourceConnInfo, DbType, OperationPolicy
from app.domain.job import Job, JobErrorCode, JobKind, JobStatus
from app.domain.lineage import LineageReport, schema_from_metadata_cache_rows
from app.domain.notify import NotifyResult
from app.domain.plan import PlanNode
from app.domain.resource import ResourceProfile
from app.domain.result import ResultRef
from app.domain.schema import Column, ColumnType, Row
from app.domain.secret import SecretKind, SecretRef
from app.domain.workflow import WorkflowNode
from app.services.notify import WorkflowNotifyService
from app.worker import (
    _EXCEL_MAX_DATA_ROWS_PER_SHEET,
    WorkerRunner,
    WorkerRunnerConfig,
    _build_workflow_child_job,
    _compare_export_row_cap,
)


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


def test_worker_heartbeats_while_waiting_for_first_sql_row() -> None:
    job = _make_job(payload={"sql": "SELECT slow", "result_set_id": "rs-1"})
    backend = _FakeBackend([job])
    result_store = _FakeResultStore()
    started = threading.Event()
    release = threading.Event()

    class _BlockingFirstRowAdapter(_FakeAdapter):
        def execute_select(self, sql: str, params: dict[str, object]) -> Iterable[Row]:
            del sql, params
            self.execute_select_calls += 1
            started.set()
            if not release.wait(2.0):
                raise TimeoutError("test did not release blocking adapter")
            if self._column_sink is not None:
                self._column_sink([Column(name="n", type=ColumnType.UNKNOWN)])
            yield Row(values=[1])

    adapter = _BlockingFirstRowAdapter([])
    runner = WorkerRunner(
        backend,
        result_store,
        lambda datasource_id: _conn_info(datasource_id),
        _adapter_factory(adapter),
        WorkerRunnerConfig(worker_id="worker-1", heartbeat_interval_seconds=0.01),
    )

    worker_thread = threading.Thread(target=runner.run_once)
    worker_thread.start()
    try:
        assert started.wait(1.0)
        deadline = time.monotonic() + 1.0
        while not backend.heartbeats and time.monotonic() < deadline:
            time.sleep(0.01)
        assert backend.heartbeats
        assert result_store.rows_by_result_set == {}
    finally:
        release.set()
        worker_thread.join(timeout=2.0)

    assert not worker_thread.is_alive()
    assert backend.completed == [("job-1", ResultRef(backend="local_fs", uri="spool/rs-1"))]
    assert result_store.rows_by_result_set == {"rs-1": [Row(values=[1])]}


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


def test_worker_runs_compare_result_export_to_four_sheet_workbook() -> None:
    bucket_spools = {
        "only_source": "rs-only-source",
        "only_target": "rs-only-target",
        "diff": "rs-diff",
        "same": "rs-same",
    }
    job = _make_job(
        kind=JobKind.RESULT_EXPORT,
        payload={
            "compare_run_id": "run-1",
            "bucket_result_set_ids": bucket_spools,
            "format": "excel",
            "filename": "run-1.xlsx",
        },
    )
    backend = _FakeBackend([job])
    result_store = _FakeResultStore()
    for result_set_id in bucket_spools.values():
        result_store.columns_by_result_set[result_set_id] = compare_result_columns()
    result_store.rows_by_result_set["rs-only-source"] = [
        encode_compare_result_row(
            pk={"id": 1},
            source={"name": "=cmd"},
            target=None,
            cells=[],
        )
    ]
    runner = WorkerRunner(
        backend,
        result_store,
        lambda datasource_id: _conn_info(datasource_id),
        _adapter_factory(_FakeAdapter([])),
        WorkerRunnerConfig(worker_id="worker-1"),
    )

    assert runner.run_once() is True

    artifact = result_store.export_artifacts[("job-1", "run-1.xlsx")]
    with zipfile.ZipFile(io.BytesIO(artifact)) as workbook:
        workbook_xml = workbook.read("xl/workbook.xml").decode("utf-8")
        sheet1_xml = workbook.read("xl/worksheets/sheet1.xml").decode("utf-8")
        sheet4_xml = workbook.read("xl/worksheets/sheet4.xml").decode("utf-8")
    assert 'name="only_source"' in workbook_xml
    assert 'name="same"' in workbook_xml
    # 解码业务列表头(pk 键 id + 值列 name),非原始 4 列 JSON blob(pk/source/target/cells)。
    assert "<t>id</t>" in sheet1_xml
    assert "<t>name</t>" in sheet1_xml
    assert "pk/source/target/cells" not in sheet1_xml
    # 公式注入防御:导出值仍走 sanitize,危险前缀被加撇号转义为文本。
    assert "'=cmd" in sheet1_xml
    # 空桶 sheet(same)据全 run schema 仍产出正确业务列表头。
    assert "<t>id</t>" in sheet4_xml
    assert "<t>name</t>" in sheet4_xml
    assert backend.completed[0][1].metadata["compare_run_id"] == "run-1"
    assert backend.failed == []


def test_compare_export_row_cap_clamps_to_excel_limit_and_honors_smaller() -> None:
    # export_max_rows 高于 Excel 单 sheet 硬顶 → 被 clamp 到硬顶。
    assert (
        _compare_export_row_cap({"export_max_rows": _EXCEL_MAX_DATA_ROWS_PER_SHEET + 5000})
        == _EXCEL_MAX_DATA_ROWS_PER_SHEET
    )
    # export_max_rows 低于硬顶 → 以 export_max_rows 为准。
    assert _compare_export_row_cap({"export_max_rows": 42}) == 42
    # 缺省 / 非正 / 非整数 → 回退到 Excel 硬顶。
    assert _compare_export_row_cap({}) == _EXCEL_MAX_DATA_ROWS_PER_SHEET
    assert _compare_export_row_cap({"export_max_rows": None}) == _EXCEL_MAX_DATA_ROWS_PER_SHEET
    assert _compare_export_row_cap({"export_max_rows": 0}) == _EXCEL_MAX_DATA_ROWS_PER_SHEET
    assert _compare_export_row_cap({"export_max_rows": -3}) == _EXCEL_MAX_DATA_ROWS_PER_SHEET
    assert _compare_export_row_cap({"export_max_rows": True}) == _EXCEL_MAX_DATA_ROWS_PER_SHEET


def test_worker_compare_result_export_caps_rows_per_sheet() -> None:
    bucket_spools = {
        "only_source": "rs-only-source",
        "only_target": "rs-only-target",
        "diff": "rs-diff",
        "same": "rs-same",
    }
    job = _make_job(
        kind=JobKind.RESULT_EXPORT,
        payload={
            "compare_run_id": "run-1",
            "bucket_result_set_ids": bucket_spools,
            "format": "excel",
            "filename": "run-1.xlsx",
            "export_max_rows": 2,
        },
    )
    backend = _FakeBackend([job])
    result_store = _FakeResultStore()
    for result_set_id in bucket_spools.values():
        result_store.columns_by_result_set[result_set_id] = compare_result_columns()
    # 5 行落在一个 sheet;export_max_rows=2 应只导出前 2 行(+ 表头共 3 行)。
    result_store.rows_by_result_set["rs-only-source"] = [
        encode_compare_result_row(pk={"id": n}, source={"id": n}, target=None, cells=[])
        for n in range(1, 6)
    ]
    runner = WorkerRunner(
        backend,
        result_store,
        lambda datasource_id: _conn_info(datasource_id),
        _adapter_factory(_FakeAdapter([])),
        WorkerRunnerConfig(worker_id="worker-1"),
    )

    assert runner.run_once() is True

    artifact = result_store.export_artifacts[("job-1", "run-1.xlsx")]
    with zipfile.ZipFile(io.BytesIO(artifact)) as workbook:
        sheet1_xml = workbook.read("xl/worksheets/sheet1.xml").decode("utf-8")
    # 表头 r="1" + 2 条数据 r="2"/r="3";第 3 条数据 r="4" 被 cap 掉。
    assert sheet1_xml.count("<row ") == 3
    assert 'r="3"' in sheet1_xml
    assert 'r="4"' not in sheet1_xml
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
        # workflow_run 编排(PR-4)
        self.enqueued: list[Job] = []
        self.children_by_parent: dict[str, list[Job]] = {}
        self.requeued_workflow_runs: list[str] = []
        self.retried_nodes: list[str] = []
        self.cancel_requested_ids: list[str] = []
        self.cancelled_pending: list[tuple[str, str]] = []

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

    def enqueue(self, job: Job) -> None:
        self.enqueued.append(job)
        if job.parent_workflow_run_id is not None:
            self.children_by_parent.setdefault(job.parent_workflow_run_id, []).append(job)

    def request_cancel(self, job_id: str) -> None:
        self.cancel_requested_ids.append(job_id)

    def list_jobs_by_parent(self, parent_workflow_run_id: str) -> list[Job]:
        return list(self.children_by_parent.get(parent_workflow_run_id, []))

    def requeue_workflow_run(self, job_id: str) -> None:
        self.requeued_workflow_runs.append(job_id)

    def retry_workflow_node(self, job_id: str) -> None:
        self.retried_nodes.append(job_id)

    def cancel_pending_job(self, job_id: str, reason: str = "cancelled") -> None:
        self.cancelled_pending.append((job_id, reason))

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
        self.run_artifacts: dict[tuple[str, str], bytes] = {}
        self.downloads: dict[str, bytes] = {}
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

    def put_artifact(self, run_id: str, name: str, stream: BinaryIO) -> ResultRef:
        data = stream.read()
        self.run_artifacts[(run_id, name)] = data
        return ResultRef(backend="local_fs", uri=f"runs/{run_id}/artifacts/{name}")

    def open_download(self, ref: ResultRef) -> io.BytesIO:
        if ref.uri not in self.downloads:
            raise FileNotFoundError(ref.uri)
        return io.BytesIO(self.downloads[ref.uri])

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

    def put_artifact(self, run_id: str, name: str, stream: BinaryIO) -> ResultRef:
        del stream
        return ResultRef(backend="local_fs", uri=f"runs/{run_id}/artifacts/{name}")

    def open_download(self, ref: ResultRef) -> io.BytesIO:
        raise FileNotFoundError(ref.uri)

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
        self.registered: list[dict[str, object]] = []
        self.running: list[dict[str, object]] = []
        self.completed: list[dict[str, object]] = []
        self.terminal: list[dict[str, object]] = []

    def register(
        self,
        *,
        run_id: str,
        job_id: str,
        owner_user_id: str,
        project_id: str,
        task_id: str | None,
        bucket_spools: dict[str, str],
    ) -> None:
        self.registered.append(
            {
                "run_id": run_id,
                "job_id": job_id,
                "owner_user_id": owner_user_id,
                "project_id": project_id,
                "task_id": task_id,
                "bucket_spools": dict(bucket_spools),
            }
        )

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


# ── workflow_run 推进器(2.4.0 PR-4)──────────────────────────────────────────


def _workflow_spec_payload(
    nodes: list[dict[str, object]] | None = None,
    edges: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    return {
        "nodes": nodes
        or [
            {
                "id": "n1",
                "job_kind": "sql_query",
                "payload": {"sql": "SELECT 1", "datasource_id": "ds-9"},
                "timeout_seconds": 60,
            }
        ],
        "edges": edges or [],
    }


def _workflow_run_job(spec: dict[str, object] | None = None) -> Job:
    # run job priority = -1(生产 trigger_workflow_run 一致):子 job = run+1 = 0,
    # 与交互 sql_query(0)同档,不被压制(backlog Workflow minor #3)
    return _make_job(
        kind=JobKind.WORKFLOW_RUN,
        payload={"workflow_id": "wf-1", "spec": spec or _workflow_spec_payload()},
    ).model_copy(update={"priority": -1})


def _workflow_runner(
    backend: _FakeBackend,
    compare_run_catalog: _FakeCompareRunCatalog | None = None,
) -> WorkerRunner:
    return WorkerRunner(
        backend,
        _FakeResultStore(),
        lambda datasource_id: _conn_info(datasource_id),
        _adapter_factory(_FakeAdapter([])),
        WorkerRunnerConfig(worker_id="worker-1", workflow_advance_interval_seconds=0.01),
        compare_run_catalog=compare_run_catalog,
    )


def _workflow_child(
    run_id: str,
    node_id: str,
    status: JobStatus,
    *,
    retry_count: int = 0,
    error: str | None = None,
) -> Job:
    return Job(
        id=f"child-{node_id}",
        kind=JobKind.SQL_QUERY,
        status=status,
        owner_user_id="u_1",
        project_id="p_1",
        datasource_ids=["ds-9"],
        priority=1,
        timeout_seconds=60,
        resource_profile=ResourceProfile(),
        audit_id="audit-child",
        payload={"sql": "SELECT 1", "workflow_node_id": node_id},
        parent_workflow_run_id=run_id,
        retry_count=retry_count,
        error=error,
    )


def test_workflow_run_first_step_enqueues_root_child_and_yields() -> None:
    backend = _FakeBackend([_workflow_run_job()])
    runner = _workflow_runner(backend)

    assert runner.run_once() is True

    # 推进一步:enqueue 根节点子 job 后让位(requeue 回 pending),不 complete/fail
    assert backend.completed == []
    assert backend.failed == []
    assert backend.requeued_workflow_runs == ["job-1"]
    assert len(backend.enqueued) == 1
    child = backend.enqueued[0]
    assert child.kind is JobKind.SQL_QUERY
    assert child.parent_workflow_run_id == "job-1"
    # 子 job = run(-1) + 1 = 0:仍高于 run 推进器(-1),防子 job 饿死;
    # 且与交互 sql_query(priority=0)同档 —— claim 序 = priority DESC,
    # 交互查询不再被排在工作流子 job 之后(backlog Workflow minor #3)
    interactive_sql_query_priority = 0
    assert child.priority == 0
    assert child.priority == interactive_sql_query_priority
    assert child.priority > _workflow_run_job().priority
    assert child.timeout_seconds == 60
    assert child.payload["workflow_node_id"] == "n1"
    assert child.datasource_ids == ["ds-9"]


def test_workflow_run_completes_when_all_nodes_success() -> None:
    run_job = _workflow_run_job()
    backend = _FakeBackend([run_job])
    backend.children_by_parent["job-1"] = [_workflow_child("job-1", "n1", JobStatus.SUCCESS)]
    runner = _workflow_runner(backend)

    assert runner.run_once() is True

    assert backend.requeued_workflow_runs == []
    assert len(backend.completed) == 1
    job_id, result_ref = backend.completed[0]
    assert job_id == "job-1"
    assert result_ref.backend == "workflow_run"
    nodes = result_ref.metadata["nodes"]
    assert isinstance(nodes, dict)
    assert nodes["n1"]["status"] == "success"
    assert nodes["n1"]["job_id"] == "child-n1"


def test_workflow_run_node_failure_fails_run_with_node_error() -> None:
    backend = _FakeBackend([_workflow_run_job()])
    backend.children_by_parent["job-1"] = [
        _workflow_child("job-1", "n1", JobStatus.FAILED, error="sql_failed")
    ]
    runner = _workflow_runner(backend)

    assert runner.run_once() is True

    assert backend.completed == []
    assert len(backend.failed) == 1
    job_id, error = backend.failed[0]
    assert job_id == "job-1"
    assert "n1" in error
    assert "sql_failed" in error


def test_workflow_run_retries_failed_node_within_budget() -> None:
    spec = _workflow_spec_payload(
        nodes=[
            {
                "id": "n1",
                "job_kind": "sql_query",
                "payload": {"sql": "SELECT 1", "datasource_id": "ds-9"},
                "timeout_seconds": 60,
                "retry_policy": {"max_retries": 2, "backoff_seconds": 0},
            }
        ]
    )
    backend = _FakeBackend([_workflow_run_job(spec)])
    backend.children_by_parent["job-1"] = [
        _workflow_child("job-1", "n1", JobStatus.FAILED, retry_count=0, error="boom")
    ]
    runner = _workflow_runner(backend)

    assert runner.run_once() is True

    assert backend.retried_nodes == ["child-n1"]
    assert backend.requeued_workflow_runs == ["job-1"]
    assert backend.failed == []


def test_workflow_run_cancel_propagates_to_children() -> None:
    backend = _FakeBackend([_workflow_run_job()])
    backend.children_by_parent["job-1"] = [_workflow_child("job-1", "n1", JobStatus.RUNNING)]
    backend.cancel_after_checks = 1
    runner = _workflow_runner(backend)

    assert runner.run_once() is True

    assert backend.cancelled == [("job-1", "cancel requested")]
    assert "child-n1" in backend.cancel_requested_ids
    assert ("child-n1", "workflow run cancelled") in backend.cancelled_pending
    assert backend.completed == []
    assert backend.failed == []


def test_workflow_run_uses_frozen_when_variables_from_payload() -> None:
    # 触发时冻结的 when 变量快照优先于按当前时刻计算的 builtin 变量:
    # day="99" 是真实时钟不可能给出的值,节点被 enqueue 即证明用的是快照
    spec = _workflow_spec_payload(
        nodes=[
            {
                "id": "n1",
                "job_kind": "sql_query",
                "payload": {"sql": "SELECT 1", "datasource_id": "ds-9"},
                "timeout_seconds": 60,
                "when": "${day} == '99'",
            }
        ]
    )
    run_job = _make_job(
        kind=JobKind.WORKFLOW_RUN,
        payload={"workflow_id": "wf-1", "spec": spec, "when_variables": {"day": "99"}},
    )
    backend = _FakeBackend([run_job])
    runner = _workflow_runner(backend)

    assert runner.run_once() is True

    assert len(backend.enqueued) == 1
    assert backend.enqueued[0].payload["workflow_node_id"] == "n1"
    assert backend.failed == []


def test_workflow_run_skips_duplicate_child_on_integrity_error() -> None:
    # uq_jobs_workflow_node_per_run 兜底:另一 worker 已 enqueue 同一节点时
    # 本 worker 的 enqueue 撞唯一索引,应跳过继续推进而不是把 run 打挂
    class _DuplicateRejectingBackend(_FakeBackend):
        def enqueue(self, job: Job) -> None:
            raise IntegrityError(
                "INSERT INTO jobs", {}, Exception("duplicate key uq_jobs_workflow_node_per_run")
            )

    backend = _DuplicateRejectingBackend([_workflow_run_job()])
    runner = _workflow_runner(backend)

    assert runner.run_once() is True

    assert backend.enqueued == []
    assert backend.failed == []
    assert backend.requeued_workflow_runs == ["job-1"]


def test_workflow_run_rejects_spec_with_forbidden_kind_at_execution() -> None:
    # R7 执行期再校验:payload 里被篡改的 spec(shell)必须在 worker 侧拒绝
    spec: dict[str, object] = {
        "nodes": [{"id": "n1", "job_kind": "shell", "timeout_seconds": 60}],
        "edges": [],
    }
    backend = _FakeBackend([_workflow_run_job(spec)])
    runner = _workflow_runner(backend)

    assert runner.run_once() is True

    assert backend.enqueued == []
    assert len(backend.failed) == 1


def test_build_workflow_child_job_interpolates_payload_and_keeps_job_kind() -> None:
    # C-7 PR1:入队时刻用冻结变量快照渲染 payload 里的 ${var};job_kind 不被插值触碰(R7)
    node = WorkflowNode(
        id="n1",
        job_kind="compare_run",
        payload={
            "source_ref": {"table_name": "t_${today}", "schema": "public"},
            "target_ref": {"table_name": "t_prev"},
            "datasource_id": "ds-9",
        },
        timeout_seconds=60,
    )
    run_job = _workflow_run_job()
    variables = {"today": "2026-07-07", "year": "2026", "month": "07", "day": "07", "now": "x"}

    child = _build_workflow_child_job(run_job, node, variables)

    # payload ${today} 渲染成快照值;非占位串原样
    assert child.payload["source_ref"]["table_name"] == "t_2026-07-07"
    assert child.payload["source_ref"]["schema"] == "public"
    assert child.payload["target_ref"]["table_name"] == "t_prev"
    assert child.payload["workflow_node_id"] == "n1"
    # R7:job_kind 只按原始 node.job_kind 构造,插值只碰 payload
    assert child.kind is JobKind.COMPARE_RUN


def test_build_workflow_child_job_expands_list_variable_via_sql_in() -> None:
    # C-7 PR3:list 型冻结变量经 ${ids | sql_in} 展开进 compare 节点的 sql 字面量位
    node = WorkflowNode(
        id="n1",
        job_kind="compare_run",
        payload={
            "source_ref": {"kind": "sql", "sql": "SELECT * FROM t WHERE id IN (${ids | sql_in})"},
            "target_ref": {"table_name": "t_prev"},
            "datasource_id": "ds-9",
        },
        timeout_seconds=60,
    )
    run_job = _workflow_run_job()
    variables: dict[str, str | list[str]] = {"ids": ["1", "2", "3"], "today": "2026-07-07"}

    child = _build_workflow_child_job(run_job, node, variables)

    assert child.payload["source_ref"]["sql"] == "SELECT * FROM t WHERE id IN (1, 2, 3)"
    assert child.kind is JobKind.COMPARE_RUN


def _compare_run_node_payload() -> dict[str, object]:
    # compare_run 节点 = 内联配置(以 _execute_compare_run 实际消费字段为准:
    # source_id/target_id + source_ref/target_ref + columns + compare_rules + run_limits;
    # 不含 run_id / bucket_result_set_ids —— 那是入队时按执行铸造的一次性身份)。
    return {
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
    }


def _compare_run_workflow_spec() -> dict[str, object]:
    return _workflow_spec_payload(
        nodes=[
            {
                "id": "n1",
                "job_kind": "compare_run",
                "payload": _compare_run_node_payload(),
                "timeout_seconds": 60,
            }
        ]
    )


def _make_compare_run_child(run_job_id: str, node_id: str, job_id: str, run_id: str) -> Job:
    return Job(
        id=job_id,
        kind=JobKind.COMPARE_RUN,
        status=JobStatus.PENDING,
        owner_user_id="u_1",
        project_id="p_1",
        datasource_ids=["ds-source", "ds-target"],
        priority=0,
        timeout_seconds=60,
        resource_profile=ResourceProfile(),
        audit_id="audit-" + node_id,
        payload={
            "workflow_node_id": node_id,
            "run_id": run_id,
            "bucket_result_set_ids": {
                "only_source": "rs-os",
                "only_target": "rs-ot",
                "diff": "rs-diff",
                "same": "rs-same",
            },
        },
        parent_workflow_run_id=run_job_id,
    )


def test_workflow_compare_run_child_mints_run_id_and_registers() -> None:
    # 核心缺口:workflow 的 compare_run 子 job 入队时必须铸造一次性 run 身份 +
    # bucket result set 占位 + 登记 run_index,否则执行期缺 run_id/bucket_result_set_ids
    # 直接炸(C-7 参数化 compare / C-8 trace_compare 全跑不通)。
    backend = _FakeBackend([_workflow_run_job(_compare_run_workflow_spec())])
    catalog = _FakeCompareRunCatalog()
    runner = _workflow_runner(backend, compare_run_catalog=catalog)

    assert runner.run_once() is True

    assert len(backend.enqueued) == 1
    child = backend.enqueued[0]
    assert child.kind is JobKind.COMPARE_RUN
    run_id = child.payload["run_id"]
    assert isinstance(run_id, str) and run_id
    bucket_spools = child.payload["bucket_result_set_ids"]
    assert isinstance(bucket_spools, dict)
    assert set(bucket_spools) == {"only_source", "only_target", "diff", "same"}
    assert len(set(bucket_spools.values())) == 4  # 四个占位互不相同

    assert len(catalog.registered) == 1
    registered = catalog.registered[0]
    assert registered["run_id"] == run_id
    assert registered["job_id"] == child.id
    assert registered["bucket_spools"] == bucket_spools
    assert registered["task_id"] == "task-1"
    assert registered["owner_user_id"] == "u_1"
    assert registered["project_id"] == "p_1"
    assert backend.failed == []
    assert backend.requeued_workflow_runs == ["job-1"]


def test_workflow_compare_run_child_payload_executes_end_to_end() -> None:
    # workflow 铸出的 compare_run 子 job payload 必须真能被 _execute_compare_run 消费:
    # 修复前缺 run_id / bucket_result_set_ids 必炸。这里取 workflow 铸出的子 job 直接执行。
    plan_backend = _FakeBackend([_workflow_run_job(_compare_run_workflow_spec())])
    _workflow_runner(plan_backend, compare_run_catalog=_FakeCompareRunCatalog()).run_once()
    child = plan_backend.enqueued[0]

    exec_backend = _FakeBackend([child])
    exec_catalog = _FakeCompareRunCatalog()
    result_store = _FakeResultStore()
    adapters = [
        _FakeAdapter(
            [Row(values=[1, 1, "same"]), Row(values=[2, 2, "left"]), Row(values=[3, 3, "old"])],
            bounds=(1, 3),
        ),
        _FakeAdapter(
            [Row(values=[1, 1, "same"]), Row(values=[3, 3, "new"]), Row(values=[4, 4, "right"])],
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
        exec_backend,
        result_store,
        lambda datasource_id: _conn_info(datasource_id),
        adapter_factory,
        WorkerRunnerConfig(worker_id="worker-1"),
        compare_run_catalog=exec_catalog,
    )

    assert runner.run_once() is True

    assert exec_backend.failed == []
    run_id = child.payload["run_id"]
    assert [entry["run_id"] for entry in exec_catalog.running] == [run_id]
    assert [entry["run_id"] for entry in exec_catalog.completed] == [run_id]
    assert exec_backend.completed[0][0] == child.id


def test_workflow_abort_writes_compare_run_terminal_for_pending_child() -> None:
    # 失败路径:workflow abort 取消 pending 的 compare_run 子 job 时,补写 run_index 终态
    # (该子 job 不进执行循环 except 分支,否则 run_index 永远停在 pending)。
    spec = _workflow_spec_payload(
        nodes=[
            {
                "id": "n1",
                "job_kind": "sql_query",
                "payload": {"sql": "SELECT 1", "datasource_id": "ds-9"},
                "timeout_seconds": 60,
            },
            {
                "id": "n2",
                "job_kind": "compare_run",
                "payload": _compare_run_node_payload(),
                "timeout_seconds": 60,
            },
        ]
    )
    backend = _FakeBackend([_workflow_run_job(spec)])
    backend.children_by_parent["job-1"] = [
        _workflow_child("job-1", "n1", JobStatus.FAILED, error="boom"),
        _make_compare_run_child("job-1", "n2", "child-n2", "run-n2"),
    ]
    catalog = _FakeCompareRunCatalog()
    runner = _workflow_runner(backend, compare_run_catalog=catalog)

    assert runner.run_once() is True

    assert len(backend.failed) == 1  # n1 abort → run FAILED
    assert ("child-n2", "workflow aborted") in backend.cancelled_pending
    assert catalog.terminal == [{"run_id": "run-n2", "status": "cancelled"}]


def test_workflow_non_compare_child_gets_no_compare_bookkeeping() -> None:
    # 非 compare_run 节点行为不变:不铸 run_id、不登记 run_index。
    backend = _FakeBackend([_workflow_run_job()])  # 默认 sql_query 节点
    catalog = _FakeCompareRunCatalog()
    runner = _workflow_runner(backend, compare_run_catalog=catalog)

    assert runner.run_once() is True

    child = backend.enqueued[0]
    assert child.kind is JobKind.SQL_QUERY
    assert "run_id" not in child.payload
    assert "bucket_result_set_ids" not in child.payload
    assert catalog.registered == []


def test_workflow_run_node_interpolation_failure_fails_run_via_on_failure() -> None:
    # 插值失败(未解析变量)= 确定性错误:节点走 when 异常同路径 → FAILED → on_failure
    # (默认 abort)→ run FAILED,而不是把 run job 崩掉 / 卡死重试
    spec = _workflow_spec_payload(
        nodes=[
            {
                "id": "n1",
                "job_kind": "sql_query",
                "payload": {"sql": "SELECT '${missing}'", "datasource_id": "ds-9"},
                "timeout_seconds": 60,
            }
        ]
    )
    backend = _FakeBackend([_workflow_run_job(spec)])
    runner = _workflow_runner(backend)

    assert runner.run_once() is True

    # 没有 enqueue 任何子 job(节点在 plan 阶段即判 FAILED),run 直接 FAILED
    assert backend.enqueued == []
    assert len(backend.failed) == 1
    job_id, error = backend.failed[0]
    assert job_id == "job-1"
    assert "n1" in error
    assert "unresolved_param_variable" in error
    # ★ R5:失败信息只含变量名,不含任何取值
    assert "missing" in error


# ── workflow_run 终态通知(C-9 worker 接入)────────────────────────────────────


class _RecordingNotifyService:
    """假通知服务:记录 worker 钩子的 notify 调用(不做 events 过滤——那是真 service 的活)。"""

    def __init__(self) -> None:
        self.calls: list[tuple[object, object, str]] = []

    def notify(self, spec: object, payload: object, run_status: str) -> list[NotifyResult]:
        self.calls.append((spec, payload, run_status))
        return []


class _RaisingNotifyService:
    """通知服务违约抛异常:验证 worker 外层失败隔离(run 终态仍落定)。"""

    def __init__(self) -> None:
        self.calls = 0

    def notify(self, spec: object, payload: object, run_status: str) -> list[NotifyResult]:
        self.calls += 1
        raise RuntimeError("notify boom")


class _RecordingChannel:
    """假渠道:调用 reveal 并记录,断言 reveal 注入 + 明文不外泄。"""

    channel = "webhook"

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    def send(self, target: object, payload: object, *, reveal: object) -> NotifyResult:
        ref = SecretRef(ref=target.url_secret_ref, kind=SecretKind.WEBHOOK_SECRET)  # type: ignore[attr-defined]
        revealed = reveal(ref)  # type: ignore[operator]
        self.sent.append(
            {"target_id": target.id, "payload": payload, "revealed": revealed}  # type: ignore[attr-defined]
        )
        return NotifyResult(channel=self.channel, target_id=target.id, ok=True)  # type: ignore[attr-defined]


def _spec_with_notify(events: list[str], *, channel: str = "webhook") -> dict[str, object]:
    spec = _workflow_spec_payload()
    spec["notifications"] = [
        {"id": "t1", "channel": channel, "url_secret_ref": "ref-1", "events": events}
    ]
    return spec


def test_workflow_run_notify_on_success() -> None:
    backend = _FakeBackend([_workflow_run_job(_spec_with_notify(["success"]))])
    backend.children_by_parent["job-1"] = [_workflow_child("job-1", "n1", JobStatus.SUCCESS)]
    runner = _workflow_runner(backend)
    service = _RecordingNotifyService()
    runner._notify_service = service  # type: ignore[assignment]

    assert runner.run_once() is True

    assert len(backend.completed) == 1
    assert len(service.calls) == 1
    _, payload, run_status = service.calls[0]
    assert run_status == "success"
    assert payload.workflow_id == "wf-1"  # type: ignore[attr-defined]
    assert payload.run_job_id == "job-1"  # type: ignore[attr-defined]
    assert payload.status == "success"  # type: ignore[attr-defined]
    assert payload.error is None  # type: ignore[attr-defined]


def test_workflow_run_notify_on_failure_passes_public_error() -> None:
    backend = _FakeBackend([_workflow_run_job(_spec_with_notify(["failed"]))])
    backend.children_by_parent["job-1"] = [
        _workflow_child("job-1", "n1", JobStatus.FAILED, error="sql_failed")
    ]
    runner = _workflow_runner(backend)
    service = _RecordingNotifyService()
    runner._notify_service = service  # type: ignore[assignment]

    assert runner.run_once() is True

    assert len(backend.failed) == 1
    assert len(service.calls) == 1
    _, payload, run_status = service.calls[0]
    assert run_status == "failed"
    assert payload.status == "failed"  # type: ignore[attr-defined]
    # 失败摘要用 _public_error_message(节点 id + 公开错误串),非敏感
    assert payload.error is not None and "n1" in payload.error  # type: ignore[attr-defined]


def test_workflow_run_notify_on_cancel() -> None:
    backend = _FakeBackend([_workflow_run_job(_spec_with_notify(["all"]))])
    backend.children_by_parent["job-1"] = [_workflow_child("job-1", "n1", JobStatus.RUNNING)]
    backend.cancel_after_checks = 1
    runner = _workflow_runner(backend)
    service = _RecordingNotifyService()
    runner._notify_service = service  # type: ignore[assignment]

    assert runner.run_once() is True

    assert backend.cancelled == [("job-1", "cancel requested")]
    assert len(service.calls) == 1
    assert service.calls[0][2] == "cancelled"


def test_workflow_run_yield_step_does_not_notify() -> None:
    # 让位分支(outcome is None)不是终态,不通知
    backend = _FakeBackend([_workflow_run_job(_spec_with_notify(["all"]))])
    runner = _workflow_runner(backend)
    service = _RecordingNotifyService()
    runner._notify_service = service  # type: ignore[assignment]

    assert runner.run_once() is True

    assert backend.requeued_workflow_runs == ["job-1"]
    assert backend.completed == []
    assert service.calls == []


def test_workflow_run_notify_events_filter_skips_unsubscribed() -> None:
    # 真 service 做 events 过滤:target 只订 failed,success run 不发
    backend = _FakeBackend([_workflow_run_job(_spec_with_notify(["failed"]))])
    backend.children_by_parent["job-1"] = [_workflow_child("job-1", "n1", JobStatus.SUCCESS)]
    runner = _workflow_runner(backend)
    channel = _RecordingChannel()
    runner._notify_service = WorkflowNotifyService(lambda ref: "unused", {"webhook": channel})

    assert runner.run_once() is True

    assert len(backend.completed) == 1
    assert channel.sent == []  # failed-only 目标在 success 终态不触发


def test_workflow_run_notify_reveal_injected_and_no_plaintext_leak() -> None:
    # 真 service + 真渠道:reveal 被注入且调用;payload 不含明文 url/token(R2/R5)
    backend = _FakeBackend([_workflow_run_job(_spec_with_notify(["all"]))])
    backend.children_by_parent["job-1"] = [
        _workflow_child("job-1", "n1", JobStatus.FAILED, error="sql_failed")
    ]
    runner = _workflow_runner(backend)
    reveal_calls: list[SecretRef] = []
    secret_url = "https://example.com/hook?key=SUPERSECRETTOKEN"

    def reveal(ref: SecretRef) -> str:
        reveal_calls.append(ref)
        return secret_url

    channel = _RecordingChannel()
    runner._notify_service = WorkflowNotifyService(reveal, {"webhook": channel})

    assert runner.run_once() is True

    assert len(backend.failed) == 1
    assert len(reveal_calls) == 1
    assert reveal_calls[0].ref == "ref-1"
    assert reveal_calls[0].kind is SecretKind.WEBHOOK_SECRET
    assert len(channel.sent) == 1
    payload = channel.sent[0]["payload"]
    dumped = payload.model_dump_json()  # type: ignore[attr-defined]
    assert "SUPERSECRETTOKEN" not in dumped
    assert secret_url not in dumped


def test_workflow_run_notify_failure_does_not_break_run_terminal() -> None:
    # ★ 硬红线:通知服务抛异常时 run 终态仍正确落定,worker 循环不崩
    backend = _FakeBackend([_workflow_run_job(_spec_with_notify(["all"]))])
    backend.children_by_parent["job-1"] = [_workflow_child("job-1", "n1", JobStatus.SUCCESS)]
    runner = _workflow_runner(backend)
    service = _RaisingNotifyService()
    runner._notify_service = service  # type: ignore[assignment]

    assert runner.run_once() is True

    assert service.calls == 1  # 通知确实被尝试
    assert len(backend.completed) == 1  # run 仍正常 complete(通知异常被吞)
    assert backend.failed == []


def test_workflow_run_without_notify_service_completes_normally() -> None:
    # 默认构造无 notify_reveal → _notify_service is None(no-op),run 照常终态
    backend = _FakeBackend([_workflow_run_job(_spec_with_notify(["all"]))])
    backend.children_by_parent["job-1"] = [_workflow_child("job-1", "n1", JobStatus.SUCCESS)]
    runner = _workflow_runner(backend)

    assert runner._notify_service is None
    assert runner.run_once() is True
    assert len(backend.completed) == 1


def test_non_workflow_job_failure_does_not_notify() -> None:
    # _notify_workflow_run_terminal 仅对 WORKFLOW_RUN 生效:sql_query 失败不通知
    job = _make_job(payload={"sql": "SELECT 1 AS r", "result_set_id": "rs-1"})
    backend = _FakeBackend([job])
    # emit_columns_twice 触发 sql_query 失败(复用既有失败范式)
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
        WorkerRunnerConfig(worker_id="worker-1"),
    )
    service = _RecordingNotifyService()
    runner._notify_service = service  # type: ignore[assignment]

    assert runner.run_once() is True

    assert len(backend.failed) == 1
    assert service.calls == []


def test_worker_runs_export_excel_as_result_export_with_forced_excel_format() -> None:
    job = _make_job(
        kind=JobKind.EXPORT_EXCEL,
        payload={
            "source_result_set_id": "rs-source",
            "filename": "report.xlsx",
        },
    )
    backend = _FakeBackend([job])
    result_store = _FakeResultStore()
    result_store.columns_by_result_set["rs-source"] = [Column(name="n", type=ColumnType.INTEGER)]
    result_store.rows_by_result_set["rs-source"] = [Row(values=[7])]
    runner = WorkerRunner(
        backend,
        result_store,
        lambda datasource_id: _conn_info(datasource_id),
        _adapter_factory(_FakeAdapter([])),
        WorkerRunnerConfig(worker_id="worker-1"),
    )

    assert runner.run_once() is True

    assert len(backend.completed) == 1
    _, result_ref = backend.completed[0]
    assert result_ref.metadata["format"] == "excel"
    assert result_ref.metadata["filename"] == "report.xlsx"
    assert ("job-1", "report.xlsx") in result_store.export_artifacts


def test_worker_runs_lineage_analyze_with_thin_catalog_handler() -> None:
    job = _make_job(
        kind=JobKind.LINEAGE_ANALYZE,
        payload={
            "datasource_id": "ds-9",
            "sql_text": "INSERT INTO t2 (a) SELECT a FROM t1",
            "dialect": "mysql",
            "source_ref": "wf-node",
        },
    )
    backend = _FakeBackend([job])
    catalog = _FakeLineageCatalog()
    runner = WorkerRunner(
        backend,
        _FakeResultStore(),
        lambda datasource_id: _conn_info(datasource_id),
        _adapter_factory(_FakeAdapter([])),
        WorkerRunnerConfig(worker_id="worker-1"),
        lineage_catalog=catalog,
    )

    assert runner.run_once() is True

    assert len(catalog.persisted) == 1
    persisted = catalog.persisted[0]
    assert persisted["project_id"] == "p_1"
    assert persisted["datasource_id"] == "ds-9"
    assert persisted["source_ref"] == "wf-node"
    assert len(backend.completed) == 1
    _, result_ref = backend.completed[0]
    assert result_ref.backend == "lineage_run"
    assert result_ref.metadata["cached"] is False


def test_worker_runs_lineage_batch_over_zip_with_lenient_parsing() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "etl/a.sql",
            "insert into kgrp.mid select * from kgrp.src a where a.p = ${v_period}",
        )
        archive.writestr("etl/b.txt", "insert into kgrp.tgt select * from kgrp.mid")
        archive.writestr("readme.md", "not sql")
    job = _make_job(
        kind=JobKind.LINEAGE_BATCH,
        payload={
            "upload_id": "up-1",
            "storage_uri": "uploads/up-1/scripts.zip",
            "datasource_id": "ds-9",
            "dialect": "oracle",
        },
    )
    backend = _FakeBackend([job])
    result_store = _FakeResultStore()
    result_store.downloads["uploads/up-1/scripts.zip"] = buffer.getvalue()
    catalog = _FakeLineageCatalog()
    runner = WorkerRunner(
        backend,
        result_store,
        lambda datasource_id: _conn_info(datasource_id),
        _adapter_factory(_FakeAdapter([])),
        WorkerRunnerConfig(worker_id="worker-1"),
        lineage_catalog=catalog,
    )

    assert runner.run_once() is True

    # 两个 SQL 文件都在空元数据下走宽松路径落了 run(readme.md 被跳过)
    assert len(catalog.persisted) == 2
    assert {item["source_ref"] for item in catalog.persisted} == {"etl/a.sql", "etl/b.txt"}
    assert all(item["table_edges"] == 1 for item in catalog.persisted)
    assert len(backend.completed) == 1
    _, result_ref = backend.completed[0]
    assert result_ref.backend == "lineage_batch"
    assert result_ref.metadata["file_count"] == 2
    assert result_ref.metadata["parsed"] == 2
    assert result_ref.metadata["failed"] == 0
    assert result_ref.metadata["script_edge_count"] == 1
    report = json.loads(result_store.run_artifacts[("job-1", "lineage_batch_report.json")])
    assert report["skipped"]["non_sql"] == 1
    assert report["script_edges"] == [
        {"source_file": "etl/a.sql", "target_file": "etl/b.txt", "tables": ["kgrp.mid"]}
    ]
    file_a = next(item for item in report["files"] if item["source_ref"] == "etl/a.sql")
    assert file_a["tables_written"] == ["kgrp.mid"]
    assert file_a["tables_read"] == ["kgrp.src"]
    assert file_a["lenient_statement_count"] == 1


def test_worker_runs_lineage_batch_without_datasource_report_only() -> None:
    # 无库纯文本导入:datasource_id 为空,dialect 显式给 → 只出报告不落 run
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a.sql", "insert into kgrp.mid select * from kgrp.src")
        archive.writestr("b.sql", "insert into kgrp.tgt select * from kgrp.mid")
    job = _make_job(
        kind=JobKind.LINEAGE_BATCH,
        payload={
            "upload_id": "up-1",
            "storage_uri": "uploads/up-1/scripts.zip",
            "datasource_id": None,
            "dialect": "oracle",
        },
    )
    backend = _FakeBackend([job])
    result_store = _FakeResultStore()
    result_store.downloads["uploads/up-1/scripts.zip"] = buffer.getvalue()
    catalog = _FakeLineageCatalog()
    runner = WorkerRunner(
        backend,
        result_store,
        lambda datasource_id: _conn_info(datasource_id),
        _adapter_factory(_FakeAdapter([])),
        WorkerRunnerConfig(worker_id="worker-1"),
        lineage_catalog=catalog,
    )

    assert runner.run_once() is True

    # 无数据源:不落任何 run,但报告与跨脚本依赖照常产出
    assert catalog.persisted == []
    _, result_ref = backend.completed[0]
    assert result_ref.metadata["parsed"] == 2
    assert result_ref.metadata["script_edge_count"] == 1
    report = json.loads(result_store.run_artifacts[("job-1", "lineage_batch_report.json")])
    assert report["table_edge_total"] == 2
    file_a = next(item for item in report["files"] if item["source_ref"] == "a.sql")
    assert file_a["run_id"] is None  # 未持久化
    assert file_a["tables_written"] == ["kgrp.mid"]
    # 内存透传键不得泄漏进序列化报告
    assert "_report" not in file_a


def test_worker_lineage_batch_emits_semantic_view_with_refresh_and_risk() -> None:
    # L-4:DELETE(无 WHERE)+ INSERT 同表 → 全量重刷 delete_insert + full_reload 风险
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "refresh.sql",
            "delete from kgrp.tgt;\ninsert into kgrp.tgt select * from kgrp.src",
        )
    job = _make_job(
        kind=JobKind.LINEAGE_BATCH,
        payload={
            "upload_id": "up-1",
            "storage_uri": "uploads/up-1/scripts.zip",
            "datasource_id": None,
            "dialect": "oracle",
        },
    )
    backend = _FakeBackend([job])
    result_store = _FakeResultStore()
    result_store.downloads["uploads/up-1/scripts.zip"] = buffer.getvalue()
    catalog = _FakeLineageCatalog()
    runner = WorkerRunner(
        backend,
        result_store,
        lambda datasource_id: _conn_info(datasource_id),
        _adapter_factory(_FakeAdapter([])),
        WorkerRunnerConfig(worker_id="worker-1"),
        lineage_catalog=catalog,
    )

    assert runner.run_once() is True

    report = json.loads(result_store.run_artifacts[("job-1", "lineage_batch_report.json")])
    semantic = report["semantic_view"]
    target = next(t for t in semantic["targets"] if t["table"] == "kgrp.tgt")
    assert target["refresh_mode"] == "delete_insert"
    assert target["counts"]["delete"] == 1 and target["counts"]["insert"] == 1
    assert "kgrp.src" in target["source_tables"]
    assert any(risk["type"] == "full_reload" for risk in semantic["risks"])
    assert any("全量重刷" in obs for obs in semantic["observations"])


def test_worker_lineage_batch_persists_projected_details() -> None:
    # L-5+ PR2:批量 report 保留每文件 B 组明细(details),供完整导出 B 组 7 sheet。
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a.sql", "insert into kgrp.mid select * from kgrp.src")
    job = _make_job(
        kind=JobKind.LINEAGE_BATCH,
        payload={
            "upload_id": "up-1",
            "storage_uri": "uploads/up-1/scripts.zip",
            "datasource_id": None,
            "dialect": "oracle",
        },
    )
    backend = _FakeBackend([job])
    result_store = _FakeResultStore()
    result_store.downloads["uploads/up-1/scripts.zip"] = buffer.getvalue()
    catalog = _FakeLineageCatalog()
    runner = WorkerRunner(
        backend,
        result_store,
        lambda datasource_id: _conn_info(datasource_id),
        _adapter_factory(_FakeAdapter([])),
        WorkerRunnerConfig(worker_id="worker-1"),
        lineage_catalog=catalog,
    )

    assert runner.run_once() is True

    report = json.loads(result_store.run_artifacts[("job-1", "lineage_batch_report.json")])
    assert report["details_truncated"] is False
    file_a = next(item for item in report["files"] if item["source_ref"] == "a.sql")
    details = file_a["details"]
    # 全 7 类键存在(空亦守空列表)
    for kind in (
        "procedure_segments",
        "statements",
        "parse_errors",
        "dynamic_sql_segments",
        "warnings",
        "insert_mappings",
        "graph_edges",
    ):
        assert kind in details
    # 表级数据流边明细已投影(表级血缘)
    edge = details["graph_edges"][0]
    assert edge["source_table"] == "kgrp.src"
    assert edge["target_table"] == "kgrp.mid"
    # 语句明细保留 index/type
    assert details["statements"][0]["index"] == 0
    # 内存透传键不得泄漏
    assert "_report" not in file_a


def test_project_lineage_batch_details_projects_keys_and_caps_cell() -> None:
    from app.domain.lineage import LineageReport
    from app.worker import _project_lineage_batch_details

    report = LineageReport(
        statements=[{"index": 0, "type": "INSERT", "noise": "drop-me"}],
        insert_mappings=[
            {
                "target_table": "t",
                "target_column": "c",
                "source_table": "s",
                "source_column": "c",
                "statement_index": 0,
                "transformation": "DIRECT",
                "transformation_subtype": "DIRECT",
                "inferred": False,
                "extra": "drop-me",
            }
        ],
        procedure_segments=[{"sql": "x" * 5000}],
    )
    details, remaining, dropped = _project_lineage_batch_details(report, 50_000)

    assert dropped == []
    # statements 投影只留 index/type,冗余键被砍
    assert details["statements"] == [{"index": 0, "type": "INSERT"}]
    # insert_mappings 投影不含 extra / inferred(不在投影键内)
    assert "extra" not in details["insert_mappings"][0]
    assert "inferred" not in details["insert_mappings"][0]
    # 未知形态 procedure_segments 原样透传,但超长 sql 被单字段封顶截断
    proc_sql = details["procedure_segments"][0]["sql"]
    assert len(proc_sql) < 5000
    assert proc_sql.endswith("…(truncated)")
    assert remaining == 50_000 - 3  # 消耗 1+1+1 行


def test_project_lineage_batch_details_enforces_per_file_row_cap() -> None:
    from app.domain.lineage import LineageReport
    from app.worker import _LINEAGE_BATCH_DETAIL_MAX_ROWS_PER_FILE, _project_lineage_batch_details

    over = _LINEAGE_BATCH_DETAIL_MAX_ROWS_PER_FILE + 25
    report = LineageReport(
        graph_edges=[
            {"source_table": f"s{i}", "target_table": "t", "statement_index": 0}
            for i in range(over)
        ]
    )
    details, _remaining, dropped = _project_lineage_batch_details(report, 50_000)

    assert len(details["graph_edges"]) == _LINEAGE_BATCH_DETAIL_MAX_ROWS_PER_FILE
    assert details["graph_edges_truncated"] is True
    assert dropped == [f"graph_edges:{over}->{_LINEAGE_BATCH_DETAIL_MAX_ROWS_PER_FILE}"]


def test_project_lineage_batch_details_exhausts_global_budget() -> None:
    from app.domain.lineage import LineageReport
    from app.worker import _project_lineage_batch_details

    report = LineageReport(
        statements=[{"index": i, "type": "SELECT"} for i in range(5)],
        graph_edges=[{"source_table": "s", "target_table": "t", "statement_index": 0}],
    )
    # 全局预算仅 3 行:statements 吃 3 行后截断,后续类(graph_edges)只出 0 行。
    details, remaining, dropped = _project_lineage_batch_details(report, 3)

    assert remaining == 0
    assert len(details["statements"]) == 3
    assert details["statements_truncated"] is True
    assert details["graph_edges"] == []
    assert details["graph_edges_truncated"] is True
    assert "statements:5->3" in dropped
    assert "graph_edges:1->0" in dropped


def test_worker_lineage_analyze_reuses_cached_run() -> None:
    job = _make_job(
        kind=JobKind.LINEAGE_ANALYZE,
        payload={
            "datasource_id": "ds-9",
            "sql_text": "INSERT INTO t2 (a) SELECT a FROM t1",
            "dialect": "mysql",
        },
    )
    backend = _FakeBackend([job])
    catalog = _FakeLineageCatalog(cached_run_id="run-cached")
    runner = WorkerRunner(
        backend,
        _FakeResultStore(),
        lambda datasource_id: _conn_info(datasource_id),
        _adapter_factory(_FakeAdapter([])),
        WorkerRunnerConfig(worker_id="worker-1"),
        lineage_catalog=catalog,
    )

    assert runner.run_once() is True

    assert catalog.persisted == []
    _, result_ref = backend.completed[0]
    assert result_ref.metadata == {"lineage_run_id": "run-cached", "cached": True}


class _FakeLineageCatalog:
    def __init__(self, cached_run_id: str | None = None) -> None:
        self._cached_run_id = cached_run_id
        self.persisted: list[dict[str, object]] = []

    def schema_context(
        self,
        datasource_id: str,
        default_schema: str | None,
    ) -> dict[str, Any]:
        return {
            "default_schema": default_schema,
            "schema": schema_from_metadata_cache_rows([]),
        }

    def cached_run_id(
        self,
        *,
        project_id: str,
        datasource_id: str,
        dialect: str,
        source_ref: str,
        sql_hash: str,
    ) -> str | None:
        return self._cached_run_id

    def persist_run(
        self,
        *,
        run_id: str,
        project_id: str,
        datasource_id: str,
        dialect: str,
        source_ref: str,
        sql_hash: str,
        sql_text: str,
        report: LineageReport,
    ) -> None:
        self.persisted.append(
            {
                "run_id": run_id,
                "project_id": project_id,
                "datasource_id": datasource_id,
                "dialect": dialect,
                "source_ref": source_ref,
                "sql_hash": sql_hash,
                "sql_text": sql_text,
                "table_edges": len(report.graph_edges),
            }
        )
