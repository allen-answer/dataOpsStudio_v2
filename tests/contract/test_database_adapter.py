"""DatabaseAdapter Protocol 契约测试(契约 §3.2)。

★ 接口设计以 Oracle/DM 为假想验证对象 —— 故契约测试同样覆盖:
- 标识符大小写差异(MySQL vs Oracle/DM)
- 分页语法差异
- server_side_cancel 多为 False 的优雅降级

Codex 实现 MySQL/DM/Oracle adapter 后:
1. 实现 adapter fixture(参数化各 driver)
2. 删除 @pytest.mark.skip
"""

from __future__ import annotations

import pytest

from app.dbclients.protocol import DatabaseAdapter

pytestmark = pytest.mark.contract


@pytest.fixture
def adapter() -> DatabaseAdapter:
    """DatabaseAdapter impl —— Codex T2 后改成参数化各 driver。"""
    pytest.skip("Codex T2(dbclients/mysql_adapter 等)实现后启用")


@pytest.mark.skip(reason="Codex T2 implements DatabaseAdapter")
def test_capabilities_declared(adapter: DatabaseAdapter) -> None:
    """capabilities 必须是 AdapterCapabilities 实例(9 字段全声明)。"""
    caps = adapter.capabilities
    assert hasattr(caps, "execute_select")
    assert hasattr(caps, "explain")
    assert hasattr(caps, "stream_rows")
    assert hasattr(caps, "server_side_cancel")
    assert hasattr(caps, "list_schemas")
    assert hasattr(caps, "list_tables")
    assert hasattr(caps, "list_columns")
    assert hasattr(caps, "list_indexes")
    assert hasattr(caps, "get_table_ddl")


@pytest.mark.skip(reason="Codex T2 implements DatabaseAdapter")
def test_test_connection_returns_bool(adapter: DatabaseAdapter) -> None:
    assert isinstance(adapter.test_connection(), bool)


@pytest.mark.skip(reason="Codex T2 implements DatabaseAdapter")
def test_execute_select_is_iterator(adapter: DatabaseAdapter) -> None:
    """execute_select 必须流式(Iterator[Row]),不一次性 fetchall。"""
    it = adapter.execute_select("SELECT 1", {})
    assert hasattr(it, "__iter__")
    assert hasattr(it, "__next__")


@pytest.mark.skip(reason="Codex T2 implements DatabaseAdapter")
def test_introspection_returns_typed_lists(adapter: DatabaseAdapter) -> None:
    """list_schemas/tables/columns/indexes 返回 list[Schema/Table/...]。"""
    schemas = adapter.list_schemas()
    assert isinstance(schemas, list)


@pytest.mark.skip(reason="Codex T2 implements DatabaseAdapter")
def test_kill_query_matches_capability(adapter: DatabaseAdapter) -> None:
    """server_side_cancel=False 时 kill_query 返回 False(优雅降级,不抛)。

    1.x AS-IS §2 已记录:MySQL/Oracle/DM/DB2 多无 driver-level cancel,
    走软取消(cancel_requested flag + statement timeout 兜底)。
    """
    result = adapter.kill_query("fake_conn_id")
    if not adapter.capabilities.server_side_cancel:
        assert result is False


@pytest.mark.skip(reason="Codex T2 implements DatabaseAdapter (Oracle/DM 大小写测试)")
def test_identifier_case_handling(adapter: DatabaseAdapter) -> None:
    """★ Oracle/DM 默认大写 + 双引号 quote;MySQL 反引号原样。
    list_columns 返回的 name 应符合该 adapter 的方言惯例。
    """
    pass  # 各 adapter 各自参数化测试


@pytest.mark.skip(reason="Codex T2 implements DatabaseAdapter")
def test_explain_returns_plan_node(adapter: DatabaseAdapter) -> None:
    if adapter.capabilities.explain:
        plan = adapter.explain("SELECT 1")
        assert plan.operation is not None
