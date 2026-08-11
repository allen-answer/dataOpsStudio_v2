from __future__ import annotations

import pytest

from app.domain.lineage import (
    LineageParseRequest,
    TransformationKind,
    TransformationSubtype,
    analyze_sql_lineage,
    schema_from_metadata_cache_rows,
    split_plsql_statements,
)
from app.domain.lineage.parser import PARSE_ERRORS_SUMMARY_LIMIT


def test_lineage_report_preserves_v1_envelope_fields() -> None:
    report = analyze_sql_lineage(
        LineageParseRequest(
            sql_text="INSERT INTO app.target_orders (id) SELECT id FROM app.orders",
            dialect="mysql",
            schema=_schema(),
            default_schema="app",
        )
    )

    assert set(report.model_dump()) >= {
        "statement_count",
        "tables",
        "columns",
        "insert_mappings",
        "target_summary",
        "table_roles",
        "joins",
        "filters",
        "group_by",
        "unions",
        "variables",
        "aliases",
        "dynamic_sql_count",
        "dynamic_sql_segments",
        "procedure_segments",
        "graph_edges",
        "graph_groups",
        "parse_errors",
        "warnings",
        "statements",
        "semantic_lineage",
        "report",
    }


def test_table_edges_column_mappings_and_transformations() -> None:
    report = analyze_sql_lineage(
        LineageParseRequest(
            sql_text=(
                "INSERT INTO app.target_orders (id, amount2) "
                "SELECT o.id, SUM(o.amount) "
                "FROM app.orders o JOIN app.customers c ON o.customer_id = c.id "
                "WHERE c.name IS NOT NULL GROUP BY o.id"
            ),
            dialect="mysql",
            schema=_schema(),
            default_schema="app",
        )
    )

    assert report.parse_errors == []
    assert {(edge["source_table"], edge["target_table"]) for edge in report.graph_edges} == {
        ("app.orders", "app.target_orders"),
        ("app.customers", "app.target_orders"),
    }
    direct_id = _mapping(report.insert_mappings, "id", "app.orders", "id")
    assert direct_id["transformation"] == TransformationKind.DIRECT
    assert direct_id["transformation_subtype"] == TransformationSubtype.DIRECT
    amount = _mapping(report.insert_mappings, "amount2", "app.orders", "amount")
    assert amount["transformation"] == TransformationKind.DIRECT
    assert amount["transformation_subtype"] == TransformationSubtype.AGGREGATION
    filter_mapping = _mapping(report.insert_mappings, "amount2", "app.customers", "name")
    assert filter_mapping["transformation"] == TransformationKind.INDIRECT
    assert filter_mapping["transformation_subtype"] == TransformationSubtype.FILTER
    join_mapping = _mapping(report.insert_mappings, "id", "app.customers", "id")
    assert join_mapping["transformation"] == TransformationKind.INDIRECT
    assert join_mapping["transformation_subtype"] == TransformationSubtype.JOIN


def test_select_star_is_expanded_by_schema_aware_qualify() -> None:
    report = analyze_sql_lineage(
        LineageParseRequest(
            sql_text="INSERT INTO app.target_orders SELECT * FROM app.orders",
            dialect="mysql",
            schema=_schema(),
            default_schema="app",
        )
    )

    assert report.parse_errors == []
    assert _mapping(report.insert_mappings, "id", "app.orders", "id")
    assert _mapping(report.insert_mappings, "amount", "app.orders", "amount")
    assert _mapping(report.insert_mappings, "customer_id", "app.orders", "customer_id")


def test_missing_schema_is_unsupported_parse_error() -> None:
    report = analyze_sql_lineage(
        LineageParseRequest(
            sql_text="INSERT INTO app.target_orders SELECT id FROM app.unknown_orders",
            dialect="mysql",
            schema=_schema(),
            default_schema="app",
        )
    )

    assert report.insert_mappings == []
    assert report.parse_errors[0]["error_type"] == "unsupported_schema"
    assert report.parse_errors[0]["unsupported"] is True
    assert "app.unknown_orders" in report.parse_errors[0]["message"]


