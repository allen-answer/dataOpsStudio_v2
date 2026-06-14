from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from datetime import date, datetime, time
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.schema import ColumnType


class CompareHashLevel(StrEnum):
    AGGREGATE = "aggregate"
    ROW = "row"


class CompareHashExecutionMode(StrEnum):
    DB_HASH = "db_hash"
    CLIENT_ROW_HASH = "client_row_hash"


class CompareDiffBucket(StrEnum):
    ONLY_SOURCE = "only_source"
    ONLY_TARGET = "only_target"
    DIFF = "diff"
    SAME = "same"


class CompareColumn(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    type: ColumnType = ColumnType.UNKNOWN
    driver_type: str | None = None


class CompareTableRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_name: str | None = None
    name: str


class CompareRules(BaseModel):
    """Rules that affect cross-database normalized row identity."""

    model_config = ConfigDict(frozen=True)

    ignore_columns: list[str] = Field(default_factory=list)
    column_mappings: dict[str, str] = Field(default_factory=dict)
    numeric_tolerance: float | None = None
    trim_strings: bool = False
    case_insensitive: bool = False
    empty_as_null: bool = False
    schema_policy: str = "warn"
    numeric_scale: int = Field(default=6, ge=0, le=18)
    timestamp_precision: int = Field(default=6, ge=0, le=6)
    null_sentinel: str = "\u2400"
    field_separator: str = "\u241f"
    charset: str = "utf8mb4"
    collation: str = "utf8mb4_bin"

    @field_validator("schema_policy")
    @classmethod
    def _validate_schema_policy(cls, value: str) -> str:
        if value not in {"warn", "strict"}:
            raise ValueError("schema_policy must be warn or strict")
        return value


class RunLimits(BaseModel):
    """Compare run limits used by the recursive hashdiff prefilter."""

    model_config = ConfigDict(frozen=True)

    max_rows: int | None = Field(default=None, gt=0)
    export_max_rows: int | None = Field(default=None, gt=0)
    fetch_chunk_size: int = Field(default=1000, gt=0)
    compare_batch_size: int = Field(default=10_000, gt=0)
    stream_compare: bool = True
    recursive_checksum: bool = True
    bisection_factor: int = Field(default=16, ge=8, le=32)
    bisection_threshold: int = Field(default=16_000, gt=0)
    max_bisection_depth: int = Field(default=8, ge=0)
    sample_quick_check: bool = False
    result_format: str = "parquet"
    persist_same_bucket: bool = False
    query_timeout_seconds: int = Field(default=1800, gt=0)
    run_disk_quota_mb: int | None = Field(default=None, gt=0)


class CompareSegment(BaseModel):
    """Half-open single-column PK range [start, end)."""

    model_config = ConfigDict(frozen=True)

    key_column: str
    start: int
    end: int

    @field_validator("end")
    @classmethod
    def _end_must_not_precede_start(cls, end: int, info: Any) -> int:
        start = info.data.get("start")
        if isinstance(start, int) and end < start:
            raise ValueError("segment end must be greater than or equal to start")
        return end

    @property
    def estimated_rows(self) -> int:
        return max(0, self.end - self.start)


class CompareHashRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    table: CompareTableRef
    columns: list[CompareColumn]
    key_columns: list[CompareColumn]
    rules: CompareRules = Field(default_factory=CompareRules)
    segment: CompareSegment | None = None
    level: CompareHashLevel = CompareHashLevel.AGGREGATE


class CompareHashPlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    execution_mode: CompareHashExecutionMode
    sql: str | None = None
    params: dict[str, object] = Field(default_factory=dict)
    paramstyle: str | None = None
    normalized_payload_expression: str | None = None
    row_hash_expression: str | None = None
    degrade_reasons: list[str] = Field(default_factory=list)

    @property
    def uses_database_hash(self) -> bool:
        return self.execution_mode is CompareHashExecutionMode.DB_HASH


class SegmentFingerprint(BaseModel):
    model_config = ConfigDict(frozen=True)

    row_count: int
    aggregate_hash: int


class CompareRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    pk: tuple[Any, ...]
    values: tuple[Any, ...]
    row_hash64: int | None = None


class CompareDiffEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    bucket: CompareDiffBucket
    pk: tuple[Any, ...]
    source_values: tuple[Any, ...] | None = None
    target_values: tuple[Any, ...] | None = None


class HashdiffProgress(BaseModel):
    model_config = ConfigDict(frozen=True)

    scanned_segments: int = 0
    skipped_segments: int = 0
    recursed_segments: int = 0
    row_mode_segments: int = 0
    max_depth_seen: int = 0


class HashdiffResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    events: list[CompareDiffEvent]
    progress: HashdiffProgress


class CompareSegmentReader(Protocol):
    def segment_fingerprint(self, segment: CompareSegment) -> SegmentFingerprint: ...

    def fetch_rows(self, segment: CompareSegment) -> Iterable[CompareRow]: ...


class _MutableProgress:
    def __init__(self) -> None:
        self.scanned_segments = 0
        self.skipped_segments = 0
        self.recursed_segments = 0
        self.row_mode_segments = 0
        self.max_depth_seen = 0

    def freeze(self) -> HashdiffProgress:
        return HashdiffProgress(
            scanned_segments=self.scanned_segments,
            skipped_segments=self.skipped_segments,
            recursed_segments=self.recursed_segments,
            row_mode_segments=self.row_mode_segments,
            max_depth_seen=self.max_depth_seen,
        )


def recursive_hashdiff(
    source: CompareSegmentReader,
    target: CompareSegmentReader,
    root_segment: CompareSegment,
    limits: RunLimits | None = None,
) -> HashdiffResult:
    """Compare two sorted PK ranges with recursive aggregate-hash prefiltering."""

    actual_limits = limits or RunLimits()
    progress = _MutableProgress()
    events: list[CompareDiffEvent] = []

    if not actual_limits.recursive_checksum or root_segment.estimated_rows <= (
        actual_limits.bisection_threshold
    ):
        events.extend(_row_level_diff(source, target, root_segment))
        progress.row_mode_segments += 1
        return HashdiffResult(events=events, progress=progress.freeze())

    initial_segments = _split_segment(root_segment, actual_limits.bisection_factor)
    stack: list[tuple[CompareSegment, int]] = [
        (segment, 0) for segment in reversed(initial_segments)
    ]

    while stack:
        segment, depth = stack.pop()
        progress.max_depth_seen = max(progress.max_depth_seen, depth)
        source_fingerprint = source.segment_fingerprint(segment)
        target_fingerprint = target.segment_fingerprint(segment)
        progress.scanned_segments += 1
        if source_fingerprint == target_fingerprint:
            progress.skipped_segments += 1
            continue

        estimated_rows = max(source_fingerprint.row_count, target_fingerprint.row_count)
        should_descend = (
            estimated_rows > actual_limits.bisection_threshold
            and depth < actual_limits.max_bisection_depth
            and segment.estimated_rows > 1
        )
        if should_descend:
            progress.recursed_segments += 1
            children = _split_segment(segment, actual_limits.bisection_factor)
            stack.extend((child, depth + 1) for child in reversed(children))
            continue

        progress.row_mode_segments += 1
        events.extend(_row_level_diff(source, target, segment))

    return HashdiffResult(events=events, progress=progress.freeze())


def _split_segment(segment: CompareSegment, factor: int) -> list[CompareSegment]:
    width = segment.estimated_rows
    if width == 0:
        return []
    chunk = max(1, (width + factor - 1) // factor)
    segments: list[CompareSegment] = []
    start = segment.start
    while start < segment.end:
        end = min(segment.end, start + chunk)
        segments.append(CompareSegment(key_column=segment.key_column, start=start, end=end))
        start = end
    return segments


def _row_level_diff(
    source: CompareSegmentReader,
    target: CompareSegmentReader,
    segment: CompareSegment,
) -> list[CompareDiffEvent]:
    source_rows = _row_map(source.fetch_rows(segment))
    target_rows = _row_map(target.fetch_rows(segment))
    events: list[CompareDiffEvent] = []
    for pk in sorted(source_rows.keys() | target_rows.keys()):
        source_row = source_rows.get(pk)
        target_row = target_rows.get(pk)
        if source_row is None and target_row is not None:
            events.append(
                CompareDiffEvent(
                    bucket=CompareDiffBucket.ONLY_TARGET,
                    pk=pk,
                    target_values=target_row.values,
                )
            )
        elif target_row is None and source_row is not None:
            events.append(
                CompareDiffEvent(
                    bucket=CompareDiffBucket.ONLY_SOURCE,
                    pk=pk,
                    source_values=source_row.values,
                )
            )
        elif source_row is not None and target_row is not None:
            bucket = (
                CompareDiffBucket.SAME
                if _rows_equivalent(source_row, target_row)
                else CompareDiffBucket.DIFF
            )
            events.append(
                CompareDiffEvent(
                    bucket=bucket,
                    pk=pk,
                    source_values=source_row.values,
                    target_values=target_row.values,
                )
            )
    return events


def _row_map(rows: Iterable[CompareRow]) -> dict[tuple[Any, ...], CompareRow]:
    return {row.pk: row for row in rows}


def _rows_equivalent(source: CompareRow, target: CompareRow) -> bool:
    if source.row_hash64 is not None and target.row_hash64 is not None:
        return source.row_hash64 == target.row_hash64
    return source.values == target.values


def compare_rows_without_prefilter(
    source_rows: Sequence[CompareRow],
    target_rows: Sequence[CompareRow],
    key_column: str = "id",
) -> HashdiffResult:
    """Small helper for tests and fallback callers that already hold row batches."""

    if not source_rows and not target_rows:
        root = CompareSegment(key_column=key_column, start=0, end=0)
    else:
        keys = [
            int(row.pk[0])
            for row in [*source_rows, *target_rows]
            if row.pk and isinstance(row.pk[0], int)
        ]
        root = CompareSegment(key_column=key_column, start=min(keys), end=max(keys) + 1)
    reader = _StaticRowReader(source_rows)
    other = _StaticRowReader(target_rows)
    return recursive_hashdiff(reader, other, root, RunLimits(recursive_checksum=False))


def normalized_compare_payload(
    columns: Sequence[CompareColumn],
    values: Sequence[Any],
    rules: CompareRules | None = None,
) -> str:
    actual_rules = rules or CompareRules()
    if len(columns) != len(values):
        raise ValueError("columns and values length mismatch")
    null_bits = ["1" if value is None else "0" for value in values]
    normalized = [
        _normalize_value(column, value, actual_rules) or actual_rules.null_sentinel
        for column, value in zip(columns, values, strict=True)
    ]
    return actual_rules.field_separator.join([*null_bits, *normalized])


def compare_row_hash64(
    columns: Sequence[CompareColumn],
    values: Sequence[Any],
    rules: CompareRules | None = None,
) -> int:
    payload = normalized_compare_payload(columns, values, rules)
    digest = hashlib.md5(payload.encode("utf-8"), usedforsecurity=False).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


def _normalize_value(column: CompareColumn, value: Any, rules: CompareRules) -> str | None:
    if value is None:
        return None
    if column.type in {ColumnType.INTEGER, ColumnType.DECIMAL, ColumnType.FLOAT}:
        return _normalize_decimal(value, rules.numeric_scale)
    if column.type is ColumnType.BOOLEAN:
        return "1" if _truthy(value) else "0"
    if column.type is ColumnType.DATE:
        return _normalize_date(value)
    if column.type is ColumnType.TIME:
        return _normalize_time(value, rules.timestamp_precision)
    if column.type is ColumnType.DATETIME:
        return _normalize_datetime(value, rules.timestamp_precision)
    text = str(value)
    if rules.trim_strings:
        text = text.strip()
    if rules.case_insensitive:
        text = text.casefold()
    if rules.empty_as_null and text == "":
        return None
    return text


def _normalize_decimal(value: Any, scale: int) -> str:
    decimal_value = Decimal(str(value))
    quant = Decimal(1).scaleb(-scale)
    rounded = decimal_value.quantize(quant, rounding=ROUND_HALF_UP)
    return f"{rounded:.{scale}f}"


def _normalize_date(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def _normalize_time(value: Any, precision: int) -> str:
    if isinstance(value, datetime):
        time_value = value.time()
    elif isinstance(value, time):
        time_value = value
    else:
        return _trim_fraction(str(value), precision)
    base = time_value.strftime("%H:%M:%S")
    return _append_fraction(base, time_value.microsecond, precision)


def _normalize_datetime(value: Any, precision: int) -> str:
    if isinstance(value, datetime):
        base = value.strftime("%Y-%m-%d %H:%M:%S")
        return _append_fraction(base, value.microsecond, precision)
    return _trim_fraction(str(value).replace("T", " "), precision)


def _append_fraction(base: str, microsecond: int, precision: int) -> str:
    if precision == 0:
        return base
    return f"{base}.{microsecond:06d}"[: len(base) + 1 + precision]


def _trim_fraction(value: str, precision: int) -> str:
    if "." not in value:
        return value
    base, fraction = value.split(".", 1)
    if precision == 0:
        return base
    return f"{base}.{fraction[:precision].ljust(precision, '0')}"


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "t", "yes", "y"}
    return bool(value)


class _StaticRowReader:
    def __init__(self, rows: Sequence[CompareRow]) -> None:
        self._rows = list(rows)

    def segment_fingerprint(self, segment: CompareSegment) -> SegmentFingerprint:
        rows = list(self.fetch_rows(segment))
        return SegmentFingerprint(
            row_count=len(rows),
            aggregate_hash=sum(row.row_hash64 or hash(row.values) for row in rows),
        )

    def fetch_rows(self, segment: CompareSegment) -> Iterable[CompareRow]:
        return [
            row
            for row in self._rows
            if row.pk and isinstance(row.pk[0], int) and segment.start <= row.pk[0] < segment.end
        ]


__all__ = [
    "CompareColumn",
    "CompareDiffBucket",
    "CompareDiffEvent",
    "CompareHashExecutionMode",
    "CompareHashLevel",
    "CompareHashPlan",
    "CompareHashRequest",
    "CompareRow",
    "CompareRules",
    "CompareSegment",
    "CompareSegmentReader",
    "CompareTableRef",
    "HashdiffProgress",
    "HashdiffResult",
    "RunLimits",
    "SegmentFingerprint",
    "compare_row_hash64",
    "compare_rows_without_prefilter",
    "normalized_compare_payload",
    "recursive_hashdiff",
]
