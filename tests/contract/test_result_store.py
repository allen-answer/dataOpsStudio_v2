"""ResultStore Protocol 契约测试(契约 §3.4)。

Codex T2 实现 LocalFsResultStore 后:
1. 实现 result_store fixture
2. 删除 @pytest.mark.skip
"""

from __future__ import annotations

import os
import time
from io import BytesIO
from pathlib import Path

import pytest

from app.domain.schema import Row
from app.infrastructure.resultstore.local_fs import LocalFsResultStore, ResultStoreError
from app.infrastructure.resultstore.protocol import ResultStore

pytestmark = pytest.mark.contract


@pytest.fixture
def result_store(tmp_path: Path) -> ResultStore:
    return LocalFsResultStore(tmp_path)


def test_append_spool_then_fetch_range(result_store: ResultStore) -> None:
    """流式追加 + 范围读 ←→ ResultSet 边拉边看模式。"""
    result_store.append_spool("rs-1", [Row(values=[1, "a"]), Row(values=[2, "b"])])
    result_store.append_spool("rs-1", [Row(values=[3, "c"])])

    assert result_store.fetch_range("rs-1", 1, 2) == [
        Row(values=[2, "b"]),
        Row(values=[3, "c"]),
    ]
    manifest = result_store.get_spool_manifest("rs-1")
    assert manifest["result_version"] == 2
    assert manifest["first_batch_at"] is not None


def test_spool_exists_reflects_manifest_presence(result_store: ResultStore) -> None:
    assert result_store.spool_exists("rs-missing") is False

    result_store.append_spool("rs-present", [Row(values=[1])])

    assert result_store.spool_exists("rs-present") is True


def test_spool_pagination_metadata_persists_without_cursor_state(
    result_store: ResultStore,
) -> None:
    result_store.append_spool("rs-pages", [Row(values=[1])])
    result_store.set_spool_pagination(
        "rs-pages",
        has_more=True,
        mode="ordered_offset",
        reason="fresh_read_ordered_offset",
    )

    manifest = result_store.get_spool_manifest("rs-pages")
    assert manifest["has_more"] is True
    assert manifest["pagination_mode"] == "ordered_offset"
    assert manifest["pagination_reason"] == "fresh_read_ordered_offset"
    assert "cursor" not in manifest


def test_snapshot_spool_survives_source_deletion(result_store: ResultStore) -> None:
    result_store.append_spool(
        "rs-source",
        [Row(values=[1, "a"]), Row(values=[2, "b"])],
    )
    result_store.set_spool_pagination(
        "rs-source",
        has_more=True,
        mode="ordered_offset",
    )

    snapshot = result_store.snapshot_spool("rs-source", "rs-snapshot")

    assert snapshot.result_set_id == "rs-snapshot"
    assert snapshot.loaded_rows == 2
    assert snapshot.has_more is True
    assert result_store.delete_spool("rs-source") is True
    assert result_store.fetch_range("rs-snapshot", 0, 10) == [
        Row(values=[1, "a"]),
        Row(values=[2, "b"]),
    ]
    manifest = result_store.get_spool_manifest("rs-snapshot")
    assert manifest["result_set_id"] == "rs-snapshot"
    assert manifest["sealed"] is True
    parts = manifest["parts"]
    assert isinstance(parts, list)
    assert all(isinstance(part, dict) and "rs-snapshot" in part["path"] for part in parts)
    with pytest.raises(ResultStoreError, match="immutable"):
        result_store.append_spool("rs-snapshot", [Row(values=[3, "c"])])
    with pytest.raises(ResultStoreError, match="immutable"):
        result_store.mark_spool_truncated("rs-snapshot")
    with pytest.raises(ResultStoreError, match="immutable"):
        result_store.set_spool_pagination(
            "rs-snapshot",
            has_more=False,
            mode="unavailable",
        )
    assert isinstance(result_store, LocalFsResultStore)
    with pytest.raises(ResultStoreError, match="immutable"):
        result_store.set_spool_columns("rs-snapshot", [])


def test_snapshot_spool_never_overwrites_existing_snapshot(result_store: ResultStore) -> None:
    result_store.append_spool("rs-source", [Row(values=[1])])
    result_store.snapshot_spool("rs-source", "rs-snapshot")

    with pytest.raises(ResultStoreError, match="already exists"):
        result_store.snapshot_spool("rs-source", "rs-snapshot")


def test_put_artifact_returns_result_ref(result_store: ResultStore) -> None:
    """一次性写入(导出场景)返回 ResultRef。"""
    ref = result_store.put_artifact("run-1", "out.txt", BytesIO(b"hello"))

    assert ref.backend == "local_fs"
    assert ref.uri.endswith("out.txt")
    assert result_store.get_manifest("run-1").artifacts == [ref]


