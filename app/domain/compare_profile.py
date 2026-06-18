from __future__ import annotations

import math
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any

from app.domain.compare import (
    CompareColumn,
    CompareDiffBucket,
    CompareDiffEvent,
    CompareRules,
    normalized_compare_identity,
)
from app.domain.schema import ColumnType

DEFAULT_SAMPLE_CONFIDENCE = 0.95
DEFAULT_SAMPLE_SIZE = 300


class DiffProfileBuilder:
    """Incrementally builds a statistical profile from row-level compare events.

    The builder never emits source/target cell values. It keeps transient values only
    long enough to count statistical patterns required by the compare profile.
    """

    def __init__(
        self,
        *,
        key_columns: list[CompareColumn],
        value_columns: list[CompareColumn],
        rules: CompareRules,
    ) -> None:
        self._key_columns = list(key_columns)
        self._value_columns = list(value_columns)
        self._rules = rules
        self._events_seen = 0
        self._paired_rows = 0
        self._diff_rows = 0
        self._same_rows_observed = 0
        self._columns = {column.name: _ColumnStats(column) for column in value_columns}
        self._missing_source = _RangeCluster()
        self._missing_target = _RangeCluster()

    def observe(self, event: CompareDiffEvent) -> None:
        self._events_seen += 1
        if event.bucket is CompareDiffBucket.ONLY_SOURCE:
            self._missing_source.add(event.pk)
            return
        if event.bucket is CompareDiffBucket.ONLY_TARGET:
            self._missing_target.add(event.pk)
            return
        if event.source_values is None or event.target_values is None:
            return

        self._paired_rows += 1
        source_values = event.source_values
        target_values = event.target_values
        if event.bucket is CompareDiffBucket.SAME:
            self._same_rows_observed += 1
        if event.bucket is CompareDiffBucket.DIFF:
            self._diff_rows += 1

        source_identity = normalized_compare_identity(
            self._value_columns,
            source_values,
            self._rules,
        )
        target_identity = normalized_compare_identity(
            self._value_columns,
            target_values,
            self._rules,
        )
        for index, column in enumerate(self._value_columns):
            source = source_values[index] if index < len(source_values) else None
            target = target_values[index] if index < len(target_values) else None
            stats = self._columns[column.name]
            stats.observe_distribution(source=source, target=target)
            if source_identity[index] == target_identity[index]:
                continue
            stats.observe_change(source=source, target=target)

    def finish(
        self,
        *,
        bucket_counts: dict[str, int],
        progress: dict[str, int],
        sample_result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        diff_count = int(bucket_counts.get(CompareDiffBucket.DIFF.value, self._diff_rows) or 0)
        same_count = int(
            bucket_counts.get(CompareDiffBucket.SAME.value, self._same_rows_observed) or 0
        )
        return {
            "version": 1,
            "generated": True,
            "summary": {
                "events_observed": self._events_seen,
                "paired_rows_observed": self._paired_rows,
                "diff_rows": diff_count,
                "same_rows": same_count,
                "skipped_same_rows": int(progress.get("skipped_rows", 0) or 0),
                "only_source_rows": int(
                    bucket_counts.get(CompareDiffBucket.ONLY_SOURCE.value, 0) or 0
                ),
                "only_target_rows": int(
                    bucket_counts.get(CompareDiffBucket.ONLY_TARGET.value, 0) or 0
                ),
            },
            "columns": {
                name: stats.to_profile(diff_rows=diff_count)
                for name, stats in self._columns.items()
            },
            "missing_key_ranges": {
                CompareDiffBucket.ONLY_SOURCE.value: self._missing_source.ranges(),
                CompareDiffBucket.ONLY_TARGET.value: self._missing_target.ranges(),
            },
            "sample_result": sample_result,
        }


def empty_diff_profile(
    *,
    reason: str | None = None,
    sample_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    profile: dict[str, Any] = {
        "version": 1,
        "generated": False,
        "summary": {},
        "columns": {},
        "missing_key_ranges": {
            CompareDiffBucket.ONLY_SOURCE.value: [],
            CompareDiffBucket.ONLY_TARGET.value: [],
        },
        "sample_result": sample_result,
    }
    if reason:
        profile["reason"] = reason
    return profile


def zero_defect_difference_rate_upper_bound(
    sample_size: int,
    *,
    confidence: float = DEFAULT_SAMPLE_CONFIDENCE,
) -> float | None:
    if sample_size <= 0:
        return None
    if confidence <= 0 or confidence >= 1:
        raise ValueError("confidence must be between 0 and 1")
    alpha = 1 - confidence
    return 1 - math.exp(math.log(alpha) / sample_size)


def sample_quick_check_result(
    *,
    requested_rows: int,
    sampled_rows: int,
    observed_differences: int,
    confidence: float = DEFAULT_SAMPLE_CONFIDENCE,
    mode: str = "pk_random_anchor",
) -> dict[str, Any]:
    all_equal = observed_differences == 0
    upper_bound = (
        zero_defect_difference_rate_upper_bound(sampled_rows, confidence=confidence)
        if all_equal
        else None
    )
    observed_rate = observed_differences / sampled_rows if sampled_rows > 0 else None
    result = {
        "enabled": True,
        "mode": mode,
        "requested_rows": requested_rows,
        "sampled_rows": sampled_rows,
        "observed_differences": observed_differences,
        "observed_difference_rate": observed_rate,
        "all_sampled_equal": all_equal,
        "confidence": confidence,
        "difference_rate_upper_bound": upper_bound,
    }
    if upper_bound is not None:
        result["statement"] = (
            f"At {confidence:.1%} confidence, estimated difference rate is < "
            f"{upper_bound:.4%}; this does not prove full equality."
        )
    return result


def profile_for_ai(diff_profile: dict[str, Any]) -> dict[str, Any]:
    """Return the L2-safe portion of a diff profile for AI attribution.

    Exact row values and exact primary-key range endpoints are excluded. The AI sees
    counts, rates, column names, and range sizes only.
    """

    safe = {
        "version": diff_profile.get("version", 1),
        "summary": diff_profile.get("summary", {}),
        "columns": diff_profile.get("columns", {}),
        "sample_result": diff_profile.get("sample_result"),
    }
    missing = diff_profile.get("missing_key_ranges")
    if isinstance(missing, dict):
        safe["missing_key_ranges"] = {
            bucket: [
                {
                    "count": item.get("count"),
                    "span": item.get("span"),
                }
                for item in ranges
                if isinstance(item, dict)
            ]
            for bucket, ranges in missing.items()
            if isinstance(ranges, list)
        }
    return safe


class _ColumnStats:
    def __init__(self, column: CompareColumn) -> None:
        self.column = column
        self.observed_rows = 0
        self.changed_rows = 0
        self.source_nulls = 0
        self.target_nulls = 0
        self.source_empty = 0
        self.target_empty = 0
        self.source_trim_changed = 0
        self.target_trim_changed = 0
        self.numeric_deltas: list[Decimal] = []
        self.time_deltas_seconds: list[int] = []
        self.date_truncation_count = 0

    def observe_distribution(self, *, source: object, target: object) -> None:
        self.observed_rows += 1
        self.source_nulls += int(source is None)
        self.target_nulls += int(target is None)
        self.source_empty += int(source == "")
        self.target_empty += int(target == "")
        self.source_trim_changed += int(_trim_changes(source))
        self.target_trim_changed += int(_trim_changes(target))

    def observe_change(self, *, source: object, target: object) -> None:
        self.changed_rows += 1
        if self.column.type in {ColumnType.INTEGER, ColumnType.DECIMAL, ColumnType.FLOAT}:
            delta = _decimal_delta(source, target)
            if delta is not None:
                self.numeric_deltas.append(delta)
        if self.column.type in {ColumnType.DATETIME, ColumnType.DATE, ColumnType.TIME}:
            delta_seconds = _time_delta_seconds(source, target)
            if delta_seconds is not None:
                self.time_deltas_seconds.append(delta_seconds)
            if _looks_like_date_truncation(source, target):
                self.date_truncation_count += 1

    def to_profile(self, *, diff_rows: int) -> dict[str, Any]:
        profile = {
            "type": self.column.type.value,
            "driver_type": self.column.driver_type,
            "observed_rows": self.observed_rows,
            "changed_rows": self.changed_rows,
            "diff_rate": self.changed_rows / diff_rows if diff_rows > 0 else 0.0,
            "nulls": {
                "source": self.source_nulls,
                "target": self.target_nulls,
                "delta": self.source_nulls - self.target_nulls,
            },
            "empty_strings": {
                "source": self.source_empty,
                "target": self.target_empty,
                "delta": self.source_empty - self.target_empty,
            },
            "trim_changed": {
                "source": self.source_trim_changed,
                "target": self.target_trim_changed,
            },
        }
        if self.numeric_deltas:
            profile["numeric_delta"] = _numeric_delta_profile(self.numeric_deltas)
        if self.time_deltas_seconds or self.date_truncation_count:
            profile["time_delta"] = _time_delta_profile(
                self.time_deltas_seconds,
                date_truncation_count=self.date_truncation_count,
            )
        return profile


class _RangeCluster:
    def __init__(self) -> None:
        self._values: list[int] = []

    def add(self, pk: tuple[object, ...]) -> None:
        scalar = _pk_scalar(pk)
        if scalar is not None:
            self._values.append(scalar)

    def ranges(self) -> list[dict[str, int]]:
        if not self._values:
            return []
        ranges: list[dict[str, int]] = []
        sorted_values = sorted(set(self._values))
        start = previous = sorted_values[0]
        for value in sorted_values[1:]:
            if value == previous + 1:
                previous = value
                continue
            ranges.append(_range_item(start, previous))
            start = previous = value
        ranges.append(_range_item(start, previous))
        return ranges


def _range_item(start: int, end: int) -> dict[str, int]:
    return {
        "start": start,
        "end": end,
        "count": end - start + 1,
        "span": end - start + 1,
    }


def _pk_scalar(pk: tuple[object, ...]) -> int | None:
    if len(pk) != 1:
        return None
    value = pk[0]
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal) and value == value.to_integral_value():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def _trim_changes(value: object) -> bool:
    return isinstance(value, str) and value != value.strip()


def _decimal_delta(source: object, target: object) -> Decimal | None:
    if source is None or target is None:
        return None
    try:
        return Decimal(str(source)) - Decimal(str(target))
    except (InvalidOperation, ValueError):
        return None


def _numeric_delta_profile(deltas: list[Decimal]) -> dict[str, Any]:
    constant = _constant(deltas)
    total = sum(deltas, Decimal("0"))
    return {
        "count": len(deltas),
        "min": str(min(deltas)),
        "max": str(max(deltas)),
        "avg": str(total / Decimal(len(deltas))),
        "constant_offset": str(constant) if constant is not None else None,
        "systematic_offset": constant is not None,
        "likely_rounding": any(delta != delta.to_integral_value() for delta in deltas),
    }


def _time_delta_profile(
    deltas: list[int],
    *,
    date_truncation_count: int,
) -> dict[str, Any]:
    constant = _constant(deltas)
    return {
        "count": len(deltas),
        "constant_offset_seconds": constant,
        "systematic_offset": constant is not None,
        "likely_timezone_offset": _is_timezone_offset(constant),
        "date_truncation_count": date_truncation_count,
    }


def _constant(values: list[Any]) -> Any | None:
    if not values:
        return None
    first = values[0]
    return first if all(value == first for value in values) else None


def _is_timezone_offset(seconds: int | None) -> bool:
    if seconds is None or seconds == 0:
        return False
    return abs(seconds) <= 14 * 3600 and seconds % 3600 == 0


def _time_delta_seconds(source: object, target: object) -> int | None:
    source_dt = _as_datetime(source)
    target_dt = _as_datetime(target)
    if source_dt is None or target_dt is None:
        return None
    return int((source_dt - target_dt).total_seconds())


def _looks_like_date_truncation(source: object, target: object) -> bool:
    source_dt = _as_datetime(source)
    target_dt = _as_datetime(target)
    if source_dt is None or target_dt is None:
        return False
    same_day = source_dt.date() == target_dt.date()
    source_midnight = source_dt.time() == time(0, 0, 0)
    target_midnight = target_dt.time() == time(0, 0, 0)
    return same_day and source_midnight != target_midnight


def _as_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime.combine(value, time(0, 0, 0))
    if isinstance(value, str):
        text = value.strip().replace("T", " ")
        if not text:
            return None
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            if len(text) >= 10:
                try:
                    return datetime.combine(date.fromisoformat(text[:10]), time(0, 0, 0))
                except ValueError:
                    return None
    return None


__all__ = [
    "DEFAULT_SAMPLE_CONFIDENCE",
    "DEFAULT_SAMPLE_SIZE",
    "DiffProfileBuilder",
    "empty_diff_profile",
    "profile_for_ai",
    "sample_quick_check_result",
    "zero_defect_difference_rate_upper_bound",
]
