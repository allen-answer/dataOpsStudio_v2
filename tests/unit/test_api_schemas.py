from __future__ import annotations

from app.api.schemas import DatasourceCreateRequest, DatasourceResponse
from app.domain.datasource import DbType


def test_datasource_create_request_accepts_missing_database() -> None:
    body = DatasourceCreateRequest(
        project_id="project-1",
        name="mysql-no-db",
        db_type=DbType.MYSQL,
        host="127.0.0.1",
        port=3306,
        username="dataops",
        database=None,
        password="secret",
    )

    assert body.database is None


def test_datasource_response_accepts_missing_database() -> None:
    response = DatasourceResponse(
        id="ds-1",
        project_id="project-1",
        name="dm-no-schema",
        db_type=DbType.DM,
        host="127.0.0.1",
        port=5236,
        username="SYSDBA",
        database=None,
        environment="test",
    )

    assert response.database is None
