"""ResultStore Protocol 契约测试(契约 §3.4)。

Codex T2 实现 LocalFsResultStore 后:
1. 实现 result_store fixture
2. 删除 @pytest.mark.skip
"""

from __future__ import annotations

import pytest

from app.infrastructure.resultstore.protocol import ResultStore

pytestmark = pytest.mark.contract


@pytest.fixture
def result_store() -> ResultStore:
    pytest.skip("Codex T2(infrastructure/resultstore/)实现后启用")


@pytest.mark.skip(reason="Codex T2 implements ResultStore")
def test_append_spool_then_fetch_range(result_store: ResultStore) -> None:
    """流式追加 + 范围读 ←→ ResultSet 边拉边看模式。"""
    pass


@pytest.mark.skip(reason="Codex T2 implements ResultStore")
def test_put_artifact_returns_result_ref(result_store: ResultStore) -> None:
    """一次性写入(导出场景)返回 ResultRef。"""
    pass


@pytest.mark.skip(reason="Codex T2 implements ResultStore")
def test_open_download_returns_binary_io(result_store: ResultStore) -> None:
    """ResultRef → BinaryIO 用于下载。"""
    pass


@pytest.mark.skip(reason="Codex T2 implements ResultStore (R6 配合)")
def test_no_cursor_held_after_append(result_store: ResultStore) -> None:
    """★ R6 配合:append_spool 写完不持 DB cursor,
    DB 资源由调用方(worker)管理,store 只管 spool 文件。
    """
    pass


@pytest.mark.skip(reason="Codex T2 implements ResultStore")
def test_gc_expired_returns_count(result_store: ResultStore) -> None:
    """过期清理返回清理数量(便于审计)。"""
    n = result_store.gc_expired()
    assert isinstance(n, int)
    assert n >= 0
