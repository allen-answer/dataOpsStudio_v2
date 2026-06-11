from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, insert
from sqlalchemy.engine import Engine

from app.api.app import create_app
from app.api.security import create_access_token
from app.api.services import ApiServices, RateLimiter
from app.db.models import metadata, revoked_tokens, users
from tests._asgi_client import AsgiClient

pytestmark = pytest.mark.integration


def test_force_logout_revokes_existing_token_on_pg() -> None:
    engine = _pg_engine_or_skip()
    metadata.create_all(engine)
    _clear_metadata(engine)

    user_id = str(uuid4())
    issued_at = int(time.time()) - 60
    services = ApiServices(
        engine=engine,
        job_backend=cast(Any, object()),
        secret_store=cast(Any, object()),
        result_store=cast(Any, object()),
        jwt_secret="jwt-integration-secret",
        rate_limiter=RateLimiter(limit=10_000),
    )
    with engine.begin() as conn:
        conn.execute(
            insert(users).values(
                id=user_id,
                username="admin-revocation-test",
                password_hash="unused",
                role="admin",
            )
        )

    token = create_access_token(
        user_id=user_id,
        role="admin",
        secret=services.jwt_secret,
        issued_at=issued_at,
        ttl_seconds=3600,
        jti="admin-token",
    )
    client = AsgiClient(create_app(services=services))
    headers = {"Authorization": f"Bearer {token}"}

    force_response = client.post(
        f"/api/admin/users/{user_id}/force-logout",
        headers=headers,
    )
    assert force_response.status_code == 200

    reused_response = client.get("/api/admin/users", headers=headers)
    assert reused_response.status_code == 401
    assert reused_response.json()["error"] == "unauthorized"


def test_jti_denylist_rejects_token_on_pg() -> None:
    engine = _pg_engine_or_skip()
    metadata.create_all(engine)
    _clear_metadata(engine)

    user_id = str(uuid4())
    issued_at = int(time.time()) - 60
    expires_at = issued_at + 3600
    services = ApiServices(
        engine=engine,
        job_backend=cast(Any, object()),
        secret_store=cast(Any, object()),
        result_store=cast(Any, object()),
        jwt_secret="jwt-integration-secret",
        rate_limiter=RateLimiter(limit=10_000),
    )
    with engine.begin() as conn:
        conn.execute(
            insert(users).values(
                id=user_id,
                username="admin-jti-denylist-test",
                password_hash="unused",
                role="admin",
            )
        )
        conn.execute(
            insert(revoked_tokens).values(
                jti="revoked-token",
                user_id=user_id,
                expires_at=datetime.fromtimestamp(expires_at, UTC),
            )
        )

    token = create_access_token(
        user_id=user_id,
        role="admin",
        secret=services.jwt_secret,
        issued_at=issued_at,
        ttl_seconds=3600,
        jti="revoked-token",
    )
    client = AsgiClient(create_app(services=services))

    response = client.get(
        "/api/admin/users",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"


def _pg_engine_or_skip() -> Engine:
    url = os.environ.get("DATAOPS_TEST_PG_URL") or os.environ.get("DATAOPS_DATABASE_URL")
    if not url:
        pytest.skip("Postgres integration tests require DATAOPS_TEST_PG_URL")
    return create_engine(url, pool_size=5, max_overflow=10, pool_pre_ping=True)


def _clear_metadata(engine: Engine) -> None:
    with engine.begin() as conn:
        for table in reversed(metadata.sorted_tables):
            conn.execute(table.delete())
