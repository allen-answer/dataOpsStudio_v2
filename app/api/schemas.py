from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.domain.compare_infer import (
    ColumnMappingCandidate,
    PrimaryKeyCandidate,
    TablePairSuggestion,
)
from app.domain.datasource import DbType, OperationPolicy
from app.domain.job import JobErrorCode, JobStatus
from app.domain.schema import Column
from app.domain.workflow import WorkflowSpec


class LoginRequest(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)
    mfa_code: str | None = Field(default=None, min_length=1)


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
    console_id: str | None = None


class SqlExecuteResponse(BaseModel):
    job_id: str
    result_set_id: str


CompareBucket = Literal["only_source", "only_target", "diff", "same"]


class CompareDataRef(BaseModel):
    kind: Literal["table", "sql"] = "table"
    schema_name: str | None = None
    table_name: str | None = Field(default=None, min_length=1)
    sql: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _validate_ref(self) -> CompareDataRef:
        if self.kind == "table" and not self.table_name:
            raise ValueError("table ref requires table_name")
        if self.kind == "sql" and not self.sql:
            raise ValueError("sql ref requires sql")
        return self


class CompareRulesPayload(BaseModel):
    key_columns: list[str] = Field(default_factory=list)
    ignore_columns: list[str] = Field(default_factory=list)
    column_mappings: dict[str, str] = Field(default_factory=dict)
    numeric_tolerance: float | None = None
    trim_strings: bool = False
    case_insensitive: bool = False
    empty_as_null: bool = False
    schema_policy: Literal["warn", "strict"] = "warn"


class CompareRunLimitsPayload(BaseModel):
    max_rows: int | None = Field(default=None, gt=0)
    export_max_rows: int | None = Field(default=None, gt=0)
    fetch_chunk_size: int = Field(default=1000, gt=0)
    compare_batch_size: int = Field(default=10_000, gt=0)
    stream_compare: bool = True
    recursive_checksum: bool = True
    bisection_factor: int = Field(default=16, ge=8, le=32)
    bisection_threshold: int = Field(default=16_000, gt=0)
    max_bisection_depth: int = Field(default=8, ge=0)
    sample_quick_check: bool = False
    sample_size: int = Field(default=300, gt=0)
    sample_confidence: float = Field(default=0.95, gt=0, lt=1)
    result_format: str = "parquet"
    persist_same_bucket: bool = False
    query_timeout_seconds: int = Field(default=1800, gt=0)
    run_disk_quota_mb: int | None = Field(default=None, gt=0)


class CompareTaskCreateRequest(BaseModel):
    project_id: str
    name: str = Field(min_length=1, max_length=128)
    source_id: str
    target_id: str
    source_ref: CompareDataRef
    target_ref: CompareDataRef
    columns: list[Column] = Field(default_factory=list)
    compare_rules: CompareRulesPayload = Field(default_factory=CompareRulesPayload)
    run_limits: CompareRunLimitsPayload = Field(default_factory=CompareRunLimitsPayload)


class CompareTaskUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    source_id: str | None = None
    target_id: str | None = None
    source_ref: CompareDataRef | None = None
    target_ref: CompareDataRef | None = None
    columns: list[Column] | None = None
    compare_rules: CompareRulesPayload | None = None
    run_limits: CompareRunLimitsPayload | None = None


class CompareTaskResponse(BaseModel):
    id: str
    project_id: str
    name: str
    source_id: str
    target_id: str
    source_ref: CompareDataRef
    target_ref: CompareDataRef
    columns: list[Column] = Field(default_factory=list)
    compare_rules: CompareRulesPayload
    run_limits: CompareRunLimitsPayload
    created_by: str | None = None
    created_at: datetime
    updated_at: datetime


class CompareRunCreateResponse(BaseModel):
    job_id: str
    run_id: str


class CompareCellDiff(BaseModel):
    column: str
    source: Any | None = None
    target: Any | None = None