def test_report_summary_includes_parse_error_details() -> None:
    report = analyze_sql_lineage(
        LineageParseRequest(
            sql_text="INSERT INTO app.target_orders SELECT id FROM app.unknown_orders",
            dialect="mysql",
            schema=_schema(),
            default_schema="app",
        )
    )

    assert report.report is not None
    assert report.report["parse_error_count"] == 1
    assert report.report["parse_errors"] == report.parse_errors
    detail = report.report["parse_errors"][0]
    assert detail["error_type"] == "unsupported_schema"
    assert detail["statement_index"] == 0
    assert detail["statement_type"] == "INSERT"
    assert "app.unknown_orders" in detail["message"]


def test_parse_error_detail_includes_line_column_and_token() -> None:
    # 模板变量发生在错误 token 之前;规范化必须保持原长度,否则 col 会向前偏移。
    sql = "SELECT * FROM app.orders WHERE amount <= ${fv_qmrq_m} AND ("
    report = analyze_sql_lineage(
        LineageParseRequest(
            sql_text=sql,
            dialect="dm",
            schema=_schema(),
            default_schema="app",
        )
    )

    assert len(report.parse_errors) == 1
    message = report.parse_errors[0]["message"]
    assert message.startswith("ParseError:")
    assert "line 1" in message
    assert f"col {len(sql)}" in message
    assert "token='('" in message


def test_report_summary_truncates_parse_error_details() -> None:
    statements = ";\n".join(
        f"INSERT INTO app.target_orders SELECT id FROM app.unknown_{index}"
        for index in range(PARSE_ERRORS_SUMMARY_LIMIT + 10)
    )
    report = analyze_sql_lineage(
        LineageParseRequest(
            sql_text=statements,
            dialect="mysql",
            schema=_schema(),
            default_schema="app",
        )
    )

    assert report.report is not None
    assert report.report["parse_error_count"] == PARSE_ERRORS_SUMMARY_LIMIT + 10
    assert len(report.report["parse_errors"]) == PARSE_ERRORS_SUMMARY_LIMIT
    assert report.report["parse_errors"] == report.parse_errors[:PARSE_ERRORS_SUMMARY_LIMIT]


def test_plsql_split_rescues_insert_select_body() -> None:
    sql = """
    CREATE OR REPLACE PROCEDURE refresh_orders AS
    BEGIN
      IF 1 = 1 THEN
        NULL;
      END IF;
      INSERT INTO app.target_orders (id, amount)
      SELECT id, amount FROM app.orders;
    END;
    /
    """

    assert split_plsql_statements(sql) == [
        "INSERT INTO app.target_orders (id, amount)\n      SELECT id, amount FROM app.orders"
    ]
    report = analyze_sql_lineage(
        LineageParseRequest(
            sql_text=sql,
            dialect="oracle",
            schema=_schema(),
            default_schema="app",
        )
    )

    assert report.parse_errors == []
    assert _mapping(report.insert_mappings, "ID", "APP.ORDERS", "ID")
    assert _mapping(report.insert_mappings, "AMOUNT", "APP.ORDERS", "AMOUNT")


def test_metadata_cache_rows_convert_to_sqlglot_schema() -> None:
    schema = schema_from_metadata_cache_rows(
        [
            {
                "cache_level": "columns",
                "schema_name": "app",
                "table_name": "orders",
                "payload": [
                    {"name": "id", "type": "integer"},
                    {"name": "amount", "driver_type": "DECIMAL(12,2)"},
                ],
            }
        ]
    )

    assert schema == {"app": {"orders": {"id": "integer", "amount": "DECIMAL(12,2)"}}}


