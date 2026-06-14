from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.dbclients.dm_compare import build_dm_compare_hash_plan
from app.dbclients.mysql_compare import build_mysql_compare_hash_plan
from app.domain.compare import (
    CompareColumn,
    CompareDiffBucket,
    CompareHashExecutionMode,
    CompareHashLevel,
    CompareHashRequest,
    CompareRow,
    CompareRules,
    CompareSegment,
    CompareTableRef,
    RunLimits,
    SegmentFingerprint,
    compare_row_hash64,
    compare_rows_without_prefilter,
    normalized_compare_identity,
    normalized_compare_payload,
    recursive_hashdiff,
)
from app.domain.schema import ColumnType


def test_compare_hash_sql_uses_md5_sum_and_not_weak_engine_hashes() -> None:
    request = _hash_request()

    mysql_plan = build_mysql_compare_hash_plan(request)
    dm_plan = build_dm_compare_hash_plan(request)

    assert mysql_plan.execution_mode is CompareHashExecutionMode.DB_HASH
    assert dm_plan.execution_mode is CompareHashExecutionMode.DB_HASH
    assert mysql_plan.sql is not None
    assert dm_plan.sql is not None
    assert "MD5(" in mysql_plan.sql
    assert "SUM(__row_hash64)" in mysql_plan.sql
    assert "DBMS_CRYPTO.HASH" in dm_plan.sql
    assert 'SUM("__ROW_HASH64")' in dm_plan.sql
    combined_sql = f"{mysql_plan.sql} {dm_plan.sql}".upper()
    assert "ORA_HASH" not in combined_sql
    assert "CRC32" not in combined_sql
    assert "BIT_XOR" not in combined_sql
    assert "CHECKSUM_AGG" not in combined_sql


def test_compare_hash_sql_contains_normalization_rules_and_segment_params() -> None:
    request = _hash_request(level=CompareHashLevel.ROW)

    mysql_plan = build_mysql_compare_hash_plan(request)
    dm_plan = build_dm_compare_hash_plan(request)

    assert mysql_plan.sql is not None
    mysql_payload = mysql_plan.normalized_payload_expression
    assert mysql_payload is not None
    assert "CONCAT_WS" in mysql_payload
    assert "CHARACTER SET utf8mb4" in mysql_payload
    assert "COLLATE utf8mb4_bin" in mysql_payload
    assert "TRIM(" in mysql_payload
    assert "LOWER(" in mysql_payload
    assert "IF((NULLIF(" in mysql_payload
    assert "CHAR_LENGTH(" in mysql_payload
    assert "%%Y-%%m-%%d" in mysql_payload
    assert "%(segment_start)s" in mysql_plan.sql
    assert mysql_plan.params == {"segment_start": 0, "segment_end": 32_000}

    assert dm_plan.sql is not None
    dm_payload = dm_plan.normalized_payload_expression
    assert dm_payload is not None
    assert " || " in dm_payload
    assert 'TO_CHAR("NAME")' in dm_payload
    assert "CONVERT(" not in dm_payload
    assert "VARCHAR(4000)" not in dm_payload
    assert "CASE WHEN (NULLIF(" in dm_payload
    assert "LENGTH(" in dm_payload
    assert dm_plan.row_hash_expression is not None
    assert "TO_NUMBER(" not in dm_plan.row_hash_expression
    assert "INSTR('0123456789ABCDEF'" in dm_plan.row_hash_expression
    assert ":segment_start" in dm_plan.sql
    assert dm_plan.params == {"segment_start": 0, "segment_end": 32_000}


def test_mysql_datetime_hash_sql_survives_pyformat_interpolation() -> None:
    plan = build_mysql_compare_hash_plan(_hash_request())

    assert plan.sql is not None
    # PyMySQL applies Python %-formatting whenever params are passed. This catches
    # DATE_FORMAT literals such as %Y before they reach a real MySQL server.
    interpolated = plan.sql % plan.params

    assert "DATE_FORMAT" in interpolated
    assert "%Y-%m-%d" in interpolated


