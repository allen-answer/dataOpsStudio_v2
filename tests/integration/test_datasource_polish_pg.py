from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete, insert, select
from sqlalchemy.engine import Engine

from app.api.app import create_app
from app.api.security import create_access_token
from app.api.services import ApiServices
from app.db.models import audit_logs, datasources, job_events, jobs, metadata, projects, users
from app.domain.license import LicenseMode
from app.domain.secret import SecretKind, SecretRef
from tests._asgi_client import AsgiClient

pytestmark = pytest.mark.integration


def test_update_datasource_password_paths_on_pg() -> None:
    engine = _pg_engine_or_skip()
    owner_id, _project_id, datasource_id = _prepare_datasource(engine)
    secret_store = _SecretStore()
    app = create_app(services=cast(ApiServices, _Services(engine, secret_store)))

    keep_response = AsgiClient(app).request(
        "PUT",
        f"/api/datasources/{datasource_id}",
        headers=_auth_headers(owner_id),
        json_body={"name": "warehouse-keep-password", "password": ""},
    )

    assert keep_response.status_code == 200
    assert secret_store.stored == []
    assert secret_store.deleted == []
    with engine.connect() as conn:
        row = (
            conn.execute(select(datasources).where(datasources.c.id == datasource_id))
            .mappings()
            .one()
        )
    assert row["name"] == "warehouse-keep-password"
    assert row["password_secret_ref"] == "secret-old"

    rotate_response = AsgiClient(app).request(
        "PUT",
        f"/api/datasources/{datasource_id}",
        headers=_auth_headers(owner_id),
        json_body={"password": "local-test-replacement"},
    )

    assert rotate_response.status_code == 200
    assert secret_store.stored == ["local-test-replacement"]
    assert secret_store.deleted == ["secret-old"]
    with engine.connect() as conn:
        row = (
            conn.execute(select(datasources).where(datasources.c.id == datasource_id))
            .mappings()
            .one()
        )
    assert row["password_secret_ref"] == "secret-new-1"


def test_delete_datasource_reference_check_and_secret_cleanup_on_pg() -> None:
    engine = _pg_engine_or_skip()
    owner_id, project_id, datasource_id = _prepare_datasource(engine)
    secret_store = _SecretStore()
    app = create_app(services=cast(ApiServices, _Services(engine, secret_store)))
    job_id = str(uuid4())
    with engine.begin() as conn:
        conn.execute(
            insert(jobs).values(
                id=job_id,
                kind="sql_query",
                status="running",
                owner_user_id=owner_id,
                project_id=project_id,
                datasource_ids=[datasource_id],
                timeout_seconds=300,
                audit_id=str(uuid4()),
                payload={"datasource_id": datasource_id},
            )
        )

    blocked = AsgiClient(app).request(
        "DELETE",
        f"/api/datasources/{datasource_id}",
        headers=_auth_headers(owner_id),
    )

    assert blocked.status_code == 409
    assert blocked.json()["references"] == [
        {"job_id": job_id, "kind": "sql_query", "status": "running"}
    ]
    assert secret_store.deleted == []

    with engine.begin() as conn:
        conn.execute(delete(job_events).where(job_events.c.job_id == job_id))
        conn.execute(delete(jobs).where(jobs.c.id == job_id))

    deleted = AsgiClient(app).request(
        "DELETE",
        f"/api/datasources/{datasource_id}",
        headers=_auth_headers(owner_id),
    )

    assert deleted.status_code == 204
    assert secret_store.deleted == ["secret-old"]
    with engine.connect() as conn:
        row = (
            conn.execute(select(datasources.c.id).where(datasources.c.id == datasource_id))
            .mappings()
            .one_or_none()
        )
    assert row is None


def _pg_engine_or_skip() -> Engine:
    url = os.environ.get("DATAOPS_TEST_PG_URL") or os.environ.get("DATAOPS_DATABASE_URL")
    if not url:
        pytest.skip("Postgres integration tests require DATAOPS_TEST_PG_URL")
    return create_engine(url, pool_size=5, max_overflow=10)


def _prepare_datasource(engine: Engine) -> tuple[str, str, str]:
    metadata.create_all(engine)
    owner_id = str(uuid4())
    project_id = str(uuid4())
    datasource_id = str(uuid4())
    now = datetime.now(UTC)
    with engine.begin() as conn:
        conn.execute(delete(job_events))
        conn.execute(delete(jobs))
        conn.execute(delete(audit_logs))
        conn.execute(delete(datasources))
        conn.execute(delete(projects))
        conn.execute(delete(users))
        conn.execute(
            insert(users).values(
                id=owner_id,
                username=uuid4().hex,
                password_hash="not-a-real-hash",
            )
        )
        conn.execute(
            insert(projects).values(
                id=project_id,
                name=uuid4().hex,
                owner_user_id=owner_id,
            )
        )
        conn.execute(
            insert(datasources).values(
                id=datasource_id,
                project_id=project_id,
                name="warehouse",
                db_type="mysql",
                host="127.0.0.1",
                port=3306,
                username="dataops",
                database_name="app",
                password_secret_ref="secret-old",
                environment="sandbox",
                created_at=now,
                updated_at=now,
            )
        )
    return owner_id, project_id, datasource_id


def _auth_headers(user_id: str) -> dict[str, str]:
    token = create_access_token(user_id=user_id, role="admin", secret=_Services.jwt_secret)
    return {"Authorization": f"Bearer {token}"}


class _Services:
    jwt_secret = "jwt-secret"

    def __init__(self, engine: Engine, secret_store: _SecretStore) -> None:
        self.engine = engine
        self.secret_store = secret_store
        self.rate_limiter = _RateLimiter()
        self.audits: list[dict[str, object]] = []

    def current_license_mode(self) -> LicenseMode:
        return LicenseMode.TRIAL

    def is_token_revoked(
        self,
        *,
        user_id: str,
        issued_at: int,
        expires_at: int,
        jti: str | None,
    ) -> bool:
        del user_id, issued_at, expires_at, jti
        return False

    def write_audit(self, **kwargs: object) -> None:
        self.audits.append(kwargs)


class _RateLimiter:
    def allow(self, key: str) -> bool:
        return True


class _SecretStore:
    def __init__(self) -> None:
        self.stored: list[str] = []
        self.deleted: list[str] = []

    def store_secret(self, plaintext: str, kind: SecretKind) -> SecretRef:
        self.stored.append(plaintext)
        return SecretRef(
            ref=f"secret-new-{len(self.stored)}",
            kind=SecretKind.DATASOURCE_PASSWORD,
        )

    def delete_secret(self, ref: SecretRef) -> None:
        self.deleted.append(ref.ref)
