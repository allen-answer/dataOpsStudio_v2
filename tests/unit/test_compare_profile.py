from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.compare import CompareColumn, CompareDiffBucket, CompareDiffEvent, CompareRules
from app.domain.compare_profile import (
    DiffProfileBuilder,
    profile_for_ai,
    sample_quick_check_result,
    zero_defect_difference_rate_upper_bound,
)
from app.domain.schema import ColumnType


def test_diff_profile_counts_column_rates_offsets_and_missing_ranges() -> None:
    columns = [
        CompareColumn(name="id", type=ColumnType.INTEGER),
        CompareColumn(name="amount", type=ColumnType.DECIMAL),
        CompareColumn(name="created_at", type=ColumnType.DATETIME),
        CompareColumn(name="name", type=ColumnType.STRING),
    ]
    builder = DiffProfileBuilder(
        key_columns=[columns[0]],
        value_columns=columns,
        rules=CompareRules(),
    )
    source_time = datetime(2026, 1, 1, 8, 0, 0)
    target_time = source_time + timedelta(hours=8)
    for pk in [(2,), (3,), (5,)]:
        builder.observe(CompareDiffEvent(bucket=CompareDiffBucket.ONLY_SOURCE, pk=pk))
    builder.observe(CompareDiffEvent(bucket=CompareDiffBucket.ONLY_TARGET, pk=(10,)))
    builder.observe(
        CompareDiffEvent(
            bucket=CompareDiffBucket.DIFF,
            pk=(20,),
            source_values=(20, Decimal("10.00"), source_time, " Alice "),
            target_values=(20, Decimal("11.00"), target_time, "Alice"),
        )
    )
    builder.observe(
        CompareDiffEvent(
            bucket=CompareDiffBucket.DIFF,
            pk=(21,),
            source_values=(21, None, source_time, ""),
            target_values=(21, None, target_time, None),
        )
    )

    profile = builder.finish(
        bucket_counts={"only_source": 3, "only_target": 1, "diff": 2, "same": 0},
        progress={"skipped_rows": 7},
    )

    assert profile["summary"]["diff_rows"] == 2
    assert profile["summary"]["skipped_same_rows"] == 7
    assert profile["columns"]["amount"]["diff_rate"] == 0.5
    assert profile["columns"]["amount"]["numeric_delta"]["constant_offset"] == "-1.00"
    assert profile["columns"]["created_at"]["time_delta"]["constant_offset_seconds"] == -28800
    assert profile["columns"]["created_at"]["time_delta"]["likely_timezone_offset"] is True
    assert profile["columns"]["name"]["empty_strings"]["source"] == 1
    assert profile["columns"]["name"]["nulls"]["target"] == 1
    assert profile["columns"]["name"]["trim_changed"]["source"] == 1
    assert profile["missing_key_ranges"]["only_source"] == [
        {"start": 2, "end": 3, "count": 2, "span": 2},
        {"start": 5, "end": 5, "count": 1, "span": 1},
    ]


def test_zero_defect_sampling_formula_matches_design_anchors() -> None:
    assert zero_defect_difference_rate_upper_bound(300, confidence=0.95) == pytest.approx(
        0.00994,
        rel=0.01,
    )
    assert zero_defect_difference_rate_upper_bound(3000, confidence=0.95) == pytest.approx(
        0.000998,
        rel=0.01,
    )


def test_sample_quick_check_result_never_claims_full_equality() -> None:
    result = sample_quick_check_result(
        requested_rows=300,
        sampled_rows=300,
        observed_differences=0,
    )

    assert result["all_sampled_equal"] is True
    assert result["difference_rate_upper_bound"] < 0.01
    assert "does not prove full equality" in result["statement"]


def test_profile_for_ai_strips_exact_missing_key_range_endpoints() -> None:
    profile = {
        "version": 1,
        "summary": {"diff_rows": 2},
        "columns": {"amount": {"diff_rate": 1.0}},
        "missing_key_ranges": {
            "only_source": [{"start": 100, "end": 120, "count": 21, "span": 21}]
        },
    }

    safe = profile_for_ai(profile)

    assert safe["columns"] == {"amount": {"diff_rate": 1.0}}
    assert safe["missing_key_ranges"] == {"only_source": [{"count": 21, "span": 21}]}
