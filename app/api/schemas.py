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
from app.domain.notify import NotifyChannelName, NotifyEvent
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


class SqlGenerateRequest(BaseModel):
    """AI Copilot C1 —— 自然语言 → SQL 生成入参(设计稿 §2.7.4)。

    schema_name / table_names 是可选过滤:给了就只把这些表的结构送 AI(最省 egress)。
    """

    natural_language: str = Field(min_length=1, max_length=2000)
    schema_name: str | None = None
    table_names: list[str] = Field(default_factory=list)


class SqlGenerateResponse(BaseModel):
    """C1 生成结果。ok=True 时 sql 必有;egress_level 恒 L2(结构信息)。"""

    ok: bool
    sql: str | None = None
    explanation: str | None = None
    provider: str | None = None
    model: str | None = None
    error: str | None = None
    egress_level: int = 2
    tables_used: list[str] = Field(default_factory=list)
    truncated: bool = False


class SqlPreflightRequest(BaseModel):
    # 体检只吃 SQL 文本(C-11 advisory,纯文本启发式,不连库、不入队)。
    sql: str = Field(min_length=1)


class SqlPreflightFinding(BaseModel):
    severity: Literal["warning", "info"]
    code: str
    message: str  # 英文兜底;前端优先按 code 走 i18n。


class SqlPreflightResponse(BaseModel):
    findings: list[SqlPreflightFinding] = Field(default_factory=list)


class SlowSqlDiagnoseRequest(BaseModel):
    """AI Copilot C4 —— 慢 SQL 根因诊断入参(设计稿 §2.7.4,egress L3)。

    explain_job_id 可选:传一个**已完成的 sql_explain job**,复用其执行计划,
    避免端点阻塞在 job 队列上同步等 explain(设计裁决,见 PR body)。不传则
    仅据表结构 + 历史基线诊断(plan_included=False,优雅降级)。
    """

    sql: str = Field(min_length=1, max_length=20000)
    explain_job_id: str | None = None


class SlowSqlDiagnoseResponse(BaseModel):
    """C4 诊断结果。ok=True 时 diagnosis 必有;egress_level 恒 L3(SQL 原文遮蔽字面量)。"""

    ok: bool
    diagnosis: str | None = None
    provider: str | None = None
    model: str | None = None
    error: str | None = None
    egress_level: int = 3
    plan_included: bool = False
    tables_analyzed: list[str] = Field(default_factory=list)
    baseline_available: bool = False
    baseline_runs: int = 0
    truncated: bool = False


CompareBucket = Literal["only_source", "only_target", "diff", "same"]


# 文件源对比支持的格式(C-1;Parquet 归次版,本域暂不含)。
CompareFileFormat = Literal["csv", "excel"]


class CompareDataRef(BaseModel):
    kind: Literal["table", "sql", "file"] = "table"
    schema_name: str | None = None
    table_name: str | None = Field(default=None, min_length=1)
    sql: str | None = Field(default=None, min_length=1)
    # kind="file"(C-1 文件源对比):指向已上传件 + 格式与解析参数。
    # task 只存 upload_id(不存原始路径),回读时按 uploads 表 storage_uri 定位,
    # 天然规避路径穿越。以下参数按格式条件生效,非 file kind 忽略。
    upload_id: str | None = Field(default=None, min_length=1)
    file_format: CompareFileFormat | None = None
    sheet: str | None = None  # excel:空=第一个 sheet
    header_row: int = Field(default=1, ge=1)  # 1-indexed 表头行(表头前导言行被跳过)
    encoding: str | None = None  # csv:空=utf-8-sig(解码失败自动 GBK 回退)
    delimiter: str | None = Field(default=None, min_length=1, max_length=4)  # csv 分隔符

    @model_validator(mode="after")
    def _validate_ref(self) -> CompareDataRef:
        if self.kind == "table" and not self.table_name:
            raise ValueError("table ref requires table_name")
        if self.kind == "sql" and not self.sql:
            raise ValueError("sql ref requires sql")
        if self.kind == "file":
            if not self.upload_id:
                raise ValueError("file ref requires upload_id")
            if self.file_format is None:
                raise ValueError("file ref requires file_format")
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


