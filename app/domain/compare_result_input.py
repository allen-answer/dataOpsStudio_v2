from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.domain.schema import Column, Row

CompareResultOriginKind = Literal["statement", "job"]
CompareResultInputState = Literal["ready", "deleted"]


@dataclass(frozen=True)
class StatementResultOrigin:
    statement_id: str


@dataclass(frozen=True)
class JobResultOrigin:
    job_id: str


type CompareResultOrigin = StatementResultOrigin | JobResultOrigin


@dataclass(frozen=True)
class ActorProject:
    actor_id: str
    project_id: str


class CompareResultInputDescriptor(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    project_id: str
    origin_kind: CompareResultOriginKind
    origin_id: str
    columns: tuple[Column, ...]
    loaded_rows: int
    total_rows: int | None
    truncated: bool
    has_more: bool
    state: CompareResultInputState
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class OpenedCompareResultInput:
    descriptor: CompareResultInputDescriptor
    batches: Iterable[tuple[Row, ...]]


@dataclass(frozen=True)
class CompareResultInputGcReport:
    deleted: int
    failed: int
    protected_result_set_ids: frozenset[str] = frozenset()


__all__ = [
    "ActorProject",
    "CompareResultInputDescriptor",
    "CompareResultInputGcReport",
    "CompareResultInputState",
    "CompareResultOrigin",
    "CompareResultOriginKind",
    "JobResultOrigin",
    "OpenedCompareResultInput",
    "StatementResultOrigin",
]