def test_dm_dialect_parses_oracle_compatible_dml() -> None:
    report = analyze_sql_lineage(
        LineageParseRequest(
            sql_text="INSERT INTO APP.TARGET_ORDERS (ID) SELECT ID FROM APP.ORDERS",
            dialect="dm",
            schema={
                "APP": {
                    "TARGET_ORDERS": {"ID": "INTEGER"},
                    "ORDERS": {"ID": "INTEGER"},
                }
            },
            default_schema="APP",
        )
    )

    assert report.parse_errors == []
    assert _mapping(report.insert_mappings, "ID", "APP.ORDERS", "ID")


def test_dm_template_variable_is_normalized_before_sqlglot() -> None:
    report = analyze_sql_lineage(
        LineageParseRequest(
            sql_text=(
                "INSERT INTO APP.TARGET_ORDERS (ID) "
                "SELECT ID FROM APP.ORDERS WHERE AMOUNT <= ${fv_qmrq_m}"
            ),
            dialect="dm",
            schema=_schema(),
            default_schema="app",
        )
    )

    assert report.parse_errors == []
    assert report.variables == [
        {"name": "fv_qmrq_m", "placeholder": "fv_qmrq_m", "assigned_value": ""}
    ]
    assert {(edge["source_table"], edge["target_table"]) for edge in report.graph_edges} == {
        ("APP.ORDERS", "APP.TARGET_ORDERS")
    }
    assert _mapping(report.insert_mappings, "ID", "APP.ORDERS", "ID")
    amount_filter = _mapping(report.insert_mappings, "ID", "APP.ORDERS", "AMOUNT")
    assert amount_filter["transformation"] == TransformationKind.INDIRECT
    assert amount_filter["transformation_subtype"] == TransformationSubtype.FILTER


def test_postgres_dialect_parses_pg_specific_syntax() -> None:
    # ::cast 与 ON CONFLICT 是 postgres 专属语法,mysql/oracle reader 均无法解析,
    # 解析成功即证明真走了 sqlglot postgres 方言(而非白名单外回退)
    report = analyze_sql_lineage(
        LineageParseRequest(
            sql_text=(
                "INSERT INTO app.target_orders (id, amount2) "
                "SELECT o.id::int, o.amount FROM app.orders o "
                "ON CONFLICT (id) DO NOTHING"
            ),
            dialect="postgres",
            schema=_schema(),
            default_schema="app",
        )
    )

    assert report.parse_errors == []
    assert _mapping(report.insert_mappings, "id", "app.orders", "id")
    assert _mapping(report.insert_mappings, "amount2", "app.orders", "amount")


def test_postgresql_alias_normalizes_to_postgres() -> None:
    # datasource.db_type 值是 "postgresql",必须与 sqlglot 方言名 "postgres" 等价
    report = analyze_sql_lineage(
        LineageParseRequest(
            sql_text="INSERT INTO app.target_orders (id) SELECT id FROM app.orders",
            dialect="postgresql",
            schema=_schema(),
            default_schema="app",
        )
    )

    assert report.parse_errors == []
    assert _mapping(report.insert_mappings, "id", "app.orders", "id")


def test_lenient_mode_emits_table_level_edges_without_metadata() -> None:
    # 宽松模式:引用表全部不在元数据缓存 → 不判 unsupported_schema,
    # 降级产出表级边 + lenient_table_level warning(列级跳过)
    report = analyze_sql_lineage(
        LineageParseRequest(
            sql_text=(
                "INSERT INTO kgrp.r_cisp_g3001_t "
                "SELECT * FROM kgrp.m_r_cisp_g3001_t a WHERE a.period = ${v_period_m}"
            ),
            dialect="oracle",
            schema={},
            default_schema=None,
            lenient=True,
        )
    )

    assert report.parse_errors == []
    assert {(edge["source_table"], edge["target_table"]) for edge in report.graph_edges} == {
        ("kgrp.m_r_cisp_g3001_t", "kgrp.r_cisp_g3001_t")
    }
    assert report.insert_mappings == []  # 列级诚实跳过
    assert any(w["code"] == "lenient_table_level" for w in report.warnings)
    assert report.target_summary[0].table == "kgrp.r_cisp_g3001_t"
    assert report.target_summary[0].operation == "insert"