def _validate_compare_side(*, side: str, kind: str, datasource_id: str | None) -> None:
    """每侧校验:DB 侧(table/sql)必须给 datasource_id;文件侧(file)不需要。

    C-1 文件源对比:file kind 的一侧 source_id/target_id 可选(照抄 #115
    数据源可选范式)。DB 侧仍强制绑数据源,否则 worker 无从取数。
    """

    if kind == "file":
        return
    if not datasource_id:
        raise ValueError(f"{side} ref kind={kind} requires {side}_id")


class CompareTaskCreateRequest(BaseModel):
    project_id: str
    name: str = Field(min_length=1, max_length=128)
    # 文件侧免 datasource:kind="file" 的一侧无需 *_id(见 _validate_compare_side)。
    source_id: str | None = None
    target_id: str | None = None
    source_ref: CompareDataRef
    target_ref: CompareDataRef
    columns: list[Column] = Field(default_factory=list)
    compare_rules: CompareRulesPayload = Field(default_factory=CompareRulesPayload)
    run_limits: CompareRunLimitsPayload = Field(default_factory=CompareRunLimitsPayload)

    @model_validator(mode="after")
    def _validate_sides(self) -> CompareTaskCreateRequest:
        _validate_compare_side(
            side="source", kind=self.source_ref.kind, datasource_id=self.source_id
        )
        _validate_compare_side(
            side="target", kind=self.target_ref.kind, datasource_id=self.target_id
        )
        return self


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
    source_id: str | None = None
    target_id: str | None = None
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


class CompareTaskRunItem(BaseModel):
    run_id: str
    job_id: str
    status: str
    created_at: datetime
    finished_at: datetime | None = None
    bucket_counts: dict[str, int] = Field(default_factory=dict)
    sampled: bool = False


class CompareTaskRunsResponse(BaseModel):
    task_id: str
    limit: int
    offset: int
    has_more: bool
    runs: list[CompareTaskRunItem] = Field(default_factory=list)


class CompareRunAbortReason(BaseModel):
    # 非成功 run 的中止原因(取 jobs.error_code;缺失归 "unknown"),按次数倒序 top N。
    reason: str
    count: int


class CompareRunsDashboardResponse(BaseModel):
    # run_index 仪表盘(C-12):项目维度近 N 天聚合。磁盘字节 / 峰值 RSS 当前未落库
    # (run_index 无该列),故本期只聚合 run 数 / 状态分布 / 成功率 / 中止 top 原因。
    project_id: str
    days: int
    total_runs: int
    status_counts: dict[str, int] = Field(default_factory=dict)
    success_rate: float  # success / total,total=0 时为 0.0
    top_abort_reasons: list[CompareRunAbortReason] = Field(default_factory=list)


class ComparePreviewRequest(BaseModel):
    datasource_id: str
    ref: CompareDataRef
    limit: int = Field(default=50, ge=1, le=200)


class ComparePreviewResponse(BaseModel):
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int
    truncated: bool


class CompareFilePreviewRequest(BaseModel):
    # ref.kind 必须为 "file"(端点会校验);复用 CompareDataRef 的 file 分支校验,
    # 前端拿到的同一个 ref 之后可直接落进 task。
    ref: CompareDataRef
    limit: int = Field(default=50, ge=1, le=200)


class CompareFilePreviewResponse(BaseModel):
    sheets: list[str] = Field(default_factory=list)  # excel:工作簿全部 sheet 名;csv 为空
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int
    truncated: bool
    detected_encoding: str | None = None  # csv:实际生效编码(GBK 回退回显);excel 为 None


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


class CompareDiffSqlSide(BaseModel):
    """UX-2 C-3:差异行定位 SQL 单侧结果(库源给 SQL,文件源标 available=False)。"""

    available: bool
    sql: str | None = None
    reason: str | None = None