class CompareResultRow(BaseModel):
    pk: dict[str, Any]
    source: dict[str, Any] | None = None
    target: dict[str, Any] | None = None
    cells: list[CompareCellDiff] = Field(default_factory=list)


class CompareRunResultResponse(BaseModel):
    job_id: str
    run_id: str
    bucket: CompareBucket
    offset: int
    limit: int
    bucket_counts: dict[CompareBucket, int]
    progress: dict[str, int] = Field(default_factory=dict)
    diff_profile: dict[str, Any] = Field(default_factory=dict)
    sample_result: dict[str, Any] | None = None
    rows: list[CompareResultRow] = Field(default_factory=list)


class CompareRunProfileResponse(BaseModel):
    job_id: str
    run_id: str
    bucket_counts: dict[CompareBucket, int]
    progress: dict[str, int] = Field(default_factory=dict)
    diff_profile: dict[str, Any] = Field(default_factory=dict)
    sample_result: dict[str, Any] | None = None


class CompareAiAttributionResponse(BaseModel):
    run_id: str
    ok: bool
    attribution: str | None = None
    provider: str | None = None
    model: str | None = None
    error: str | None = None
    egress_level: int = 2


class CompareTableRequest(BaseModel):
    schema_name: str = Field(min_length=1)
    table_name: str = Field(min_length=1)


class CompareInferRequest(BaseModel):
    source_id: str
    target_id: str
    source_table: CompareTableRequest
    target_table: CompareTableRequest


class CompareInferResponse(BaseModel):
    project_id: str
    source_id: str
    target_id: str
    source_table: CompareTableRequest
    target_table: CompareTableRequest
    mappings: list[ColumnMappingCandidate]
    pk_candidates: list[PrimaryKeyCandidate]
    needs_manual_pk: bool
    compare_rules: CompareRulesPayload
    columns: list[Column] = Field(default_factory=list)


class CompareSuggestedTaskCreateRequest(CompareInferRequest):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    run_limits: CompareRunLimitsPayload = Field(default_factory=CompareRunLimitsPayload)


class CompareTaskSuggestionResponse(BaseModel):
    suggestions: list[TablePairSuggestion]


LineageDirection = Literal["upstream", "downstream", "both"]


class LineageAnalyzeRequest(BaseModel):
    datasource_id: str
    sql_text: str = Field(min_length=1)
    source_ref: str = Field(min_length=1, max_length=512)
    dialect: str | None = Field(default=None, min_length=1, max_length=32)
    default_schema: str | None = Field(default=None, min_length=1, max_length=128)
    # AI 兜底解析(L2,默认关;设计稿 §2.4 第 7 条)。开关形态设计稿未定,
    # 先按请求级可选字段实现(最小假设,见 PR 说明)。
    ai_fallback: bool = False


class LineageAiFallbackResult(BaseModel):
    """analyze 响应中 AI 兜底执行情况(纯追加;executed=False 时 reason 说明原因)。"""

    requested: bool
    executed: bool
    reason: str | None = None
    inferred_table_edge_count: int = 0
    inferred_column_edge_count: int = 0
    provider: str | None = None
    model: str | None = None


class LineageAnalyzeResponse(BaseModel):
    run_id: str
    project_id: str
    datasource_id: str
    dialect: str
    source_ref: str
    sql_hash: str
    parser_version: str
    status: Literal["success", "failed"]
    cached: bool
    parse_summary: dict[str, Any] = Field(default_factory=dict)
    table_edge_count: int
    column_edge_count: int
    ai_fallback: LineageAiFallbackResult | None = None


class LineageSubgraphNode(BaseModel):
    id: str
    label: str
    kind: Literal["table", "column"]
    table: str
    column: str | None = None
    depth: int


class LineageSubgraphEdge(BaseModel):
    id: str
    source: str
    target: str
    source_table: str
    target_table: str
    source_column: str | None = None
    target_column: str | None = None
    depth: int
    direction: Literal["upstream", "downstream"]
    edge_kind: str
    inferred: bool = False
    inference_status: str = "confirmed"
    confidence: float = 1.0
    transformation: str | None = None
    transformation_subtype: str | None = None


