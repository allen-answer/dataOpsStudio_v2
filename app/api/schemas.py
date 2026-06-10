from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.domain.datasource import DbType, OperationPolicy
from app.domain.job import JobErrorCode, JobStatus
from app.domain.schema import Column


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
    operation_policy: OperationPolicy = Field(default_factory=OperationPolicy)


class DatasourceUpdateRequest(BaseModel):
    project_id: str | None = None
    name: str | None = Field(default=None, min_length=1)
    db_type: DbType | None = None
    host: str | None = Field(default=None, min_length=1)
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = Field(default=None, min_length=1)
    database: str | None = Field(default=None, min_length=1)
    password: str | None = None
    environment: str | None = None
    extra: dict[str, Any] | None = None
    operation_policy: OperationPolicy | None = None


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
    operation_policy: OperationPolicy = Field(default_factory=OperationPolicy)


class DatasourceListItem(BaseModel):
    id: str
    name: str
    db_type: DbType
    host: str
    port: int
    environment: str
    environment_verified: bool = False
    database: str | None = None
    operation_policy: OperationPolicy = Field(default_factory=OperationPolicy)
    created_at: datetime


class DatasourceReferenceItem(BaseModel):
    job_id: str
    kind: str
    status: JobStatus


class DatasourceDeleteBlockedResponse(BaseModel):
    error: str
    message: str
    references: list[DatasourceReferenceItem] = Field(default_factory=list)


DatasourceTestErrorCode = Literal[
    "auth_failed",
    "host_unreachable",
    "timeout",
    "permission_denied",
    "unknown",
]
# 2.0.0 先诚实暴露保守分类:当前实际只稳定产出 unknown / timeout。
# auth_failed / host_unreachable / permission_denied 是多方言统一错误分类的留位枚举,
# 待 Oracle / DM / DB2 等 adapter 一起定义结构化错误码后再落精确分类。


class DatasourceTestResponse(BaseModel):
    ok: bool
    server_version: str | None = None
    latency_ms: int | None = None
    error_code: DatasourceTestErrorCode | None = None


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
    error_code: JobErrorCode | None = None
    message: str | None = None


class JobListItem(BaseModel):
    id: str
    kind: str
    status: JobStatus
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    error_code: JobErrorCode | None = None


class RowResponse(BaseModel):
    values: list[Any]


class JobResultResponse(BaseModel):
    job_id: str
    result_set_id: str
    offset: int
    limit: int
    columns: list[Column] = Field(default_factory=list)
    rows: list[RowResponse]
    loaded_rows: int | None = None
    truncated: bool | None = None


class CancelResponse(BaseModel):
    cancelled: bool


class LicenseStatusResponse(BaseModel):
    mode: str
    edition: str | None = None
    customer: str | None = None
    expires_at: datetime | None = None
    limits: dict[str, Any] = Field(default_factory=dict)
    features: list[str] = Field(default_factory=list)
    repair_reason: str | None = None
    trial_days_remaining: int | None = None


class LicenseUploadRequest(BaseModel):
    license_text: str = Field(min_length=1)


class AdminUserItem(BaseModel):
    id: str
    username: str
    role: str
    mfa_enabled: bool
    created_at: datetime


class AdminUserCreateRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    role: str = Field(min_length=1)
    initial_password: str = Field(min_length=1)


class AdminUserPatchRequest(BaseModel):
    role: str = Field(min_length=1)


class AdminResetPasswordResponse(BaseModel):
    temporary_password: str


class AdminDeleteBlockedResponse(BaseModel):
    error: str
    message: str
    owned_projects: list[ProjectResponse] = Field(default_factory=list)


class AdminProjectItem(BaseModel):
    id: str
    name: str
    description: str | None = None
    owner_user_id: str
    member_count: int
    datasource_count: int
    job_count: int
    created_at: datetime


class AdminProjectCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str | None = None
    owner_user_id: str
    members: list[str] = Field(default_factory=list)


class AdminProjectPatchRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = None
    owner_user_id: str | None = None
    members: list[str] | None = None


class AdminProjectDeleteImpactResponse(BaseModel):
    datasource_count: int
    job_count: int


class AuditLogItem(BaseModel):
    id: int
    ts: datetime
    user_id: str | None = None
    project_id: str | None = None
    action: str
    resource_type: str | None = None
    resource_id: str | None = None
    result: str
    request_id: str | None = None
    detail: dict[str, Any] | None = None
