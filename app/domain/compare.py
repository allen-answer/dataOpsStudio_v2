from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator, Sequence
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

    key_columns: list[str] = Field(default_factory=list)
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
    sample_size: int = Field(default=300, gt=0)
    sample_confidence: float = Field(default=0.95, gt=0, lt=1)
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
    raw_values: tuple[Any, ...] | None = None


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
    skipped_rows: int = 0
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
        self.skipped_rows = 0
        self.recursed_segments = 0
        self.row_mode_segments = 0
        self.max_depth_seen = 0

    def freeze(self) -> HashdiffProgress:
        return HashdiffProgress(
            scanned_segments=self.scanned_segments,
            skipped_segments=self.skipped_segments,
            skipped_rows=self.skipped_rows,
            recursed_segments=self.recursed_segments,
            row_mode_segments=self.row_mode_segments,
            max_depth_seen=self.max_depth_seen,
        )


class CompareDiffStream:
    """惰性差异事件流。

    存在的理由:差异事件此前是一次性攒进 list 再交给 worker 落盘,而单条
    CompareDiffEvent(16 列、diff 桶带两侧值)约 1.7KB —— 百万级差异就是 1.6GB
    常驻内存,且这个量与总行数无关(两侧只差一百万行的十亿行表照样会爆)。
    改成边算边吐后,worker 按 compare_batch_size 冲刷,内存与差异条数解耦。

    ★ progress() 随消费推进,只有 events 耗尽后才是终值 —— 这是流式的代价。
      调用方必须先消费完 events 再读 progress(尤其 skipped_rows)。
    """

    __slots__ = ("_progress", "events")

    def __init__(
        self,
        events: Iterator[CompareDiffEvent],
        progress: _MutableProgress | None = None,
    ) -> None:
        self.events = events
        self._progress = progress if progress is not None else _MutableProgress()

    def progress(self) -> HashdiffProgress:
        return self._progress.freeze()

    def materialize(self) -> HashdiffResult:
        """消费完并打包成旧的 HashdiffResult(给测试与有界调用方用)。"""
        events = list(self.events)
        return HashdiffResult(events=events, progress=self.progress())


def diff_stream_from_events(events: Iterable[CompareDiffEvent]) -> CompareDiffStream:
    """把一段事件序列包成流,首个事件出现时记一个 row-mode 段。

    与既有 `HashdiffProgress(row_mode_segments=1 if events else 0)` 等价,
    只是改成惰性判定 —— 流式下「有没有事件」要到消费时才知道。
    """
    progress = _MutableProgress()

    def _generate() -> Iterator[CompareDiffEvent]:
        for event in events:
            progress.row_mode_segments = 1
            yield event

    return CompareDiffStream(_generate(), progress)


def recursive_hashdiff_stream(
    source: CompareSegmentReader,
    target: CompareSegmentReader,
    root_segment: CompareSegment,
    limits: RunLimits | None = None,
) -> CompareDiffStream:
    """recursive_hashdiff 的流式版:边分段边吐事件,不攒全量。"""

    actual_limits = limits or RunLimits()
    progress = _MutableProgress()

    def _generate() -> Iterator[CompareDiffEvent]:
        if not actual_limits.recursive_checksum or root_segment.estimated_rows <= (
            actual_limits.bisection_threshold
        ):
            progress.row_mode_segments += 1
            yield from _row_level_diff(source, target, root_segment)
            return

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
                progress.skipped_rows += source_fingerprint.row_count
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
            yield from _row_level_diff(source, target, segment)

    return CompareDiffStream(_generate(), progress)


