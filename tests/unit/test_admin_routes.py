from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest

from app.api.app import create_app
from app.api.security import create_access_token
from app.api.services import ApiServices
from app.domain.ai import AiContext, AiOptions, AiResponse
from app.domain.license import LicenseMode
from app.domain.secret import SecretKind, SecretRef
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


def test_admin_force_logout_sets_user_cutoff_and_audits() -> None:
    revoked_after = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    services = _AdminServices(
        _QueueEngine(
            [
                [
                    {
                        "id": "user-1",
                        "username": "target",
                        "role": "viewer",
                        "mfa_secret_ref": None,
                        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
                    }
                ]
            ]
        ),
        revoked_after=revoked_after,
    )
    app = create_app(services=cast(ApiServices, services))
    token = create_access_token(user_id="admin-1", role="admin", secret=services.jwt_secret)

    response = AsgiClient(app).post(
        "/api/admin/users/user-1/force-logout",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "user_id": "user-1",
        "revoked_after": "2026-01-01T12:00:00Z",
    }
    assert services.revoked_users == ["user-1"]
    assert any(item["action"] == "admin_user_force_logout" for item in services.audits)


def test_admin_put_ai_config_stores_key_without_echoing_secret() -> None:
    api_key = "not-a-real-test-key"
    row = _ai_config_row(api_key_secret_ref="secret-new")
    services = _AdminServices(_QueueEngine([[], [], [row]]))
    app = create_app(services=cast(ApiServices, services))
    token = create_access_token(user_id="admin-1", role="admin", secret=services.jwt_secret)

    response = AsgiClient(app).request(
        "PUT",
        "/api/admin/ai-config",
        json_body={
            "enabled": True,
            "provider": "mock",
            "model": "mock-model",
            "base_url": None,
            "api_key": api_key,
            "max_auto_egress_level": 2,
            "l4_requires_optin": True,
            "enable_inference": True,
            "enable_auto_translation": False,
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["api_key_source"] == "stored"
    assert payload["has_stored_api_key"] is True
    assert "api_key" not in payload
    assert services.secret_store.stored == [api_key]
    assert api_key not in repr(services.audits)


def test_admin_put_ai_config_rejects_disabling_l4_optin() -> None:
    api_key = "not-a-real-test-key"
    services = _AdminServices(_QueueEngine([]))
    app = create_app(services=cast(ApiServices, services))
    token = create_access_token(user_id="admin-1", role="admin", secret=services.jwt_secret)

    response = AsgiClient(app).request(
        "PUT",
        "/api/admin/ai-config",
        json_body={
            "enabled": True,
            "provider": "mock",
            "model": "mock-model",
            "base_url": None,
            "api_key": api_key,
            "max_auto_egress_level": 2,
            "l4_requires_optin": False,
            "enable_inference": True,
            "enable_auto_translation": False,
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_ai_config"
    assert services.secret_store.stored == []


def test_admin_ai_config_test_uses_reasoning_safe_budget_and_audits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_max_tokens: list[int | None] = []

    class _Gateway:
        def complete(
            self,
            prompt: str,
            context: AiContext,
            options: AiOptions,
        ) -> AiResponse:
            del prompt, context
            captured_max_tokens.append(options.max_tokens)
            return AiResponse(
                content="pong",
                tokens_in=1,
                tokens_out=1,
                provider="mock",
                model="mock-model",
            )

    monkeypatch.setattr(
        "app.api.routes.admin.build_gateway_from_runtime_config",
        lambda runtime: _Gateway(),
    )
    services = _AdminServices(_QueueEngine([[_ai_config_row(provider="mock")]]))
    app = create_app(services=cast(ApiServices, services))
    token = create_access_token(user_id="admin-1", role="admin", secret=services.jwt_secret)

    response = AsgiClient(app).post(
        "/api/admin/ai-config/test",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["provider"] == "mock"
    assert payload["error"] is None
    assert captured_max_tokens == [256]
    assert any(item["action"] == "admin_ai_config_test" for item in services.audits)


class _AdminServices:
    def __init__(
        self,
        engine: object,
        *,
        revoked_after: datetime | None = None,
    ) -> None:
        self.jwt_secret = "jwt-secret"
        self.rate_limiter = _RateLimiter()
        self.engine = engine
        self.secret_store = _SecretStore()
        self.audits: list[dict[str, object]] = []
        self.revoked_after = revoked_after or datetime(2026, 1, 1, tzinfo=UTC)
        self.revoked_users: list[str] = []

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

    def revoke_user_tokens(self, *, user_id: str) -> datetime:
        self.revoked_users.append(user_id)
        return self.revoked_after

    def write_audit(self, **kwargs: object) -> None:
        self.audits.append(kwargs)


class _RateLimiter:
    def allow(self, key: str) -> bool:
        return True


class _SecretStore:
    def __init__(self) -> None:
        self.stored: list[str] = []
        self.rotated: list[tuple[str, str]] = []
        self.deleted: list[str] = []

    def store_secret(self, plaintext: str, kind: SecretKind) -> SecretRef:
        assert kind is SecretKind.AI_API_KEY
        self.stored.append(plaintext)
        return SecretRef(ref="secret-new", kind=kind)

    def rotate_secret(self, ref: SecretRef, new_plaintext: str) -> SecretRef:
        self.rotated.append((ref.ref, new_plaintext))
        return ref

    def delete_secret(self, ref: SecretRef) -> None:
        self.deleted.append(ref.ref)

    def reveal_secret(self, ref: SecretRef) -> str:
        return f"revealed-{ref.ref}"


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


def _ai_config_row(
    *,
    provider: str = "mock",
    api_key_secret_ref: str | None = None,
) -> dict[str, object]:
    return {
        "id": 1,
        "enabled": True,
        "provider": provider,
        "model": "mock-model",
        "base_url": None,
        "api_key_secret_ref": api_key_secret_ref,
        "max_auto_egress_level": 2,
        "l4_requires_optin": True,
        "enable_inference": True,
        "enable_auto_translation": False,
        "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