class LineageSubgraphResponse(BaseModel):
    project_id: str
    focus: str
    direction: LineageDirection
    max_depth: int
    include_columns: bool
    truncated: bool
    node_count: int
    edge_count: int
    depth_counts: dict[int, int] = Field(default_factory=dict)
    nodes: list[LineageSubgraphNode] = Field(default_factory=list)
    edges: list[LineageSubgraphEdge] = Field(default_factory=list)


class LineageEdgeInferenceUpdateRequest(BaseModel):
    """AI 推断边状态机转换请求(设计稿 §2.4 第 7 条:inferred → confirmed / rejected)。"""

    inference_status: Literal["confirmed", "rejected"]


class LineageEdgeInferenceResponse(BaseModel):
    edge_id: str
    edge_kind: Literal["table", "column"]
    inference_status: str
    inferred: bool
    confidence: float


class LineageImpactItem(BaseModel):
    node: str
    table: str
    column: str | None = None
    depth: int
    paths: list[list[str]] = Field(default_factory=list)


class LineageImpactResponse(BaseModel):
    project_id: str
    focus: str
    max_depth: int
    truncated: bool
    impact_count: int
    impacts: list[LineageImpactItem] = Field(default_factory=list)


class JobResponse(BaseModel):
    id: str
    kind: str
    status: JobStatus
    created_at: datetime
    finished_at: datetime | None = None
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
    total_rows: int | None = None
    state: str | None = None
    truncated: bool | None = None


ExportFormat = Literal["csv", "excel", "json", "sql"]


class ExportCreateRequest(BaseModel):
    format: ExportFormat
    table_name: str = Field(default="exported_result", min_length=1, max_length=128)


class CompareExportCreateRequest(BaseModel):
    format: Literal["excel"] = "excel"


class ExportCreateResponse(BaseModel):
    job_id: str
    download_token: str
    expires_at: datetime
    format: ExportFormat
    filename: str


class CancelResponse(BaseModel):
    cancelled: bool


class MetadataSchemaItem(BaseModel):
    name: str


class MetadataTableItem(BaseModel):
    schema_name: str
    name: str
    table_type: str | None = None


class MetadataColumnItem(BaseModel):
    name: str
    type: str
    driver_type: str | None = None
    nullable: bool
    primary_key: bool
    comment: str | None = None


class MetadataIndexItem(BaseModel):
    name: str
    columns: list[str] = Field(default_factory=list)
    is_unique: bool = False
    is_primary: bool = False


class SqlFormatRequest(BaseModel):
    sql: str = Field(min_length=1)
    dialect: str = Field(min_length=1)


class SqlFormatResponse(BaseModel):
    formatted_sql: str


class SqlExpandStarRequest(BaseModel):
    sql: str = Field(min_length=1)
    datasource_id: str


class SqlExpandStarResponse(BaseModel):
    expanded_sql: str


class SqlConsoleCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    datasource_id: str | None = None
    sql: str = ""
    pinned: bool = False


class SqlConsoleUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    datasource_id: str | None = None
    sql: str | None = None
    pinned: bool | None = None


class SqlConsoleResponse(BaseModel):
    id: str
    name: str
    datasource_id: str | None = None
    sql: str
    pinned: bool
    created_at: datetime
    updated_at: datetime


class SqlHistoryItem(BaseModel):
    job_id: str
    datasource_id: str | None = None
    datasource_name: str | None = None
    sql: str
    sql_hash: str
    status: JobStatus
    created_at: datetime
    finished_at: datetime | None = None
    result_set_id: str | None = None


class SqlTemplateCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    sql_text: str = Field(min_length=1)
    variables: list[str] = Field(default_factory=list)
    category: str = Field(default="general", min_length=1, max_length=64)
    project_id: str | None = None


class SqlTemplateUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = None
    sql_text: str | None = Field(default=None, min_length=1)
    variables: list[str] | None = None
    category: str | None = Field(default=None, min_length=1, max_length=64)
    project_id: str | None = None


