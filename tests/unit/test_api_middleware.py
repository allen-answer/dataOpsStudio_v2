from __future__ import annotations

import asyncio
import threading
from typing import Literal, cast

import pytest
from fastapi import FastAPI, Request

from app.api.middleware.crosscutting import CrossCuttingMiddleware
from app.api.security import create_access_token
from app.api.services import ApiServices, RateLimiter, RateLimitRule
from app.domain.license import LicenseMode
from tests._asgi_client import AsgiClient

_BLOCKING_CALL_TIMEOUT_SECONDS = 2.0


def test_crosscutting_middleware_requires_auth_for_api_paths() -> None:
    services = _FakeServices()
    app = _app_with_services(services)

    response = AsgiClient(app).get("/api/protected")

    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized", "message": "Authentication required"}


def test_crosscutting_middleware_authenticates_and_audits_success() -> None:
    services = _FakeServices()
    app = _app_with_services(services)
    token = create_access_token(
        user_id="user-1",
        role="admin",
        secret=services.jwt_secret,
        jti="jti-1",
    )

    response = AsgiClient(app).get(
        "/api/protected",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == {"user_id": "user-1"}
    assert [item["action"] for item in services.audits] == [
        "api_request_start",
        "api_request_end",
    ]
    assert services.revocation_checks == [
        {
            "user_id": "user-1",
            "jti": "jti-1",
        }
    ]
    assert services.call_order == [
        "token_revocation",
        "license",
        "api_request_start",
        "api_request_end",
    ]
    assert response.headers["x-request-id"]


def test_crosscutting_middleware_keeps_loop_responsive_during_license_lookup() -> None:
    asyncio.run(_assert_sync_io_does_not_block_event_loop("license"))


def test_crosscutting_middleware_keeps_loop_responsive_during_token_revocation_lookup() -> None:
    asyncio.run(_assert_sync_io_does_not_block_event_loop("revocation"))


def test_crosscutting_middleware_keeps_loop_responsive_during_request_audit() -> None:
    asyncio.run(_assert_sync_io_does_not_block_event_loop("audit_start"))


def test_crosscutting_middleware_keeps_loop_responsive_during_response_audit() -> None:
    asyncio.run(_assert_sync_io_does_not_block_event_loop("audit_end"))


def test_crosscutting_middleware_audits_error_before_reraising_handler_exception() -> None:
    services = _FakeServices()
    app = _app_with_services(services)
    token = create_access_token(user_id="user-1", role="admin", secret=services.jwt_secret)

    with pytest.raises(RuntimeError, match="handler failed"):
        AsgiClient(app).get(
            "/api/fails",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert [item["result"] for item in services.audits] == ["started", "error"]
    assert services.call_order == [
        "token_revocation",
        "license",
        "api_request_start",
        "api_request_end",
    ]


def test_crosscutting_middleware_bounds_long_http_audit_resource_id() -> None:
    services = _FakeServices()
    app = _app_with_services(services)
    token = create_access_token(
        user_id="user-1",
        role="admin",
        secret=services.jwt_secret,
        jti="jti-1",
    )

    response = AsgiClient(app).post(
        "/api/sql/templates/12345678-1234-1234-1234-123456789012/render",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    resource_ids = [cast(str, item["resource_id"]) for item in services.audits]
    assert len(resource_ids) == 2
    assert resource_ids[0] == resource_ids[1]
    assert resource_ids[0].startswith("POST /api/sql/templates/")
    assert "#" in resource_ids[0]
    assert all(len(resource_id) <= 64 for resource_id in resource_ids)


def test_crosscutting_middleware_rejects_revoked_token() -> None:
    services = _FakeServices()
    services.token_revoked = True
    app = _app_with_services(services)
    token = create_access_token(user_id="user-1", role="admin", secret=services.jwt_secret)

    response = AsgiClient(app).get(
        "/api/protected",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized", "message": "Authentication required"}
    assert services.audits == []


def test_crosscutting_middleware_blocks_repair_mode_mutations() -> None:
    services = _FakeServices()
    services.mode = LicenseMode.REPAIR
    app = _app_with_services(services)
    token = create_access_token(user_id="user-1", role="admin", secret=services.jwt_secret)

    response = AsgiClient(app).post(
        "/api/sql/execute",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    assert response.json()["error"] == "license_repair_mode"


def test_crosscutting_middleware_blocks_repair_mode_datasource_create() -> None:
    services = _FakeServices()
    services.mode = LicenseMode.REPAIR
    app = _app_with_services(services)
    token = create_access_token(user_id="user-1", role="admin", secret=services.jwt_secret)

    response = AsgiClient(app).post(
        "/api/datasources",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    assert response.json()["error"] == "license_repair_mode"


def test_crosscutting_middleware_allows_repair_mode_license_update() -> None:
    services = _FakeServices()
    services.mode = LicenseMode.REPAIR
    app = _app_with_services(services)
    token = create_access_token(user_id="user-1", role="admin", secret=services.jwt_secret)

    response = AsgiClient(app).post(
        "/api/admin/license",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}


def test_crosscutting_middleware_blocks_in_grace_mutations() -> None:
    services = _FakeServices()
    services.mode = LicenseMode.IN_GRACE
    app = _app_with_services(services)
    token = create_access_token(user_id="user-1", role="admin", secret=services.jwt_secret)

    response = AsgiClient(app).post(
        "/api/datasources",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    assert response.json()["error"] == "license_in_grace"


def test_crosscutting_middleware_allows_in_grace_license_update() -> None:
    services = _FakeServices()
    services.mode = LicenseMode.IN_GRACE
    app = _app_with_services(services)
    token = create_access_token(user_id="user-1", role="admin", secret=services.jwt_secret)

    response = AsgiClient(app).post(
        "/api/admin/license",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200


def test_crosscutting_middleware_allows_in_grace_read_and_backup() -> None:
    services = _FakeServices()
    services.mode = LicenseMode.IN_GRACE
    app = _app_with_services(services)
    token = create_access_token(user_id="user-1", role="admin", secret=services.jwt_secret)
    client = AsgiClient(app)

    read_response = client.get(
        "/api/protected",
        headers={"Authorization": f"Bearer {token}"},
    )
    backup_response = client.post(
        "/api/admin/backups",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert read_response.status_code == 200
    assert backup_response.status_code == 200


def test_crosscutting_middleware_blocks_business_export_download_in_restricted_modes() -> None:
    token = create_access_token(user_id="user-1", role="admin", secret=_FakeServices().jwt_secret)

    repair_services = _FakeServices()
    repair_services.mode = LicenseMode.REPAIR
    repair_response = AsgiClient(_app_with_services(repair_services)).get(
        "/api/exports/download-token",
        headers={"Authorization": f"Bearer {token}"},
    )

    grace_services = _FakeServices()
    grace_services.mode = LicenseMode.IN_GRACE
    grace_response = AsgiClient(_app_with_services(grace_services)).get(
        "/api/exports/download-token",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert repair_response.status_code == 403
    assert repair_response.json()["error"] == "license_repair_mode"
    assert grace_response.status_code == 403
    assert grace_response.json()["error"] == "license_in_grace"


def test_crosscutting_middleware_groups_job_reads_and_returns_retry_after() -> None:
    services = _FakeServices()
    services.rate_limiter = RateLimiter(
        policies={
            "auth": RateLimitRule(1),
            "sensitive_write": RateLimitRule(1),
            "sql_control": RateLimitRule(1),
            "job_read": RateLimitRule(1),
            "general_read": RateLimitRule(1),
        }
    )
    app = _app_with_services(services)
    token = create_access_token(user_id="user-1", role="admin", secret=services.jwt_secret)
    headers = {"Authorization": f"Bearer {token}"}
    client = AsgiClient(app)

    first = client.get("/api/jobs/job-1/progress", headers=headers)
    limited = client.get("/api/jobs/job-1/result", headers=headers)
    control = client.post("/api/sql/execute", headers=headers)

    assert first.status_code == 200
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) >= 1
    assert control.status_code == 200


def test_crosscutting_middleware_does_not_write_generic_audit_for_poll_reads() -> None:
    services = _FakeServices()
    app = _app_with_services(services)
    token = create_access_token(user_id="user-1", role="admin", secret=services.jwt_secret)
    headers = {"Authorization": f"Bearer {token}"}
    client = AsgiClient(app)

    assert client.get("/api/jobs/job-1/progress", headers=headers).status_code == 200
    assert client.get("/api/jobs/job-1/result", headers=headers).status_code == 200

    assert services.audits == []


def _app_with_services(services: _FakeServices) -> FastAPI:
    app = FastAPI()
    app.add_middleware(CrossCuttingMiddleware, services=cast(ApiServices, services))

    @app.get("/api/protected")
    def protected(request: Request) -> dict[str, str]:
        return {"user_id": request.state.user.id}

    @app.get("/api/fails")
    def fails() -> None:
        raise RuntimeError("handler failed")

    @app.post("/api/sql/execute")
    def execute_sql() -> dict[str, str]:
        return {"status": "accepted"}

    @app.post("/api/datasources")
    def create_datasource() -> dict[str, str]:
        return {"status": "created"}

    @app.post("/api/admin/license")
    def update_license() -> dict[str, str]:
        return {"status": "accepted"}

    @app.post("/api/admin/backups")
    def create_backup() -> dict[str, str]:
        return {"status": "accepted"}

    @app.get("/api/exports/{token}")
    def download_export(token: str) -> dict[str, str]:
        return {"token": token}

    @app.post("/api/sql/templates/{template_id}/render")
    def render_sql_template(template_id: str) -> dict[str, str]:
        return {"template_id": template_id}

    @app.get("/api/jobs/{job_id}/progress")
    def job_progress(job_id: str) -> dict[str, str]:
        return {"job_id": job_id}

    @app.get("/api/jobs/{job_id}/result")
    def job_result(job_id: str) -> dict[str, str]:
        return {"job_id": job_id}

    return app


async def _assert_sync_io_does_not_block_event_loop(
    block_point: Literal["license", "revocation", "audit_start", "audit_end"],
) -> None:
    services = _BlockingServices(block_point)
    app = _app_with_services(services)
    token = create_access_token(
        user_id="user-1",
        role="admin",
        secret=services.jwt_secret,
        jti="jti-1",
    )
    client = AsgiClient(app)
    headers = {"Authorization": f"Bearer {token}"}

    async def observe_heartbeat() -> None:
        while not services.blocking_started.is_set():
            await asyncio.sleep(0)
        services.heartbeat_observed.set()

    heartbeat = asyncio.create_task(observe_heartbeat())
    responses = await asyncio.gather(
        client.request_async("GET", "/api/protected", headers=headers),
        client.request_async("GET", "/api/protected", headers=headers),
    )
    await asyncio.wait_for(heartbeat, timeout=_BLOCKING_CALL_TIMEOUT_SECONDS)

    assert [response.status_code for response in responses] == [200, 200]


class _RateLimiter:
    def allow(self, key: str) -> bool:
        return True


class _FakeServices:
    def __init__(self) -> None:
        self.jwt_secret = "jwt-secret"
        self.rate_limiter: _RateLimiter | RateLimiter = _RateLimiter()
        self.mode = LicenseMode.TRIAL
        self.audits: list[dict[str, object]] = []
        self.token_revoked = False
        self.revocation_checks: list[dict[str, object]] = []
        self.call_order: list[str] = []

    def current_license_mode(self) -> LicenseMode:
        self.call_order.append("license")
        return self.mode

    def is_token_revoked(
        self,
        *,
        user_id: str,
        issued_at: int,
        expires_at: int,
        jti: str | None,
    ) -> bool:
        del issued_at, expires_at
        self.call_order.append("token_revocation")
        self.revocation_checks.append({"user_id": user_id, "jti": jti})
        return self.token_revoked

    def write_audit(self, **kwargs: object) -> None:
        self.call_order.append(str(kwargs["action"]))
        self.audits.append(kwargs)


class _BlockingServices(_FakeServices):
    def __init__(
        self,
        block_point: Literal["license", "revocation", "audit_start", "audit_end"],
    ) -> None:
        super().__init__()
        self._block_point = block_point
        self.blocking_started = threading.Event()
        self.heartbeat_observed = threading.Event()
        self._concurrent_calls = threading.Barrier(2)

    def current_license_mode(self) -> LicenseMode:
        self._block_sync_io("license")
        return super().current_license_mode()

    def is_token_revoked(
        self,
        *,
        user_id: str,
        issued_at: int,
        expires_at: int,
        jti: str | None,
    ) -> bool:
        self._block_sync_io("revocation")
        return super().is_token_revoked(
            user_id=user_id,
            issued_at=issued_at,
            expires_at=expires_at,
            jti=jti,
        )

    def write_audit(self, **kwargs: object) -> None:
        if kwargs.get("action") == "api_request_start":
            self._block_sync_io("audit_start")
        if kwargs.get("action") == "api_request_end":
            self._block_sync_io("audit_end")
        super().write_audit(**kwargs)

    def _block_sync_io(
        self,
        block_point: Literal["license", "revocation", "audit_start", "audit_end"],
    ) -> None:
        if self._block_point != block_point:
            return
        self.blocking_started.set()
        self._concurrent_calls.wait(timeout=_BLOCKING_CALL_TIMEOUT_SECONDS)
        if not self.heartbeat_observed.wait(timeout=_BLOCKING_CALL_TIMEOUT_SECONDS):
            raise AssertionError("event-loop heartbeat did not run during blocking synchronous I/O")
