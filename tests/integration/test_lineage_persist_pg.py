from __future__ import annotations

import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, insert, select
from sqlalchemy.engine import Engine

from app.api.app import create_app
from app.api.security import create_access_token
from app.api.services import ApiServices, RateLimiter
from app.db.models import (
    datasources,
    lineage_column_edges,
    lineage_edges,
    lineage_runs,
    metadata,
    metadata_caches,
    projects,
    users,
)
from app.infrastructure.jobbackend.postgres import PostgresJobBackend
from app.infrastructure.resultstore.local_fs import LocalFsResultStore
from app.infrastructure.secretstore.local_file import LocalFileSecretStore
from tests._asgi_client import AsgiClient

pytestmark = pytest.mark.integration


def test_lineage_persist_subgraph_impact_and_sql_hash_cache(tmp_path: Path) -> None:
    engine = _pg_engine_or_skip()
    metadata.create_all(engine)
    _clear_metadata(engine)
    try:
        jwt_secret = secrets.token_urlsafe(48)
        services = ApiServices(
            engine=engine,
            job_backend=PostgresJobBackend(engine),
            secret_store=cast(LocalFileSecretStore, object()),
            result_store=LocalFsResultStore(tmp_path / "results"),
            jwt_secret=jwt_secret,
            rate_limiter=RateLimiter(limit=10_000),
        )
        client = AsgiClient(create_app(services=services))
        seed = _seed_lineage_project(engine)
        headers = _headers(seed.user_id, jwt_secret)
        sql = (
            "INSERT INTO app.tgt (id, amount) "
            "SELECT s.id, s.amount FROM app.src s "
            "JOIN app.dim d ON d.id = s.id WHERE d.flag = 1"
        )

        first = client.post(
            f"/api/projects/{seed.project_id}/lineage/analyze",
            headers=headers,
            json_body={
                "datasource_id": seed.datasource_id,
                "source_ref": "lineage-fixture.sql",
                "default_schema": "app",
                "sql_text": sql,
            },
        )

        assert first.status_code == 201, first.body.decode("utf-8")
        first_payload = first.json()
        assert first_payload["cached"] is False
        assert first_payload["table_edge_count"] >= 1
        with engine.connect() as conn:
            run_count = conn.execute(select(lineage_runs.c.id)).all()
            table_edges = conn.execute(select(lineage_edges)).mappings().all()
            column_edges = conn.execute(select(lineage_column_edges)).mappings().all()
        assert len(run_count) == 1
        assert {row["target_table"] for row in table_edges} == {"app.tgt"}
        assert any(row["source_table"] == "app.src" for row in table_edges)
        assert any(row["target_column"] == "id" for row in column_edges)

        subgraph = client.get(
            f"/api/projects/{seed.project_id}/lineage/subgraph",
            headers=headers,
            params={"focus": "app.src", "direction": "downstream", "max_depth": 2},
        )
        assert subgraph.status_code == 200, subgraph.body.decode("utf-8")
        subgraph_payload = subgraph.json()
        assert subgraph_payload["edge_count"] >= 1
        assert any(node["id"] == "app.tgt" for node in subgraph_payload["nodes"])

        impact = client.get(
            f"/api/projects/{seed.project_id}/lineage/impact",
            headers=headers,
            params={"focus": "app.src", "max_depth": 2},
        )
        assert impact.status_code == 200, impact.body.decode("utf-8")
        impact_payload = impact.json()
        assert any(item["node"] == "app.tgt" for item in impact_payload["impacts"])

        second = client.post(
            f"/api/projects/{seed.project_id}/lineage/analyze",
            headers=headers,
            json_body={
                "datasource_id": seed.datasource_id,
                "source_ref": "lineage-fixture.sql",
                "default_schema": "app",
                "sql_text": sql,
            },
        )
        assert second.status_code == 201, second.body.decode("utf-8")
        assert second.json()["cached"] is True
        assert second.json()["run_id"] == first_payload["run_id"]
        with engine.connect() as conn:
            assert len(conn.execute(select(lineage_runs.c.id)).all()) == 1
    finally:
        _clear_metadata(engine)


class _SeededLineageProject:
    def __init__(self, *, user_id: str, project_id: str, datasource_id: str) -> None:
        self.user_id = user_id
        self.project_id = project_id
        self.datasource_id = datasource_id


def _seed_lineage_project(engine: Engine) -> _SeededLineageProject:
    user_id = str(uuid4())
    project_id = str(uuid4())
    datasource_id = str(uuid4())
    with engine.begin() as conn:
        conn.execute(
            insert(users).values(
                id=user_id,
                username="user-" + user_id,
                password_hash="not-used-in-this-test",
                role="admin",
            )
        )
        conn.execute(
            insert(projects).values(
                id=project_id,
                name="Project " + project_id,
                owner_user_id=user_id,
            )
        )
        conn.execute(
            insert(datasources).values(
                id=datasource_id,
                project_id=project_id,
                name="Lineage Source " + datasource_id,
                db_type="mysql",
                host="db.example.test",
                port=3306,
                username="dataops",
                database_name="app",
                password_secret_ref="not-used",
            )
        )
        for table_name, columns in {
            "src": ["id", "amount"],
            "dim": ["id", "flag"],
            "tgt": ["id", "amount"],
        }.items():
            conn.execute(
                insert(metadata_caches).values(
                    id=str(uuid4()),
                    datasource_id=datasource_id,
                    cache_level="columns",
                    schema_name="app",
                    table_name=table_name,
                    payload=[{"name": column, "type": "integer"} for column in columns],
                    expires_at=datetime(2099, 1, 1, tzinfo=UTC),
                )
            )
    return _SeededLineageProject(
        user_id=user_id,
        project_id=project_id,
        datasource_id=datasource_id,
    )


def _pg_engine_or_skip() -> Engine:
    url = os.environ.get("DATAOPS_TEST_PG_URL") or os.environ.get("DATAOPS_DATABASE_URL")
    if not url:
        pytest.skip("Postgres integration tests require DATAOPS_TEST_PG_URL")
    return create_engine(url, pool_size=10, max_overflow=20, pool_pre_ping=True)


def _clear_metadata(engine: Engine) -> None:
    with engine.begin() as conn:
        for table in reversed(metadata.sorted_tables):
            conn.execute(table.delete())


def _headers(user_id: str, jwt_secret: str) -> dict[str, str]:
    token = create_access_token(user_id=user_id, role="admin", secret=jwt_secret)
    return {"Authorization": "Bearer " + token}
