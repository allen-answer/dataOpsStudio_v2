from __future__ import annotations

from collections import deque
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Engine

from app.domain.compare_result_input import ActorProject, JobResultOrigin, StatementResultOrigin
from app.domain.schema import Column, ColumnType, Row
from app.infrastructure.compare_result_inputs import (
    CompareResultInputCaptureFailed,
    CompareResultInputExpired,
    CompareResultInputNotFound,
    CompareResultInputs,
    ResultColumnsNotUnique,
    ResultNotTerminal,
    ResultOriginNotFound,
    ResultPartialConfirmationRequired,
)
from app.infrastructure.resultstore.local_fs import LocalFsResultStore

_NOW = datetime(2026, 8, 23, 8, 0, tzinfo=UTC)


def test_capture_and_open_statement_result_survives_source_deletion(tmp_path: Path) -> None:
    store = _source_store(tmp_path)
    capture_engine = _FakeEngine(
        _scalar("project-1"),
        _row(
            {
                "id": "statement-1",
                "state": "succeeded",
                "result_set_id": "rs-source",
                "owner_user_id": "user-1",
                "project_id": "project-1",
            }
        ),
        _row({"state": "complete", "total_rows": 2}),
    )
    module = CompareResultInputs(
        cast(Engine, capture_engine),
        store,
        ttl_days=7,
        clock=lambda: _NOW,
    )

    descriptor = module.capture(
        StatementResultOrigin("statement-1"),
        scope=ActorProject(actor_id="user-1", project_id="project-1"),
    )

    assert descriptor.origin_kind == "statement"
    assert descriptor.loaded_rows == 2
    assert descriptor.total_rows == 2
    assert descriptor.expires_at == _NOW + timedelta(days=7)
    assert capture_engine.write_count == 1
    assert store.delete_spool("rs-source") is True

    open_engine = _FakeEngine(
        _scalar("project-1"),
        _row(
            {
                **descriptor.model_dump(mode="python"),
                "origin_kind": "statement",
                "state": "ready",
                "created_by": "user-1",
            }
        ),
    )
    opened = CompareResultInputs(
        cast(Engine, open_engine),
        store,
        ttl_days=7,
        clock=lambda: _NOW,
    ).open(
        descriptor.id,
        scope=ActorProject(actor_id="user-1", project_id="project-1"),
        batch_size=1,
        retain_until=_NOW + timedelta(hours=1),
    )

    assert list(opened.batches) == [
        (Row(values=[1, "a"]),),
        (Row(values=[2, "b"]),),
    ]


def test_capture_rejects_active_statement_without_creating_snapshot(tmp_path: Path) -> None:
    store = _source_store(tmp_path)
    engine = _FakeEngine(
        _scalar("project-1"),
        _row(
            {
                "id": "statement-1",
                "state": "streaming",
                "result_set_id": "rs-source",
                "owner_user_id": "user-1",
                "project_id": "project-1",
                "created_by": "user-1",
            }
        ),
    )
    module = CompareResultInputs(cast(Engine, engine), store, clock=lambda: _NOW)

    with pytest.raises(ResultNotTerminal):
        module.capture(
            StatementResultOrigin("statement-1"),
            scope=ActorProject(actor_id="user-1", project_id="project-1"),
        )

    assert _snapshot_ids(tmp_path) == []


def test_capture_requires_partial_confirmation_without_leaving_snapshot(tmp_path: Path) -> None:
    store = _source_store(tmp_path)
    engine = _FakeEngine(
        _scalar("project-1"),
        _row(
            {
                "id": "statement-1",
                "state": "cancelled",
                "result_set_id": "rs-source",
                "owner_user_id": "user-1",
                "project_id": "project-1",
            }
        ),
        _row({"state": "complete", "total_rows": 2}),
    )
    module = CompareResultInputs(cast(Engine, engine), store, clock=lambda: _NOW)

    with pytest.raises(ResultPartialConfirmationRequired):
        module.capture(
            StatementResultOrigin("statement-1"),
            scope=ActorProject(actor_id="user-1", project_id="project-1"),
        )

    assert engine.write_count == 0
    assert _snapshot_ids(tmp_path) == []


