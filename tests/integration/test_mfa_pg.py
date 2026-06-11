from __future__ import annotations

import base64
import os
import time
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, insert, select
from sqlalchemy.engine import Engine

from app.api.app import create_app
from app.api.services import ApiServices, RateLimiter
from app.db.models import metadata, secret_refs, users
from app.infrastructure.bootstrap.local_file import LocalFileBootstrapSecrets
from app.infrastructure.jobbackend.postgres import PostgresJobBackend
from app.infrastructure.resultstore.local_fs import LocalFsResultStore
from app.infrastructure.secretstore.local_file import LocalFileSecretStore
from app.infrastructure.secretstore.totp import totp_code_for_test
from tests._asgi_client import AsgiClient

pytestmark = pytest.mark.integration


def test_mfa_password_and_recovery_flow_on_pg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _pg_engine_or_skip()
    metadata.create_all(engine)
    _clear_metadata(engine)

    jwt_secret = uuid4().hex
    bootstrap = LocalFileBootstrapSecrets.from_config_dir(
        _write_bootstrap_files(tmp_path, jwt_secret)
    )
    secret_store = LocalFileSecretStore(engine, bootstrap)
    services = ApiServices(
        engine=engine,
        job_backend=PostgresJobBackend(engine),
        secret_store=secret_store,
        result_store=LocalFsResultStore(tmp_path / "results"),
        jwt_secret=bootstrap.get_jwt_secret(),
        rate_limiter=RateLimiter(limit=10_000),
    )
    user_id = str(uuid4())
    username_suffix = uuid4().hex[:24]
    username = f"mfa-{username_suffix}"
    original_password = uuid4().hex
    changed_password = uuid4().hex
    with engine.begin() as conn:
        conn.execute(
            insert(users).values(
                id=user_id,
                username=username,
                password_hash=secret_store.hash_password(original_password).ref,
                role="admin",
            )
        )

    client = AsgiClient(create_app(services=services))
    login_response = client.post(
        "/api/auth/login",
        json_body={"username": username, "password": original_password},
    )
    assert login_response.status_code == 200
    headers = {"Authorization": f"Bearer {login_response.json()['access_token']}"}

    password_response = client.post(
        "/api/account/password",
        headers=headers,
        json_body={"old_password": original_password, "new_password": changed_password},
    )
    assert password_response.status_code == 200
    assert password_response.json()["changed"] is True
    assert (
        client.post(
            "/api/auth/login",
            json_body={"username": username, "password": original_password},
        ).status_code
        == 401
    )

    login_response = client.post(
        "/api/auth/login",
        json_body={"username": username, "password": changed_password},
    )
    assert login_response.status_code == 200
    headers = {"Authorization": f"Bearer {login_response.json()['access_token']}"}

    enroll_response = client.post("/api/account/mfa/enroll", headers=headers)
    assert enroll_response.status_code == 200
    enroll_payload = enroll_response.json()
    assert enroll_payload["otpauth_uri"].startswith("otpauth://totp/")
    with engine.connect() as conn:
        old_pending_ref = conn.execute(
            select(users.c.mfa_pending_secret_ref).where(users.c.id == user_id)
        ).scalar_one()
    assert isinstance(old_pending_ref, str)

    enroll_response = client.post("/api/account/mfa/enroll", headers=headers)
    assert enroll_response.status_code == 200
    enroll_payload = enroll_response.json()
    with engine.connect() as conn:
        old_pending_row = conn.execute(
            select(secret_refs.c.ref).where(secret_refs.c.ref == old_pending_ref)
        ).one_or_none()
    assert old_pending_row is None

    secret = enroll_payload["secret"]
    base_time = int(time.time())
    monkeypatch.setattr("app.infrastructure.secretstore.totp.time.time", lambda: base_time)
    totp = totp_code_for_test(secret=secret, now=base_time)

    verify_response = client.post(
        "/api/account/mfa/verify",
        headers=headers,
        json_body={"code": totp},
    )
    assert verify_response.status_code == 200
    recovery_codes = verify_response.json()["recovery_codes"]
    assert len(recovery_codes) == 8

    assert (
        client.post(
            "/api/auth/login",
            json_body={"username": username, "password": changed_password},
        ).json()["error"]
        == "mfa_required"
    )

    enroll_code_replay_login = client.post(
        "/api/auth/login",
        json_body={"username": username, "password": changed_password, "mfa_code": totp},
    )
    assert enroll_code_replay_login.status_code == 401
    assert enroll_code_replay_login.json()["error"] == "invalid_mfa_code"

    login_time = base_time + 30
    monkeypatch.setattr("app.infrastructure.secretstore.totp.time.time", lambda: login_time)
    login_totp = totp_code_for_test(secret=secret, now=login_time)
    totp_login = client.post(
        "/api/auth/login",
        json_body={
            "username": username,
            "password": changed_password,
            "mfa_code": login_totp,
        },
    )
    assert totp_login.status_code == 200
    mfa_headers = {"Authorization": f"Bearer {totp_login.json()['access_token']}"}
    replayed_totp_login = client.post(
        "/api/auth/login",
        json_body={
            "username": username,
            "password": changed_password,
            "mfa_code": login_totp,
        },
    )
    assert replayed_totp_login.status_code == 401
    assert replayed_totp_login.json()["error"] == "invalid_mfa_code"

    recovery_login = client.post(
        "/api/auth/login",
        json_body={
            "username": username,
            "password": changed_password,
            "mfa_code": recovery_codes[0],
        },
    )
    assert recovery_login.status_code == 200
    assert (
        client.post(
            "/api/auth/login",
            json_body={
                "username": username,
                "password": changed_password,
                "mfa_code": recovery_codes[0],
            },
        ).json()["error"]
        == "invalid_mfa_code"
    )

    status_response = client.get("/api/account/security", headers=mfa_headers)
    assert status_response.status_code == 200
    assert status_response.json()["recovery_codes_total"] == 8
    assert status_response.json()["recovery_codes_used"] == 1

    regenerate_time = base_time + 60
    monkeypatch.setattr("app.infrastructure.secretstore.totp.time.time", lambda: regenerate_time)
    regenerate_totp = totp_code_for_test(secret=secret, now=regenerate_time)
    regenerate_response = client.post(
        "/api/account/recovery-codes/regenerate",
        headers=mfa_headers,
        json_body={"code": regenerate_totp},
    )
    assert regenerate_response.status_code == 200
    assert len(regenerate_response.json()["recovery_codes"]) == 8
    replayed_regenerate_response = client.post(
        "/api/account/recovery-codes/regenerate",
        headers=mfa_headers,
        json_body={"code": regenerate_totp},
    )
    assert replayed_regenerate_response.status_code == 401
    assert replayed_regenerate_response.json()["error"] == "invalid_mfa_code"
    replayed_disable_response = client.post(
        "/api/account/mfa/disable",
        headers=mfa_headers,
        json_body={"code": regenerate_totp},
    )
    assert replayed_disable_response.status_code == 401
    assert replayed_disable_response.json()["error"] == "invalid_mfa_code"

    disable_time = base_time + 90
    monkeypatch.setattr("app.infrastructure.secretstore.totp.time.time", lambda: disable_time)
    disable_totp = totp_code_for_test(secret=secret, now=disable_time)
    disable_response = client.post(
        "/api/account/mfa/disable",
        headers=mfa_headers,
        json_body={"code": disable_totp},
    )
    assert disable_response.status_code == 200

    admin_disable_response = client.post(
        f"/api/admin/users/{user_id}/mfa/disable",
        headers=mfa_headers,
    )
    assert admin_disable_response.status_code == 200
    assert admin_disable_response.json()["mfa_enabled"] is False
    assert (
        client.post(
            "/api/auth/login",
            json_body={"username": username, "password": changed_password},
        ).status_code
        == 200
    )


def _pg_engine_or_skip() -> Engine:
    url = os.environ.get("DATAOPS_TEST_PG_URL") or os.environ.get("DATAOPS_DATABASE_URL")
    if not url:
        pytest.skip("Postgres integration tests require DATAOPS_TEST_PG_URL")
    return create_engine(url, pool_size=5, max_overflow=10, pool_pre_ping=True)


def _write_bootstrap_files(tmp_path: Path, jwt_secret: str) -> Path:
    config_dir = tmp_path / "config"
    _write_secret(config_dir / ".secret_master.key", _fernet_key())
    _write_secret(config_dir / "secrets" / "pg_app_password", b"unused-in-test\n")
    _write_secret(config_dir / "secrets" / "pg_superuser_password", b"unused-in-test\n")
    _write_secret(config_dir / "secrets" / "jwt_secret", f"{jwt_secret}\n".encode())
    return config_dir


def _write_secret(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    if os.name != "nt":
        path.chmod(0o600)


def _fernet_key() -> bytes:
    return base64.urlsafe_b64encode(b"test-master-key-material-0000000")


def _clear_metadata(engine: Engine) -> None:
    with engine.begin() as conn:
        for table in reversed(metadata.sorted_tables):
            conn.execute(table.delete())
