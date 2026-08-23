from __future__ import annotations

import re
from collections import Counter
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.domain.compare import CompareRules
from app.domain.schema import Column, ColumnType, Index, Table


class MappingReason(StrEnum):
    EXACT = "exact"
    NORMALIZED = "normalized"
    TYPE_COMPATIBLE = "type-compatible"


class ConflictKind(StrEnum):
    ONE_TO_MANY = "one_to_many"
    MANY_TO_ONE = "many_to_one"
    MANY_TO_MANY = "many_to_many"


class PrimaryKeyReason(StrEnum):
    PRIMARY_KEY = "primary_key"
    UNIQUE_INDEX = "unique_index"


class TableSuggestionReason(StrEnum):
    EXACT = "exact"
    NORMALIZED = "normalized"


class ColumnMappingCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_column: str
    target_column: str
    source_type: ColumnType
    target_type: ColumnType
    confidence: float = Field(ge=0, le=1)
    reason: MappingReason
    conflict: bool = False
    conflict_kind: ConflictKind | None = None


class PrimaryKeyCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_columns: list[str]
    target_columns: list[str]
    confidence: float = Field(ge=0, le=1)
    reason: PrimaryKeyReason


class CompareInferenceDraft(BaseModel):
    model_config = ConfigDict(frozen=True)

    mappings: list[ColumnMappingCandidate]
    pk_candidates: list[PrimaryKeyCandidate]
    needs_manual_pk: bool
    compare_rules: CompareRules
    columns: list[Column]