def test_capture_accepts_explicitly_confirmed_partial_result(tmp_path: Path) -> None:
    store = _source_store(tmp_path)
    engine = _FakeEngine(
        _scalar("project-1"),
        _row(
            {
                "id": "statement-1",
                "state": "cancelled",
                "result_set_id": "rs-source",
                "owner_user_id": "user-1",
                "project_id": "project-1",
            }
        ),
        _row({"state": "complete", "total_rows": 2}),
    )

    descriptor = CompareResultInputs(cast(Engine, engine), store, clock=lambda: _NOW).capture(
        StatementResultOrigin("statement-1"),
        scope=ActorProject(actor_id="user-1", project_id="project-1"),
        allow_partial=True,
    )

    assert descriptor.truncated is True
    assert descriptor.has_more is False
    assert descriptor.total_rows is None
    assert engine.write_count == 1


def test_capture_rejects_duplicate_column_names_without_leaving_snapshot(tmp_path: Path) -> None:
    store = _source_store(tmp_path)
    store.set_spool_columns(
        "rs-source",
        [
            Column(name="id", type=ColumnType.INTEGER),
            Column(name="id", type=ColumnType.STRING),
        ],
    )
    engine = _FakeEngine(
        _scalar("project-1"),
        _row(
            {
                "id": "statement-1",
                "state": "succeeded",
                "result_set_id": "rs-source",
                "owner_user_id": "user-1",
                "project_id": "project-1",
            }
        ),
        _row({"state": "complete", "total_rows": 2}),
    )

    with pytest.raises(ResultColumnsNotUnique):
        CompareResultInputs(cast(Engine, engine), store, clock=lambda: _NOW).capture(
            StatementResultOrigin("statement-1"),
            scope=ActorProject(actor_id="user-1", project_id="project-1"),
        )

    assert engine.write_count == 0
    assert _snapshot_ids(tmp_path) == []


def test_capture_supports_legacy_sql_job_result(tmp_path: Path) -> None:
    store = _source_store(tmp_path)
    engine = _FakeEngine(
        _scalar("project-1"),
        _row(
            {
                "id": "job-1",
                "kind": "sql_query",
                "status": "success",
                "owner_user_id": "user-1",
                "project_id": "project-1",
                "payload": {"result_set_id": "rs-source"},
            }
        ),
        _row({"state": "complete", "total_rows": 2}),
    )
    module = CompareResultInputs(cast(Engine, engine), store, clock=lambda: _NOW)

    descriptor = module.capture(
        JobResultOrigin("job-1"),
        scope=ActorProject(actor_id="user-1", project_id="project-1"),
    )

    assert descriptor.origin_kind == "job"
    assert descriptor.origin_id == "job-1"
    assert descriptor.total_rows == 2


def test_capture_hides_cross_user_statement_as_not_found(tmp_path: Path) -> None:
    store = _source_store(tmp_path)
    engine = _FakeEngine(
        _scalar("project-1"),
        _row(
            {
                "id": "statement-1",
                "state": "succeeded",
                "result_set_id": "rs-source",
                "owner_user_id": "other-user",
                "project_id": "project-1",
            }
        ),
    )
    module = CompareResultInputs(cast(Engine, engine), store, clock=lambda: _NOW)

    with pytest.raises(ResultOriginNotFound):
        module.capture(
            StatementResultOrigin("statement-1"),
            scope=ActorProject(actor_id="user-1", project_id="project-1"),
        )

    assert _snapshot_ids(tmp_path) == []


