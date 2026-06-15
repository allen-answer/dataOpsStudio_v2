from __future__ import annotations

from app.domain.compare_infer import (
    ConflictKind,
    MappingReason,
    PrimaryKeyReason,
    TableSuggestionReason,
    column_types_compatible,
    infer_column_mappings,
    infer_compare_draft,
    infer_table_pair_suggestions,
)
from app.domain.schema import Column, ColumnType, Index, Table


def test_column_mapping_infers_exact_normalized_alias_and_type_compatible() -> None:
    mappings = infer_column_mappings(
        [
            Column(name="id", type=ColumnType.INTEGER),
            Column(name="UserName", type=ColumnType.STRING),
            Column(name="src_amount", type=ColumnType.DECIMAL),
            Column(name="\u7528\u6237\u7f16\u53f7", type=ColumnType.INTEGER),
            Column(name="customer_name", type=ColumnType.STRING),
            Column(name="flag", type=ColumnType.BOOLEAN),
        ],
        [
            Column(name="ID", type=ColumnType.INTEGER),
            Column(name="user_name", type=ColumnType.STRING),
            Column(name="amount", type=ColumnType.FLOAT),
            Column(name="user_id", type=ColumnType.INTEGER),
            Column(name="customer_full_name", type=ColumnType.STRING),
            Column(name="flag_text", type=ColumnType.STRING),
        ],
    )

    by_pair = {(item.source_column, item.target_column): item for item in mappings}
    assert by_pair[("id", "ID")].reason is MappingReason.EXACT
    assert by_pair[("UserName", "user_name")].reason is MappingReason.NORMALIZED
    assert by_pair[("src_amount", "amount")].reason is MappingReason.NORMALIZED
    assert by_pair[("\u7528\u6237\u7f16\u53f7", "user_id")].reason is MappingReason.NORMALIZED
    assert by_pair[("customer_name", "customer_full_name")].reason is MappingReason.TYPE_COMPATIBLE
    assert ("flag", "flag_text") not in by_pair


def test_column_type_compatibility_matrix_filters_incompatible_pairs() -> None:
    assert column_types_compatible(ColumnType.INTEGER, ColumnType.DECIMAL)
    assert column_types_compatible(ColumnType.FLOAT, ColumnType.DECIMAL)
    assert column_types_compatible(ColumnType.DATE, ColumnType.DATETIME)
    assert column_types_compatible(ColumnType.BOOLEAN, ColumnType.INTEGER)
    assert not column_types_compatible(ColumnType.STRING, ColumnType.INTEGER)
    assert not column_types_compatible(ColumnType.JSON, ColumnType.STRING)
    assert not column_types_compatible(ColumnType.UNKNOWN, ColumnType.STRING)


def test_column_mapping_marks_one_to_many_conflicts() -> None:
    mappings = infer_column_mappings(
        [Column(name="user_id", type=ColumnType.INTEGER)],
        [
            Column(name="src_user_id", type=ColumnType.INTEGER),
            Column(name="target_user_id", type=ColumnType.INTEGER),
        ],
    )

    assert len(mappings) == 2
    assert {item.target_column for item in mappings} == {"src_user_id", "target_user_id"}
    assert all(item.conflict for item in mappings)
    assert {item.conflict_kind for item in mappings} == {ConflictKind.ONE_TO_MANY}


def test_primary_key_inference_prefers_cached_primary_key_columns() -> None:
    draft = infer_compare_draft(
        source_columns=[
            Column(name="id", type=ColumnType.INTEGER, primary_key=True),
            Column(name="name", type=ColumnType.STRING),
        ],
        target_columns=[
            Column(name="ID", type=ColumnType.INTEGER, primary_key=True),
            Column(name="name", type=ColumnType.STRING),
        ],
    )

    assert not draft.needs_manual_pk
    assert draft.pk_candidates[0].reason is PrimaryKeyReason.PRIMARY_KEY
    assert draft.pk_candidates[0].source_columns == ["id"]
    assert draft.pk_candidates[0].target_columns == ["ID"]
    assert draft.compare_rules.key_columns == ["id"]
    assert draft.compare_rules.column_mappings["id"] == "ID"


def test_primary_key_inference_uses_unique_indexes_when_no_primary_key() -> None:
    draft = infer_compare_draft(
        source_columns=[
            Column(name="email", type=ColumnType.STRING),
            Column(name="name", type=ColumnType.STRING),
        ],
        target_columns=[
            Column(name="email", type=ColumnType.STRING),
            Column(name="name", type=ColumnType.STRING),
        ],
        source_indexes=[Index(name="uq_source_email", columns=["email"], is_unique=True)],
        target_indexes=[Index(name="uq_target_email", columns=["email"], is_unique=True)],
    )

    assert not draft.needs_manual_pk
    assert draft.pk_candidates[0].reason is PrimaryKeyReason.UNIQUE_INDEX
    assert draft.pk_candidates[0].source_columns == ["email"]


def test_primary_key_inference_returns_needs_manual_without_safe_candidate() -> None:
    draft = infer_compare_draft(
        source_columns=[Column(name="name", type=ColumnType.STRING)],
        target_columns=[Column(name="name", type=ColumnType.STRING)],
    )

    assert draft.needs_manual_pk
    assert draft.pk_candidates == []
    assert draft.compare_rules.key_columns == []


def test_table_pair_suggestions_match_exact_and_normalized_names() -> None:
    suggestions = infer_table_pair_suggestions(
        [
            Table(schema_name="src", name="orders"),
            Table(schema_name="src", name="src_customer"),
        ],
        [
            Table(schema_name="dst", name="orders"),
            Table(schema_name="dst", name="customer"),
        ],
    )

    by_pair = {(item.source_table, item.target_table): item for item in suggestions}
    assert by_pair[("orders", "orders")].reason is TableSuggestionReason.EXACT
    assert by_pair[("src_customer", "customer")].reason is TableSuggestionReason.NORMALIZED
