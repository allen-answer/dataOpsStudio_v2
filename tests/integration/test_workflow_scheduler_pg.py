"""Workflow cron tick 端到端(env-gate,无 PG 则 skip):seed → fire → dedup →
禁用不触发,针对真实 PG 校验原子认领 + workflow_run 入队(迁移 0022 列)。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, insert, select, update
from sqlalchemy.engine import Engine

from app.api.services import ApiServices
from app.db.models import jobs, metadata, projects, users, workflows
from app.domain.workflow import WorkflowSpec
from app.infrastructure.jobbackend.postgres import PostgresJobBackend
from app.services.workflow_scheduler import WorkflowScheduler

pytestmark = pytest.mark.integration

_CRON = "*/15 * * * *"
_SPEC = {
    "nodes": [
        {
            "id": "extract",
            "job_kind": "sql_query",
            "payload": {"sql": "SELECT 1"},
            "timeout_seconds": 60,
        }
    ],
    "edges": [],
    "schedule": {"cron": _CRON, "enabled": True},
}


@dataclass
class _FakeServices:
    """WorkflowScheduler 只用 engine / job_backend / write_audit 三件套。"""

    engine: Engine
    job_backend: PostgresJobBackend
    audits: list[dict[str, Any]] = field(default_factory=list)

    def write_audit(self, **kwargs: Any) -> None:
        self.audits.append(kwargs)


def test_scheduler_seed_fire_dedup_and_disabled() -> None:
    engine = _pg_engine_or_skip()
    metadata.create_all(engine)
    _clear_metadata(engine)
    try:
        user_id, project_id = _seed_user_and_project(engine)
        workflow_id = _seed_workflow(engine, project_id=project_id, created_by=user_id)
        services = _FakeServices(engine=engine, job_backend=PostgresJobBackend(engine))
        scheduler = WorkflowScheduler(cast(ApiServices, services), tick_interval_seconds=30.0)

        t0 = datetime(2026, 7, 8, 10, 7, 30, tzinfo=UTC)  # 最近点 10:00

        # 1) 首次 tick:播种基线,不入队
        scheduler.tick(t0)
        assert _run_job_count(engine) == 0
        assert _last_fired(engine, workflow_id) == datetime(2026, 7, 8, 10, 0, tzinfo=UTC)

        # 2) 同一分钟再 tick:防重,仍不入队
        scheduler.tick(t0)
        assert _run_job_count(engine) == 0

        # 3) 跨到新触发点(10:30):入队 1 个 workflow_run
        t1 = datetime(2026, 7, 8, 10, 37, 0, tzinfo=UTC)  # 最近点 10:30
        scheduler.tick(t1)
        assert _run_job_count(engine) == 1
        job_row = _one_run_job(engine)
        assert job_row["kind"] == "workflow_run"
        assert job_row["parent_workflow_run_id"] is None
        assert job_row["owner_user_id"] == user_id
        assert job_row["payload"]["trigger"] == "schedule"
        assert job_row["payload"]["workflow_id"] == workflow_id
        assert _last_fired(engine, workflow_id) == datetime(2026, 7, 8, 10, 30, tzinfo=UTC)
        assert len(services.audits) == 1

        # 4) 同点再 tick:防重,仍是 1 个
        scheduler.tick(t1)
        assert _run_job_count(engine) == 1

        # 5) 禁用调度后跨新点:不再触发
        with engine.begin() as conn:
            conn.execute(
                update(workflows)
                .where(workflows.c.id == workflow_id)
                .values(schedule_enabled=False)
            )
        t2 = datetime(2026, 7, 8, 11, 7, 0, tzinfo=UTC)
        scheduler.tick(t2)
        assert _run_job_count(engine) == 1
    finally:
        _clear_metadata(engine)


def _seed_workflow(engine: Engine, *, project_id: str, created_by: str) -> str:
    workflow_id = str(uuid4())
    spec = WorkflowSpec.model_validate(_SPEC)
    with engine.begin() as conn:
        conn.execute(
            insert(workflows).values(
                id=workflow_id,
                project_id=project_id,
                name="daily-" + workflow_id,
                dag_jsonb=spec.model_dump(mode="json"),
                schedule_cron=_CRON,
                schedule_enabled=True,
                enabled=True,
                created_by=created_by,
            )
        )
    return workflow_id


def _run_job_count(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                select(func.count()).select_from(jobs).where(jobs.c.kind == "workflow_run")
            ).scalar_one()
        )


def _one_run_job(engine: Engine) -> dict[str, Any]:
    with engine.connect() as conn:
        return dict(
            conn.execute(select(jobs).where(jobs.c.kind == "workflow_run")).mappings().one()
        )


def _last_fired(engine: Engine, workflow_id: str) -> datetime | None:
    with engine.connect() as conn:
        value = conn.execute(
            select(workflows.c.schedule_last_fired_at).where(workflows.c.id == workflow_id)
        ).scalar_one()
    if value is None:
        return None
    assert isinstance(value, datetime)
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _seed_user_and_project(engine: Engine) -> tuple[str, str]:
    user_id = str(uuid4())
    project_id = str(uuid4())
    with engine.begin() as conn:
        conn.execute(
            insert(users).values(
                id=user_id,
                username="user-" + user_id,
                password_hash="not-used-in-this-test",
                role="admin",
            )
        )
        conn.execute(
            insert(projects).values(
                id=project_id,
                name="Project " + project_id,
                owner_user_id=user_id,
            )
        )
    return user_id, project_id


def _pg_engine_or_skip() -> Engine:
    url = os.environ.get("DATAOPS_TEST_PG_URL") or os.environ.get("DATAOPS_DATABASE_URL")
    if not url:
        pytest.skip("Postgres integration tests require DATAOPS_TEST_PG_URL")
    return create_engine(url, pool_size=10, max_overflow=20, pool_pre_ping=True)


def _clear_metadata(engine: Engine) -> None:
    with engine.begin() as conn:
        for table in reversed(metadata.sorted_tables):
            conn.execute(table.delete())
