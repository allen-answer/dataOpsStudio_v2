from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from app.api.app import create_app
from app.api.routes.core import _trial_days_remaining
from app.api.security import create_access_token
from app.api.services import ApiServices
from app.domain.job import Job, JobKind, JobStatus
from app.domain.license import LicenseMode
from app.domain.resource import ResourceProfile
from app.domain.result import ResultRef
from app.domain.schema import Column, ColumnType, Row
from tests._asgi_client import AsgiClient


def test_test_datasource_timeout_triggers_request_cancel() -> None:
    """F2-2:test_datasource 超时返回前必须 request_cancel,避免僵尸 job。"""

    backend = _AlwaysRunningJobBackend()
    services = _FakeServices(job_backend=backend)
    app = create_app(services=cast(ApiServices, services))
    token = create_access_token(user_id="user-1", role="admin", secret=services.jwt_secret)

    response = AsgiClient(app).post(
        "/api/datasources/ds-1/test",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["error_code"] == "timeout"
    assert backend.enqueued_job_id is not None
    assert backend.cancelled_job_ids == [backend.enqueued_job_id]


def test_test_datasource_success_returns_structured_probe_result() -> None:
    services = _FakeServices(
        job_backend=_TerminalJobBackend(
            JobStatus.SUCCESS,
            result_ref=ResultRef(
                backend="connection_test",
                uri="test_connection/job-1",
                metadata={"server_version": "MySQL 8.0.32", "latency_ms": 12},
            ),
        )
    )
    app = create_app(services=cast(ApiServices, services))
    token = create_access_token(user_id="user-1", role="admin", secret=services.jwt_secret)

    response = AsgiClient(app).post(
        "/api/datasources/ds-1/test",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["latency_ms"] == 12
    assert payload["server_version"] == "MySQL 8.0.32"
    assert payload["error_code"] is None


def test_test_datasource_failure_returns_safe_error_code_without_raw_driver_error() -> None:
    services = _FakeServices(
        job_backend=_TerminalJobBackend(
            JobStatus.FAILED,
            error="Access denied for user should-not-return",
        )
    )
    app = create_app(services=cast(ApiServices, services))
    token = create_access_token(user_id="user-1", role="admin", secret=services.jwt_secret)

    response = AsgiClient(app).post(
        "/api/datasources/ds-1/test",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["error_code"] == "unknown"
    assert "Access denied" not in response.body.decode("utf-8")
    assert "should-not-return" not in response.body.decode("utf-8")


def test_get_job_result_returns_columns_and_positional_rows() -> None:
    services = _ResultServices()
    app = create_app(services=cast(ApiServices, services))
    token = create_access_token(user_id="user-1", role="admin", secret=services.jwt_secret)

    response = AsgiClient(app).get(
        "/api/jobs/job-1/result",
        headers={"Authorization": f"Bearer {token}"},
        params={"offset": 0, "limit": 10},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["columns"][0]["name"] == "r"
    assert payload["rows"][0]["values"][0] == 2


def test_get_license_status_without_license_row_returns_trial_countdown() -> None:
    services = _LicenseStatusServices(row=None)
    app = create_app(services=cast(ApiServices, services))
    token = create_access_token(user_id="user-1", role="admin", secret=services.jwt_secret)

    response = AsgiClient(app).get(
        "/api/license/status",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["mode"] == "trial"
    assert response.json()["trial_days_remaining"] == 30


def test_trial_days_remaining_rounds_up_partial_days() -> None:
    expires_at = datetime.now(UTC) + timedelta(days=29, hours=23)

    assert _trial_days_remaining("trial", expires_at) == 30


class _FakeServices:
    def __init__(self, job_backend: object | None = None) -> None:
        self.jwt_secret = "jwt-secret"
        self.rate_limiter = _RateLimiter()
        self.engine = _FakeEngine()
        self.job_backend = job_backend or _AlwaysRunningJobBackend()
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
                "environment_verified": False,
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


class _TerminalJobBackend:
    def __init__(
        self,
        status: JobStatus,
        error: str | None = None,
        result_ref: ResultRef | None = None,
    ) -> None:
        self.status = status
        self.error = error
        self.result_ref = result_ref
        self.enqueued_job_id: str | None = None

    def enqueue(self, job: Job) -> None:
        self.enqueued_job_id = job.id

    def get_job(self, job_id: str) -> Job:
        return Job(
            id=job_id,
            kind=JobKind.TEST_CONNECTION,
            status=self.status,
            owner_user_id="user-1",
            project_id="project-1",
            datasource_ids=["ds-1"],
            priority=0,
            timeout_seconds=60,
            resource_profile=ResourceProfile(),
            result_ref=self.result_ref,
            audit_id="audit-1",
            error=self.error,
            payload={"datasource_id": "ds-1"},
        )


class _ResultServices:
    def __init__(self) -> None:
        self.jwt_secret = "jwt-secret"
        self.rate_limiter = _RateLimiter()
        self.engine = _ResultEngine()
        self.result_store = _ResultStore()
        self.audits: list[dict[str, object]] = []

    def current_license_mode(self) -> LicenseMode:
        return LicenseMode.TRIAL

    def write_audit(self, **kwargs: object) -> None:
        self.audits.append(kwargs)


class _ResultEngine:
    def connect(self) -> _ResultConnection:
        return _ResultConnection()


class _ResultConnection:
    def __init__(self) -> None:
        self._rows: list[dict[str, Any] | None] = [
            {
                "id": "job-1",
                "kind": "sql_query",
                "status": "success",
                "project_id": "project-1",
                "payload": {"result_set_id": "rs-1"},
            },
            {"id": "project-1"},
        ]

    def __enter__(self) -> _ResultConnection:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def execute(self, statement: object) -> _FakeResult:
        return _FakeResult(self._rows.pop(0))


class _ResultStore:
    def fetch_range(self, result_set_id: str, offset: int, limit: int) -> list[Row]:
        return [Row(values=[2])]

    def get_spool_manifest(self, result_set_id: str) -> dict[str, object]:
        return {
            "columns": [Column(name="r", type=ColumnType.UNKNOWN).model_dump()],
            "loaded_rows": 1,
            "truncated": False,
        }


class _LicenseStatusServices:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.jwt_secret = "jwt-secret"
        self.rate_limiter = _RateLimiter()
        self.engine = _SingleRowEngine(row)
        self.audits: list[dict[str, object]] = []

    def current_license_mode(self) -> LicenseMode:
        return LicenseMode.TRIAL

    def write_audit(self, **kwargs: object) -> None:
        self.audits.append(kwargs)


class _SingleRowEngine:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    def connect(self) -> _SingleRowConnection:
        return _SingleRowConnection(self._row)


class _SingleRowConnection:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    def __enter__(self) -> _SingleRowConnection:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def execute(self, statement: object) -> _FakeResult:
        return _FakeResult(self._row)
