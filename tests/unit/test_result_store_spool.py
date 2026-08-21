from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.domain.schema import Row
from app.infrastructure.resultstore import local_fs
from app.infrastructure.resultstore.local_fs import LocalFsResultStore


def _rows(count: int = 32) -> list[Row]:
    return [Row(values=[index, f"value-{index}-{'x' * 128}"]) for index in range(count)]


def _batch_size(root: Path, rows: list[Row]) -> int:
    store = LocalFsResultStore(root)
    store.append_spool("rs-size", rows)
    return int(store.get_spool_manifest("rs-size")["data_bytes"])


@pytest.fixture
def parquet_writer_calls(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    original_write_table = local_fs._pq_write_table
    calls: list[Path] = []

    def count_write(table: Any, path: Path) -> None:
        calls.append(path)
        original_write_table(table, path)

    monkeypatch.setattr(local_fs, "_pq_write_table", count_write)
    return calls


def test_append_spool_writes_fitting_batch_once(
    tmp_path: Path,
    parquet_writer_calls: list[Path],
) -> None:
    store = LocalFsResultStore(tmp_path, spool_max_bytes=1024 * 1024)
    rows = _rows()

    store.append_spool("rs-fit", rows)

    assert len(parquet_writer_calls) == 1
    assert store.fetch_range("rs-fit", 0, len(rows)) == rows
    manifest = store.get_spool_manifest("rs-fit")
    assert manifest["loaded_rows"] == len(rows)
    assert manifest["truncated"] is False


def test_append_spool_writes_batch_once_at_exact_byte_limit(
    tmp_path: Path,
    parquet_writer_calls: list[Path],
) -> None:
    rows = _rows()
    exact_size = _batch_size(tmp_path / "calibration", rows)
    parquet_writer_calls.clear()
    store = LocalFsResultStore(tmp_path / "exact", spool_max_bytes=exact_size)

    store.append_spool("rs-exact", rows)

    assert len(parquet_writer_calls) == 1
    assert store.fetch_range("rs-exact", 0, len(rows)) == rows
    manifest = store.get_spool_manifest("rs-exact")
    assert manifest["loaded_rows"] == len(rows)
    assert manifest["data_bytes"] == exact_size
    assert manifest["truncated"] is True


def test_append_spool_falls_back_to_probe_when_batch_barely_exceeds_limit(
    tmp_path: Path,
    parquet_writer_calls: list[Path],
) -> None:
    rows = _rows()
    full_batch_size = _batch_size(tmp_path / "calibration", rows)
    byte_limit = full_batch_size - 1
    parquet_writer_calls.clear()
    store = LocalFsResultStore(tmp_path / "overflow", spool_max_bytes=byte_limit)

    store.append_spool("rs-overflow", rows)

    manifest = store.get_spool_manifest("rs-overflow")
    loaded_rows = int(manifest["loaded_rows"])
    assert len(parquet_writer_calls) > 2
    assert 0 < loaded_rows < len(rows)
    assert store.fetch_range("rs-overflow", 0, len(rows)) == rows[:loaded_rows]
    assert 0 < int(manifest["data_bytes"]) <= byte_limit
    assert manifest["truncated"] is True
    assert manifest["has_more"] is True
    assert manifest["result_version"] == 1


def test_fast_path_preserves_multi_part_fetch_and_manifest(
    tmp_path: Path,
    parquet_writer_calls: list[Path],
) -> None:
    store = LocalFsResultStore(tmp_path, spool_max_bytes=1024 * 1024)
    first_rows = _rows(3)
    second_rows = _rows(2)

    store.append_spool("rs-pages", first_rows)
    store.append_spool("rs-pages", second_rows)

    assert len(parquet_writer_calls) == 2
    assert store.fetch_range("rs-pages", 1, 3) == [*first_rows[1:], second_rows[0]]
    manifest = store.get_spool_manifest("rs-pages")
    assert manifest["loaded_rows"] == 5
    assert manifest["data_bytes"] == sum(part["bytes"] for part in manifest["parts"])
    assert [part["rows"] for part in manifest["parts"]] == [3, 2]
    assert manifest["truncated"] is False
    assert manifest["has_more"] is False
    assert manifest["result_version"] == 2