def test_strict_mode_still_rejects_missing_metadata() -> None:
    report = analyze_sql_lineage(
        LineageParseRequest(
            sql_text="INSERT INTO kgrp.t1 SELECT * FROM kgrp.t2",
            dialect="oracle",
            schema={},
            default_schema=None,
        )
    )

    assert report.graph_edges == []
    assert any(err["error_type"] == "unsupported_schema" for err in report.parse_errors)


def test_alias_prefixed_insert_rewritten_and_parsed() -> None:
    # Oracle 别名前缀 INSERT(用户真实语料 4 例):insert into t a (a.c1, ...)
    report = analyze_sql_lineage(
        LineageParseRequest(
            sql_text=(
                "insert into dw.t_etl_y1_yyb_dlmmzqk a (a.yyb, a.dlmmzqk) "
                "select b.yyb, b.dlmmzqk from dw.src_yyb b"
            ),
            dialect="oracle",
            schema={},
            default_schema=None,
            lenient=True,
        )
    )

    assert report.parse_errors == []
    assert {(edge["source_table"], edge["target_table"]) for edge in report.graph_edges} == {
        ("dw.src_yyb", "dw.t_etl_y1_yyb_dlmmzqk")
    }


def test_unsupported_dialect_rejected() -> None:
    with pytest.raises(ValueError, match="lineage dialect"):
        analyze_sql_lineage(
            LineageParseRequest(
                sql_text="SELECT 1",
                dialect="sqlite",
                schema=_schema(),
                default_schema="app",
            )
        )


def test_ctas_emits_edges_and_column_mappings_for_new_target_table() -> None:
    report = analyze_sql_lineage(
        LineageParseRequest(
            sql_text=(
                "CREATE TABLE app.order_stats AS "
                "SELECT o.id AS order_id, SUM(o.amount) AS total_amount "
                "FROM app.orders o GROUP BY o.id"
            ),
            dialect="mysql",
            schema=_schema(),
            default_schema="app",
        )
    )

    assert report.parse_errors == []
    assert {(edge["source_table"], edge["target_table"]) for edge in report.graph_edges} == {
        ("app.orders", "app.order_stats")
    }
    assert report.target_summary[0].table == "app.order_stats"
    assert report.target_summary[0].operation == "create"
    direct = _mapping(report.insert_mappings, "order_id", "app.orders", "id")
    assert direct["transformation"] == TransformationKind.DIRECT
    assert direct["transformation_subtype"] == TransformationSubtype.DIRECT
    total = _mapping(report.insert_mappings, "total_amount", "app.orders", "amount")
    assert total["transformation_subtype"] == TransformationSubtype.AGGREGATION


def test_ctas_dm_dialect_with_join_and_filter() -> None:
    report = analyze_sql_lineage(
        LineageParseRequest(
            sql_text=(
                "CREATE TABLE app.order_names AS "
                "SELECT o.id, c.name FROM app.orders o "
                "JOIN app.customers c ON o.customer_id = c.id "
                "WHERE c.name IS NOT NULL"
            ),
            dialect="dm",
            schema=_schema(),
            default_schema="app",
        )
    )

    assert report.parse_errors == []
    assert {(edge["source_table"], edge["target_table"]) for edge in report.graph_edges} == {
        ("APP.ORDERS", "APP.ORDER_NAMES"),
        ("APP.CUSTOMERS", "APP.ORDER_NAMES"),
    }
    assert _mapping(report.insert_mappings, "ID", "APP.ORDERS", "ID")
    assert _mapping(report.insert_mappings, "NAME", "APP.CUSTOMERS", "NAME")
    join_mapping = _mapping(report.insert_mappings, "NAME", "APP.ORDERS", "CUSTOMER_ID")
    assert join_mapping["transformation"] == TransformationKind.INDIRECT
    assert join_mapping["transformation_subtype"] == TransformationSubtype.JOIN


