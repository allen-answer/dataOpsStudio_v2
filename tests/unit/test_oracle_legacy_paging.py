"""11g 分页改写:query_limit 渲染出的 FETCH FIRST / OFFSET → ROWNUM 嵌套。"""

from __future__ import annotations

import pytest

from app.dbclients.oracle_legacy_paging import (
    ROWNUM_COLUMN,
    rewrite_for_pre12c,
    server_major_version,
)
from app.dbclients.query_limit import apply_database_page, apply_database_row_limit
from app.domain.datasource import DbType


def test_row_limit_becomes_rownum_wrapper_keeping_inner_order() -> None:
    limited = apply_database_row_limit("SELECT a FROM t ORDER BY a", DbType.ORACLE, 101)

    rewrite = rewrite_for_pre12c(limited)

    assert rewrite.sql == "SELECT * FROM (SELECT a FROM t ORDER BY a) WHERE ROWNUM <= 101"
    assert rewrite.drop_trailing_column is False


def test_page_with_offset_uses_row_number_column_and_marks_it_for_dropping() -> None:
    page = apply_database_page(
        "SELECT a FROM t ORDER BY a", DbType.ORACLE, page_offset=200, row_limit=101
    )

    rewrite = rewrite_for_pre12c(page)

    assert rewrite.sql == (
        f"SELECT * FROM (SELECT DATAOPS_Q__.*, ROWNUM AS {ROWNUM_COLUMN} "
        "FROM (SELECT a FROM t ORDER BY a) DATAOPS_Q__ WHERE ROWNUM <= 301) "
        f"WHERE {ROWNUM_COLUMN} > 200"
    )
    assert rewrite.drop_trailing_column is True


@pytest.mark.parametrize(
    "sql",
    [
        "WITH x AS (SELECT 1 AS a FROM dual) SELECT a FROM x",
        "SELECT a FROM t WHERE b = :v UNION ALL SELECT a FROM u",
    ],
)
def test_cte_union_and_binds_are_wrapped_whole(sql: str) -> None:
    limited = apply_database_row_limit(sql, DbType.ORACLE, 5)

    rewrite = rewrite_for_pre12c(limited)

    assert rewrite.sql.startswith("SELECT * FROM (")
    assert rewrite.sql.endswith(") WHERE ROWNUM <= 5")
    assert "FETCH" not in rewrite.sql


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a FROM t",  # 没有分页子句
        "SELECT a FROM t FETCH FIRST 10 PERCENT ROWS ONLY",
        "SELECT a FROM t ORDER BY a FETCH FIRST 3 ROWS WITH TIES",
        "SELECT a FROM t FETCH FIRST :n ROWS ONLY",  # 参数化行数
        "THIS IS NOT SQL (",
    ],
)
def test_unrewritable_sql_is_returned_unchanged(sql: str) -> None:
    rewrite = rewrite_for_pre12c(sql)

    assert rewrite.sql == sql
    assert rewrite.drop_trailing_column is False


def test_nested_fetch_in_subquery_is_left_to_the_database() -> None:
    sql = "SELECT * FROM (SELECT a FROM t FETCH FIRST 3 ROWS ONLY)"

    assert rewrite_for_pre12c(sql).sql == sql


@pytest.mark.parametrize(
    ("version", "major"),
    [("11.2.0.4.0", 11), ("19.0.0.0.0", 19), ("23.4", 23), (None, None), ("x", None)],
)
def test_server_major_version(version: object, major: int | None) -> None:
    assert server_major_version(version) == major