def test_capture_removes_snapshot_when_catalog_write_fails(tmp_path: Path) -> None:
    store = _source_store(tmp_path)
    engine = _FakeEngine(
        _scalar("project-1"),
        _row(
            {
                "id": "statement-1",
                "state": "succeeded",
                "result_set_id": "rs-source",
                "owner_user_id": "user-1",
                "project_id": "project-1",
            }
        ),
        _row({"state": "complete", "total_rows": 2}),
        fail_writes=True,
    )
    module = CompareResultInputs(cast(Engine, engine), store, clock=lambda: _NOW)

    with pytest.raises(CompareResultInputCaptureFailed):
        module.capture(
            StatementResultOrigin("statement-1"),
            scope=ActorProject(actor_id="user-1", project_id="project-1"),
        )

    assert _snapshot_ids(tmp_path) == []


def test_open_rejects_expired_input_before_reading_rows(tmp_path: Path) -> None:
    store = _source_store(tmp_path)
    store.snapshot_spool("rs-source", "input-1")
    engine = _FakeEngine(
        _scalar("project-1"),
        _row(
            {
                "id": "input-1",
                "project_id": "project-1",
                "created_by": "user-1",
                "origin_kind": "statement",
                "origin_id": "statement-1",
                "columns": [Column(name="id", type=ColumnType.INTEGER).model_dump(mode="json")],
                "loaded_rows": 2,
                "total_rows": 2,
                "truncated": False,
                "has_more": False,
                "state": "ready",
                "created_at": _NOW - timedelta(days=8),
                "expires_at": _NOW - timedelta(seconds=1),
            }
        ),
    )
    module = CompareResultInputs(cast(Engine, engine), store, clock=lambda: _NOW)

    with pytest.raises(CompareResultInputExpired):
        module.open(
            "input-1",
            scope=ActorProject(actor_id="user-1", project_id="project-1"),
            batch_size=100,
            retain_until=_NOW + timedelta(hours=1),
        )


def test_open_hides_other_project_members_snapshot_as_not_found(tmp_path: Path) -> None:
    store = _source_store(tmp_path)
    store.snapshot_spool("rs-source", "input-1")
    engine = _FakeEngine(
        _scalar("project-1"),
        _row(
            {
                "id": "input-1",
                "project_id": "project-1",
                "created_by": "other-user",
                "origin_kind": "statement",
                "origin_id": "statement-1",
                "columns": [],
                "loaded_rows": 2,
                "total_rows": 2,
                "truncated": False,
                "has_more": False,
                "state": "ready",
                "created_at": _NOW,
                "expires_at": _NOW + timedelta(days=1),
            }
        ),
    )
    module = CompareResultInputs(cast(Engine, engine), store, clock=lambda: _NOW)

    with pytest.raises(CompareResultInputNotFound):
        module.open(
            "input-1",
            scope=ActorProject(actor_id="user-1", project_id="project-1"),
            batch_size=100,
            retain_until=_NOW + timedelta(hours=1),
        )


def test_open_extends_catalog_lease_before_returning_batches(tmp_path: Path) -> None:
    store = _source_store(tmp_path)
    store.snapshot_spool("rs-source", "input-1")
    engine = _FakeEngine(
        _scalar("project-1"),
        _row(
            {
                "id": "input-1",
                "project_id": "project-1",
                "created_by": "user-1",
                "origin_kind": "statement",
                "origin_id": "statement-1",
                "columns": [Column(name="id", type=ColumnType.INTEGER).model_dump(mode="json")],
                "loaded_rows": 2,
                "total_rows": 2,
                "truncated": False,
                "has_more": False,
                "state": "ready",
                "created_at": _NOW,
                "expires_at": _NOW + timedelta(minutes=1),
            }
        ),
    )
    module = CompareResultInputs(cast(Engine, engine), store, clock=lambda: _NOW)

    opened = module.open(
        "input-1",
        scope=ActorProject(actor_id="user-1", project_id="project-1"),
        batch_size=100,
        retain_until=_NOW + timedelta(hours=1),
    )

    assert opened.descriptor.expires_at == _NOW + timedelta(hours=1)
    assert engine.write_count == 1
    dialect = postgresql.dialect()  # type: ignore[no-untyped-call]
    compiled = str(engine.statements[1].compile(dialect=dialect))
    assert "FOR UPDATE" in compiled


