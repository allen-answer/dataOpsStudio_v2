"""ResultStore Protocol 契约测试(契约 §3.4)。

Codex T2 实现 LocalFsResultStore 后:
1. 实现 result_store fixture
2. 删除 @pytest.mark.skip
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest

from app.domain.schema import Row
from app.infrastructure.resultstore.local_fs import LocalFsResultStore
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
