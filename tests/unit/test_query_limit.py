from __future__ import annotations

import pytest

from app.dbclients.query_limit import (
    QueryLimitError,
    analyze_database_row_limit,
    apply_database_page,
    apply_database_row_limit,
    supports_ordered_pagination,
)
from app.domain.datasource import DbType


@pytest.mark.parametrize(
    ("db_type", "expected"),
    [
        (DbType.MYSQL, "LIMIT 101"),
        (DbType.POSTGRESQL, "LIMIT 101"),
        (DbType.DM, "FETCH FIRST 101 ROWS ONLY"),
        (DbType.ORACLE, "FETCH FIRST 101 ROWS ONLY"),
        (DbType.DB2, "FETCH FIRST 101 ROWS ONLY"),
    ],
)
def test_database_limit_is_rendered_for_supported_dialects(
    db_type: DbType,
    expected: str,
) -> None:
    limited = apply_database_row_limit(
        "WITH source_rows AS (SELECT id FROM items) SELECT id FROM source_rows ORDER BY id;",
        db_type,
        101,
    )

    assert expected in limited
    assert limited.startswith("WITH source_rows AS")
    assert not limited.endswith(";")


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT * FROM t LIMIT 50 OFFSET 2", "OFFSET 2 ROWS FETCH FIRST 50 ROWS ONLY"),
        (
            "SELECT * FROM t OFFSET 2 ROWS FETCH NEXT 50 ROWS ONLY",
            "OFFSET 2 ROWS FETCH NEXT 50 ROWS ONLY",
        ),
        ("SELECT * FROM t WHERE ROWNUM <= 50", "ROWNUM <= 50"),
    ],
)
def test_existing_smaller_limits_offsets_and_rownum_are_preserved(
    sql: str,
    expected: str,
) -> None:
    limited = apply_database_row_limit(sql, DbType.ORACLE, 101)

    assert expected in limited


def test_existing_larger_top_level_limit_is_replaced_but_subquery_limit_is_preserved() -> None:
    limited = apply_database_row_limit(
        "SELECT * FROM (SELECT * FROM t LIMIT 7) nested LIMIT 500",
        DbType.MYSQL,
        101,
    )

    assert "LIMIT 7" in limited
    assert limited.endswith("LIMIT 101")


@pytest.mark.parametrize(
    ("db_type", "sql", "binding"),
    [
        (DbType.MYSQL, "SELECT * FROM t WHERE id = %(item_id)s", "%(item_id)s"),
        (DbType.POSTGRESQL, "SELECT * FROM t WHERE id = %s", "%s"),
        (DbType.DM, "SELECT * FROM t WHERE id = ?", "?"),
        (DbType.ORACLE, "SELECT * FROM t WHERE id = :item_id", ":item_id"),
        (DbType.DB2, "SELECT * FROM t WHERE id = ?", "?"),
    ],
)
def test_driver_bindings_survive_ast_rewrite(db_type: DbType, sql: str, binding: str) -> None:
    limited = apply_database_row_limit(sql, db_type, 101)

    assert binding in limited


def test_placeholder_like_text_inside_literals_and_comments_is_not_rewritten() -> None:
    limited = apply_database_row_limit(
        "SELECT '%s' AS literal_value, id FROM t WHERE id = %s -- %(comment)s",
        DbType.MYSQL,
        101,
    )

    assert "'%s' AS literal_value" in limited
    assert "id = %s" in limited
    assert "%(comment)s" in limited


def test_unparseable_select_is_rejected_instead_of_executed_without_a_cap() -> None:
    with pytest.raises(QueryLimitError, match="safely parsed"):
        apply_database_row_limit("SELECT FROM", DbType.MYSQL, 101)


@pytest.mark.parametrize(
    ("db_type", "expected_limit", "expected_offset"),
    [
        (DbType.MYSQL, "LIMIT 101", "OFFSET 220"),
        (DbType.POSTGRESQL, "LIMIT 101", "OFFSET 220"),
        (DbType.DM, "FETCH FIRST 101 ROWS ONLY", "OFFSET 220 ROWS"),
        (DbType.ORACLE, "FETCH FIRST 101 ROWS ONLY", "OFFSET 220 ROWS"),
        (DbType.DB2, "FETCH FIRST 101 ROWS ONLY", "OFFSET 220 ROWS"),
    ],
)
def test_database_page_combines_original_and_page_offsets(
    db_type: DbType,
    expected_limit: str,
    expected_offset: str,
) -> None:
    paged = apply_database_page(
        "SELECT id FROM items ORDER BY id OFFSET 20 ROWS",
        db_type,
        page_offset=200,
        row_limit=101,
    )

    assert expected_limit in paged
    assert expected_offset in paged