class TablePairSuggestion(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_schema: str
    source_table: str
    target_schema: str
    target_table: str
    confidence: float = Field(ge=0, le=1)
    reason: TableSuggestionReason


_PHRASE_ALIASES = {
    "\u7f16\u53f7": "id",
    "\u540d\u79f0": "name",
    "\u59d3\u540d": "name",
    "\u7528\u6237": "user",
    "\u8d26\u53f7": "account",
    "\u91d1\u989d": "amount",
    "\u6570\u91cf": "quantity",
    "\u65e5\u671f": "date",
    "\u65f6\u95f4": "time",
    "\u72b6\u6001": "status",
    "\u7c7b\u578b": "type",
    "\u63cf\u8ff0": "description",
    "\u5907\u6ce8": "remark",
}
_TOKEN_ALIASES = {
    "uid": "id",
    "pk": "id",
    "num": "number",
    "no": "number",
    "amt": "amount",
    "qty": "quantity",
    "cnt": "count",
    "dt": "date",
    "ts": "time",
    "desc": "description",
    "memo": "remark",
}
_AFFIX_TOKENS = {
    "src",
    "source",
    "s",
    "tgt",
    "target",
    "dst",
    "dest",
    "old",
    "new",
    "ods",
    "dwd",
    "tbl",
    "tb",
}
_NUMERIC_TYPES = {ColumnType.INTEGER, ColumnType.FLOAT, ColumnType.DECIMAL}
_TEMPORAL_TYPES = {ColumnType.DATE, ColumnType.DATETIME}


def infer_compare_draft(
    *,
    source_columns: list[Column],
    target_columns: list[Column],
    source_indexes: list[Index] | None = None,
    target_indexes: list[Index] | None = None,
) -> CompareInferenceDraft:
    mappings = infer_column_mappings(source_columns, target_columns)
    pk_candidates = infer_primary_key_candidates(
        source_columns=source_columns,
        target_columns=target_columns,
        source_indexes=source_indexes or [],
        target_indexes=target_indexes or [],
        mappings=mappings,
    )
    selected_mappings = select_confirmed_mappings(mappings)
    key_columns = pk_candidates[0].source_columns if pk_candidates else []
    compare_columns = [
        column
        for column in source_columns
        if column.name in selected_mappings and column.name not in set(key_columns)
    ]
    return CompareInferenceDraft(
        mappings=mappings,
        pk_candidates=pk_candidates,
        needs_manual_pk=not pk_candidates,
        compare_rules=CompareRules(
            key_columns=key_columns,
            column_mappings=selected_mappings,
        ),
        columns=compare_columns,
    )


def infer_column_mappings(
    source_columns: list[Column],
    target_columns: list[Column],
) -> list[ColumnMappingCandidate]:
    candidates: list[ColumnMappingCandidate] = []
    for source in source_columns:
        source_norm = _normalized_identifier(source.name)
        source_tokens = _normalized_tokens(source.name)
        for target in target_columns:
            if not column_types_compatible(source.type, target.type):
                continue
            target_norm = _normalized_identifier(target.name)
            target_tokens = _normalized_tokens(target.name)
            reason: MappingReason | None = None
            confidence = 0.0
            if source.name == target.name:
                reason = MappingReason.EXACT
                confidence = 1.0
            elif source.name.lower() == target.name.lower():
                reason = MappingReason.EXACT
                confidence = 0.98
            elif source_norm and source_norm == target_norm:
                reason = MappingReason.NORMALIZED
                confidence = 0.9
            elif _token_subset_match(source_tokens, target_tokens):
                reason = MappingReason.TYPE_COMPATIBLE
                confidence = 0.72
            if reason is None:
                continue
            confidence = max(0.0, confidence - _type_penalty(source.type, target.type))
            candidates.append(
                ColumnMappingCandidate(
                    source_column=source.name,
                    target_column=target.name,
                    source_type=source.type,
                    target_type=target.type,
                    confidence=round(confidence, 3),
                    reason=reason,
                )
            )
    return _mark_mapping_conflicts(candidates)


def infer_primary_key_candidates(
    *,
    source_columns: list[Column],
    target_columns: list[Column],
    source_indexes: list[Index],
    target_indexes: list[Index],
    mappings: list[ColumnMappingCandidate],
) -> list[PrimaryKeyCandidate]:
    selected_mappings = select_confirmed_mappings(mappings)
    mapping_confidence = {
        candidate.source_column: candidate.confidence
        for candidate in mappings
        if not candidate.conflict
    }
    source_constraints = _key_constraints(source_columns, source_indexes)
    target_constraints = _key_constraints(target_columns, target_indexes)
    candidates: list[PrimaryKeyCandidate] = []
    seen: set[tuple[tuple[str, ...], tuple[str, ...], PrimaryKeyReason]] = set()
    for source_key, source_reason in source_constraints:
        mapped_target = [selected_mappings.get(column) for column in source_key]
        if any(column is None for column in mapped_target):
            continue
        target_key = [str(column) for column in mapped_target]
        for candidate_target, target_reason in target_constraints:
            if set(target_key) != set(candidate_target):
                continue
            reason = (
                PrimaryKeyReason.PRIMARY_KEY
                if source_reason is PrimaryKeyReason.PRIMARY_KEY
                or target_reason is PrimaryKeyReason.PRIMARY_KEY
                else PrimaryKeyReason.UNIQUE_INDEX
            )
            identity = (tuple(source_key), tuple(target_key), reason)
            if identity in seen:
                continue
            seen.add(identity)
            base_confidence = 0.98 if reason is PrimaryKeyReason.PRIMARY_KEY else 0.86
            if source_key:
                base_confidence *= min(mapping_confidence.get(column, 0.7) for column in source_key)
            candidates.append(
                PrimaryKeyCandidate(
                    source_columns=list(source_key),
                    target_columns=target_key,
                    confidence=round(base_confidence, 3),
                    reason=reason,
                )
            )
            break
    return sorted(
        candidates,
        key=lambda candidate: (
            0 if candidate.reason is PrimaryKeyReason.PRIMARY_KEY else 1,
            -candidate.confidence,
            candidate.source_columns,
        ),
    )


def infer_table_pair_suggestions(
    source_tables: list[Table],
    target_tables: list[Table],
    *,
    limit: int = 100,
) -> list[TablePairSuggestion]:
    targets_by_normalized_name: dict[str, list[Table]] = {}
    for target in target_tables:
        normalized = _normalized_identifier(target.name)
        targets_by_normalized_name.setdefault(normalized, []).append(target)

    suggestions: list[TablePairSuggestion] = []
    for source in source_tables:
        source_norm = _normalized_identifier(source.name)
        for target in targets_by_normalized_name.get(source_norm, []):
            reason: TableSuggestionReason | None = None
            confidence = 0.0
            if source.name == target.name or source.name.lower() == target.name.lower():
                reason = TableSuggestionReason.EXACT
                confidence = 1.0 if source.name == target.name else 0.98
            elif source_norm and source_norm == _normalized_identifier(target.name):
                reason = TableSuggestionReason.NORMALIZED
                confidence = 0.9
            if reason is None:
                continue
            suggestions.append(
                TablePairSuggestion(
                    source_schema=source.schema_name,
                    source_table=source.name,
                    target_schema=target.schema_name,
                    target_table=target.name,
                    confidence=confidence,
                    reason=reason,
                )
            )
    return sorted(
        suggestions,
        key=lambda item: (
            0 if item.reason is TableSuggestionReason.EXACT else 1,
            -item.confidence,
            item.source_schema.lower(),
            item.source_table.lower(),
            item.target_schema.lower(),
            item.target_table.lower(),
        ),
    )[:limit]


def select_confirmed_mappings(candidates: list[ColumnMappingCandidate]) -> dict[str, str]:
    selected: dict[str, str] = {}
    used_targets: set[str] = set()
    for candidate in sorted(
        candidates,
        key=lambda item: (-item.confidence, item.source_column.lower(), item.target_column.lower()),
    ):
        if candidate.conflict:
            continue
        if candidate.source_column in selected or candidate.target_column in used_targets:
            continue
        selected[candidate.source_column] = candidate.target_column
        used_targets.add(candidate.target_column)
    return dict(sorted(selected.items()))


def column_types_compatible(source_type: ColumnType, target_type: ColumnType) -> bool:
    if source_type == target_type:
        return True
    if source_type in _NUMERIC_TYPES and target_type in _NUMERIC_TYPES:
        return True
    if source_type in _TEMPORAL_TYPES and target_type in _TEMPORAL_TYPES:
        return True
    if {source_type, target_type} == {ColumnType.BOOLEAN, ColumnType.INTEGER}:
        return True
    return False


def _mark_mapping_conflicts(
    candidates: list[ColumnMappingCandidate],
) -> list[ColumnMappingCandidate]:
    source_counts = Counter(candidate.source_column for candidate in candidates)
    target_counts = Counter(candidate.target_column for candidate in candidates)
    marked: list[ColumnMappingCandidate] = []
    for candidate in candidates:
        source_conflict = source_counts[candidate.source_column] > 1
        target_conflict = target_counts[candidate.target_column] > 1
        conflict_kind: ConflictKind | None = None
        if source_conflict and target_conflict:
            conflict_kind = ConflictKind.MANY_TO_MANY
        elif source_conflict:
            conflict_kind = ConflictKind.ONE_TO_MANY
        elif target_conflict:
            conflict_kind = ConflictKind.MANY_TO_ONE
        marked.append(
            candidate.model_copy(
                update={
                    "conflict": conflict_kind is not None,
                    "conflict_kind": conflict_kind,
                }
            )
        )
    return sorted(
        marked,
        key=lambda item: (
            item.source_column.lower(),
            -item.confidence,
            item.target_column.lower(),
        ),
    )


def _key_constraints(
    columns: list[Column],
    indexes: list[Index],
) -> list[tuple[list[str], PrimaryKeyReason]]:
    primary_columns = [column.name for column in columns if column.primary_key]
    constraints: list[tuple[list[str], PrimaryKeyReason]] = []
    if primary_columns:
        constraints.append((primary_columns, PrimaryKeyReason.PRIMARY_KEY))
    for index in indexes:
        if not index.columns:
            continue
        if index.is_primary:
            if not primary_columns:
                constraints.append((list(index.columns), PrimaryKeyReason.PRIMARY_KEY))
            continue
        if index.is_unique:
            constraints.append((list(index.columns), PrimaryKeyReason.UNIQUE_INDEX))
    return constraints


def _normalized_identifier(name: str) -> str:
    return "".join(_normalized_tokens(name))


def _normalized_tokens(name: str) -> tuple[str, ...]:
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    value = value.lower()
    for phrase, alias in _PHRASE_ALIASES.items():
        value = value.replace(phrase, f"_{alias}_")
    tokens = [_TOKEN_ALIASES.get(token, token) for token in re.split(r"[^a-z0-9]+", value) if token]
    while len(tokens) > 1 and tokens[0] in _AFFIX_TOKENS:
        tokens.pop(0)
    while len(tokens) > 1 and tokens[-1] in _AFFIX_TOKENS:
        tokens.pop()
    return tuple(tokens)


def _token_subset_match(source_tokens: tuple[str, ...], target_tokens: tuple[str, ...]) -> bool:
    if not source_tokens or not target_tokens:
        return False
    source_set = set(source_tokens)
    target_set = set(target_tokens)
    if source_set == target_set:
        return True
    shared = source_set & target_set
    if not shared:
        return False
    smaller = min(len(source_set), len(target_set))
    return len(shared) == smaller and smaller >= 2


def _type_penalty(source_type: ColumnType, target_type: ColumnType) -> float:
    return 0.0 if source_type == target_type else 0.08
