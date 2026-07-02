from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from fastapi.routing import APIRoute

from app.api.app import create_app
from app.api.routes import core as core_routes
from app.api.security import create_access_token
from app.api.services import ApiServices
from app.dbclients.protocol import AdapterConnectionError
from app.domain.ai import AiResponse
from app.domain.compare_result import encode_compare_result_row
from app.domain.job import Job, JobKind
from app.domain.license import LicenseMode
from app.domain.result import ResultRef
from app.domain.schema import Column, ColumnType, Index, Row, Schema, Table
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
    assert ("GET", "/api/compare/tasks") in routes
    assert ("POST", "/api/compare/tasks") in routes
    assert ("GET", "/api/compare/tasks/{task_id}") in routes
    assert ("PATCH", "/api/compare/tasks/{task_id}") in routes
    assert ("DELETE", "/api/compare/tasks/{task_id}") in routes
    assert ("POST", "/api/compare/tasks/{task_id}/run") in routes
    assert ("GET", "/api/compare/runs/{run_id}/results") in routes
    assert ("GET", "/api/compare/runs/{run_id}/profile") in routes
    assert ("POST", "/api/compare/runs/{run_id}/ai-attribution") in routes
    assert ("POST", "/api/projects/{project_id}/compare/infer") in routes
    assert ("GET", "/api/projects/{project_id}/compare/suggest-tasks") in routes
    assert ("POST", "/api/projects/{project_id}/compare/draft-task") in routes
    assert ("POST", "/api/projects/{project_id}/lineage/analyze") in routes
    assert ("GET", "/api/projects/{project_id}/lineage/subgraph") in routes
    assert ("GET", "/api/projects/{project_id}/lineage/impact") in routes
    assert ("PATCH", "/api/projects/{project_id}/lineage/edges/{edge_id}") in routes
    assert ("GET", "/api/projects/{project_id}/workflows") in routes
    assert ("POST", "/api/projects/{project_id}/workflows") in routes
    assert ("GET", "/api/projects/{project_id}/workflows/{workflow_id}") in routes
    assert ("PUT", "/api/projects/{project_id}/workflows/{workflow_id}") in routes
    assert ("DELETE", "/api/projects/{project_id}/workflows/{workflow_id}") in routes
    assert ("POST", "/api/projects/{project_id}/workflows/{workflow_id}/runs") in routes
    assert ("GET", "/api/projects/{project_id}/workflow-runs/{run_id}") in routes
    assert ("POST", "/api/projects/{project_id}/workflow-runs/{run_id}:cancel") in routes
    assert ("GET", "/api/datasources/{datasource_id}/metadata/schemas") in routes
    assert ("GET", "/api/datasources/{datasource_id}/metadata/tables") in routes
    assert ("GET", "/api/datasources/{datasource_id}/metadata/columns") in routes
    assert ("GET", "/api/datasources/{datasource_id}/metadata/indexes") in routes
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
            [],
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


