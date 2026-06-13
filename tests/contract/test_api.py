from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from fastapi.routing import APIRoute

from app.api.app import create_app
from app.api.routes import core as core_routes
from app.api.security import create_access_token
from app.api.services import ApiServices
from app.dbclients.protocol import AdapterConnectionError
from app.domain.job import Job, JobKind
from app.domain.license import LicenseMode
from app.domain.result import ResultRef
from app.domain.schema import Column, ColumnType, Schema, Table
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
    assert ("PUT", "/api/datasources/{datasource_id}") in routes
    assert ("DELETE", "/api/datasources/{datasource_id}") in routes
    assert ("POST", "/api/datasources/{datasource_id}/test") in routes
    assert ("POST", "/api/sql/execute") in routes
    assert ("GET", "/api/sql/consoles") in routes
    assert ("POST", "/api/sql/consoles") in routes
    assert ("PATCH", "/api/sql/consoles/{console_id}") in routes
    assert ("DELETE", "/api/sql/consoles/{console_id}") in routes
    assert ("GET", "/api/sql/history") in routes
    assert ("GET", "/api/sql/templates") in routes
    assert ("POST", "/api/sql/templates") in routes
    assert ("PATCH", "/api/sql/templates/{template_id}") in routes
    assert ("DELETE", "/api/sql/templates/{template_id}") in routes
    assert ("POST", "/api/sql/templates/{template_id}/render") in routes
    assert ("POST", "/api/sql/explain") in routes
    assert ("POST", "/api/sql/format") in routes
    assert ("POST", "/api/sql/expand-star") in routes
    assert ("GET", "/api/datasources/{datasource_id}/metadata/schemas") in routes
    assert ("GET", "/api/datasources/{datasource_id}/metadata/tables") in routes
    assert ("GET", "/api/datasources/{datasource_id}/metadata/columns") in routes
    assert ("GET", "/api/jobs") in routes
    assert ("GET", "/api/jobs/{job_id}") in routes
    assert ("GET", "/api/jobs/{job_id}/result") in routes
    assert ("POST", "/api/jobs/{job_id}/export") in routes
    assert ("GET", "/api/exports/{token}") in routes
    assert ("POST", "/api/jobs/{job_id}/cancel") in routes
    assert ("GET", "/api/account/security") in routes
    assert ("POST", "/api/account/password") in routes
    assert ("POST", "/api/account/mfa/enroll") in routes
    assert ("POST", "/api/account/mfa/verify") in routes
    assert ("POST", "/api/account/mfa/disable") in routes
    assert ("POST", "/api/account/recovery-codes/regenerate") in routes
    assert ("GET", "/api/admin/ai-config") in routes
    assert ("PUT", "/api/admin/ai-config") in routes
    assert ("POST", "/api/admin/ai-config/test") in routes


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
                    "environment": "prod",
                    "environment_verified": True,
                    "database_name": "app",
                    "created_at": _dt(1),
                    "username": "should-not-return",
                    "password_secret_ref": "secret-should-not-return",
                    "operation_policy": _policy(),
                },
                {
                    "id": "ds-no-database",
                    "name": "warehouse-no-db",
                    "db_type": "mysql",
                    "host": "mysql2.internal",
                    "port": 3307,
                    "environment": "sandbox",
                    "environment_verified": False,
                    "database_name": None,
                    "created_at": _dt(2),
                    "username": "should-not-return",
                    "password_secret_ref": "secret-should-not-return",
                    "operation_policy": _policy(allow_select=False),
                },
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
            "environment": "prod",
            "environment_verified": True,
            "database": "app",
            "operation_policy": _policy(),
            "created_at": "2026-01-01T00:00:01Z",
        },
        {
            "id": "ds-no-database",
            "name": "warehouse-no-db",
            "db_type": "mysql",
            "host": "mysql2.internal",
            "port": 3307,
            "environment": "sandbox",
            "environment_verified": False,
            "database": None,
            "operation_policy": _policy(allow_select=False),
            "created_at": "2026-01-01T00:00:02Z",
        },
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
                    "error_code": "connection_failed",
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
                    "error_code": "sql_failed",
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
                    "error_code": "timeout",
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
        "connection_failed",
        "sql_failed",
        "timeout",
    ]
    assert [job["error_code"] for job in payload] == [
        "connection_failed",
        "sql_failed",
        "timeout",
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


def test_list_jobs_classifies_cancelled_without_leaking_reason() -> None:
    engine = _FakeEngine(
        [
            [
                {
                    "id": "job-cancelled",
                    "kind": "sql_query",
                    "status": "cancelled",
                    "created_at": _dt(0),
                    "started_at": None,
                    "finished_at": _dt(8),
                    "error": "user requested cancel",
                    "error_code": None,
                    "payload": {"sql": "SELECT cancelled_should_not_return"},
                }
            ],
        ]
    )
    app = create_app(services=cast(ApiServices, _Services(engine)))

    response = AsgiClient(app).get(
        "/api/jobs",
        headers=_auth_headers(),
        params={"status": "cancelled"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert [job["id"] for job in payload] == ["job-cancelled"]
    assert payload[0]["error"] == "cancelled"
    assert payload[0]["error_code"] is None
    body = response.body.decode("utf-8")
    assert "user requested cancel" not in body
    assert "SELECT cancelled_should_not_return" not in body


def test_get_job_response_includes_timestamps_for_terminal_jobs() -> None:
    engine = _FakeEngine(
        [
            {
                "id": "job-success",
                "project_id": "project-1",
                "kind": "sql_query",
                "status": "success",
                "created_at": _dt(1),
                "started_at": _dt(2),
                "finished_at": _dt(9),
                "error": None,
                "error_code": None,
                "payload": {"result_set_id": "rs-1", "sql": "SELECT should_not_return"},
            },
            {"id": "project-1"},
        ]
    )
    app = create_app(services=cast(ApiServices, _Services(engine)))

    response = AsgiClient(app).get(
        "/api/jobs/job-success",
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["created_at"] == "2026-01-01T00:00:01Z"
    assert payload["finished_at"] == "2026-01-01T00:00:09Z"
    assert "SELECT should_not_return" not in response.body.decode("utf-8")


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


def test_update_datasource_keeps_password_when_blank_and_returns_policy() -> None:
    engine = _FakeEngine(
        [
            _datasource_row(password_secret_ref="secret-old"),
            {"id": "project-1"},
            _datasource_row(
                name="warehouse-renamed",
                password_secret_ref="secret-old",
                operation_policy=_policy(allow_select=False),
            ),
            {"id": "project-1"},
        ]
    )
    secret_store = _SecretStore()
    services = _Services(engine, secret_store=secret_store)
    app = create_app(services=cast(ApiServices, services))

    response = AsgiClient(app).request(
        "PUT",
        "/api/datasources/ds-1",
        headers=_auth_headers(),
        json_body={
            "name": "warehouse-renamed",
            "password": "",
            "operation_policy": _policy(allow_select=False),
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["name"] == "warehouse-renamed"
    assert payload["operation_policy"]["allow_select"] is False
    assert secret_store.stored == []
    assert secret_store.deleted == []
    assert "password" not in response.body.decode("utf-8")
    assert "secret-old" not in response.body.decode("utf-8")
    assert any("UPDATE datasources" in statement for statement in engine.statements)
    assert any(audit["action"] == "datasource_update" for audit in services.audits)


def test_update_datasource_rotates_password_when_new_value_present() -> None:
    engine = _FakeEngine(
        [
            _datasource_row(password_secret_ref="secret-old"),
            {"id": "project-1"},
            _datasource_row(password_secret_ref="secret-new"),
            {"id": "project-1"},
        ]
    )
    secret_store = _SecretStore()
    services = _Services(engine, secret_store=secret_store)
    app = create_app(services=cast(ApiServices, services))

    response = AsgiClient(app).request(
        "PUT",
        "/api/datasources/ds-1",
        headers=_auth_headers(),
        json_body={"password": "new-local-test-password"},
    )

    assert response.status_code == 200
    assert secret_store.stored == ["new-local-test-password"]
    assert secret_store.deleted == ["secret-old"]
    assert "new-local-test-password" not in response.body.decode("utf-8")
    assert "secret-new" not in response.body.decode("utf-8")


def test_delete_datasource_returns_409_with_job_references() -> None:
    engine = _FakeEngine(
        [
            _datasource_row(password_secret_ref="secret-old"),
            {"id": "project-1"},
            [
                {
                    "id": "job-1",
                    "kind": "sql_query",
                    "status": "running",
                }
            ],
        ]
    )
    secret_store = _SecretStore()
    services = _Services(engine, secret_store=secret_store)
    app = create_app(services=cast(ApiServices, services))

    response = AsgiClient(app).request(
        "DELETE",
        "/api/datasources/ds-1",
        headers=_auth_headers(),
    )

    assert response.status_code == 409
    assert response.json() == {
        "error": "datasource_in_use",
        "message": "Datasource is referenced by existing jobs",
        "references": [{"job_id": "job-1", "kind": "sql_query", "status": "running"}],
    }
    assert secret_store.deleted == []
    assert not any("DELETE FROM datasources" in statement for statement in engine.statements)


def test_metadata_endpoints_return_cached_contract_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _MetadataAdapter()
    adapter_kwargs: list[dict[str, object]] = []

    def build_adapter(
        conn_info: object,
        secret_store: object,
        **kwargs: object,
    ) -> _MetadataAdapter:
        del conn_info, secret_store
        adapter_kwargs.append(kwargs)
        return adapter

    monkeypatch.setattr(
        core_routes,
        "build_database_adapter",
        build_adapter,
    )
    engine = _FakeEngine(
        [
            _datasource_row(),
            {"id": "project-1"},
            _datasource_row(),
            {"id": "project-1"},
            _datasource_row(),
            {"id": "project-1"},
        ]
    )
    services = _Services(engine, metadata_probe_timeout_seconds=6)
    app = create_app(services=cast(ApiServices, services))
    client = AsgiClient(app)

    schemas_response = client.get(
        "/api/datasources/ds-1/metadata/schemas",
        headers=_auth_headers(),
        params={"refresh": "1"},
    )
    tables_response = client.get(
        "/api/datasources/ds-1/metadata/tables",
        headers=_auth_headers(),
        params={"schema": "app", "refresh": "1"},
    )
    columns_response = client.get(
        "/api/datasources/ds-1/metadata/columns",
        headers=_auth_headers(),
        params={"schema": "app", "table": "users", "refresh": "1"},
    )

    assert schemas_response.status_code == 200
    assert schemas_response.json() == [{"name": "app"}]
    assert tables_response.status_code == 200
    assert tables_response.json() == [
        {"schema_name": "app", "name": "users", "table_type": "BASE TABLE"}
    ]
    assert columns_response.status_code == 200
    assert columns_response.json() == [
        {
            "name": "id",
            "type": "integer",
            "driver_type": "INT",
            "nullable": False,
            "primary_key": True,
            "comment": "primary key",
        },
        {
            "name": "name",
            "type": "string",
            "driver_type": "VARCHAR(64)",
            "nullable": True,
            "primary_key": False,
            "comment": None,
        },
    ]
    assert {audit["action"] for audit in services.audits} >= {
        "metadata_schemas_list",
        "metadata_tables_list",
        "metadata_columns_list",
    }
    assert adapter_kwargs == [
        {"connect_timeout_seconds": 6, "statement_timeout_seconds": 6},
        {"connect_timeout_seconds": 6, "statement_timeout_seconds": 6},
        {"connect_timeout_seconds": 6, "statement_timeout_seconds": 6},
    ]


def test_metadata_probe_connection_failure_is_structured_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        core_routes,
        "build_database_adapter",
        lambda conn_info, secret_store, **kwargs: _FailingMetadataAdapter(),
    )
    engine = _FakeEngine([_datasource_row(), {"id": "project-1"}])
    app = create_app(services=cast(ApiServices, _Services(engine)))

    response = AsgiClient(app).get(
        "/api/datasources/ds-1/metadata/schemas",
        headers=_auth_headers(),
        params={"refresh": "1"},
    )

    assert response.status_code == 503
    assert response.json()["error"] == "metadata_probe_failed"


def test_sql_format_endpoint_returns_formatted_sql() -> None:
    app = create_app(services=cast(ApiServices, _Services(_FakeEngine([]))))

    response = AsgiClient(app).post(
        "/api/sql/format",
        headers=_auth_headers(),
        json_body={"sql": "select 1 as n", "dialect": "mysql"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"formatted_sql"}
    assert "SELECT" in payload["formatted_sql"]
    assert "select 1 as n" not in payload["formatted_sql"]


def test_sql_expand_star_uses_metadata_cache_contract_fields() -> None:
    engine = _FakeEngine(
        [
            _datasource_row(),
            {"id": "project-1"},
            {
                "payload": [
                    {
                        "name": "id",
                        "type": "integer",
                        "driver_type": "INT",
                        "nullable": False,
                        "primary_key": True,
                        "comment": "primary key",
                    },
                    {
                        "name": "name",
                        "type": "string",
                        "driver_type": "VARCHAR(64)",
                        "nullable": True,
                        "primary_key": False,
                        "comment": None,
                    },
                ],
                "expires_at": _dt(30),
            },
        ]
    )
    app = create_app(services=cast(ApiServices, _Services(engine)))

    response = AsgiClient(app).post(
        "/api/sql/expand-star",
        headers=_auth_headers(),
        json_body={"sql": "SELECT * FROM users", "datasource_id": "ds-1"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"expanded_sql"}
    assert "id" in payload["expanded_sql"]
    assert "name" in payload["expanded_sql"]
    assert "*" not in payload["expanded_sql"]


def test_sql_expand_star_cache_miss_is_structured_error() -> None:
    engine = _FakeEngine([_datasource_row(), {"id": "project-1"}, None])
    app = create_app(services=cast(ApiServices, _Services(engine)))

    response = AsgiClient(app).post(
        "/api/sql/expand-star",
        headers=_auth_headers(),
        json_body={"sql": "SELECT * FROM users", "datasource_id": "ds-1"},
    )

    assert response.status_code == 409
    assert response.json()["error"] == "metadata_cache_missing"


def test_sql_explain_enqueues_explain_job_with_result_set() -> None:
    engine = _FakeEngine(
        [
            _datasource_row(operation_policy=_policy(allow_explain=True)),
            {"id": "project-1"},
        ]
    )
    services = _Services(engine)
    app = create_app(services=cast(ApiServices, services))

    response = AsgiClient(app).post(
        "/api/sql/explain",
        headers=_auth_headers(),
        json_body={"sql": "SELECT * FROM users", "datasource_id": "ds-1"},
    )

    assert response.status_code == 202
    payload = response.json()
    assert set(payload) == {"job_id", "result_set_id"}
    job = services.job_backend.enqueued[0]
    assert job.kind is JobKind.SQL_EXPLAIN
    assert job.payload["result_set_id"] == payload["result_set_id"]
    assert job.payload["datasource_id"] == "ds-1"
    assert "params" not in job.payload
    assert any(audit["action"] == "sql_explain" for audit in services.audits)


def test_sql_explain_rejects_params_instead_of_ignoring_them() -> None:
    engine = _FakeEngine(
        [
            _datasource_row(operation_policy=_policy(allow_explain=True)),
            {"id": "project-1"},
        ]
    )
    services = _Services(engine)
    app = create_app(services=cast(ApiServices, services))

    response = AsgiClient(app).post(
        "/api/sql/explain",
        headers=_auth_headers(),
        json_body={
            "sql": "SELECT * FROM users",
            "datasource_id": "ds-1",
            "params": {"id": 1},
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == "unsupported_sql_params"
    assert services.job_backend.enqueued == []


def test_create_export_enqueues_result_export_job_and_returns_one_time_token() -> None:
    engine = _FakeEngine(
        [
            _source_job_row(),
            {"id": "project-1"},
            0,
            {"db_type": "mysql"},
        ]
    )
    job_backend = _JobBackend()
    services = _Services(engine, job_backend=job_backend)
    app = create_app(services=cast(ApiServices, services))

    response = AsgiClient(app).post(
        "/api/jobs/job-source/export",
        headers=_auth_headers(),
        json_body={"format": "csv"},
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["job_id"]
    assert payload["download_token"]
    assert payload["format"] == "csv"
    assert payload["filename"] == "job-source.csv"
    job = job_backend.enqueued[0]
    assert job.kind is JobKind.RESULT_EXPORT
    assert job.payload["source_job_id"] == "job-source"
    assert job.payload["source_result_set_id"] == "rs-source"
    assert job.payload["format"] == "csv"
    assert job.payload["source_db_type"] == "mysql"
    assert services.audits[-1]["action"] == "sql_export"
    assert "download_token" not in str(services.audits[-1])


def test_create_export_rate_limit_returns_429_without_enqueue() -> None:
    engine = _FakeEngine(
        [
            _source_job_row(),
            {"id": "project-1"},
            10,
        ]
    )
    job_backend = _JobBackend()
    services = _Services(engine, job_backend=job_backend)
    app = create_app(services=cast(ApiServices, services))

    response = AsgiClient(app).post(
        "/api/jobs/job-source/export",
        headers=_auth_headers(),
        json_body={"format": "json"},
    )

    assert response.status_code == 429
    assert response.json()["error"] == "export_rate_limited"
    assert job_backend.enqueued == []


def test_create_export_requires_source_spool_to_still_exist() -> None:
    engine = _FakeEngine([_source_job_row(), {"id": "project-1"}])
    job_backend = _JobBackend()
    result_store = _ResultStore(spool_exists=False)
    services = _Services(engine, job_backend=job_backend, result_store=result_store)
    app = create_app(services=cast(ApiServices, services))

    response = AsgiClient(app).post(
        "/api/jobs/job-source/export",
        headers=_auth_headers(),
        json_body={"format": "csv"},
    )

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"
    assert job_backend.enqueued == []


def test_export_download_token_is_one_time() -> None:
    token_row = {
        "token_hash": "not-read-by-fake-engine",
        "owner_user_id": "user-1",
        "filename": "job-source.csv",
        "content_type": "text/csv; charset=utf-8",
        "expires_at": datetime.now(UTC) + timedelta(minutes=5),
        "consumed_at": None,
        "job_id": "job-export",
        "status": "success",
        "result_ref": {
            "backend": "local_fs",
            "uri": "exports/job-export/job-source.csv",
            "metadata": {},
        },
    }
    engine = _FakeEngine([token_row, token_row | {"consumed_at": _dt(1)}])
    result_store = _ResultStore(downloads={"exports/job-export/job-source.csv": b"value\n1\n"})
    app = create_app(services=cast(ApiServices, _Services(engine, result_store=result_store)))

    first = AsgiClient(app).get("/api/exports/download-token", headers=_auth_headers())
    second = AsgiClient(app).get("/api/exports/download-token", headers=_auth_headers())

    assert first.status_code == 200
    assert first.body == b"value\n1\n"
    assert first.headers["content-disposition"] == 'attachment; filename="job-source.csv"'
    assert second.status_code == 410
    assert second.json()["error"] == "download_token_consumed"


def _auth_headers(user_id: str = "user-1") -> dict[str, str]:
    token = create_access_token(user_id=user_id, role="admin", secret=_Services.jwt_secret)
    return {"Authorization": f"Bearer {token}"}


def _dt(second: int) -> datetime:
    return datetime(2026, 1, 1, 0, 0, second, tzinfo=UTC)


def _policy(**overrides: bool) -> dict[str, bool]:
    values = {
        "allow_select": True,
        "allow_explain": False,
        "allow_dm_explain": False,
        "allow_oracle_plan_table": False,
        "allow_schema_import": False,
        "allow_schema_save": False,
        "allow_scenario_write": False,
        "allow_record_task": False,
    }
    values.update(overrides)
    return values


def _datasource_row(
    *,
    name: str = "warehouse",
    password_secret_ref: str = "secret-old",
    operation_policy: dict[str, bool] | None = None,
) -> dict[str, object]:
    return {
        "id": "ds-1",
        "project_id": "project-1",
        "name": name,
        "db_type": "mysql",
        "host": "mysql.internal",
        "port": 3306,
        "username": "dataops",
        "database_name": "app",
        "password_secret_ref": password_secret_ref,
        "environment": "sandbox",
        "environment_verified": False,
        "capability_profile": {},
        "operation_policy": operation_policy or _policy(),
        "created_at": _dt(1),
    }


def _source_job_row() -> dict[str, object]:
    return {
        "id": "job-source",
        "kind": "sql_query",
        "status": "success",
        "owner_user_id": "user-1",
        "project_id": "project-1",
        "datasource_ids": ["ds-1"],
        "created_at": _dt(1),
        "started_at": _dt(2),
        "finished_at": _dt(3),
        "error": None,
        "error_code": None,
        "payload": {"result_set_id": "rs-source"},
    }


class _NoopServices:
    pass


class _Services:
    jwt_secret = "jwt-secret"

    def __init__(
        self,
        engine: _FakeEngine,
        secret_store: _SecretStore | None = None,
        job_backend: _JobBackend | None = None,
        result_store: _ResultStore | None = None,
        metadata_probe_timeout_seconds: int = 5,
        export_per_user_per_hour: int = 10,
        download_url_ttl_seconds: int = 300,
    ) -> None:
        self.engine = engine
        self.rate_limiter = _RateLimiter()
        self.secret_store = secret_store or _SecretStore()
        self.job_backend = job_backend or _JobBackend()
        self.result_store = result_store or _ResultStore()
        self.metadata_probe_timeout_seconds = metadata_probe_timeout_seconds
        self.export_per_user_per_hour = export_per_user_per_hour
        self.download_url_ttl_seconds = download_url_ttl_seconds
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


class _FakeEngine:
    def __init__(self, results: list[object]) -> None:
        self.results = list(results)
        self.statements: list[str] = []

    def connect(self) -> _FakeConnection:
        return _FakeConnection(self)

    def begin(self) -> _FakeConnection:
        return _FakeConnection(self)


class _FakeConnection:
    def __init__(self, engine: _FakeEngine) -> None:
        self._engine = engine

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def execute(self, statement: object) -> _FakeResult:
        statement_text = str(statement)
        self._engine.statements.append(statement_text)
        if statement_text.lstrip().upper().startswith("SELECT"):
            return _FakeResult(self._engine.results.pop(0))
        return _FakeResult(None)


class _FakeResult:
    def __init__(self, result: object) -> None:
        self._result = result
        self.rowcount = 1

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

    def scalar_one(self) -> object:
        if isinstance(self._result, int):
            return self._result
        raise AssertionError(f"expected scalar result, got {type(self._result).__name__}")


class _JobBackend:
    def __init__(self) -> None:
        self.enqueued: list[Job] = []

    def enqueue(self, job: Job) -> None:
        self.enqueued.append(job)


class _ResultStore:
    def __init__(
        self,
        downloads: dict[str, bytes] | None = None,
        spool_exists: bool = True,
    ) -> None:
        self._downloads = downloads or {}
        self._spool_exists = spool_exists

    def spool_ref(self, result_set_id: str) -> ResultRef:
        return ResultRef(backend="local_fs", uri=f"spool/{result_set_id}")

    def spool_exists(self, result_set_id: str) -> bool:
        del result_set_id
        return self._spool_exists

    def get_spool_manifest(self, result_set_id: str) -> dict[str, object]:
        del result_set_id
        return {
            "columns": [{"name": "value", "type": "string"}],
            "loaded_rows": 1,
            "truncated": False,
        }

    def open_download(self, ref: ResultRef) -> io.BytesIO:
        if ref.uri not in self._downloads:
            raise FileNotFoundError(ref.uri)
        return io.BytesIO(self._downloads[ref.uri])


class _MetadataAdapter:
    def list_schemas(self) -> list[Schema]:
        return [Schema(name="app")]

    def list_tables(self, schema: str) -> list[Table]:
        return [Table(schema_name=schema, name="users", table_type="BASE TABLE")]

    def list_columns(self, schema: str, table: str) -> list[Column]:
        del schema, table
        return [
            Column(
                name="id",
                type=ColumnType.INTEGER,
                driver_type="INT",
                nullable=False,
                primary_key=True,
                comment="primary key",
            ),
            Column(
                name="name",
                type=ColumnType.STRING,
                driver_type="VARCHAR(64)",
                nullable=True,
                primary_key=False,
            ),
        ]


class _FailingMetadataAdapter:
    def list_schemas(self) -> list[Schema]:
        raise AdapterConnectionError("adapter connection failed")


class _SecretStore:
    def __init__(self) -> None:
        self.stored: list[str] = []
        self.deleted: list[str] = []

    def store_secret(self, plaintext: str, kind: object) -> object:
        self.stored.append(plaintext)
        return _SecretRef("secret-new")

    def delete_secret(self, ref: Any) -> None:
        self.deleted.append(str(ref.ref))


class _SecretRef:
    def __init__(self, ref: str) -> None:
        self.ref = ref