def test_ctas_missing_source_table_is_unsupported_schema_but_target_is_not() -> None:
    report = analyze_sql_lineage(
        LineageParseRequest(
            sql_text="CREATE TABLE app.brand_new AS SELECT id FROM app.unknown_orders",
            dialect="mysql",
            schema=_schema(),
            default_schema="app",
        )
    )

    assert report.graph_edges == []
    assert len(report.parse_errors) == 1
    assert report.parse_errors[0]["error_type"] == "unsupported_schema"
    assert report.parse_errors[0]["statement_type"] == "CREATE"
    assert "app.unknown_orders" in report.parse_errors[0]["message"]
    assert "app.brand_new" not in report.parse_errors[0]["message"]


def test_merge_using_table_emits_edges_and_when_clause_mappings() -> None:
    report = analyze_sql_lineage(
        LineageParseRequest(
            sql_text=(
                "MERGE INTO app.target_orders t USING app.orders o ON t.id = o.id "
                "WHEN MATCHED THEN UPDATE SET t.amount = o.amount "
                "WHEN NOT MATCHED THEN INSERT (id, amount) VALUES (o.id, o.amount)"
            ),
            dialect="oracle",
            schema=_schema(),
            default_schema="app",
        )
    )

    assert report.parse_errors == []
    assert {(edge["source_table"], edge["target_table"]) for edge in report.graph_edges} == {
        ("APP.ORDERS", "APP.TARGET_ORDERS")
    }
    assert report.target_summary[0].operation == "merge"
    update_mapping = _mapping(report.insert_mappings, "AMOUNT", "APP.ORDERS", "AMOUNT")
    assert update_mapping["transformation"] == TransformationKind.DIRECT
    assert update_mapping["transformation_subtype"] == TransformationSubtype.DIRECT
    assert _mapping(report.insert_mappings, "ID", "APP.ORDERS", "ID")
    on_mapping = _mapping(report.insert_mappings, "AMOUNT", "APP.ORDERS", "ID")
    assert on_mapping["transformation"] == TransformationKind.INDIRECT
    assert on_mapping["transformation_subtype"] == TransformationSubtype.JOIN


def test_merge_using_subquery_resolves_through_derived_table() -> None:
    report = analyze_sql_lineage(
        LineageParseRequest(
            sql_text=(
                "MERGE INTO app.target_orders t USING ("
                "SELECT o.id, SUM(o.amount) AS total FROM app.orders o GROUP BY o.id"
                ") s ON t.id = s.id "
                "WHEN MATCHED THEN UPDATE SET t.amount = s.total"
            ),
            dialect="dm",
            schema=_schema(),
            default_schema="app",
        )
    )

    assert report.parse_errors == []
    assert {(edge["source_table"], edge["target_table"]) for edge in report.graph_edges} == {
        ("APP.ORDERS", "APP.TARGET_ORDERS")
    }
    total = _mapping(report.insert_mappings, "AMOUNT", "APP.ORDERS", "AMOUNT")
    assert total["transformation"] == TransformationKind.DIRECT
    assert total["transformation_subtype"] == TransformationSubtype.AGGREGATION


def test_merge_insert_without_column_list_is_parse_error() -> None:
    report = analyze_sql_lineage(
        LineageParseRequest(
            sql_text=(
                "MERGE INTO app.target_orders t USING app.orders o ON t.id = o.id "
                "WHEN NOT MATCHED THEN INSERT VALUES (o.id, o.amount, o.customer_id)"
            ),
            dialect="oracle",
            schema=_schema(),
            default_schema="app",
        )
    )

    assert {(edge["source_table"], edge["target_table"]) for edge in report.graph_edges} == {
        ("APP.ORDERS", "APP.TARGET_ORDERS")
    }
    errors = [e for e in report.parse_errors if e["error_type"] == "unsupported_merge"]
    assert len(errors) == 1
    assert errors[0]["statement_type"] == "MERGE"
    assert errors[0]["unsupported"] is True