def test_collect_expired_deletes_snapshot_and_leaves_catalog_tombstone(tmp_path: Path) -> None:
    store = _source_store(tmp_path)
    store.snapshot_spool("rs-source", "input-1")
    engine = _FakeEngine(
        _FakeResult(scalars=["input-1"]),
        _FakeResult(scalars=[]),
    )
    module = CompareResultInputs(cast(Engine, engine), store, clock=lambda: _NOW)

    report = module.collect_expired(now=_NOW, limit=10)

    assert report.deleted == 1
    assert report.failed == 0
    assert report.protected_result_set_ids == frozenset()
    assert store.spool_exists("input-1") is False
    assert engine.write_count == 1
    dialect = postgresql.dialect()  # type: ignore[no-untyped-call]
    compiled = str(engine.statements[0].compile(dialect=dialect))
    assert "FOR UPDATE SKIP LOCKED" in compiled


def _source_store(root: Path) -> LocalFsResultStore:
    store = LocalFsResultStore(root)
    store.set_spool_columns(
        "rs-source",
        [
            Column(name="id", type=ColumnType.INTEGER),
            Column(name="name", type=ColumnType.STRING),
        ],
    )
    store.append_spool(
        "rs-source",
        [Row(values=[1, "a"]), Row(values=[2, "b"])],
    )
    return store


def _snapshot_ids(root: Path) -> list[str]:
    resultsets = root / "resultsets"
    return sorted(path.name for path in resultsets.iterdir() if path.name != "rs-source")


class _FakeResult:
    def __init__(
        self,
        *,
        row: dict[str, Any] | None = None,
        scalar: object | None = None,
        scalars: list[object] | None = None,
    ) -> None:
        self._row = row
        self._scalar = scalar
        self._scalars = scalars or []

    def mappings(self) -> _FakeResult:
        return self

    def one_or_none(self) -> dict[str, Any] | None:
        return self._row

    def scalar_one_or_none(self) -> object | None:
        return self._scalar

    def scalars(self) -> _FakeResult:
        return self

    def all(self) -> list[object]:
        return self._scalars


def _row(value: dict[str, Any]) -> _FakeResult:
    return _FakeResult(row=value)


def _scalar(value: object) -> _FakeResult:
    return _FakeResult(scalar=value)


class _FakeConnection:
    def __init__(self, results: deque[_FakeResult], *, fail_writes: bool) -> None:
        self._results = results
        self._fail_writes = fail_writes
        self.write_count = 0
        self.statements: list[Any] = []

    def execute(self, statement: Any) -> _FakeResult:
        self.statements.append(statement)
        if bool(getattr(statement, "is_insert", False)) or bool(
            getattr(statement, "is_update", False)
        ):
            self.write_count += 1
            if self._fail_writes:
                raise RuntimeError("catalog write failed")
            return _FakeResult()
        return self._results.popleft()


class _FakeContext(AbstractContextManager[_FakeConnection]):
    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection

    def __enter__(self) -> _FakeConnection:
        return self._connection

    def __exit__(self, *args: object) -> None:
        return None


class _FakeEngine:
    def __init__(self, *results: _FakeResult, fail_writes: bool = False) -> None:
        self._connection = _FakeConnection(deque(results), fail_writes=fail_writes)

    @property
    def write_count(self) -> int:
        return self._connection.write_count

    @property
    def statements(self) -> list[Any]:
        return self._connection.statements

    def connect(self) -> _FakeContext:
        return _FakeContext(self._connection)

    def begin(self) -> _FakeContext:
        return _FakeContext(self._connection)