def test_unknown_or_json_columns_explicitly_degrade_to_client_hash() -> None:
    request = _hash_request(
        columns=[
            CompareColumn(name="id", type=ColumnType.INTEGER),
            CompareColumn(name="payload", type=ColumnType.JSON),
        ]
    )

    mysql_plan = build_mysql_compare_hash_plan(request)
    dm_plan = build_dm_compare_hash_plan(request)

    assert mysql_plan.execution_mode is CompareHashExecutionMode.CLIENT_ROW_HASH
    assert dm_plan.execution_mode is CompareHashExecutionMode.CLIENT_ROW_HASH
    assert mysql_plan.sql is None
    assert dm_plan.sql is None
    assert mysql_plan.degrade_reasons == [
        "column payload type json requires client row hash",
    ]
    assert dm_plan.degrade_reasons == [
        "column payload type json requires client row hash",
    ]


def test_mysql_unsafe_charset_or_collation_degrades_to_client_hash() -> None:
    request = _hash_request(
        rules=_rules().model_copy(update={"charset": "utf8mb4;select", "collation": "utf8mb4-bin"})
    )

    plan = build_mysql_compare_hash_plan(request)

    assert plan.execution_mode is CompareHashExecutionMode.CLIENT_ROW_HASH
    assert plan.sql is None
    assert plan.degrade_reasons == [
        "mysql compare charset requires a safe identifier name",
        "mysql compare collation requires a safe identifier name",
    ]


def test_dm_non_utf8_charset_degrades_to_client_hash() -> None:
    request = _hash_request(rules=_rules().model_copy(update={"charset": "latin1"}))

    plan = build_dm_compare_hash_plan(request)

    assert plan.execution_mode is CompareHashExecutionMode.CLIENT_ROW_HASH
    assert plan.sql is None
    assert plan.degrade_reasons == [
        "dm compare db hash requires a UTF-8 normalization charset",
    ]


def test_golden_normalized_hash_matches_mysql_and_dm_logical_rows() -> None:
    columns = _columns()
    rules = _rules()
    mysql_row = [
        7,
        Decimal("12.3400"),
        datetime(2026, 6, 14, 12, 13, 14, 123456),
        "  张三  ",
        None,
    ]
    dm_row = [
        Decimal("7"),
        Decimal("12.34"),
        "2026-06-14 12:13:14.123456",
        "张三",
        None,
    ]

    assert compare_row_hash64(columns, mysql_row, rules) == compare_row_hash64(
        columns,
        dm_row,
        rules,
    )


def test_compare_row_hash64_uses_first_8_md5_bytes_big_endian() -> None:
    columns = [CompareColumn(name="payload", type=ColumnType.STRING)]
    values = ["abc"]
    payload = normalized_compare_payload(columns, values)
    digest = hashlib.md5(payload.encode("utf-8"), usedforsecurity=False).digest()

    assert compare_row_hash64(columns, values) == int.from_bytes(
        digest[:8],
        "big",
        signed=False,
    )
    assert compare_row_hash64(columns, values) != int.from_bytes(
        digest[-8:],
        "big",
        signed=False,
    )


def test_empty_as_null_affects_null_bitmap_after_normalization() -> None:
    columns = [
        CompareColumn(name="id", type=ColumnType.INTEGER),
        CompareColumn(name="memo", type=ColumnType.STRING),
        CompareColumn(name="enabled", type=ColumnType.BOOLEAN),
    ]
    rules = CompareRules(empty_as_null=True)

    assert compare_row_hash64(columns, [1, "", None], rules) == compare_row_hash64(
        columns,
        [1, None, None],
        rules,
    )
    payload = normalized_compare_payload(columns, [1, "", None], rules)
    parts = payload.split(rules.field_separator)
    assert parts[:3] == ["0", "1", "1"]


def test_normalized_identity_preserves_null_bitmap_truth() -> None:
    columns = [CompareColumn(name="memo", type=ColumnType.STRING)]
    rules = CompareRules(empty_as_null=True)

    assert normalized_compare_identity(columns, [""], rules) == ((True, rules.null_sentinel),)
    assert normalized_compare_identity(columns, [rules.null_sentinel], rules) == (
        (False, rules.null_sentinel),
    )