def test_database_page_intersects_existing_literal_limit() -> None:
    paged = apply_database_page(
        "SELECT id FROM items ORDER BY id LIMIT 250 OFFSET 10",
        DbType.MYSQL,
        page_offset=200,
        row_limit=101,
    )

    assert paged.endswith("LIMIT 50 OFFSET 210")


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT * FROM t ORDER BY id", True),
        ("WITH q AS (SELECT * FROM t) SELECT * FROM q ORDER BY id", True),
        ("SELECT * FROM (SELECT * FROM t ORDER BY id) nested", False),
        ("SELECT * FROM t", False),
    ],
)
def test_ordered_pagination_requires_top_level_order_by(sql: str, expected: bool) -> None:
    assert supports_ordered_pagination(sql, DbType.MYSQL) is expected


def test_parameterized_limit_is_preserved_inside_safe_outer_cap() -> None:
    limited = apply_database_row_limit(
        "SELECT id FROM items ORDER BY id LIMIT %(user_limit)s OFFSET %(user_offset)s",
        DbType.MYSQL,
        101,
    )

    assert "LIMIT %(user_limit)s OFFSET %(user_offset)s" in limited
    assert limited.endswith("AS _dataops_page LIMIT 101")
    assert (
        supports_ordered_pagination(
            "SELECT id FROM items ORDER BY id LIMIT %(user_limit)s",
            DbType.MYSQL,
        )
        is False
    )


@pytest.mark.parametrize("db_type", [DbType.DM, DbType.ORACLE, DbType.DB2])
def test_parameterized_fetch_is_preserved_for_oracle_compatible_dialects(
    db_type: DbType,
) -> None:
    limited = apply_database_row_limit(
        "SELECT id FROM items ORDER BY id FETCH NEXT :row_count ROWS ONLY",
        db_type,
        101,
    )

    assert ":row_count" in limited
    assert limited.endswith("FETCH FIRST 101 ROWS ONLY")


@pytest.mark.parametrize("db_type", [DbType.MYSQL, DbType.DM])
@pytest.mark.parametrize(
    ("sql", "shape", "pushdown", "reason"),
    [
        (
            "SELECT id FROM items WHERE active = 1",
            "simple_select",
            True,
            "top_level_limit_can_stop_row_production",
        ),
        (
            "SELECT category, COUNT(*) FROM items GROUP BY category",
            "aggregate",
            False,
            "aggregate_requires_full_input",
        ),
        (
            "SELECT DISTINCT category FROM items",
            "distinct",
            False,
            "distinct_requires_deduplication",
        ),
        (
            "SELECT id, ROW_NUMBER() OVER (ORDER BY id) AS rn FROM items",
            "window",
            False,
            "window_requires_partition_input",
        ),
        (
            "SELECT id FROM current_items UNION ALL SELECT id FROM archived_items",
            "set_operation",
            False,
            "set_operation_requires_combined_input",
        ),
        (
            "SELECT id FROM items ORDER BY created_at",
            "ordered",
            False,
            "order_by_may_require_full_sort",
        ),
    ],
)
def test_limit_analysis_does_not_confuse_output_cap_with_scan_reduction(
    db_type: DbType,
    sql: str,
    shape: str,
    pushdown: bool,
    reason: str,
) -> None:
    analysis = analyze_database_row_limit(sql, db_type)

    assert analysis.query_shape == shape
    assert analysis.limit_pushdown is pushdown
    assert analysis.limit_pushdown_reason == reason
    assert analysis.output_limit_applied is True


@pytest.mark.parametrize("db_type", [DbType.MYSQL, DbType.DM, DbType.POSTGRESQL])
def test_aggregate_limit_is_applied_only_to_final_output(db_type: DbType) -> None:
    limited = apply_database_row_limit(
        "SELECT category, COUNT(*) AS item_count FROM items GROUP BY category",
        db_type,
        101,
    )

    assert limited.count("101") == 1
    assert limited.rfind("101") > limited.rfind("GROUP BY")