def test_mysql_multi_table_update_with_join() -> None:
    report = analyze_sql_lineage(
        LineageParseRequest(
            sql_text=(
                "UPDATE app.target_orders t1 JOIN app.orders t2 ON t1.id = t2.id "
                "SET t1.amount = t2.amount WHERE t2.customer_id > 0"
            ),
            dialect="mysql",
            schema=_schema(),
            default_schema="app",
        )
    )

    assert report.parse_errors == []
    assert {(edge["source_table"], edge["target_table"]) for edge in report.graph_edges} == {
        ("app.orders", "app.target_orders")
    }
    assert report.target_summary[0].operation == "update"
    direct = _mapping(report.insert_mappings, "amount", "app.orders", "amount")
    assert direct["transformation"] == TransformationKind.DIRECT
    assert direct["transformation_subtype"] == TransformationSubtype.DIRECT
    join_mapping = _mapping(report.insert_mappings, "amount", "app.orders", "id")
    assert join_mapping["transformation"] == TransformationKind.INDIRECT
    assert join_mapping["transformation_subtype"] == TransformationSubtype.JOIN
    filter_mapping = _mapping(report.insert_mappings, "amount", "app.orders", "customer_id")
    assert filter_mapping["transformation"] == TransformationKind.INDIRECT
    assert filter_mapping["transformation_subtype"] == TransformationSubtype.FILTER


def test_delete_without_where_emits_full_wipe_target_summary() -> None:
    # 无 WHERE 的 DELETE = 全表清空,供 L-4 判定 DELETE+INSERT 全量重刷。
    report = analyze_sql_lineage(
        LineageParseRequest(
            sql_text="DELETE FROM app.target_orders",
            dialect="mysql",
            schema=_schema(),
            default_schema="app",
        )
    )

    assert report.parse_errors == []
    assert report.graph_edges == []  # DELETE 无 source,不产血缘边
    assert len(report.target_summary) == 1
    summary = report.target_summary[0]
    assert summary.table == "app.target_orders"
    assert summary.operation == "delete"
    assert summary.has_where is False


def test_delete_with_where_marks_has_where_true() -> None:
    report = analyze_sql_lineage(
        LineageParseRequest(
            sql_text="DELETE FROM app.target_orders WHERE customer_id > 0",
            dialect="mysql",
            schema=_schema(),
            default_schema="app",
        )
    )

    assert report.parse_errors == []
    assert report.target_summary[0].operation == "delete"
    assert report.target_summary[0].has_where is True


def test_truncate_table_emits_truncate_target_summary() -> None:
    report = analyze_sql_lineage(
        LineageParseRequest(
            sql_text="TRUNCATE TABLE app.target_orders",
            dialect="oracle",
            schema=_schema(),
            default_schema="app",
        )
    )

    assert report.parse_errors == []
    assert report.graph_edges == []
    assert len(report.target_summary) == 1
    summary = report.target_summary[0]
    assert summary.table == "app.target_orders"
    assert summary.operation == "truncate"
    assert summary.has_where is None


def test_lenient_mode_delete_and_truncate_still_land_target_summary() -> None:
    # 提前短路 → strict/lenient 两路都覆盖;缺元数据也照样落 target_summary。
    delete_report = analyze_sql_lineage(
        LineageParseRequest(
            sql_text="DELETE FROM kgrp.r_cisp_g3001_t",
            dialect="oracle",
            schema={},
            default_schema=None,
            lenient=True,
        )
    )
    assert delete_report.parse_errors == []
    assert delete_report.target_summary[0].table == "kgrp.r_cisp_g3001_t"
    assert delete_report.target_summary[0].operation == "delete"
    assert delete_report.target_summary[0].has_where is False

    truncate_report = analyze_sql_lineage(
        LineageParseRequest(
            sql_text="TRUNCATE TABLE kgrp.r_cisp_g3001_t",
            dialect="oracle",
            schema={},
            default_schema=None,
            lenient=True,
        )
    )
    assert truncate_report.parse_errors == []
    assert truncate_report.target_summary[0].table == "kgrp.r_cisp_g3001_t"
    assert truncate_report.target_summary[0].operation == "truncate"
    assert truncate_report.target_summary[0].has_where is None


