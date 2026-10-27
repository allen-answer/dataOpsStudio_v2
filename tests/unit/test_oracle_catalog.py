"""oracle_catalog:同一份 ALL_* 目录 SQL 按两种绑定风格渲染。

Oracle 用命名绑定,OB-Oracle(PyMySQL)用 pyformat。
"""

from __future__ import annotations

import pytest

from app.dbclients import oracle_catalog
from app.dbclients.oracle_catalog import CatalogSql

_ALL = [
    oracle_catalog.LIST_SCHEMAS,
    oracle_catalog.LIST_TABLES,
    oracle_catalog.LIST_COLUMNS,
    oracle_catalog.LIST_INDEXES,
    oracle_catalog.PRIMARY_KEY_COLUMNS,
    oracle_catalog.PRIMARY_KEY_INDEX_NAMES,
    oracle_catalog.TABLE_DDL,
]


def test_render_named_and_pyformat() -> None:
    sql = CatalogSql("SELECT 1 FROM t WHERE a = UPPER({owner}) AND b = {owner}", ("owner",))

    assert sql.render("named") == "SELECT 1 FROM t WHERE a = UPPER(:owner) AND b = :owner"
    assert sql.render("pyformat") == "SELECT 1 FROM t WHERE a = UPPER(%(owner)s) AND b = %(owner)s"


@pytest.mark.parametrize("catalog_sql", _ALL)
def test_every_declared_param_is_rendered_and_nothing_else(catalog_sql: CatalogSql) -> None:
    named = catalog_sql.render("named")
    pyformat = catalog_sql.render("pyformat")

    assert "{" not in named and "}" not in named
    for param in catalog_sql.params:
        assert f":{param}" in named
        assert f"%({param})s" in pyformat
    # 无参 SQL 两种风格完全相同(不得残留占位符)
    if not catalog_sql.params:
        assert named == pyformat


def test_table_ddl_binds_table_before_owner() -> None:
    # GET_DDL 的参数顺序是 (类型, 名称, schema),与列表查询相反,防止写反
    assert oracle_catalog.TABLE_DDL.render("named") == (
        "SELECT DBMS_METADATA.GET_DDL('TABLE', UPPER(:tbl), UPPER(:owner)) FROM dual"
    )
