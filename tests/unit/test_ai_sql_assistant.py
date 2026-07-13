from app.domain.ai import AiResponse, ReasoningMode
from app.domain.schema import Column, ColumnType
from app.services.ai.sql_assistant import (
    TableSchema,
    classify_reasoning_mode,
    diagnose_empty_response,
    extract_sql,
    rank_table_candidates,
    should_repair,
    validate_generated_sql,
)

USERS = TableSchema(
    schema_name="app",
    table_name="users",
    columns=(
        Column(name="id", type=ColumnType.INTEGER),
        Column(name="name", type=ColumnType.STRING),
    ),
)
ORDERS = TableSchema(
    schema_name="app",
    table_name="orders",
    columns=(
        Column(name="id", type=ColumnType.INTEGER),
        Column(name="customer_id", type=ColumnType.INTEGER),
        Column(name="amount", type=ColumnType.DECIMAL),
    ),
)


def test_reasoning_mode_is_disabled_only_for_simple_single_table_query() -> None:
    assert classify_reasoning_mode("list user names", [USERS]) is ReasoningMode.DISABLED
    assert (
        classify_reasoning_mode("sum order amount by customer", [ORDERS]) is ReasoningMode.ENABLED
    )
    assert (
        classify_reasoning_mode("join users and orders", [USERS, ORDERS]) is ReasoningMode.ENABLED
    )


def test_candidate_ranking_prefers_editor_then_table_then_column_match() -> None:
    ranked = rank_table_candidates(
        "customer order amount",
        "SELECT * FROM app.users",
        [ORDERS, USERS],
        limit=10,
    )
    assert ranked[0].table_name == "users"
    assert ranked[0].matched_by == ("editor_reference",)
    assert ranked[1].table_name == "orders"
    assert "table_name" in ranked[1].matched_by
    assert "column_name" in ranked[1].matched_by


def test_extract_sql_accepts_fence_and_rejects_multiple_statements() -> None:
    assert extract_sql("```sql\nSELECT id FROM app.users\n```") == "SELECT id FROM app.users"
    assert extract_sql("SELECT 1; SELECT 2") is None


def test_validation_understands_alias_cte_and_unknown_identifiers() -> None:
    valid = validate_generated_sql(
        "WITH paid AS (SELECT customer_id, amount FROM app.orders) "
        "SELECT u.name, p.amount FROM app.users u JOIN paid p ON p.customer_id = u.id",
        dialect="mysql",
        tables=[USERS, ORDERS],
    )
    assert valid.ok is True
    assert valid.readonly == "passed"
    assert valid.tables == "passed"
    assert valid.columns == "passed"

    unknown = validate_generated_sql(
        "SELECT missing FROM app.users", dialect="mysql", tables=[USERS]
    )
    assert unknown.ok is False
    assert unknown.diagnostic_code == "sql_unknown_column"


def test_validation_rejects_write_and_unknown_table() -> None:
    write = validate_generated_sql("DELETE FROM app.users", dialect="mysql", tables=[USERS])
    assert write.diagnostic_code == "sql_not_readonly"
    unknown = validate_generated_sql("SELECT id FROM app.accounts", dialect="mysql", tables=[USERS])
    assert unknown.diagnostic_code == "sql_unknown_table"


def test_response_diagnostics_and_repair_policy_are_bounded() -> None:
    truncated = AiResponse(content="", finish_reason="length", reasoning_chars=20)
    reasoning_only = AiResponse(content="", finish_reason="stop", reasoning_chars=20)
    assert diagnose_empty_response(truncated) == "provider_output_truncated"
    assert diagnose_empty_response(reasoning_only) == "provider_reasoning_only"
    assert should_repair("provider_output_truncated", attempts=1) is True
    assert should_repair("sql_parse_failed", attempts=1) is True
    assert should_repair("provider_auth_failed", attempts=1) is False
    assert should_repair("sql_parse_failed", attempts=2) is False
