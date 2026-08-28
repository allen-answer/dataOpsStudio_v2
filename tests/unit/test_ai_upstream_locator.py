"""AI 组装上游定位 SQL 的纯函数单测(app/domain/ai_upstream_locator.py)。

重点不是"AI 答得对不对"(不可测),而是**约束是否守得住**:
prompt 里不出现任何行值、模型产出必须过四道校验闸、自报置信度只能下调。
"""

from __future__ import annotations

from app.domain.ai_upstream_locator import (
    PK_PLACEHOLDER,
    build_ai_upstream_prompt,
    build_lineage_context_payload,
    combine_confidence,
    fill_pk_placeholder,
    parse_ai_upstream_response,
    validate_ai_sql,
)
from app.domain.compare_upstream_sql import CONFIDENCE_FLOOR, UpstreamEdge

EDGE = UpstreamEdge(
    edge_id="e1",
    run_id="run-1",
    source_table="ods.orders_raw",
    source_column="amt_fen",
    target_table="dwd.orders_clean",
    target_column="amount",
    transformation="DIRECT",
    transformation_subtype="EXPRESSION",
    inference_status="inferred",
    confidence=0.74,
)

ALLOWED = {"ods.orders_raw", "dwd.orders_clean", "order_id", "amt_fen", "amount", "o"}


def test_context_payload_carries_only_structure_no_row_values() -> None:
    payload, truncated = build_lineage_context_payload(
        focus_table="ads.orders_agg",
        blocked_table="ods.orders_raw",
        key_columns=["order_id"],
        edges=[EDGE],
    )

    assert truncated is False
    assert payload["blocked_upstream_table"] == "ods.orders_raw"
    edges = payload["column_edges"]
    assert isinstance(edges, list)
    # 结构字段齐全,且没有任何承载行值的键
    assert set(edges[0]) == {
        "source",
        "target",
        "transformation",
        "inference_status",
        "confidence",
    }


def test_prompt_never_contains_primary_key_values() -> None:
    """L4 铁律的机器化断言:prompt 里不可能出现主键值 —— 函数根本没有这个入参。"""
    payload, _ = build_lineage_context_payload(
        focus_table="ads.orders_agg",
        blocked_table="ods.orders_raw",
        key_columns=["order_id"],
        edges=[EDGE],
    )
    prompt, sql_text, truncated = build_ai_upstream_prompt(
        dialect="mysql",
        lineage_payload=payload,
        archived_sql="INSERT INTO dwd.orders_clean SELECT amt_fen / 100 FROM ods.orders_raw",
    )

    for pk_value in ("100234", "100501", "101377"):
        assert pk_value not in prompt
    assert PK_PLACEHOLDER in prompt
    assert sql_text is not None
    assert truncated is False


def test_prompt_truncates_oversized_archived_sql() -> None:
    _, sql_text, truncated = build_ai_upstream_prompt(
        dialect="mysql",
        lineage_payload={},
        archived_sql="x" * 50,
        max_sql_chars=10,
    )
    assert truncated is True
    assert sql_text is not None and len(sql_text) == 10


def test_parse_extracts_template_and_self_reported_confidence() -> None:
    result = parse_ai_upstream_response(
        "Inverted the fen-to-yuan division.\n"
        f"```sql\nSELECT * FROM ods.orders_raw WHERE order_id IN {PK_PLACEHOLDER}\n```\n"
        "confidence: 0.8\nAmounts are stored in cents."
    )

    assert result is not None
    assert PK_PLACEHOLDER in result.sql_template
    assert result.ai_confidence == 0.8
    assert result.explanation is not None
    assert "confidence" not in result.explanation.lower()


def test_parse_without_confidence_falls_back_to_floor() -> None:
    result = parse_ai_upstream_response(f"```sql\nSELECT 1 FROM t WHERE k IN {PK_PLACEHOLDER}\n```")
    assert result is not None
    # 缺省不等于自信
    assert result.ai_confidence == CONFIDENCE_FLOOR


def test_parse_returns_none_without_a_sql_fence() -> None:
    assert parse_ai_upstream_response("I could not work out the mapping, sorry.") is None


def test_validate_accepts_a_single_read_only_select_within_lineage() -> None:
    sql = f"SELECT o.order_id, o.amt_fen FROM ods.orders_raw o WHERE o.order_id IN {PK_PLACEHOLDER}"
    assert validate_ai_sql(sql, dialect="mysql", allowed_identifiers=ALLOWED) is None


def test_validate_rejects_missing_placeholder() -> None:
    sql = "SELECT * FROM ods.orders_raw WHERE order_id IN (1, 2)"
    assert (
        validate_ai_sql(sql, dialect="mysql", allowed_identifiers=ALLOWED)
        == "missing_pk_placeholder"
    )


def test_validate_rejects_write_statements() -> None:
    sql = f"DELETE FROM ods.orders_raw WHERE order_id IN {PK_PLACEHOLDER}"
    assert (
        validate_ai_sql(sql, dialect="mysql", allowed_identifiers=ALLOWED) == "not_read_only_select"
    )


def test_validate_rejects_multiple_statements() -> None:
    sql = f"SELECT 1 FROM ods.orders_raw WHERE order_id IN {PK_PLACEHOLDER}; DROP TABLE x"
    assert validate_ai_sql(sql, dialect="mysql", allowed_identifiers=ALLOWED) in {
        "not_single_statement",
        "unparsable_ai_sql",
    }


def test_validate_rejects_identifiers_outside_the_lineage_graph() -> None:
    sql = f"SELECT * FROM secret.credentials WHERE order_id IN {PK_PLACEHOLDER}"
    assert (
        validate_ai_sql(sql, dialect="mysql", allowed_identifiers=ALLOWED)
        == "identifier_out_of_lineage"
    )


def test_fill_placeholder_injects_locally_built_literals() -> None:
    sql = fill_pk_placeholder(
        f"SELECT * FROM ods.orders_raw WHERE order_id IN {PK_PLACEHOLDER}",
        pk_tuples_sql="(100234, 100501)",
    )
    assert sql.endswith("IN (100234, 100501)")
    assert PK_PLACEHOLDER not in sql


def test_ai_confidence_can_only_lower_the_deterministic_value() -> None:
    # 自报再高也不能超过确定性边的最小值
    assert combine_confidence(0.9, 1.0) == 0.9
    assert combine_confidence(0.9, 2.0) == 0.9
    # 自报低则整体被拉低,但不低于下限
    assert combine_confidence(0.9, 0.8) == 0.9 * 0.8
    assert combine_confidence(0.9, 0.0) == CONFIDENCE_FLOOR
