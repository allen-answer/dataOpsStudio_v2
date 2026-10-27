"""方言特征表单测:收敛前各处硬分支的输出逐项快照,证明 PR-0 行为零变化。

快照值照抄收敛前的实现(sql_build / query_limit / compare_sql / result_export /
core._sqlglot_dialect / ai_upstream_locator 各自的 if 分支或本地字典)。
"""

from __future__ import annotations

import pytest

from app.api.errors import ApiError
from app.api.routes.core import _sqlglot_dialect as api_sqlglot_dialect
from app.broker.wiring import SESSION_CAPABLE_DB_TYPES
from app.dbclients import factory
from app.dbclients.query_limit import apply_database_row_limit
from app.dbclients.sql_build import limit_clause, quote_alias, quote_identifier
from app.domain.ai_upstream_locator import _sqlglot_dialect as ai_sqlglot_dialect
from app.domain.datasource import DbType
from app.domain.dialect import DIALECT_TRAITS, IdentifierStyle, traits, traits_for_name
from app.infrastructure.result_export import _quote_identifier as export_quote_identifier


def test_every_db_type_has_traits_and_an_adapter() -> None:
    assert set(DIALECT_TRAITS) == set(DbType)
    assert set(factory._ADAPTER_CLASSES) == set(DbType)


@pytest.mark.parametrize(
    ("db_type", "identifier", "alias", "limit"),
    [
        (DbType.MYSQL, "`app`.`orders`", "`Total`", " LIMIT 5"),
        (DbType.ORACLE, '"APP"."ORDERS"', '"Total"', " FETCH FIRST 5 ROWS ONLY"),
        (DbType.DM, '"APP"."ORDERS"', '"Total"', " FETCH FIRST 5 ROWS ONLY"),
        (DbType.DB2, '"APP"."ORDERS"', '"Total"', " FETCH FIRST 5 ROWS ONLY"),
        (DbType.POSTGRESQL, '"APP"."ORDERS"', '"Total"', " FETCH FIRST 5 ROWS ONLY"),
    ],
)
def test_sql_build_snapshot(db_type: DbType, identifier: str, alias: str, limit: str) -> None:
    assert quote_identifier(db_type, "app.orders") == identifier
    assert quote_alias(db_type, "Total") == alias
    assert limit_clause(db_type, 5) == limit
    assert limit_clause(db_type, None) == ""


@pytest.mark.parametrize(
    ("db_type", "row_limit_dialect", "sqlglot_dialect"),
    [
        (DbType.MYSQL, "mysql", "mysql"),
        (DbType.ORACLE, "oracle", "oracle"),
        (DbType.DM, "oracle", "oracle"),
        (DbType.DB2, "oracle", None),
        (DbType.POSTGRESQL, "postgres", "postgres"),
    ],
)
def test_sqlglot_dialect_snapshot(
    db_type: DbType, row_limit_dialect: str, sqlglot_dialect: str | None
) -> None:
    assert traits(db_type).row_limit_dialect == row_limit_dialect
    assert traits(db_type).sqlglot_dialect == sqlglot_dialect


def test_row_limit_rendering_snapshot() -> None:
    assert (
        apply_database_row_limit("SELECT a FROM t", DbType.MYSQL, 10) == "SELECT a FROM t LIMIT 10"
    )
    assert (
        apply_database_row_limit("SELECT a FROM t", DbType.DB2, 10)
        == "SELECT a FROM t FETCH FIRST 10 ROWS ONLY"
    )


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("mysql", "mysql"),
        ("MySQL", "mysql"),
        ("oracle", "oracle"),
        ("dm", "oracle"),
        ("postgresql", "postgres"),
        ("postgres", "postgres"),
    ],
)
def test_api_sqlglot_dialect_snapshot(name: str, expected: str) -> None:
    assert api_sqlglot_dialect(name) == expected


@pytest.mark.parametrize("name", ["db2", "sqlserver", ""])
def test_api_sqlglot_dialect_rejects_unsupported(name: str) -> None:
    with pytest.raises(ApiError) as excinfo:
        api_sqlglot_dialect(name)
    assert excinfo.value.code == "unsupported_dialect"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("mysql", "mysql"),
        ("postgresql", "postgres"),
        ("dm", "oracle"),
        ("ORACLE", "oracle"),
        ("db2", None),
        ("postgres", None),
        ("tsql", None),
    ],
)
def test_ai_locator_dialect_snapshot(name: str, expected: str | None) -> None:
    assert ai_sqlglot_dialect(name) == expected


@pytest.mark.parametrize(
    ("db_type", "expected"),
    [
        ("mysql", "`Order``Id`"),
        ("oracle", '"Order`Id"'),
        ("dm", '"Order`Id"'),
        ("postgresql", '"Order`Id"'),
        ("unknown", '"Order`Id"'),
    ],
)
def test_export_quote_snapshot(db_type: str, expected: str) -> None:
    # 导出保留原大小写,与 sql_build 的大写引用不同
    assert export_quote_identifier("Order`Id", db_type) == expected


def test_traits_for_name_unknown_returns_none() -> None:
    assert traits_for_name("sqlserver") is None
    assert traits_for_name("MYSQL") is traits(DbType.MYSQL)
    assert traits(DbType.MYSQL).identifier_style is IdentifierStyle.BACKTICK


def test_session_capable_types_unchanged() -> None:
    assert SESSION_CAPABLE_DB_TYPES == frozenset({DbType.MYSQL, DbType.DM})
