from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, insert, select
from sqlalchemy.engine import Engine

from app.api.app import create_app
from app.api.security import create_access_token
from app.api.services import ApiServices, RateLimiter
from app.db.models import compare_tasks, datasources, jobs, metadata, projects, run_index, users
from app.domain.job import JobKind, JobStatus
from app.infrastructure.jobbackend.postgres import PostgresJobBackend
from app.infrastructure.resultstore.local_fs import LocalFsResultStore
from app.infrastructure.secretstore.local_file import LocalFileSecretStore
from tests._asgi_client import AsgiClient

pytestmark = pytest.mark.integration


def test_compare_task_run_route_persists_job_before_run_index(tmp_path: Path) -> None:
    engine = _pg_engine_or_skip()
    metadata.create_all(engine)
    _clear_metadata(engine)
    jwt_secret = secrets.token_urlsafe(48)
    services = ApiServices(
        engine=engine,
        job_backend=PostgresJobBackend(engine),
        secret_store=cast(LocalFileSecretStore, object()),
        result_store=LocalFsResultStore(tmp_path / "results"),
        jwt_secret=jwt_secret,
        rate_limiter=RateLimiter(limit=10_000),
    )
    client = AsgiClient(create_app(services=services))
    seed = _seed_compare_task(engine)

    response = client.post(
        f"/api/compare/tasks/{seed.task_id}/run",
        headers=_headers(seed.user_id, jwt_secret),
    )

    assert response.status_code == 202
    payload = response.json()
    with engine.connect() as conn:
        job_row = (
            conn.execute(select(jobs).where(jobs.c.id == payload["job_id"]))
            .mappings()
            .one_or_none()
        )
        run_row = (
            conn.execute(select(run_index).where(run_index.c.run_id == payload["run_id"]))
            .mappings()
            .one_or_none()
        )
    assert job_row is not None
    assert job_row["kind"] == JobKind.COMPARE_RUN.value
    assert job_row["status"] == JobStatus.PENDING.value
    assert run_row is not None
    assert run_row["job_id"] == payload["job_id"]
    assert run_row["task_id"] == seed.task_id


class _SeededCompareTask:
    def __init__(self, *, user_id: str, task_id: str) -> None:
        self.user_id = user_id
        self.task_id = task_id


def _seed_compare_task(engine: Engine) -> _SeededCompareTask:
    user_id = str(uuid4())
    project_id = str(uuid4())
    source_id = str(uuid4())
    target_id = str(uuid4())
    task_id = str(uuid4())
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
        for datasource_id, name in (
            (source_id, "Source " + source_id),
            (target_id, "Target " + target_id),
        ):
            conn.execute(
                insert(datasources).values(
                    id=datasource_id,
                    project_id=project_id,
                    name=name,
                    db_type="mysql",
                    host="db.example.test",
                    port=3306,
                    username="dataops",
                    database_name="app",
                    password_secret_ref="not-used",
                )
            )
        conn.execute(
            insert(compare_tasks).values(
                id=task_id,
                project_id=project_id,
                name="orders compare",
                source_id=source_id,
                target_id=target_id,
                source_ref={"kind": "table", "schema_name": "app", "table_name": "orders_a"},
                target_ref={"kind": "table", "schema_name": "app", "table_name": "orders_b"},
                columns=[
                    {"name": "id", "type": "integer"},
                    {"name": "amount", "type": "decimal"},
                ],
                compare_rules={"key_columns": ["id"], "schema_policy": "strict"},
                run_limits={"query_timeout_seconds": 1800},
                created_by=user_id,
            )
        )
    return _SeededCompareTask(user_id=user_id, task_id=task_id)


def _pg_engine_or_skip() -> Engine:
    url = os.environ.get("DATAOPS_TEST_PG_URL") or os.environ.get("DATAOPS_DATABASE_URL")
    if not url:
        pytest.skip("Postgres integration tests require DATAOPS_TEST_PG_URL")
    return create_engine(url, pool_size=10, max_overflow=20, pool_pre_ping=True)


def _clear_metadata(engine: Engine) -> None:
    with engine.begin() as conn:
        for table in reversed(metadata.sorted_tables):
            conn.execute(table.delete())


def _headers(user_id: str, jwt_secret: str) -> dict[str, str]:
    token = create_access_token(user_id=user_id, role="admin", secret=jwt_secret)
    return {"Authorization": "Bearer " + token}
