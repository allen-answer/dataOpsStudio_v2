from __future__ import annotations

import base64
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, insert
from sqlalchemy.engine import Engine

from app.api.app import create_app
from app.api.services import ApiServices, RateLimiter
from app.db.models import metadata, projects, users
from app.dbclients.mysql_adapter import MySQLAdapter
from app.domain.datasource import DatasourceConnInfo
from app.infrastructure.bootstrap.local_file import LocalFileBootstrapSecrets
from app.infrastructure.jobbackend.postgres import PostgresJobBackend
from app.infrastructure.resultstore.local_fs import LocalFsResultStore
from app.infrastructure.secretstore.local_file import LocalFileSecretStore
from app.worker import PostgresDatasourceLoader, WorkerRunner, WorkerRunnerConfig
from tests._asgi_client import AsgiClient

pytestmark = pytest.mark.integration


def test_e2e_login_create_mysql_datasource_execute_select(tmp_path: Path) -> None:
    pg_engine = _pg_engine_or_skip()
    mysql_env = _mysql_env_or_skip()
    metadata.create_all(pg_engine)
    _clear_metadata(pg_engine)

    bootstrap = LocalFileBootstrapSecrets.from_config_dir(_write_bootstrap_files(tmp_path))
    secret_store = LocalFileSecretStore(pg_engine, bootstrap)
    result_store = LocalFsResultStore(tmp_path / "results")
    api_backend = PostgresJobBackend(pg_engine)
    services = ApiServices(
        engine=pg_engine,
        job_backend=api_backend,
        secret_store=secret_store,
        result_store=result_store,
        jwt_secret=bootstrap.get_jwt_secret(),
        rate_limiter=RateLimiter(limit=10_000),
        job_wait_timeout_seconds=10.0,
    )
    app = create_app(services=services)
    client = AsgiClient(app)

    username = uuid4().hex
    password = "pw-for-e2e"
    project_id = _seed_user_and_project(pg_engine, secret_store, username, password)

    # This e2e uses an in-process worker thread to validate the functional API →
    # PG queue → worker → result spool chain. Real API-worker process separation
    # is covered by launcher/deployment validation, not by this test.
    stop_worker = threading.Event()
    worker_errors: list[BaseException] = []
    worker_thread = _start_worker_thread(
        pg_engine,
        secret_store,
        result_store,
        stop_worker,
        worker_errors,
    )

    try:
        login_response = client.post(
            "/api/auth/login",
            json_body={"username": username, "password": password},
        )
        assert login_response.status_code == 200
        token = login_response.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        datasource_response = client.post(
            "/api/datasources",
            headers=headers,
            json_body={
                "project_id": project_id,
                "name": "mysql-e2e",
                "db_type": "mysql",
                "host": mysql_env["host"],
                "port": int(mysql_env["port"]),
                "username": mysql_env["user"],
                "database": mysql_env["database"],
                "password": mysql_env["password"],
                "environment": "test",
                "extra": {},
            },
        )
        assert datasource_response.status_code == 201
        datasource_id = datasource_response.json()["id"]
        assert "password" not in datasource_response.json()

        test_response = client.post(f"/api/datasources/{datasource_id}/test", headers=headers)
        assert test_response.status_code == 200
        assert test_response.json() == {"ok": True}

        execute_response = client.post(
            "/api/sql/execute",
            headers=headers,
            json_body={"datasource_id": datasource_id, "sql": "SELECT 1 + 1 AS r", "params": {}},
        )
        assert execute_response.status_code == 202
        execution = execute_response.json()
        job_id = execution["job_id"]
        result_set_id = execution["result_set_id"]

        job_payload = _wait_for_success(client, headers, job_id)
        assert job_payload["result_set_id"] == result_set_id

        result_response = client.get(
            f"/api/jobs/{job_id}/result",
            headers=headers,
            params={"offset": 0, "limit": 10},
        )
        assert result_response.status_code == 200
        rows = result_response.json()["rows"]
        assert rows[0]["values"][0] == 2
        assert not worker_errors
    finally:
        stop_worker.set()
        worker_thread.join(timeout=5)


def _pg_engine_or_skip() -> Engine:
    url = os.environ.get("DATAOPS_TEST_PG_URL") or os.environ.get("DATAOPS_DATABASE_URL")
    if not url:
        pytest.skip("Postgres integration tests require DATAOPS_TEST_PG_URL")
    return create_engine(url, pool_size=10, max_overflow=20, pool_pre_ping=True)