class CompareDiffSqlResponse(BaseModel):
    """UX-2 C-3:按桶主键生成 SELECT ... WHERE pk IN (...) 逃生口(仅生成文本,不执行)。"""

    run_id: str
    bucket: CompareBucket
    key_columns: list[str]
    pk_count: int
    truncated: bool
    cap: int
    source: CompareDiffSqlSide
    target: CompareDiffSqlSide


class ComparePkPrecheckRequest(BaseModel):
    """UX-2 C-4:建任务前主键健康预检(单侧)——唯一性 / 空值。"""

    datasource_id: str = Field(min_length=1)
    ref: CompareDataRef
    key_columns: list[str] = Field(min_length=1)


class ComparePkPrecheckResponse(BaseModel):
    total_rows: int
    distinct_pk_rows: int
    duplicate_pk_rows: int
    null_pk_rows: int
    has_duplicates: bool
    has_nulls: bool


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


class LineageAiEnrichmentResponse(BaseModel):
    """L-7 整份血缘报告的 AI 解读结果(纯追加;标注 AI 生成)。"""

    run_id: str
    ok: bool
    interpretation: str | None = None
    provider: str | None = None
    model: str | None = None
    egress_level: int = 2
    created_at: datetime | None = None
    error: str | None = None


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


UploadPurpose = Literal["lineage_batch", "compare_source"]


class UploadResponse(BaseModel):
    upload_id: str
    project_id: str
    purpose: UploadPurpose
    filename: str
    content_type: str
    bytes: int
    created_at: datetime


class LineageBatchCreateRequest(BaseModel):
    upload_id: str
    # 数据源可选:给了则元数据感知 + 落持久化子图;不给(纯文本导入无库场景)
    # 则必须显式指定 dialect,走纯宽松表级解析,只出批量报告(不落可查询子图)。
    datasource_id: str | None = None
    dialect: str | None = Field(default=None, min_length=1, max_length=32)
    default_schema: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def _require_dialect_without_datasource(self) -> LineageBatchCreateRequest:
        if self.datasource_id is None and not self.dialect:
            raise ValueError("dialect is required when datasource_id is not provided")
        return self


class LineageBatchCreateResponse(BaseModel):
    job_id: str


class LineageBatchStatusResponse(BaseModel):
    job_id: str
    status: JobStatus
    error: str | None = None
    # success 时回读 worker 落的汇总报告(files/script_edges/skipped 等,
    # 形状由 worker _execute_lineage_batch 决定,前端按已知字段读取)
    report: dict[str, Any] | None = None


class LineageEdgeRunInfo(BaseModel):
    """边所属解析 run 的溯源信息(边抽屉用)。sql_text 在 0018 前的旧 run 为 NULL。"""

    run_id: str
    source_ref: str
    dialect: str
    created_at: datetime
    sql_text: str | None = None


class LineageEdgeDetailResponse(BaseModel):
    edge_id: str
    edge_kind: Literal["table", "column"]
    source_table: str
    source_column: str | None = None
    target_table: str
    target_column: str | None = None
    transformation: str | None = None
    transformation_subtype: str | None = None
    inferred: bool
    inference_status: str
    confidence: float
    run: LineageEdgeRunInfo | None = None


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


class LineageAiImpactRequest(BaseModel):
    """C3 加权影响分析入参(设计稿 §2.7.4)—— 焦点表/列 + 深度 + 频率窗口。"""

    focus: str = Field(min_length=1)
    max_depth: int = Field(default=3, ge=1, le=5)
    # 引用频率统计窗口(近 N 天);频率数据稀薄时优雅降级(提前交付说明)。
    window_days: int = Field(default=90, ge=1, le=365)


class LineageImpactSignal(BaseModel):
    """单个受影响资产的确定性画像(基础影响 + 频率),AI 加权的输入证据。

    频率来自血缘运行历史(引用该表的去重 sql_hash 数,近 window_days 天);
    无历史时全为 0 —— 前端据此展示证据、后端据此判定 degraded。
    """

    node: str
    table: str
    column: str | None = None
    depth: int
    ref_count: int = 0
    source_ref_count: int = 0
    last_referenced_days_ago: int | None = None


