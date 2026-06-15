from __future__ import annotations

import os
import secrets
import threading
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, insert, select
from sqlalchemy.engine import Engine

from app.api.app import create_app
from app.api.security import create_access_token
from app.api.services import ApiServices, RateLimiter
from app.db.models import datasources, jobs, metadata, projects, result_sets, users
from app.domain.compare import CompareHashPlan, CompareHashRequest
from app.domain.datasource import DatasourceConnInfo, DbType
from app.domain.job import JobKind, JobStatus
from app.domain.plan import PlanNode
from app.domain.result import ResultRef
from app.domain.schema import Column, ColumnType, Row
from app.domain.secret import SecretKind, SecretRef
from app.infrastructure.jobbackend.postgres import PostgresJobBackend
from app.infrastructure.resultstore.local_fs import LocalFsResultStore
from app.infrastructure.secretstore.local_file import LocalFileSecretStore
from app.worker import PostgresResultSetCatalog, WorkerRunner, WorkerRunnerConfig
from tests._asgi_client import AsgiClient

pytestmark = pytest.mark.integration


def test_sql_console_owner_scope_crud_and_limit(tmp_path: Path) -> None:
    engine = _pg_engine_or_skip()
    metadata.create_all(engine)
    _clear_metadata(engine)
    jwt_secret = _test_jwt_secret()
    services = _services(engine, tmp_path, jwt_secret=jwt_secret)
    client = AsgiClient(create_app(services=services))
    owner = _seed_user_project_datasource(engine, role="viewer")
    other = _seed_user_project_datasource(engine, role="viewer")
    owner_headers = _headers(owner.user_id, "viewer", jwt_secret)
    other_headers = _headers(other.user_id, "viewer", jwt_secret)

    created = client.post(
        "/api/sql/consoles",
        headers=owner_headers,
        json_body={
            "name": "scratch",
            "datasource_id": owner.datasource_id,
            "sql": "SELECT 1",
            "pinned": True,
        },
    )
    assert created.status_code == 201
    console_id = created.json()["id"]

    owner_list = client.get("/api/sql/consoles", headers=owner_headers)
    assert [item["id"] for item in owner_list.json()] == [console_id]
    other_list = client.get("/api/sql/consoles", headers=other_headers)
    assert other_list.json() == []

    other_patch = client.request(
        "PATCH",
        f"/api/sql/consoles/{console_id}",
        headers=other_headers,
        json_body={"name": "stolen"},
    )
    assert other_patch.status_code == 404

    patched = client.request(
        "PATCH",
        f"/api/sql/consoles/{console_id}",
        headers=owner_headers,
        json_body={"name": "renamed", "sql": "SELECT 2", "pinned": False},
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "renamed"
    assert patched.json()["sql"] == "SELECT 2"
    assert patched.json()["pinned"] is False

    deleted = client.request("DELETE", f"/api/sql/consoles/{console_id}", headers=owner_headers)
    assert deleted.status_code == 204

    for index in range(20):
        response = client.post(
            "/api/sql/consoles",
            headers=owner_headers,
            json_body={"name": "console " + str(index), "sql": "SELECT 1"},
        )
        assert response.status_code == 201
    overflow = client.post(
        "/api/sql/consoles",
        headers=owner_headers,
        json_body={"name": "overflow", "sql": "SELECT 1"},
    )
    assert overflow.status_code == 409
    assert overflow.json()["error"] == "console_limit_exceeded"


def test_sql_templates_are_public_read_admin_write_and_rendered_without_eval(
    tmp_path: Path,
) -> None:
    engine = _pg_engine_or_skip()
    metadata.create_all(engine)
    _clear_metadata(engine)
    jwt_secret = _test_jwt_secret()
    services = _services(engine, tmp_path, jwt_secret=jwt_secret)
    client = AsgiClient(create_app(services=services))
    admin = _seed_user_project_datasource(engine, role="admin")
    viewer = _seed_user_project_datasource(engine, role="viewer")
    admin_headers = _headers(admin.user_id, "admin", jwt_secret)
    viewer_headers = _headers(viewer.user_id, "viewer", jwt_secret)

    denied = client.post(
        "/api/sql/templates",
        headers=viewer_headers,
        json_body={"name": "viewer-write", "sql_text": "SELECT 1"},
    )
    assert denied.status_code == 403

    created = client.post(
        "/api/sql/templates",
        headers=admin_headers,
        json_body={
            "name": "by-id",
            "description": "lookup",
            "sql_text": "SELECT * FROM users WHERE id = {{user_id}}",
            "variables": ["user_id", "user_id", ""],
            "category": "lookup",
            "project_id": admin.project_id,
        },
    )
    assert created.status_code == 201
    template_id = created.json()["id"]
    assert created.json()["variables"] == ["user_id"]

    visible = client.get("/api/sql/templates", headers=viewer_headers, params={"q": "by-id"})
    assert visible.status_code == 200
    assert [item["id"] for item in visible.json()] == [template_id]

    rendered = client.post(
        f"/api/sql/templates/{template_id}/render",
        headers=viewer_headers,
        json_body={"values": {"user_id": "42; DROP TABLE users;"}},
    )
    assert rendered.status_code == 200
    assert rendered.json()["sql_text"] == "SELECT * FROM users WHERE id = 42; DROP TABLE users;"

    denied_patch = client.request(
        "PATCH",
        f"/api/sql/templates/{template_id}",
        headers=viewer_headers,
        json_body={"name": "viewer-edit"},
    )
    assert denied_patch.status_code == 403

    patched = client.request(
        "PATCH",
        f"/api/sql/templates/{template_id}",
        headers=admin_headers,
        json_body={"category": "admin"},
    )
    assert patched.status_code == 200
    assert patched.json()["category"] == "admin"

    denied_delete = client.request(
        "DELETE",
        f"/api/sql/templates/{template_id}",
        headers=viewer_headers,
    )
    assert denied_delete.status_code == 403
    deleted = client.request("DELETE", f"/api/sql/templates/{template_id}", headers=admin_headers)
    assert deleted.status_code == 204


def test_sql_history_is_derived_from_jobs_deduped_and_owner_scoped(tmp_path: Path) -> None:
    engine = _pg_engine_or_skip()
    metadata.create_all(engine)
    _clear_metadata(engine)
    jwt_secret = _test_jwt_secret()
    services = _services(engine, tmp_path, jwt_secret=jwt_secret)
    client = AsgiClient(create_app(services=services))
    owner = _seed_user_project_datasource(engine, role="viewer")
    other = _seed_user_project_datasource(engine, role="viewer")
    now = datetime.now(UTC)
    _seed_sql_job(
        engine,
        user_id=owner.user_id,
        project_id=owner.project_id,
        datasource_id=owner.datasource_id,
        job_id="job-old-duplicate",
        sql="SELECT 1",
        sql_hash="sha256:same",
        created_at=now - timedelta(hours=2),
    )
    _seed_sql_job(
        engine,
        user_id=owner.user_id,
        project_id=owner.project_id,
        datasource_id=owner.datasource_id,
        job_id="job-new-duplicate",
        sql="SELECT 1",
        sql_hash="sha256:same",
        created_at=now - timedelta(minutes=5),
    )
    _seed_sql_job(
        engine,
        user_id=owner.user_id,
        project_id=owner.project_id,
        datasource_id=owner.datasource_id,
        job_id="job-unique",
        sql="SELECT 2",
        sql_hash="sha256:unique",
        created_at=now - timedelta(minutes=30),
    )
    _seed_sql_job(
        engine,
        user_id=other.user_id,
        project_id=other.project_id,
        datasource_id=other.datasource_id,
        job_id="job-other-user",
        sql="SELECT secret",
        sql_hash="sha256:other",
        created_at=now,
    )

    response = client.get(
        "/api/sql/history",
        headers=_headers(owner.user_id, "viewer", jwt_secret),
        params={"datasource_id": owner.datasource_id},
    )
    assert response.status_code == 200
    payload = response.json()
    assert [item["job_id"] for item in payload] == ["job-new-duplicate", "job-unique"]
    assert {item["sql"] for item in payload} == {"SELECT 1", "SELECT 2"}

    recent = client.get(
        "/api/sql/history",
        headers=_headers(owner.user_id, "viewer", jwt_secret),
        params={
            "datasource_id": owner.datasource_id,
            "created_after": (now - timedelta(minutes=10)).isoformat(),
        },
    )
    assert recent.status_code == 200
    assert [item["job_id"] for item in recent.json()] == ["job-new-duplicate"]


def test_console_resultsets_lru_eviction_keeps_three_active(tmp_path: Path) -> None:
    engine = _pg_engine_or_skip()
    metadata.create_all(engine)
    _clear_metadata(engine)
    result_store = LocalFsResultStore(tmp_path / "results")
    jwt_secret = _test_jwt_secret()
    services = _services(engine, tmp_path, result_store=result_store, jwt_secret=jwt_secret)
    client = AsgiClient(create_app(services=services))
    owner = _seed_user_project_datasource(engine, role="admin")
    headers = _headers(owner.user_id, "admin", jwt_secret)
    console_response = client.post(
        "/api/sql/consoles",
        headers=headers,
        json_body={"name": "lru", "datasource_id": owner.datasource_id},
    )
    assert console_response.status_code == 201
    console_id = console_response.json()["id"]

    result_set_ids: list[str] = []
    for index in range(3):
        response = client.post(
            "/api/sql/execute",
            headers=headers,
            json_body={
                "datasource_id": owner.datasource_id,
                "console_id": console_id,
                "sql": "SELECT " + str(index),
                "params": {},
            },
        )
        assert response.status_code == 202
        result_set_ids.append(response.json()["result_set_id"])

    evicted_result_set_id = result_set_ids[0]
    result_store.append_spool(evicted_result_set_id, [Row(values=["old"])])
    evicted_spool_dir = tmp_path / "results" / "resultsets" / evicted_result_set_id
    assert evicted_spool_dir.exists()

    response = client.post(
        "/api/sql/execute",
        headers=headers,
        json_body={
            "datasource_id": owner.datasource_id,
            "console_id": console_id,
            "sql": "SELECT 3",
            "params": {},
        },
    )
    assert response.status_code == 202

    with engine.connect() as conn:
        states = (
            conn.execute(
                select(result_sets.c.state)
                .where(result_sets.c.console_id == console_id)
                .order_by(result_sets.c.created_at.asc())
            )
            .scalars()
            .all()
        )
    assert states.count("closed") == 1
    assert states.count("streaming") == 3
    assert not evicted_spool_dir.exists()


def test_running_sql_result_can_read_spooled_rows(tmp_path: Path) -> None:
    engine = _pg_engine_or_skip()
    metadata.create_all(engine)
    _clear_metadata(engine)
    result_store = LocalFsResultStore(tmp_path / "results")
    jwt_secret = _test_jwt_secret()
    services = _services(engine, tmp_path, result_store=result_store, jwt_secret=jwt_secret)
    client = AsgiClient(create_app(services=services))
    owner = _seed_user_project_datasource(engine, role="admin")
    headers = _headers(owner.user_id, "admin", jwt_secret)

    console_response = client.post(
        "/api/sql/consoles",
        headers=headers,
        json_body={"name": "slow", "datasource_id": owner.datasource_id, "sql": "SELECT 1"},
    )
    assert console_response.status_code == 201
    console_id = console_response.json()["id"]
    execute_response = client.post(
        "/api/sql/execute",
        headers=headers,
        json_body={
            "datasource_id": owner.datasource_id,
            "console_id": console_id,
            "sql": "SELECT n FROM slow_source",
            "params": {},
        },
    )
    assert execute_response.status_code == 202
    execution = execute_response.json()
    job_id = execution["job_id"]
    first_row_spooled = threading.Event()
    release_second_row = threading.Event()
    worker_errors: list[BaseException] = []
    runner = WorkerRunner(
        PostgresJobBackend(engine, worker_id="slow-worker"),
        result_store,
        lambda datasource_id: _conn_info(datasource_id),
        _slow_adapter_factory(release_second_row),
        WorkerRunnerConfig(
            worker_id="slow-worker",
            poll_interval_seconds=0.01,
            sql_spool_batch_size=1,
        ),
        _NotifyingCatalog(engine, first_row_spooled),
    )

    thread = threading.Thread(
        target=_run_once_capture_errors,
        args=(runner, worker_errors),
        daemon=True,
    )
    thread.start()

    assert first_row_spooled.wait(timeout=5), "worker did not spool the first row"
    result_response = client.get(
        f"/api/jobs/{job_id}/result",
        headers=headers,
        params={"offset": 0, "limit": 10},
    )
    assert result_response.status_code == 200
    partial = result_response.json()
    assert partial["state"] == "streaming"
    assert partial["loaded_rows"] == 1
    assert partial["rows"] == [{"values": [1]}]

    release_second_row.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert not worker_errors
    final_response = client.get(
        f"/api/jobs/{job_id}/result",
        headers=headers,
        params={"offset": 0, "limit": 10},
    )
    assert final_response.status_code == 200
    assert final_response.json()["state"] == "complete"
    assert final_response.json()["loaded_rows"] == 2


def _pg_engine_or_skip() -> Engine:
    url = os.environ.get("DATAOPS_TEST_PG_URL") or os.environ.get("DATAOPS_DATABASE_URL")
    if not url:
        pytest.skip("Postgres integration tests require DATAOPS_TEST_PG_URL")
    return create_engine(url, pool_size=10, max_overflow=20, pool_pre_ping=True)


def _services(
    engine: Engine,
    tmp_path: Path,
    *,
    result_store: LocalFsResultStore | None = None,
    jwt_secret: str,
) -> ApiServices:
    return ApiServices(
        engine=engine,
        job_backend=PostgresJobBackend(engine),
        secret_store=cast(LocalFileSecretStore, object()),
        result_store=result_store or LocalFsResultStore(tmp_path / "results"),
        jwt_secret=jwt_secret,
        rate_limiter=RateLimiter(limit=10_000),
        job_wait_timeout_seconds=1.0,
    )


def _clear_metadata(engine: Engine) -> None:
    with engine.begin() as conn:
        for table in reversed(metadata.sorted_tables):
            conn.execute(table.delete())


class _SeededScope:
    def __init__(self, *, user_id: str, project_id: str, datasource_id: str) -> None:
        self.user_id = user_id
        self.project_id = project_id
        self.datasource_id = datasource_id


def _seed_user_project_datasource(engine: Engine, *, role: str) -> _SeededScope:
    user_id = str(uuid4())
    project_id = str(uuid4())
    datasource_id = str(uuid4())
    with engine.begin() as conn:
        conn.execute(
            insert(users).values(
                id=user_id,
                username="user-" + user_id,
                password_hash="not-used-in-this-test",
                role=role,
            )
        )
        conn.execute(
            insert(projects).values(
                id=project_id,
                name="Project " + project_id,
                owner_user_id=user_id,
            )
        )
        conn.execute(
            insert(datasources).values(
                id=datasource_id,
                project_id=project_id,
                name="Datasource " + datasource_id,
                db_type="mysql",
                host="db.example.test",
                port=3306,
                username="dataops",
                database_name="app",
                password_secret_ref="not-used",
            )
        )
    return _SeededScope(user_id=user_id, project_id=project_id, datasource_id=datasource_id)


def _test_jwt_secret() -> str:
    return secrets.token_urlsafe(48)


def _headers(user_id: str, role: str, jwt_secret: str) -> dict[str, str]:
    token = create_access_token(user_id=user_id, role=role, secret=jwt_secret)
    return {"Authorization": "Bearer " + token}


def _seed_sql_job(
    engine: Engine,
    *,
    user_id: str,
    project_id: str,
    datasource_id: str,
    job_id: str,
    sql: str,
    sql_hash: str,
    created_at: datetime,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(jobs).values(
                id=job_id,
                kind=JobKind.SQL_QUERY.value,
                status=JobStatus.SUCCESS.value,
                owner_user_id=user_id,
                project_id=project_id,
                datasource_ids=[datasource_id],
                timeout_seconds=300,
                resource_profile={},
                audit_id=str(uuid4()),
                payload={
                    "datasource_id": datasource_id,
                    "sql": sql,
                    "sql_hash": sql_hash,
                    "result_set_id": "rs-" + job_id,
                },
                created_at=created_at,
                finished_at=created_at + timedelta(seconds=1),
            )
        )


def _conn_info(datasource_id: str) -> DatasourceConnInfo:
    del datasource_id
    return DatasourceConnInfo(
        host="db.example.test",
        port=3306,
        username="dataops",
        database="app",
        password_ref=SecretRef(ref="not-used", kind=SecretKind.DATASOURCE_PASSWORD),
        db_type=DbType.MYSQL,
    )


class _SlowAdapter:
    def __init__(self, release_second_row: threading.Event) -> None:
        self._release_second_row = release_second_row
        self._column_sink: Callable[[list[Column]], None] | None = None

    def with_column_sink(self, column_sink: Callable[[list[Column]], None]) -> _SlowAdapter:
        self._column_sink = column_sink
        return self

    def execute_select(self, sql: str, params: dict[str, object]) -> Iterable[Row]:
        del sql, params
        if self._column_sink is not None:
            self._column_sink([Column(name="n", type=ColumnType.INTEGER)])
        yield Row(values=[1])
        assert self._release_second_row.wait(timeout=5)
        yield Row(values=[2])

    def explain(self, sql: str) -> PlanNode:
        del sql
        return PlanNode(
            operation="EXPLAIN",
            details={"rows": [{"operation": "EXPLAIN"}]},
        )

    def test_connection(self) -> bool:
        return True

    def build_compare_hash_query(self, request: CompareHashRequest) -> CompareHashPlan:
        del request
        raise NotImplementedError


def _slow_adapter_factory(
    release_second_row: threading.Event,
) -> Callable[
    [DatasourceConnInfo, Callable[[], bool], Callable[[list[Column]], None]],
    _SlowAdapter,
]:
    adapter = _SlowAdapter(release_second_row)

    def factory(
        conn_info: DatasourceConnInfo,
        cancel_check: Callable[[], bool],
        column_sink: Callable[[list[Column]], None],
    ) -> _SlowAdapter:
        del conn_info, cancel_check
        return adapter.with_column_sink(column_sink)

    return factory


class _NotifyingCatalog(PostgresResultSetCatalog):
    def __init__(self, engine: Engine, first_row_spooled: threading.Event) -> None:
        super().__init__(engine)
        self._first_row_spooled = first_row_spooled

    def write_streaming(
        self,
        *,
        result_set_id: str,
        execution_id: str,
        storage_ref: ResultRef,
        columns: list[Column],
        loaded_rows: int,
        console_id: str | None,
    ) -> None:
        super().write_streaming(
            result_set_id=result_set_id,
            execution_id=execution_id,
            storage_ref=storage_ref,
            columns=columns,
            loaded_rows=loaded_rows,
            console_id=console_id,
        )
        if loaded_rows >= 1:
            self._first_row_spooled.set()


def _run_once_capture_errors(runner: WorkerRunner, errors: list[BaseException]) -> None:
    try:
        runner.run_once()
    except BaseException as exc:
        errors.append(exc)
