from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from app.api.app import create_app
from app.api.security import create_access_token
from app.api.services import ApiServices
from app.domain.license import LicenseMode
from tests._asgi_client import AsgiClient


def test_admin_routes_require_admin_role() -> None:
    services = _AdminServices(_QueueEngine([]))
    app = create_app(services=cast(ApiServices, services))
    token = create_access_token(user_id="user-1", role="viewer", secret=services.jwt_secret)

    response = AsgiClient(app).get(
        "/api/admin/users",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    assert response.json()["error"] == "forbidden"


def test_admin_list_users_projects_mfa_enabled() -> None:
    services = _AdminServices(
        _QueueEngine(
            [
                [
                    {
                        "id": "user-1",
                        "username": "admin",
                        "role": "admin",
                        "mfa_secret_ref": "secret-1",
                        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
                    }
                ]
            ]
        )
    )
    app = create_app(services=cast(ApiServices, services))
    token = create_access_token(user_id="user-1", role="admin", secret=services.jwt_secret)

    response = AsgiClient(app).get(
        "/api/admin/users",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload[0]["username"] == "admin"
    assert payload[0]["mfa_enabled"] is True


def test_admin_delete_user_returns_owned_projects_409() -> None:
    services = _AdminServices(
        _QueueEngine(
            [
                [
                    {
                        "id": "project-1",
                        "name": "Default",
                        "description": None,
                    }
                ]
            ]
        )
    )
    app = create_app(services=cast(ApiServices, services))
    token = create_access_token(user_id="admin-1", role="admin", secret=services.jwt_secret)

    response = AsgiClient(app).request(
        "DELETE",
        "/api/admin/users/user-1",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409
    payload = response.json()
    assert payload["error"] == "user_owns_projects"
    assert payload["owned_projects"][0]["id"] == "project-1"


class _AdminServices:
    def __init__(self, engine: object) -> None:
        self.jwt_secret = "jwt-secret"
        self.rate_limiter = _RateLimiter()
        self.engine = engine
        self.audits: list[dict[str, object]] = []

    def current_license_mode(self) -> LicenseMode:
        return LicenseMode.TRIAL

    def write_audit(self, **kwargs: object) -> None:
        self.audits.append(kwargs)


class _RateLimiter:
    def allow(self, key: str) -> bool:
        return True


class _QueueEngine:
    def __init__(self, rows: list[list[dict[str, Any]]]) -> None:
        self.rows = rows

    def connect(self) -> _QueueConnection:
        return _QueueConnection(self.rows)

    def begin(self) -> _QueueConnection:
        return _QueueConnection(self.rows)


class _QueueConnection:
    def __init__(self, rows: list[list[dict[str, Any]]]) -> None:
        self.rows = rows

    def __enter__(self) -> _QueueConnection:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def execute(self, statement: object, params: object | None = None) -> _QueueResult:
        del statement, params
        rows = self.rows.pop(0) if self.rows else []
        return _QueueResult(rows)


class _QueueResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def mappings(self) -> _QueueResult:
        return self

    def all(self) -> list[dict[str, Any]]:
        return self.rows

    def one_or_none(self) -> dict[str, Any] | None:
        return self.rows[0] if self.rows else None

    def one(self) -> dict[str, Any]:
        return self.rows[0]