def recursive_hashdiff(
    source: CompareSegmentReader,
    target: CompareSegmentReader,
    root_segment: CompareSegment,
    limits: RunLimits | None = None,
) -> HashdiffResult:
    """Compare two sorted PK ranges with recursive aggregate-hash prefiltering.

    全量物化版,保留给测试与有界调用方;生产路径走 recursive_hashdiff_stream。
    """

    return recursive_hashdiff_stream(source, target, root_segment, limits).materialize()


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
) -> Iterator[CompareDiffEvent]:
    # 单段内仍需两侧全量物化(段大小由 bisection_threshold 钳住),但段与段之间
    # 不再累积 —— 事件逐条 yield 给调用方落盘。
    source_rows = _row_map(source.fetch_rows(segment), side="source")
    target_rows = _row_map(target.fetch_rows(segment), side="target")
    for pk in sorted_compare_keys(source_rows.keys() | target_rows.keys()):
        source_row = source_rows.get(pk)
        target_row = target_rows.get(pk)
        if source_row is None and target_row is not None:
            yield CompareDiffEvent(
                bucket=CompareDiffBucket.ONLY_TARGET,
                pk=pk,
                target_values=target_row.raw_values or target_row.values,
            )
        elif target_row is None and source_row is not None:
            yield CompareDiffEvent(
                bucket=CompareDiffBucket.ONLY_SOURCE,
                pk=pk,
                source_values=source_row.raw_values or source_row.values,
            )
        elif source_row is not None and target_row is not None:
            bucket = (
                CompareDiffBucket.SAME
                if _rows_equivalent(source_row, target_row)
                else CompareDiffBucket.DIFF
            )
            yield CompareDiffEvent(
                bucket=bucket,
                pk=pk,
                source_values=source_row.raw_values or source_row.values,
                target_values=target_row.raw_values or target_row.values,
            )


class CompareDuplicateKeyError(RuntimeError):
    """主键在数据中不唯一。

    对比按主键建字典对齐两侧,重复主键会被后来的行覆盖 —— 静默丢数据,
    而且结果里的行数看起来完全正常(现场遇到过 32 行只报 2 条)。
    宁可明确失败,也不能给出一个看似正常的错误答案。

    ★ R5:异常信息只带计数与侧别,绝不带主键值本身(业务数据)。
    """

    def __init__(self, side: str, duplicates: int, total: int) -> None:
        super().__init__(f"duplicate compare key on {side}: {duplicates}/{total} rows")
        self.side = side
        self.duplicates = duplicates
        self.total = total


def compare_pk_sort_key(pk: Sequence[Any]) -> tuple[tuple[int, float, str], ...]:
    """主键排序键:NULL 安全 + 跨类型总序。

    裸 sorted() 在主键列含 NULL 时直接抛
    ``TypeError: '<' not supported between 'NoneType' and 'str'``。数仓维度/代码列
    有 NULL 极常见(现场 8020 行里 222 行主键列为空),对比不该因此崩掉。
    两侧类型不一致(一边 int 一边 str)同理。

    分档 NULL < 数值 < 其它:数值按数值序(保住整数主键的自然顺序),其余按
    字符串序;同档内再用字符串形式兜底,保证是确定的全序而非偶然相等。
    """
    key: list[tuple[int, float, str]] = []
    for value in pk:
        if value is None:
            key.append((0, 0.0, ""))
        elif isinstance(value, (int, float, Decimal)):
            try:
                key.append((1, float(value), str(value)))
            except (OverflowError, ValueError):
                # 超出 float 表示范围的 Decimal:退到字符串序,仍是确定的
                key.append((2, 0.0, str(value)))
        else:
            key.append((2, 0.0, str(value)))
    return tuple(key)


