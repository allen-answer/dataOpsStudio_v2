from __future__ import annotations

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