def test_mysql_multi_table_update_comma_join() -> None:
    report = analyze_sql_lineage(
        LineageParseRequest(
            sql_text=(
                "UPDATE app.target_orders t1, app.orders t2 "
                "SET t1.amount = t2.amount WHERE t1.id = t2.id"
            ),
            dialect="mysql",
            schema=_schema(),
            default_schema="app",
        )
    )

    assert report.parse_errors == []
    assert {(edge["source_table"], edge["target_table"]) for edge in report.graph_edges} == {
        ("app.orders", "app.target_orders")
    }
    assert _mapping(report.insert_mappings, "amount", "app.orders", "amount")
    filter_mapping = _mapping(report.insert_mappings, "amount", "app.orders", "id")
    assert filter_mapping["transformation"] == TransformationKind.INDIRECT


def test_oracle_correlated_subquery_update() -> None:
    report = analyze_sql_lineage(
        LineageParseRequest(
            sql_text=(
                "UPDATE app.target_orders t SET amount = ("
                "SELECT SUM(o.amount) FROM app.orders o WHERE o.id = t.id)"
            ),
            dialect="oracle",
            schema=_schema(),
            default_schema="app",
        )
    )

    assert report.parse_errors == []
    assert {(edge["source_table"], edge["target_table"]) for edge in report.graph_edges} == {
        ("APP.ORDERS", "APP.TARGET_ORDERS")
    }
    direct = _mapping(report.insert_mappings, "AMOUNT", "APP.ORDERS", "AMOUNT")
    assert direct["transformation"] == TransformationKind.DIRECT
    assert direct["transformation_subtype"] == TransformationSubtype.AGGREGATION
    filter_mapping = _mapping(report.insert_mappings, "AMOUNT", "APP.ORDERS", "ID")
    assert filter_mapping["transformation"] == TransformationKind.INDIRECT
    assert filter_mapping["transformation_subtype"] == TransformationSubtype.FILTER


def test_dm_correlated_subquery_update() -> None:
    report = analyze_sql_lineage(
        LineageParseRequest(
            sql_text=(
                "UPDATE app.target_orders t SET customer_id = ("
                "SELECT c.id FROM app.customers c WHERE c.name = 'x' AND c.id = t.customer_id)"
            ),
            dialect="dm",
            schema=_schema(),
            default_schema="app",
        )
    )

    assert report.parse_errors == []
    assert {(edge["source_table"], edge["target_table"]) for edge in report.graph_edges} == {
        ("APP.CUSTOMERS", "APP.TARGET_ORDERS")
    }
    direct = _mapping(report.insert_mappings, "CUSTOMER_ID", "APP.CUSTOMERS", "ID")
    assert direct["transformation"] == TransformationKind.DIRECT
    filter_mapping = _mapping(report.insert_mappings, "CUSTOMER_ID", "APP.CUSTOMERS", "NAME")
    assert filter_mapping["transformation"] == TransformationKind.INDIRECT
    assert filter_mapping["transformation_subtype"] == TransformationSubtype.FILTER


def test_update_from_missing_table_is_unsupported_schema() -> None:
    report = analyze_sql_lineage(
        LineageParseRequest(
            sql_text=(
                "UPDATE app.target_orders t1 JOIN app.unknown_orders t2 ON t1.id = t2.id "
                "SET t1.amount = t2.amount"
            ),
            dialect="mysql",
            schema=_schema(),
            default_schema="app",
        )
    )

    assert report.graph_edges == []
    assert report.insert_mappings == []
    assert report.parse_errors[0]["error_type"] == "unsupported_schema"
    assert report.parse_errors[0]["statement_type"] == "UPDATE"
    assert "app.unknown_orders" in report.parse_errors[0]["message"]