def test_payload_length_prefix_prevents_separator_boundary_collision() -> None:
    separator = CompareRules().field_separator
    columns = [
        CompareColumn(name="left", type=ColumnType.STRING),
        CompareColumn(name="right", type=ColumnType.STRING),
    ]

    first = normalized_compare_payload(columns, [f"a{separator}b", "c"])
    second = normalized_compare_payload(columns, ["a", f"b{separator}c"])

    assert first != second
    assert first.split(separator)[:4] == ["0", "0", "3", "a"]
    assert second.split(separator)[:4] == ["0", "0", "1", "a"]


def test_python_string_normalization_matches_sql_lower_and_space_trim() -> None:
    columns = [CompareColumn(name="name", type=ColumnType.STRING)]
    rules = CompareRules(trim_strings=True, case_insensitive=True)

    assert normalized_compare_payload(columns, ["  Alpha  "], rules).endswith("alpha")
    assert normalized_compare_payload(columns, ["\tAlpha\t"], rules).endswith("\talpha\t")


def test_fallback_row_compare_uses_normalized_identity_when_columns_given() -> None:
    columns = [
        CompareColumn(name="id", type=ColumnType.INTEGER),
        CompareColumn(name="amount", type=ColumnType.DECIMAL),
        CompareColumn(name="name", type=ColumnType.STRING),
        CompareColumn(name="memo", type=ColumnType.STRING),
    ]
    rules = CompareRules(trim_strings=True, case_insensitive=True, empty_as_null=True)
    source_rows = [CompareRow(pk=(1,), values=(1, "12.3400", "  Alpha  ", ""))]
    target_rows = [CompareRow(pk=(1,), values=(1, Decimal("12.34"), "alpha", None))]

    result = compare_rows_without_prefilter(
        source_rows,
        target_rows,
        columns=columns,
        rules=rules,
    )

    assert [(event.bucket, event.pk) for event in result.events] == [(CompareDiffBucket.SAME, (1,))]


def test_recursive_hashdiff_skips_identical_segments_without_row_fetch() -> None:
    rows = [CompareRow(pk=(idx,), values=(idx,), row_hash64=idx) for idx in range(64_000)]
    source = _MemorySegmentReader(rows)
    target = _MemorySegmentReader(rows)

    result = recursive_hashdiff(
        source,
        target,
        CompareSegment(key_column="id", start=0, end=64_000),
        RunLimits(bisection_factor=8, bisection_threshold=1000),
    )

    assert result.events == []
    assert result.progress.scanned_segments == 8
    assert result.progress.skipped_segments == 8
    assert result.progress.row_mode_segments == 0
    assert source.fetch_calls == []
    assert target.fetch_calls == []


def test_recursive_hashdiff_sparse_diff_avoids_full_row_scan() -> None:
    source_rows = [CompareRow(pk=(idx,), values=(idx,), row_hash64=idx) for idx in range(64_000)]
    target_rows = list(source_rows)
    target_rows[42_123] = CompareRow(pk=(42_123,), values=("changed",), row_hash64=99_999_999)
    source = _MemorySegmentReader(source_rows)
    target = _MemorySegmentReader(target_rows)

    result = recursive_hashdiff(
        source,
        target,
        CompareSegment(key_column="id", start=0, end=64_000),
        RunLimits(bisection_factor=8, bisection_threshold=1000),
    )

    diff_events = [event for event in result.events if event.bucket is CompareDiffBucket.DIFF]
    same_events = [event for event in result.events if event.bucket is CompareDiffBucket.SAME]
    assert [event.pk for event in diff_events] == [(42_123,)]
    assert len(same_events) < len(source_rows)
    assert sum(end - start for start, end in source.fetch_calls) < len(source_rows)
    assert result.progress.recursed_segments > 0


