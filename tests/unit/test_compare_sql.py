from __future__ import annotations

import pytest

from app.domain.compare_sql import (
    CompareSqlProjectionError,
    inspect_compare_sql,
    legacy_generated_aliases,
    normalize_compare_sql,
)
from app.domain.datasource import DbType


def test_unaliased_sum_and_case_receive_stable_aliases() -> None:
    sql = """SELECT CUST_NO,
      CASE WHEN BALANCE > 0 THEN 'Y' ELSE 'N' END,
      SUM(AMOUNT)
    FROM ACCOUNT GROUP BY CUST_NO, CASE WHEN BALANCE > 0 THEN 'Y' ELSE 'N' END"""

    plan = normalize_compare_sql(sql, DbType.DB2)

    assert [item.name for item in plan.projections] == [
        "CUST_NO",
        "RESULT_1",
        "RESULT_2",
    ]
    assert [item.generated for item in plan.projections] == [False, True, True]
    assert "AS RESULT_1" in plan.sql.upper()
    assert "AS RESULT_2" in plan.sql.upper()
    assert plan.projections[1].projection_index == 2
    assert "CASE" in (plan.projections[1].expression or "").upper()


def test_explicit_alias_and_existing_result_name_win_case_insensitively() -> None:
    plan = normalize_compare_sql(
        "SELECT amount AS result_1, SUM(tax), price * quantity AS total FROM sales",
        DbType.MYSQL,
    )

    assert [item.name for item in plan.projections] == [
        "result_1",
        "RESULT_2",
        "total",
    ]
    assert [item.generated for item in plan.projections] == [False, True, False]


@pytest.mark.parametrize(
    "expression",
    [
        "COALESCE(name, '')",
        "CAST(amount AS DECIMAL(12, 2))",
        "price * quantity",
    ],
)
def test_other_computed_expressions_receive_aliases(expression: str) -> None:
    plan = normalize_compare_sql(f"SELECT {expression} FROM sales", DbType.DB2)

    assert plan.projections[0].name == "RESULT_1"
    assert plan.projections[0].generated is True
    assert plan.projections[0].expression


def test_simple_columns_and_star_are_not_rewritten() -> None:
    plan = normalize_compare_sql("SELECT t.*, t.id FROM app.t AS t", DbType.POSTGRESQL)

    assert plan.rewritten is False
    assert "RESULT_" not in plan.sql
    assert [item.name for item in plan.projections] == ["*", "id"]


def test_cte_outer_select_is_supported() -> None:
    plan = normalize_compare_sql(
        "WITH x AS (SELECT amount FROM sales) SELECT SUM(amount) FROM x",
        DbType.DB2,
    )

    assert plan.projections[0].name == "RESULT_1"
    assert "WITH x AS" in plan.sql


def test_union_requires_explicit_aliases() -> None:
    with pytest.raises(CompareSqlProjectionError) as excinfo:
        normalize_compare_sql(
            "SELECT SUM(a) FROM x UNION ALL SELECT SUM(a) FROM y",
            DbType.DB2,
        )

    assert excinfo.value.code == "explicit_alias_required"


@pytest.mark.parametrize("sql", ["", "SELECT ("])
def test_blank_or_invalid_sql_is_rejected_without_echoing_sql(sql: str) -> None:
    with pytest.raises(CompareSqlProjectionError) as excinfo:
        normalize_compare_sql(sql, DbType.DB2)

    assert excinfo.value.code == "invalid_sql"
    if sql:
        assert sql not in str(excinfo.value)


def test_normalizing_an_already_normalized_sql_does_not_add_more_aliases() -> None:
    first = normalize_compare_sql("SELECT SUM(amount) FROM sales", DbType.DB2)
    second = normalize_compare_sql(first.sql, DbType.DB2)

    assert second.sql == first.sql
    assert second.projections[0].name == "RESULT_1"


def test_legacy_repairs_only_proven_numeric_driver_names() -> None:
    projections = inspect_compare_sql(
        "SELECT D, K1, K2, K3, C, SUM(A), SUM(B), SUM(C) FROM T GROUP BY D, K1, K2, K3, C"
    )

    assert legacy_generated_aliases(
        ["D", "K1", "K2", "K3", "C", "6", "7", "8"],
        projections,
    ) == {"6": "RESULT_1", "7": "RESULT_2", "8": "RESULT_3"}
    assert (
        legacy_generated_aliases(
            ["6"],
            inspect_compare_sql('SELECT 1 AS "6"'),
        )
        == {}
    )