def test_insert_with_cte_resolves_to_physical_tables() -> None:
    report = analyze_sql_lineage(
        LineageParseRequest(
            sql_text=(
                "INSERT INTO app.target_orders (id, amount) "
                "WITH recent AS (SELECT id, amount FROM app.orders) "
                "SELECT id, amount FROM recent"
            ),
            dialect="mysql",
            schema=_schema(),
            default_schema="app",
        )
    )

    assert report.parse_errors == []
    assert {(edge["source_table"], edge["target_table"]) for edge in report.graph_edges} == {
        ("app.orders", "app.target_orders")
    }
    assert all("recent" not in edge["source_table"] for edge in report.graph_edges)
    assert _mapping(report.insert_mappings, "id", "app.orders", "id")
    assert _mapping(report.insert_mappings, "amount", "app.orders", "amount")
    assert all(m["source_table"] == "app.orders" for m in report.insert_mappings)


def test_ctas_with_cte_and_union_oracle() -> None:
    report = analyze_sql_lineage(
        LineageParseRequest(
            sql_text=(
                "CREATE TABLE app.all_ids AS "
                "WITH o_ids AS (SELECT id FROM app.orders) "
                "SELECT id FROM o_ids UNION SELECT id FROM app.customers"
            ),
            dialect="oracle",
            schema=_schema(),
            default_schema="app",
        )
    )

    assert report.parse_errors == []
    assert {(edge["source_table"], edge["target_table"]) for edge in report.graph_edges} == {
        ("APP.ORDERS", "APP.ALL_IDS"),
        ("APP.CUSTOMERS", "APP.ALL_IDS"),
    }
    assert _mapping(report.insert_mappings, "ID", "APP.ORDERS", "ID")
    assert _mapping(report.insert_mappings, "ID", "APP.CUSTOMERS", "ID")
    assert report.unions == [{"statement_index": 0}]


def test_insert_union_all_merges_sources_per_branch_subtype() -> None:
    report = analyze_sql_lineage(
        LineageParseRequest(
            sql_text=(
                "INSERT INTO app.target_orders (id) "
                "SELECT o.id FROM app.orders o "
                "UNION ALL SELECT MAX(c.id) FROM app.customers c"
            ),
            dialect="mysql",
            schema=_schema(),
            default_schema="app",
        )
    )

    assert report.parse_errors == []
    assert {(edge["source_table"], edge["target_table"]) for edge in report.graph_edges} == {
        ("app.orders", "app.target_orders"),
        ("app.customers", "app.target_orders"),
    }
    orders_mapping = _mapping(report.insert_mappings, "id", "app.orders", "id")
    assert orders_mapping["transformation_subtype"] == TransformationSubtype.DIRECT
    customers_mapping = _mapping(report.insert_mappings, "id", "app.customers", "id")
    assert customers_mapping["transformation_subtype"] == TransformationSubtype.AGGREGATION


def _mapping(
    mappings: list[dict[str, object]],
    target_column: str,
    source_table: str,
    source_column: str,
) -> dict[str, object]:
    for mapping in mappings:
        if (
            mapping["target_column"] == target_column
            and mapping["source_table"] == source_table
            and mapping["source_column"] == source_column
        ):
            return mapping
    raise AssertionError((target_column, source_table, source_column, mappings))


def _schema() -> dict[str, dict[str, dict[str, str]]]:
    return {
        "app": {
            "target_orders": {
                "id": "integer",
                "amount": "decimal",
                "amount2": "decimal",
                "customer_id": "integer",
            },
            "orders": {
                "id": "integer",
                "amount": "decimal",
                "customer_id": "integer",
            },
            "customers": {"id": "integer", "name": "string"},
        }
    }
