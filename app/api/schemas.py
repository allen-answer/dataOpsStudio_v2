from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.domain.datasource import DbType
from app.domain.job import JobStatus


class LoginRequest(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class ProjectResponse(BaseModel):
    id: str
    name: str
    description: str | None = None


class DatasourceCreateRequest(BaseModel):
    project_id: str
    name: str = Field(min_length=1)
    db_type: DbType
    host: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)
    username: str = Field(min_length=1)
    database: str = Field(min_length=1)
    password: str = Field(min_length=1)
    environment: str = "dev"
    extra: dict[str, Any] = Field(default_factory=dict)


class DatasourceResponse(BaseModel):
    id: str
    project_id: str
    name: str
    db_type: DbType
    host: str
    port: int
    username: str
    database: str
    environment: str
    extra: dict[str, Any] = Field(default_factory=dict)


class DatasourceTestResponse(BaseModel):
    ok: bool


class SqlExecuteRequest(BaseModel):
    datasource_id: str
    sql: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)


class SqlExecuteResponse(BaseModel):
    job_id: str
    result_set_id: str


class JobResponse(BaseModel):
    id: str
    kind: str
    status: JobStatus
    result_set_id: str | None = None
    error: str | None = None
    message: str | None = None


class RowResponse(BaseModel):
    values: list[Any]


class JobResultResponse(BaseModel):
    job_id: str
    result_set_id: str
    offset: int
    limit: int
    rows: list[RowResponse]
    loaded_rows: int | None = None
    truncated: bool | None = None


class CancelResponse(BaseModel):
    cancelled: bool