def test_open_download_returns_binary_io(result_store: ResultStore) -> None:
    """ResultRef → BinaryIO 用于下载。"""
    ref = result_store.put_artifact("run-1", "out.txt", BytesIO(b"hello"))

    with result_store.open_download(ref) as stream:
        assert stream.read() == b"hello"


def test_put_export_artifact_uses_export_ttl_namespace(result_store: ResultStore) -> None:
    ref = result_store.put_export_artifact("export-1", "out.csv", BytesIO(b"value\n1\n"))

    assert ref.backend == "local_fs"
    assert ref.uri == "exports/export-1/out.csv"
    with result_store.open_download(ref) as stream:
        assert stream.read() == b"value\n1\n"


def test_gc_expired_removes_old_export_artifacts(tmp_path: Path) -> None:
    store = LocalFsResultStore(tmp_path, sql_export_ttl_hours=1)
    ref = store.put_export_artifact("export-old", "out.csv", BytesIO(b"value\n1\n"))
    export_dir = tmp_path / "exports" / "export-old"
    old = time.time() - 2 * 60 * 60
    os.utime(export_dir, (old, old))

    assert store.gc_expired() == 1
    with pytest.raises(FileNotFoundError):
        store.open_download(ref)


def test_gc_expired_removes_stale_result_set_spools(tmp_path: Path) -> None:
    """基线:过了 TTL 的结果集 spool 会被回收 —— 下面 R3 用例的对照组。"""

    store = LocalFsResultStore(tmp_path, result_ttl_days=1)
    store.append_spool("rs-stale", [Row(values=[1])])
    _age_spool(tmp_path, "rs-stale")

    assert store.gc_expired() == 1
    assert store.spool_exists("rs-stale") is False


def test_gc_expired_never_reclaims_a_protected_result_set(tmp_path: Path) -> None:
    """★ 评审修订 R3(设计 §10-A5):活跃会话语句的结果集不得被 spool GC 回收。

    会话结果集与 job 结果共用同一个 spool 目录,而 GC 只看 mtime;一条用户还在
    翻页/导出的会话结果集,mtime 一过 TTL 就会被删掉(`result_ttl_days=0` 时
    窗口甚至是"立刻")。判定权必须回到 catalog:调用方给保护集,GC 跳过。
    """

    store = LocalFsResultStore(tmp_path, result_ttl_days=0)
    store.append_spool("rs-live", [Row(values=[1])])
    store.append_spool("rs-dead", [Row(values=[2])])
    _age_spool(tmp_path, "rs-live")
    _age_spool(tmp_path, "rs-dead")

    removed = store.gc_expired(keep_result_set_ids={"rs-live"})

    assert removed == 1
    assert store.spool_exists("rs-live") is True
    assert store.fetch_range("rs-live", 0, 10) == [Row(values=[1])]
    assert store.spool_exists("rs-dead") is False


def test_protecting_a_result_set_does_not_protect_exports_or_runs(tmp_path: Path) -> None:
    """保护集只作用于结果集目录:导出件与 run 件仍按各自 TTL 回收。"""

    store = LocalFsResultStore(tmp_path, result_ttl_days=0, sql_export_ttl_hours=1)
    store.put_export_artifact("rs-live", "out.csv", BytesIO(b"value\n1\n"))
    export_dir = tmp_path / "exports" / "rs-live"
    old = time.time() - 2 * 60 * 60
    os.utime(export_dir, (old, old))

    assert store.gc_expired(keep_result_set_ids={"rs-live"}) == 1
    assert not export_dir.exists()


def _age_spool(root: Path, result_set_id: str) -> None:
    """把 spool 目录的 mtime 推到很久以前(GC 只认 mtime)。"""

    spool_dir = root / "resultsets" / result_set_id
    old = time.time() - 10 * 24 * 60 * 60
    os.utime(spool_dir, (old, old))


def test_no_cursor_held_after_append(result_store: ResultStore) -> None:
    """★ R6 配合:append_spool 写完不持 DB cursor,
    DB 资源由调用方(worker)管理,store 只管 spool 文件。
    """
    result_store.append_spool("rs-1", [Row(values=[1])])

    assert not hasattr(result_store, "cursor")
    assert result_store.fetch_range("rs-1", 0, 1) == [Row(values=[1])]


def test_gc_expired_returns_count(result_store: ResultStore) -> None:
    """过期清理返回清理数量(便于审计)。"""
    n = result_store.gc_expired()
    assert isinstance(n, int)
    assert n >= 0


def test_delete_spool_removes_result_set_files(result_store: ResultStore) -> None:
    result_store.append_spool("rs-1", [Row(values=[1])])

    assert result_store.delete_spool("rs-1") is True
    assert result_store.delete_spool("rs-1") is False
