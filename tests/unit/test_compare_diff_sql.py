"""UX-2 C-3:差异行定位 SQL 生成器单测(纯函数,不接库)。"""

from __future__ import annotations

from app.dbclients.sql_build import sql_literal
from app.domain.compare_diff_sql import build_diff_row_select, compare_side_expression
from app.domain.datasource import DbType


def test_sql_literal_renders_types() -> None:
    assert sql_literal(None) == "NULL"
    assert sql_literal(True) == "1"
    assert sql_literal(False) == "0"
    assert sql_literal(42) == "42"
    assert sql_literal("O'Brien") == "'O''Brien'"
    assert sql_literal("2026-01-02") == "'2026-01-02'"
    assert sql_literal(float("nan")) == "NULL"


def test_side_expression_table_and_sql_and_file() -> None:
    table_ref = {"kind": "table", "schema_name": "app", "table_name": "orders"}
    assert compare_side_expression(DbType.MYSQL, table_ref) == "`app`.`orders`"
    assert compare_side_expression(DbType.POSTGRESQL, table_ref) == '"APP"."ORDERS"'

    sql_ref = {"kind": "sql", "sql": "SELECT * FROM app.orders"}
    assert (
        compare_side_expression(DbType.MYSQL, sql_ref) == "(SELECT * FROM app.orders) DATAOPS_DIFF"
    )

    assert compare_side_expression(DbType.MYSQL, {"kind": "file"}) is None
    assert compare_side_expression(DbType.MYSQL, {"kind": "table"}) is None


def test_single_key_in_clause() -> None:
    sql = build_diff_row_select(
        DbType.MYSQL,
        ref={"kind": "table", "schema_name": "app", "table_name": "orders"},
        columns=["id"],
        pk_rows=[[3], [4], [7]],
    )
    assert sql == "SELECT * FROM `app`.`orders` WHERE `id` IN (3, 4, 7)"


def test_composite_key_row_value_in_clause() -> None:
    sql = build_diff_row_select(
        DbType.POSTGRESQL,
        ref={"kind": "table", "table_name": "orders"},
        columns=["region", "id"],
        pk_rows=[["us", 1], ["eu", 2]],
    )
    assert sql == ('SELECT * FROM "ORDERS" WHERE ("REGION", "ID") IN ((\'us\', 1), (\'eu\', 2))')


def test_string_values_are_escaped() -> None:
    sql = build_diff_row_select(
        DbType.MYSQL,
        ref={"kind": "table", "table_name": "t"},
        columns=["name"],
        pk_rows=[["a'b"], [None]],
    )
    assert sql == "SELECT * FROM `t` WHERE `name` IN ('a''b', NULL)"


def test_returns_none_for_file_or_empty() -> None:
    ref = {"kind": "table", "table_name": "t"}
    assert build_diff_row_select(DbType.MYSQL, ref=ref, columns=["id"], pk_rows=[]) is None
    assert build_diff_row_select(DbType.MYSQL, ref=ref, columns=[], pk_rows=[[1]]) is None
    assert (
        build_diff_row_select(DbType.MYSQL, ref={"kind": "file"}, columns=["id"], pk_rows=[[1]])
        is None
    )