def test_delete_datasource_ignores_terminal_test_connection_jobs() -> None:
    engine = _FakeEngine(
        [
            _datasource_row(password_secret_ref="secret-old"),
            {"id": "project-1"},
            [],
            [],
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

    assert response.status_code == 204
    assert secret_store.deleted == ["secret-old"]
    assert any("DELETE FROM datasources" in statement for statement in engine.statements)
    reference_query = next(statement for statement in engine.statements if "FROM jobs" in statement)
    assert "jobs.kind !=" in reference_query
    assert "jobs.status NOT IN" in reference_query


def test_delete_datasource_still_blocks_business_job_references() -> None:
    engine = _FakeEngine(
        [
            _datasource_row(password_secret_ref="secret-old"),
            {"id": "project-1"},
            [
                {
                    "id": "job-query-success",
                    "kind": "sql_query",
                    "status": "success",
                },
            ],
            [],
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
        "references": [{"job_id": "job-query-success", "kind": "sql_query", "status": "success"}],
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
    indexes_response = client.get(
        "/api/datasources/ds-1/metadata/indexes",
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
    assert indexes_response.status_code == 200
    assert indexes_response.json() == [
        {"name": "PRIMARY", "columns": ["id"], "is_unique": True, "is_primary": True}
    ]
    assert {audit["action"] for audit in services.audits} >= {
        "metadata_schemas_list",
        "metadata_tables_list",
        "metadata_columns_list",
        "metadata_indexes_list",
    }
    assert adapter_kwargs == [
        {"connect_timeout_seconds": 6, "statement_timeout_seconds": 6},
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
                "expires_at": datetime.now(UTC) + timedelta(days=1),
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


def test_compare_task_create_persists_explicit_rules_contract() -> None:
    engine = _FakeEngine(
        [
            {"id": "project-1"},
            [
                {"id": "ds-source", "project_id": "project-1"},
                {"id": "ds-target", "project_id": "project-1"},
            ],
            _compare_task_row(),
        ]
    )
    services = _Services(engine)
    app = create_app(services=cast(ApiServices, services))

    response = AsgiClient(app).post(
        "/api/compare/tasks",
        headers=_auth_headers(),
        json_body={
            "project_id": "project-1",
            "name": "orders",
            "source_id": "ds-source",
            "target_id": "ds-target",
            "source_ref": {"kind": "table", "schema_name": "app", "table_name": "orders_a"},
            "target_ref": {"kind": "table", "schema_name": "app", "table_name": "orders_b"},
            "columns": [
                {"name": "id", "type": "integer"},
                {"name": "amount", "type": "decimal"},
            ],
            "compare_rules": {
                "key_columns": ["id"],
                "numeric_tolerance": 0.01,
                "schema_policy": "strict",
            },
            "run_limits": {"recursive_checksum": True, "bisection_factor": 8},
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["id"] == "task-1"
    assert payload["compare_rules"]["key_columns"] == ["id"]
    assert payload["compare_rules"]["schema_policy"] == "strict"
    assert payload["run_limits"]["bisection_factor"] == 8
    assert any("INSERT INTO compare_tasks" in statement for statement in engine.statements)
    assert any(audit["action"] == "compare_task_create" for audit in services.audits)


def test_compare_infer_contract_returns_mapping_confidence_and_pk_draft() -> None:
    source_row = _datasource_row()
    source_row["id"] = "ds-source"
    target_row = _datasource_row()
    target_row["id"] = "ds-target"
    engine = _FakeEngine(
        [
            {"id": "project-1"},
            [source_row, target_row],
            _metadata_cache(
                [
                    {
                        "name": "id",
                        "type": "integer",
                        "driver_type": "INT",
                        "nullable": False,
                        "primary_key": True,
                        "comment": None,
                    },
                    {
                        "name": "amount",
                        "type": "decimal",
                        "driver_type": "DECIMAL(12,2)",
                        "nullable": True,
                        "primary_key": False,
                        "comment": None,
                    },
                ]
            ),
            _metadata_cache(
                [
                    {
                        "name": "ID",
                        "type": "integer",
                        "driver_type": "INT",
                        "nullable": False,
                        "primary_key": True,
                        "comment": None,
                    },
                    {
                        "name": "amount",
                        "type": "decimal",
                        "driver_type": "DECIMAL(12,2)",
                        "nullable": True,
                        "primary_key": False,
                        "comment": None,
                    },
                ]
            ),
            _metadata_cache(
                [{"name": "PRIMARY", "columns": ["id"], "is_unique": True, "is_primary": True}]
            ),
            _metadata_cache(
                [{"name": "PRIMARY", "columns": ["ID"], "is_unique": True, "is_primary": True}]
            ),
        ]
    )
    app = create_app(services=cast(ApiServices, _Services(engine)))

    response = AsgiClient(app).post(
        "/api/projects/project-1/compare/infer",
        headers=_auth_headers(),
        json_body={
            "source_id": "ds-source",
            "target_id": "ds-target",
            "source_table": {"schema_name": "app", "table_name": "orders"},
            "target_table": {"schema_name": "app", "table_name": "orders"},
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["needs_manual_pk"] is False
    assert payload["pk_candidates"][0]["source_columns"] == ["id"]
    assert payload["pk_candidates"][0]["target_columns"] == ["ID"]
    assert payload["pk_candidates"][0]["reason"] == "primary_key"
    id_mapping = next(item for item in payload["mappings"] if item["source_column"] == "id")
    assert id_mapping["target_column"] == "ID"
    assert id_mapping["confidence"] > 0.8
    assert id_mapping["reason"] == "exact"
    assert id_mapping["conflict"] is False
    assert payload["compare_rules"]["key_columns"] == ["id"]
    assert payload["compare_rules"]["column_mappings"]["id"] == "ID"
    assert payload["columns"] == [
        {
            "name": "amount",
            "type": "decimal",
            "driver_type": "DECIMAL(12,2)",
            "nullable": True,
            "primary_key": False,
            "comment": None,
        }
    ]


def test_compare_suggest_tasks_contract_returns_normalized_table_pairs() -> None:
    source_row = _datasource_row()
    source_row["id"] = "ds-source"
    target_row = _datasource_row()
    target_row["id"] = "ds-target"
    engine = _FakeEngine(
        [
            {"id": "project-1"},
            [source_row, target_row],
            _metadata_cache([{"name": "src"}]),
            _metadata_cache(
                [
                    {"schema_name": "src", "name": "orders", "table_type": "BASE TABLE"},
                    {"schema_name": "src", "name": "src_customer", "table_type": "BASE TABLE"},
                ]
            ),
            _metadata_cache([{"name": "dst"}]),
            _metadata_cache(
                [
                    {"schema_name": "dst", "name": "orders", "table_type": "BASE TABLE"},
                    {"schema_name": "dst", "name": "customer", "table_type": "BASE TABLE"},
                ]
            ),
        ]
    )
    app = create_app(services=cast(ApiServices, _Services(engine)))

    response = AsgiClient(app).get(
        "/api/projects/project-1/compare/suggest-tasks",
        headers=_auth_headers(),
        params={"source_id": "ds-source", "target_id": "ds-target"},
    )

    assert response.status_code == 200
    suggestions = response.json()["suggestions"]
    assert suggestions[0]["source_table"] == "orders"
    assert suggestions[0]["target_table"] == "orders"
    assert suggestions[0]["reason"] == "exact"
    assert any(
        item["source_table"] == "src_customer"
        and item["target_table"] == "customer"
        and item["reason"] == "normalized"
        for item in suggestions
    )


def test_compare_draft_task_contract_reuses_compare_task_create() -> None:
    source_row = _datasource_row()
    source_row["id"] = "ds-source"
    target_row = _datasource_row()
    target_row["id"] = "ds-target"
    engine = _FakeEngine(
        [
            {"id": "project-1"},
            [source_row, target_row],
            _metadata_cache(
                [
                    {
                        "name": "id",
                        "type": "integer",
                        "driver_type": "INT",
                        "nullable": False,
                        "primary_key": True,
                        "comment": None,
                    },
                    {
                        "name": "amount",
                        "type": "decimal",
                        "driver_type": "DECIMAL(12,2)",
                        "nullable": True,
                        "primary_key": False,
                        "comment": None,
                    },
                ]
            ),
            _metadata_cache(
                [
                    {
                        "name": "ID",
                        "type": "integer",
                        "driver_type": "INT",
                        "nullable": False,
                        "primary_key": True,
                        "comment": None,
                    },
                    {
                        "name": "amount",
                        "type": "decimal",
                        "driver_type": "DECIMAL(12,2)",
                        "nullable": True,
                        "primary_key": False,
                        "comment": None,
                    },
                ]
            ),
            _metadata_cache(
                [{"name": "PRIMARY", "columns": ["id"], "is_unique": True, "is_primary": True}]
            ),
            _metadata_cache(
                [{"name": "PRIMARY", "columns": ["ID"], "is_unique": True, "is_primary": True}]
            ),
            {"id": "project-1"},
            [
                {"id": "ds-source", "project_id": "project-1"},
                {"id": "ds-target", "project_id": "project-1"},
            ],
            _compare_task_row(),
        ]
    )
    services = _Services(engine)
    app = create_app(services=cast(ApiServices, services))

    response = AsgiClient(app).post(
        "/api/projects/project-1/compare/draft-task",
        headers=_auth_headers(),
        json_body={
            "name": "orders draft",
            "source_id": "ds-source",
            "target_id": "ds-target",
            "source_table": {"schema_name": "app", "table_name": "orders_a"},
            "target_table": {"schema_name": "app", "table_name": "orders_b"},
        },
    )

    assert response.status_code == 201
    assert response.json()["id"] == "task-1"
    assert any("INSERT INTO compare_tasks" in statement for statement in engine.statements)
    assert any(audit["action"] == "compare_task_draft_create" for audit in services.audits)


def test_compare_task_run_enqueues_compare_job_and_run_index() -> None:
    engine = _FakeEngine([_compare_task_row(), {"id": "project-1"}])
    job_backend = _JobBackend()
    services = _Services(engine, job_backend=job_backend)
    app = create_app(services=cast(ApiServices, services))

    response = AsgiClient(app).post(
        "/api/compare/tasks/task-1/run",
        headers=_auth_headers(),
    )

    assert response.status_code == 202
    payload = response.json()
    assert set(payload) == {"job_id", "run_id"}
    job = job_backend.enqueued[0]
    assert job.kind is JobKind.COMPARE_RUN
    assert job.timeout_seconds == 1800
    assert job.datasource_ids == ["ds-source", "ds-target"]
    assert set(job.payload["bucket_result_set_ids"]) == {
        "only_source",
        "only_target",
        "diff",
        "same",
    }
    assert any("INSERT INTO run_index" in statement for statement in engine.statements)
    assert any(audit["action"] == "compare_run" for audit in services.audits)


def test_compare_run_result_contract_returns_bucket_counts_and_cell_diffs() -> None:
    diff_profile = _diff_profile_payload()
    sample_result = _sample_result_payload()
    run_row = {
        "run_id": "run-1",
        "job_id": "job-1",
        "project_id": "project-1",
        "bucket_spools": {"diff": "rs-diff"},
        "bucket_counts": {
            "only_source": 1,
            "only_target": 1,
            "diff": 1,
            "same": 7,
        },
        "progress": {
            "scanned_segments": 2,
            "skipped_segments": 1,
            "skipped_rows": 7,
            "row_mode_segments": 1,
        },
        "diff_profile": diff_profile,
        "sample_result": sample_result,
    }
    result_store = _ResultStore(
        rows=[
            encode_compare_result_row(
                pk={"id": 3},
                source={"id": 3, "amount": "10.00"},
                target={"id": 3, "amount": "11.00"},
                cells=[{"column": "amount", "source": "10.00", "target": "11.00"}],
            )
        ]
    )
    engine = _FakeEngine([run_row, {"id": "project-1"}])
    app = create_app(services=cast(ApiServices, _Services(engine, result_store=result_store)))

    response = AsgiClient(app).get(
        "/api/compare/runs/run-1/results",
        headers=_auth_headers(),
        params={"bucket": "diff", "offset": 0, "limit": 10},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["bucket_counts"] == {
        "only_source": 1,
        "only_target": 1,
        "diff": 1,
        "same": 7,
    }
    assert payload["progress"]["skipped_rows"] == 7
    assert payload["diff_profile"] == diff_profile
    assert payload["sample_result"] == sample_result
    assert payload["rows"] == [
        {
            "pk": {"id": 3},
            "source": {"id": 3, "amount": "10.00"},
            "target": {"id": 3, "amount": "11.00"},
            "cells": [{"column": "amount", "source": "10.00", "target": "11.00"}],
        }
    ]


def test_compare_run_profile_contract_returns_profile_without_rows() -> None:
    diff_profile = _diff_profile_payload()
    sample_result = _sample_result_payload()
    run_row = {
        "run_id": "run-1",
        "job_id": "job-1",
        "project_id": "project-1",
        "bucket_spools": {},
        "bucket_counts": {"only_source": 0, "only_target": 0, "diff": 1, "same": 10},
        "progress": {"row_mode_segments": 1},
        "diff_profile": diff_profile,
        "sample_result": sample_result,
    }
    app = create_app(
        services=cast(
            ApiServices,
            _Services(_FakeEngine([run_row, {"id": "project-1"}])),
        )
    )

    response = AsgiClient(app).get("/api/compare/runs/run-1/profile", headers=_auth_headers())

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_id"] == "run-1"
    assert payload["diff_profile"] == diff_profile
    assert payload["sample_result"] == sample_result
    assert "rows" not in payload


def test_compare_ai_attribution_uses_gateway_l2_profile() -> None:
    run_row = {
        "run_id": "run-1",
        "job_id": "job-1",
        "project_id": "project-1",
        "bucket_spools": {},
        "bucket_counts": {"only_source": 0, "only_target": 0, "diff": 1, "same": 10},
        "progress": {},
        "diff_profile": _diff_profile_payload(),
        "sample_result": None,
    }
    ai_row = {
        "enabled": True,
        "provider": "mock",
        "model": "mock-model",
        "base_url": None,
        "api_key_secret_ref": None,
        "max_auto_egress_level": 2,
        "l4_requires_optin": True,
        "enable_inference": True,
    }
    services = _Services(_FakeEngine([run_row, {"id": "project-1"}, ai_row]))
    app = create_app(services=cast(ApiServices, services))

    response = AsgiClient(app).post(
        "/api/compare/runs/run-1/ai-attribution",
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["attribution"] == "ok"
    assert payload["egress_level"] == 2
    assert any(audit["action"] == "compare_diff_attribution" for audit in services.audits)


def test_compare_ai_attribution_disabled_degrades_without_profile_loss() -> None:
    run_row = {
        "run_id": "run-1",
        "job_id": "job-1",
        "project_id": "project-1",
        "bucket_spools": {},
        "bucket_counts": {"only_source": 0, "only_target": 0, "diff": 1, "same": 10},
        "progress": {},
        "diff_profile": _diff_profile_payload(),
        "sample_result": None,
    }
    services = _Services(_FakeEngine([run_row, {"id": "project-1"}, None]))
    app = create_app(services=cast(ApiServices, services))

    response = AsgiClient(app).post(
        "/api/compare/runs/run-1/ai-attribution",
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    assert response.json()["error"] == "ai_disabled"
    assert response.json()["ok"] is False


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
    assert any(audit["action"] == "sql_export" for audit in services.audits)
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


def test_lineage_analyze_persists_edges_and_returns_cache_contract() -> None:
    engine = _FakeEngine(
        [
            {"id": "project-1"},
            _datasource_row(),
            [
                {
                    "cache_level": "columns",
                    "schema_name": "app",
                    "table_name": "src",
                    "payload": [{"name": "id", "type": "integer"}],
                },
                {
                    "cache_level": "columns",
                    "schema_name": "app",
                    "table_name": "tgt",
                    "payload": [{"name": "id", "type": "integer"}],
                },
            ],
            None,
            {
                "id": "run-1",
                "project_id": "project-1",
                "datasource_id": "ds-1",
                "dialect": "mysql",
                "source_ref": "script.sql",
                "sql_hash": "hash-1",
                "parser_version": "sqlglot-w1-v1",
                "status": "success",
                "parse_summary": {
                    "table_edge_count": 1,
                    "column_mapping_count": 1,
                    "parse_error_count": 0,
                },
            },
            1,
            1,
        ]
    )
    app = create_app(services=cast(ApiServices, _Services(engine)))

    response = AsgiClient(app).post(
        "/api/projects/project-1/lineage/analyze",
        headers=_auth_headers(),
        json_body={
            "datasource_id": "ds-1",
            "source_ref": "script.sql",
            "default_schema": "app",
            "sql_text": "INSERT INTO app.tgt (id) SELECT id FROM app.src",
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["run_id"] == "run-1"
    assert payload["project_id"] == "project-1"
    assert payload["cached"] is False
    assert payload["parser_version"] == "sqlglot-w1-v1"
    assert payload["table_edge_count"] == 1
    assert payload["column_edge_count"] == 1
    assert any("INSERT INTO lineage_runs" in statement for statement in engine.statements)
    assert any("INSERT INTO lineage_edges" in statement for statement in engine.statements)
    assert any("INSERT INTO lineage_column_edges" in statement for statement in engine.statements)


def test_lineage_analyze_cache_hit_does_not_reinsert_edges() -> None:
    engine = _FakeEngine(
        [
            {"id": "project-1"},
            _datasource_row(),
            [],
            {
                "id": "run-cached",
                "project_id": "project-1",
                "datasource_id": "ds-1",
                "dialect": "mysql",
                "source_ref": "script.sql",
                "sql_hash": "hash-1",
                "parser_version": "sqlglot-w1-v1",
                "status": "success",
                "parse_summary": {
                    "table_edge_count": 1,
                    "column_mapping_count": 0,
                    "parse_error_count": 1,
                    "parse_errors": [
                        {
                            "statement_index": 0,
                            "error_type": "unsupported_schema",
                            "message": "metadata cache missing for table app.src",
                            "unsupported": True,
                            "statement_type": "INSERT",
                        }
                    ],
                },
            },
            1,
            0,
        ]
    )
    app = create_app(services=cast(ApiServices, _Services(engine)))

    response = AsgiClient(app).post(
        "/api/projects/project-1/lineage/analyze",
        headers=_auth_headers(),
        json_body={
            "datasource_id": "ds-1",
            "source_ref": "script.sql",
            "sql_text": "INSERT INTO tgt (id) SELECT id FROM src",
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["cached"] is True
    assert payload["run_id"] == "run-cached"
    assert payload["table_edge_count"] == 1
    assert payload["parse_summary"]["parse_error_count"] == 1
    assert payload["parse_summary"]["parse_errors"] == [
        {
            "statement_index": 0,
            "error_type": "unsupported_schema",
            "message": "metadata cache missing for table app.src",
            "unsupported": True,
            "statement_type": "INSERT",
        }
    ]
    assert not any("INSERT INTO lineage_edges" in statement for statement in engine.statements)


def test_lineage_subgraph_contract_uses_recursive_cte() -> None:
    engine = _FakeEngine(
        [
            {"id": "project-1"},
            [
                {
                    "edge_id": "edge-1",
                    "source_table": "app.src",
                    "target_table": "app.mid",
                    "source_column": None,
                    "target_column": None,
                    "source": "app.src",
                    "target": "app.mid",
                    "edge_kind": "table",
                    "transformation": None,
                    "transformation_subtype": None,
                    "inferred": False,
                    "inference_status": "confirmed",
                    "confidence": 1,
                    "depth": 1,
                    "path": ["app.src", "app.mid"],
                    "direction": "downstream",
                }
            ],
        ]
    )
    app = create_app(services=cast(ApiServices, _Services(engine)))

    response = AsgiClient(app).get(
        "/api/projects/project-1/lineage/subgraph",
        headers=_auth_headers(),
        params={"focus": "app.src", "direction": "downstream", "max_depth": 3},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["node_count"] == 2
    assert payload["edge_count"] == 1
    assert payload["truncated"] is False
    assert payload["nodes"][0] == {
        "id": "app.src",
        "label": "app.src",
        "kind": "table",
        "table": "app.src",
        "column": None,
        "depth": 0,
    }
    assert payload["edges"][0]["source"] == "app.src"
    assert payload["edges"][0]["target"] == "app.mid"
    assert any("WITH RECURSIVE walk" in statement for statement in engine.statements)


def test_lineage_subgraph_include_columns_expands_bounded_column_edges() -> None:
    engine = _FakeEngine(
        [
            {"id": "project-1"},
            "edge-exists",
            [
                {
                    "edge_id": "edge-1",
                    "source_table": "app.src",
                    "target_table": "app.mid",
                    "source_column": None,
                    "target_column": None,
                    "source": "app.src",
                    "target": "app.mid",
                    "edge_kind": "table",
                    "transformation": None,
                    "transformation_subtype": None,
                    "inferred": False,
                    "inference_status": "confirmed",
                    "confidence": 1,
                    "depth": 1,
                    "path": ["app.src", "app.mid"],
                    "direction": "downstream",
                }
            ],
            [
                {
                    "id": "col-edge-1",
                    "source_table": "app.src",
                    "source_column": "id",
                    "target_table": "app.mid",
                    "target_column": "id",
                    "transformation": "DIRECT",
                    "transformation_subtype": "DIRECT",
                    "inferred": False,
                    "inference_status": "confirmed",
                    "confidence": 1,
                }
            ],
        ]
    )
    app = create_app(services=cast(ApiServices, _Services(engine)))

    response = AsgiClient(app).get(
        "/api/projects/project-1/lineage/subgraph",
        headers=_auth_headers(),
        params={
            "focus": "app.src",
            "direction": "downstream",
            "max_depth": 3,
            "include_columns": True,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert any(node["id"] == "app.src.id" and node["kind"] == "column" for node in payload["nodes"])
    assert any(
        edge["id"] == "col-edge-1" and edge["edge_kind"] == "column" for edge in payload["edges"]
    )


def test_lineage_impact_contract_returns_downstream_paths() -> None:
    engine = _FakeEngine(
        [
            {"id": "project-1"},
            "edge-exists",
            [
                {
                    "edge_id": "edge-1",
                    "source_table": "app.src",
                    "target_table": "app.mid",
                    "source_column": None,
                    "target_column": None,
                    "source": "app.src",
                    "target": "app.mid",
                    "edge_kind": "table",
                    "transformation": None,
                    "transformation_subtype": None,
                    "inferred": False,
                    "inference_status": "confirmed",
                    "confidence": 1,
                    "depth": 1,
                    "path": ["app.src", "app.mid"],
                    "direction": "downstream",
                }
            ],
        ]
    )
    app = create_app(services=cast(ApiServices, _Services(engine)))

    response = AsgiClient(app).get(
        "/api/projects/project-1/lineage/impact",
        headers=_auth_headers(),
        params={"focus": "app.src", "max_depth": 3},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["impact_count"] == 1
    assert payload["impacts"][0] == {
        "node": "app.mid",
        "table": "app.mid",
        "column": None,
        "depth": 1,
        "paths": [["app.src", "app.mid"]],
    }


def _lineage_run_row(parse_error_count: int = 2) -> dict[str, object]:
    return {
        "id": "run-1",
        "project_id": "project-1",
        "datasource_id": "ds-1",
        "dialect": "mysql",
        "source_ref": "script.sql",
        "sql_hash": "hash-1",
        "parser_version": "sqlglot-w1-v1",
        "status": "success",
        "parse_summary": {
            "table_edge_count": 0,
            "column_mapping_count": 0,
            "parse_error_count": parse_error_count,
        },
    }


def _ai_config_row() -> dict[str, object]:
    return {
        "enabled": True,
        "provider": "mock",
        "model": "mock-model",
        "base_url": None,
        "api_key_secret_ref": None,
        "max_auto_egress_level": 2,
        "l4_requires_optin": True,
        "enable_inference": True,
    }


def test_lineage_analyze_ai_fallback_writes_inferred_edges_via_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _FakeEngine(
        [
            {"id": "project-1"},
            _datasource_row(),
            [],  # empty metadata cache → unsupported_schema parse errors
            None,  # no cached run
            _lineage_run_row(),
            0,
            0,
            _ai_config_row(),
        ]
    )
    services = _Services(engine)
    app = create_app(services=cast(ApiServices, services))
    captured: dict[str, Any] = {}

    class _Gateway:
        def complete(self, prompt: str, context: Any, options: Any) -> Any:
            captured["prompt"] = prompt
            captured["context"] = context
            captured["options"] = options
            return AiResponse(
                content=json.dumps(
                    {
                        "table_edges": [
                            {
                                "source_table": "app.src",
                                "target_table": "app.tgt",
                                "confidence": 0.62,
                            }
                        ],
                        "column_edges": [
                            {
                                "source_table": "app.src",
                                "source_column": "id",
                                "target_table": "app.tgt",
                                "target_column": "id",
                                "confidence": 0.55,
                            }
                        ],
                    }
                ),
                provider="mock",
                model="mock-model",
            )

    monkeypatch.setattr(
        core_routes,
        "build_gateway_from_runtime_config",
        lambda runtime, **kwargs: _Gateway(),
    )

    response = AsgiClient(app).post(
        "/api/projects/project-1/lineage/analyze",
        headers=_auth_headers(),
        json_body={
            "datasource_id": "ds-1",
            "source_ref": "script.sql",
            "sql_text": (
                "INSERT INTO app.tgt (id, name) SELECT id, name FROM app.src "
                "WHERE name = 'top-secret-value'"
            ),
            "ai_fallback": True,
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["ai_fallback"] == {
        "requested": True,
        "executed": True,
        "reason": None,
        "inferred_table_edge_count": 1,
        "inferred_column_edge_count": 1,
        "provider": "mock",
        "model": "mock-model",
    }
    # R-AI:AI 只收脱敏片段(字面量不出境),L2 出站。
    context = captured["context"]
    assert len(context.items) == 1
    assert context.items[0].egress_level == 2
    assert context.items[0].redacted is True
    assert "top-secret-value" not in context.items[0].content
    assert "app.src" in context.items[0].content
    assert captured["options"].purpose == "lineage_ai_fallback"
    assert any("INSERT INTO lineage_edges" in statement for statement in engine.statements)
    assert any("INSERT INTO lineage_column_edges" in statement for statement in engine.statements)
    assert any("UPDATE lineage_runs" in statement for statement in engine.statements)
    assert any(
        audit["action"] == "lineage_ai_fallback" and audit["result"] == "success"
        for audit in services.audits
    )
    assert "top-secret-value" not in str(services.audits)


def test_lineage_analyze_ai_fallback_disabled_degrades_to_deterministic() -> None:
    engine = _FakeEngine(
        [
            {"id": "project-1"},
            _datasource_row(),
            [],
            None,
            _lineage_run_row(),
            0,
            0,
            None,  # no ai_configs row → ai disabled
        ]
    )
    services = _Services(engine)
    app = create_app(services=cast(ApiServices, services))

    response = AsgiClient(app).post(
        "/api/projects/project-1/lineage/analyze",
        headers=_auth_headers(),
        json_body={
            "datasource_id": "ds-1",
            "source_ref": "script.sql",
            "sql_text": "INSERT INTO app.tgt (id) SELECT id FROM app.src",
            "ai_fallback": True,
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["run_id"] == "run-1"
    assert payload["ai_fallback"]["requested"] is True
    assert payload["ai_fallback"]["executed"] is False
    assert payload["ai_fallback"]["reason"] == "ai_disabled"
    assert not any("INSERT INTO lineage_edges" in statement for statement in engine.statements)
    assert any(
        audit["action"] == "lineage_ai_fallback" and audit["result"] == "skipped"
        for audit in services.audits
    )


def test_lineage_analyze_ai_fallback_invalid_ai_response_degrades() -> None:
    # 真 DefaultAiGateway + MockProvider(回 "ok",不可解析为边 JSON)→ 优雅降级。
    engine = _FakeEngine(
        [
            {"id": "project-1"},
            _datasource_row(),
            [],
            None,
            _lineage_run_row(),
            0,
            0,
            _ai_config_row(),
        ]
    )
    services = _Services(engine)
    app = create_app(services=cast(ApiServices, services))

    response = AsgiClient(app).post(
        "/api/projects/project-1/lineage/analyze",
        headers=_auth_headers(),
        json_body={
            "datasource_id": "ds-1",
            "source_ref": "script.sql",
            "sql_text": "INSERT INTO app.tgt (id) SELECT id FROM app.src",
            "ai_fallback": True,
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["ai_fallback"]["executed"] is False
    assert payload["ai_fallback"]["reason"] == "invalid_ai_response"
    assert payload["table_edge_count"] == 0
    assert not any("INSERT INTO lineage_edges" in statement for statement in engine.statements)
    assert not any("UPDATE lineage_runs" in statement for statement in engine.statements)


def test_lineage_analyze_without_ai_fallback_keeps_response_shape() -> None:
    engine = _FakeEngine(
        [
            {"id": "project-1"},
            _datasource_row(),
            [],
            None,
            _lineage_run_row(),
            0,
            0,
        ]
    )
    services = _Services(engine)
    app = create_app(services=cast(ApiServices, services))

    response = AsgiClient(app).post(
        "/api/projects/project-1/lineage/analyze",
        headers=_auth_headers(),
        json_body={
            "datasource_id": "ds-1",
            "source_ref": "script.sql",
            "sql_text": "INSERT INTO app.tgt (id) SELECT id FROM app.src",
        },
    )

    assert response.status_code == 201
    assert response.json()["ai_fallback"] is None
    assert not any(audit["action"] == "lineage_ai_fallback" for audit in services.audits)


def _inferred_edge_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "edge-1",
        "run_id": "run-1",
        "project_id": "project-1",
        "source_table": "app.src",
        "target_table": "app.tgt",
        "edge_kind": "table",
        "inferred": True,
        "inference_status": "inferred",
        "confidence": 0.66,
        "sql_hash": "hash-1",
    }
    row.update(overrides)
    return row


def test_lineage_edge_inference_confirm_table_edge() -> None:
    engine = _FakeEngine([{"id": "project-1"}, _inferred_edge_row()])
    services = _Services(engine)
    app = create_app(services=cast(ApiServices, services))

    response = AsgiClient(app).request(
        "PATCH",
        "/api/projects/project-1/lineage/edges/edge-1",
        headers=_auth_headers(),
        json_body={"inference_status": "confirmed"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "edge_id": "edge-1",
        "edge_kind": "table",
        "inference_status": "confirmed",
        "inferred": True,
        "confidence": 0.66,
    }
    assert any(statement.startswith("UPDATE lineage_edges") for statement in engine.statements)
    assert any(audit["action"] == "lineage_edge_inference" for audit in services.audits)


def test_lineage_edge_inference_reject_column_edge() -> None:
    column_row = _inferred_edge_row(
        id="col-edge-1",
        source_column="id",
        target_column="id",
        transformation="DIRECT",
        transformation_subtype="DIRECT",
    )
    engine = _FakeEngine([{"id": "project-1"}, None, column_row])
    app = create_app(services=cast(ApiServices, _Services(engine)))

    response = AsgiClient(app).request(
        "PATCH",
        "/api/projects/project-1/lineage/edges/col-edge-1",
        headers=_auth_headers(),
        json_body={"inference_status": "rejected"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["edge_kind"] == "column"
    assert payload["inference_status"] == "rejected"
    assert any(
        statement.startswith("UPDATE lineage_column_edges") for statement in engine.statements
    )


def test_lineage_edge_inference_rejects_non_inferred_transition() -> None:
    engine = _FakeEngine(
        [{"id": "project-1"}, _inferred_edge_row(inference_status="confirmed", inferred=False)]
    )
    app = create_app(services=cast(ApiServices, _Services(engine)))

    response = AsgiClient(app).request(
        "PATCH",
        "/api/projects/project-1/lineage/edges/edge-1",
        headers=_auth_headers(),
        json_body={"inference_status": "rejected"},
    )

    assert response.status_code == 409
    assert response.json()["error"] == "invalid_inference_transition"
    assert not any(statement.startswith("UPDATE lineage_edges") for statement in engine.statements)


def test_lineage_edge_inference_rejects_invalid_status_value() -> None:
    engine = _FakeEngine([{"id": "project-1"}, _inferred_edge_row()])
    app = create_app(services=cast(ApiServices, _Services(engine)))

    response = AsgiClient(app).request(
        "PATCH",
        "/api/projects/project-1/lineage/edges/edge-1",
        headers=_auth_headers(),
        json_body={"inference_status": "inferred"},
    )

    assert response.status_code == 422


def test_lineage_edge_inference_cross_project_edge_is_not_found() -> None:
    engine = _FakeEngine([{"id": "project-1"}, _inferred_edge_row(project_id="project-other")])
    app = create_app(services=cast(ApiServices, _Services(engine)))

    response = AsgiClient(app).request(
        "PATCH",
        "/api/projects/project-1/lineage/edges/edge-1",
        headers=_auth_headers(),
        json_body={"inference_status": "confirmed"},
    )

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"
    assert not any(statement.startswith("UPDATE lineage_edges") for statement in engine.statements)


def test_workflow_create_contract_persists_spec_and_schedule_redundant_columns() -> None:
    engine = _FakeEngine([{"id": "project-1"}, None, _workflow_row()])
    services = _Services(engine)
    app = create_app(services=cast(ApiServices, services))

    response = AsgiClient(app).post(
        "/api/projects/project-1/workflows",
        headers=_auth_headers(),
        json_body={
            "name": "nightly-report",
            "spec": _workflow_spec_payload(schedule={"cron": "0 2 * * *", "enabled": True}),
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["id"] == "wf-1"
    assert payload["name"] == "nightly-report"
    assert payload["enabled"] is True
    assert payload["schedule_cron"] == "0 2 * * *"
    assert payload["schedule_enabled"] is True
    assert payload["spec"]["nodes"][0]["job_kind"] == "sql_query"
    insert_statement = next(
        statement
        for statement in engine.statements
        if statement.startswith("INSERT INTO workflows")
    )
    assert "dag_jsonb" in insert_statement
    assert "schedule_cron" in insert_statement
    assert "schedule_enabled" in insert_statement
    assert any(audit["action"] == "workflow_create" for audit in services.audits)


def test_workflow_create_rejects_forbidden_node_kind_before_any_db_write() -> None:
    engine = _FakeEngine([])
    app = create_app(services=cast(ApiServices, _Services(engine)))

    response = AsgiClient(app).post(
        "/api/projects/project-1/workflows",
        headers=_auth_headers(),
        json_body={"name": "bad", "spec": _workflow_spec_payload(job_kind="shell")},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "forbidden_node_kind"
    assert engine.statements == []


def test_workflow_create_rejects_unsupported_node_kind_distinct_from_forbidden() -> None:
    engine = _FakeEngine([])
    app = create_app(services=cast(ApiServices, _Services(engine)))

    response = AsgiClient(app).post(
        "/api/projects/project-1/workflows",
        headers=_auth_headers(),
        json_body={"name": "bad", "spec": _workflow_spec_payload(job_kind="notify")},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "unsupported_node_kind"
    assert engine.statements == []


def test_workflow_create_rejects_cyclic_dag() -> None:
    engine = _FakeEngine([])
    app = create_app(services=cast(ApiServices, _Services(engine)))

    response = AsgiClient(app).post(
        "/api/projects/project-1/workflows",
        headers=_auth_headers(),
        json_body={
            "name": "cyclic",
            "spec": {
                "nodes": [
                    {"id": "a", "job_kind": "sql_query", "timeout_seconds": 60},
                    {"id": "b", "job_kind": "sql_query", "timeout_seconds": 60},
                ],
                "edges": [
                    {"source": "a", "target": "b"},
                    {"source": "b", "target": "a"},
                ],
            },
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == "cycle_detected"
    assert engine.statements == []


def test_workflow_create_conflicting_name_returns_409() -> None:
    engine = _FakeEngine([{"id": "project-1"}, {"id": "wf-existing"}])
    app = create_app(services=cast(ApiServices, _Services(engine)))

    response = AsgiClient(app).post(
        "/api/projects/project-1/workflows",
        headers=_auth_headers(),
        json_body={"name": "nightly-report", "spec": _workflow_spec_payload()},
    )

    assert response.status_code == 409
    assert response.json()["error"] == "workflow_name_conflict"
    assert not any(statement.startswith("INSERT INTO workflows") for statement in engine.statements)


def test_workflow_create_without_project_access_returns_404() -> None:
    engine = _FakeEngine([None])
    app = create_app(services=cast(ApiServices, _Services(engine)))

    response = AsgiClient(app).post(
        "/api/projects/project-other/workflows",
        headers=_auth_headers(),
        json_body={"name": "nightly-report", "spec": _workflow_spec_payload()},
    )

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"
    assert not any(statement.startswith("INSERT INTO workflows") for statement in engine.statements)


def test_workflow_list_contract_scopes_to_project_and_paginates() -> None:
    engine = _FakeEngine([{"id": "project-1"}, [_workflow_row()]])
    app = create_app(services=cast(ApiServices, _Services(engine)))

    response = AsgiClient(app).get(
        "/api/projects/project-1/workflows",
        headers=_auth_headers(),
        params={"limit": 10, "offset": 0},
    )

    assert response.status_code == 200
    payload = response.json()
    assert [item["id"] for item in payload] == ["wf-1"]
    assert payload[0]["node_count"] == 1
    assert payload[0]["schedule_cron"] == "0 2 * * *"
    assert "spec" not in payload[0]
    statement = engine.statements[-1]
    assert "workflows.project_id" in statement
    assert "LIMIT" in statement
    assert "OFFSET" in statement


def test_workflow_detail_contract_returns_full_spec() -> None:
    engine = _FakeEngine([{"id": "project-1"}, _workflow_row()])
    app = create_app(services=cast(ApiServices, _Services(engine)))

    response = AsgiClient(app).get(
        "/api/projects/project-1/workflows/wf-1",
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == "wf-1"
    assert payload["spec"]["nodes"][0]["id"] == "n1"
    assert payload["spec"]["schedule"] == {"cron": "0 2 * * *", "enabled": True}


def test_workflow_detail_outside_project_returns_404() -> None:
    engine = _FakeEngine([{"id": "project-1"}, None])
    app = create_app(services=cast(ApiServices, _Services(engine)))

    response = AsgiClient(app).get(
        "/api/projects/project-1/workflows/wf-missing",
        headers=_auth_headers(),
    )

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_workflow_update_contract_revalidates_spec_and_syncs_schedule_columns() -> None:
    updated_row = _workflow_row()
    updated_row["name"] = "renamed"
    updated_row["dag_jsonb"] = _workflow_spec_payload()
    updated_row["schedule_cron"] = None
    updated_row["schedule_enabled"] = False
    engine = _FakeEngine([{"id": "project-1"}, _workflow_row(), None, updated_row])
    services = _Services(engine)
    app = create_app(services=cast(ApiServices, services))

    response = AsgiClient(app).request(
        "PUT",
        "/api/projects/project-1/workflows/wf-1",
        headers=_auth_headers(),
        json_body={"name": "renamed", "spec": _workflow_spec_payload()},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["name"] == "renamed"
    assert payload["schedule_cron"] is None
    assert payload["schedule_enabled"] is False
    update_statement = next(
        statement for statement in engine.statements if statement.startswith("UPDATE workflows")
    )
    assert "updated_at" in update_statement
    assert "schedule_cron" in update_statement
    assert any(audit["action"] == "workflow_update" for audit in services.audits)


def test_workflow_update_rejects_forbidden_node_kind_before_any_db_write() -> None:
    engine = _FakeEngine([])
    app = create_app(services=cast(ApiServices, _Services(engine)))

    response = AsgiClient(app).request(
        "PUT",
        "/api/projects/project-1/workflows/wf-1",
        headers=_auth_headers(),
        json_body={"name": "bad", "spec": _workflow_spec_payload(job_kind="shell")},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "forbidden_node_kind"
    assert engine.statements == []


def test_workflow_delete_contract_removes_definition() -> None:
    engine = _FakeEngine([{"id": "project-1"}, _workflow_row()])
    services = _Services(engine)
    app = create_app(services=cast(ApiServices, services))

    response = AsgiClient(app).request(
        "DELETE",
        "/api/projects/project-1/workflows/wf-1",
        headers=_auth_headers(),
    )

    assert response.status_code == 204
    assert any(statement.startswith("DELETE FROM workflows") for statement in engine.statements)
    assert any(audit["action"] == "workflow_delete" for audit in services.audits)


def test_workflow_run_trigger_contract_enqueues_workflow_run_job() -> None:
    engine = _FakeEngine([{"id": "project-1"}, _workflow_row()])
    services = _Services(engine)
    app = create_app(services=cast(ApiServices, services))

    response = AsgiClient(app).post(
        "/api/projects/project-1/workflows/wf-1/runs",
        headers=_auth_headers(),
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["workflow_id"] == "wf-1"
    assert payload["run_id"] == payload["job_id"]  # WorkflowRun 本身就是 job(ADR-0009)
    backend = services.job_backend
    assert len(backend.enqueued) == 1
    job = backend.enqueued[0]
    assert job.kind is JobKind.WORKFLOW_RUN
    assert job.id == payload["run_id"]
    assert job.project_id == "project-1"
    assert job.payload["workflow_id"] == "wf-1"
    assert job.payload["trigger"] == "manual"
    # spec 快照进 payload:执行期不回读 workflows 表
    spec = job.payload["spec"]
    assert isinstance(spec, dict)
    assert spec["nodes"][0]["id"] == "n1"
    # when 变量在触发时刻冻结进 payload:执行器与状态查询同源,决策不随时间漂移
    frozen = job.payload["when_variables"]
    assert isinstance(frozen, dict)
    assert set(frozen) == {"today", "now", "year", "month", "day"}
    assert all(isinstance(value, str) for value in frozen.values())
    assert any(audit["action"] == "workflow_run_trigger" for audit in services.audits)


def test_workflow_run_trigger_disabled_workflow_returns_409() -> None:
    row = _workflow_row()
    row["enabled"] = False
    engine = _FakeEngine([{"id": "project-1"}, row])
    services = _Services(engine)
    app = create_app(services=cast(ApiServices, services))

    response = AsgiClient(app).post(
        "/api/projects/project-1/workflows/wf-1/runs",
        headers=_auth_headers(),
    )

    assert response.status_code == 409
    assert response.json()["error"] == "workflow_disabled"
    assert services.job_backend.enqueued == []


def test_workflow_run_trigger_missing_workflow_returns_404() -> None:
    engine = _FakeEngine([{"id": "project-1"}, None])
    services = _Services(engine)
    app = create_app(services=cast(ApiServices, services))

    response = AsgiClient(app).post(
        "/api/projects/project-1/workflows/wf-missing/runs",
        headers=_auth_headers(),
    )

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"
    assert services.job_backend.enqueued == []


def test_workflow_run_status_contract_returns_run_and_node_states() -> None:
    engine = _FakeEngine(
        [
            {"id": "project-1"},
            _workflow_run_job_row(status="running"),
            [_workflow_child_job_row("n1", status="success")],
        ]
    )
    app = create_app(services=cast(ApiServices, _Services(engine)))

    response = AsgiClient(app).get(
        "/api/projects/project-1/workflow-runs/run-1",
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_id"] == "run-1"
    assert payload["workflow_id"] == "wf-1"
    assert payload["project_id"] == "project-1"
    assert payload["status"] == "running"
    assert len(payload["nodes"]) == 1
    node = payload["nodes"][0]
    assert node["node_id"] == "n1"
    assert node["job_kind"] == "sql_query"
    assert node["status"] == "success"
    assert node["job_id"] == "child-1"
    assert node["attempts"] == 0


def test_workflow_run_status_not_found_returns_404() -> None:
    engine = _FakeEngine([{"id": "project-1"}, None])
    app = create_app(services=cast(ApiServices, _Services(engine)))

    response = AsgiClient(app).get(
        "/api/projects/project-1/workflow-runs/run-missing",
        headers=_auth_headers(),
    )

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_workflow_run_cancel_contract_propagates_to_unfinished_children() -> None:
    engine = _FakeEngine(
        [
            {"id": "project-1"},
            _workflow_run_job_row(status="running"),
            [
                _workflow_child_job_row("n1", status="running", job_id="child-1"),
                _workflow_child_job_row("n2", status="success", job_id="child-2"),
                _workflow_child_job_row("n3", status="pending", job_id="child-3"),
            ],
        ]
    )
    services = _Services(engine)
    app = create_app(services=cast(ApiServices, services))

    response = AsgiClient(app).post(
        "/api/projects/project-1/workflow-runs/run-1:cancel",
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    assert response.json() == {"cancelled": True}
    backend = services.job_backend
    # run 软取消 + 未终态子 job 传播;已 success 的子 job 不动
    assert backend.cancel_requested == ["run-1", "child-1", "child-3"]
    assert backend.cancelled_pending == [("child-3", "workflow run cancelled")]
    assert any(audit["action"] == "workflow_run_cancel" for audit in services.audits)


def _workflow_run_job_row(*, status: str = "running") -> dict[str, object]:
    return {
        "id": "run-1",
        "kind": "workflow_run",
        "status": status,
        "owner_user_id": "user-1",
        "project_id": "project-1",
        "payload": {
            "workflow_id": "wf-1",
            "workflow_name": "nightly-report",
            "trigger": "manual",
            "spec": _workflow_spec_payload(),
        },
        "error": None,
        "retry_count": 0,
        "created_at": _dt(1),
        "started_at": _dt(2),
        "finished_at": None,
    }


def _workflow_child_job_row(
    node_id: str,
    *,
    status: str,
    job_id: str = "child-1",
) -> dict[str, object]:
    return {
        "id": job_id,
        "kind": "sql_query",
        "status": status,
        "owner_user_id": "user-1",
        "project_id": "project-1",
        "payload": {"sql": "SELECT 1", "workflow_node_id": node_id},
        "error": None,
        "retry_count": 0,
        "created_at": _dt(3),
        "started_at": _dt(4),
        "finished_at": _dt(5) if status in {"success", "failed", "cancelled"} else None,
        "parent_workflow_run_id": "run-1",
    }


def _workflow_spec_payload(
    *,
    job_kind: str = "sql_query",
    schedule: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "nodes": [{"id": "n1", "job_kind": job_kind, "timeout_seconds": 60}],
        "edges": [],
        "schedule": schedule,
    }


def _workflow_row() -> dict[str, object]:
    return {
        "id": "wf-1",
        "project_id": "project-1",
        "name": "nightly-report",
        "dag_jsonb": {
            "nodes": [
                {
                    "id": "n1",
                    "job_kind": "sql_query",
                    "payload": {},
                    "retry_policy": None,
                    "timeout_seconds": 60,
                    "on_failure": "abort",
                    "when": None,
                }
            ],
            "edges": [],
            "schedule": {"cron": "0 2 * * *", "enabled": True},
        },
        "schedule_cron": "0 2 * * *",
        "schedule_enabled": True,
        "enabled": True,
        "created_by": "user-1",
        "created_at": _dt(1),
        "updated_at": _dt(2),
    }


def _auth_headers(user_id: str = "user-1") -> dict[str, str]:
    token = create_access_token(user_id=user_id, role="admin", secret=_Services.jwt_secret)
    return {"Authorization": f"Bearer {token}"}


def _dt(second: int) -> datetime:
    return datetime(2026, 1, 1, 0, 0, second, tzinfo=UTC)


def _metadata_cache(payload: list[dict[str, object]]) -> dict[str, object]:
    return {"payload": payload, "expires_at": datetime.now(UTC) + timedelta(days=1)}


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


def _compare_task_row() -> dict[str, object]:
    return {
        "id": "task-1",
        "project_id": "project-1",
        "name": "orders",
        "source_id": "ds-source",
        "target_id": "ds-target",
        "source_ref": {"kind": "table", "schema_name": "app", "table_name": "orders_a"},
        "target_ref": {"kind": "table", "schema_name": "app", "table_name": "orders_b"},
        "columns": [
            {"name": "id", "type": "integer"},
            {"name": "amount", "type": "decimal"},
        ],
        "compare_rules": {"key_columns": ["id"], "schema_policy": "strict"},
        "run_limits": {
            "recursive_checksum": True,
            "bisection_factor": 8,
            "query_timeout_seconds": 1800,
        },
        "created_by": "user-1",
        "created_at": _dt(1),
        "updated_at": _dt(2),
    }


def _diff_profile_payload() -> dict[str, object]:
    return {
        "version": 1,
        "generated": True,
        "summary": {"diff_rows": 1, "same_rows": 10, "paired_rows_observed": 11},
        "columns": {
            "amount": {
                "type": "decimal",
                "observed_rows": 11,
                "changed_rows": 1,
                "diff_rate": 1.0,
                "numeric_delta": {
                    "count": 1,
                    "constant_offset": "-1.00",
                    "systematic_offset": True,
                },
            }
        },
        "missing_key_ranges": {"only_source": [], "only_target": []},
    }


def _sample_result_payload() -> dict[str, object]:
    return {
        "enabled": True,
        "mode": "pk_random_anchor",
        "requested_rows": 300,
        "sampled_rows": 300,
        "observed_differences": 0,
        "all_sampled_equal": True,
        "confidence": 0.95,
        "difference_rate_upper_bound": 0.00994,
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

    def execute(self, statement: object, parameters: object | None = None) -> _FakeResult:
        del parameters
        statement_text = str(statement)
        self._engine.statements.append(statement_text)
        command = statement_text.lstrip().upper()
        if command.startswith("SELECT") or command.startswith("WITH"):
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

    def scalar_one_or_none(self) -> object | None:
        if self._result is None or isinstance(self._result, (int, str)):
            return self._result
        raise AssertionError(f"expected optional scalar result, got {type(self._result).__name__}")


class _JobBackend:
    def __init__(self) -> None:
        self.enqueued: list[Job] = []
        self.cancel_requested: list[str] = []
        self.cancelled_pending: list[tuple[str, str]] = []

    def enqueue(self, job: Job) -> None:
        self.enqueued.append(job)

    def request_cancel(self, job_id: str) -> None:
        self.cancel_requested.append(job_id)

    def cancel_pending_job(self, job_id: str, reason: str = "cancelled") -> None:
        self.cancelled_pending.append((job_id, reason))


class _ResultStore:
    def __init__(
        self,
        downloads: dict[str, bytes] | None = None,
        spool_exists: bool = True,
        rows: list[Row] | None = None,
    ) -> None:
        self._downloads = downloads or {}
        self._spool_exists = spool_exists
        self._rows = rows or []

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

    def fetch_range(self, result_set_id: str, offset: int, limit: int) -> list[Row]:
        del result_set_id
        return list(self._rows[offset : offset + limit])

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

    def list_indexes(self, schema: str, table: str) -> list[Index]:
        del schema, table
        return [Index(name="PRIMARY", columns=["id"], is_unique=True, is_primary=True)]


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
