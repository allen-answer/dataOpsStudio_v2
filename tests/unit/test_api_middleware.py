from __future__ import annotations

from typing import cast

from fastapi import FastAPI, Request

from app.api.middleware.crosscutting import CrossCuttingMiddleware
from app.api.security import create_access_token
from app.api.services import ApiServices
from app.domain.license import LicenseMode
from tests._asgi_client import AsgiClient


def test_crosscutting_middleware_requires_auth_for_api_paths() -> None:
    services = _FakeServices()
    app = _app_with_services(services)

    response = AsgiClient(app).get("/api/protected")

    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized", "message": "Authentication required"}


def test_crosscutting_middleware_authenticates_and_audits_success() -> None:
    services = _FakeServices()
    app = _app_with_services(services)
    token = create_access_token(user_id="user-1", role="admin", secret=services.jwt_secret)

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
    assert response.headers["x-request-id"]


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


def _app_with_services(services: _FakeServices) -> FastAPI:
    app = FastAPI()
    app.add_middleware(CrossCuttingMiddleware, services=cast(ApiServices, services))

    @app.get("/api/protected")
    def protected(request: Request) -> dict[str, str]:
        return {"user_id": request.state.user.id}

    @app.post("/api/sql/execute")
    def execute_sql() -> dict[str, str]:
        return {"status": "accepted"}

    return app


class _RateLimiter:
    def allow(self, key: str) -> bool:
        return True


class _FakeServices:
    def __init__(self) -> None:
        self.jwt_secret = "jwt-secret"
        self.rate_limiter = _RateLimiter()
        self.mode = LicenseMode.TRIAL
        self.audits: list[dict[str, object]] = []

    def current_license_mode(self) -> LicenseMode:
        return self.mode

    def write_audit(self, **kwargs: object) -> None:
        self.audits.append(kwargs)
