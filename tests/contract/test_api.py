from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest
from fastapi.routing import APIRoute

from app.api.app import create_app
from app.api.security import create_access_token
from app.api.services import ApiServices
from app.domain.license import LicenseMode
from tests._asgi_client import AsgiClient

pytestmark = pytest.mark.contract


def test_t4_api_route_surface_matches_contract() -> None:
    app = create_app(services=cast(ApiServices, _NoopServices()))

    routes = {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }

    assert ("POST", "/api/auth/login") in routes
    assert ("GET", "/api/projects") in routes
    assert ("GET", "/api/datasources") in routes
    assert ("POST", "/api/datasources") in routes
    assert ("GET", "/api/datasources/{datasource_id}") in routes
    assert ("POST", "/api/datasources/{datasource_id}/test") in routes
    assert ("POST", "/api/sql/execute") in routes
    assert ("GET", "/api/jobs") in routes
    assert ("GET", "/api/jobs/{job_id}") in routes
    assert ("GET", "/api/jobs/{job_id}/result") in routes
    assert ("POST", "/api/jobs/{job_id}/cancel") in routes


def test_list_datasources_filters_by_authz_and_hides_secret_fields() -> None:
    engine = _FakeEngine(
        [
            [
                {
                    "id": "ds-visible",
                    "name": "warehouse",
                    "db_type": "mysql",
                    "host": "mysql.internal",
                    "port": 3306,
                    "database_name": "app",
                    "created_at": _dt(1),
                    "username": "should-not-return",
                    "password_secret_ref": "secret-should-not-return",
                }
            ]
        ]
    )
    app = create_app(services=cast(ApiServices, _Services(engine)))

    response = AsgiClient(app).get(
        "/api/datasources",
        headers=_auth_headers(),
        params={"limit": 10, "offset": 0},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload == [
        {
            "id": "ds-visible",
            "name": "warehouse",
            "db_type": "mysql",
            "host": "mysql.internal",
            "port": 3306,
            "database": "app",
            "created_at": "2026-01-01T00:00:01Z",
        }
    ]
    assert "password" not in response.body.decode("utf-8")
    assert "secret" not in response.body.decode("utf-8")
    assert "username" not in response.body.decode("utf-8")
    statement = engine.statements[-1]
    assert "project_members" in statement
    assert "owner_user_id" in statement
    assert "password_secret_ref" not in statement


def test_list_jobs_filters_paginates_and_hides_payload_and_raw_errors() -> None:
    engine = _FakeEngine(
        [
            {"id": "project-1"},
            [
                {
                    "id": "job-conn",
                    "kind": "test_connection",
                    "status": "failed",
                    "created_at": _dt(3),
                    "started_at": _dt(4),
                    "finished_at": _dt(5),
                    "error": "Access denied for user should-not-return",
                    "payload": {"sql": "SELECT secret_should_not_return"},
                },
                {
                    "id": "job-sql",
                    "kind": "sql_query",
                    "status": "failed",
                    "created_at": _dt(2),
                    "started_at": None,
                    "finished_at": _dt(6),
                    "error": "Unknown column secret_schema.should_not_return",
                    "payload": {"sql": "SELECT secret_should_not_return"},
                },
                {
                    "id": "job-timeout",
                    "kind": "sql_query",
                    "status": "failed",
                    "created_at": _dt(1),
                    "started_at": None,
                    "finished_at": _dt(7),
                    "error": "worker heartbeat timed out",
                },
            ],
        ]
    )
    app = create_app(services=cast(ApiServices, _Services(engine)))

    response = AsgiClient(app).get(
        "/api/jobs",
        headers=_auth_headers(),
        params={"project_id": "project-1", "status": "failed", "limit": 3, "offset": 0},
    )

    assert response.status_code == 200
    payload = response.json()
    assert [job["id"] for job in payload] == ["job-conn", "job-sql", "job-timeout"]
    assert [job["error"] for job in payload] == [
        "datasource_connection_failed",
        "sql_execution_failed",
        "query_timeout",
    ]
    body = response.body.decode("utf-8")
    assert "payload" not in body
    assert "SELECT secret_should_not_return" not in body
    assert "Access denied" not in body
    assert "Unknown column" not in body
    statement = engine.statements[-1]
    assert "project_members" in statement
    assert "jobs.owner_user_id" in statement
    assert "jobs.project_id" in statement
    assert "jobs.status" in statement
    assert "LIMIT" in statement
    assert "OFFSET" in statement


def test_list_jobs_project_filter_requires_authz() -> None:
    engine = _FakeEngine([None])
    app = create_app(services=cast(ApiServices, _Services(engine)))

    response = AsgiClient(app).get(
        "/api/jobs",
        headers=_auth_headers(),
        params={"project_id": "other-project"},
    )

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def _auth_headers(user_id: str = "user-1") -> dict[str, str]:
    token = create_access_token(user_id=user_id, role="admin", secret=_Services.jwt_secret)
    return {"Authorization": f"Bearer {token}"}


def _dt(second: int) -> datetime:
    return datetime(2026, 1, 1, 0, 0, second, tzinfo=UTC)


class _NoopServices:
    pass


class _Services:
    jwt_secret = "jwt-secret"

    def __init__(self, engine: _FakeEngine) -> None:
        self.engine = engine
        self.rate_limiter = _RateLimiter()
        self.audits: list[dict[str, object]] = []

    def current_license_mode(self) -> LicenseMode:
        return LicenseMode.TRIAL

    def write_audit(self, **kwargs: object) -> None:
        self.audits.append(kwargs)


class _RateLimiter:
    def allow(self, key: str) -> bool:
        return True


class _FakeEngine:
    def __init__(self, results: list[object]) -> None:
        self.results = list(results)
        self.statements: list[str] = []

    def connect(self) -> _FakeConnection:
        return _FakeConnection(self)


class _FakeConnection:
    def __init__(self, engine: _FakeEngine) -> None:
        self._engine = engine

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def execute(self, statement: object) -> _FakeResult:
        self._engine.statements.append(str(statement))
        return _FakeResult(self._engine.results.pop(0))


class _FakeResult:
    def __init__(self, result: object) -> None:
        self._result = result

    def mappings(self) -> _FakeResult:
        return self

    def all(self) -> list[dict[str, Any]]:
        if isinstance(self._result, list):
            return [dict(row) for row in self._result if isinstance(row, dict)]
        raise AssertionError(f"expected list result, got {type(self._result).__name__}")

    def one_or_none(self) -> dict[str, Any] | None:
        if self._result is None:
            return None
        if isinstance(self._result, dict):
            return dict(self._result)
        raise AssertionError(f"expected single row result, got {type(self._result).__name__}")
