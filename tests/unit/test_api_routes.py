from __future__ import annotations

from typing import Any, cast

from app.api.app import create_app
from app.api.security import create_access_token
from app.api.services import ApiServices
from app.domain.job import Job, JobKind, JobStatus
from app.domain.license import LicenseMode
from app.domain.resource import ResourceProfile
from tests._asgi_client import AsgiClient


def test_test_datasource_timeout_triggers_request_cancel() -> None:
    """F2-2:test_datasource 超时返回前必须 request_cancel,避免僵尸 job。"""

    services = _FakeServices()
    app = create_app(services=cast(ApiServices, services))
    token = create_access_token(user_id="user-1", role="admin", secret=services.jwt_secret)

    response = AsgiClient(app).post(
        "/api/datasources/ds-1/test",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 504
    assert response.json()["error"] == "job_timeout"
    assert services.job_backend.enqueued_job_id is not None
    assert services.job_backend.cancelled_job_ids == [services.job_backend.enqueued_job_id]


class _FakeServices:
    def __init__(self) -> None:
        self.jwt_secret = "jwt-secret"
        self.rate_limiter = _RateLimiter()
        self.engine = _FakeEngine()
        self.job_backend = _AlwaysRunningJobBackend()
        self.job_wait_timeout_seconds = 0.01
        self.audits: list[dict[str, object]] = []

    def current_license_mode(self) -> LicenseMode:
        return LicenseMode.TRIAL

    def write_audit(self, **kwargs: object) -> None:
        self.audits.append(kwargs)


class _RateLimiter:
    def allow(self, key: str) -> bool:
        return True


class _FakeEngine:
    def connect(self) -> _FakeConnection:
        return _FakeConnection()


class _FakeConnection:
    def __init__(self) -> None:
        self._rows: list[dict[str, Any] | None] = [
            {
                "id": "ds-1",
                "project_id": "project-1",
                "db_type": "mysql",
                "host": "127.0.0.1",
                "port": 3306,
                "username": "dataops",
                "database_name": "app",
                "environment": "test",
                "capability_profile": {},
            },
            {"id": "project-1"},
        ]

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def execute(self, statement: object) -> _FakeResult:
        return _FakeResult(self._rows.pop(0))


class _FakeResult:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    def mappings(self) -> _FakeResult:
        return self

    def one_or_none(self) -> dict[str, Any] | None:
        return self._row


class _AlwaysRunningJobBackend:
    def __init__(self) -> None:
        self.enqueued_job_id: str | None = None
        self.cancelled_job_ids: list[str] = []

    def enqueue(self, job: Job) -> None:
        self.enqueued_job_id = job.id

    def get_job(self, job_id: str) -> Job:
        return Job(
            id=job_id,
            kind=JobKind.TEST_CONNECTION,
            status=JobStatus.RUNNING,
            owner_user_id="user-1",
            project_id="project-1",
            datasource_ids=["ds-1"],
            priority=0,
            timeout_seconds=60,
            resource_profile=ResourceProfile(),
            audit_id="audit-1",
            payload={"datasource_id": "ds-1"},
        )

    def request_cancel(self, job_id: str) -> None:
        self.cancelled_job_ids.append(job_id)
