"""忽略列语义单测:不参与差异判定,但仍取值、仍出现在结果行里。

回归的是这个 bug —— 早先 worker 把「忽略列」直接从 SELECT / 结果行 / 导出里整个
抹掉(`_effective_compare_columns` 的结果同时兼任投影列、哈希列、展示列),用户
一勾忽略,结果表里该字段就凭空消失了。忽略只应当影响 same/diff 归桶与标红。

复用 tests/unit/test_worker.py 的假实现,走文件↔DB 全量物化路径:
既覆盖 `_FileCompareReader`,也覆盖 `_DatabaseCompareReader._row_to_compare`。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from app.domain.compare_result import decode_compare_result_row
from app.domain.datasource import DatasourceConnInfo
from app.domain.job import JobKind
from app.domain.schema import Column, Row
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
    {"name": "updated_at", "type": "string"},
]
_BUCKET_SPOOLS = {
    "only_source": "rs-only-source",
    "only_target": "rs-only-target",
    "diff": "rs-diff",
    "same": "rs-same",
}

# 源 CSV 与 DB 目标在 updated_at 上「每一行都不同」—— 若忽略列仍进哈希,
# 四行会全部落进 diff 桶,断言会立刻炸。
_SOURCE_CSV = (
    b"id,name,updated_at\n"
    b"1,same,2026-09-01\n"
    b"2,left,2026-09-01\n"
    b"3,old,2026-09-01\n"
)
_TARGET_ROWS = [
    Row(values=[1, 1, "same", "2026-09-09"]),
    Row(values=[3, 3, "new", "2026-09-09"]),
    Row(values=[4, 4, "right", "2026-09-09"]),
]


def _payload(*, persist_same: bool) -> dict[str, object]:
    return {
        "run_id": "run-1",
        "task_id": "task-1",
        "source_id": None,
        "target_id": "ds-target",
        "source_ref": {
            "kind": "file",
            "file_format": "csv",
            "header_row": 1,
            "storage_uri": "uploads/src.csv",
            "filename": "src.csv",
        },
        "target_ref": {"kind": "table", "schema_name": "app", "table_name": "tgt"},
        "columns": _COLUMNS,
        "compare_rules": {"key_columns": ["id"], "ignore_columns": ["updated_at"]},
        "run_limits": {
            "recursive_checksum": False,
            "persist_same_bucket": persist_same,
        },
        "bucket_result_set_ids": dict(_BUCKET_SPOOLS),
    }


def _run(
    payload: dict[str, object],
) -> tuple[_FakeBackend, _FakeResultStore, _FakeCompareRunCatalog]:
    job = _make_job(kind=JobKind.COMPARE_RUN, payload=payload)
    backend = _FakeBackend([job])
    result_store = _FakeResultStore()
    result_store.downloads["uploads/src.csv"] = _SOURCE_CSV
    compare_catalog = _FakeCompareRunCatalog()
    remaining = [_FakeAdapter(list(_TARGET_ROWS))]

    def adapter_factory(
        conn_info: DatasourceConnInfo,
        cancel_check: Callable[[], bool],
        column_sink: Callable[[list[Column]], None],
        fetch_chunk_size: int,
        *,
        timeout_seconds: int | None = None,
    ) -> _ColumnSinkAdapter:
        del conn_info, cancel_check, column_sink, fetch_chunk_size
        return remaining.pop(0)

    runner = WorkerRunner(
        backend,
        result_store,
        lambda datasource_id: _conn_info(datasource_id),
        adapter_factory,
        WorkerRunnerConfig(worker_id="worker-1"),
        compare_run_catalog=compare_catalog,
        job_error_code_writer=_FakeErrorCodeWriter(),
        compare_result_inputs=cast(Any, None),
    )
    assert runner.run_once() is True
    return backend, result_store, compare_catalog


def test_ignored_column_does_not_flip_rows_into_diff() -> None:
    backend, _, compare_catalog = _run(_payload(persist_same=False))

    # id=1 两侧只有 updated_at 不同 → 必须仍判为 same,不是 diff。
    assert compare_catalog.completed[0]["bucket_counts"] == {
        "only_source": 1,
        "only_target": 1,
        "diff": 1,
        "same": 1,
    }
    assert not backend.failed


def test_ignored_column_still_present_in_result_rows_but_never_in_cells() -> None:
    _, result_store, _ = _run(_payload(persist_same=True))

    diff_row = decode_compare_result_row(result_store.rows_by_result_set["rs-diff"][0])
    assert diff_row["pk"] == {"id": "3"}
    # 忽略列两侧值照常落进结果行 —— 这正是此前丢失的东西。
    assert diff_row["source"] == {"id": "3", "name": "old", "updated_at": "2026-09-01"}
    assert diff_row["target"] == {"id": 3, "name": "new", "updated_at": "2026-09-09"}
    # 但它不产出 cell:前端按 cells 标红,忽略列因此永远不会被标红。
    assert diff_row["cells"] == [{"column": "name", "source": "old", "target": "new"}]

    same_row = decode_compare_result_row(result_store.rows_by_result_set["rs-same"][0])
    assert same_row["source"] == {"id": "1", "name": "same", "updated_at": "2026-09-01"}
    assert same_row["target"] == {"id": 1, "name": "same", "updated_at": "2026-09-09"}
    assert same_row["cells"] == []

    only_source = decode_compare_result_row(result_store.rows_by_result_set["rs-only-source"][0])
    assert only_source["source"] == {"id": "2", "name": "left", "updated_at": "2026-09-01"}
    only_target = decode_compare_result_row(result_store.rows_by_result_set["rs-only-target"][0])
    assert only_target["target"] == {"id": 4, "name": "right", "updated_at": "2026-09-09"}


def test_ignored_column_excluded_from_diff_profile_columns() -> None:
    _, _, compare_catalog = _run(_payload(persist_same=False))

    profile = cast(dict[str, Any], compare_catalog.completed[0]["diff_profile"])
    # 逐列匹配率只统计参与判定的列;忽略列进来会显示成 100% 不匹配,是噪音。
    assert set(profile["columns"]) == {"id", "name"}
