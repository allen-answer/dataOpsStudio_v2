from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import structlog
from sqlalchemy import and_, insert, or_, select, update
from sqlalchemy.engine import Connection, Engine, RowMapping

from app.db.models import (
    compare_result_inputs,
    console_statements,
    datasources,
    jobs,
    project_members,
    projects,
    result_sets,
)
from app.domain.compare_result_input import (
    ActorProject,
    CompareResultInputDescriptor,
    CompareResultInputGcReport,
    CompareResultInputState,
    CompareResultOrigin,
    CompareResultOriginKind,
    JobResultOrigin,
    OpenedCompareResultInput,
    StatementResultOrigin,
)
from app.domain.console_session import ConsoleStatementState
from app.domain.job import JobKind, JobStatus
from app.domain.schema import Column, Row
from app.infrastructure.resultstore.protocol import ResultStore

logger = structlog.get_logger(__name__)

_SNAPSHOTABLE_STATEMENT_STATES = frozenset(
    {
        ConsoleStatementState.SUCCEEDED.value,
        ConsoleStatementState.CANCELLED.value,
        ConsoleStatementState.TIMEOUT.value,
    }
)
_ACTIVE_STATEMENT_STATES = frozenset(
    {
        ConsoleStatementState.ACCEPTED.value,
        ConsoleStatementState.EXECUTING.value,
        ConsoleStatementState.STREAMING.value,
    }
)


class CompareResultInputError(RuntimeError):
    code = "compare_result_input_error"


class ResultOriginNotFound(CompareResultInputError):
    code = "result_origin_not_found"


class ResultNotTerminal(CompareResultInputError):
    code = "result_not_terminal"


class ResultNotComparable(CompareResultInputError):
    code = "result_not_comparable"


class ResultGone(CompareResultInputError):
    code = "result_gone"


class CompareResultInputNotFound(CompareResultInputError):
    code = "compare_input_not_found"


class CompareResultInputExpired(CompareResultInputError):
    code = "compare_input_expired"


class CompareResultInputDeleted(CompareResultInputError):
    code = "compare_input_deleted"


class CompareResultInputLost(CompareResultInputError):
    code = "compare_input_lost"


class CompareResultInputCaptureFailed(CompareResultInputError):
    code = "snapshot_capture_failed"


@dataclass(frozen=True)
class _ResolvedOrigin:
    kind: CompareResultOriginKind
    id: str
    result_set_id: str
    allow_failed_result_set: bool