def _mysql_env_or_skip() -> dict[str, str]:
    keys = {
        "host": "DATAOPS_TEST_MYSQL_HOST",
        "port": "DATAOPS_TEST_MYSQL_PORT",
        "user": "DATAOPS_TEST_MYSQL_USER",
        "password": "DATAOPS_TEST_MYSQL_PASSWORD",
        "database": "DATAOPS_TEST_MYSQL_DATABASE",
    }
    values = {name: os.environ.get(env_name) for name, env_name in keys.items()}
    missing = [keys[name] for name, value in values.items() if not value]
    if missing:
        pytest.skip(f"MySQL integration env not set: {', '.join(missing)}")
    return {name: str(value) for name, value in values.items()}


def _write_bootstrap_files(tmp_path: Path) -> Path:
    config_dir = tmp_path / "config"
    _write_secret(config_dir / ".secret_master.key", _fernet_key())
    _write_secret(config_dir / "secrets" / "pg_app_password", b"unused-in-test\n")
    _write_secret(config_dir / "secrets" / "pg_superuser_password", b"unused-in-test\n")
    _write_secret(config_dir / "secrets" / "jwt_secret", b"jwt-e2e-secret\n")
    return config_dir


def _write_secret(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    if os.name != "nt":
        path.chmod(0o400)


def _fernet_key() -> bytes:
    return base64.urlsafe_b64encode(b"test-master-key-material-0000000")


def _clear_metadata(engine: Engine) -> None:
    with engine.begin() as conn:
        for table in reversed(metadata.sorted_tables):
            conn.execute(table.delete())


def _seed_user_and_project(
    engine: Engine,
    secret_store: LocalFileSecretStore,
    username: str,
    password: str,
) -> str:
    user_id = str(uuid4())
    project_id = str(uuid4())
    password_hash = secret_store.hash_password(password).ref
    with engine.begin() as conn:
        conn.execute(
            insert(users).values(
                id=user_id,
                username=username,
                password_hash=password_hash,
                role="admin",
            )
        )
        conn.execute(
            insert(projects).values(
                id=project_id,
                name="E2E Project",
                owner_user_id=user_id,
            )
        )
    return project_id


def _start_worker_thread(
    engine: Engine,
    secret_store: LocalFileSecretStore,
    result_store: LocalFsResultStore,
    stop_worker: threading.Event,
    worker_errors: list[BaseException],
) -> threading.Thread:
    worker_id = str(uuid4())
    backend = PostgresJobBackend(engine, worker_id=worker_id)
    datasource_loader = PostgresDatasourceLoader(engine)

    def adapter_factory(
        conn_info: DatasourceConnInfo,
        cancel_check: Callable[[], bool],
    ) -> MySQLAdapter:
        return MySQLAdapter(
            conn_info,
            secret_store,
            cancel_check=cancel_check,
            statement_timeout_seconds=30,
            cursor_max_hold_seconds=300,
        )

    runner = WorkerRunner(
        backend,
        result_store,
        datasource_loader,
        cast(Callable[[DatasourceConnInfo, Callable[[], bool]], MySQLAdapter], adapter_factory),
        WorkerRunnerConfig(
            worker_id=worker_id,
            poll_interval_seconds=0.05,
            heartbeat_timeout_seconds=600,
        ),
    )

    def worker_loop() -> None:
        while not stop_worker.is_set():
            try:
                did_work = runner.run_once()
            except BaseException as exc:
                worker_errors.append(exc)
                return
            if not did_work:
                time.sleep(0.02)

    thread = threading.Thread(target=worker_loop, name="e2e-worker", daemon=True)
    thread.start()
    return thread


def _wait_for_success(
    client: AsgiClient,
    headers: dict[str, str],
    job_id: str,
) -> dict[str, object]:
    deadline = time.monotonic() + 10
    last_payload: dict[str, object] | None = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{job_id}", headers=headers)
        assert response.status_code == 200
        payload = cast(dict[str, object], response.json())
        last_payload = payload
        if payload["status"] == "success":
            return payload
        if payload["status"] in {"failed", "cancelled", "timeout"}:
            pytest.fail(f"job ended unexpectedly: {payload}")
        time.sleep(0.1)
    pytest.fail(f"job did not finish in time: {last_payload}")