class LineageAiImpactResponse(BaseModel):
    project_id: str
    focus: str
    max_depth: int
    window_days: int
    ok: bool
    error: str | None = None
    provider: str | None = None
    model: str | None = None
    egress_level: int = 2
    impact_count: int = 0
    # 频率数据稀薄(所有受影响资产近窗口内引用为 0)→ AI 仅按图深度加权,低置信。
    degraded: bool = False
    project_owner: str | None = None
    assessment: str | None = None
    signals: list[LineageImpactSignal] = Field(default_factory=list)


class LineageExportRequest(BaseModel):
    """血缘报告基础导出请求(L-5)—— 与 subgraph 端点同参,POST body 形态。"""

    focus: str = Field(min_length=1)
    direction: LineageDirection = "downstream"
    max_depth: int = Field(default=3, ge=1, le=5)
    include_columns: bool = False


class LineageColumnTraceItem(BaseModel):
    """字段链上的一跳:node 为本跳字段,from_node 为它经由的上一跳字段。"""

    node: str
    table: str
    column: str
    from_node: str
    direction: Literal["upstream", "downstream"]
    edge_id: str
    transformation: str | None = None
    transformation_subtype: str | None = None
    inferred: bool = False
    inference_status: str = "confirmed"
    confidence: float = 1.0


class LineageColumnTraceHop(BaseModel):
    depth: int
    items: list[LineageColumnTraceItem] = Field(default_factory=list)


class LineageColumnTraceResponse(BaseModel):
    project_id: str
    focus: str
    direction: Literal["upstream", "downstream", "both"]
    max_depth: int
    truncated: bool
    hop_count: int
    hops: list[LineageColumnTraceHop] = Field(default_factory=list)


class TraceCompareRequest(BaseModel):
    """C-8 逐跳血缘对比:给定焦点字段 + 两套环境数据源 + 共享主键,沿上游血缘每跳生成
    一个 compare_run 节点,组装成 workflow(只创建不触发)。

    - ``focus``:``table.column`` 焦点字段(必须是列引用,否则 400);
    - ``source_id`` / ``target_id``:两个待对比数据源(两套环境),链上每张表在二者间对比;
    - ``key_columns``:行级对比 join 主键(链上稳定,>=1);
    - ``max_hops``:生成 compare 节点数上限(1..20;链更长时 400 too_many_hops);
    - ``dry_run=true``:只返回预览计划,不落库 workflow(``workflow_id`` 为 null)。
    """

    focus: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    key_columns: list[str] = Field(min_length=1)
    schema_name: str | None = None
    max_hops: int = Field(default=10, ge=1, le=20)
    max_depth: int = Field(default=5, ge=1, le=5)
    name: str | None = Field(default=None, max_length=128)
    dry_run: bool = False


class TraceCompareHopItem(BaseModel):
    """预览:链上一跳对比。node_id 为生成的 workflow 节点 id。"""

    node_id: str
    depth: int
    table: str
    column: str
    key_columns: list[str] = Field(default_factory=list)


class TraceCompareResponse(BaseModel):
    project_id: str
    focus: str
    source_id: str
    target_id: str
    # dry_run=true 时为 null(只预览未落库);否则为新建 workflow id。
    workflow_id: str | None = None
    workflow_name: str | None = None
    hop_count: int
    truncated: bool
    hops: list[TraceCompareHopItem] = Field(default_factory=list)


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
    sensor_enabled: bool = False
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
    sensor_enabled: bool = False
    created_by: str | None = None
    created_at: datetime
    updated_at: datetime


# ── Workflow run 通知目标配置(C-9 PR4:收明文 url → SecretStore → 只存 ref)──
#
#   ★ R2/R5:请求收整条明文 url(webhook / 企微,含 ?key=token),路由层立即
#   store_secret 换成 SecretRef,配置里只留 url_secret_ref。响应模型 **没有 url
#   字段**——绝不把明文 url / token 回显给客户端或日志。


