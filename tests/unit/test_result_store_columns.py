from __future__ import annotations

import json
from pathlib import Path

from app.domain.schema import Column, ColumnType, Row
from app.infrastructure.resultstore.local_fs import LocalFsResultStore


def test_spool_manifest_persists_columns(tmp_path: Path) -> None:
    store = LocalFsResultStore(tmp_path)
    columns = [Column(name="r", type=ColumnType.UNKNOWN, nullable=True)]

    store.set_spool_columns("rs-1", columns)
    store.append_spool("rs-1", [Row(values=[2])])

    manifest = store.get_spool_manifest("rs-1")
    assert manifest["columns"] == [columns[0].model_dump()]
    assert store.fetch_range("rs-1", 0, 1) == [Row(values=[2])]


def test_spool_manifest_columns_are_backward_compatible(tmp_path: Path) -> None:
    resultset_dir = tmp_path / "resultsets" / "rs-old"
    resultset_dir.mkdir(parents=True)
    (resultset_dir / "manifest.json").write_text(
        json.dumps(
            {
                "result_set_id": "rs-old",
                "parts": [],
                "loaded_rows": 0,
                "data_bytes": 0,
                "truncated": False,
                "created_at": 1.0,
                "updated_at": 1.0,
            }
        ),
        encoding="utf-8",
    )
    store = LocalFsResultStore(tmp_path)

    assert store.get_spool_manifest("rs-old")["columns"] == []