class CompareResultInputs:
    """SQL 结果到 Compare 的不可变输入 module。

    interface 只暴露 capture/open/collect_expired;双来源解析、权限、spool snapshot
    与 tombstone 生命周期全部留在 implementation 内。
    """

    def __init__(
        self,
        engine: Engine,
        result_store: ResultStore,
        *,
        ttl_days: int = 7,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._engine = engine
        self._result_store = result_store
        self._ttl_days = ttl_days
        self._clock = clock or (lambda: datetime.now(UTC))

    def capture(
        self,
        origin: CompareResultOrigin,
        *,
        scope: ActorProject,
    ) -> CompareResultInputDescriptor:
        now = self._clock()
        with self._engine.connect() as conn:
            self._require_project_access(conn, scope)
            resolved = self._resolve_origin(conn, origin, scope)
            total_rows = self._require_stable_result_set(
                conn,
                resolved.result_set_id,
                allow_failed=resolved.allow_failed_result_set,
            )

        input_id = str(uuid4())
        try:
            snapshot = self._result_store.snapshot_spool(resolved.result_set_id, input_id)
        except FileNotFoundError as exc:
            raise ResultGone("source result spool is gone") from exc
        except Exception as exc:
            raise CompareResultInputCaptureFailed("result snapshot failed") from exc
        if not snapshot.columns:
            self._discard_snapshot(input_id)
            raise ResultNotComparable("result has no columns")

        descriptor = CompareResultInputDescriptor(
            id=input_id,
            project_id=scope.project_id,
            origin_kind=resolved.kind,
            origin_id=resolved.id,
            columns=snapshot.columns,
            loaded_rows=snapshot.loaded_rows,
            total_rows=total_rows,
            truncated=snapshot.truncated,
            has_more=snapshot.has_more,
            state="ready",
            created_at=now,
            expires_at=(
                datetime.max.replace(tzinfo=UTC)
                if self._ttl_days < 0
                else now + timedelta(days=self._ttl_days)
            ),
        )
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    insert(compare_result_inputs).values(
                        id=descriptor.id,
                        project_id=descriptor.project_id,
                        created_by=scope.actor_id,
                        origin_kind=descriptor.origin_kind,
                        origin_id=descriptor.origin_id,
                        source_result_set_id=resolved.result_set_id,
                        columns=[column.model_dump(mode="json") for column in descriptor.columns],
                        loaded_rows=descriptor.loaded_rows,
                        total_rows=descriptor.total_rows,
                        truncated=descriptor.truncated,
                        has_more=descriptor.has_more,
                        state=descriptor.state,
                        created_at=descriptor.created_at,
                        expires_at=descriptor.expires_at,
                    )
                )
        except Exception as exc:
            self._discard_snapshot(input_id)
            raise CompareResultInputCaptureFailed("result snapshot catalog write failed") from exc
        return descriptor

    def open(
        self,
        input_id: str,
        *,
        scope: ActorProject,
        batch_size: int,
        retain_until: datetime,
    ) -> OpenedCompareResultInput:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        now = self._clock()
        lease_until = _aware_datetime(retain_until)
        if lease_until <= now:
            raise ValueError("retain_until must be in the future")
        with self._engine.begin() as conn:
            self._require_project_access(conn, scope)
            row = (
                conn.execute(
                    select(compare_result_inputs)
                    .where(compare_result_inputs.c.id == input_id)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if (
                row is None
                or str(row["project_id"]) != scope.project_id
                or str(row["created_by"]) != scope.actor_id
            ):
                raise CompareResultInputNotFound("compare result input not found")
            if str(row["state"]) == "deleted":
                raise CompareResultInputDeleted("compare result input was deleted")
            current_expiry = _aware_datetime(row["expires_at"])
            if current_expiry <= now:
                raise CompareResultInputExpired("compare result input expired")
            if not self._result_store.spool_exists(input_id):
                raise CompareResultInputLost("compare result input spool is missing")
            effective_expiry = max(current_expiry, lease_until)
            if effective_expiry > current_expiry:
                conn.execute(
                    update(compare_result_inputs)
                    .where(compare_result_inputs.c.id == input_id)
                    .where(compare_result_inputs.c.state == "ready")
                    .values(expires_at=effective_expiry)
                )
            descriptor = _descriptor_from_row(row).model_copy(
                update={"expires_at": effective_expiry}
            )
        return OpenedCompareResultInput(
            descriptor=descriptor,
            batches=self._iter_batches(descriptor, batch_size),
        )

    def collect_expired(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> CompareResultInputGcReport:
        if limit <= 0:
            raise ValueError("limit must be positive")
        cutoff = now or self._clock()
        with self._engine.begin() as conn:
            rows = list(
                conn.execute(
                    select(compare_result_inputs.c.id)
                    .where(compare_result_inputs.c.state == "ready")
                    .where(compare_result_inputs.c.expires_at <= cutoff)
                    .order_by(compare_result_inputs.c.expires_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
                .scalars()
                .all()
            )
            deleted = 0
            failed = 0
            for value in rows:
                input_id = str(value)
                try:
                    self._result_store.delete_spool(input_id)
                except Exception:
                    failed += 1
                    continue
                conn.execute(
                    update(compare_result_inputs)
                    .where(compare_result_inputs.c.id == input_id)
                    .where(compare_result_inputs.c.state == "ready")
                    .values(state="deleted", deleted_at=cutoff)
                )
                deleted += 1
        with self._engine.connect() as conn:
            protected = frozenset(
                str(value)
                for value in conn.execute(
                    select(compare_result_inputs.c.id).where(
                        compare_result_inputs.c.state == "ready"
                    )
                )
                .scalars()
                .all()
            )
        return CompareResultInputGcReport(
            deleted=deleted,
            failed=failed,
            protected_result_set_ids=protected,
        )

    def _resolve_origin(
        self,
        conn: Connection,
        origin: CompareResultOrigin,
        scope: ActorProject,
    ) -> _ResolvedOrigin:
        if isinstance(origin, StatementResultOrigin):
            return self._resolve_statement(conn, origin, scope)
        return self._resolve_job(conn, origin, scope)

    def _resolve_statement(
        self,
        conn: Connection,
        origin: StatementResultOrigin,
        scope: ActorProject,
    ) -> _ResolvedOrigin:
        row = (
            conn.execute(
                select(
                    console_statements.c.id,
                    console_statements.c.state,
                    console_statements.c.result_set_id,
                    console_statements.c.owner_user_id,
                    datasources.c.project_id,
                )
                .join(datasources, datasources.c.id == console_statements.c.datasource_id)
                .where(console_statements.c.id == origin.statement_id)
            )
            .mappings()
            .one_or_none()
        )
        if (
            row is None
            or str(row["owner_user_id"]) != scope.actor_id
            or str(row["project_id"]) != scope.project_id
        ):
            raise ResultOriginNotFound("statement result origin not found")
        state = str(row["state"])
        if state in _ACTIVE_STATEMENT_STATES:
            raise ResultNotTerminal("statement result is not terminal")
        if state not in _SNAPSHOTABLE_STATEMENT_STATES or row["result_set_id"] is None:
            raise ResultNotComparable("statement result is not comparable")
        return _ResolvedOrigin(
            kind="statement",
            id=origin.statement_id,
            result_set_id=str(row["result_set_id"]),
            allow_failed_result_set=state
            in {ConsoleStatementState.CANCELLED.value, ConsoleStatementState.TIMEOUT.value},
        )

    def _resolve_job(
        self,
        conn: Connection,
        origin: JobResultOrigin,
        scope: ActorProject,
    ) -> _ResolvedOrigin:
        row = conn.execute(select(jobs).where(jobs.c.id == origin.job_id)).mappings().one_or_none()
        if (
            row is None
            or str(row["owner_user_id"]) != scope.actor_id
            or str(row["project_id"]) != scope.project_id
        ):
            raise ResultOriginNotFound("job result origin not found")
        status = str(row["status"])
        if status in {JobStatus.PENDING.value, JobStatus.RUNNING.value}:
            raise ResultNotTerminal("job result is not terminal")
        if str(row["kind"]) != JobKind.SQL_QUERY.value or status != JobStatus.SUCCESS.value:
            raise ResultNotComparable("job result is not comparable")
        payload = row["payload"]
        result_set_id = payload.get("result_set_id") if isinstance(payload, dict) else None
        if not isinstance(result_set_id, str) or not result_set_id:
            raise ResultNotComparable("job result set is missing")
        return _ResolvedOrigin(
            kind="job",
            id=origin.job_id,
            result_set_id=result_set_id,
            allow_failed_result_set=False,
        )

    def _require_stable_result_set(
        self,
        conn: Connection,
        result_set_id: str,
        *,
        allow_failed: bool,
    ) -> int | None:
        row = (
            conn.execute(
                select(result_sets.c.state, result_sets.c.total_rows).where(
                    result_sets.c.id == result_set_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None or str(row["state"]) == "closed":
            raise ResultGone("source result set is gone")
        if str(row["state"]) == "streaming":
            raise ResultNotTerminal("source result set is still streaming")
        if str(row["state"]) == "failed" and not allow_failed:
            raise ResultNotComparable("source result set failed")
        return _optional_non_negative_int(row["total_rows"])

    def _require_project_access(self, conn: Connection, scope: ActorProject) -> None:
        value = conn.execute(
            select(projects.c.id)
            .outerjoin(
                project_members,
                and_(
                    project_members.c.project_id == projects.c.id,
                    project_members.c.user_id == scope.actor_id,
                ),
            )
            .where(projects.c.id == scope.project_id)
            .where(
                or_(
                    projects.c.owner_user_id == scope.actor_id,
                    project_members.c.user_id == scope.actor_id,
                )
            )
        ).scalar_one_or_none()
        if value is None:
            raise ResultOriginNotFound("project not found")

    def _iter_batches(
        self,
        descriptor: CompareResultInputDescriptor,
        batch_size: int,
    ) -> Iterable[tuple[Row, ...]]:
        offset = 0
        while offset < descriptor.loaded_rows:
            rows = self._result_store.fetch_range(descriptor.id, offset, batch_size)
            if not rows:
                raise CompareResultInputLost("compare result input ended before loaded_rows")
            yield tuple(rows)
            offset += len(rows)

    def _discard_snapshot(self, input_id: str) -> None:
        try:
            self._result_store.delete_spool(input_id)
        except Exception as exc:
            logger.warning(
                "compare result snapshot cleanup failed",
                input_id=input_id,
                error_type=type(exc).__name__,
            )


def _descriptor_from_row(row: RowMapping) -> CompareResultInputDescriptor:
    raw_columns = row["columns"]
    columns = (
        tuple(Column.model_validate(item) for item in raw_columns if isinstance(item, dict))
        if isinstance(raw_columns, list)
        else ()
    )
    return CompareResultInputDescriptor(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        origin_kind=cast(CompareResultOriginKind, str(row["origin_kind"])),
        origin_id=str(row["origin_id"]),
        columns=columns,
        loaded_rows=_non_negative_int(row["loaded_rows"]),
        total_rows=_optional_non_negative_int(row["total_rows"]),
        truncated=bool(row["truncated"]),
        has_more=bool(row["has_more"]),
        state=cast(CompareResultInputState, str(row["state"])),
        created_at=_aware_datetime(row["created_at"]),
        expires_at=_aware_datetime(row["expires_at"]),
    )


def _non_negative_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _optional_non_negative_int(value: Any) -> int | None:
    if value is None:
        return None
    return _non_negative_int(value)


def _aware_datetime(value: Any) -> datetime:
    if not isinstance(value, datetime):
        raise CompareResultInputLost("compare result input timestamp is invalid")
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


__all__ = [
    "CompareResultInputCaptureFailed",
    "CompareResultInputDeleted",
    "CompareResultInputError",
    "CompareResultInputExpired",
    "CompareResultInputLost",
    "CompareResultInputNotFound",
    "CompareResultInputs",
    "ResultGone",
    "ResultNotComparable",
    "ResultNotTerminal",
    "ResultOriginNotFound",
]