class NotifyTargetCreateRequest(BaseModel):
    """新增通知目标。

    webhook / 企微:``url`` 是整条明文地址(含 token)→ store_secret 换 ref。
    email:``smtp_*`` 连接信息(非敏感)+ 可选 ``smtp_password``(明文,仅 store_secret
    用,★ 绝不回显 / 绝不入库,R2/R5)。
    """

    channel: NotifyChannelName
    # webhook / wecom
    url: str | None = Field(default=None, min_length=1)
    # email(SMTP)
    smtp_host: str | None = None
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_from: str | None = None
    smtp_to: list[str] = Field(default_factory=list)
    smtp_user: str | None = None
    smtp_password: str | None = Field(default=None, min_length=1)
    events: list[NotifyEvent] | None = None
    enabled: bool = True
    timeout_seconds: int = Field(default=5, ge=1, le=60)


class NotifyTargetUpdateRequest(BaseModel):
    """更新通知目标:仅传要改的字段。

    传 ``url`` 则重新 store_secret 换 ref;传 ``smtp_password`` 则重新 store_secret
    换 SMTP 密码 ref(旧 secret 删除)。
    """

    url: str | None = Field(default=None, min_length=1)
    smtp_host: str | None = None
    smtp_port: int | None = Field(default=None, ge=1, le=65535)
    smtp_from: str | None = None
    smtp_to: list[str] | None = None
    smtp_user: str | None = None
    smtp_password: str | None = Field(default=None, min_length=1)
    events: list[NotifyEvent] | None = None
    enabled: bool | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, le=60)


class NotifyTargetResponse(BaseModel):
    """通知目标视图 —— ★ 无 url / 密码字段(R5:绝不回显明文 url / token / 密码)。

    email 的连接信息(host/port/from/to/user)非敏感,回显便于配置查看。
    """

    id: str
    channel: str
    events: list[str]
    enabled: bool
    timeout_seconds: int
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_from: str | None = None
    smtp_to: list[str] | None = None
    smtp_user: str | None = None


# ── Workflow run(2.4.0 PR-4:手动触发 / 状态查询 / 取消;cron tick 属 PR-4b)──


class WorkflowRunTriggerRequest(BaseModel):
    """手动触发 run 的可选 body(C-7 PR2):运行时变量。

    并入 when_variables 快照,合并优先级 builtin < spec.variables < 触发时。
    值走与 spec 变量同一套安全字符集校验(路由层 validate_workflow_variables),
    校验错误只含变量名(R5)。无 body / 空 variables 时行为与既往一致(仅内置变量)。

    值为 ``str`` 或 ``list[str]``(C-7 PR3;list 供 ``${var | sql_in}`` / ``${var | csv}``
    展开)。
    """

    variables: dict[str, str | list[str]] = Field(default_factory=dict)


class WorkflowRunCreateResponse(BaseModel):
    # WorkflowRun 本身就是 job(ADR-0009):run_id == job_id,双字段便于前端对齐 jobs API
    run_id: str
    job_id: str
    workflow_id: str


class WorkflowRunNodeItem(BaseModel):
    node_id: str
    job_kind: str
    # 节点执行态:waiting / running / retry_wait / success / failed / skipped / cancelled
    status: str
    job_id: str | None = None
    attempts: int = 0
    error: str | None = None


class WorkflowRunStatusResponse(BaseModel):
    run_id: str
    workflow_id: str | None = None
    project_id: str
    status: JobStatus
    error: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    nodes: list[WorkflowRunNodeItem]


class WorkflowRunListItem(BaseModel):
    """Workflow run 历史列表项(轻量视图,run 级状态 + 时间戳;不含节点明细)。

    节点明细走单 run 状态端点(GET workflow-runs/{run_id});此列表只喂历史面板。
    run_id == job_id(WorkflowRun 本身即 job,ADR-0009),双字段便于前端对齐 jobs API。
    """

    run_id: str
    job_id: str
    status: JobStatus
    error: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class WorkflowRunsResponse(BaseModel):
    """某 workflow 的历史 run 分页列表(倒序;对齐 compare /runs 的 has_more 口径)。"""

    workflow_id: str
    limit: int
    offset: int
    has_more: bool
    runs: list[WorkflowRunListItem]