def test_row_level_diff_does_not_use_hash_as_final_truth() -> None:
    source = _MemorySegmentReader([CompareRow(pk=(1,), values=("old",), row_hash64=123)])
    target = _MemorySegmentReader([CompareRow(pk=(1,), values=("new",), row_hash64=123)])

    result = recursive_hashdiff(
        source,
        target,
        CompareSegment(key_column="id", start=1, end=2),
        RunLimits(recursive_checksum=False),
    )

    assert [(event.bucket, event.pk) for event in result.events] == [(CompareDiffBucket.DIFF, (1,))]


def test_recursive_and_disabled_prefilter_return_same_diff_set() -> None:
    source_rows = [CompareRow(pk=(idx,), values=(idx,), row_hash64=idx) for idx in range(20_000)]
    target_rows = list(source_rows)
    target_rows[17_001] = CompareRow(pk=(17_001,), values=("changed",), row_hash64=100)
    target_rows.append(CompareRow(pk=(20_001,), values=("new",), row_hash64=20_001))
    source = _MemorySegmentReader(source_rows)
    target = _MemorySegmentReader(target_rows)
    root = CompareSegment(key_column="id", start=0, end=20_002)

    recursive = recursive_hashdiff(
        source,
        target,
        root,
        RunLimits(bisection_factor=8, bisection_threshold=500),
    )
    disabled = recursive_hashdiff(
        _MemorySegmentReader(source_rows),
        _MemorySegmentReader(target_rows),
        root,
        RunLimits(recursive_checksum=False),
    )

    assert _interesting_events(recursive.events) == _interesting_events(disabled.events)


def _hash_request(
    *,
    columns: list[CompareColumn] | None = None,
    level: CompareHashLevel = CompareHashLevel.AGGREGATE,
    rules: CompareRules | None = None,
) -> CompareHashRequest:
    return CompareHashRequest(
        table=CompareTableRef(schema_name="app", name="orders"),
        columns=columns or _columns(),
        key_columns=[CompareColumn(name="id", type=ColumnType.INTEGER)],
        rules=rules or _rules(),
        segment=CompareSegment(key_column="id", start=0, end=32_000),
        level=level,
    )


def _columns() -> list[CompareColumn]:
    return [
        CompareColumn(name="id", type=ColumnType.INTEGER),
        CompareColumn(name="amount", type=ColumnType.DECIMAL),
        CompareColumn(name="updated_at", type=ColumnType.DATETIME),
        CompareColumn(name="name", type=ColumnType.STRING),
        CompareColumn(name="memo", type=ColumnType.STRING),
    ]


def _rules() -> CompareRules:
    return CompareRules(
        trim_strings=True,
        case_insensitive=True,
        empty_as_null=True,
        numeric_scale=4,
        timestamp_precision=6,
    )


def _interesting_events(events: Sequence[Any]) -> list[tuple[str, tuple[Any, ...]]]:
    return sorted(
        (event.bucket.value, event.pk)
        for event in events
        if event.bucket is not CompareDiffBucket.SAME
    )


class _MemorySegmentReader:
    def __init__(self, rows: Iterable[CompareRow]) -> None:
        self._rows = {int(row.pk[0]): row for row in rows}
        self.fingerprint_calls: list[tuple[int, int]] = []
        self.fetch_calls: list[tuple[int, int]] = []

    def segment_fingerprint(self, segment: CompareSegment) -> SegmentFingerprint:
        self.fingerprint_calls.append((segment.start, segment.end))
        rows = list(self._rows_in_segment(segment))
        return SegmentFingerprint(
            row_count=len(rows),
            aggregate_hash=sum(row.row_hash64 or 0 for row in rows),
        )

    def fetch_rows(self, segment: CompareSegment) -> Iterable[CompareRow]:
        self.fetch_calls.append((segment.start, segment.end))
        return list(self._rows_in_segment(segment))

    def _rows_in_segment(self, segment: CompareSegment) -> Iterable[CompareRow]:
        for key in sorted(self._rows):
            if segment.start <= key < segment.end:
                yield self._rows[key]