def sorted_compare_keys(keys: Iterable[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    """按 compare_pk_sort_key 排序主键集合。"""
    return sorted(keys, key=compare_pk_sort_key)


def _row_map(rows: Iterable[CompareRow], *, side: str) -> dict[tuple[Any, ...], CompareRow]:
    """按主键建行字典;主键不唯一即报错,不静默折叠。"""
    out: dict[tuple[Any, ...], CompareRow] = {}
    total = 0
    duplicates = 0
    for row in rows:
        total += 1
        if row.pk in out:
            duplicates += 1
        out[row.pk] = row
    if duplicates:
        raise CompareDuplicateKeyError(side, duplicates, total)
    return out


def _rows_equivalent(source: CompareRow, target: CompareRow) -> bool:
    return source.values == target.values


def compare_rows_without_prefilter(
    source_rows: Sequence[CompareRow],
    target_rows: Sequence[CompareRow],
    key_column: str = "id",
    *,
    columns: Sequence[CompareColumn] | None = None,
    rules: CompareRules | None = None,
) -> HashdiffResult:
    """Small helper for tests and fallback callers that already hold row batches."""

    actual_source_rows = _normalize_compare_rows(source_rows, columns, rules)
    actual_target_rows = _normalize_compare_rows(target_rows, columns, rules)
    if not source_rows and not target_rows:
        root = CompareSegment(key_column=key_column, start=0, end=0)
    else:
        keys = [
            int(row.pk[0])
            for row in [*actual_source_rows, *actual_target_rows]
            if row.pk and isinstance(row.pk[0], int)
        ]
        root = CompareSegment(key_column=key_column, start=min(keys), end=max(keys) + 1)
    reader = _StaticRowReader(actual_source_rows)
    other = _StaticRowReader(actual_target_rows)
    return recursive_hashdiff(reader, other, root, RunLimits(recursive_checksum=False))


def normalized_compare_identity(
    columns: Sequence[CompareColumn],
    values: Sequence[Any],
    rules: CompareRules | None = None,
) -> tuple[tuple[bool, str], ...]:
    actual_rules = rules or CompareRules()
    normalized = _normalized_values(columns, values, actual_rules)
    return tuple(
        (value is None, value if value is not None else actual_rules.null_sentinel)
        for value in normalized
    )


def normalized_compare_payload(
    columns: Sequence[CompareColumn],
    values: Sequence[Any],
    rules: CompareRules | None = None,
) -> str:
    actual_rules = rules or CompareRules()
    identity = normalized_compare_identity(columns, values, actual_rules)
    null_bits = ["1" if is_null else "0" for is_null, _value in identity]
    normalized_parts = [item for _is_null, value in identity for item in (str(len(value)), value)]
    return actual_rules.field_separator.join([*null_bits, *normalized_parts])


def _normalized_values(
    columns: Sequence[CompareColumn],
    values: Sequence[Any],
    rules: CompareRules,
) -> list[str | None]:
    if len(columns) != len(values):
        raise ValueError("columns and values length mismatch")
    return [
        _normalize_value(column, value, rules)
        for column, value in zip(columns, values, strict=True)
    ]


def _normalize_compare_rows(
    rows: Sequence[CompareRow],
    columns: Sequence[CompareColumn] | None,
    rules: CompareRules | None,
) -> list[CompareRow]:
    if columns is None:
        return list(rows)
    return [
        row.model_copy(
            update={
                "values": normalized_compare_identity(columns, row.values, rules),
                "row_hash64": row.row_hash64
                if row.row_hash64 is not None
                else compare_row_hash64(columns, row.values, rules),
                "raw_values": row.raw_values or row.values,
            }
        )
        for row in rows
    ]


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
        text = text.strip(" ")
    if rules.case_insensitive:
        text = text.lower()
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
            aggregate_hash=sum(
                row.row_hash64 if row.row_hash64 is not None else _stable_tuple_hash(row.values)
                for row in rows
            ),
        )

    def fetch_rows(self, segment: CompareSegment) -> Iterable[CompareRow]:
        return [
            row
            for row in self._rows
            if row.pk and isinstance(row.pk[0], int) and segment.start <= row.pk[0] < segment.end
        ]


def _stable_tuple_hash(values: tuple[Any, ...]) -> int:
    payload = repr(values).encode("utf-8")
    digest = hashlib.md5(payload, usedforsecurity=False).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


__all__ = [
    "CompareColumn",
    "CompareDiffBucket",
    "CompareDiffEvent",
    "CompareDiffStream",
    "CompareDuplicateKeyError",
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
    "compare_pk_sort_key",
    "compare_row_hash64",
    "compare_rows_without_prefilter",
    "diff_stream_from_events",
    "normalized_compare_identity",
    "normalized_compare_payload",
    "recursive_hashdiff",
    "recursive_hashdiff_stream",
    "sorted_compare_keys",
]
