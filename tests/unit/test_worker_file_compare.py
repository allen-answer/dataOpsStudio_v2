"""C-1 文件源对比 worker 全量物化路径单测(文件↔DB / 文件↔文件)。

复用 tests/unit/test_worker.py 的假实现(backend / result_store / adapter /
compare_run_catalog),只对"文件侧接入对比内核"这条新路径做断言:
key 对齐、4 桶差异检出、max_rows 超限 failed、临时文件清理。
不连 PG、不起 API、不碰真磁盘上传件。
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from app import worker as worker_module
from app.domain.compare_result import decode_compare_result_row
from app.domain.compare_result_input import (
    CompareResultInputDescriptor,
    OpenedCompareResultInput,
)
from app.domain.datasource import DatasourceConnInfo
from app.domain.job import JobErrorCode, JobKind
from app.domain.schema import Column, ColumnType, Row
from app.infrastructure.compare_result_inputs import CompareResultInputExpired
from app.worker import WorkerRunner, WorkerRunnerConfig
from tests.unit.test_worker import (
    _ColumnSinkAdapter,
    _conn_info,
    _FakeAdapter,
    _FakeBackend,
    _FakeCompareRunCatalog,
    _FakeErrorCodeWriter,
    _FakeResultStore,
    _make_job,
)

_COLUMNS = [
    {"name": "id", "type": "integer"},
    {"name": "name", "type": "string"},
]
_BUCKET_SPOOLS = {
    "only_source": "rs-only-source",
    "only_target": "rs-only-target",
    "diff": "rs-diff",
    "same": "rs-same",
}


def _file_ref(storage_uri: str, filename: str, *, file_format: str = "csv") -> dict[str, object]:
    return {
        "kind": "file",
        "file_format": file_format,
        "header_row": 1,
        "storage_uri": storage_uri,
        "filename": filename,
    }


def _compare_payload(
    *,
    source_id: str | None,
    target_id: str | None,
    source_ref: dict[str, object],
    target_ref: dict[str, object],
    max_rows: int | None = None,
) -> dict[str, object]:
    run_limits: dict[str, object] = {"recursive_checksum": False, "persist_same_bucket": False}
    if max_rows is not None:
        run_limits["max_rows"] = max_rows
    return {
        "run_id": "run-1",
        "task_id": "task-1",
        "source_id": source_id,
        "target_id": target_id,
        "source_ref": source_ref,
        "target_ref": target_ref,
        "columns": _COLUMNS,
        "compare_rules": {"key_columns": ["id"]},
        "run_limits": run_limits,
        "bucket_result_set_ids": dict(_BUCKET_SPOOLS),
    }


def _run_worker(
    *,
    payload: dict[str, object],
    downloads: dict[str, bytes],
    adapters: list[_FakeAdapter] | None = None,
    compare_result_inputs: object | None = None,
    datasource_loader: Callable[[str], DatasourceConnInfo] | None = None,
) -> tuple[_FakeBackend, _FakeResultStore, _FakeCompareRunCatalog, _FakeErrorCodeWriter]:
    job = _make_job(kind=JobKind.COMPARE_RUN, payload=payload)
    backend = _FakeBackend([job])
    result_store = _FakeResultStore()
    result_store.downloads.update(downloads)
    compare_catalog = _FakeCompareRunCatalog()
    error_writer = _FakeErrorCodeWriter()
    remaining = list(adapters or [])

    def adapter_factory(
        conn_info: DatasourceConnInfo,
        cancel_check: Callable[[], bool],
        column_sink: Callable[[list[Column]], None],
        fetch_chunk_size: int,
    ) -> _ColumnSinkAdapter:
        del conn_info, cancel_check, column_sink, fetch_chunk_size
        return remaining.pop(0)

    runner = WorkerRunner(
        backend,
        result_store,
        datasource_loader or (lambda datasource_id: _conn_info(datasource_id)),
        adapter_factory,
        WorkerRunnerConfig(worker_id="worker-1"),
        compare_run_catalog=compare_catalog,
        job_error_code_writer=error_writer,
        compare_result_inputs=cast(Any, compare_result_inputs),
    )
    assert runner.run_once() is True
    return backend, result_store, compare_catalog, error_writer


class _SnapshotInputs:
    def __init__(
        self,
        rows: list[Row] | None = None,
        *,
        error: Exception | None = None,
        partial: bool = False,
    ) -> None:
        self.rows = rows or []
        self.error = error
        self.partial = partial
        self.calls: list[dict[str, object]] = []

    def open(
        self,
        input_id: str,
        *,
        scope: object,
        batch_size: int,
        retain_until: datetime,
        allow_expired: bool = False,
    ) -> OpenedCompareResultInput:
        self.calls.append(
            {
                "input_id": input_id,
                "scope": scope,
                "batch_size": batch_size,
                "retain_until": retain_until,
                "allow_expired": allow_expired,
            }
        )
        if self.error is not None:
            raise self.error
        now = datetime.now(UTC)
        return OpenedCompareResultInput(
            descriptor=CompareResultInputDescriptor(
                id=input_id,
                project_id="project-1",
                origin_kind="statement",
                origin_id="statement-1",
                columns=(
                    Column(name="id", type=ColumnType.INTEGER),
                    Column(name="name", type=ColumnType.STRING),
                ),
                loaded_rows=len(self.rows),
                total_rows=len(self.rows),
                truncated=self.partial,
                has_more=self.partial,
                state="ready",
                created_at=now,
                expires_at=now + timedelta(days=1),
            ),
            batches=(tuple(self.rows),),
        )


def test_worker_compare_file_source_vs_db_target_detects_all_buckets() -> None:
    # CSV 源(id 为字符串 "1"/"2"/"3")↔ DB 目标(id 为 int)——跨类型 key 须对齐。
    csv_bytes = b"id,name\n1,same\n2,left\n3,old\n"
    db_adapter = _FakeAdapter(
        [
            Row(values=[1, 1, "same"]),
            Row(values=[3, 3, "new"]),
            Row(values=[4, 4, "right"]),
        ]
    )
    payload = _compare_payload(
        source_id=None,
        target_id="ds-target",
        source_ref=_file_ref("uploads/src.csv", "src.csv"),
        target_ref={"kind": "table", "schema_name": "app", "table_name": "tgt"},
    )
    backend, result_store, compare_catalog, _ = _run_worker(
        payload=payload,
        downloads={"uploads/src.csv": csv_bytes},
        adapters=[db_adapter],
    )

    completed = compare_catalog.completed[0]
    assert completed["bucket_counts"] == {
        "only_source": 1,
        "only_target": 1,
        "diff": 1,
        "same": 1,
    }
    # key 对齐:CSV "2" 只在源、DB 4 只在目标。
    assert decode_compare_result_row(result_store.rows_by_result_set["rs-only-source"][0])[
        "source"
    ] == {"id": "2", "name": "left"}
    assert decode_compare_result_row(result_store.rows_by_result_set["rs-only-target"][0])[
        "target"
    ] == {"id": 4, "name": "right"}
    diff_row = decode_compare_result_row(result_store.rows_by_result_set["rs-diff"][0])
    # 事件 pk 保留源侧原始 key 值(CSV 为字符串)。
    assert diff_row["pk"] == {"id": "3"}
    assert diff_row["cells"] == [{"column": "name", "source": "old", "target": "new"}]
    assert result_store.rows_by_result_set.get("rs-same", []) == []
    assert backend.completed and backend.completed[0][0] == "job-1"
    assert not backend.failed


def test_worker_compare_result_snapshot_vs_db_never_loads_snapshot_datasource() -> None:
    snapshot_inputs = _SnapshotInputs(
        [
            Row(values=[1, "same"]),
            Row(values=[2, "left"]),
            Row(values=[3, "old"]),
        ]
    )
    db_adapter = _FakeAdapter(
        [
            Row(values=[1, 1, "same"]),
            Row(values=[3, 3, "new"]),
            Row(values=[4, 4, "right"]),
        ]
    )
    loaded_datasources: list[str] = []

    def load_datasource(datasource_id: str) -> DatasourceConnInfo:
        loaded_datasources.append(datasource_id)
        return _conn_info(datasource_id)

    payload = _compare_payload(
        source_id=None,
        target_id="ds-target",
        source_ref={"kind": "result_snapshot", "input_id": "input-1"},
        target_ref={"kind": "table", "schema_name": "app", "table_name": "tgt"},
    )
    backend, result_store, compare_catalog, _ = _run_worker(
        payload=payload,
        downloads={},
        adapters=[db_adapter],
        compare_result_inputs=snapshot_inputs,
        datasource_loader=load_datasource,
    )

    assert compare_catalog.completed[0]["bucket_counts"] == {
        "only_source": 1,
        "only_target": 1,
        "diff": 1,
        "same": 1,
    }
    assert loaded_datasources == ["ds-target"]
    assert snapshot_inputs.calls[0]["input_id"] == "input-1"
    assert decode_compare_result_row(result_store.rows_by_result_set["rs-diff"][0])["cells"] == [
        {"column": "name", "source": "old", "target": "new"}
    ]
    assert backend.completed
    assert not backend.failed


def test_worker_compare_expired_result_snapshot_fails_without_sql_fallback() -> None:
    snapshot_inputs = _SnapshotInputs(error=CompareResultInputExpired("expired"))
    payload = _compare_payload(
        source_id=None,
        target_id=None,
        source_ref={"kind": "result_snapshot", "input_id": "input-expired"},
        target_ref={"kind": "result_snapshot", "input_id": "input-target"},
    )
    backend, _, compare_catalog, error_writer = _run_worker(
        payload=payload,
        downloads={},
        adapters=[],
        compare_result_inputs=snapshot_inputs,
        datasource_loader=lambda datasource_id: pytest.fail(
            f"unexpected datasource load: {datasource_id}"
        ),
    )

    assert backend.failed == [("job-1", "compare_input_expired")]
    assert error_writer.error_codes == [("job-1", JobErrorCode.SQL_FAILED)]
    assert compare_catalog.terminal == [{"run_id": "run-1", "status": "failed"}]


def test_worker_snapshot_vs_db_enforces_max_rows_on_database_side() -> None:
    snapshot_inputs = _SnapshotInputs([Row(values=[1, "one"])])
    db_adapter = _FakeAdapter(
        [
            Row(values=[1, 1, "one"]),
            Row(values=[2, 2, "two"]),
            Row(values=[3, 3, "three"]),
        ]
    )
    payload = _compare_payload(
        source_id=None,
        target_id="ds-target",
        source_ref={"kind": "result_snapshot", "input_id": "input-1"},
        target_ref={"kind": "table", "schema_name": "app", "table_name": "tgt"},
        max_rows=2,
    )
    backend, _, compare_catalog, error_writer = _run_worker(
        payload=payload,
        downloads={},
        adapters=[db_adapter],
        compare_result_inputs=snapshot_inputs,
    )

    assert backend.failed == [("job-1", "compare_limit_exceeded")]
    assert error_writer.error_codes == [("job-1", JobErrorCode.COMPARE_LIMIT_EXCEEDED)]
    assert compare_catalog.terminal == [{"run_id": "run-1", "status": "failed"}]


def test_worker_rejects_partial_snapshot_without_confirmation() -> None:
    snapshot_inputs = _SnapshotInputs([Row(values=[1, "one"])], partial=True)
    payload = _compare_payload(
        source_id=None,
        target_id=None,
        source_ref={"kind": "result_snapshot", "input_id": "input-partial"},
        target_ref={"kind": "result_snapshot", "input_id": "input-target"},
    )
    backend, _, compare_catalog, error_writer = _run_worker(
        payload=payload,
        downloads={},
        adapters=[],
        compare_result_inputs=snapshot_inputs,
    )

    assert backend.failed == [("job-1", "result_not_comparable")]
    assert error_writer.error_codes == [("job-1", JobErrorCode.SQL_FAILED)]
    assert compare_catalog.terminal == [{"run_id": "run-1", "status": "failed"}]


def test_worker_snapshot_materialization_enforces_byte_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_module, "_COMPARE_RESULT_SNAPSHOT_MAX_BYTES", 8)
    snapshot_inputs = _SnapshotInputs([Row(values=[1, "value-too-large"])])
    payload = _compare_payload(
        source_id=None,
        target_id=None,
        source_ref={"kind": "result_snapshot", "input_id": "input-large"},
        target_ref={"kind": "result_snapshot", "input_id": "input-target"},
    )
    backend, _, compare_catalog, error_writer = _run_worker(
        payload=payload,
        downloads={},
        adapters=[],
        compare_result_inputs=snapshot_inputs,
    )

    assert backend.failed == [("job-1", "compare_limit_exceeded")]
    assert error_writer.error_codes == [("job-1", JobErrorCode.COMPARE_LIMIT_EXCEEDED)]
    assert compare_catalog.terminal == [{"run_id": "run-1", "status": "failed"}]


def test_worker_compare_file_vs_file_detects_diff_and_only_rows() -> None:
    source_csv = b"id,name\n1,alpha\n2,beta\n3,gamma\n"
    target_csv = b"id,name\n1,alpha\n2,BETA\n4,delta\n"
    payload = _compare_payload(
        source_id=None,
        target_id=None,
        source_ref=_file_ref("uploads/a.csv", "a.csv"),
        target_ref=_file_ref("uploads/b.csv", "b.csv"),
    )
    backend, result_store, compare_catalog, _ = _run_worker(
        payload=payload,
        downloads={"uploads/a.csv": source_csv, "uploads/b.csv": target_csv},
        adapters=[],
    )

    completed = compare_catalog.completed[0]
    assert completed["bucket_counts"] == {
        "only_source": 1,  # id=3 只在源
        "only_target": 1,  # id=4 只在目标
        "diff": 1,  # id=2 beta vs BETA
        "same": 1,  # id=1 alpha
    }
    diff_row = decode_compare_result_row(result_store.rows_by_result_set["rs-diff"][0])
    assert diff_row["pk"] == {"id": "2"}
    assert diff_row["cells"] == [{"column": "name", "source": "beta", "target": "BETA"}]
    assert not backend.failed


def test_worker_compare_file_vs_file_case_insensitive_rule_collapses_diff() -> None:
    # CompareRules.case_insensitive 让 beta/BETA 归一相等 → 落 same,不落 diff。
    source_csv = b"id,name\n1,alpha\n2,beta\n"
    target_csv = b"id,name\n1,alpha\n2,BETA\n"
    payload = _compare_payload(
        source_id=None,
        target_id=None,
        source_ref=_file_ref("uploads/a.csv", "a.csv"),
        target_ref=_file_ref("uploads/b.csv", "b.csv"),
    )
    payload["compare_rules"] = {"key_columns": ["id"], "case_insensitive": True}
    _, _, compare_catalog, _ = _run_worker(
        payload=payload,
        downloads={"uploads/a.csv": source_csv, "uploads/b.csv": target_csv},
        adapters=[],
    )
    assert compare_catalog.completed[0]["bucket_counts"] == {
        "only_source": 0,
        "only_target": 0,
        "diff": 0,
        "same": 2,
    }


def test_worker_compare_file_max_rows_exceeded_fails_job() -> None:
    csv_bytes = b"id,name\n1,a\n2,b\n3,c\n"  # 3 行 > max_rows=2 → 硬顶
    payload = _compare_payload(
        source_id=None,
        target_id=None,
        source_ref=_file_ref("uploads/a.csv", "a.csv"),
        target_ref=_file_ref("uploads/b.csv", "b.csv"),
        max_rows=2,
    )
    backend, result_store, compare_catalog, error_writer = _run_worker(
        payload=payload,
        downloads={"uploads/a.csv": csv_bytes, "uploads/b.csv": csv_bytes},
        adapters=[],
    )
    assert backend.failed == [("job-1", "compare_limit_exceeded")]
    assert error_writer.error_codes == [("job-1", JobErrorCode.COMPARE_LIMIT_EXCEEDED)]
    assert compare_catalog.terminal == [{"run_id": "run-1", "status": "failed"}]
    # 超限不静默截断:不落任何结果行。
    assert all(not rows for rows in result_store.rows_by_result_set.values())


def test_worker_compare_file_cleans_temp_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    csv_bytes = b"id,name\n1,alpha\n2,beta\n"
    payload = _compare_payload(
        source_id=None,
        target_id=None,
        source_ref=_file_ref("uploads/a.csv", "a.csv"),
        target_ref=_file_ref("uploads/b.csv", "b.csv"),
    )
    backend, _, _, _ = _run_worker(
        payload=payload,
        downloads={"uploads/a.csv": csv_bytes, "uploads/b.csv": csv_bytes},
        adapters=[],
    )
    assert not backend.failed
    # 物化用的临时文件(下载副本)已全部删除。
    assert os.listdir(tmp_path) == []


def test_worker_compare_file_excel_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Excel 源(openpyxl)↔ DB 目标:验证 excel reader 装配与原生类型对齐。
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["id", "name"])
    sheet.append([1, "same"])
    sheet.append([2, "left"])
    xlsx_path = tmp_path / "src.xlsx"
    workbook.save(xlsx_path)
    xlsx_bytes = xlsx_path.read_bytes()

    db_adapter = _FakeAdapter(
        [
            Row(values=[1, 1, "same"]),
            Row(values=[3, 3, "right"]),
        ]
    )
    payload = _compare_payload(
        source_id=None,
        target_id="ds-target",
        source_ref=_file_ref("uploads/src.xlsx", "src.xlsx", file_format="excel"),
        target_ref={"kind": "table", "schema_name": "app", "table_name": "tgt"},
    )
    _, result_store, compare_catalog, _ = _run_worker(
        payload=payload,
        downloads={"uploads/src.xlsx": xlsx_bytes},
        adapters=[db_adapter],
    )
    assert compare_catalog.completed[0]["bucket_counts"] == {
        "only_source": 1,  # id=2 只在 Excel
        "only_target": 1,  # id=3 只在 DB
        "diff": 0,
        "same": 1,  # id=1 same
    }
    assert decode_compare_result_row(result_store.rows_by_result_set["rs-only-source"][0])[
        "source"
    ] == {"id": 2, "name": "left"}