class SqlTemplateResponse(BaseModel):
    id: str
    name: str
    description: str | None = None
    sql_text: str
    variables: list[str] = Field(default_factory=list)
    category: str
    project_id: str | None = None
    created_at: datetime
    updated_at: datetime


class SqlTemplateRenderRequest(BaseModel):
    values: dict[str, str] = Field(default_factory=dict)


class SqlTemplateRenderResponse(BaseModel):
    sql_text: str


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


class AccountSecurityStatusResponse(BaseModel):
    mfa_enabled: bool
    recovery_codes_total: int = 0
    recovery_codes_used: int = 0


class AccountPasswordChangeRequest(BaseModel):
    old_password: str = Field(min_length=1)
    new_password: str = Field(min_length=1)


class AccountPasswordChangeResponse(BaseModel):
    changed: bool


class MfaEnrollResponse(BaseModel):
    secret: str
    otpauth_uri: str


class MfaVerifyRequest(BaseModel):
    code: str = Field(min_length=6, max_length=64)


class MfaVerifyResponse(BaseModel):
    enabled: bool
    recovery_codes: list[str] = Field(default_factory=list)


class MfaDisableRequest(BaseModel):
    code: str = Field(min_length=6, max_length=6)


class MfaDisableResponse(BaseModel):
    enabled: bool = False


class RecoveryCodesRegenerateRequest(BaseModel):
    code: str = Field(min_length=6, max_length=6)


class RecoveryCodesResponse(BaseModel):
    recovery_codes: list[str] = Field(default_factory=list)


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


class AdminForceLogoutResponse(BaseModel):
    user_id: str
    revoked_after: datetime


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


AiProvider = Literal["off", "mock", "openai_compatible", "anthropic", "ollama"]
AiApiKeySource = Literal["none", "stored", "env"]


class AdminAiConfigResponse(BaseModel):
    enabled: bool
    provider: AiProvider
    model: str | None = None
    base_url: str | None = None
    max_auto_egress_level: int = Field(ge=0, le=3)
    l4_requires_optin: bool
    enable_inference: bool
    enable_auto_translation: bool
    api_key_source: AiApiKeySource
    has_stored_api_key: bool
    updated_at: datetime | None = None


class AdminAiConfigUpdateRequest(BaseModel):
    enabled: bool
    provider: AiProvider
    model: str | None = Field(default=None, max_length=128)
    base_url: str | None = Field(default=None, min_length=1)
    api_key: str | None = Field(default=None, min_length=1)
    clear_api_key: bool = False
    max_auto_egress_level: int = Field(ge=0, le=3)
    l4_requires_optin: bool = True
    enable_inference: bool = False
    enable_auto_translation: bool = False


class AdminAiConfigTestResponse(BaseModel):
    ok: bool
    provider: str
    model: str | None = None
    latency_ms: int
    error: str | None = None


# ── Workflow(2.4.0 PR-3:定义 CRUD;run 触发 / 取消 / 调度属 PR-4)──────────


class WorkflowCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    # WorkflowSpec 形状的原始 payload:路由层显式 model_validate,
    # 以便把 R7 门禁等构造期校验错误映射为结构化错误码
    # (直接类型化会走 FastAPI 默认 422 detail,丢失 forbidden/unsupported 区分)
    spec: dict[str, Any]


class WorkflowUpdateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    spec: dict[str, Any]


class WorkflowResponse(BaseModel):
    id: str
    project_id: str
    name: str
    spec: WorkflowSpec
    enabled: bool
    schedule_cron: str | None = None
    schedule_enabled: bool = False
    created_by: str | None = None
    created_at: datetime
    updated_at: datetime


class WorkflowListItem(BaseModel):
    id: str
    project_id: str
    name: str
    node_count: int
    enabled: bool
    schedule_cron: str | None = None
    schedule_enabled: bool = False
    created_by: str | None = None
    created_at: datetime
    updated_at: datetime
