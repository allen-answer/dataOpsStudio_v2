from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from secrets import token_urlsafe
from typing import Any, BinaryIO, Literal, cast
from urllib.parse import quote

import sqlglot
import structlog
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import ValidationError
from sqlalchemy import Table as SqlaTable
from sqlalchemy import (
    and_,
    delete,
    exists,
    func,
    insert,
    literal,
    or_,
    select,
    text,
    union_all,
    update,
)
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.exc import IntegrityError
from sqlglot import exp
from sqlglot.errors import ParseError
from starlette.background import BackgroundTask

from app.api.dependencies import current_user_from, request_id_from, services_from
from app.api.errors import ApiError
from app.api.routes.account import consume_recovery_code, verify_user_totp
from app.api.routes.compare_result_inputs import compare_result_input_api_error
from app.api.schemas import (
    CancelResponse,
    CompareAiAttributionResponse,
    CompareAiMapSuggestItem,
    CompareAiMapSuggestRequest,
    CompareAiMapSuggestResponse,
    CompareBucket,
    CompareDataRef,
    CompareDiffSqlResponse,
    CompareDiffSqlSide,
    CompareExportCreateRequest,
    CompareFilePreviewRequest,
    CompareFilePreviewResponse,
    CompareInferRequest,
    CompareInferResponse,
    ComparePkPrecheckRequest,
    ComparePkPrecheckResponse,
    ComparePreviewRequest,
    ComparePreviewResponse,
    CompareProjectionDetail,
    CompareResultRow,
    CompareRulesPayload,
    CompareRunAbortReason,
    CompareRunCreateResponse,
    CompareRunLimitsPayload,
    CompareRunProfileResponse,
    CompareRunResultResponse,
    CompareRunsDashboardResponse,
    CompareSuggestedTaskCreateRequest,
    CompareTableRequest,
    CompareTaskCreateRequest,
    CompareTaskResponse,
    CompareTaskRunItem,
    CompareTaskRunsResponse,
    CompareTaskSuggestionResponse,
    CompareTaskUpdateRequest,
    CompareTraceSqlAiRequest,
    CompareTraceSqlAiResponse,
    CompareTraceSqlResponse,
    CompareUpstreamEdge,
    CompareUpstreamHop,
    DatasourceCreateRequest,
    DatasourceDeleteBlockedResponse,
    DatasourceListItem,
    DatasourceReferenceItem,
    DatasourceResponse,
    DatasourceTestErrorCode,
    DatasourceTestResponse,
    DatasourceUpdateRequest,
    ExportCreateRequest,
    ExportCreateResponse,
    ExportFormat,
    JobExecutionMetrics,
    JobListItem,
    JobPageRequest,
    JobPageResponse,
    JobProgressResponse,
    JobResponse,
    JobResultResponse,
    JobStageTimings,
    LicenseStatusResponse,
    LineageAiEnrichmentResponse,
    LineageAiFallbackResult,
    LineageAiImpactRequest,
    LineageAiImpactResponse,
    LineageAnalyzeRequest,
    LineageAnalyzeResponse,
    LineageBatchCreateRequest,
    LineageBatchCreateResponse,
    LineageBatchStatusResponse,
    LineageColumnTraceHop,
    LineageColumnTraceItem,
    LineageColumnTraceResponse,
    LineageDirection,
    LineageEdgeDetailResponse,
    LineageEdgeInferenceResponse,
    LineageEdgeInferenceUpdateRequest,
    LineageEdgeRunInfo,
    LineageExportRequest,
    LineageImpactItem,
    LineageImpactResponse,
    LineageImpactSignal,
    LineageRunListItem,
    LineageRunListResponse,
    LineageSubgraphEdge,
    LineageSubgraphNode,
    LineageSubgraphResponse,
    LoginRequest,
    MetadataColumnItem,
    MetadataIndexItem,
    MetadataSchemaItem,
    MetadataTableItem,
    NotifyTargetCreateRequest,
    NotifyTargetResponse,
    NotifyTargetUpdateRequest,
    ProjectResponse,
    RowResponse,
    SlowSqlDiagnoseRequest,
    SlowSqlDiagnoseResponse,
    SqlConsoleCreateRequest,
    SqlConsoleResponse,
    SqlConsoleUpdateRequest,
    SqlExecuteRequest,
    SqlExecuteResponse,
    SqlExpandStarRequest,
    SqlExpandStarResponse,
    SqlFormatRequest,
    SqlFormatResponse,
    SqlGenerateRequest,
    SqlGenerateResponse,
    SqlGenerateValidation,
    SqlHistoryItem,
    SqlPreflightFinding,
    SqlPreflightRequest,
    SqlPreflightResponse,
    SqlTableCandidateItem,
    SqlTableCandidatesRequest,
    SqlTableCandidatesResponse,
    SqlTemplateCreateRequest,
    SqlTemplateRenderRequest,
    SqlTemplateRenderResponse,
    SqlTemplateResponse,
    SqlTemplateUpdateRequest,
    TokenResponse,
    TraceCompareHopItem,
    TraceCompareRequest,
    TraceCompareResponse,
    UploadPurpose,
    UploadResponse,
    WorkflowCreateRequest,
    WorkflowListItem,
    WorkflowResponse,
    WorkflowRunCreateResponse,
    WorkflowRunListItem,
    WorkflowRunNodeItem,
    WorkflowRunsResponse,
    WorkflowRunStatusResponse,
    WorkflowRunTriggerRequest,
    WorkflowUpdateRequest,
)
from app.api.security import create_access_token
from app.api.services import ApiServices, new_id
from app.db.models import (
    ai_configs,
    compare_tasks,
    console_statements,
    datasources,
    export_download_tokens,
    jobs,
    license_state,
    lineage_ai_enrichments,
    lineage_column_edges,
    lineage_edges,
    lineage_runs,
    metadata_caches,
    project_members,
    projects,
    result_sets,
    run_index,
    sql_consoles,
    sql_templates,
    uploads,
    users,
    workflows,
)
from app.dbclients.factory import UnsupportedDbTypeError, build_database_adapter
from app.dbclients.protocol import AdapterConnectionError, DatabaseAdapter
from app.dbclients.query_limit import (
    QueryLimitError,
    analyze_database_row_limit,
    supports_ordered_pagination,
)
from app.dbclients.sql_build import limit_clause, quote_identifier, sql_literal
from app.dbclients.sql_guard import SqlGuardError, validate_readonly_sql
from app.domain.ai import AiContext, AiOptions, ContextItem, EgressLevel, ReasoningMode
from app.domain.ai_compare_map import (
    MAX_SAMPLE_ROWS,
    aggregate_mapping_history,
    build_map_suggest_prompt,
    column_schema_payload,
    parse_map_suggestions,
    project_sample_rows,
    split_residual_columns,
)
from app.domain.ai_copilot import (
    MAX_TABLES,  # C1 与 C4 共用同一"送 AI 表数上限"(均为 12,语义一致)
    build_schema_context,
)
from app.domain.ai_slowsql import (
    MAX_PLAN_ROWS,
    BaselineStats,
    build_diagnose_prompt,
    build_plan_payload,
    build_table_stats,
    extract_table_refs,
    mask_sql,
    summarize_baseline,
)
from app.domain.ai_upstream_locator import (
    build_ai_upstream_prompt,
    build_lineage_context_payload,
    combine_confidence,
    fill_pk_placeholder,
    parse_ai_upstream_response,
    validate_ai_sql,
)
from app.domain.compare_diff_sql import build_diff_row_select
from app.domain.compare_infer import (
    CompareInferenceDraft,
    infer_compare_draft,
    infer_table_pair_suggestions,
)
from app.domain.compare_profile import profile_for_ai
from app.domain.compare_result import (
    COMPARE_BUCKETS,
    decode_compare_result_row,
    empty_bucket_counts,
)
from app.domain.compare_result_input import ActorProject, CompareResultInputDescriptor
from app.domain.compare_sql import (
    CompareSqlProjection,
    CompareSqlProjectionError,
    inspect_compare_sql,
    legacy_generated_aliases,
    normalize_compare_sql,
)
from app.domain.compare_upstream_sql import (
    CONFIDENCE_CEILING,
    MAX_UPSTREAM_DEPTH,
    UpstreamEdge,
    UpstreamHop,
    build_upstream_hops,
    dedupe_pk_rows,
    hop_risks,
    render_upstream_sql,
    upstream_header_comment,
)
from app.domain.console_session import ConsoleStatementState
from app.domain.datasource import DatasourceConnInfo, DbType, OperationPolicy
from app.domain.job import Job, JobErrorCode, JobKind, JobStatus
from app.domain.lineage import (
    LINEAGE_PARSER_VERSION,
    LineageParseRequest,
    LineageReport,
    analyze_sql_lineage,
    apply_ddl_schema,
    build_lineage_edge_rows,
    build_semantic_view,
    lineage_ddl_fingerprint,
    lineage_sql_hash,
    resolve_dialect,
    schema_from_metadata_cache_rows,
)
from app.domain.lineage.ai_enrichment import build_enrichment_profile
from app.domain.lineage.ai_fallback import (
    InferredLineageEdges,
    fallback_statements,
    parse_inferred_edges,
)
from app.domain.lineage.trace_compare import (
    TraceChainNode,
    TraceCompareBuildError,
    build_trace_compare_nodes,
    build_trace_compare_spec,
)
from app.domain.notify import NotifyTarget
from app.domain.readers import (
    CsvReader,
    ExcelReader,
    ReaderError,
    RowReader,
    list_sheets,
)
from app.domain.resource import ResourceProfile
from app.domain.result import ResultRef
from app.domain.schema import Column, Index, Row, Table
from app.domain.secret import HashedRef, SecretKind, SecretRef
from app.domain.workflow import WorkflowSpec, validate_workflow_variables
from app.domain.workflow_execution import (
    TERMINAL_NODE_STATUSES,
    WorkflowChildJob,
    WorkflowNodeExecStatus,
    plan_workflow_step,
)
from app.domain.workflow_outputs import NodeOutputValue, extract_workflow_node_outputs
from app.domain.workflow_when import builtin_when_variables, when_variables_from_payload
from app.infrastructure.compare_result_inputs import CompareResultInputError, CompareResultInputs
from app.infrastructure.jobbackend.postgres import PostgresJobBackend
from app.infrastructure.result_export import (
    ExportSizeLimitExceeded,
    export_content_type,
    export_extension,
    write_xlsx_workbook,
)
from app.infrastructure.run_index import register_compare_run_index
from app.services.ai.default_gateway import (
    AI_API_KEY_ENV,
    AiGatewayRuntimeConfig,
    build_gateway_from_runtime_config,
)
from app.services.ai.errors import (
    AiDisabledError,
    AiGatewayError,
    BudgetExceededError,
    EgressBlockedError,
    ProviderError,
)
from app.services.ai.sql_assistant import (
    TableSchema,
    build_generation_prompt,
    build_repair_prompt,
    classify_reasoning_mode,
    diagnose_empty_response,
    extract_sql,
    rank_table_candidates,
    should_repair,
    validate_generated_sql,
)
from app.services.lineage_batch_export import batch_export_sheets
from app.services.sql_preflight import run_sql_preflight
from app.services.workflow_scheduler import build_workflow_run_job

logger = structlog.get_logger(__name__)

_DATASOURCE_TEST_ERROR_CODES: tuple[DatasourceTestErrorCode, ...] = (
    "auth_failed",
    "host_unreachable",
    "timeout",
    "permission_denied",
    "unknown",
)
_METADATA_CACHE_TTL = timedelta(days=7)
_METADATA_LEVEL_SCHEMAS = "schemas"
_METADATA_LEVEL_TABLES = "tables"
_METADATA_LEVEL_COLUMNS = "columns"
_METADATA_LEVEL_INDEXES = "indexes"
_MetadataCacheKey = tuple[str, str, str]
_EXPORT_FORMATS = frozenset({"csv", "excel", "json", "sql"})
# 会话语句状态 → job 状态词汇(SQL 历史合流的显示映射,设计 §3.3)。
# 会话独有的状态一律**保守**落到不代表成功的那一格,原值另存 statement_state。
_STATEMENT_STATE_TO_JOB_STATUS: dict[ConsoleStatementState, JobStatus] = {
    ConsoleStatementState.ACCEPTED: JobStatus.PENDING,
    ConsoleStatementState.EXECUTING: JobStatus.RUNNING,
    ConsoleStatementState.STREAMING: JobStatus.RUNNING,
    ConsoleStatementState.SUCCEEDED: JobStatus.SUCCESS,
    ConsoleStatementState.FAILED: JobStatus.FAILED,
    ConsoleStatementState.CANCELLED: JobStatus.CANCELLED,
    ConsoleStatementState.TIMEOUT: JobStatus.TIMEOUT,
    ConsoleStatementState.OUTCOME_UNKNOWN: JobStatus.FAILED,
    ConsoleStatementState.SKIPPED: JobStatus.CANCELLED,
}
# 血缘导出同步生成(子图 depth<=5 数据量小),不复用 worker 的 export_limit_mb 配置;
# 50MB 上限只是防御性护栏,正常子图导出远小于此。
_LINEAGE_EXPORT_LIMIT_BYTES = 50 * 1024 * 1024
_COMPARE_BUCKET_QUERY = Query(default="diff")
# UX-2 C-3:定位 SQL 最多列多少个主键(超过则截断并在响应标 truncated,防生成超长 SQL)。
_RESULT_CELL_PREVIEW_MAX_CHARS = 4096
_DIFF_SQL_PK_CAP = 500
_COMPARE_RESULT_INPUT_QUEUE_LEASE_GRACE_SECONDS = 3600
_UPLOAD_PURPOSE_QUERY = Query()
_UPLOAD_FILENAME_QUERY = Query(min_length=1, max_length=255)
_LINEAGE_FOCUS_QUERY = Query(min_length=1)
_LINEAGE_DIRECTION_QUERY = Query(default="downstream")
_LINEAGE_DEPTH_QUERY = Query(default=3, ge=1, le=5)
_LINEAGE_INCLUDE_COLUMNS_QUERY = Query(default=False)

# WorkflowSpec 构造期校验的 code 前缀约定(app/domain/workflow.py 的 ValueError);
# 不在集合内的 pydantic 校验问题(缺字段 / 类型错)统一映射 invalid_workflow_spec。
_WORKFLOW_SPEC_ERROR_CODES = frozenset(
    {
        # WorkflowNode kind / common fields
        "forbidden_node_kind",
        "unsupported_node_kind",
        "unsupported_on_failure",
        "invalid_node_id",
        "invalid_when",
        # Intrinsic node payloads
        "invalid_branch_payload",
        "invalid_sleep_payload",
        "invalid_notify_payload",
        # Edges / DAG / routing
        "invalid_edge_when",
        "invalid_cron",
        "duplicate_node_id",
        "unknown_edge_node",
        "self_loop",
        "duplicate_edge",
        "cycle_detected",
        "invalid_branch_routes",
        "conditional_edge_requires_branch",
        "missing_failure_route",
        "invalid_failure_routes",
        "failure_edge_requires_branch",
        # Notify target references
        "duplicate_notify_target_id",
        "unknown_notify_target",
        # Upstream node output references. These are deliberately code-only
        # ValueError messages so raw expressions never enter the API response.
        "invalid_node_output_reference",
        "unknown_node_output",
        "forbidden_node_output_field",
        "non_upstream_node_output",
        # C-10 sensor 触发
        "invalid_sensor_datasource",
        # C-7 PR2 变量来源校验(spec.variables + 触发时 variables 共用错误码;
        # unsafe_variable_value 只含变量名不含取值 —— R5)
        "invalid_variable_name",
        "variable_name_collides_builtin",
        "invalid_variable_value",
        "variable_value_too_long",
        "unsafe_variable_value",
        "variable_list_too_long",
        "too_many_variables",
    }
)
_PYDANTIC_VALUE_ERROR_PREFIX = "Value error, "

_LINEAGE_TABLE_DOWNSTREAM_SQL = text(
    """
    WITH RECURSIVE walk AS (
        SELECT
            e.id AS edge_id,
            e.source_table,
            e.target_table,
            NULL::text AS source_column,
            NULL::text AS target_column,
            e.source_table AS source,
            e.target_table AS target,
            e.edge_kind,
            NULL::text AS transformation,
            NULL::text AS transformation_subtype,
            e.inferred,
            e.inference_status,
            e.confidence,
            1 AS depth,
            ARRAY[e.source_table, e.target_table]::text[] AS path
        FROM lineage_edges e
        WHERE e.project_id = :project_id
          AND e.source_table = :focus_table
          AND e.inference_status <> 'rejected'
          AND NOT EXISTS (
              SELECT 1 FROM lineage_runs r
              WHERE r.id = e.run_id AND r.superseded_at IS NOT NULL
          )
        UNION ALL
        SELECT
            e.id,
            e.source_table,
            e.target_table,
            NULL::text,
            NULL::text,
            e.source_table,
            e.target_table,
            e.edge_kind,
            NULL::text,
            NULL::text,
            e.inferred,
            e.inference_status,
            e.confidence,
            walk.depth + 1,
            walk.path || e.target_table
        FROM lineage_edges e
        JOIN walk ON e.source_table = walk.target_table
        WHERE e.project_id = :project_id
          AND e.inference_status <> 'rejected'
          AND NOT EXISTS (
              SELECT 1 FROM lineage_runs r
              WHERE r.id = e.run_id AND r.superseded_at IS NOT NULL
          )
          AND walk.depth < :query_depth
          AND NOT e.target_table = ANY(walk.path)
    )
    SELECT *, :direction AS direction FROM walk
    """
)

_LINEAGE_TABLE_UPSTREAM_SQL = text(
    """
    WITH RECURSIVE walk AS (
        SELECT
            e.id AS edge_id,
            e.source_table,
            e.target_table,
            NULL::text AS source_column,
            NULL::text AS target_column,
            e.source_table AS source,
            e.target_table AS target,
            e.edge_kind,
            NULL::text AS transformation,
            NULL::text AS transformation_subtype,
            e.inferred,
            e.inference_status,
            e.confidence,
            1 AS depth,
            ARRAY[e.target_table, e.source_table]::text[] AS path
        FROM lineage_edges e
        WHERE e.project_id = :project_id
          AND e.target_table = :focus_table
          AND e.inference_status <> 'rejected'
          AND NOT EXISTS (
              SELECT 1 FROM lineage_runs r
              WHERE r.id = e.run_id AND r.superseded_at IS NOT NULL
          )
        UNION ALL
        SELECT
            e.id,
            e.source_table,
            e.target_table,
            NULL::text,
            NULL::text,
            e.source_table,
            e.target_table,
            e.edge_kind,
            NULL::text,
            NULL::text,
            e.inferred,
            e.inference_status,
            e.confidence,
            walk.depth + 1,
            walk.path || e.source_table
        FROM lineage_edges e
        JOIN walk ON e.target_table = walk.source_table
        WHERE e.project_id = :project_id
          AND e.inference_status <> 'rejected'
          AND NOT EXISTS (
              SELECT 1 FROM lineage_runs r
              WHERE r.id = e.run_id AND r.superseded_at IS NOT NULL
          )
          AND walk.depth < :query_depth
          AND NOT e.source_table = ANY(walk.path)
    )
    SELECT *, :direction AS direction FROM walk
    """
)

_LINEAGE_COLUMN_DOWNSTREAM_SQL = text(
    """
    WITH RECURSIVE walk AS (
        SELECT
            e.id AS edge_id,
            e.source_table,
            e.target_table,
            e.source_column,
            e.target_column,
            e.source_table || '.' || e.source_column AS source,
            e.target_table || '.' || e.target_column AS target,
            'column'::text AS edge_kind,
            e.transformation,
            e.transformation_subtype,
            e.inferred,
            e.inference_status,
            e.confidence,
            1 AS depth,
            ARRAY[
                e.source_table || '.' || e.source_column,
                e.target_table || '.' || e.target_column
            ]::text[] AS path
        FROM lineage_column_edges e
        WHERE e.project_id = :project_id
          AND e.source_table = :focus_table
          AND e.source_column = :focus_column
          AND e.inference_status <> 'rejected'
          AND NOT EXISTS (
              SELECT 1 FROM lineage_runs r
              WHERE r.id = e.run_id AND r.superseded_at IS NOT NULL
          )
        UNION ALL
        SELECT
            e.id,
            e.source_table,
            e.target_table,
            e.source_column,
            e.target_column,
            e.source_table || '.' || e.source_column,
            e.target_table || '.' || e.target_column,
            'column'::text,
            e.transformation,
            e.transformation_subtype,
            e.inferred,
            e.inference_status,
            e.confidence,
            walk.depth + 1,
            walk.path || (e.target_table || '.' || e.target_column)
        FROM lineage_column_edges e
        JOIN walk
          ON e.source_table = walk.target_table
         AND e.source_column = walk.target_column
        WHERE e.project_id = :project_id
          AND e.inference_status <> 'rejected'
          AND NOT EXISTS (
              SELECT 1 FROM lineage_runs r
              WHERE r.id = e.run_id AND r.superseded_at IS NOT NULL
          )
          AND walk.depth < :query_depth
          AND NOT (e.target_table || '.' || e.target_column) = ANY(walk.path)
    )
    SELECT *, :direction AS direction FROM walk
    """
)

_LINEAGE_COLUMN_UPSTREAM_SQL = text(
    """
    WITH RECURSIVE walk AS (
        SELECT
            e.id AS edge_id,
            e.source_table,
            e.target_table,
            e.source_column,
            e.target_column,
            e.source_table || '.' || e.source_column AS source,
            e.target_table || '.' || e.target_column AS target,
            'column'::text AS edge_kind,
            e.transformation,
            e.transformation_subtype,
            e.inferred,
            e.inference_status,
            e.confidence,
            1 AS depth,
            ARRAY[
                e.target_table || '.' || e.target_column,
                e.source_table || '.' || e.source_column
            ]::text[] AS path
        FROM lineage_column_edges e
        WHERE e.project_id = :project_id
          AND e.target_table = :focus_table
          AND e.target_column = :focus_column
          AND e.inference_status <> 'rejected'
          AND NOT EXISTS (
              SELECT 1 FROM lineage_runs r
              WHERE r.id = e.run_id AND r.superseded_at IS NOT NULL
          )
        UNION ALL
        SELECT
            e.id,
            e.source_table,
            e.target_table,
            e.source_column,
            e.target_column,
            e.source_table || '.' || e.source_column,
            e.target_table || '.' || e.target_column,
            'column'::text,
            e.transformation,
            e.transformation_subtype,
            e.inferred,
            e.inference_status,
            e.confidence,
            walk.depth + 1,
            walk.path || (e.source_table || '.' || e.source_column)
        FROM lineage_column_edges e
        JOIN walk
          ON e.target_table = walk.source_table
         AND e.target_column = walk.source_column
        WHERE e.project_id = :project_id
          AND e.inference_status <> 'rejected'
          AND NOT EXISTS (
              SELECT 1 FROM lineage_runs r
              WHERE r.id = e.run_id AND r.superseded_at IS NOT NULL
          )
          AND walk.depth < :query_depth
          AND NOT (e.source_table || '.' || e.source_column) = ANY(walk.path)
    )
    SELECT *, :direction AS direction FROM walk
    """
)

router = APIRouter()


@router.post("/auth/login", response_model=TokenResponse)
def login(body: LoginRequest, request: Request) -> TokenResponse:
    services = services_from(request)
    row = _user_by_username(services, body.username)
    if row is None or not services.secret_store.verify_password(
        # ast-grep-ignore: r2-no-plaintext-password-access
        body.password,
        HashedRef(ref=str(row["password_hash"]), algorithm="bcrypt"),
    ):
        raise ApiError(401, "invalid_credentials", "Invalid username or password")
    mfa_ref = _optional_str(row["mfa_secret_ref"])
    if mfa_ref is not None:
        if body.mfa_code is None:
            raise ApiError(401, "mfa_required", "MFA verification required")
        with services.engine.begin() as conn:
            totp_ok = verify_user_totp(
                conn,
                services,
                user_id=str(row["id"]),
                mfa_ref=mfa_ref,
                code=body.mfa_code,
            )
            recovery_ok = (
                False
                if totp_ok
                else consume_recovery_code(
                    conn,
                    services,
                    user_id=str(row["id"]),
                    code=body.mfa_code,
                )
            )
        if not totp_ok and not recovery_ok:
            raise ApiError(401, "invalid_mfa_code", "Invalid MFA code")

    token = create_access_token(
        user_id=str(row["id"]),
        role=str(row["role"]),
        secret=services.jwt_secret,
        ttl_seconds=_access_token_ttl_seconds(services),
    )
    _audit_business(
        services,
        request,
        user_id=str(row["id"]),
        project_id=None,
        action="login",
        resource_type="user",
        resource_id=str(row["id"]),
        result="success",
    )
    return TokenResponse(access_token=token)


@router.get("/projects", response_model=list[ProjectResponse])
def list_projects(request: Request) -> list[ProjectResponse]:
    services = services_from(request)
    user = current_user_from(request)
    with services.engine.connect() as conn:
        rows = (
            conn.execute(
                select(projects)
                .outerjoin(
                    project_members,
                    project_members.c.project_id == projects.c.id,
                )
                .where(
                    or_(
                        projects.c.owner_user_id == user.id,
                        project_members.c.user_id == user.id,
                    )
                )
                .order_by(projects.c.created_at.asc())
            )
            .mappings()
            .all()
        )
    return [
        ProjectResponse(
            id=str(row["id"]),
            name=str(row["name"]),
            description=_optional_str(row["description"]),
        )
        for row in rows
    ]


@router.post("/datasources", response_model=DatasourceResponse, status_code=201)
def create_datasource(body: DatasourceCreateRequest, request: Request) -> DatasourceResponse:
    services = services_from(request)
    user = current_user_from(request)
    database_name = _optional_stripped(body.database)
    with services.engine.begin() as conn:
        _require_project_access(conn, body.project_id, user.id)
        secret_ref = services.secret_store.store_secret(
            # ast-grep-ignore: r2-no-plaintext-password-access
            body.password,
            SecretKind.DATASOURCE_PASSWORD,
        )
        datasource_id = new_id()
        conn.execute(
            insert(datasources).values(
                id=datasource_id,
                project_id=body.project_id,
                name=body.name,
                db_type=body.db_type.value,
                host=body.host,
                port=body.port,
                username=body.username,
                database_name=database_name,
                password_secret_ref=secret_ref.ref,
                environment=body.environment,
                capability_profile=body.extra,
                operation_policy=body.operation_policy.model_dump(),
            )
        )
    _audit_business(
        services,
        request,
        user_id=user.id,
        project_id=body.project_id,
        action="datasource_create",
        resource_type="datasource",
        resource_id=datasource_id,
        result="success",
        detail={"db_type": body.db_type.value},
    )
    return DatasourceResponse(
        id=datasource_id,
        project_id=body.project_id,
        name=body.name,
        db_type=body.db_type,
        host=body.host,
        port=body.port,
        username=body.username,
        database=database_name,
        environment=body.environment,
        extra=body.extra,
        operation_policy=body.operation_policy,
    )


@router.get("/datasources", response_model=list[DatasourceListItem])
def list_datasources(
    request: Request,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> list[DatasourceListItem]:
    services = services_from(request)
    user = current_user_from(request)
    with services.engine.connect() as conn:
        rows = (
            conn.execute(
                select(
                    datasources.c.id,
                    datasources.c.name,
                    datasources.c.db_type,
                    datasources.c.host,
                    datasources.c.port,
                    datasources.c.environment,
                    datasources.c.environment_verified,
                    datasources.c.database_name,
                    datasources.c.operation_policy,
                    datasources.c.created_at,
                )
                .select_from(_datasources_visible_to_user(user.id))
                .where(_project_access_filter(user.id))
                .order_by(datasources.c.created_at.desc(), datasources.c.id.desc())
                .limit(limit)
                .offset(offset)
            )
            .mappings()
            .all()
        )
    return [_datasource_list_item(row) for row in rows]


@router.get("/datasources/{datasource_id}", response_model=DatasourceResponse)
def get_datasource(datasource_id: str, request: Request) -> DatasourceResponse:
    row = _datasource_for_current_user(request, datasource_id)
    return _datasource_response(row)


@router.put("/datasources/{datasource_id}", response_model=DatasourceResponse)
def update_datasource(
    datasource_id: str,
    body: DatasourceUpdateRequest,
    request: Request,
) -> DatasourceResponse:
    services = services_from(request)
    user = current_user_from(request)
    old_secret_ref: str | None = None
    new_secret_ref: str | None = None
    audit_project_id: str
    with services.engine.begin() as conn:
        row = (
            conn.execute(select(datasources).where(datasources.c.id == datasource_id))
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ApiError(404, "not_found", "Datasource not found")
        audit_project_id = str(row["project_id"])
        _require_project_access(conn, str(row["project_id"]), user.id)
        if body.project_id is not None:
            _require_project_access(conn, body.project_id, user.id)
            audit_project_id = body.project_id

        values: dict[str, object] = {}
        if body.project_id is not None:
            values["project_id"] = body.project_id
        if body.name is not None:
            values["name"] = body.name
        if body.db_type is not None:
            values["db_type"] = body.db_type.value
        if body.host is not None:
            values["host"] = body.host
        if body.port is not None:
            values["port"] = body.port
        if body.username is not None:
            values["username"] = body.username
        if "database" in body.model_fields_set:
            values["database_name"] = _optional_stripped(body.database)
        if body.environment is not None:
            values["environment"] = body.environment
        if body.extra is not None:
            values["capability_profile"] = body.extra
        if body.operation_policy is not None:
            values["operation_policy"] = body.operation_policy.model_dump()
        # ast-grep-ignore: r2-no-plaintext-password-access
        if body.password:
            secret_ref = services.secret_store.store_secret(
                # ast-grep-ignore: r2-no-plaintext-password-access
                body.password,
                SecretKind.DATASOURCE_PASSWORD,
            )
            new_secret_ref = secret_ref.ref
            old_secret_ref = str(row["password_secret_ref"])
            values["password_secret_ref"] = new_secret_ref

        if values:
            values["updated_at"] = datetime.now(UTC)
            conn.execute(
                update(datasources).where(datasources.c.id == datasource_id).values(**values)
            )

    if old_secret_ref is not None and new_secret_ref is not None:
        services.secret_store.delete_secret(
            SecretRef(ref=old_secret_ref, kind=SecretKind.DATASOURCE_PASSWORD)
        )

    _audit_business(
        services,
        request,
        user_id=user.id,
        project_id=audit_project_id,
        action="datasource_update",
        resource_type="datasource",
        resource_id=datasource_id,
        result="success",
        detail={"password_changed": new_secret_ref is not None},
    )
    return _datasource_for_current_user_response(request, datasource_id)


@router.delete("/datasources/{datasource_id}", response_model=None)
def delete_datasource(datasource_id: str, request: Request) -> JSONResponse | Response:
    services = services_from(request)
    user = current_user_from(request)
    old_secret_ref: str
    project_id: str
    with services.engine.begin() as conn:
        row = (
            conn.execute(select(datasources).where(datasources.c.id == datasource_id))
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ApiError(404, "not_found", "Datasource not found")
        project_id = str(row["project_id"])
        old_secret_ref = str(row["password_secret_ref"])
        _require_project_access(conn, project_id, user.id)
        references = _datasource_job_references(conn, datasource_id)
        compare_references = _datasource_compare_task_references(conn, datasource_id)
        if references:
            payload = DatasourceDeleteBlockedResponse(
                error="datasource_in_use",
                message="Datasource is referenced by existing jobs",
                references=references,
            )
            return JSONResponse(status_code=409, content=payload.model_dump(mode="json"))
        if compare_references:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "datasource_in_use",
                    "message": "Datasource is referenced by existing compare tasks",
                    "references": compare_references,
                },
            )
        conn.execute(delete(datasources).where(datasources.c.id == datasource_id))

    services.secret_store.delete_secret(
        SecretRef(ref=old_secret_ref, kind=SecretKind.DATASOURCE_PASSWORD)
    )
    _audit_business(
        services,
        request,
        user_id=user.id,
        project_id=project_id,
        action="datasource_delete",
        resource_type="datasource",
        resource_id=datasource_id,
        result="success",
    )
    return Response(status_code=204)


@router.post("/datasources/{datasource_id}/test", response_model=DatasourceTestResponse)
def test_datasource(datasource_id: str, request: Request) -> DatasourceTestResponse:
    services = services_from(request)
    user = current_user_from(request)
    row = _datasource_for_current_user(request, datasource_id)
    started_at = time.monotonic()
    job_id = new_id()
    job = Job(
        id=job_id,
        kind=JobKind.TEST_CONNECTION,
        status=JobStatus.PENDING,
        owner_user_id=user.id,
        project_id=str(row["project_id"]),
        datasource_ids=[datasource_id],
        priority=0,
        timeout_seconds=60,
        resource_profile=ResourceProfile(),
        audit_id=new_id(),
        payload={"datasource_id": datasource_id},
    )
    services.job_backend.enqueue(job)
    terminal_job = _wait_for_job_terminal(services, job_id)
    if terminal_job and terminal_job.status is JobStatus.SUCCESS:
        metadata = terminal_job.result_ref.metadata if terminal_job.result_ref else {}
        latency_ms = _metadata_int(metadata, "latency_ms")
        return DatasourceTestResponse(
            ok=True,
            server_version=_metadata_str(metadata, "server_version"),
            latency_ms=latency_ms if latency_ms is not None else _elapsed_ms(started_at),
        )
    return DatasourceTestResponse(
        ok=False,
        error_code=_datasource_test_error_code(terminal_job),
    )


@router.post("/sql/execute", response_model=SqlExecuteResponse, status_code=202)
def execute_sql(body: SqlExecuteRequest, request: Request) -> SqlExecuteResponse:
    services = services_from(request)
    user = current_user_from(request)
    row = _datasource_for_current_user(request, body.datasource_id)
    console_id = body.console_id
    if console_id is not None:
        _console_for_current_user(request, console_id)
    try:
        sql = validate_readonly_sql(body.sql)
    except SqlGuardError as exc:
        logger.info(
            "sql guard rejected statement",
            request_id=request_id_from(request),
            reason=str(exc),
        )
        raise ApiError(400, "invalid_sql", "Only read-only SELECT/WITH SQL is allowed") from exc

    job_id = new_id()
    result_set_id = new_id()
    sql_hash = _sql_hash(sql)
    try:
        db_type = DbType(str(row["db_type"]))
        ordered_pagination = supports_ordered_pagination(sql, db_type)
        limit_analysis = analyze_database_row_limit(sql, db_type)
    except QueryLimitError as exc:
        raise ApiError(400, "invalid_sql", "SQL could not be safely parsed") from exc
    pagination_mode = "ordered_offset" if ordered_pagination else "unavailable"
    pagination_reason = (
        "fresh_read_ordered_offset"
        if pagination_mode == "ordered_offset"
        else "top_level_order_by_required"
    )
    job = Job(
        id=job_id,
        kind=JobKind.SQL_QUERY,
        status=JobStatus.PENDING,
        owner_user_id=user.id,
        project_id=str(row["project_id"]),
        datasource_ids=[body.datasource_id],
        priority=0,
        timeout_seconds=300,
        resource_profile=ResourceProfile(),
        audit_id=new_id(),
        payload={
            "datasource_id": body.datasource_id,
            "sql": sql,
            "sql_hash": sql_hash,
            "params": body.params,
            "result_set_id": result_set_id,
            "console_id": console_id,
            "page_size": body.page_size,
            "max_result_rows": body.effective_max_result_rows,
            "max_rows": body.effective_max_result_rows,
            "pagination_mode": pagination_mode,
            "pagination_reason": pagination_reason,
            "page_offset": 0,
            "db_type": str(row["db_type"]),
            "query_shape": limit_analysis.query_shape,
            "limit_pushdown": limit_analysis.limit_pushdown,
            "limit_pushdown_reason": limit_analysis.limit_pushdown_reason,
            "output_limit_applied": limit_analysis.output_limit_applied,
        },
    )
    _create_result_set_placeholder(
        services,
        result_set_id=result_set_id,
        job_id=job_id,
        console_id=console_id,
    )
    if console_id is not None:
        _evict_console_resultsets(services, console_id)
    services.job_backend.enqueue(job)
    _audit_business(
        services,
        request,
        user_id=user.id,
        project_id=str(row["project_id"]),
        action="sql_execute",
        resource_type="job",
        resource_id=job_id,
        result="accepted",
        detail={"datasource_id": body.datasource_id},
    )
    return SqlExecuteResponse(job_id=job_id, result_set_id=result_set_id)


@router.post("/sql/explain", response_model=SqlExecuteResponse, status_code=202)
def explain_sql(body: SqlExecuteRequest, request: Request) -> SqlExecuteResponse:
    services = services_from(request)
    user = current_user_from(request)
    row = _datasource_for_current_user(request, body.datasource_id)
    console_id = body.console_id
    if console_id is not None:
        _console_for_current_user(request, console_id)
    try:
        sql = validate_readonly_sql(body.sql)
    except SqlGuardError as exc:
        logger.info(
            "sql guard rejected explain statement",
            request_id=request_id_from(request),
            reason=str(exc),
        )
        raise ApiError(
            400,
            "invalid_sql",
            "Only read-only SELECT/WITH SQL can be explained",
        ) from exc
    if body.params:
        raise ApiError(400, "unsupported_sql_params", "SQL EXPLAIN does not support parameters")

    job_id = new_id()
    result_set_id = new_id()
    job = Job(
        id=job_id,
        kind=JobKind.SQL_EXPLAIN,
        status=JobStatus.PENDING,
        owner_user_id=user.id,
        project_id=str(row["project_id"]),
        datasource_ids=[body.datasource_id],
        priority=0,
        timeout_seconds=300,
        resource_profile=ResourceProfile(),
        audit_id=new_id(),
        payload={
            "datasource_id": body.datasource_id,
            "sql": sql,
            "sql_hash": _sql_hash(sql),
            "result_set_id": result_set_id,
            "console_id": console_id,
        },
    )
    _create_result_set_placeholder(
        services,
        result_set_id=result_set_id,
        job_id=job_id,
        console_id=console_id,
    )
    if console_id is not None:
        _evict_console_resultsets(services, console_id)
    services.job_backend.enqueue(job)
    _audit_business(
        services,
        request,
        user_id=user.id,
        project_id=str(row["project_id"]),
        action="sql_explain",
        resource_type="job",
        resource_id=job_id,
        result="accepted",
        detail={"datasource_id": body.datasource_id},
    )
    return SqlExecuteResponse(job_id=job_id, result_set_id=result_set_id)


@router.post("/sql/preflight", response_model=SqlPreflightResponse)
def preflight_sql(body: SqlPreflightRequest, request: Request) -> SqlPreflightResponse:
    """SQL 体检(C-11 advisory):纯文本启发式,只提示不拦截、不连库、不入队。

    行数估算走既有 `/sql/explain` 端点(库内 EXPLAIN,前端并行触发落 Plan tab);
    本端点只回文本级 finding(全表写 / SELECT * / 缺 LIMIT)。同步返回,轻量。
    """
    current_user_from(request)  # 仅要求已登录;体检不触库,无需 datasource 授权
    findings = run_sql_preflight(body.sql)
    return SqlPreflightResponse(
        findings=[
            SqlPreflightFinding(
                severity=finding.severity,
                code=finding.code,
                message=finding.message,
            )
            for finding in findings
        ]
    )


def _metadata_tables_for_schema(
    services: ApiServices,
    datasource_row: RowMapping,
    schema_name: str,
    *,
    metadata_request: _MetadataRequest | None = None,
) -> list[Table]:
    payload = _metadata_payload(
        services,
        datasource_row,
        _METADATA_LEVEL_TABLES,
        schema_name=schema_name,
        table_name="",
        refresh=False,
        load=lambda adapter: [
            MetadataTableItem(
                schema_name=item.schema_name,
                name=item.name,
                table_type=item.table_type,
            ).model_dump(mode="json")
            for item in adapter.list_tables(schema_name)
        ],
        metadata_request=metadata_request,
    )
    return [Table.model_validate(item) for item in payload]


@router.post(
    "/datasources/{datasource_id}/ai/slow-sql-diagnose",
    response_model=SlowSqlDiagnoseResponse,
)
def diagnose_slow_sql(
    datasource_id: str,
    body: SlowSqlDiagnoseRequest,
    request: Request,
) -> SlowSqlDiagnoseResponse:
    """AI Copilot C4 —— 慢 SQL 根因诊断(设计稿 §2.7.4,egress L3;提前交付)。

    出站上下文(egress 分层见 §2.7.5):
    - 遮蔽字面量后的 SQL(L3;`mask_sql` 复用 #83 血缘先例,抹掉业务过滤值)
    - EXPLAIN plan(L1;可选,复用调用方已跑完的 sql_explain job,不阻塞 job 队列)
    - 表结构 + 索引统计(L2;元数据缓存,取不到的表跳过 → 优雅降级)
    - 同一 SQL 历史 duration 基线(L1;jobs 聚合,为空则如实告知无基线)
    → AiGateway.complete → 根因排序 + 建议。未启用 → 409;gateway 失败 → 200 ok:false。
    详见 docs/design/C4-copilot-slowsql.md。
    """
    services = services_from(request)
    user = current_user_from(request)
    row = _datasource_for_current_user(request, datasource_id)
    project_id = str(row["project_id"])
    dialect = str(row["db_type"])

    def audit(result: str, detail: dict[str, object]) -> None:
        _audit_business(
            services,
            request,
            user_id=user.id,
            project_id=project_id,
            action="ai_assist_call",
            resource_type="datasource",
            resource_id=datasource_id,
            result=result,
            detail=detail,
        )

    try:
        sql = validate_readonly_sql(body.sql)
    except SqlGuardError as exc:
        logger.info(
            "sql guard rejected slow-sql diagnose statement",
            request_id=request_id_from(request),
            reason=str(exc),
        )
        raise ApiError(
            400, "invalid_sql", "Only read-only SELECT/WITH SQL can be diagnosed"
        ) from exc

    with services.engine.connect() as conn:
        ai_row = _ai_config_row_or_none(conn)
    runtime = _ai_runtime_config(services, ai_row)
    if not runtime.enabled or runtime.provider in {None, "off"}:
        audit("skipped", {"error": "ai_disabled"})
        raise ApiError(409, "ai_disabled", "AI copilot is not enabled")

    plan_payload, plan_truncated = _slow_sql_plan_for_ai(request, body.explain_job_id, project_id)
    stats_payload, tables_used, stats_truncated = _slow_sql_table_stats(services, row, sql, dialect)
    baseline = _slow_sql_baseline(services, project_id, datasource_id, _sql_hash(sql))
    baseline_summary = summarize_baseline(baseline)
    baseline_available = bool(baseline_summary.get("available"))
    has_plan = plan_payload is not None
    truncated = plan_truncated or stats_truncated

    # ★ egress:SQL=L3(已遮蔽字面量),结构=L2,plan/基线=L1。Gateway 的
    # LevelEgressChecker 是最终闸门——L3 需管理员放行 max_auto>=3,否则 EgressBlockedError。
    items = [
        ContextItem(content=mask_sql(sql), egress_level=EgressLevel.L3),
        ContextItem(content=_ai_json({"tables": stats_payload}), egress_level=EgressLevel.L2),
        ContextItem(content=_ai_json({"baseline": baseline_summary}), egress_level=EgressLevel.L1),
    ]
    if plan_payload is not None:
        items.append(
            ContextItem(content=_ai_json({"plan": plan_payload}), egress_level=EgressLevel.L1)
        )
    context = AiContext(items=items)
    prompt = build_diagnose_prompt(
        dialect=dialect,
        has_plan=has_plan,
        baseline_available=baseline_available,
    )
    try:
        response = build_gateway_from_runtime_config(runtime).complete(
            prompt,
            context,
            AiOptions(purpose="ai_slow_sql_diagnose", max_tokens=900),
        )
    except AiDisabledError:
        audit("skipped", {"error": "ai_disabled"})
        raise ApiError(409, "ai_disabled", "AI copilot is not enabled") from None
    except AiGatewayError as exc:
        error = type(exc).__name__
        audit("failed", {"error": error})
        return SlowSqlDiagnoseResponse(
            ok=False,
            error=error,
            plan_included=has_plan,
            tables_analyzed=tables_used,
            baseline_available=baseline_available,
            baseline_runs=baseline.runs,
            truncated=truncated,
        )
    audit(
        "success",
        {
            "provider": response.provider,
            "tables": len(tables_used),
            "plan_included": has_plan,
            "baseline_runs": baseline.runs,
            "truncated": truncated,
        },
    )
    return SlowSqlDiagnoseResponse(
        ok=True,
        diagnosis=response.content,
        provider=response.provider,
        model=response.model,
        egress_level=int(EgressLevel.L3),
        plan_included=has_plan,
        tables_analyzed=tables_used,
        baseline_available=baseline_available,
        baseline_runs=baseline.runs,
        truncated=truncated,
    )


def _ai_json(payload: object) -> str:
    """出站 context 的紧凑 JSON 序列化(与 Compare 归因 / C1 同一风格)。"""
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )


def _slow_sql_plan_for_ai(
    request: Request,
    explain_job_id: str | None,
    project_id: str,
) -> tuple[dict[str, object] | None, bool]:
    """取可选执行计划:复用一个已完成的 sql_explain job 的结果集(L1)。

    不传 job_id / job 非本 project / 非 sql_explain / 未成功 / 无结果集 → 返回
    (None, False)(优雅降级为无 plan,不阻塞 job 队列同步等 explain,见 PR body 裁决)。
    404(job 不存在或无访问权)由 `_job_for_current_user` 抛出,视为调用方传错 id。
    """
    if not explain_job_id:
        return None, False
    job_row = _job_for_current_user(request, explain_job_id)
    if str(job_row["kind"]) != JobKind.SQL_EXPLAIN.value:
        return None, False
    if str(job_row["status"]) != JobStatus.SUCCESS.value:
        return None, False
    if str(job_row["project_id"]) != project_id:
        return None, False
    result_set_id = _result_set_id_from_payload(job_row)
    if result_set_id is None:
        return None, False
    services = services_from(request)
    rows = services.result_store.fetch_range(result_set_id, 0, MAX_PLAN_ROWS)
    manifest = _spool_manifest_or_none(services, result_set_id)
    columns = [column.name for column in _columns_from_manifest(manifest)]
    row_values = [list(row.values) for row in rows]
    return build_plan_payload(columns, row_values)


def _slow_sql_table_stats(
    services: ApiServices,
    datasource_row: RowMapping,
    sql: str,
    dialect: str,
) -> tuple[list[dict[str, object]], list[str], bool]:
    """取 SQL 引用表的结构 + 索引统计(L2,元数据缓存;取不到的表跳过)。"""
    refs = extract_table_refs(sql, dialect=dialect)
    default_schema = _optional_str(datasource_row["database_name"])
    table_refs = [
        (schema_name or default_schema or "", table_name)
        for schema_name, table_name in refs[:MAX_TABLES]
    ]
    metadata_request = _MetadataRequest(services, datasource_row)
    try:
        metadata_request.prefetch_keys(
            [
                (cache_level, schema_name, table_name)
                for schema_name, table_name in table_refs
                for cache_level in (_METADATA_LEVEL_COLUMNS, _METADATA_LEVEL_INDEXES)
            ],
            refresh=False,
        )
    except Exception:
        # 与旧逐表 cache read 的降级等价:元数据库暂不可用时不阻断 AI 诊断。
        payload, tables_used, truncated = build_table_stats([])
        return payload, tables_used, truncated or len(refs) > MAX_TABLES
    collected: list[tuple[str, str, list[Column], list[Index]]] = []
    for schema, table_name in table_refs:
        try:
            columns = _metadata_columns_for_table(
                services,
                datasource_row,
                schema_name=schema,
                table_name=table_name,
                refresh=False,
                metadata_request=metadata_request,
            )
            indexes = _metadata_indexes_for_table(
                services,
                datasource_row,
                schema_name=schema,
                table_name=table_name,
                refresh=False,
                metadata_request=metadata_request,
            )
        except Exception:
            # 元数据取不到(缓存缺 + 探库失败 / 不支持)→ 跳过该表,诊断降级。
            continue
        collected.append((schema, table_name, columns, indexes))
    payload, tables_used, truncated = build_table_stats(collected)
    return payload, tables_used, truncated or len(refs) > MAX_TABLES


def _slow_sql_baseline(
    services: ApiServices,
    project_id: str,
    datasource_id: str,
    sql_hash: str,
) -> BaselineStats:
    """同一 SQL(sql_hash 相同)近期成功执行的 duration 聚合(L1 统计量)。

    从 jobs 聚合 count/avg/min/max/p95(EXTRACT epoch FROM finished-started)。
    无匹配 → runs=0,上层据此如实告知"无历史基线"(提前交付的优雅降级)。
    ★ 不建新表、不落 SQL 原文,只出聚合数字。
    """
    duration = func.extract("epoch", jobs.c.finished_at - jobs.c.started_at)
    stmt = select(
        func.count().label("runs"),
        func.avg(duration).label("avg_seconds"),
        func.min(duration).label("min_seconds"),
        func.max(duration).label("max_seconds"),
        func.percentile_cont(0.95).within_group(duration).label("p95_seconds"),
    ).where(
        jobs.c.kind == JobKind.SQL_QUERY.value,
        jobs.c.status == JobStatus.SUCCESS.value,
        jobs.c.project_id == project_id,
        jobs.c.datasource_ids.contains([datasource_id]),
        jobs.c.started_at.is_not(None),
        jobs.c.finished_at.is_not(None),
        text("payload->>'sql_hash' = :sql_hash"),
    )
    with services.engine.connect() as conn:
        result = conn.execute(stmt, {"sql_hash": sql_hash}).mappings().one_or_none()
    if result is None:
        return BaselineStats(runs=0)
    runs = int(result["runs"] or 0)
    if runs <= 0:
        return BaselineStats(runs=0)
    return BaselineStats(
        runs=runs,
        avg_seconds=_float_or_none(result["avg_seconds"]),
        min_seconds=_float_or_none(result["min_seconds"]),
        max_seconds=_float_or_none(result["max_seconds"]),
        p95_seconds=_float_or_none(result["p95_seconds"]),
    )


def _float_or_none(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


@router.get(
    "/datasources/{datasource_id}/metadata/schemas",
    response_model=list[MetadataSchemaItem],
)
def list_datasource_metadata_schemas(
    datasource_id: str,
    request: Request,
    refresh: bool = Query(default=False),
) -> list[MetadataSchemaItem]:
    row = _datasource_for_current_user(request, datasource_id)
    services = services_from(request)
    payload = _metadata_payload(
        services,
        row,
        _METADATA_LEVEL_SCHEMAS,
        schema_name="",
        table_name="",
        refresh=refresh,
        load=lambda adapter: [
            MetadataSchemaItem(name=item.name).model_dump(mode="json")
            for item in adapter.list_schemas()
        ],
    )
    _audit_metadata(request, services, row, "metadata_schemas_list", datasource_id, refresh)
    return [MetadataSchemaItem.model_validate(item) for item in payload]


@router.get(
    "/datasources/{datasource_id}/metadata/tables",
    response_model=list[MetadataTableItem],
)
def list_datasource_metadata_tables(
    datasource_id: str,
    request: Request,
    schema: str = Query(min_length=1),
    refresh: bool = Query(default=False),
) -> list[MetadataTableItem]:
    row = _datasource_for_current_user(request, datasource_id)
    services = services_from(request)
    payload = _metadata_payload(
        services,
        row,
        _METADATA_LEVEL_TABLES,
        schema_name=schema,
        table_name="",
        refresh=refresh,
        load=lambda adapter: [
            MetadataTableItem(
                schema_name=item.schema_name,
                name=item.name,
                table_type=item.table_type,
            ).model_dump(mode="json")
            for item in adapter.list_tables(schema)
        ],
    )
    _audit_metadata(request, services, row, "metadata_tables_list", datasource_id, refresh)
    return [MetadataTableItem.model_validate(item) for item in payload]


@router.get(
    "/datasources/{datasource_id}/metadata/columns",
    response_model=list[MetadataColumnItem],
)
def list_datasource_metadata_columns(
    datasource_id: str,
    request: Request,
    schema: str = Query(min_length=1),
    table: str = Query(min_length=1),
    refresh: bool = Query(default=False),
) -> list[MetadataColumnItem]:
    row = _datasource_for_current_user(request, datasource_id)
    services = services_from(request)
    payload = _metadata_payload(
        services,
        row,
        _METADATA_LEVEL_COLUMNS,
        schema_name=schema,
        table_name=table,
        refresh=refresh,
        load=lambda adapter: [
            _metadata_column_item(column).model_dump(mode="json")
            for column in adapter.list_columns(schema, table)
        ],
    )
    _audit_metadata(request, services, row, "metadata_columns_list", datasource_id, refresh)
    return [MetadataColumnItem.model_validate(item) for item in payload]


@router.get(
    "/datasources/{datasource_id}/metadata/indexes",
    response_model=list[MetadataIndexItem],
)
def list_datasource_metadata_indexes(
    datasource_id: str,
    request: Request,
    schema: str = Query(min_length=1),
    table: str = Query(min_length=1),
    refresh: bool = Query(default=False),
) -> list[MetadataIndexItem]:
    row = _datasource_for_current_user(request, datasource_id)
    services = services_from(request)
    payload = _metadata_payload(
        services,
        row,
        _METADATA_LEVEL_INDEXES,
        schema_name=schema,
        table_name=table,
        refresh=refresh,
        load=lambda adapter: [
            _metadata_index_item(index).model_dump(mode="json")
            for index in adapter.list_indexes(schema, table)
        ],
    )
    _audit_metadata(request, services, row, "metadata_indexes_list", datasource_id, refresh)
    return [MetadataIndexItem.model_validate(item) for item in payload]


@router.post(
    "/datasources/{datasource_id}/ai/sql-table-candidates",
    response_model=SqlTableCandidatesResponse,
)
def suggest_ai_sql_tables(
    datasource_id: str,
    body: SqlTableCandidatesRequest,
    request: Request,
) -> SqlTableCandidatesResponse:
    services = services_from(request)
    row = _datasource_for_current_user(request, datasource_id)
    metadata_request = _MetadataRequest(services, row)
    if body.schema_name is None:
        filtered = _metadata_all_tables(
            services,
            row,
            refresh=False,
            metadata_request=metadata_request,
        )
    else:
        filtered = _metadata_tables_for_schema(
            services,
            row,
            body.schema_name,
            metadata_request=metadata_request,
        )
    limited_tables = filtered[:MAX_TABLES]
    metadata_request.prefetch_keys(
        [(_METADATA_LEVEL_COLUMNS, item.schema_name, item.name) for item in limited_tables],
        refresh=False,
    )
    table_schemas = [
        TableSchema(
            schema_name=item.schema_name,
            table_name=item.name,
            columns=tuple(
                _metadata_columns_for_table(
                    services,
                    row,
                    schema_name=item.schema_name,
                    table_name=item.name,
                    refresh=False,
                    metadata_request=metadata_request,
                )
            ),
        )
        for item in limited_tables
    ]
    ranked = rank_table_candidates(
        body.natural_language,
        body.editor_sql or "",
        table_schemas,
        limit=8,
    )
    return SqlTableCandidatesResponse(
        candidates=[
            SqlTableCandidateItem(
                schema_name=item.schema_name,
                table_name=item.table_name,
                matched_by=cast(
                    list[Literal["editor_reference", "table_name", "column_name"]],
                    list(item.matched_by),
                ),
            )
            for item in ranked
        ],
        truncated=len(filtered) > MAX_TABLES,
    )


@router.post(
    "/datasources/{datasource_id}/ai/sql-generate",
    response_model=SqlGenerateResponse,
)
def generate_sql_from_nl(
    datasource_id: str,
    body: SqlGenerateRequest,
    request: Request,
) -> SqlGenerateResponse:
    """Generate a validated SQL preview from confirmed trusted metadata."""
    if not body.table_names:
        raise ApiError(422, "ai_table_scope_required", "Confirm at least one table")

    started_at = time.monotonic()
    services = services_from(request)
    user = current_user_from(request)
    row = _datasource_for_current_user(request, datasource_id)
    project_id = str(row["project_id"])

    def audit(result: str, detail: dict[str, object]) -> None:
        _audit_business(
            services,
            request,
            user_id=user.id,
            project_id=project_id,
            action="ai_copilot_run",
            resource_type="datasource",
            resource_id=datasource_id,
            result=result,
            detail=detail,
        )

    def audit_detail(
        *,
        diagnostic_code: str | None,
        stage: str,
        duration_ms: int,
        provider_duration_ms: int,
        attempts: int,
        provider: str | None,
        model: str | None,
        reasoning_mode: str,
        table_count: int,
        tokens_in: int,
        tokens_out: int,
        tokens_total: int,
        egress_level: int,
    ) -> dict[str, object]:
        return {
            "diagnostic_code": diagnostic_code,
            "stage": stage,
            "duration_ms": duration_ms,
            "provider_duration_ms": provider_duration_ms,
            "attempts": attempts,
            "provider": provider,
            "model": model,
            "reasoning_mode": reasoning_mode,
            "table_count": table_count,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "tokens_total": tokens_total,
            "egress_level": egress_level,
        }

    with services.engine.connect() as conn:
        ai_row = _ai_config_row_or_none(conn)
    runtime = _ai_runtime_config(services, ai_row)
    if not runtime.enabled or runtime.provider in {None, "off"}:
        audit("skipped", {"diagnostic_code": "ai_disabled", "stage": "failed"})
        raise ApiError(409, "ai_disabled", "AI copilot is not enabled")

    try:
        tables, more_tables = _schema_tables_for_ai(services, row, body)
    except ApiError as exc:
        audit("failed", {"diagnostic_code": exc.code, "stage": "failed"})
        raise

    schema_payload, tables_used, column_truncated = build_schema_context(tables)
    truncated = more_tables or column_truncated
    table_schemas = [
        TableSchema(schema_name=schema_name, table_name=table_name, columns=tuple(columns))
        for schema_name, table_name, columns in tables
    ]
    db_dialect = str(row["db_type"])
    parser_dialect = _sqlglot_dialect(db_dialect)
    reasoning_mode = classify_reasoning_mode(body.natural_language, table_schemas)
    first_budget = 1200 if reasoning_mode is ReasoningMode.DISABLED else 3200
    repair_budget = 1800 if reasoning_mode is ReasoningMode.DISABLED else 4800
    egress_level = EgressLevel.L3 if body.candidate_sql is not None else EgressLevel.L2
    context_items = [
        ContextItem(
            content=json.dumps(
                schema_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            # ★ trusted schema only; never includes sample rows or values.
            egress_level=EgressLevel.L2,
        )
    ]
    if body.candidate_sql is not None:
        context_items.append(ContextItem(content=body.candidate_sql, egress_level=EgressLevel.L3))
    generation_prompt = build_generation_prompt(
        body.natural_language,
        dialect=db_dialect,
        revision_instruction=body.revision_instruction,
    )
    gateway = build_gateway_from_runtime_config(runtime)

    attempts = 0
    provider_duration_ms = 0
    tokens_in = 0
    tokens_out = 0
    tokens_total = 0
    provider = runtime.provider
    model = runtime.model
    diagnostic_code = "provider_invalid_response"
    repair_candidate = body.candidate_sql or ""

    while attempts < 2:
        attempts += 1
        if attempts == 1:
            prompt = generation_prompt
            max_tokens = first_budget
            attempt_context = AiContext(items=list(context_items))
        else:
            repair_prompt = build_repair_prompt(
                diagnostic_code,
                dialect=db_dialect,
            )
            prompt = (
                repair_prompt
                if repair_candidate
                else f"{generation_prompt}\nRetry instruction: {repair_prompt}"
            )
            max_tokens = repair_budget
            repair_items = list(context_items)
            if repair_candidate:
                repair_items.append(
                    ContextItem(content=repair_candidate, egress_level=EgressLevel.L3)
                )
            attempt_context = AiContext(items=repair_items)

        provider_started_at = time.monotonic()
        try:
            response = gateway.complete(
                prompt,
                attempt_context,
                AiOptions(
                    purpose="ai_copilot_run",
                    max_tokens=max_tokens,
                    reasoning_mode=reasoning_mode,
                ),
            )
        except AiDisabledError:
            audit("skipped", {"diagnostic_code": "ai_disabled", "stage": "failed"})
            raise ApiError(409, "ai_disabled", "AI copilot is not enabled") from None
        except ProviderError as exc:
            provider_duration_ms += round((time.monotonic() - provider_started_at) * 1000)
            diagnostic_code = exc.diagnostic_code
            if should_repair(diagnostic_code, attempts=attempts):
                continue
            break
        except BudgetExceededError:
            provider_duration_ms += round((time.monotonic() - provider_started_at) * 1000)
            diagnostic_code = "ai_budget_exceeded"
            break
        except EgressBlockedError:
            provider_duration_ms += round((time.monotonic() - provider_started_at) * 1000)
            diagnostic_code = "ai_egress_blocked"
            break
        except AiGatewayError:
            provider_duration_ms += round((time.monotonic() - provider_started_at) * 1000)
            diagnostic_code = "ai_gateway_failed"
            break

        provider_duration_ms += response.duration_ms
        tokens_in += response.tokens_in
        tokens_out += response.tokens_out
        tokens_total += response.tokens_total
        provider = response.provider
        model = response.model

        diagnostic_code = diagnose_empty_response(response) or ""
        sql: str | None = None
        if not diagnostic_code:
            sql = extract_sql(response.content)
            if sql is None:
                diagnostic_code = "sql_parse_failed"
            else:
                validation = validate_generated_sql(
                    sql,
                    dialect=parser_dialect,
                    tables=table_schemas,
                )
                if validation.ok:
                    detail = audit_detail(
                        diagnostic_code=None,
                        stage="validated",
                        duration_ms=round((time.monotonic() - started_at) * 1000),
                        provider_duration_ms=provider_duration_ms,
                        attempts=attempts,
                        provider=provider,
                        model=model,
                        reasoning_mode=reasoning_mode.value,
                        table_count=len(tables_used),
                        tokens_in=tokens_in,
                        tokens_out=tokens_out,
                        tokens_total=tokens_total,
                        egress_level=int(egress_level),
                    )
                    audit("success", detail)
                    return SqlGenerateResponse(
                        ok=True,
                        sql=sql,
                        explanation=None,
                        provider=provider,
                        model=model,
                        egress_level=int(egress_level),
                        tables_used=tables_used,
                        truncated=truncated,
                        stage="validated",
                        diagnostic_code=None,
                        attempts=attempts,
                        reasoning_mode=reasoning_mode.value,
                        validation=SqlGenerateValidation(
                            readonly=validation.readonly,
                            tables=validation.tables,
                            columns=validation.columns,
                            warnings=list(validation.warnings),
                        ),
                        request_id=request_id_from(request),
                    )
                diagnostic_code = validation.diagnostic_code or "provider_invalid_response"

        repair_candidate = sql if sql is not None else response.content
        if should_repair(diagnostic_code, attempts=attempts):
            continue
        break

    detail = audit_detail(
        diagnostic_code=diagnostic_code,
        stage="failed",
        duration_ms=round((time.monotonic() - started_at) * 1000),
        provider_duration_ms=provider_duration_ms,
        attempts=attempts,
        provider=provider,
        model=model,
        reasoning_mode=reasoning_mode.value,
        table_count=len(tables_used),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        tokens_total=tokens_total,
        egress_level=int(egress_level),
    )
    audit("failed", detail)
    return SqlGenerateResponse(
        ok=False,
        error=diagnostic_code,
        stage="failed",
        diagnostic_code=diagnostic_code,
        attempts=attempts,
        reasoning_mode=reasoning_mode.value,
        request_id=request_id_from(request),
    )


def _schema_tables_for_ai(
    services: ApiServices,
    datasource_row: RowMapping,
    body: SqlGenerateRequest,
) -> tuple[list[tuple[str, str, list[Column]]], bool]:
    """取 C1 上下文所需的 (schema, table, columns) 列表 + 是否还有更多表被略过。

    过滤优先级:显式 table_names(最省)> 单 schema 表清单 > 全 schema 表清单。
    只为前 MAX_TABLES 张表取列(避免海量表时打爆缓存读),more=True 表示有表被略过。
    列取用走 metadata 缓存(#65),不额外打库;绝不读行值。
    """
    metadata_request = _MetadataRequest(services, datasource_row)
    if body.schema_name and body.table_names:
        table_refs = [(body.schema_name, name) for name in body.table_names]
    elif body.schema_name:
        table_refs = [
            (item.schema_name, item.name)
            for item in _metadata_tables_for_schema(
                services,
                datasource_row,
                body.schema_name,
                metadata_request=metadata_request,
            )
        ]
    elif body.table_names:
        all_tables = _metadata_all_tables(
            services,
            datasource_row,
            refresh=False,
            metadata_request=metadata_request,
        )
        schemas_by_name: dict[str, set[str]] = {}
        for item in all_tables:
            schemas_by_name.setdefault(item.name, set()).add(item.schema_name)
        table_refs = []
        for table_name in dict.fromkeys(body.table_names):
            schema_names = schemas_by_name.get(table_name, set())
            if len(schema_names) > 1:
                raise ApiError(
                    422,
                    "ai_table_scope_ambiguous",
                    "Confirmed table name matches multiple schemas",
                )
            if schema_names:
                table_refs.append((next(iter(schema_names)), table_name))
    else:
        table_refs = []

    more_tables = len(table_refs) > MAX_TABLES
    limited_refs = table_refs[:MAX_TABLES]
    metadata_request.prefetch_keys(
        [
            (_METADATA_LEVEL_COLUMNS, schema_name, table_name)
            for schema_name, table_name in limited_refs
        ],
        refresh=False,
    )
    result: list[tuple[str, str, list[Column]]] = []
    for schema_name, table_name in limited_refs:
        columns = _metadata_columns_for_table(
            services,
            datasource_row,
            schema_name=schema_name,
            table_name=table_name,
            refresh=False,
            metadata_request=metadata_request,
        )
        result.append((schema_name, table_name, columns))
    return result, more_tables


@router.post("/sql/format", response_model=SqlFormatResponse)
def format_sql(body: SqlFormatRequest, request: Request) -> SqlFormatResponse:
    services = services_from(request)
    user = current_user_from(request)
    try:
        formatted = sqlglot.transpile(
            body.sql,
            read=_sqlglot_dialect(body.dialect),
            write=_sqlglot_dialect(body.dialect),
            pretty=True,
        )[0]
    except (ParseError, IndexError, ValueError) as exc:
        raise ApiError(400, "invalid_sql", "SQL could not be formatted") from exc
    _audit_business(
        services,
        request,
        user_id=user.id,
        project_id=None,
        action="sql_format",
        resource_type="sql",
        resource_id=None,
        result="success",
        detail={"dialect": body.dialect, "sql_len": len(body.sql)},
    )
    return SqlFormatResponse(formatted_sql=formatted)


@router.post("/sql/expand-star", response_model=SqlExpandStarResponse)
def expand_sql_star(body: SqlExpandStarRequest, request: Request) -> SqlExpandStarResponse:
    services = services_from(request)
    user = current_user_from(request)
    row = _datasource_for_current_user(request, body.datasource_id)
    try:
        expanded, refreshed_at = _expand_star_sql(services, row, body.sql, refresh=body.refresh)
    except ParseError as exc:
        raise ApiError(400, "invalid_sql", "SQL could not be parsed") from exc
    _audit_business(
        services,
        request,
        user_id=user.id,
        project_id=str(row["project_id"]),
        action="sql_expand_star",
        resource_type="datasource",
        resource_id=body.datasource_id,
        result="success",
        detail={"sql_len": len(body.sql), "refresh": body.refresh},
    )
    return SqlExpandStarResponse(expanded_sql=expanded, metadata_refreshed_at=refreshed_at)


@router.get("/sql/consoles", response_model=list[SqlConsoleResponse])
def list_sql_consoles(request: Request) -> list[SqlConsoleResponse]:
    services = services_from(request)
    user = current_user_from(request)
    with services.engine.connect() as conn:
        rows = (
            conn.execute(
                select(sql_consoles)
                .where(sql_consoles.c.owner_user_id == user.id)
                .order_by(
                    sql_consoles.c.pinned.desc(),
                    sql_consoles.c.updated_at.desc(),
                    sql_consoles.c.created_at.desc(),
                )
            )
            .mappings()
            .all()
        )
    return [_sql_console_response(row) for row in rows]


@router.post("/sql/consoles", response_model=SqlConsoleResponse, status_code=201)
def create_sql_console(
    body: SqlConsoleCreateRequest,
    request: Request,
) -> SqlConsoleResponse:
    services = services_from(request)
    user = current_user_from(request)
    if body.datasource_id is not None:
        _datasource_for_current_user(request, body.datasource_id)
    now = datetime.now(UTC)
    console_id = new_id()
    with services.engine.begin() as conn:
        count = conn.execute(
            select(func.count())
            .select_from(sql_consoles)
            .where(sql_consoles.c.owner_user_id == user.id)
        ).scalar_one()
        if int(count) >= 20:
            raise ApiError(
                409,
                "console_limit_exceeded",
                "A user can have at most 20 SQL consoles",
            )
        conn.execute(
            insert(sql_consoles).values(
                id=console_id,
                owner_user_id=user.id,
                datasource_id=body.datasource_id,
                name=body.name,
                sql=body.sql,
                pinned=body.pinned,
                created_at=now,
                updated_at=now,
            )
        )
        row = (
            conn.execute(select(sql_consoles).where(sql_consoles.c.id == console_id))
            .mappings()
            .one()
        )
    _audit_business(
        services,
        request,
        user_id=user.id,
        project_id=None,
        action="sql_console_create",
        resource_type="sql_console",
        resource_id=console_id,
        result="success",
    )
    return _sql_console_response(row)


@router.patch("/sql/consoles/{console_id}", response_model=SqlConsoleResponse)
def update_sql_console(
    console_id: str,
    body: SqlConsoleUpdateRequest,
    request: Request,
) -> SqlConsoleResponse:
    services = services_from(request)
    user = current_user_from(request)
    _console_for_current_user(request, console_id)
    values: dict[str, object] = {"updated_at": datetime.now(UTC)}
    if "name" in body.model_fields_set:
        values["name"] = body.name
    if "datasource_id" in body.model_fields_set:
        if body.datasource_id is not None:
            _datasource_for_current_user(request, body.datasource_id)
        values["datasource_id"] = body.datasource_id
    if "sql" in body.model_fields_set:
        values["sql"] = body.sql
    if "pinned" in body.model_fields_set:
        values["pinned"] = body.pinned
    with services.engine.begin() as conn:
        conn.execute(update(sql_consoles).where(sql_consoles.c.id == console_id).values(**values))
        row = (
            conn.execute(select(sql_consoles).where(sql_consoles.c.id == console_id))
            .mappings()
            .one()
        )
    _audit_business(
        services,
        request,
        user_id=user.id,
        project_id=None,
        action="sql_console_update",
        resource_type="sql_console",
        resource_id=console_id,
        result="success",
    )
    return _sql_console_response(row)


@router.delete("/sql/consoles/{console_id}", status_code=204)
def delete_sql_console(console_id: str, request: Request) -> Response:
    services = services_from(request)
    user = current_user_from(request)
    _console_for_current_user(request, console_id)
    with services.engine.begin() as conn:
        conn.execute(delete(sql_consoles).where(sql_consoles.c.id == console_id))
    _audit_business(
        services,
        request,
        user_id=user.id,
        project_id=None,
        action="sql_console_delete",
        resource_type="sql_console",
        resource_id=console_id,
        result="success",
    )
    return Response(status_code=204)


@router.get("/sql/history", response_model=list[SqlHistoryItem])
def list_sql_history(
    request: Request,
    datasource_id: str | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    limit: int = Query(default=100, ge=1, le=100),
) -> list[SqlHistoryItem]:
    """SQL 历史。job 路径与会话语句**合流**后按 sql_hash 去重(设计 §3.3)。

    去重口径不变(仍按 sql_hash),排序仍是 created_at desc —— 合流后同一条
    SQL 只留最近一次执行,不管它当时走的是哪条路径。
    """

    services = services_from(request)
    user = current_user_from(request)
    filters = [jobs.c.owner_user_id == user.id, jobs.c.kind == JobKind.SQL_QUERY.value]
    if datasource_id is not None:
        filters.append(jobs.c.datasource_ids.any(datasource_id))
    if created_after is not None:
        filters.append(jobs.c.created_at >= created_after)
    if created_before is not None:
        filters.append(jobs.c.created_at <= created_before)
    statement_filters = [console_statements.c.owner_user_id == user.id]
    if datasource_id is not None:
        statement_filters.append(console_statements.c.datasource_id == datasource_id)
    if created_after is not None:
        statement_filters.append(console_statements.c.submitted_at >= created_after)
    if created_before is not None:
        statement_filters.append(console_statements.c.submitted_at <= created_before)
    with services.engine.connect() as conn:
        rows = (
            conn.execute(
                select(
                    jobs.c.id,
                    jobs.c.status,
                    jobs.c.datasource_ids,
                    jobs.c.payload,
                    jobs.c.created_at,
                    jobs.c.finished_at,
                )
                .where(and_(*filters))
                .order_by(jobs.c.created_at.desc(), jobs.c.id.desc())
                .limit(500)
            )
            .mappings()
            .all()
        )
        statement_rows = (
            conn.execute(
                select(
                    console_statements.c.id,
                    console_statements.c.datasource_id,
                    console_statements.c.sql_text,
                    console_statements.c.sql_hash,
                    console_statements.c.state,
                    console_statements.c.result_set_id,
                    console_statements.c.submitted_at,
                    console_statements.c.finished_at,
                )
                .where(and_(*statement_filters))
                .order_by(
                    console_statements.c.submitted_at.desc(),
                    console_statements.c.id.desc(),
                )
                .limit(500)
            )
            .mappings()
            .all()
        )
        datasource_ids = {
            item for row in rows for item in (row["datasource_ids"] or []) if isinstance(item, str)
        }
        datasource_ids |= {
            str(row["datasource_id"]) for row in statement_rows if row["datasource_id"] is not None
        }
        datasource_names = _datasource_names(conn, datasource_ids)

    candidates = [
        *_job_history_candidates(rows),
        *_statement_history_candidates(statement_rows),
    ]
    candidates.sort(key=lambda item: (_as_utc(item.created_at), item.sql_hash), reverse=True)
    out: list[SqlHistoryItem] = []
    seen: set[str] = set()
    for item in candidates:
        if item.sql_hash in seen:
            continue
        seen.add(item.sql_hash)
        name = datasource_names.get(item.datasource_id) if item.datasource_id is not None else None
        out.append(item.model_copy(update={"datasource_name": name}))
        if len(out) >= limit:
            break
    return out


def _job_history_candidates(rows: Sequence[RowMapping]) -> list[SqlHistoryItem]:
    out: list[SqlHistoryItem] = []
    for row in rows:
        payload = row["payload"] if isinstance(row["payload"], dict) else {}
        sql = payload.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            continue
        digest = payload.get("sql_hash")
        out.append(
            SqlHistoryItem(
                job_id=str(row["id"]),
                source="job",
                datasource_id=_history_datasource_id(row, payload),
                sql=sql,
                sql_hash=digest if isinstance(digest, str) else _sql_hash(sql),
                status=JobStatus(str(row["status"])),
                created_at=row["created_at"],
                finished_at=row["finished_at"],
                result_set_id=_result_set_id_from_payload(row),
            )
        )
    return out


def _statement_history_candidates(rows: Sequence[RowMapping]) -> list[SqlHistoryItem]:
    out: list[SqlHistoryItem] = []
    for row in rows:
        sql = row["sql_text"]
        if not isinstance(sql, str) or not sql.strip():
            continue
        state = ConsoleStatementState(str(row["state"]))
        out.append(
            SqlHistoryItem(
                statement_id=str(row["id"]),
                source="console_statement",
                datasource_id=_optional_str(row["datasource_id"]),
                sql=sql,
                sql_hash=str(row["sql_hash"]),
                status=_statement_state_as_job_status(state),
                statement_state=state,
                created_at=row["submitted_at"],
                finished_at=row["finished_at"],
                result_set_id=_optional_str(row["result_set_id"]),
            )
        )
    return out


def _statement_state_as_job_status(state: ConsoleStatementState) -> JobStatus:
    """语句状态 → job 状态词汇(仅为复用历史列表的状态徽标)。

    `outcome_unknown` 映射到 `failed` 是**显示口径**的保守选择(不显示成功);
    真值仍在 `statement_state`,前端要区分就读那一列 —— 绝不反过来把
    outcome_unknown 洗成 success。
    """

    return _STATEMENT_STATE_TO_JOB_STATUS[state]


@router.get("/sql/templates", response_model=list[SqlTemplateResponse])
def list_sql_templates(
    request: Request,
    category: str | None = None,
    project_id: str | None = None,
    q: str | None = None,
) -> list[SqlTemplateResponse]:
    services = services_from(request)
    current_user_from(request)
    filters = []
    if category is not None:
        filters.append(sql_templates.c.category == category)
    if project_id is not None:
        filters.append(sql_templates.c.project_id == project_id)
    if q is not None and q.strip():
        pattern = f"%{q.strip()}%"
        filters.append(
            or_(
                sql_templates.c.name.ilike(pattern),
                sql_templates.c.description.ilike(pattern),
                sql_templates.c.sql_text.ilike(pattern),
            )
        )
    with services.engine.connect() as conn:
        statement = select(sql_templates).order_by(
            sql_templates.c.category.asc(),
            sql_templates.c.name.asc(),
        )
        if filters:
            statement = statement.where(and_(*filters))
        rows = conn.execute(statement).mappings().all()
    return [_sql_template_response(row) for row in rows]


@router.post("/sql/templates", response_model=SqlTemplateResponse, status_code=201)
def create_sql_template(
    body: SqlTemplateCreateRequest,
    request: Request,
) -> SqlTemplateResponse:
    services = services_from(request)
    user = current_user_from(request)
    _require_admin(user.role)
    template_id = new_id()
    now = datetime.now(UTC)
    variables = _normalize_variables(body.variables)
    with services.engine.begin() as conn:
        if body.project_id is not None:
            _require_project_exists(conn, body.project_id)
        conn.execute(
            insert(sql_templates).values(
                id=template_id,
                name=body.name,
                description=body.description,
                sql_text=body.sql_text,
                variables=variables,
                category=body.category,
                project_id=body.project_id,
                created_by=user.id,
                created_at=now,
                updated_at=now,
            )
        )
        row = (
            conn.execute(select(sql_templates).where(sql_templates.c.id == template_id))
            .mappings()
            .one()
        )
    _audit_business(
        services,
        request,
        user_id=user.id,
        project_id=body.project_id,
        action="sql_template_create",
        resource_type="sql_template",
        resource_id=template_id,
        result="success",
    )
    return _sql_template_response(row)


@router.patch("/sql/templates/{template_id}", response_model=SqlTemplateResponse)
def update_sql_template(
    template_id: str,
    body: SqlTemplateUpdateRequest,
    request: Request,
) -> SqlTemplateResponse:
    services = services_from(request)
    user = current_user_from(request)
    _require_admin(user.role)
    _template_for_read(services, template_id)
    values: dict[str, object] = {"updated_at": datetime.now(UTC)}
    if "name" in body.model_fields_set:
        values["name"] = body.name
    if "description" in body.model_fields_set:
        values["description"] = body.description
    if "sql_text" in body.model_fields_set:
        values["sql_text"] = body.sql_text
    if "variables" in body.model_fields_set:
        values["variables"] = _normalize_variables(body.variables or [])
    if "category" in body.model_fields_set:
        values["category"] = body.category
    if "project_id" in body.model_fields_set:
        values["project_id"] = body.project_id
    with services.engine.begin() as conn:
        if body.project_id is not None:
            _require_project_exists(conn, body.project_id)
        conn.execute(
            update(sql_templates).where(sql_templates.c.id == template_id).values(**values)
        )
        row = (
            conn.execute(select(sql_templates).where(sql_templates.c.id == template_id))
            .mappings()
            .one()
        )
    _audit_business(
        services,
        request,
        user_id=user.id,
        project_id=_optional_str(row["project_id"]),
        action="sql_template_update",
        resource_type="sql_template",
        resource_id=template_id,
        result="success",
    )
    return _sql_template_response(row)


@router.delete("/sql/templates/{template_id}", status_code=204)
def delete_sql_template(template_id: str, request: Request) -> Response:
    services = services_from(request)
    user = current_user_from(request)
    _require_admin(user.role)
    row = _template_for_read(services, template_id)
    with services.engine.begin() as conn:
        conn.execute(delete(sql_templates).where(sql_templates.c.id == template_id))
    _audit_business(
        services,
        request,
        user_id=user.id,
        project_id=_optional_str(row["project_id"]),
        action="sql_template_delete",
        resource_type="sql_template",
        resource_id=template_id,
        result="success",
    )
    return Response(status_code=204)


@router.post("/sql/templates/{template_id}/render", response_model=SqlTemplateRenderResponse)
def render_sql_template(
    template_id: str,
    body: SqlTemplateRenderRequest,
    request: Request,
) -> SqlTemplateRenderResponse:
    services = services_from(request)
    current_user_from(request)
    row = _template_for_read(services, template_id)
    rendered = str(row["sql_text"])
    for name, value in body.values.items():
        rendered = rendered.replace("{{" + name + "}}", value)
    return SqlTemplateRenderResponse(sql_text=rendered)


@router.post(
    "/projects/{project_id}/compare/infer",
    response_model=CompareInferResponse,
)
def infer_project_compare_task(
    project_id: str,
    body: CompareInferRequest,
    request: Request,
    refresh: bool = Query(default=False),
) -> CompareInferResponse:
    services = services_from(request)
    user = current_user_from(request)
    source_row, target_row = _compare_datasource_rows_for_project(
        services,
        project_id=project_id,
        source_id=body.source_id,
        target_id=body.target_id,
        user_id=user.id,
    )
    draft = _compare_inference_draft(
        services,
        body,
        source_row=source_row,
        target_row=target_row,
        refresh=refresh,
    )
    return _compare_infer_response(
        project_id=project_id,
        body=body,
        draft=draft,
    )


@router.post(
    "/projects/{project_id}/compare/ai-map-suggest",
    response_model=CompareAiMapSuggestResponse,
)
def suggest_compare_column_mappings(
    project_id: str,
    body: CompareAiMapSuggestRequest,
    request: Request,
    refresh: bool = Query(default=False),
) -> CompareAiMapSuggestResponse:
    """AI Copilot C2 —— 规则推断后的残余列映射学习(设计稿 §2.7.4,egress L2 + 可选 L4)。

    提前交付(原排 2.7.0):历史为空时优雅降级,如实告知 AI "无历史",不假装有历史。
    出站三层拼装:两侧残余列 schema(L2)+ 历史映射决策(L2,聚合既有 compare 任务
    已确认 column_mappings)+ 样本(仅 include_samples 且 gateway 配置允许 L4 时,≤20
    行、仅残余列、过 gateway 脱敏钩子)。★ 只是建议,不自动应用;审计只记计数/等级,
    绝不记建议内容 / 样本值 / prompt 原文(R5)。详见 docs/design/C2-copilot-mapping.md。
    """
    services = services_from(request)
    user = current_user_from(request)
    source_row, target_row = _compare_datasource_rows_for_project(
        services,
        project_id=project_id,
        source_id=body.source_id,
        target_id=body.target_id,
        user_id=user.id,
    )

    def audit(result: str, detail: dict[str, object]) -> None:
        _audit_business(
            services,
            request,
            user_id=user.id,
            project_id=project_id,
            action="ai_assist_call",
            resource_type="compare_task",
            resource_id=None,
            result=result,
            detail=detail,
        )

    with services.engine.connect() as conn:
        ai_row = _ai_config_row_or_none(conn)
    runtime = _ai_runtime_config(services, ai_row)
    if not runtime.enabled or runtime.provider in {None, "off"}:
        audit("skipped", {"error": "ai_disabled"})
        raise ApiError(409, "ai_disabled", "AI copilot is not enabled")

    # ★ L4 样本 opt-in 闸门(设计稿 §2.7.5)。include_samples 是用户显式 opt-in;样本
    # (L4)出站还需 gateway 配置允许——本代码库里 L4 只有在 l4_requires_optin=False
    # (管理员为本形态授权 L4 出站)时才过 LevelEgressChecker(max_auto 上限仅到 L3)。
    # 二者缺一即拒发样本 → 403 说明要开什么;portable 默认 True → 永不发样本。
    if body.include_samples and runtime.l4_requires_optin:
        audit("skipped", {"error": "l4_optin_required", "include_samples": True})
        raise ApiError(
            403,
            "l4_optin_required",
            "Sending sample values (L4) requires an administrator to authorize L4 egress "
            "for this deployment (set l4_requires_optin=false).",
        )

    source_columns = _metadata_columns_for_table(
        services,
        source_row,
        schema_name=body.source_table.schema_name,
        table_name=body.source_table.table_name,
        refresh=refresh,
    )
    target_columns = _metadata_columns_for_table(
        services,
        target_row,
        schema_name=body.target_table.schema_name,
        table_name=body.target_table.table_name,
        refresh=refresh,
    )
    residual_source, residual_target, truncated = split_residual_columns(
        source_columns, target_columns, body.confirmed_mappings
    )
    residual_source_names = [column.name for column in residual_source]
    residual_target_names = [column.name for column in residual_target]

    if not residual_source or not residual_target:
        # 无残余列可映射 → 不浪费出站,直接空建议(ok=True,规则已覆盖全部列)。
        audit(
            "success",
            {"suggestion_count": 0, "reason": "no_residual_columns", "egress_level": 2},
        )
        return CompareAiMapSuggestResponse(
            ok=True,
            egress_level=int(EgressLevel.L2),
            residual_source_columns=residual_source_names,
            residual_target_columns=residual_target_names,
            truncated=truncated,
        )

    history = _compare_mapping_history(services, project_id=project_id)
    history_available = bool(history)
    context_items = [
        ContextItem(
            content=json.dumps(
                {
                    "source_columns": column_schema_payload(residual_source),
                    "target_columns": column_schema_payload(residual_target),
                    "confirmed_mappings": body.confirmed_mappings,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            egress_level=EgressLevel.L2,
        ),
        ContextItem(
            content=json.dumps(
                {"history": history},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            egress_level=EgressLevel.L2,
        ),
    ]

    samples_included = False
    sample_skip_reason: str | None = None
    if body.include_samples:
        # 已过 L4 闸门(l4_requires_optin=False);拉两侧样本,仅残余列、≤20 行。
        sample_payload, sample_skip_reason = _compare_ai_samples(
            services,
            request,
            source_row=source_row,
            source_table=body.source_table,
            residual_source_names=residual_source_names,
            target_row=target_row,
            target_table=body.target_table,
            residual_target_names=residual_target_names,
        )
        if sample_payload is not None:
            context_items.append(
                ContextItem(
                    content=json.dumps(
                        sample_payload, ensure_ascii=False, default=str, separators=(",", ":")
                    ),
                    egress_level=EgressLevel.L4,
                )
            )
            samples_included = True

    prompt = build_map_suggest_prompt(
        source_dialect=str(source_row["db_type"]),
        target_dialect=str(target_row["db_type"]),
        has_history=history_available,
        include_samples=samples_included,
    )
    max_level = max(int(item.egress_level) for item in context_items)
    try:
        response = build_gateway_from_runtime_config(runtime).complete(
            prompt,
            AiContext(items=context_items),
            AiOptions(purpose="compare_ai_map_suggest", max_tokens=900),
        )
    except AiDisabledError:
        audit("skipped", {"error": "ai_disabled"})
        raise ApiError(409, "ai_disabled", "AI copilot is not enabled") from None
    except EgressBlockedError as exc:
        # L2 schema/history 未获管理员放行(max_auto_egress_level<2)等 → 结构化 403。
        audit("failed", {"error": "egress_blocked", "level": int(exc.level)})
        raise ApiError(
            403,
            "egress_blocked",
            "AI egress policy blocked this request; an administrator must raise the allowed "
            "egress level so schema/sample context can be sent.",
        ) from None
    except AiGatewayError as exc:
        error = type(exc).__name__
        audit("failed", {"error": error})
        return CompareAiMapSuggestResponse(
            ok=False,
            error=error,
            history_available=history_available,
            history_count=len(history),
            residual_source_columns=residual_source_names,
            residual_target_columns=residual_target_names,
            truncated=truncated,
        )

    suggestions = parse_map_suggestions(
        response.content,
        valid_sources=set(residual_source_names),
        valid_targets=set(residual_target_names),
    )
    # ★ R5:审计只记计数 / 等级 / 是否含样本,绝不记建议内容 / 样本值 / prompt 原文。
    audit(
        "success",
        {
            "provider": response.provider,
            "suggestion_count": len(suggestions),
            "egress_level": max_level,
            "samples_included": samples_included,
            "history_count": len(history),
        },
    )
    return CompareAiMapSuggestResponse(
        ok=True,
        suggestions=[
            CompareAiMapSuggestItem(
                source_column=item.source_column,
                target_column=item.target_column,
                confidence=item.confidence,
                rationale=item.rationale,
            )
            for item in suggestions
        ],
        provider=response.provider,
        model=response.model,
        egress_level=max_level,
        history_available=history_available,
        history_count=len(history),
        samples_included=samples_included,
        sample_skip_reason=sample_skip_reason,
        residual_source_columns=residual_source_names,
        residual_target_columns=residual_target_names,
        truncated=truncated,
    )


@router.get(
    "/projects/{project_id}/compare/suggest-tasks",
    response_model=CompareTaskSuggestionResponse,
)
def suggest_project_compare_tasks(
    project_id: str,
    request: Request,
    source_id: str = Query(min_length=1),
    target_id: str = Query(min_length=1),
    refresh: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
) -> CompareTaskSuggestionResponse:
    services = services_from(request)
    user = current_user_from(request)
    source_row, target_row = _compare_datasource_rows_for_project(
        services,
        project_id=project_id,
        source_id=source_id,
        target_id=target_id,
        user_id=user.id,
    )
    suggestions = infer_table_pair_suggestions(
        _metadata_all_tables(services, source_row, refresh=refresh),
        _metadata_all_tables(services, target_row, refresh=refresh),
        limit=limit,
    )
    return CompareTaskSuggestionResponse(suggestions=suggestions)


@router.post(
    "/projects/{project_id}/compare/draft-task",
    response_model=CompareTaskResponse,
    status_code=201,
)
def create_project_compare_task_draft(
    project_id: str,
    body: CompareSuggestedTaskCreateRequest,
    request: Request,
    refresh: bool = Query(default=False),
) -> CompareTaskResponse:
    services = services_from(request)
    user = current_user_from(request)
    source_row, target_row = _compare_datasource_rows_for_project(
        services,
        project_id=project_id,
        source_id=body.source_id,
        target_id=body.target_id,
        user_id=user.id,
    )
    draft = _compare_inference_draft(
        services,
        body,
        source_row=source_row,
        target_row=target_row,
        refresh=refresh,
    )
    if draft.needs_manual_pk:
        raise ApiError(409, "needs_manual_pk", "Compare task draft requires manual key columns")
    task_body = CompareTaskCreateRequest(
        project_id=project_id,
        name=body.name or f"{body.source_table.table_name} -> {body.target_table.table_name}",
        source_id=body.source_id,
        target_id=body.target_id,
        source_ref=CompareDataRef(
            kind="table",
            schema_name=body.source_table.schema_name,
            table_name=body.source_table.table_name,
        ),
        target_ref=CompareDataRef(
            kind="table",
            schema_name=body.target_table.schema_name,
            table_name=body.target_table.table_name,
        ),
        columns=draft.columns,
        compare_rules=CompareRulesPayload.model_validate(
            draft.compare_rules.model_dump(mode="json")
        ),
        run_limits=body.run_limits,
    )
    return _create_compare_task_record(
        services,
        request,
        user_id=user.id,
        body=task_body,
        audit_action="compare_task_draft_create",
    )


@router.get("/compare/tasks", response_model=list[CompareTaskResponse])
def list_compare_tasks(
    request: Request,
    project_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[CompareTaskResponse]:
    services = services_from(request)
    user = current_user_from(request)
    with services.engine.connect() as conn:
        if project_id is not None:
            _require_project_access(conn, project_id, user.id)
        filters = [_project_access_filter(user.id)]
        if project_id is not None:
            filters.append(compare_tasks.c.project_id == project_id)
        rows = (
            conn.execute(
                select(compare_tasks)
                .select_from(_compare_tasks_visible_to_user(user.id))
                .where(and_(*filters))
                .order_by(compare_tasks.c.updated_at.desc(), compare_tasks.c.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            .mappings()
            .all()
        )
    return [_compare_task_response(row) for row in rows]


@router.post("/compare/tasks", response_model=CompareTaskResponse, status_code=201)
def create_compare_task(
    body: CompareTaskCreateRequest,
    request: Request,
) -> CompareTaskResponse:
    services = services_from(request)
    user = current_user_from(request)
    return _create_compare_task_record(
        services,
        request,
        user_id=user.id,
        body=body,
        audit_action="compare_task_create",
    )


def _create_compare_task_record(
    services: ApiServices,
    request: Request,
    *,
    user_id: str,
    body: CompareTaskCreateRequest,
    audit_action: str,
) -> CompareTaskResponse:
    request_columns = cast(list[object], list(body.columns))
    _reject_stale_compare_aliases(request_columns, body.source_ref)
    _reject_stale_compare_aliases(request_columns, body.target_ref)
    now = datetime.now(UTC)
    task_id = new_id()
    _validate_compare_result_snapshot_refs(
        services,
        project_id=body.project_id,
        user_id=user_id,
        refs=[body.source_ref, body.target_ref],
        retain_until=now + timedelta(minutes=5),
        columns=body.columns,
        compare_rules=body.compare_rules.model_dump(mode="python"),
    )
    with services.engine.begin() as conn:
        _validate_compare_task_datasources(
            conn,
            project_id=body.project_id,
            source_id=body.source_id,
            target_id=body.target_id,
            user_id=user_id,
        )
        _validate_compare_file_uploads(
            conn,
            project_id=body.project_id,
            refs=[body.source_ref, body.target_ref],
        )
        conn.execute(
            insert(compare_tasks).values(
                id=task_id,
                project_id=body.project_id,
                name=body.name,
                source_id=body.source_id,
                target_id=body.target_id,
                source_ref=body.source_ref.model_dump(mode="json"),
                target_ref=body.target_ref.model_dump(mode="json"),
                columns=[column.model_dump(mode="json") for column in body.columns],
                compare_rules=body.compare_rules.model_dump(mode="json"),
                run_limits=body.run_limits.model_dump(mode="json"),
                created_by=user_id,
                created_at=now,
                updated_at=now,
            )
        )
        row = (
            conn.execute(select(compare_tasks).where(compare_tasks.c.id == task_id))
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ApiError(404, "not_found", "Compare task not found")
    _audit_business(
        services,
        request,
        user_id=user_id,
        project_id=body.project_id,
        action=audit_action,
        resource_type="compare_task",
        resource_id=task_id,
        result="success",
        detail={"source_id": body.source_id, "target_id": body.target_id},
    )
    return _compare_task_response(row)


@router.get("/compare/tasks/{task_id}", response_model=CompareTaskResponse)
def get_compare_task(task_id: str, request: Request) -> CompareTaskResponse:
    return _compare_task_response(_compare_task_for_current_user(request, task_id))


@router.post(
    "/compare/tasks/{task_id}/clone",
    response_model=CompareTaskResponse,
    status_code=201,
)
def clone_compare_task(task_id: str, request: Request) -> CompareTaskResponse:
    """复制对比任务:配置全量拷贝,名字自动去重(`{name} (copy)` / `(copy N)`)。"""
    services = services_from(request)
    user = current_user_from(request)
    source_row = _compare_task_for_current_user(request, task_id)
    project_id = str(source_row["project_id"])
    now = datetime.now(UTC)
    clone_id = new_id()
    with services.engine.begin() as conn:
        name = _next_copy_name(conn, project_id=project_id, base_name=str(source_row["name"]))
        conn.execute(
            insert(compare_tasks).values(
                id=clone_id,
                project_id=project_id,
                name=name,
                source_id=source_row["source_id"],
                target_id=source_row["target_id"],
                source_ref=source_row["source_ref"],
                target_ref=source_row["target_ref"],
                columns=source_row["columns"],
                compare_rules=source_row["compare_rules"],
                run_limits=source_row["run_limits"],
                created_by=user.id,
                created_at=now,
                updated_at=now,
            )
        )
        row = (
            conn.execute(select(compare_tasks).where(compare_tasks.c.id == clone_id))
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ApiError(404, "not_found", "Compare task not found")
    _audit_business(
        services,
        request,
        user_id=user.id,
        project_id=project_id,
        action="compare_task_clone",
        resource_type="compare_task",
        resource_id=clone_id,
        result="success",
        detail={"cloned_from": task_id},
    )
    return _compare_task_response(row)


def _next_copy_name(conn: Connection, *, project_id: str, base_name: str) -> str:
    """在项目内生成不撞 uq_compare_tasks_project_name 的副本名。

    name 列宽 128:先给 " (copy N)" 后缀留够余量再拼,避免超宽被 DB 拒。
    """
    existing = {
        str(row["name"])
        for row in conn.execute(
            select(compare_tasks.c.name).where(compare_tasks.c.project_id == project_id)
        )
        .mappings()
        .all()
    }
    trimmed = base_name[:110]
    candidate = f"{trimmed} (copy)"
    suffix = 2
    while candidate in existing:
        candidate = f"{trimmed} (copy {suffix})"
        suffix += 1
    return candidate


@router.get("/compare/tasks/{task_id}/runs", response_model=CompareTaskRunsResponse)
def list_compare_task_runs(
    task_id: str,
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> CompareTaskRunsResponse:
    """任务历史 run 列表(1.x history 的 2.0 形态):倒序分页,读 run_index 一表即得。"""
    _compare_task_for_current_user(request, task_id)
    services = services_from(request)
    with services.engine.connect() as conn:
        rows = (
            conn.execute(
                select(run_index)
                .where(run_index.c.task_id == task_id)
                .order_by(run_index.c.created_at.desc(), run_index.c.run_id.desc())
                .limit(limit + 1)
                .offset(offset)
            )
            .mappings()
            .all()
        )
    has_more = len(rows) > limit
    items = [
        CompareTaskRunItem(
            run_id=str(row["run_id"]),
            job_id=str(row["job_id"]),
            status=str(row["status"]),
            created_at=row["created_at"],
            finished_at=row["finished_at"],
            bucket_counts={
                str(key): int(value)
                for key, value in dict(row["bucket_counts"] or {}).items()
                if isinstance(value, int)
            },
            sampled=row["sample_result"] is not None,
        )
        for row in rows[:limit]
    ]
    return CompareTaskRunsResponse(
        task_id=task_id,
        limit=limit,
        offset=offset,
        has_more=has_more,
        runs=items,
    )


@router.get(
    "/projects/{project_id}/compare/runs-dashboard",
    response_model=CompareRunsDashboardResponse,
)
def compare_runs_dashboard(
    project_id: str,
    request: Request,
    days: int = Query(default=30, ge=1, le=365),
) -> CompareRunsDashboardResponse:
    """run_index 仪表盘(C-12):项目维度近 N 天聚合。

    读 run_index 一表得状态分布 / 成功率;中止原因 join jobs.error_code(仅非成功
    run)。磁盘字节 / 峰值 RSS 当前未落库(run_index 无该列),本期不含 —— 见 PR
    设计裁决项。SQL 聚合下推,不拉全量 run 进内存。
    """
    services = services_from(request)
    user = current_user_from(request)
    cutoff = datetime.now(UTC) - timedelta(days=days)
    with services.engine.connect() as conn:
        _require_project_access(conn, project_id, user.id)
        status_rows = (
            conn.execute(
                select(run_index.c.status, func.count().label("n"))
                .where(
                    and_(
                        run_index.c.project_id == project_id,
                        run_index.c.created_at >= cutoff,
                    )
                )
                .group_by(run_index.c.status)
            )
            .mappings()
            .all()
        )
        # 中止原因:非成功 run join jobs 取 error_code(缺失归 unknown),top 5。
        reason_expr = func.coalesce(jobs.c.error_code, "unknown")
        reason_rows = (
            conn.execute(
                select(reason_expr.label("reason"), func.count().label("n"))
                .select_from(run_index.join(jobs, run_index.c.job_id == jobs.c.id))
                .where(
                    and_(
                        run_index.c.project_id == project_id,
                        run_index.c.created_at >= cutoff,
                        run_index.c.status.in_(("failed", "timeout", "cancelled")),
                    )
                )
                .group_by(reason_expr)
                .order_by(func.count().desc())
                .limit(5)
            )
            .mappings()
            .all()
        )
    status_counts = {str(row["status"]): int(row["n"]) for row in status_rows}
    total_runs = sum(status_counts.values())
    success = status_counts.get("success", 0)
    success_rate = round(success / total_runs, 4) if total_runs else 0.0
    top_abort_reasons = [
        CompareRunAbortReason(reason=str(row["reason"]), count=int(row["n"])) for row in reason_rows
    ]
    return CompareRunsDashboardResponse(
        project_id=project_id,
        days=days,
        total_runs=total_runs,
        status_counts=status_counts,
        success_rate=success_rate,
        top_abort_reasons=top_abort_reasons,
    )


@router.post(
    "/projects/{project_id}/compare/preview",
    response_model=ComparePreviewResponse,
)
def preview_compare_data(
    project_id: str,
    body: ComparePreviewRequest,
    request: Request,
) -> ComparePreviewResponse:
    """不落任务先看数据(1.x preview 的 2.0 形态):表引用或只读 SQL 预览前 N 行。

    与元数据探测同通道:API 进程直连 adapter,探测级超时;上限 200 行 +
    SQL 侧 limit 双保险。kind=sql 必须过 sql_guard 只读校验;kind=table
    标识符过白名单正则后按方言引用 —— 两条路都无拼接注入面。
    """
    services = services_from(request)
    user = current_user_from(request)
    with services.engine.connect() as conn:
        _require_project_access(conn, project_id, user.id)
        ds_row = (
            conn.execute(
                select(datasources).where(
                    and_(
                        datasources.c.id == body.datasource_id,
                        datasources.c.project_id == project_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
    if ds_row is None:
        raise ApiError(404, "not_found", "Datasource not found")
    policy = dict(ds_row["operation_policy"] or {})
    if not bool(policy.get("allow_select", False)):
        raise ApiError(403, "select_not_allowed", "Datasource operation policy denies SELECT")
    db_type = DbType(str(ds_row["db_type"]))
    projection_details: tuple[CompareSqlProjection, ...] = ()
    if body.ref.kind == "sql":
        try:
            inner_sql = validate_readonly_sql(body.ref.sql or "")
        except SqlGuardError as exc:
            raise ApiError(400, "invalid_sql", "Only read-only SELECT/WITH SQL is allowed") from exc
        try:
            plan = normalize_compare_sql(inner_sql, db_type)
        except CompareSqlProjectionError:
            normalized_sql = inner_sql
        else:
            normalized_sql = plan.sql
            projection_details = plan.projections
        # 与 worker _compare_table_expression 同形:子查询别名不带引用
        source_expr = f"({normalized_sql}) DATAOPS_PREVIEW"
    else:
        identifier = (
            f"{body.ref.schema_name}.{body.ref.table_name}"
            if body.ref.schema_name
            else str(body.ref.table_name)
        )
        try:
            source_expr = quote_identifier(db_type, identifier)
        except ValueError as exc:
            raise ApiError(400, "invalid_identifier", "Invalid schema or table name") from exc
    # SQL 侧取 limit+1 行:多出的一行只用来判断 truncated,不返回
    sql = f"SELECT * FROM {source_expr}{limit_clause(db_type, body.limit + 1)}"
    columns: list[str] = []
    adapter = build_database_adapter(
        _datasource_conn_info(ds_row),
        services.secret_store,
        column_sink=lambda cols: columns.extend(column.name for column in cols),
        connect_timeout_seconds=services.metadata_probe_timeout_seconds,
        statement_timeout_seconds=services.metadata_probe_timeout_seconds,
    )
    fetched: list[list[Any]] = []
    try:
        for data_row in adapter.execute_select(sql, {}):
            fetched.append([_preview_value(value) for value in data_row.values])
            if len(fetched) > body.limit:
                break
    except UnsupportedDbTypeError as exc:
        raise ApiError(400, "unsupported_db_type", "Datasource type is not supported") from exc
    except AdapterConnectionError as exc:
        raise ApiError(502, "datasource_unreachable", "Datasource connection failed") from exc
    except Exception as exc:
        # R5:driver 原始错误可能含 SQL 片段,不透传给客户端,只记类型
        logger.info(
            "compare preview query failed",
            request_id=request_id_from(request),
            datasource_id=body.datasource_id,
            error_type=type(exc).__name__,
        )
        raise ApiError(400, "preview_failed", "Preview query failed") from exc
    truncated = len(fetched) > body.limit
    rows_out = fetched[: body.limit]
    explicit_numeric_names = {
        item.name for item in projection_details if not item.generated and item.name.isdigit()
    }
    if any(name.isdigit() and name not in explicit_numeric_names for name in columns):
        raise ApiError(
            400,
            "compare_sql_alias_required",
            "Computed SQL columns require explicit aliases",
        )
    _audit_business(
        services,
        request,
        user_id=user.id,
        project_id=project_id,
        action="compare_preview",
        resource_type="datasource",
        resource_id=body.datasource_id,
        result="success",
        detail={"kind": body.ref.kind, "limit": body.limit, "row_count": len(rows_out)},
    )
    return ComparePreviewResponse(
        columns=columns,
        column_details=_compare_projection_details(projection_details),
        rows=rows_out,
        row_count=len(rows_out),
        truncated=truncated,
    )


def _preview_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _precheck_int(value: object) -> int:
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(str(value))  # Decimal / driver 特有数值类型走字符串兜底
    except (TypeError, ValueError):
        return 0


@router.post(
    "/projects/{project_id}/compare/pk-precheck",
    response_model=ComparePkPrecheckResponse,
)
def precheck_compare_primary_key(
    project_id: str,
    body: ComparePkPrecheckRequest,
    request: Request,
) -> ComparePkPrecheckResponse:
    """UX-2 C-4:建任务前主键健康预检——COUNT(*) / DISTINCT / NULL,提前发现噪声源。

    与 preview 同通道(API 直连 adapter,探测级超时,只读)。文件源不支持(前端不该调用)。
    两条轻量聚合查询:总行数+主键空值 / 主键去重行数;重复 = 总 - 去重(含 null 组的粗略近似,
    只为给用户一个"主键不唯一"的黄条警告,不追求精确)。
    """
    services = services_from(request)
    user = current_user_from(request)
    with services.engine.connect() as conn:
        _require_project_access(conn, project_id, user.id)
        ds_row = (
            conn.execute(
                select(datasources).where(
                    and_(
                        datasources.c.id == body.datasource_id,
                        datasources.c.project_id == project_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
    if ds_row is None:
        raise ApiError(404, "not_found", "Datasource not found")
    policy = dict(ds_row["operation_policy"] or {})
    if not bool(policy.get("allow_select", False)):
        raise ApiError(403, "select_not_allowed", "Datasource operation policy denies SELECT")
    db_type = DbType(str(ds_row["db_type"]))
    if body.ref.kind == "file":
        raise ApiError(
            400,
            "precheck_unsupported_file",
            "Primary key precheck is not available for file sources",
        )
    if body.ref.kind == "sql":
        try:
            inner_sql = validate_readonly_sql(body.ref.sql or "")
        except SqlGuardError as exc:
            raise ApiError(400, "invalid_sql", "Only read-only SELECT/WITH SQL is allowed") from exc
        try:
            inner_sql = normalize_compare_sql(inner_sql, db_type).sql
        except CompareSqlProjectionError:
            pass
        source_expr = f"({inner_sql}) DATAOPS_PRECHECK"
    else:
        identifier = (
            f"{body.ref.schema_name}.{body.ref.table_name}"
            if body.ref.schema_name
            else str(body.ref.table_name)
        )
        try:
            source_expr = quote_identifier(db_type, identifier)
        except ValueError as exc:
            raise ApiError(400, "invalid_identifier", "Invalid schema or table name") from exc
    try:
        quoted_keys = [quote_identifier(db_type, key) for key in body.key_columns]
    except ValueError as exc:
        raise ApiError(400, "invalid_identifier", "Invalid key column name") from exc

    null_cond = " OR ".join(f"{key} IS NULL" for key in quoted_keys)
    count_sql = (
        "SELECT COUNT(*) AS total_rows, "
        f"COALESCE(SUM(CASE WHEN {null_cond} THEN 1 ELSE 0 END), 0) AS null_pk_rows "
        f"FROM {source_expr}"
    )
    distinct_sql = (
        "SELECT COUNT(*) AS distinct_pk_rows FROM "
        f"(SELECT DISTINCT {', '.join(quoted_keys)} FROM {source_expr}) DATAOPS_PK"
    )

    adapter = build_database_adapter(
        _datasource_conn_info(ds_row),
        services.secret_store,
        connect_timeout_seconds=services.metadata_probe_timeout_seconds,
        statement_timeout_seconds=services.metadata_probe_timeout_seconds,
    )

    def _run_scalar_row(sql: str) -> list[object]:
        for data_row in adapter.execute_select(sql, {}):
            return list(data_row.values)
        return []

    try:
        count_row = _run_scalar_row(count_sql)
        distinct_row = _run_scalar_row(distinct_sql)
    except UnsupportedDbTypeError as exc:
        raise ApiError(400, "unsupported_db_type", "Datasource type is not supported") from exc
    except AdapterConnectionError as exc:
        raise ApiError(502, "datasource_unreachable", "Datasource connection failed") from exc
    except Exception as exc:
        # R5:driver 原始错误可能含 SQL 片段,不透传,只记类型
        logger.info(
            "compare pk precheck query failed",
            request_id=request_id_from(request),
            datasource_id=body.datasource_id,
            error_type=type(exc).__name__,
        )
        raise ApiError(400, "precheck_failed", "Primary key precheck query failed") from exc

    total_rows = _precheck_int(count_row[0]) if count_row else 0
    null_pk_rows = _precheck_int(count_row[1]) if len(count_row) > 1 else 0
    distinct_pk_rows = _precheck_int(distinct_row[0]) if distinct_row else 0
    duplicate_pk_rows = max(total_rows - distinct_pk_rows, 0)

    _audit_business(
        services,
        request,
        user_id=user.id,
        project_id=project_id,
        action="compare_pk_precheck",
        resource_type="datasource",
        resource_id=body.datasource_id,
        result="success",
        detail={"kind": body.ref.kind, "key_columns": len(body.key_columns)},
    )
    return ComparePkPrecheckResponse(
        total_rows=total_rows,
        distinct_pk_rows=distinct_pk_rows,
        duplicate_pk_rows=duplicate_pk_rows,
        null_pk_rows=null_pk_rows,
        has_duplicates=duplicate_pk_rows > 0,
        has_nulls=null_pk_rows > 0,
    )


@router.post(
    "/projects/{project_id}/compare/file-preview",
    response_model=CompareFilePreviewResponse,
)
def preview_compare_file(
    project_id: str,
    body: CompareFilePreviewRequest,
    request: Request,
) -> CompareFilePreviewResponse:
    """文件源上传后预览(C-1 PR2):按 upload_id + 格式参数读 sheet / 列名 / 前 N 行,
    供前端"选 sheet / 表头 / 编码 + 列映射"用。不落任务、不碰 DB —— 纯 domain reader。

    上传归属校验:upload 必须属本项目且 purpose=compare_source。文件本体经
    result_store.open_download 落临时文件(openpyxl 需 seek + 复开工作簿),
    读完即删。reader 原始错误(解码 / 格式失败)可能含文件内容,R5 下不透传,
    只回通用 preview_failed + 记错误类型。
    """
    if body.ref.kind != "file":
        raise ApiError(400, "invalid_ref_kind", "file-preview requires ref.kind=file")
    services = services_from(request)
    user = current_user_from(request)
    with services.engine.connect() as conn:
        _require_project_access(conn, project_id, user.id)
        upload_row = (
            conn.execute(
                select(uploads).where(
                    and_(
                        uploads.c.id == body.ref.upload_id,
                        uploads.c.project_id == project_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
    if upload_row is None:
        raise ApiError(404, "not_found", "Upload not found")
    if str(upload_row["purpose"]) != "compare_source":
        raise ApiError(400, "invalid_upload_purpose", "Upload purpose must be compare_source")

    storage_uri = str(upload_row["storage_uri"])
    # 保留原始扩展名:openpyxl 靠扩展名判格式,无 .xlsx/.xlsm 后缀会直接拒读。
    suffix = os.path.splitext(str(upload_row["filename"]))[1]
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp_path = tmp.name
        with services.result_store.open_download(
            ResultRef(backend="local_fs", uri=storage_uri)
        ) as stream:
            shutil.copyfileobj(stream, tmp)
    try:
        result = _read_file_preview(body.ref, tmp_path, body.limit)
    except ReaderError as exc:
        logger.info(
            "compare file preview failed",
            request_id=request_id_from(request),
            upload_id=body.ref.upload_id,
            error_type=type(exc).__name__,
        )
        raise ApiError(400, "preview_failed", "File preview failed") from exc
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    _audit_business(
        services,
        request,
        user_id=user.id,
        project_id=project_id,
        action="compare_file_preview",
        resource_type="upload",
        resource_id=str(body.ref.upload_id),
        result="success",
        detail={
            "file_format": body.ref.file_format,
            "limit": body.limit,
            "row_count": result.row_count,
        },
    )
    return result


def _read_file_preview(ref: CompareDataRef, path: str, limit: int) -> CompareFilePreviewResponse:
    """按 ref.file_format 装配 reader,取 sheet / 列 / 前 limit 行(多探一行判 truncated)。"""
    sheets: list[str] = []
    detected_encoding: str | None = None
    reader: RowReader
    if ref.file_format == "excel":
        sheets = list_sheets(path)
        reader = ExcelReader(path, sheet=ref.sheet, header_row=ref.header_row)
    else:  # csv(schema 已把 file_format 限定为 csv|excel)
        csv_reader = CsvReader(
            path,
            encoding=ref.encoding or "utf-8-sig",
            delimiter=ref.delimiter or ",",
            header_row=ref.header_row,
        )
        detected_encoding = csv_reader.resolved_encoding
        reader = csv_reader
    columns = reader.columns()
    rows: list[list[Any]] = []
    truncated = False
    for index, row in enumerate(reader.iter_rows()):
        if index >= limit:
            truncated = True
            break
        rows.append([_preview_value(row.get(column)) for column in columns])
    return CompareFilePreviewResponse(
        sheets=sheets,
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
        detected_encoding=detected_encoding,
    )


@router.post(
    "/projects/{project_id}/uploads",
    response_model=UploadResponse,
    status_code=201,
)
async def create_upload(
    project_id: str,
    request: Request,
    purpose: UploadPurpose = _UPLOAD_PURPOSE_QUERY,
    filename: str = _UPLOAD_FILENAME_QUERY,
) -> UploadResponse:
    """通用文件上传(血缘批量 ZIP / 对比文件源共用地基)。

    原始 body 上传(octet-stream + query 传 filename/purpose),不引 multipart
    依赖 —— 前后端都是自己的,fetch(blob) 即可。流式落盘边写边限额
    (upload_max_mb,超限 413 不落盘);文件本体进 result_store uploads/
    目录(upload_ttl_hours GC),uploads 表登记所有权与元信息。
    """
    services = services_from(request)
    user = current_user_from(request)
    with services.engine.connect() as conn:
        _require_project_access(conn, project_id, user.id)
    limit_bytes = services.upload_max_mb * 1024 * 1024
    upload_id = new_id()
    total_bytes = 0
    with tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024, mode="w+b") as buffer:
        async for chunk in request.stream():
            total_bytes += len(chunk)
            if total_bytes > limit_bytes:
                raise ApiError(
                    413,
                    "upload_too_large",
                    f"Upload exceeds {services.upload_max_mb} MB limit",
                )
            buffer.write(chunk)
        if total_bytes == 0:
            raise ApiError(400, "empty_upload", "Upload body is empty")
        buffer.seek(0)
        ref = services.result_store.put_upload_artifact(upload_id, filename, cast(BinaryIO, buffer))
    content_type = (request.headers.get("content-type") or "application/octet-stream")[:128]
    now = datetime.now(UTC)
    with services.engine.begin() as conn:
        conn.execute(
            insert(uploads).values(
                id=upload_id,
                project_id=project_id,
                owner_user_id=user.id,
                purpose=purpose,
                filename=filename,
                content_type=content_type,
                bytes=total_bytes,
                storage_uri=ref.uri,
                created_at=now,
            )
        )
    _audit_business(
        services,
        request,
        user_id=user.id,
        project_id=project_id,
        action="upload_create",
        resource_type="upload",
        resource_id=upload_id,
        result="success",
        detail={"purpose": purpose, "filename": filename, "bytes": total_bytes},
    )
    return UploadResponse(
        upload_id=upload_id,
        project_id=project_id,
        purpose=purpose,
        filename=filename,
        content_type=content_type,
        bytes=total_bytes,
        created_at=now,
    )


@router.patch("/compare/tasks/{task_id}", response_model=CompareTaskResponse)
def update_compare_task(
    task_id: str,
    body: CompareTaskUpdateRequest,
    request: Request,
) -> CompareTaskResponse:
    services = services_from(request)
    user = current_user_from(request)
    with services.engine.begin() as conn:
        row = (
            conn.execute(select(compare_tasks).where(compare_tasks.c.id == task_id))
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ApiError(404, "not_found", "Compare task not found")
        project_id = str(row["project_id"])
        _require_project_access(conn, project_id, user.id)
        fields_set = body.model_fields_set
        current_source_id = _optional_str(row["source_id"])
        current_target_id = _optional_str(row["target_id"])
        effective_source_ref = body.source_ref or CompareDataRef.model_validate(row["source_ref"])
        effective_target_ref = body.target_ref or CompareDataRef.model_validate(row["target_ref"])
        source_id = body.source_id if "source_id" in fields_set else current_source_id
        target_id = body.target_id if "target_id" in fields_set else current_target_id
        if body.source_ref is not None and body.source_ref.kind == "result_snapshot":
            source_id = None
        if body.target_ref is not None and body.target_ref.kind == "result_snapshot":
            target_id = None
        _validate_compare_effective_side("source", effective_source_ref, source_id)
        _validate_compare_effective_side("target", effective_target_ref, target_id)
        _validate_compare_task_datasources(
            conn,
            project_id=project_id,
            source_id=source_id,
            target_id=target_id,
            user_id=user.id,
        )
        _validate_compare_file_uploads(
            conn,
            project_id=project_id,
            refs=[effective_source_ref, effective_target_ref],
        )
        effective_columns = cast(
            list[object],
            list(body.columns) if body.columns is not None else list(row["columns"] or []),
        )
        effective_rules = body.compare_rules or CompareRulesPayload.model_validate(
            row["compare_rules"] or {}
        )
        _validate_compare_result_snapshot_refs(
            services,
            project_id=project_id,
            user_id=user.id,
            refs=[effective_source_ref, effective_target_ref],
            retain_until=datetime.now(UTC) + timedelta(minutes=5),
            columns=effective_columns,
            compare_rules=effective_rules.model_dump(mode="python"),
        )
        _reject_stale_compare_aliases(effective_columns, effective_source_ref)
        _reject_stale_compare_aliases(effective_columns, effective_target_ref)
        values: dict[str, object] = {}
        if body.name is not None:
            values["name"] = body.name
        if "source_id" in fields_set or source_id != current_source_id:
            values["source_id"] = source_id
        if "target_id" in fields_set or target_id != current_target_id:
            values["target_id"] = target_id
        if body.source_ref is not None:
            values["source_ref"] = body.source_ref.model_dump(mode="json")
        if body.target_ref is not None:
            values["target_ref"] = body.target_ref.model_dump(mode="json")
        if body.columns is not None:
            values["columns"] = [column.model_dump(mode="json") for column in body.columns]
        if body.compare_rules is not None:
            values["compare_rules"] = body.compare_rules.model_dump(mode="json")
        if body.run_limits is not None:
            values["run_limits"] = body.run_limits.model_dump(mode="json")
        if values:
            values["updated_at"] = datetime.now(UTC)
            conn.execute(
                update(compare_tasks).where(compare_tasks.c.id == task_id).values(**values)
            )
        updated = (
            conn.execute(select(compare_tasks).where(compare_tasks.c.id == task_id))
            .mappings()
            .one_or_none()
        )
        if updated is None:
            raise ApiError(404, "not_found", "Compare task not found")
    _audit_business(
        services,
        request,
        user_id=user.id,
        project_id=str(updated["project_id"]),
        action="compare_task_update",
        resource_type="compare_task",
        resource_id=task_id,
        result="success",
    )
    return _compare_task_response(updated)


@router.delete("/compare/tasks/{task_id}", response_model=None)
def delete_compare_task(task_id: str, request: Request) -> Response:
    services = services_from(request)
    user = current_user_from(request)
    with services.engine.begin() as conn:
        row = (
            conn.execute(select(compare_tasks).where(compare_tasks.c.id == task_id))
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ApiError(404, "not_found", "Compare task not found")
        _require_project_access(conn, str(row["project_id"]), user.id)
        conn.execute(delete(compare_tasks).where(compare_tasks.c.id == task_id))
    _audit_business(
        services,
        request,
        user_id=user.id,
        project_id=str(row["project_id"]),
        action="compare_task_delete",
        resource_type="compare_task",
        resource_id=task_id,
        result="success",
    )
    return Response(status_code=204)


@router.post(
    "/compare/tasks/{task_id}/run",
    response_model=CompareRunCreateResponse,
    status_code=202,
)
def run_compare_task(task_id: str, request: Request) -> CompareRunCreateResponse:
    services = services_from(request)
    user = current_user_from(request)
    row = _compare_task_for_current_user(request, task_id)
    rules = dict(row["compare_rules"] or {})
    limits = dict(row["run_limits"] or {})
    key_columns = rules.get("key_columns")
    if not isinstance(key_columns, list) or not key_columns:
        raise ApiError(400, "missing_compare_key", "Compare task requires key_columns")
    columns = row["columns"] if isinstance(row["columns"], list) else []
    if not columns:
        raise ApiError(400, "missing_compare_columns", "Compare task requires columns")

    project_id = str(row["project_id"])
    source_id = _optional_str(row["source_id"])
    target_id = _optional_str(row["target_id"])
    source_ref = dict(row["source_ref"] or {})
    target_ref = dict(row["target_ref"] or {})
    _reject_stale_compare_aliases(cast(list[object], columns), source_ref)
    _reject_stale_compare_aliases(cast(list[object], columns), target_ref)
    timeout_seconds = min(int(limits.get("query_timeout_seconds") or 1800), 3600)
    _validate_compare_result_snapshot_refs(
        services,
        project_id=project_id,
        user_id=user.id,
        refs=[source_ref, target_ref],
        retain_until=datetime.now(UTC)
        + timedelta(seconds=timeout_seconds + _COMPARE_RESULT_INPUT_QUEUE_LEASE_GRACE_SECONDS),
        columns=columns,
        compare_rules=rules,
    )
    # 文件侧:把 upload_id 解析成 storage_uri + filename 注入 ref(worker 回读用),
    # 与 lineage_batch 同范式(API 侧解析上传件,worker 不直连 uploads 表)。
    with services.engine.connect() as conn:
        _resolve_compare_file_ref(conn, project_id=project_id, ref=source_ref)
        _resolve_compare_file_ref(conn, project_id=project_id, ref=target_ref)

    job_id = new_id()
    run_id = new_id()
    bucket_spools = {bucket: new_id() for bucket in COMPARE_BUCKETS}
    datasource_ids = [ds_id for ds_id in (source_id, target_id) if ds_id is not None]
    job = Job(
        id=job_id,
        kind=JobKind.COMPARE_RUN,
        status=JobStatus.PENDING,
        owner_user_id=user.id,
        project_id=project_id,
        datasource_ids=datasource_ids,
        priority=0,
        timeout_seconds=timeout_seconds,
        resource_profile=ResourceProfile(timeout_seconds=timeout_seconds),
        audit_id=new_id(),
        payload={
            "task_id": task_id,
            "run_id": run_id,
            "source_id": source_id,
            "target_id": target_id,
            "source_ref": source_ref,
            "target_ref": target_ref,
            "columns": columns,
            "compare_rules": rules,
            "run_limits": limits,
            "bucket_result_set_ids": bucket_spools,
        },
    )
    with services.engine.begin() as conn:
        _enqueue_compare_run_job(conn, services, job)
        register_compare_run_index(
            conn,
            run_id=run_id,
            job_id=job_id,
            owner_user_id=user.id,
            project_id=str(row["project_id"]),
            task_id=task_id,
            bucket_spools=bucket_spools,
        )
    _audit_business(
        services,
        request,
        user_id=user.id,
        project_id=str(row["project_id"]),
        action="compare_run",
        resource_type="compare_task",
        resource_id=task_id,
        result="accepted",
        detail={"job_id": job_id},
    )
    return CompareRunCreateResponse(job_id=job_id, run_id=run_id)


def _enqueue_compare_run_job(conn: Connection, services: ApiServices, job: Job) -> None:
    job_backend = cast(Any, services).job_backend
    if isinstance(job_backend, PostgresJobBackend):
        PostgresJobBackend(conn).enqueue(job)
        return
    job_backend.enqueue(job)


@router.get("/compare/runs/{run_id}/results", response_model=CompareRunResultResponse)
def get_compare_run_results(
    run_id: str,
    request: Request,
    bucket: CompareBucket = _COMPARE_BUCKET_QUERY,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
) -> CompareRunResultResponse:
    services = services_from(request)
    run_row = _compare_run_for_current_user(request, run_id)
    bucket_spools = dict(run_row["bucket_spools"] or {})
    bucket_counts = _compare_bucket_counts(run_row["bucket_counts"])
    result_set_id = bucket_spools.get(bucket)
    rows = []
    if isinstance(result_set_id, str) and services.result_store.spool_exists(result_set_id):
        rows = [
            CompareResultRow.model_validate(decode_compare_result_row(row))
            for row in services.result_store.fetch_range(result_set_id, offset, limit)
        ]
    return CompareRunResultResponse(
        job_id=str(run_row["job_id"]),
        run_id=str(run_row["run_id"]),
        bucket=bucket,
        offset=offset,
        limit=limit,
        bucket_counts=bucket_counts,
        progress=_compare_progress(run_row["progress"]),
        diff_profile=_compare_diff_profile(_row_value(run_row, "diff_profile", {})),
        sample_result=_compare_sample_result(_row_value(run_row, "sample_result", None)),
        rows=rows,
    )


def _compare_side_db_type(
    services: ApiServices, datasource_id: object, ref: dict[str, object]
) -> DbType | None:
    """取某侧数据源方言;物化源 / 无 id / 未知类型返回 None。"""
    if str(ref.get("kind") or "table") in {"file", "result_snapshot"}:
        return None
    ds_id = _optional_str(datasource_id)
    if not ds_id:
        return None
    with services.engine.connect() as conn:
        row = (
            conn.execute(select(datasources.c.db_type).where(datasources.c.id == ds_id))
            .mappings()
            .one_or_none()
        )
    if row is None:
        return None
    try:
        return DbType(str(row["db_type"]))
    except ValueError:
        return None


def _compare_diff_sql_side(
    db_type: DbType | None,
    ref: dict[str, object],
    columns: list[str],
    pk_rows: list[list[object]],
) -> CompareDiffSqlSide:
    kind = str(ref.get("kind") or "table")
    if kind == "file":
        return CompareDiffSqlSide(available=False, reason="file_source")
    if kind == "result_snapshot":
        return CompareDiffSqlSide(available=False, reason="result_snapshot")
    if db_type is None:
        return CompareDiffSqlSide(available=False, reason="unknown_datasource")
    if not pk_rows:
        return CompareDiffSqlSide(available=False, reason="empty_bucket")
    sql = build_diff_row_select(db_type, ref=ref, columns=columns, pk_rows=pk_rows)
    if sql is None:
        return CompareDiffSqlSide(available=False, reason="unsupported_ref")
    return CompareDiffSqlSide(available=True, sql=sql)


@router.get("/compare/runs/{run_id}/diff-sql", response_model=CompareDiffSqlResponse)
def get_compare_run_diff_sql(
    run_id: str,
    request: Request,
    bucket: CompareBucket = _COMPARE_BUCKET_QUERY,
) -> CompareDiffSqlResponse:
    """UX-2 C-3:按桶主键生成两侧定位 SQL(仅文本,不执行)。

    读该 run 的 bucket spool 主键(封顶 _DIFF_SQL_PK_CAP,超出标 truncated),
    源侧列名用 key_columns、目标侧过 column_mappings 映射;文件源那一侧标 available=False。
    """
    services = services_from(request)
    run_row = _compare_run_for_current_user(request, run_id)
    task_row = _compare_task_for_current_user(request, str(run_row["task_id"]))
    rules = dict(task_row["compare_rules"] or {})
    key_columns = rules.get("key_columns")
    if not isinstance(key_columns, list) or not key_columns:
        raise ApiError(400, "missing_compare_key", "Compare run has no key columns")
    key_columns = [str(col) for col in key_columns]
    column_mappings = dict(rules.get("column_mappings") or {})
    source_ref = dict(task_row["source_ref"] or {})
    target_ref = dict(task_row["target_ref"] or {})
    source_db_type = _compare_side_db_type(services, task_row["source_id"], source_ref)
    target_db_type = _compare_side_db_type(services, task_row["target_id"], target_ref)

    bucket_spools = dict(run_row["bucket_spools"] or {})
    result_set_id = bucket_spools.get(bucket)
    pk_dicts: list[dict[str, object]] = []
    truncated = False
    if isinstance(result_set_id, str) and services.result_store.spool_exists(result_set_id):
        raw = services.result_store.fetch_range(result_set_id, 0, _DIFF_SQL_PK_CAP + 1)
        if len(raw) > _DIFF_SQL_PK_CAP:
            truncated = True
            raw = raw[:_DIFF_SQL_PK_CAP]
        pk_dicts = [decode_compare_result_row(row)["pk"] for row in raw]

    # pk 以 source key 列名存一份;目标侧同值、列名过映射。
    target_columns = [column_mappings.get(col, col) for col in key_columns]
    pk_values = [[pk.get(col) for col in key_columns] for pk in pk_dicts]

    return CompareDiffSqlResponse(
        run_id=str(run_row["run_id"]),
        bucket=bucket,
        key_columns=key_columns,
        pk_count=len(pk_dicts),
        truncated=truncated,
        cap=_DIFF_SQL_PK_CAP,
        source=_compare_diff_sql_side(source_db_type, source_ref, key_columns, pk_values),
        target=_compare_diff_sql_side(target_db_type, target_ref, target_columns, pk_values),
    )


def _compare_focus_table(ref: dict[str, object]) -> str | None:
    """对比一侧的物理表名(schema.table);sql / file / 快照源没有可锚定的表名。"""
    if str(ref.get("kind") or "table") != "table":
        return None
    table = _optional_str(ref.get("table_name"))
    if not table:
        return None
    schema_name = _optional_str(ref.get("schema_name"))
    return f"{schema_name}.{table}" if schema_name else table


def _lineage_run_for_focus_table(
    conn: Connection, *, project_id: str, focus_table: str
) -> RowMapping | None:
    """按"对比表是该记录的写入目标"自动匹配最新生效血缘记录(设计稿 D2)。

    表名大小写按各库书写习惯不一(DM/Oracle 常大写),故精确匹配未命中时回退到
    大小写不敏感匹配 —— 不做更强的归一化,那会牵动整个血缘域的存储口径。
    """
    for predicate in (
        lineage_edges.c.target_table == focus_table,
        func.lower(lineage_edges.c.target_table) == focus_table.lower(),
    ):
        row = (
            conn.execute(
                select(lineage_runs)
                .select_from(
                    lineage_runs.join(lineage_edges, lineage_edges.c.run_id == lineage_runs.c.id)
                )
                .where(
                    and_(
                        lineage_runs.c.project_id == project_id,
                        lineage_runs.c.superseded_at.is_(None),
                        lineage_runs.c.status == "success",
                        lineage_edges.c.inference_status != "rejected",
                        predicate,
                    )
                )
                .order_by(lineage_runs.c.created_at.desc(), lineage_runs.c.id.desc())
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )
        if row is not None:
            return row
    return None


def _compare_upstream_edges(
    conn: Connection, *, project_id: str, tables: list[str]
) -> list[UpstreamEdge]:
    """取这些表作为**下游**的列级边(仅生效 run);逐跳调用,规模随链长而非全图。"""
    if not tables:
        return []
    rows = (
        conn.execute(
            select(lineage_column_edges).where(
                and_(
                    lineage_column_edges.c.project_id == project_id,
                    lineage_column_edges.c.target_table.in_(tables),
                    lineage_column_edges.c.inference_status != "rejected",
                    _lineage_active_run_clause(lineage_column_edges),
                )
            )
        )
        .mappings()
        .all()
    )
    return [
        UpstreamEdge(
            edge_id=str(row["id"]),
            run_id=str(row["run_id"]),
            source_table=str(row["source_table"]),
            source_column=str(row["source_column"]),
            target_table=str(row["target_table"]),
            target_column=str(row["target_column"]),
            transformation=str(row["transformation"]),
            transformation_subtype=str(row["transformation_subtype"]),
            inference_status=str(row["inference_status"]),
            confidence=float(row["confidence"]),
        )
        for row in rows
    ]


def _compare_trace_sql_hop(
    hop: UpstreamHop,
    *,
    db_type: DbType | None,
    key_columns: list[str],
    pk_values: list[list[object]],
    lineage_run_id: str | None,
) -> CompareUpstreamHop:
    missing = [key_columns[i] for i, col in enumerate(hop.key_columns) if col is None]
    edges = [
        CompareUpstreamEdge(
            edge_id=edge.edge_id,
            run_id=edge.run_id,
            source_table=edge.source_table,
            source_column=edge.source_column,
            target_table=edge.target_table,
            target_column=edge.target_column,
            transformation_subtype=edge.transformation_subtype,
            inference_status=edge.inference_status,
            confidence=edge.confidence,
        )
        for edge in hop.edges
    ]
    if not hop.available:
        return CompareUpstreamHop(
            depth=hop.depth,
            table=hop.table,
            available=False,
            reason=hop.reason,
            blocked_by=hop.blocked_by,
            missing_key_columns=missing,
            confidence=hop.confidence,
            edges=edges,
        )

    # 追不到的位置整列丢掉 -> 剩余元组必然出现重复,去重(设计稿 §2.2 partial_key)
    kept = [i for i, col in enumerate(hop.key_columns) if col is not None]
    rows = dedupe_pk_rows([[row[i] for i in kept] for row in pk_values])
    risks = hop_risks(hop, missing_columns=missing)
    sql = (
        None
        if db_type is None
        else render_upstream_sql(
            db_type,
            table=hop.table,
            columns=hop.resolved_columns,
            pk_rows=rows,
        )
    )
    if sql is None:
        return CompareUpstreamHop(
            depth=hop.depth,
            table=hop.table,
            available=False,
            reason="unknown_datasource" if db_type is None else "unsupported_identifier",
            missing_key_columns=missing,
            warnings=hop.warnings,
            confidence=hop.confidence,
            edges=edges,
        )
    header = upstream_header_comment(hop, lineage_run_id=lineage_run_id, risks=risks)
    return CompareUpstreamHop(
        depth=hop.depth,
        table=hop.table,
        available=True,
        sql=f"{header}\n{sql}",
        key_columns=hop.resolved_columns,
        missing_key_columns=missing,
        warnings=hop.warnings,
        risks=risks,
        confidence=hop.confidence,
        edges=edges,
    )


@router.get("/compare/runs/{run_id}/trace-sql", response_model=CompareTraceSqlResponse)
def get_compare_run_trace_sql(
    run_id: str,
    request: Request,
    bucket: CompareBucket = _COMPARE_BUCKET_QUERY,
    lineage_run_id: str | None = Query(default=None),
    include_inferred: bool = Query(default=False),
    max_depth: int = Query(default=3, ge=1, le=MAX_UPSTREAM_DEPTH),
) -> CompareTraceSqlResponse:
    """沿列级血缘上游反推:把桶内主键逐跳映射到上游表,每跳每表一条只读 SELECT。

    与 C-3 定位 SQL 同一逃生口哲学:**只生成文本,平台永不执行**。
    ``lineage_run_id`` 省略时按"对比表是谁的写入目标"自动匹配最新生效记录。
    """
    services = services_from(request)
    run_row = _compare_run_for_current_user(request, run_id)
    task_row = _compare_task_for_current_user(request, str(run_row["task_id"]))
    project_id = str(task_row["project_id"])
    rules = dict(task_row["compare_rules"] or {})
    key_columns_raw = rules.get("key_columns")
    if not isinstance(key_columns_raw, list) or not key_columns_raw:
        raise ApiError(400, "missing_compare_key", "Compare run has no key columns")
    key_columns = [str(col) for col in key_columns_raw]
    source_ref = dict(task_row["source_ref"] or {})
    db_type = _compare_side_db_type(services, task_row["source_id"], source_ref)
    focus_table = _compare_focus_table(source_ref)

    empty = CompareTraceSqlResponse(
        run_id=str(run_row["run_id"]),
        bucket=bucket,
        focus_table=focus_table,
        key_columns=key_columns,
        pk_count=0,
        truncated=False,
        cap=_DIFF_SQL_PK_CAP,
        include_inferred=include_inferred,
        dialect=db_type.value if db_type is not None else None,
        available=False,
    )
    if focus_table is None:
        # sql / file / 快照源没有可锚定的物理表名 —— 血缘图无从下手
        return empty.model_copy(update={"reason": "unsupported_ref"})

    bucket_spools = dict(run_row["bucket_spools"] or {})
    result_set_id = bucket_spools.get(bucket)
    pk_dicts: list[dict[str, object]] = []
    truncated = False
    if isinstance(result_set_id, str) and services.result_store.spool_exists(result_set_id):
        raw = services.result_store.fetch_range(result_set_id, 0, _DIFF_SQL_PK_CAP + 1)
        if len(raw) > _DIFF_SQL_PK_CAP:
            truncated = True
            raw = raw[:_DIFF_SQL_PK_CAP]
        pk_dicts = [decode_compare_result_row(row)["pk"] for row in raw]
    if not pk_dicts:
        return empty.model_copy(update={"reason": "empty_bucket", "truncated": truncated})
    pk_values = [[pk.get(col) for col in key_columns] for pk in pk_dicts]

    with services.engine.connect() as conn:
        lineage_row: RowMapping | None
        if lineage_run_id is not None:
            lineage_row = _lineage_run_for_project(
                conn,
                project_id=project_id,
                run_id=lineage_run_id,
                user_id=current_user_from(request).id,
            )
            matched_by = "manual"
        else:
            lineage_row = _lineage_run_for_focus_table(
                conn, project_id=project_id, focus_table=focus_table
            )
            matched_by = "target_table"
        if lineage_row is None:
            return empty.model_copy(
                update={
                    "reason": "no_lineage_run",
                    "pk_count": len(pk_dicts),
                    "truncated": truncated,
                }
            )
        # 逐跳取边:规模随链长增长,不把整个项目的列级边捞进内存
        hops: list[UpstreamHop] = []
        edges: list[UpstreamEdge] = []
        frontier = [focus_table]
        seen_tables = {focus_table}
        for _ in range(max_depth):
            batch = _compare_upstream_edges(conn, project_id=project_id, tables=frontier)
            if not batch:
                break
            edges.extend(batch)
            frontier = [edge.source_table for edge in batch if edge.source_table not in seen_tables]
            seen_tables.update(frontier)
            if not frontier:
                break

    hops = build_upstream_hops(
        focus_table=focus_table,
        key_columns=key_columns,
        edges=edges,
        include_inferred=include_inferred,
        max_depth=max_depth,
    )
    lineage_run_ref = str(lineage_row["id"])
    hop_models = [
        _compare_trace_sql_hop(
            hop,
            db_type=db_type,
            key_columns=key_columns,
            pk_values=pk_values,
            lineage_run_id=lineage_run_ref,
        )
        for hop in hops
    ]
    _audit_business(
        services,
        request,
        user_id=current_user_from(request).id,
        project_id=project_id,
        action="compare_trace_sql",
        resource_type="compare_run",
        resource_id=str(run_row["run_id"]),
        result="success",
        # R5:只记结构性计数,SQL 文本与主键值绝不进日志
        detail={
            "bucket": bucket,
            "hop_count": len(hop_models),
            "available_hops": sum(1 for hop in hop_models if hop.available),
            "include_inferred": include_inferred,
        },
    )
    return CompareTraceSqlResponse(
        run_id=str(run_row["run_id"]),
        bucket=bucket,
        focus_table=focus_table,
        key_columns=key_columns,
        pk_count=len(pk_dicts),
        truncated=truncated,
        cap=_DIFF_SQL_PK_CAP,
        lineage_run_id=lineage_run_ref,
        lineage_source_ref=str(lineage_row["source_ref"]),
        lineage_matched_by=matched_by,
        include_inferred=include_inferred,
        dialect=db_type.value if db_type is not None else None,
        available=any(hop.available for hop in hop_models),
        reason=None if hop_models else "no_upstream_lineage",
        hops=hop_models,
    )


@router.post(
    "/compare/runs/{run_id}/trace-sql/ai",
    response_model=CompareTraceSqlAiResponse,
)
def create_compare_trace_sql_ai(
    run_id: str,
    body: CompareTraceSqlAiRequest,
    request: Request,
) -> CompareTraceSqlAiResponse:
    """AI 组装某个断链跳的上游定位 SQL(受约束单轮管道,设计稿 Q5 形态 A)。

    确定性反推能算的绝不交给模型:图遍历、主键谓词拼接都在本地。AI 只做"读血缘边 +
    存档 SQL,组装该跳查询模板"这一件事,恰好调 gateway 一次。

    **主键值(L4)永不出网** —— prompt 里是 ``{{PK_TUPLES}}`` 占位符,字面量在
    校验通过之后由本地 ``sql_literal`` 回填。AI 关闭 → 409 ``ai_disabled``;
    gateway 故障 / 校验不过 → ``ok=false`` + error(不 500、不给半成品 SQL)。
    """
    services = services_from(request)
    user = current_user_from(request)
    run_row = _compare_run_for_current_user(request, run_id)
    task_row = _compare_task_for_current_user(request, str(run_row["task_id"]))
    project_id = str(task_row["project_id"])
    rules = dict(task_row["compare_rules"] or {})
    key_columns_raw = rules.get("key_columns")
    if not isinstance(key_columns_raw, list) or not key_columns_raw:
        raise ApiError(400, "missing_compare_key", "Compare run has no key columns")
    key_columns = [str(col) for col in key_columns_raw]
    source_ref = dict(task_row["source_ref"] or {})
    focus_table = _compare_focus_table(source_ref)
    if focus_table is None:
        raise ApiError(400, "unsupported_ref", "Compare source is not a physical table")
    db_type = _compare_side_db_type(services, task_row["source_id"], source_ref)
    if db_type is None:
        raise ApiError(400, "unknown_datasource", "Compare source dialect is unknown")

    with services.engine.connect() as conn:
        ai_row = _ai_config_row_or_none(conn)
    runtime = _ai_runtime_config(services, ai_row)
    if not runtime.enabled:
        raise ApiError(409, "ai_disabled", "AI is disabled")

    def fail(error: str, steps: list[str]) -> CompareTraceSqlAiResponse:
        _audit_business(
            services,
            request,
            user_id=user.id,
            project_id=project_id,
            action="compare_trace_sql_ai",
            resource_type="compare_run",
            resource_id=str(run_row["run_id"]),
            result="failed",
            detail={"upstream_table": body.upstream_table, "error": error},
        )
        return CompareTraceSqlAiResponse(
            run_id=str(run_row["run_id"]),
            upstream_table=body.upstream_table,
            ok=False,
            error=error,
            steps=steps,
        )

    steps = ["read_lineage_edges_local"]
    bucket_spools = dict(run_row["bucket_spools"] or {})
    result_set_id = bucket_spools.get(body.bucket)
    pk_dicts: list[dict[str, object]] = []
    if isinstance(result_set_id, str) and services.result_store.spool_exists(result_set_id):
        raw = services.result_store.fetch_range(result_set_id, 0, _DIFF_SQL_PK_CAP)
        pk_dicts = [decode_compare_result_row(row)["pk"] for row in raw]
    if not pk_dicts:
        return fail("empty_bucket", steps)
    pk_values = [[pk.get(col) for col in key_columns] for pk in pk_dicts]

    with services.engine.connect() as conn:
        if body.lineage_run_id is not None:
            lineage_row: RowMapping | None = _lineage_run_for_project(
                conn, project_id=project_id, run_id=body.lineage_run_id, user_id=user.id
            )
        else:
            lineage_row = _lineage_run_for_focus_table(
                conn, project_id=project_id, focus_table=focus_table
            )
        if lineage_row is None:
            return fail("no_lineage_run", steps)
        edges: list[UpstreamEdge] = []
        frontier = [focus_table]
        seen_tables = {focus_table}
        for _ in range(body.max_depth):
            batch = _compare_upstream_edges(conn, project_id=project_id, tables=frontier)
            if not batch:
                break
            edges.extend(batch)
            frontier = [edge.source_table for edge in batch if edge.source_table not in seen_tables]
            seen_tables.update(frontier)
            if not frontier:
                break

    # 确定性能算到哪儿就算到哪儿:AI 只接手断链的那一跳
    hops = build_upstream_hops(
        focus_table=focus_table,
        key_columns=key_columns,
        edges=edges,
        include_inferred=body.include_inferred,
        max_depth=body.max_depth,
    )
    steps.append("deterministic_mapping_local")
    target_hop = next((hop for hop in hops if hop.table == body.upstream_table), None)
    if target_hop is None:
        return fail("hop_not_found", steps)
    if target_hop.available:
        # 确定性已经能生成 —— 不浪费一次出网调用
        return fail("hop_already_deterministic", steps)
    steps.append(f"blocked_by:{target_hop.blocked_by or 'unknown'}")

    hop_edges = [edge for edge in edges if edge.source_table == body.upstream_table]
    lineage_payload, payload_truncated = build_lineage_context_payload(
        focus_table=focus_table,
        blocked_table=body.upstream_table,
        key_columns=key_columns,
        edges=hop_edges or edges,
    )
    archived_sql = _optional_str(lineage_row["sql_text"])
    prompt, sql_text_for_egress, sql_truncated = build_ai_upstream_prompt(
        dialect=db_type.value,
        lineage_payload=lineage_payload,
        archived_sql=archived_sql,
    )
    # 出网级别按内容如实标注:边结构是 L2,SQL 原文是 L3。
    context_items = [
        ContextItem(
            content=json.dumps(lineage_payload, ensure_ascii=False, sort_keys=True),
            egress_level=EgressLevel.L2,
        )
    ]
    if sql_text_for_egress:
        context_items.append(ContextItem(content=sql_text_for_egress, egress_level=EgressLevel.L3))
    steps.append("ai_gateway_complete_once")
    try:
        ai_response = build_gateway_from_runtime_config(runtime).complete(
            prompt,
            AiContext(items=context_items),
            AiOptions(purpose="compare_trace_sql_ai", max_tokens=800),
        )
    except AiGatewayError as exc:
        return fail(type(exc).__name__, steps)

    parsed = parse_ai_upstream_response(ai_response.content)
    if parsed is None:
        return fail("invalid_ai_sql", steps)
    steps.append("validate_local")
    allowed = {focus_table.lower(), body.upstream_table.lower()}
    for edge in edges:
        allowed.update(
            {
                edge.source_table.lower(),
                edge.target_table.lower(),
                edge.source_column.lower(),
                edge.target_column.lower(),
            }
        )
    allowed.update(col.lower() for col in key_columns)
    reason = validate_ai_sql(
        parsed.sql_template, dialect=db_type.value, allowed_identifiers=allowed
    )
    if reason is not None:
        return fail(reason, steps)

    # ★ 字面量在这里才拼:模型既没见过值,也没机会写值。
    deduped = dedupe_pk_rows(pk_values)
    if len(key_columns) == 1:
        pk_tuples_sql = "(" + ", ".join(sql_literal(row[0]) for row in deduped) + ")"
    else:
        pk_tuples_sql = (
            "("
            + ", ".join(
                "(" + ", ".join(sql_literal(value) for value in row) + ")" for row in deduped
            )
            + ")"
        )
    filled = fill_pk_placeholder(parsed.sql_template, pk_tuples_sql=pk_tuples_sql)
    steps.append("fill_pk_local")

    deterministic_min = min((edge.confidence for edge in hop_edges), default=CONFIDENCE_CEILING)
    confidence = combine_confidence(deterministic_min, parsed.ai_confidence)
    risks = ["ai_generated_needs_review", "pk_name_stability_assumed"]
    if any(edge.inference_status == "inferred" for edge in hop_edges):
        risks.append("inferred_edge_used")
    if payload_truncated or sql_truncated:
        risks.append("context_truncated")
    header = "\n".join(
        [
            f"-- 血缘溯源(AI 组装)· run {lineage_row['id']} · {body.upstream_table}",
            f"-- 整体置信度 {round(confidence * 100)}%"
            f"(确定性 {round(deterministic_min * 100)}%"
            f" x AI 自报 {round(parsed.ai_confidence * 100)}%)",
            "-- AI 推断,未执行;字面量由系统本地回填。请自行核对后再运行。",
        ]
    )
    _audit_business(
        services,
        request,
        user_id=user.id,
        project_id=project_id,
        action="compare_trace_sql_ai",
        resource_type="compare_run",
        resource_id=str(run_row["run_id"]),
        result="success",
        # R5:只记结构性字段,SQL 文本与主键值绝不进日志
        detail={
            "upstream_table": body.upstream_table,
            "provider": ai_response.provider,
            "egress_level": int(EgressLevel.L3 if sql_text_for_egress else EgressLevel.L2),
            "confidence": round(confidence, 4),
        },
    )
    return CompareTraceSqlAiResponse(
        run_id=str(run_row["run_id"]),
        upstream_table=body.upstream_table,
        ok=True,
        sql=f"{header}\n{filled}",
        explanation=parsed.explanation,
        confidence=confidence,
        risks=risks,
        steps=steps,
        provider=ai_response.provider,
        model=ai_response.model,
        egress_level=int(EgressLevel.L3 if sql_text_for_egress else EgressLevel.L2),
        context_truncated=payload_truncated or sql_truncated,
    )


@router.get("/compare/runs/{run_id}/profile", response_model=CompareRunProfileResponse)
def get_compare_run_profile(run_id: str, request: Request) -> CompareRunProfileResponse:
    run_row = _compare_run_for_current_user(request, run_id)
    return CompareRunProfileResponse(
        job_id=str(run_row["job_id"]),
        run_id=str(run_row["run_id"]),
        bucket_counts=_compare_bucket_counts(run_row["bucket_counts"]),
        progress=_compare_progress(run_row["progress"]),
        diff_profile=_compare_diff_profile(_row_value(run_row, "diff_profile", {})),
        sample_result=_compare_sample_result(_row_value(run_row, "sample_result", None)),
    )


@router.post("/compare/runs/{run_id}/export", response_model=ExportCreateResponse, status_code=202)
def create_compare_run_export(
    run_id: str,
    body: CompareExportCreateRequest,
    request: Request,
) -> ExportCreateResponse:
    services = services_from(request)
    user = current_user_from(request)
    run_row = _compare_run_for_current_user(request, run_id)
    _require_compare_run_exportable(run_row)
    bucket_spools = _compare_bucket_spools(run_row["bucket_spools"])
    for result_set_id in bucket_spools.values():
        if not services.result_store.spool_exists(result_set_id):
            raise ApiError(404, "not_found", "Compare run result not found")
    _enforce_export_rate_limit(services, user.id)

    export_job_id = new_id()
    task_name: str | None = None
    task_id = _optional_str(run_row["task_id"]) if "task_id" in run_row else None
    if task_id:
        with services.engine.connect() as conn:
            task_name = conn.execute(
                select(compare_tasks.c.name).where(compare_tasks.c.id == task_id)
            ).scalar_one_or_none()
    filename = _compare_export_filename(run_row, task_name)
    content_type = export_content_type(body.format)
    download_token = token_urlsafe(32)
    token_hash = _download_token_hash(download_token)
    expires_at = datetime.now(UTC) + timedelta(seconds=services.download_url_ttl_seconds)
    job = Job(
        id=export_job_id,
        kind=JobKind.RESULT_EXPORT,
        status=JobStatus.PENDING,
        owner_user_id=user.id,
        project_id=str(run_row["project_id"]),
        datasource_ids=[],
        priority=0,
        timeout_seconds=300,
        resource_profile=ResourceProfile(),
        audit_id=new_id(),
        payload={
            "compare_run_id": str(run_row["run_id"]),
            "bucket_result_set_ids": bucket_spools,
            "format": body.format,
            "filename": filename,
            "export_max_rows": _compare_run_export_max_rows(services, run_row),
        },
    )
    services.job_backend.enqueue(job)
    with services.engine.begin() as conn:
        conn.execute(
            insert(export_download_tokens).values(
                token_hash=token_hash,
                job_id=export_job_id,
                owner_user_id=user.id,
                format=body.format,
                filename=filename,
                content_type=content_type,
                expires_at=expires_at,
            )
        )
    _audit_business(
        services,
        request,
        user_id=user.id,
        project_id=str(run_row["project_id"]),
        action="compare_export",
        resource_type="compare_run",
        resource_id=str(run_row["run_id"]),
        result="accepted",
        detail={"export_job_id": export_job_id, "format": body.format},
    )
    return ExportCreateResponse(
        job_id=export_job_id,
        download_token=download_token,
        expires_at=expires_at,
        format=body.format,
        filename=filename,
    )


@router.post(
    "/compare/runs/{run_id}/ai-attribution",
    response_model=CompareAiAttributionResponse,
)
def explain_compare_run_diff_profile(
    run_id: str,
    request: Request,
) -> CompareAiAttributionResponse:
    services = services_from(request)
    user = current_user_from(request)
    run_row = _compare_run_for_current_user(request, run_id)
    diff_profile = _compare_diff_profile(_row_value(run_row, "diff_profile", {}))
    if not diff_profile:
        return CompareAiAttributionResponse(
            run_id=str(run_row["run_id"]),
            ok=False,
            error="no_diff_profile",
        )

    try:
        with services.engine.connect() as conn:
            ai_row = _ai_config_row_or_none(conn)
        runtime = _ai_runtime_config(services, ai_row)
    except Exception as exc:
        error = type(exc).__name__
        _audit_business(
            services,
            request,
            user_id=user.id,
            project_id=str(run_row["project_id"]),
            action="compare_diff_attribution",
            resource_type="compare_run",
            resource_id=str(run_row["run_id"]),
            result="failed",
            detail={"error": error},
        )
        return CompareAiAttributionResponse(
            run_id=str(run_row["run_id"]),
            ok=False,
            error=error,
        )
    if not runtime.enabled or runtime.provider in {None, "off"}:
        _audit_business(
            services,
            request,
            user_id=user.id,
            project_id=str(run_row["project_id"]),
            action="compare_diff_attribution",
            resource_type="compare_run",
            resource_id=str(run_row["run_id"]),
            result="skipped",
            detail={"error": "ai_disabled"},
        )
        return CompareAiAttributionResponse(
            run_id=str(run_row["run_id"]),
            ok=False,
            error="ai_disabled",
        )

    safe_profile = profile_for_ai(diff_profile)
    context = AiContext(
        items=[
            ContextItem(
                content=json.dumps(
                    safe_profile,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                    separators=(",", ":"),
                ),
                egress_level=EgressLevel.L2,
            )
        ]
    )
    prompt = (
        "Explain the likely causes of this database compare diff profile. "
        "Use only aggregate statistics from the provided JSON. "
        "Do not infer or invent row values. Return concise attribution and next checks."
    )
    try:
        response = build_gateway_from_runtime_config(runtime).complete(
            prompt,
            context,
            AiOptions(purpose="compare_diff_attribution", max_tokens=600),
        )
    except AiGatewayError as exc:
        error = type(exc).__name__
        _audit_business(
            services,
            request,
            user_id=user.id,
            project_id=str(run_row["project_id"]),
            action="compare_diff_attribution",
            resource_type="compare_run",
            resource_id=str(run_row["run_id"]),
            result="failed",
            detail={"error": error},
        )
        return CompareAiAttributionResponse(
            run_id=str(run_row["run_id"]),
            ok=False,
            error=error,
        )
    _audit_business(
        services,
        request,
        user_id=user.id,
        project_id=str(run_row["project_id"]),
        action="compare_diff_attribution",
        resource_type="compare_run",
        resource_id=str(run_row["run_id"]),
        result="success",
        detail={"provider": response.provider},
    )
    return CompareAiAttributionResponse(
        run_id=str(run_row["run_id"]),
        ok=True,
        attribution=response.content,
        provider=response.provider,
        model=response.model,
        egress_level=int(EgressLevel.L2),
    )


@router.post(
    "/projects/{project_id}/lineage/analyze",
    response_model=LineageAnalyzeResponse,
    status_code=201,
)
def analyze_project_lineage(
    project_id: str,
    body: LineageAnalyzeRequest,
    request: Request,
    refresh: bool = Query(default=False),
) -> LineageAnalyzeResponse:
    services = services_from(request)
    user = current_user_from(request)
    with services.engine.begin() as conn:
        datasource_row = _lineage_datasource_for_project(
            conn,
            project_id=project_id,
            datasource_id=body.datasource_id,
            user_id=user.id,
        )
        # L-1:dialect 缺省跟随数据源 db_type(现行为);显式 "auto" 则从 SQL 自动识别,
        # 平票 / 无特征回落 db_type。显式真实方言永远原样透传(不改行为)。
        db_type = str(datasource_row["db_type"])
        detection = resolve_dialect(body.dialect or db_type, body.sql_text, default=db_type)
        dialect = detection.dialect
        schema_context = _lineage_schema_context(conn, body.datasource_id, body.default_schema)
        # DDL 文本数据源的缓存维度取**原文**指纹,不取合并后的 schema_context:
        # 合并可能是空操作,那时带 DDL 与不带 DDL 会算出同一个 hash,缓存双向串位。
        sql_hash = lineage_sql_hash(
            sql_text=body.sql_text,
            dialect=dialect,
            schema_context=schema_context,
            parser_version=LINEAGE_PARSER_VERSION,
            ddl_source=lineage_ddl_fingerprint(
                body.ddl_text, dialect=dialect, default_schema=body.default_schema
            ),
        )
        if not refresh:
            cached = _lineage_cached_run(
                conn,
                project_id=project_id,
                datasource_id=body.datasource_id,
                dialect=dialect,
                source_ref=body.source_ref,
                sql_hash=sql_hash,
            )
            if cached is not None:
                cached_response = _lineage_analyze_response(conn, cached, cached=True)
                if body.ai_fallback:
                    # 缓存命中不重跑 AI(要重跑用 refresh=true);原因对前端可见。
                    cached_response.ai_fallback = LineageAiFallbackResult(
                        requested=True, executed=False, reason="cached_run"
                    )
                return cached_response

        # ★ 缓存未命中之后才解析 DDL:解析在查缓存之前的话,命中缓存也要在
        # engine.begin() 事务内、占着连接池连接,把最多 1 MB 文本全额解析一遍再丢弃。
        try:
            ddl_summary = apply_ddl_schema(
                schema_context,
                body.ddl_text,
                dialect=dialect,
                default_schema=body.default_schema,
            )
        except ValueError as exc:
            # ★ 名副其实:schema_from_ddl_text 对畸形 DDL 从不抛错(只跳过并计数),
            # 这里唯一可能的原因就是方言不在白名单。原来的 lineage_ddl_parse_failed
            # 会让用户反复修改本来正确的 DDL。
            raise ApiError(
                400, "lineage_dialect_unsupported", "Lineage DDL dialect is not supported"
            ) from exc

        try:
            report = analyze_sql_lineage(
                LineageParseRequest(
                    sql_text=body.sql_text,
                    dialect=dialect,
                    schema=schema_context["schema"],
                    default_schema=body.default_schema,
                )
            )
        except ValueError as exc:
            raise ApiError(400, "lineage_parse_failed", "Lineage SQL parse failed") from exc

        run_id = new_id()
        now = datetime.now(UTC)
        if refresh:
            # 0030:标记旧 run 被本次取代(不删除),让出 partial unique index 上的生效位。
            # 放在解析成功之后:解析失败时整事务回滚,历史与生效标记都不动。
            _supersede_lineage_cache(
                conn,
                project_id=project_id,
                datasource_id=body.datasource_id,
                dialect=dialect,
                source_ref=body.source_ref,
                sql_hash=sql_hash,
                superseded_by=run_id,
            )
        parse_summary = dict(report.report or {})
        parse_summary["parser_version"] = LINEAGE_PARSER_VERSION
        if detection.auto_detected:
            # L-1:识别结果落 parse_summary(前端可显示 "auto-detected: oracle")。
            parse_summary["dialect_detection"] = detection.as_summary()
        if ddl_summary is not None:
            # DDL 数据源摘要随 parse_summary 持久化:缓存命中的 run 回读时同样带着,
            # 前端能显示"DDL 补充了 N 张表 / M 列"(与 dialect_detection 同范式)。
            parse_summary["ddl_schema"] = ddl_summary
        conn.execute(
            insert(lineage_runs).values(
                id=run_id,
                project_id=project_id,
                datasource_id=body.datasource_id,
                dialect=dialect,
                source_ref=body.source_ref,
                sql_hash=sql_hash,
                sql_text=body.sql_text,
                parser_version=LINEAGE_PARSER_VERSION,
                status="success",
                parse_summary=parse_summary,
                created_at=now,
                updated_at=now,
            )
        )
        _insert_lineage_edges(
            conn,
            run_id=run_id,
            project_id=project_id,
            sql_hash=sql_hash,
            graph_edges=report.graph_edges,
            insert_mappings=report.insert_mappings,
        )
        row = (
            conn.execute(select(lineage_runs).where(lineage_runs.c.id == run_id))
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ApiError(404, "not_found", "Lineage run not found")
        response = _lineage_analyze_response(conn, row, cached=False)
    # ★ AI 兜底在确定性事务提交之后执行:AI 故障 / 慢调用绝不拖垮主路径,
    # deterministic parse_errors 已落库,AI 只能补充(设计稿 §2.4 第 7 条)。
    if body.ai_fallback:
        response.ai_fallback = _run_lineage_ai_fallback(
            services,
            request,
            user_id=user.id,
            project_id=project_id,
            run_id=run_id,
            sql_text=body.sql_text,
            dialect=dialect,
            sql_hash=sql_hash,
            report=report,
            parse_summary=parse_summary,
        )
    _audit_business(
        services,
        request,
        user_id=user.id,
        project_id=project_id,
        action="lineage_analyze",
        resource_type="lineage_run",
        resource_id=str(row["id"]),
        result="success",
        detail={
            "table_edge_count": response.table_edge_count,
            "column_edge_count": response.column_edge_count,
        },
    )
    return response


_LINEAGE_AI_ENRICH_PROMPT = (
    "You are a senior data engineer reviewing a SQL data-lineage report. You are "
    "given a STRUCTURED JSON profile of one lineage run: aggregate statistics, "
    "target tables with their roles and refresh modes, table roles, table-level "
    "edges, deterministic observations and risks. Only table/column names, roles "
    "and counts are provided — there are no row values and no raw SQL. Write a "
    "concise interpretation of the whole report for a human operator: (1) explain "
    "the refresh pattern of the key target tables, (2) call out the most important "
    "risks and why they matter, (3) give actionable recommendations and checks. "
    "Use only the provided profile; never invent tables, columns or row values. "
    "Answer in the same language as the observations."
)


@router.get(
    "/projects/{project_id}/lineage/runs/{run_id}/ai-enrich",
    response_model=LineageAiEnrichmentResponse,
)
def get_lineage_run_ai_enrichment(
    project_id: str,
    run_id: str,
    request: Request,
) -> LineageAiEnrichmentResponse:
    """读取该 run 最近一次 AI 解读(纯追加存储;无则 ok=False)。"""
    services = services_from(request)
    user = current_user_from(request)
    with services.engine.connect() as conn:
        _lineage_run_for_project(conn, project_id=project_id, run_id=run_id, user_id=user.id)
        row = (
            conn.execute(
                select(lineage_ai_enrichments)
                .where(lineage_ai_enrichments.c.run_id == run_id)
                .order_by(lineage_ai_enrichments.c.created_at.desc())
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        return LineageAiEnrichmentResponse(run_id=run_id, ok=False, error="no_enrichment")
    return LineageAiEnrichmentResponse(
        run_id=run_id,
        ok=True,
        interpretation=str(row["interpretation"]),
        provider=str(row["provider"]),
        model=str(row["model"]),
        egress_level=int(row["egress_level"]),
        created_at=row["created_at"],
    )


@router.post(
    "/projects/{project_id}/lineage/runs/{run_id}/ai-enrich",
    response_model=LineageAiEnrichmentResponse,
    status_code=201,
)
def enrich_lineage_run_with_ai(
    project_id: str,
    run_id: str,
    request: Request,
) -> LineageAiEnrichmentResponse:
    """L-7:对整份血缘报告做 AI 解读并**追加**落库(绝不回写确定性 parse_summary)。

    出站只送 L2 结构画像(表 / 边 / 角色 / 刷新方式 / 计数 / 观察 / 风险),无行值、
    无 SQL 原文全文。AI 关闭 → 结构化 4xx;每步失败都审计。
    """
    services = services_from(request)
    user = current_user_from(request)

    def audit(result: str, detail: dict[str, object]) -> None:
        _audit_business(
            services,
            request,
            user_id=user.id,
            project_id=project_id,
            action="lineage_ai_enrich",
            resource_type="lineage_run",
            resource_id=run_id,
            result=result,
            detail=detail,
        )

    with services.engine.connect() as conn:
        run_row = _lineage_run_for_project(
            conn, project_id=project_id, run_id=run_id, user_id=user.id
        )
        ai_row = _ai_config_row_or_none(conn)
    runtime = _ai_runtime_config(services, ai_row)
    if not runtime.enabled or runtime.provider in {None, "off"}:
        audit("skipped", {"reason": "ai_disabled"})
        raise ApiError(409, "ai_disabled", "AI enrichment is disabled")

    sql_text = _optional_str(run_row["sql_text"])
    if sql_text is None:
        # 0018 之前的旧 run 未存档 SQL —— 无法重建结构化报告。
        audit("skipped", {"reason": "no_sql_text"})
        raise ApiError(409, "lineage_run_not_enrichable", "Lineage run has no archived SQL")

    with services.engine.connect() as conn:
        schema_context = _lineage_schema_context(conn, str(run_row["datasource_id"]), None)
    # 重跑确定性解析(纯函数)得到完整报告 + L-4 语义视图,再投影成 L2 画像。
    # 确定性结果只读不写,parse_summary / 边表原样不动(只增不改)。
    try:
        report = analyze_sql_lineage(
            LineageParseRequest(
                sql_text=sql_text,
                dialect=str(run_row["dialect"]),
                schema=schema_context["schema"],
                default_schema=None,
            )
        )
    except ValueError as exc:
        audit("failed", {"reason": "lineage_parse_failed"})
        raise ApiError(400, "lineage_parse_failed", "Lineage SQL parse failed") from exc
    view = build_semantic_view([(str(run_row["source_ref"]), report)])
    profile = build_enrichment_profile(view, report)

    # ★ 姿态同 Compare `profile_for_ai` / Lineage 兜底:只送结构与统计,按 L2 出站。
    context = AiContext(
        items=[
            ContextItem(
                content=json.dumps(
                    profile,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                egress_level=EgressLevel.L2,
            )
        ]
    )
    try:
        ai_response = build_gateway_from_runtime_config(runtime).complete(
            _LINEAGE_AI_ENRICH_PROMPT,
            context,
            AiOptions(purpose="lineage_ai_enrich", max_tokens=900),
        )
    except AiGatewayError as exc:
        error = type(exc).__name__
        audit("failed", {"error": error})
        raise ApiError(
            502, "ai_provider_error", "AI enrichment call failed", internal_detail=error
        ) from exc

    enrich_id = new_id()
    now = datetime.now(UTC)
    egress = int(EgressLevel.L2)
    with services.engine.begin() as conn:
        conn.execute(
            insert(lineage_ai_enrichments).values(
                id=enrich_id,
                run_id=run_id,
                project_id=project_id,
                interpretation=ai_response.content,
                provider=ai_response.provider,
                model=ai_response.model,
                egress_level=egress,
                created_at=now,
            )
        )
    audit("success", {"provider": ai_response.provider})
    return LineageAiEnrichmentResponse(
        run_id=run_id,
        ok=True,
        interpretation=ai_response.content,
        provider=ai_response.provider,
        model=ai_response.model,
        egress_level=egress,
        created_at=now,
    )


@router.get(
    "/projects/{project_id}/lineage/runs",
    response_model=LineageRunListResponse,
)
def list_project_lineage_runs(
    project_id: str,
    request: Request,
    datasource_id: str | None = Query(default=None),
    source_ref: str | None = Query(default=None),
    status: Literal["success", "failed"] | None = Query(default=None),
    active_only: bool = Query(default=True),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> LineageRunListResponse:
    """0030 解析历史列表:留痕后的 lineage_runs 入口,也是定位 SQL 手动选记录的数据源。

    ``active_only`` 默认 true —— 历史噪音不进首屏;``source_ref`` 是子串匹配(前端
    "按来源标识 / 表名搜索")。每行带该 run 的边数、最低置信度与未确认推断边数。
    """
    services = services_from(request)
    user = current_user_from(request)
    with services.engine.connect() as conn:
        _require_project_access(conn, project_id, user.id)
        conditions = [lineage_runs.c.project_id == project_id]
        if datasource_id is not None:
            conditions.append(lineage_runs.c.datasource_id == datasource_id)
        if source_ref:
            conditions.append(lineage_runs.c.source_ref.ilike(f"%{source_ref}%"))
        if status is not None:
            conditions.append(lineage_runs.c.status == status)
        if active_only:
            conditions.append(lineage_runs.c.superseded_at.is_(None))
        rows = (
            conn.execute(
                select(lineage_runs, datasources.c.name.label("datasource_name"))
                .select_from(
                    lineage_runs.outerjoin(
                        datasources, datasources.c.id == lineage_runs.c.datasource_id
                    )
                )
                .where(and_(*conditions))
                .order_by(lineage_runs.c.created_at.desc(), lineage_runs.c.id.desc())
                .limit(limit + 1)
                .offset(offset)
            )
            .mappings()
            .all()
        )
        page = rows[:limit]
        stats = _lineage_run_edge_stats(conn, [str(row["id"]) for row in page])
    items = [
        LineageRunListItem(
            run_id=str(row["id"]),
            project_id=str(row["project_id"]),
            datasource_id=str(row["datasource_id"]),
            datasource_name=(
                str(row["datasource_name"]) if row["datasource_name"] is not None else None
            ),
            dialect=str(row["dialect"]),
            source_ref=str(row["source_ref"]),
            sql_hash=str(row["sql_hash"]),
            status=cast(Literal["success", "failed"], str(row["status"])),
            table_edge_count=stats[str(row["id"])]["table_edge_count"],
            column_edge_count=stats[str(row["id"])]["column_edge_count"],
            min_confidence=stats[str(row["id"])]["min_confidence"],
            unconfirmed_inferred_count=stats[str(row["id"])]["unconfirmed_inferred_count"],
            target_tables=stats[str(row["id"])]["target_tables"],
            active=row["superseded_at"] is None,
            superseded_at=row["superseded_at"],
            superseded_by=(str(row["superseded_by"]) if row["superseded_by"] is not None else None),
            created_at=row["created_at"],
        )
        for row in page
    ]
    return LineageRunListResponse(project_id=project_id, items=items, has_more=len(rows) > limit)


# 列表页每行只展示前几张写入表(避免一个大脚本把行撑爆);超出部分前端显示 "+N"。
_LINEAGE_RUN_TARGET_TABLE_CAP = 5


def _lineage_run_edge_stats(conn: Connection, run_ids: list[str]) -> dict[str, dict[str, Any]]:
    """逐 run 汇总边数 / 最低置信度 / 未确认推断边数(rejected 边不计)。

    最低置信度取列级边(定位 SQL 反推吃的就是列级边,与 D4 置信度口径一致);
    没有列级边时为 None,前端显示 "—"。
    """

    def empty() -> dict[str, Any]:
        # ★ 每个 run 一份全新的 dict —— target_tables 是可变 list,共享同一个模板对象
        # 会让所有 run 往同一个列表里 append(真机验证抓到:两条 run 各带两份写入表)。
        return {
            "table_edge_count": 0,
            "column_edge_count": 0,
            "min_confidence": None,
            "unconfirmed_inferred_count": 0,
            "target_tables": [],
        }

    stats: dict[str, dict[str, Any]] = {run_id: empty() for run_id in run_ids}
    if not run_ids:
        return stats
    table_rows = (
        conn.execute(
            select(lineage_edges.c.run_id, func.count().label("edge_count"))
            .where(
                and_(
                    lineage_edges.c.run_id.in_(run_ids),
                    lineage_edges.c.inference_status != "rejected",
                )
            )
            .group_by(lineage_edges.c.run_id)
        )
        .mappings()
        .all()
    )
    for row in table_rows:
        stats[str(row["run_id"])]["table_edge_count"] = int(row["edge_count"])
    column_rows = (
        conn.execute(
            select(
                lineage_column_edges.c.run_id,
                func.count().label("edge_count"),
                func.min(lineage_column_edges.c.confidence).label("min_confidence"),
                func.count()
                .filter(lineage_column_edges.c.inference_status == "inferred")
                .label("unconfirmed_count"),
            )
            .where(
                and_(
                    lineage_column_edges.c.run_id.in_(run_ids),
                    lineage_column_edges.c.inference_status != "rejected",
                )
            )
            .group_by(lineage_column_edges.c.run_id)
        )
        .mappings()
        .all()
    )
    for row in column_rows:
        entry = stats[str(row["run_id"])]
        entry["column_edge_count"] = int(row["edge_count"])
        entry["min_confidence"] = (
            float(row["min_confidence"]) if row["min_confidence"] is not None else None
        )
        entry["unconfirmed_inferred_count"] = int(row["unconfirmed_count"])
    # 该 run 写入了哪些表 —— 列表页"查看子图"的焦点,也是后续按对比表名反查血缘记录的锚点。
    target_rows = (
        conn.execute(
            select(lineage_edges.c.run_id, lineage_edges.c.target_table)
            .where(
                and_(
                    lineage_edges.c.run_id.in_(run_ids),
                    lineage_edges.c.inference_status != "rejected",
                )
            )
            .distinct()
            .order_by(lineage_edges.c.run_id, lineage_edges.c.target_table)
        )
        .mappings()
        .all()
    )
    for row in target_rows:
        targets = stats[str(row["run_id"])]["target_tables"]
        if len(targets) < _LINEAGE_RUN_TARGET_TABLE_CAP:
            targets.append(str(row["target_table"]))
    return stats


@router.get(
    "/projects/{project_id}/lineage/subgraph",
    response_model=LineageSubgraphResponse,
)
def get_lineage_subgraph(
    project_id: str,
    request: Request,
    focus: str = _LINEAGE_FOCUS_QUERY,
    direction: LineageDirection = _LINEAGE_DIRECTION_QUERY,
    max_depth: int = _LINEAGE_DEPTH_QUERY,
    include_columns: bool = _LINEAGE_INCLUDE_COLUMNS_QUERY,
) -> LineageSubgraphResponse:
    services = services_from(request)
    user = current_user_from(request)
    with services.engine.connect() as conn:
        _require_project_access(conn, project_id, user.id)
        return _lineage_subgraph_response(
            conn,
            project_id=project_id,
            focus=focus,
            direction=direction,
            max_depth=min(max_depth, 5),
            include_columns=include_columns,
        )


@router.get(
    "/projects/{project_id}/lineage/impact",
    response_model=LineageImpactResponse,
)
def get_lineage_impact(
    project_id: str,
    request: Request,
    focus: str = _LINEAGE_FOCUS_QUERY,
    max_depth: int = _LINEAGE_DEPTH_QUERY,
) -> LineageImpactResponse:
    services = services_from(request)
    user = current_user_from(request)
    with services.engine.connect() as conn:
        _require_project_access(conn, project_id, user.id)
        return _lineage_impact_response(
            conn,
            project_id=project_id,
            focus=focus,
            max_depth=min(max_depth, 5),
        )


@router.post(
    "/projects/{project_id}/lineage/ai-impact",
    response_model=LineageAiImpactResponse,
)
def weighted_lineage_impact(
    project_id: str,
    body: LineageAiImpactRequest,
    request: Request,
) -> LineageAiImpactResponse:
    """C3 Copilot:加权影响分析(设计稿 §2.7.4,L2,提前交付)。

    在基础影响分析(纯图下游遍历,复用 ``_lineage_impact_response``)之上叠加:
    每个受影响资产近 ``window_days`` 天的引用频率 + 项目负责人 → AiGateway 出
    加权排序清单 + 每项理由。全程 L2 出站(仅表/列名 + 聚合计数 + 深度 + 负责人
    名,无行值、无 SQL 原文)。AI 关闭 → 409;gateway 故障 → ok=false 优雅降级
    (确定性 signals 仍返回)。频率数据稀薄(全 0)→ degraded=true,AI 仅按深度加权。
    """
    services = services_from(request)
    user = current_user_from(request)
    max_depth = min(body.max_depth, 5)

    def _audit(result: str, detail: dict[str, object]) -> None:
        _audit_business(
            services,
            request,
            user_id=user.id,
            project_id=project_id,
            action="lineage_ai_impact",
            resource_type="lineage_impact",
            resource_id=body.focus,
            result=result,
            detail=detail,
        )

    # AI 配置先读:关闭直接 409(不跑图查询);审计 skipped。
    try:
        with services.engine.connect() as conn:
            _require_project_access(conn, project_id, user.id)
            ai_row = _ai_config_row_or_none(conn)
        runtime = _ai_runtime_config(services, ai_row)
    except ApiError:
        raise
    except Exception as exc:
        _audit("failed", {"error": type(exc).__name__})
        raise
    if not runtime.enabled or runtime.provider in {None, "off"}:
        _audit("skipped", {"error": "ai_disabled"})
        raise ApiError(409, "ai_disabled", "AI inference is disabled")

    with services.engine.connect() as conn:
        impact = _lineage_impact_response(
            conn,
            project_id=project_id,
            focus=body.focus,
            max_depth=max_depth,
        )
        owner = _project_owner_username(conn, project_id)
        signals = _lineage_impact_signals(
            conn,
            project_id=project_id,
            impacts=impact.impacts,
            window_days=body.window_days,
        )
    degraded = all(sig.ref_count == 0 for sig in signals)

    # ★ L2 出站画像:只送名字 + 聚合计数 + 深度 + 负责人名;无行值、无 SQL 原文。
    outbound = {
        "focus": body.focus,
        "window_days": body.window_days,
        "project_owner": owner,
        "frequency_available": not degraded,
        "assets": [
            {
                "node": sig.node,
                "table": sig.table,
                "column": sig.column,
                "depth": sig.depth,
                "ref_count": sig.ref_count,
                "source_ref_count": sig.source_ref_count,
                "last_referenced_days_ago": sig.last_referenced_days_ago,
            }
            for sig in signals
        ],
    }
    context = AiContext(
        items=[
            ContextItem(
                content=json.dumps(
                    outbound,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ),
                egress_level=EgressLevel.L2,
            )
        ]
    )
    try:
        response = build_gateway_from_runtime_config(runtime).complete(
            _LINEAGE_AI_IMPACT_PROMPT,
            context,
            AiOptions(purpose="lineage_ai_impact", max_tokens=800),
        )
    except AiGatewayError as exc:
        error = type(exc).__name__
        _audit("failed", {"error": error})
        return LineageAiImpactResponse(
            project_id=project_id,
            focus=body.focus,
            max_depth=max_depth,
            window_days=body.window_days,
            ok=False,
            error=error,
            impact_count=impact.impact_count,
            degraded=degraded,
            project_owner=owner,
            signals=signals,
        )
    _audit(
        "success",
        {
            "provider": response.provider,
            "impact_count": impact.impact_count,
            "degraded": degraded,
        },
    )
    return LineageAiImpactResponse(
        project_id=project_id,
        focus=body.focus,
        max_depth=max_depth,
        window_days=body.window_days,
        ok=True,
        assessment=response.content,
        provider=response.provider,
        model=response.model,
        egress_level=int(EgressLevel.L2),
        impact_count=impact.impact_count,
        degraded=degraded,
        project_owner=owner,
        signals=signals,
    )


@router.post(
    "/projects/{project_id}/lineage/export",
    response_model=ExportCreateResponse,
    status_code=201,
)
def create_lineage_export(
    project_id: str,
    body: LineageExportRequest,
    request: Request,
) -> ExportCreateResponse:
    """血缘报告基础导出(L-5):焦点子图 + 影响分析同步生成一个 xlsx
    (三 sheet:table_edges / column_edges / impact)。

    与 sql/compare 导出复用同一 artifact 存储 + 一次性下载 token 通道
    (GET /exports/{token} / GC / 每小时限额);差别是子图 depth<=5 数据量小,
    同步生成不走 job 队列 —— jobs 表直接落 status=success 的 RESULT_EXPORT 行,
    让下载端点的 join 与限额计数照常工作。
    """
    services = services_from(request)
    user = current_user_from(request)
    max_depth = min(body.max_depth, 5)
    with services.engine.connect() as conn:
        _require_project_access(conn, project_id, user.id)
    _enforce_export_rate_limit(services, user.id)
    with services.engine.connect() as conn:
        subgraph = _lineage_subgraph_response(
            conn,
            project_id=project_id,
            focus=body.focus,
            direction=body.direction,
            max_depth=max_depth,
            include_columns=body.include_columns,
        )
        impact = _lineage_impact_response(
            conn,
            project_id=project_id,
            focus=body.focus,
            max_depth=max_depth,
        )

    export_job_id = new_id()
    filename = f"lineage-{export_job_id}.xlsx"
    content_type = export_content_type("excel")
    try:
        with tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024, mode="w+b") as stream:
            binary_stream = cast(BinaryIO, stream)
            exported_bytes = write_xlsx_workbook(
                stream=binary_stream,
                sheets=_lineage_export_sheets(
                    subgraph, impact, include_columns=body.include_columns
                ),
                limit_bytes=_LINEAGE_EXPORT_LIMIT_BYTES,
            )
            binary_stream.seek(0)
            result_ref = services.result_store.put_export_artifact(
                export_job_id, filename, binary_stream
            )
    except ExportSizeLimitExceeded as exc:
        raise ApiError(413, "export_limit_exceeded", "Lineage export exceeds size limit") from exc
    result_ref = result_ref.model_copy(
        update={"metadata": {"format": "excel", "filename": filename, "bytes": exported_bytes}}
    )

    download_token = token_urlsafe(32)
    token_hash = _download_token_hash(download_token)
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=services.download_url_ttl_seconds)
    table_edge_count = sum(1 for edge in subgraph.edges if edge.edge_kind != "column")
    column_edge_count = subgraph.edge_count - table_edge_count
    with services.engine.begin() as conn:
        conn.execute(
            insert(jobs).values(
                id=export_job_id,
                kind=JobKind.RESULT_EXPORT.value,
                status=JobStatus.SUCCESS.value,
                owner_user_id=user.id,
                project_id=project_id,
                datasource_ids=[],
                priority=0,
                timeout_seconds=300,
                resource_profile={},
                result_ref=result_ref.model_dump(),
                audit_id=new_id(),
                payload={
                    "lineage_export": True,
                    "focus": body.focus,
                    "direction": body.direction,
                    "format": "excel",
                    "filename": filename,
                },
                created_at=now,
                started_at=now,
                finished_at=now,
            )
        )
        conn.execute(
            insert(export_download_tokens).values(
                token_hash=token_hash,
                job_id=export_job_id,
                owner_user_id=user.id,
                format="excel",
                filename=filename,
                content_type=content_type,
                expires_at=expires_at,
            )
        )
    _audit_business(
        services,
        request,
        user_id=user.id,
        project_id=project_id,
        action="lineage_export",
        resource_type="lineage_export",
        resource_id=export_job_id,
        result="success",
        detail={
            "focus": body.focus,
            "direction": body.direction,
            "table_edge_count": table_edge_count,
            "column_edge_count": column_edge_count,
            "impact_count": impact.impact_count,
        },
    )
    return ExportCreateResponse(
        job_id=export_job_id,
        download_token=download_token,
        expires_at=expires_at,
        format="excel",
        filename=filename,
    )


@router.post(
    "/projects/{project_id}/lineage/batch",
    response_model=LineageBatchCreateResponse,
    status_code=202,
)
def create_lineage_batch(
    project_id: str,
    body: LineageBatchCreateRequest,
    request: Request,
) -> LineageBatchCreateResponse:
    """ZIP 批量血缘分析(L-2):上传件入队 worker 逐文件宽松解析 + 汇总报告。"""
    services = services_from(request)
    user = current_user_from(request)
    with services.engine.begin() as conn:
        _require_project_access(conn, project_id, user.id)
        upload_row = (
            conn.execute(
                select(uploads).where(
                    and_(
                        uploads.c.id == body.upload_id,
                        uploads.c.project_id == project_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if upload_row is None:
            raise ApiError(404, "not_found", "Upload not found")
        if str(upload_row["purpose"]) != "lineage_batch":
            raise ApiError(400, "invalid_upload_purpose", "Upload purpose must be lineage_batch")
        # 数据源可选:给了则校验归属(元数据感知 + 落持久化子图);不给
        # (无库纯文本导入)则要求显式 dialect(schema 层已校验),纯宽松表级。
        if body.datasource_id is not None:
            datasource_row = (
                conn.execute(
                    select(datasources).where(
                        and_(
                            datasources.c.id == body.datasource_id,
                            datasources.c.project_id == project_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if datasource_row is None:
                raise ApiError(404, "not_found", "Datasource not found")
        job_id = new_id()
        job = Job(
            id=job_id,
            kind=JobKind.LINEAGE_BATCH,
            status=JobStatus.PENDING,
            owner_user_id=user.id,
            project_id=project_id,
            datasource_ids=[body.datasource_id] if body.datasource_id else [],
            priority=0,
            timeout_seconds=1800,
            resource_profile=ResourceProfile(timeout_seconds=1800),
            audit_id=new_id(),
            payload={
                "upload_id": body.upload_id,
                "storage_uri": str(upload_row["storage_uri"]),
                "datasource_id": body.datasource_id,
                "dialect": body.dialect,
                "default_schema": body.default_schema,
                "filename": str(upload_row["filename"]),
                # DDL 文本数据源(可选):worker 侧解析成列元数据后并入 schema,
                # 让无库 / 缺元数据的批量分析也能出列级血缘。
                "ddl_text": body.ddl_text,
            },
        )
        _enqueue_job_txn(conn, services, job)
    _audit_business(
        services,
        request,
        user_id=user.id,
        project_id=project_id,
        action="lineage_batch_create",
        resource_type="lineage_batch",
        resource_id=job_id,
        result="accepted",
        detail={"upload_id": body.upload_id, "datasource_id": body.datasource_id},
    )
    return LineageBatchCreateResponse(job_id=job_id)


@router.get(
    "/projects/{project_id}/lineage/batch/{job_id}",
    response_model=LineageBatchStatusResponse,
)
def get_lineage_batch(
    project_id: str,
    job_id: str,
    request: Request,
) -> LineageBatchStatusResponse:
    """批量分析状态 + success 后回读汇总报告(worker 落的 job artifact)。"""
    services = services_from(request)
    user = current_user_from(request)
    with services.engine.connect() as conn:
        _require_project_access(conn, project_id, user.id)
        row = (
            conn.execute(
                select(jobs).where(
                    and_(
                        jobs.c.id == job_id,
                        jobs.c.kind == JobKind.LINEAGE_BATCH.value,
                        jobs.c.project_id == project_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise ApiError(404, "not_found", "Lineage batch job not found")
    status = JobStatus(str(row["status"]))
    report: dict[str, Any] | None = None
    if status is JobStatus.SUCCESS:
        result_ref = dict(row["result_ref"] or {})
        report_uri = str((result_ref.get("metadata") or {}).get("report_uri") or "")
        if report_uri:
            with services.result_store.open_download(
                ResultRef(backend="local_fs", uri=report_uri)
            ) as stream:
                report = json.loads(stream.read().decode("utf-8"))
    return LineageBatchStatusResponse(
        job_id=job_id,
        status=status,
        error=_optional_str(row["error"]),
        report=report,
    )


@router.post(
    "/projects/{project_id}/lineage/batch/{job_id}/export",
    response_model=ExportCreateResponse,
    status_code=201,
)
def create_lineage_batch_export(
    project_id: str,
    job_id: str,
    request: Request,
) -> ExportCreateResponse:
    """批量血缘完整导出(L-5+ PR1):把批量作业(L-2)的 report JSON + L-4 语义视图
    同步导出为一个 xlsx(A 组 7 sheet:流程总览 / 脚本清单 / 跨脚本依赖 / 风险提示 /
    流程图分组 / 表角色 / 目标整合)。

    与 /lineage/export(焦点子图)是**平行特性、不同数据面**:此端点吃批量作业
    落的 lineage_batch_report.json,那个吃项目级持久化子图。二者复用同一
    artifact 存储 + 一次性下载 token + 限额 + GC 通道(不新开下载链路)。
    """
    services = services_from(request)
    user = current_user_from(request)
    with services.engine.connect() as conn:
        _require_project_access(conn, project_id, user.id)
        row = (
            conn.execute(
                select(jobs).where(
                    and_(
                        jobs.c.id == job_id,
                        jobs.c.kind == JobKind.LINEAGE_BATCH.value,
                        jobs.c.project_id == project_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise ApiError(404, "not_found", "Lineage batch job not found")
    status = JobStatus(str(row["status"]))
    if status is not JobStatus.SUCCESS:
        raise ApiError(409, "batch_not_ready", "Lineage batch job is not complete")
    result_ref = dict(row["result_ref"] or {})
    report_uri = str((result_ref.get("metadata") or {}).get("report_uri") or "")
    if not report_uri:
        raise ApiError(409, "batch_report_missing", "Lineage batch report is unavailable")
    with services.result_store.open_download(
        ResultRef(backend="local_fs", uri=report_uri)
    ) as stream:
        report: dict[str, Any] = json.loads(stream.read().decode("utf-8"))

    _enforce_export_rate_limit(services, user.id)
    export_job_id = new_id()
    filename = f"lineage-batch-{export_job_id}.xlsx"
    content_type = export_content_type("excel")
    try:
        with tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024, mode="w+b") as stream:
            binary_stream = cast(BinaryIO, stream)
            exported_bytes = write_xlsx_workbook(
                stream=binary_stream,
                sheets=batch_export_sheets(report),
                limit_bytes=_LINEAGE_EXPORT_LIMIT_BYTES,
            )
            binary_stream.seek(0)
            export_ref = services.result_store.put_export_artifact(
                export_job_id, filename, binary_stream
            )
    except ExportSizeLimitExceeded as exc:
        raise ApiError(413, "export_limit_exceeded", "Lineage export exceeds size limit") from exc
    export_ref = export_ref.model_copy(
        update={"metadata": {"format": "excel", "filename": filename, "bytes": exported_bytes}}
    )

    download_token = token_urlsafe(32)
    token_hash = _download_token_hash(download_token)
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=services.download_url_ttl_seconds)
    with services.engine.begin() as conn:
        conn.execute(
            insert(jobs).values(
                id=export_job_id,
                kind=JobKind.RESULT_EXPORT.value,
                status=JobStatus.SUCCESS.value,
                owner_user_id=user.id,
                project_id=project_id,
                datasource_ids=[],
                priority=0,
                timeout_seconds=300,
                resource_profile={},
                result_ref=export_ref.model_dump(),
                audit_id=new_id(),
                payload={
                    "lineage_batch_export": True,
                    "batch_job_id": job_id,
                    "format": "excel",
                    "filename": filename,
                },
                created_at=now,
                started_at=now,
                finished_at=now,
            )
        )
        conn.execute(
            insert(export_download_tokens).values(
                token_hash=token_hash,
                job_id=export_job_id,
                owner_user_id=user.id,
                format="excel",
                filename=filename,
                content_type=content_type,
                expires_at=expires_at,
            )
        )
    _audit_business(
        services,
        request,
        user_id=user.id,
        project_id=project_id,
        action="lineage_batch_export",
        resource_type="lineage_batch_export",
        resource_id=export_job_id,
        result="success",
        detail={
            "batch_job_id": job_id,
            "file_count": int(report.get("file_count") or 0),
        },
    )
    return ExportCreateResponse(
        job_id=export_job_id,
        download_token=download_token,
        expires_at=expires_at,
        format="excel",
        filename=filename,
    )


@router.get(
    "/projects/{project_id}/lineage/column-trace",
    response_model=LineageColumnTraceResponse,
)
def get_lineage_column_trace(
    project_id: str,
    request: Request,
    focus: str = _LINEAGE_FOCUS_QUERY,
    direction: LineageDirection = _LINEAGE_DIRECTION_QUERY,
    max_depth: int = _LINEAGE_DEPTH_QUERY,
) -> LineageColumnTraceResponse:
    """字段多跳追溯(1.x column-lineage 链式形态):给定 table.column,
    返回按 hop 分组的上/下游字段链,每项带 from_node(经由的上一跳)。

    与子图端点同一套列级 CTE(ADR-0019:聚焦递归,不整图加载),
    差别只在输出形态 —— 链式列表(供列表展示与 trace_compare 前置)。
    """
    table, column = _lineage_split_column_focus(focus)
    if column is None:
        raise ApiError(
            400,
            "column_focus_required",
            "focus must be a table.column reference, e.g. app.orders.id",
        )
    services = services_from(request)
    user = current_user_from(request)
    with services.engine.connect() as conn:
        _require_project_access(conn, project_id, user.id)
        return _lineage_column_trace_response(
            conn,
            project_id=project_id,
            focus=focus,
            focus_table=table,
            focus_column=column,
            direction=direction,
            max_depth=min(max_depth, 5),
        )


def _extract_trace_compare_chain(
    trace: LineageColumnTraceResponse,
    *,
    focus_table: str,
    focus_column: str,
) -> list[TraceChainNode]:
    """从上游 column-trace 结果抽出逐跳对比链:焦点表(depth 0)+ 各上游字段节点。

    按 depth 升序遍历、按节点 id 去重(同字段经多路径重复出现只保留最浅一次),
    顺序即 workflow 线性 DAG 的对比顺序。
    """
    chain: list[TraceChainNode] = [TraceChainNode(depth=0, table=focus_table, column=focus_column)]
    seen: set[str] = {f"{focus_table}.{focus_column}"}
    for hop in sorted(trace.hops, key=lambda item: item.depth):
        for item in hop.items:
            if item.node in seen:
                continue
            seen.add(item.node)
            chain.append(TraceChainNode(depth=hop.depth, table=item.table, column=item.column))
    return chain


@router.post(
    "/projects/{project_id}/lineage/trace-compare",
    response_model=TraceCompareResponse,
    status_code=201,
)
def create_lineage_trace_compare(
    project_id: str,
    body: TraceCompareRequest,
    request: Request,
) -> TraceCompareResponse:
    """C-8 逐跳血缘对比:沿焦点字段上游血缘每跳生成一个 compare_run 节点,组装成
    workflow(**只创建不触发**,返回 ``workflow_id`` 供用户在 workflow 页手动触发)。

    两套环境(``source_id`` / ``target_id``)之间对比链上每张表的被追踪字段,定位
    "哪一跳把数据搞错"。``dry_run=true`` 只返回预览计划、不落库。链超 ``max_hops``
    上限 → 400 ``too_many_hops``;焦点无上游血缘 → 400 ``no_upstream_lineage``。
    """
    focus_table, focus_column = _lineage_split_column_focus(body.focus)
    if focus_column is None:
        raise ApiError(
            400,
            "column_focus_required",
            "focus must be a table.column reference, e.g. app.orders.id",
        )
    services = services_from(request)
    user = current_user_from(request)
    with services.engine.begin() as conn:
        _require_project_access(conn, project_id, user.id)
        trace = _lineage_column_trace_response(
            conn,
            project_id=project_id,
            focus=body.focus,
            focus_table=focus_table,
            focus_column=focus_column,
            direction="upstream",
            max_depth=min(body.max_depth, 5),
        )
        if not trace.hops:
            # 焦点字段无上游血缘:逐跳对比无跳可比(链只剩焦点自身),显式 400
            raise ApiError(
                400,
                "no_upstream_lineage",
                "focus column has no upstream lineage to trace-compare",
            )
        chain = _extract_trace_compare_chain(
            trace, focus_table=focus_table, focus_column=focus_column
        )
        try:
            nodes = build_trace_compare_nodes(
                chain=chain,
                source_id=body.source_id,
                target_id=body.target_id,
                key_columns=body.key_columns,
                schema_name=body.schema_name,
                max_hops=body.max_hops,
            )
            spec = build_trace_compare_spec(nodes)
        except TraceCompareBuildError as exc:
            code, _, detail = str(exc).partition(":")
            raise ApiError(400, code.strip() or "trace_compare_invalid", detail.strip()) from exc
        # R7 门禁复核:虽然 build_trace_compare_spec 只生成 compare_run,仍经 WorkflowSpec
        # 构造期双门禁;此处再走一遍 API 侧结构化错误映射,与手写 spec 同口径。
        spec = _validated_workflow_spec(spec.model_dump(mode="json"))

        hops = [
            TraceCompareHopItem(
                node_id=node.node_id,
                depth=node.depth,
                table=node.table,
                column=node.column,
                key_columns=node.key_columns,
            )
            for node in nodes
        ]
        if body.dry_run:
            return TraceCompareResponse(
                project_id=project_id,
                focus=body.focus,
                source_id=body.source_id,
                target_id=body.target_id,
                workflow_id=None,
                workflow_name=None,
                hop_count=len(hops),
                truncated=trace.truncated,
                hops=hops,
            )

        workflow_name = body.name or f"trace-compare {body.focus}"[:128]
        _require_workflow_name_available(conn, project_id=project_id, name=workflow_name)
        workflow_id = new_id()
        now = datetime.now(UTC)
        conn.execute(
            insert(workflows).values(
                id=workflow_id,
                project_id=project_id,
                name=workflow_name,
                dag_jsonb=spec.model_dump(mode="json"),
                schedule_cron=None,
                schedule_enabled=False,
                enabled=True,
                created_by=user.id,
                created_at=now,
                updated_at=now,
            )
        )
    _audit_business(
        services,
        request,
        user_id=user.id,
        project_id=project_id,
        action="lineage_trace_compare",
        resource_type="workflow",
        resource_id=workflow_id,
        result="success",
        detail={"focus": body.focus, "node_count": len(hops)},
    )
    return TraceCompareResponse(
        project_id=project_id,
        focus=body.focus,
        source_id=body.source_id,
        target_id=body.target_id,
        workflow_id=workflow_id,
        workflow_name=workflow_name,
        hop_count=len(hops),
        truncated=trace.truncated,
        hops=hops,
    )


@router.get(
    "/projects/{project_id}/lineage/edges/{edge_id}",
    response_model=LineageEdgeDetailResponse,
)
def get_lineage_edge_detail(
    project_id: str,
    edge_id: str,
    request: Request,
) -> LineageEdgeDetailResponse:
    """边详情(UX P0-L1 边抽屉):边元数据 + 所属解析 run 的溯源(含 SQL 原文)。

    回答"这条边为什么存在"——对标 OpenMetadata 边详情 / Atlan process 侧栏。
    """
    services = services_from(request)
    user = current_user_from(request)
    with services.engine.connect() as conn:
        _require_project_access(conn, project_id, user.id)
        edge_table, row = _lineage_edge_row(conn, edge_id)
        if row is None or str(row["project_id"]) != project_id:
            raise ApiError(404, "not_found", "Lineage edge not found")
        run_row = (
            conn.execute(select(lineage_runs).where(lineage_runs.c.id == str(row["run_id"])))
            .mappings()
            .one_or_none()
        )
    edge = dict(row)
    edge_kind: Literal["table", "column"] = "table" if edge_table is lineage_edges else "column"
    run_info = (
        LineageEdgeRunInfo(
            run_id=str(run_row["id"]),
            source_ref=str(run_row["source_ref"]),
            dialect=str(run_row["dialect"]),
            created_at=run_row["created_at"],
            sql_text=_optional_str(run_row.get("sql_text")),
        )
        if run_row is not None
        else None
    )
    return LineageEdgeDetailResponse(
        edge_id=edge_id,
        edge_kind=edge_kind,
        source_table=str(edge["source_table"]),
        source_column=_optional_str(edge.get("source_column")),
        target_table=str(edge["target_table"]),
        target_column=_optional_str(edge.get("target_column")),
        transformation=_optional_str(edge.get("transformation")),
        transformation_subtype=_optional_str(edge.get("transformation_subtype")),
        inferred=bool(edge["inferred"]),
        inference_status=str(edge["inference_status"]),
        confidence=float(edge["confidence"]),
        run=run_info,
    )


@router.patch(
    "/projects/{project_id}/lineage/edges/{edge_id}",
    response_model=LineageEdgeInferenceResponse,
)
def update_lineage_edge_inference(
    project_id: str,
    edge_id: str,
    body: LineageEdgeInferenceUpdateRequest,
    request: Request,
) -> LineageEdgeInferenceResponse:
    """AI 推断边状态机(设计稿 §2.4 第 7 条):inferred → confirmed / rejected。

    只允许 inferred 起点;confirmed/rejected 为终态(rejected 保留行以留审计轨迹,
    子图/影响分析 CTE 已按 inference_status <> 'rejected' 排除)。
    """
    services = services_from(request)
    user = current_user_from(request)
    with services.engine.begin() as conn:
        _require_project_access(conn, project_id, user.id)
        edge_table, row = _lineage_edge_row(conn, edge_id)
        if row is None or str(row["project_id"]) != project_id:
            raise ApiError(404, "not_found", "Lineage edge not found")
        current_status = str(row["inference_status"])
        if current_status != "inferred":
            raise ApiError(
                409,
                "invalid_inference_transition",
                "Only inferred edges can be confirmed or rejected",
            )
        conn.execute(
            update(edge_table)
            .where(edge_table.c.id == edge_id)
            .values(inference_status=body.inference_status)
        )
    edge_kind: Literal["table", "column"] = "table" if edge_table is lineage_edges else "column"
    _audit_business(
        services,
        request,
        user_id=user.id,
        project_id=project_id,
        action="lineage_edge_inference",
        resource_type="lineage_edge",
        resource_id=edge_id,
        result="success",
        detail={
            "edge_kind": edge_kind,
            "from_status": current_status,
            "to_status": body.inference_status,
        },
    )
    return LineageEdgeInferenceResponse(
        edge_id=edge_id,
        edge_kind=edge_kind,
        inference_status=body.inference_status,
        inferred=bool(row["inferred"]),
        confidence=float(row["confidence"]),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Workflow 定义 CRUD(2.4.0 PR-3;run 触发 / 取消 / 调度端点属 PR-4)
#   R7 门禁落地点 = _validated_workflow_spec:WorkflowSpec 构造期校验
#   (forbidden_node_kind / unsupported_node_kind / 环检测等)映射为结构化 4xx,
#   forbidden 与 unsupported 错误码必须可区分(ADR-0009)。
# ─────────────────────────────────────────────────────────────────────────────


@router.get("/projects/{project_id}/workflows", response_model=list[WorkflowListItem])
def list_project_workflows(
    project_id: str,
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[WorkflowListItem]:
    services = services_from(request)
    user = current_user_from(request)
    with services.engine.connect() as conn:
        _require_project_access(conn, project_id, user.id)
        rows = (
            conn.execute(
                select(workflows)
                .where(workflows.c.project_id == project_id)
                .order_by(workflows.c.updated_at.desc(), workflows.c.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            .mappings()
            .all()
        )
    return [_workflow_list_item(row) for row in rows]


@router.post(
    "/projects/{project_id}/workflows",
    response_model=WorkflowResponse,
    status_code=201,
)
def create_project_workflow(
    project_id: str,
    body: WorkflowCreateRequest,
    request: Request,
) -> WorkflowResponse:
    services = services_from(request)
    user = current_user_from(request)
    spec = _validated_workflow_spec(body.spec)
    workflow_id = new_id()
    now = datetime.now(UTC)
    with services.engine.begin() as conn:
        _require_project_access(conn, project_id, user.id)
        _require_workflow_name_available(conn, project_id=project_id, name=body.name)
        conn.execute(
            insert(workflows).values(
                id=workflow_id,
                project_id=project_id,
                name=body.name,
                dag_jsonb=spec.model_dump(mode="json"),
                schedule_cron=spec.schedule.cron if spec.schedule else None,
                schedule_enabled=spec.schedule.enabled if spec.schedule else False,
                sensor_enabled=spec.sensor.enabled if spec.sensor else False,
                enabled=body.enabled,
                created_by=user.id,
                created_at=now,
                updated_at=now,
            )
        )
        row = (
            conn.execute(select(workflows).where(workflows.c.id == workflow_id))
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ApiError(404, "not_found", "Workflow not found")
    _audit_business(
        services,
        request,
        user_id=user.id,
        project_id=project_id,
        action="workflow_create",
        resource_type="workflow",
        resource_id=workflow_id,
        result="success",
        detail={"node_count": len(spec.nodes)},
    )
    return _workflow_response(row)


@router.get(
    "/projects/{project_id}/workflows/{workflow_id}",
    response_model=WorkflowResponse,
)
def get_project_workflow(
    project_id: str,
    workflow_id: str,
    request: Request,
) -> WorkflowResponse:
    services = services_from(request)
    user = current_user_from(request)
    with services.engine.connect() as conn:
        _require_project_access(conn, project_id, user.id)
        row = _workflow_row_for_project(conn, project_id=project_id, workflow_id=workflow_id)
    return _workflow_response(row)


@router.put(
    "/projects/{project_id}/workflows/{workflow_id}",
    response_model=WorkflowResponse,
)
def update_project_workflow(
    project_id: str,
    workflow_id: str,
    body: WorkflowUpdateRequest,
    request: Request,
) -> WorkflowResponse:
    services = services_from(request)
    user = current_user_from(request)
    # ``notifications`` 省略且仍有 notify 节点时,单独校验请求体会把合法的
    # 既有 target 引用误判为 unknown_notify_target;此形状必须等旧 targets
    # 合并后再校验。其余请求保留 DB 前门禁(尤其 R7 forbidden kind)。
    defer_spec_validation = _workflow_update_needs_stored_notifications(body.spec)
    spec = None if defer_spec_validation else _validated_workflow_spec(body.spec)
    with services.engine.begin() as conn:
        _require_project_access(conn, project_id, user.id)
        existing = _workflow_row_for_project(
            conn,
            project_id=project_id,
            workflow_id=workflow_id,
            for_update=True,
        )
        # preserve-on-omit(C-9/C-7 数据丢失防护):body.spec 是原始 dict,PUT 未
        # 显式带 notifications / variables 时沿用库里既有值,避免 GET-then-PUT
        # 少带字段就静默清空已配置的 notify 目标(连带孤儿化其 SecretStore
        # url_secret_ref)/ 变量。显式传 [] / {} 表达"我要清空",按原样处理。
        preserved: dict[str, Any] = {}
        stored_dag = dict(existing["dag_jsonb"] or {})
        if "notifications" not in body.spec:
            preserved["notifications"] = stored_dag.get("notifications", [])
        if "variables" not in body.spec:
            preserved["variables"] = stored_dag.get("variables", {})
        if preserved or spec is None:
            # 合并后再过一遍 _validated_workflow_spec:R7 / notify id 唯一 / 变量
            # 校验仍作用于最终落库 spec(沿用值本就来自旧的合法 spec)。
            spec = _validated_workflow_spec({**body.spec, **preserved})
        assert spec is not None  # 已在 DB 前或 preserve 合并后完成构造期校验
        _require_workflow_name_available(
            conn,
            project_id=project_id,
            name=body.name,
            exclude_workflow_id=workflow_id,
        )
        workflow_values: dict[str, Any] = {
            "name": body.name,
            "dag_jsonb": spec.model_dump(mode="json"),
            "schedule_cron": spec.schedule.cron if spec.schedule else None,
            "schedule_enabled": spec.schedule.enabled if spec.schedule else False,
            "sensor_enabled": spec.sensor.enabled if spec.sensor else False,
            "updated_at": datetime.now(UTC),
        }
        if body.enabled is not None:
            workflow_values["enabled"] = body.enabled
        conn.execute(
            update(workflows).where(workflows.c.id == workflow_id).values(**workflow_values)
        )
        updated = (
            conn.execute(select(workflows).where(workflows.c.id == workflow_id))
            .mappings()
            .one_or_none()
        )
        if updated is None:
            raise ApiError(404, "not_found", "Workflow not found")
    _audit_business(
        services,
        request,
        user_id=user.id,
        project_id=project_id,
        action="workflow_update",
        resource_type="workflow",
        resource_id=workflow_id,
        result="success",
        detail={"node_count": len(spec.nodes)},
    )
    return _workflow_response(updated)


@router.delete("/projects/{project_id}/workflows/{workflow_id}", status_code=204)
def delete_project_workflow(
    project_id: str,
    workflow_id: str,
    request: Request,
) -> Response:
    services = services_from(request)
    user = current_user_from(request)
    with services.engine.begin() as conn:
        _require_project_access(conn, project_id, user.id)
        _workflow_row_for_project(conn, project_id=project_id, workflow_id=workflow_id)
        # workflow 目前无 run 引用(run 属 PR-4),直接删;引入 run 后再议引用检查
        conn.execute(delete(workflows).where(workflows.c.id == workflow_id))
    _audit_business(
        services,
        request,
        user_id=user.id,
        project_id=project_id,
        action="workflow_delete",
        resource_type="workflow",
        resource_id=workflow_id,
        result="success",
    )
    return Response(status_code=204)


# ── Workflow run 通知目标配置(C-9 PR4)────────────────────────────────────────
#   通知目标是 workflow 的子资源,持久化进 WorkflowSpec.notifications → dag_jsonb。
#   ★ R2/R5:客户端提交整条明文 url(webhook / 企微,含 ?key=token),路由层
#   store_secret 换成 SecretRef,配置里只留 url_secret_ref;明文 url 绝不落 dag_jsonb、
#   绝不回显、绝不进日志。响应 / 审计只带 target_id / channel(不含 url)。


@router.post(
    "/projects/{project_id}/workflows/{workflow_id}/notifications",
    response_model=NotifyTargetResponse,
    status_code=201,
)
def create_workflow_notification(
    project_id: str,
    workflow_id: str,
    body: NotifyTargetCreateRequest,
    request: Request,
) -> NotifyTargetResponse:
    """新增通知目标:收明文 url / SMTP 密码 → store_secret → 只把 ref 追加进 spec。"""
    services = services_from(request)
    user = current_user_from(request)
    target_id = new_id()
    with services.engine.begin() as conn:
        _require_project_access(conn, project_id, user.id)
        row = _workflow_row_for_project(
            conn,
            project_id=project_id,
            workflow_id=workflow_id,
            for_update=True,
        )
        spec = _validated_workflow_spec(dict(row["dag_jsonb"] or {}))
        # 先确认 workflow 存在再 store_secret,避免 404 时留下孤儿 secret。
        url_secret_ref: str | None = None
        password_secret_ref: str | None = None
        if body.channel == "email":
            # ast-grep-ignore: r2-no-plaintext-prefixed-credential-attribute-access
            if body.smtp_password is not None:
                password_secret_ref = services.secret_store.store_secret(
                    # ast-grep-ignore: r2-no-plaintext-prefixed-credential-attribute-access
                    body.smtp_password,
                    SecretKind.SMTP_PASSWORD,
                ).ref
        else:
            if body.url is None:
                raise ApiError(400, "invalid_url_secret_ref", "webhook/wecom 目标必须提供 url")
            url_secret_ref = services.secret_store.store_secret(
                body.url, SecretKind.WEBHOOK_SECRET
            ).ref
        target = _notify_target_from_create(
            target_id,
            body,
            url_secret_ref=url_secret_ref,
            password_secret_ref=password_secret_ref,
        )
        updated = spec.model_copy(update={"notifications": [*spec.notifications, target]})
        _persist_workflow_notifications(conn, workflow_id, updated)
    _audit_business(
        services,
        request,
        user_id=user.id,
        project_id=project_id,
        action="workflow_notify_add",
        resource_type="workflow",
        resource_id=workflow_id,
        result="success",
        detail={"target_id": target_id, "channel": target.channel},
    )
    return _notify_target_response(target)


@router.put(
    "/projects/{project_id}/workflows/{workflow_id}/notifications/{target_id}",
    response_model=NotifyTargetResponse,
)
def update_workflow_notification(
    project_id: str,
    workflow_id: str,
    target_id: str,
    body: NotifyTargetUpdateRequest,
    request: Request,
) -> NotifyTargetResponse:
    """更新通知目标:传新明文 url / SMTP 密码则重新 store_secret 换 ref(旧 secret 删除)。"""
    services = services_from(request)
    user = current_user_from(request)
    old_url_ref: str | None = None
    old_password_ref: str | None = None
    with services.engine.begin() as conn:
        _require_project_access(conn, project_id, user.id)
        row = _workflow_row_for_project(
            conn,
            project_id=project_id,
            workflow_id=workflow_id,
            for_update=True,
        )
        spec = _validated_workflow_spec(dict(row["dag_jsonb"] or {}))
        existing = _require_notify_target(spec, target_id)
        _require_no_active_workflow_run_notify_target(
            conn,
            project_id=project_id,
            workflow_id=workflow_id,
            target_id=target_id,
        )
        url_secret_ref = existing.url_secret_ref
        password_secret_ref = existing.password_secret_ref
        if existing.channel == "email":
            # ast-grep-ignore: r2-no-plaintext-prefixed-credential-attribute-access
            if body.smtp_password is not None:
                secret_ref = services.secret_store.store_secret(
                    # ast-grep-ignore: r2-no-plaintext-prefixed-credential-attribute-access
                    body.smtp_password,
                    SecretKind.SMTP_PASSWORD,
                )
                old_password_ref = existing.password_secret_ref
                password_secret_ref = secret_ref.ref
        elif body.url is not None:
            secret_ref = services.secret_store.store_secret(body.url, SecretKind.WEBHOOK_SECRET)
            old_url_ref = existing.url_secret_ref
            url_secret_ref = secret_ref.ref
        target = _notify_target_from_update(
            existing, body, url_secret_ref=url_secret_ref, password_secret_ref=password_secret_ref
        )
        notifications = [target if item.id == target_id else item for item in spec.notifications]
        updated = spec.model_copy(update={"notifications": notifications})
        _persist_workflow_notifications(conn, workflow_id, updated)
    if old_url_ref is not None:
        services.secret_store.delete_secret(
            SecretRef(ref=old_url_ref, kind=SecretKind.WEBHOOK_SECRET)
        )
    if old_password_ref is not None:
        services.secret_store.delete_secret(
            SecretRef(ref=old_password_ref, kind=SecretKind.SMTP_PASSWORD)
        )
    _audit_business(
        services,
        request,
        user_id=user.id,
        project_id=project_id,
        action="workflow_notify_update",
        resource_type="workflow",
        resource_id=workflow_id,
        result="success",
        detail={"target_id": target_id, "channel": target.channel},
    )
    return _notify_target_response(target)


@router.delete(
    "/projects/{project_id}/workflows/{workflow_id}/notifications/{target_id}",
    status_code=204,
)
def delete_workflow_notification(
    project_id: str,
    workflow_id: str,
    target_id: str,
    request: Request,
) -> Response:
    """删除通知目标:从 spec.notifications 移除,并清理其 SecretStore 条目。"""
    services = services_from(request)
    user = current_user_from(request)
    removed_url_ref: str | None = None
    removed_password_ref: str | None = None
    with services.engine.begin() as conn:
        _require_project_access(conn, project_id, user.id)
        row = _workflow_row_for_project(
            conn,
            project_id=project_id,
            workflow_id=workflow_id,
            for_update=True,
        )
        spec = _validated_workflow_spec(dict(row["dag_jsonb"] or {}))
        existing = _require_notify_target(spec, target_id)
        if _workflow_spec_notify_node_references_target(spec, target_id):
            raise ApiError(
                409,
                "workflow_notify_target_in_use",
                "Notify target is referenced by a workflow node",
            )
        _require_no_active_workflow_run_notify_target(
            conn,
            project_id=project_id,
            workflow_id=workflow_id,
            target_id=target_id,
        )
        if existing.channel == "email":
            removed_password_ref = existing.password_secret_ref
        else:
            removed_url_ref = existing.url_secret_ref
        notifications = [item for item in spec.notifications if item.id != target_id]
        updated_payload = spec.model_dump(mode="json")
        updated_payload["notifications"] = [item.model_dump(mode="json") for item in notifications]
        # model_copy 不运行 WorkflowSpec 的 after-validator;从最终 payload 重建,
        # 保证任何未来的 notifications 交叉约束也在落库前生效。
        updated = _validated_workflow_spec(updated_payload)
        _persist_workflow_notifications(conn, workflow_id, updated)
    if removed_url_ref is not None:
        services.secret_store.delete_secret(
            SecretRef(ref=removed_url_ref, kind=SecretKind.WEBHOOK_SECRET)
        )
    if removed_password_ref is not None:
        services.secret_store.delete_secret(
            SecretRef(ref=removed_password_ref, kind=SecretKind.SMTP_PASSWORD)
        )
    _audit_business(
        services,
        request,
        user_id=user.id,
        project_id=project_id,
        action="workflow_notify_remove",
        resource_type="workflow",
        resource_id=workflow_id,
        result="success",
        detail={"target_id": target_id},
    )
    return Response(status_code=204)


def _notify_target_from_create(
    target_id: str,
    body: NotifyTargetCreateRequest,
    *,
    url_secret_ref: str | None,
    password_secret_ref: str | None,
) -> NotifyTarget:
    fields: dict[str, Any] = {
        "id": target_id,
        "channel": body.channel,
        "url_secret_ref": url_secret_ref,
        "enabled": body.enabled,
        "timeout_seconds": body.timeout_seconds,
    }
    if body.channel == "email":
        fields.update(
            {
                "smtp_host": body.smtp_host,
                "smtp_port": body.smtp_port,
                "smtp_from": body.smtp_from,
                "smtp_to": body.smtp_to,
                "smtp_user": body.smtp_user,
                "password_secret_ref": password_secret_ref,
            }
        )
    if body.events is not None:
        fields["events"] = body.events
    try:
        return NotifyTarget.model_validate(fields)
    except ValidationError as exc:
        raise _notify_target_error(exc) from exc


def _notify_target_from_update(
    existing: NotifyTarget,
    body: NotifyTargetUpdateRequest,
    *,
    url_secret_ref: str | None,
    password_secret_ref: str | None,
) -> NotifyTarget:
    fields: dict[str, Any] = existing.model_dump()
    fields["url_secret_ref"] = url_secret_ref
    fields["password_secret_ref"] = password_secret_ref
    if existing.channel == "email":
        if body.smtp_host is not None:
            fields["smtp_host"] = body.smtp_host
        if body.smtp_port is not None:
            fields["smtp_port"] = body.smtp_port
        if body.smtp_from is not None:
            fields["smtp_from"] = body.smtp_from
        if body.smtp_to is not None:
            fields["smtp_to"] = body.smtp_to
        if body.smtp_user is not None:
            fields["smtp_user"] = body.smtp_user
    if body.events is not None:
        fields["events"] = body.events
    if body.enabled is not None:
        fields["enabled"] = body.enabled
    if body.timeout_seconds is not None:
        fields["timeout_seconds"] = body.timeout_seconds
    try:
        return NotifyTarget.model_validate(fields)
    except ValidationError as exc:
        raise _notify_target_error(exc) from exc


def _notify_target_error(exc: ValidationError) -> ApiError:
    # 领域层 ValueError 约定 "code: 说明";pydantic 包装后带 "Value error, " 前缀。
    # ★ 不把 exc 全文塞进 message(可能含请求字段回显);只取领域 code。
    for error in exc.errors():
        message = str(error.get("msg", "")).removeprefix(_PYDANTIC_VALUE_ERROR_PREFIX)
        code, sep, _ = message.partition(":")
        if sep and code.strip():
            return ApiError(400, code.strip(), message)
    return ApiError(400, "invalid_notify_target", "Notify target is invalid")


def _require_notify_target(spec: WorkflowSpec, target_id: str) -> NotifyTarget:
    for target in spec.notifications:
        if target.id == target_id:
            return target
    raise ApiError(404, "not_found", "Notify target not found")


def _workflow_spec_notify_node_references_target(spec: WorkflowSpec, target_id: str) -> bool:
    return any(
        node.job_kind == "notify" and target_id in node.payload["target_ids"] for node in spec.nodes
    )


def _require_no_active_workflow_run_notify_target(
    conn: Connection,
    *,
    project_id: str,
    workflow_id: str,
    target_id: str,
) -> None:
    active_statuses = (JobStatus.PENDING.value, JobStatus.RUNNING.value)
    rows = (
        conn.execute(
            select(jobs.c.status, jobs.c.payload)
            .where(
                jobs.c.kind.in_(
                    (
                        JobKind.WORKFLOW_RUN.value,
                        JobKind.WORKFLOW_SENSOR_CHECK.value,
                    )
                )
            )
            .where(jobs.c.project_id == project_id)
            .where(jobs.c.status.in_(active_statuses))
            .where(jobs.c.payload["workflow_id"].astext == workflow_id)
        )
        .mappings()
        .all()
    )
    for row in rows:
        # SQL 已过滤;Python 再核对使防护面对测试替身/异常行时仍保持确定。
        if str(row.get("status")) not in active_statuses:
            continue
        payload = row.get("payload")
        if not isinstance(payload, Mapping):
            continue
        frozen_spec = payload.get("spec")
        if not isinstance(frozen_spec, Mapping):
            continue
        notifications = frozen_spec.get("notifications")
        if not isinstance(notifications, list):
            continue
        if any(
            isinstance(target, Mapping) and target.get("id") == target_id
            for target in notifications
        ):
            raise ApiError(
                409,
                "workflow_notify_target_active_run",
                "Notify target is referenced by an active workflow run",
            )


def _persist_workflow_notifications(
    conn: Connection,
    workflow_id: str,
    spec: WorkflowSpec,
) -> None:
    conn.execute(
        update(workflows)
        .where(workflows.c.id == workflow_id)
        .values(dag_jsonb=spec.model_dump(mode="json"), updated_at=datetime.now(UTC))
    )


def _notify_target_response(target: NotifyTarget) -> NotifyTargetResponse:
    # ★ R5:绝不含 url / 密码;email 的连接信息(非敏感)回显便于配置查看。
    is_email = target.channel == "email"
    return NotifyTargetResponse(
        id=target.id,
        channel=target.channel,
        events=list(target.events),
        enabled=target.enabled,
        timeout_seconds=target.timeout_seconds,
        smtp_host=target.smtp_host if is_email else None,
        smtp_port=target.smtp_port if is_email else None,
        smtp_from=target.smtp_from if is_email else None,
        smtp_to=list(target.smtp_to) if is_email else None,
        smtp_user=target.smtp_user if is_email else None,
    )


def _workflow_update_needs_stored_notifications(payload: Mapping[str, Any]) -> bool:
    if "notifications" in payload:
        return False
    nodes = payload.get("nodes")
    return isinstance(nodes, list) and any(
        isinstance(node, Mapping) and node.get("job_kind") == "notify" for node in nodes
    )


def _validated_workflow_spec(payload: dict[str, Any]) -> WorkflowSpec:
    """R7 门禁落地点:WorkflowSpec 构造期校验错误 → 结构化 4xx。

    forbidden_node_kind(R7 红线,永不开放)与 unsupported_node_kind
    (白名单内但首版未实现)必须可区分(ADR-0009 Consequences)。
    """
    try:
        spec = WorkflowSpec.model_validate(payload)
    except ValidationError as exc:
        code, message = _workflow_spec_error(exc)
        raise ApiError(400, code, message) from exc
    # C-10:sensor SQL 只读校验(写语句 → 400)。域层不 import dbclients,故 guard 落此;
    # 所有 create/update 入口都经本函数,故这里是单一门禁。stored spec 复检也走这里
    # (已存的合法 SQL 幂等通过,成本可忽略)。
    if spec.sensor is not None:
        try:
            validate_readonly_sql(spec.sensor.sql)
        except SqlGuardError as exc:
            raise ApiError(
                400,
                "sensor_sql_not_readonly",
                "Sensor SQL must be a read-only SELECT/WITH statement",
            ) from exc
    return spec


def _validated_trigger_variables(
    variables: Mapping[str, str | list[str]],
) -> dict[str, str | list[str]]:
    """触发时运行时变量校验(与 spec.variables 同一套规则,单一实现)。

    校验失败 → 结构化 400;错误消息只含变量名(R5),不含取值。
    """
    try:
        return validate_workflow_variables(variables)
    except ValueError as exc:
        code, _, _ = str(exc).partition(":")
        raise ApiError(400, code or "invalid_variable", str(exc)) from exc


def _workflow_spec_error(exc: ValidationError) -> tuple[str, str]:
    # 领域层 ValueError 约定 "code: 说明";pydantic 包装后位于 errors()[i]["msg"]
    # 且带 "Value error, " 前缀。取第一条可识别 code(按节点声明顺序,确定性)。
    for error in exc.errors():
        message = str(error.get("msg", "")).removeprefix(_PYDANTIC_VALUE_ERROR_PREFIX)
        code, _, _ = message.partition(":")
        code = code.strip()
        if code in _WORKFLOW_SPEC_ERROR_CODES:
            return code, message
    return "invalid_workflow_spec", "Workflow spec is invalid"


def _require_workflow_name_available(
    conn: Connection,
    *,
    project_id: str,
    name: str,
    exclude_workflow_id: str | None = None,
) -> None:
    query = (
        select(workflows.c.id)
        .where(workflows.c.project_id == project_id)
        .where(workflows.c.name == name)
    )
    if exclude_workflow_id is not None:
        query = query.where(workflows.c.id != exclude_workflow_id)
    row = conn.execute(query).mappings().one_or_none()
    if row is not None:
        raise ApiError(409, "workflow_name_conflict", "Workflow name already exists in project")


def _workflow_row_for_project(
    conn: Connection,
    *,
    project_id: str,
    workflow_id: str,
    for_update: bool = False,
) -> RowMapping:
    query = (
        select(workflows)
        .where(workflows.c.id == workflow_id)
        .where(workflows.c.project_id == project_id)
    )
    if for_update:
        query = query.with_for_update()
    row = conn.execute(query).mappings().one_or_none()
    if row is None:
        raise ApiError(404, "not_found", "Workflow not found")
    return row


def _workflow_response(row: RowMapping) -> WorkflowResponse:
    return WorkflowResponse(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        name=str(row["name"]),
        spec=WorkflowSpec.model_validate(row["dag_jsonb"]),
        enabled=bool(row["enabled"]),
        schedule_cron=_optional_str(row["schedule_cron"]),
        schedule_enabled=bool(row["schedule_enabled"]),
        sensor_enabled=bool(row["sensor_enabled"]),
        created_by=_optional_str(row["created_by"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _workflow_list_item(row: RowMapping) -> WorkflowListItem:
    dag = dict(row["dag_jsonb"] or {})
    return WorkflowListItem(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        name=str(row["name"]),
        node_count=len(dag.get("nodes") or []),
        enabled=bool(row["enabled"]),
        schedule_cron=_optional_str(row["schedule_cron"]),
        schedule_enabled=bool(row["schedule_enabled"]),
        sensor_enabled=bool(row["sensor_enabled"]),
        created_by=_optional_str(row["created_by"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ── Workflow run(2.4.0 PR-4:手动触发 / 状态查询 / 取消;cron tick 属 PR-4b)──


@router.post(
    "/projects/{project_id}/workflows/{workflow_id}/runs",
    response_model=WorkflowRunCreateResponse,
    status_code=201,
)
def trigger_workflow_run(
    project_id: str,
    workflow_id: str,
    request: Request,
    body: WorkflowRunTriggerRequest | None = None,
) -> WorkflowRunCreateResponse:
    """手动触发:enqueue 一个 workflow_run job(WorkflowRun 本身是 job,ADR-0009)。

    spec 快照进 payload:执行期不再回读 workflows 表,触发后改定义不影响在途 run。

    可选 body ``{"variables": {...}}``(C-7 PR2):触发时运行时变量,与 spec 默认
    变量、内置变量合并成 when_variables 快照(优先级 builtin < spec < 触发时)。
    """
    services = services_from(request)
    user = current_user_from(request)
    trigger_variables = _validated_trigger_variables(body.variables) if body is not None else {}
    with services.engine.begin() as conn:
        _require_project_access(conn, project_id, user.id)
        row = _workflow_row_for_project(
            conn,
            project_id=project_id,
            workflow_id=workflow_id,
            for_update=True,
        )
        if not bool(row["enabled"]):
            raise ApiError(409, "workflow_disabled", "Workflow is disabled")
        # R7 enqueue 期再校验(ADR-0009 Consequences:创建 + enqueue 双门禁)
        spec = _validated_workflow_spec(dict(row["dag_jsonb"] or {}))
        # 手动触发与 cron tick 共用同一入队构造点(build_workflow_run_job):
        # 变量合并 builtin < spec.variables < 触发时,在触发时刻冻结进 payload;
        # run job 优先级 -1(低于交互 sql_query)。cron tick 走同一路径,详见
        # app/services/workflow_scheduler.py。
        job = build_workflow_run_job(
            workflow_id=workflow_id,
            workflow_name=str(row["name"]),
            project_id=project_id,
            owner_user_id=user.id,
            spec=spec,
            trigger="manual",
            now=datetime.now(UTC),
            extra_variables=trigger_variables,
        )
        run_id = job.id
        _enqueue_job_txn(conn, services, job)
    _audit_business(
        services,
        request,
        user_id=user.id,
        project_id=project_id,
        action="workflow_run_trigger",
        resource_type="workflow",
        resource_id=workflow_id,
        result="accepted",
        detail={"run_id": run_id, "node_count": len(spec.nodes)},
    )
    return WorkflowRunCreateResponse(run_id=run_id, job_id=run_id, workflow_id=workflow_id)


@router.get(
    "/projects/{project_id}/workflow-runs/{run_id}",
    response_model=WorkflowRunStatusResponse,
)
def get_workflow_run(
    project_id: str,
    run_id: str,
    request: Request,
) -> WorkflowRunStatusResponse:
    services = services_from(request)
    user = current_user_from(request)
    with services.engine.connect() as conn:
        _require_project_access(conn, project_id, user.id)
        run_row = _workflow_run_row_for_project(conn, project_id=project_id, run_id=run_id)
        children = _workflow_run_children(conn, run_id)
    return _workflow_run_status_response(
        run_row,
        children,
        default_max_retries=services.workflow_node_default_max_retries,
    )


@router.post(
    "/projects/{project_id}/workflow-runs/{run_id}:cancel",
    response_model=CancelResponse,
)
def cancel_workflow_run(
    project_id: str,
    run_id: str,
    request: Request,
) -> CancelResponse:
    """run 取消:软取消 run job 并立即传播到未终态子 job(worker 侧再兜底传播)。"""
    services = services_from(request)
    user = current_user_from(request)
    with services.engine.connect() as conn:
        _require_project_access(conn, project_id, user.id)
        run_row = _workflow_run_row_for_project(conn, project_id=project_id, run_id=run_id)
        children = _workflow_run_children(conn, run_id)
    run_status = JobStatus(str(run_row["status"]))
    if run_status in {
        JobStatus.SUCCESS,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
        JobStatus.TIMEOUT,
    }:
        raise ApiError(
            409,
            "workflow_run_terminal",
            "Workflow run has already finished and cannot be cancelled",
        )
    services.job_backend.request_cancel(run_id)
    for child in children:
        child_status = str(child["status"])
        child_id = str(child["id"])
        if child_status in {JobStatus.PENDING.value, JobStatus.RUNNING.value}:
            services.job_backend.request_cancel(child_id)
        if child_status == JobStatus.PENDING.value:
            services.job_backend.cancel_pending_job(child_id, "workflow run cancelled")
    _audit_business(
        services,
        request,
        user_id=user.id,
        project_id=project_id,
        action="workflow_run_cancel",
        resource_type="workflow_run",
        resource_id=run_id,
        result="accepted",
    )
    return CancelResponse(cancelled=True)


@router.get(
    "/projects/{project_id}/workflows/{workflow_id}/runs",
    response_model=WorkflowRunsResponse,
)
def list_workflow_runs(
    project_id: str,
    workflow_id: str,
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> WorkflowRunsResponse:
    """列某 workflow 的历史 run(倒序分页;对齐 compare /runs 的 limit+1 has_more 口径)。

    run = kind=workflow_run 的 job,workflow_id 存于 job.payload;节点明细走单 run
    状态端点(GET workflow-runs/{run_id}),此列表只回 run 级状态 + 时间戳。
    """
    services = services_from(request)
    user = current_user_from(request)
    with services.engine.connect() as conn:
        _require_project_access(conn, project_id, user.id)
        # 先确认 workflow 存在(404 语义与其余 workflow 子资源一致)
        _workflow_row_for_project(conn, project_id=project_id, workflow_id=workflow_id)
        rows = (
            conn.execute(
                select(jobs)
                .where(jobs.c.kind == JobKind.WORKFLOW_RUN.value)
                .where(jobs.c.project_id == project_id)
                .where(jobs.c.payload["workflow_id"].astext == workflow_id)
                .order_by(jobs.c.created_at.desc(), jobs.c.id.desc())
                .limit(limit + 1)
                .offset(offset)
            )
            .mappings()
            .all()
        )
    has_more = len(rows) > limit
    return WorkflowRunsResponse(
        workflow_id=workflow_id,
        limit=limit,
        offset=offset,
        has_more=has_more,
        runs=[_workflow_run_list_item(row) for row in rows[:limit]],
    )


def _workflow_run_list_item(row: RowMapping) -> WorkflowRunListItem:
    return WorkflowRunListItem(
        run_id=str(row["id"]),
        job_id=str(row["id"]),
        status=JobStatus(str(row["status"])),
        error=_optional_str(row["error"]),
        created_at=_row_value_or_none(row, "created_at"),
        started_at=_row_value_or_none(row, "started_at"),
        finished_at=_row_value_or_none(row, "finished_at"),
    )


def _enqueue_job_txn(conn: Connection, services: ApiServices, job: Job) -> None:
    """事务内 enqueue(与 _enqueue_compare_run_job 同款:PG backend 复用当前连接)。"""
    job_backend = cast(Any, services).job_backend
    if isinstance(job_backend, PostgresJobBackend):
        PostgresJobBackend(conn).enqueue(job)
        return
    job_backend.enqueue(job)


def _workflow_run_row_for_project(
    conn: Connection,
    *,
    project_id: str,
    run_id: str,
) -> RowMapping:
    row = (
        conn.execute(
            select(jobs)
            .where(jobs.c.id == run_id)
            .where(jobs.c.kind == JobKind.WORKFLOW_RUN.value)
            .where(jobs.c.project_id == project_id)
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise ApiError(404, "not_found", "Workflow run not found")
    return row


def _workflow_run_children(conn: Connection, run_id: str) -> list[RowMapping]:
    return list(
        conn.execute(
            select(jobs)
            .where(jobs.c.parent_workflow_run_id == run_id)
            .order_by(jobs.c.created_at.asc(), jobs.c.id.asc())
        )
        .mappings()
        .all()
    )


def _workflow_run_status_response(
    run_row: RowMapping,
    children: list[RowMapping],
    *,
    default_max_retries: int,
) -> WorkflowRunStatusResponse:
    payload = dict(run_row["payload"] or {})
    run_status = JobStatus(str(run_row["status"]))
    nodes = _workflow_run_node_items(
        payload, children, run_status, default_max_retries=default_max_retries
    )
    return WorkflowRunStatusResponse(
        run_id=str(run_row["id"]),
        workflow_id=_optional_str(payload.get("workflow_id")),
        project_id=str(run_row["project_id"]),
        status=run_status,
        error=_optional_str(run_row["error"]),
        created_at=_row_value_or_none(run_row, "created_at"),
        started_at=_row_value_or_none(run_row, "started_at"),
        finished_at=_row_value_or_none(run_row, "finished_at"),
        nodes=nodes,
    )


def _workflow_run_node_items(
    payload: dict[str, Any],
    children: list[RowMapping],
    run_status: JobStatus,
    *,
    default_max_retries: int,
) -> list[WorkflowRunNodeItem]:
    raw_spec = payload.get("spec")
    try:
        spec = WorkflowSpec.model_validate(raw_spec)
    except ValidationError:
        # spec 快照缺失/损坏时退化为子 job 平铺(不 500,保底可观测)
        fallback_items: list[WorkflowRunNodeItem] = []
        for child in children:
            snapshot = _workflow_child_snapshot_from_row(child)
            if snapshot is None:
                continue
            fallback_items.append(
                WorkflowRunNodeItem(
                    node_id=snapshot.node_id,
                    job_kind=str(child["kind"]),
                    status=snapshot.status.value,
                    job_id=snapshot.job_id,
                    attempts=snapshot.retry_count,
                    error=snapshot.error,
                    outputs=_workflow_child_response_outputs(snapshot),
                )
            )
        return fallback_items
    snapshots = _workflow_child_snapshots_from_rows(children)
    now = datetime.now(UTC)
    # 与 worker 推进器共用同一纯函数 + 同一份触发时冻结的 when 变量快照,
    # 保证节点状态口径一致且不随查询时刻漂移(只读,不执行计划)
    frozen_variables = when_variables_from_payload(payload)
    plan = plan_workflow_step(
        spec,
        snapshots,
        now=now,
        default_max_retries=default_max_retries,
        when_variables=(
            frozen_variables if frozen_variables is not None else builtin_when_variables(now)
        ),
    )
    kinds = {node.id: node.job_kind for node in spec.nodes}
    items: list[WorkflowRunNodeItem] = []
    for node in spec.nodes:
        state = plan.node_states[node.id]
        status = state.status
        if (
            run_status in {JobStatus.FAILED, JobStatus.CANCELLED}
            and status not in TERMINAL_NODE_STATUSES
        ):
            # run 已终态:未终态节点(waiting/running/retry_wait)展示为 cancelled
            status = WorkflowNodeExecStatus.CANCELLED
        items.append(
            WorkflowRunNodeItem(
                node_id=node.id,
                job_kind=kinds[node.id],
                status=status.value,
                job_id=state.job_id,
                attempts=state.attempts,
                error=state.error,
                outputs=state.outputs,
            )
        )
    return items


def _workflow_child_snapshots_from_rows(
    children: list[RowMapping],
) -> dict[str, WorkflowChildJob]:
    snapshots: dict[str, WorkflowChildJob] = {}
    for child in children:
        snapshot = _workflow_child_snapshot_from_row(child)
        if snapshot is None:
            continue
        snapshots[snapshot.node_id] = snapshot
    return snapshots


def _workflow_child_snapshot_from_row(child: RowMapping) -> WorkflowChildJob | None:
    child_payload = child["payload"] if isinstance(child["payload"], dict) else {}
    node_id = child_payload.get("workflow_node_id")
    if not isinstance(node_id, str) or not node_id:
        return None
    result_ref: ResultRef | None = None
    raw_result_ref = child["result_ref"] if "result_ref" in child else None
    if raw_result_ref is not None:
        try:
            result_ref = ResultRef.model_validate(raw_result_ref)
        except ValidationError:
            result_ref = None
    finished_at = child["finished_at"]
    job_kind = str(child["kind"])
    return WorkflowChildJob(
        node_id=node_id,
        job_id=str(child["id"]),
        status=JobStatus(str(child["status"])),
        retry_count=int(child["retry_count"] or 0),
        finished_at=finished_at if isinstance(finished_at, datetime) else None,
        error=_optional_str(child["error"]),
        error_code=_optional_str(child["error_code"] if "error_code" in child else None),
        outputs=extract_workflow_node_outputs(job_kind, result_ref),
    )


def _workflow_child_response_outputs(
    snapshot: WorkflowChildJob,
) -> dict[str, NodeOutputValue]:
    outputs = cast(dict[str, NodeOutputValue], dict(snapshot.outputs))
    outputs.update(
        status=snapshot.status.value,
        job_id=snapshot.job_id,
        error_code=snapshot.error_code,
    )
    return outputs


def _row_value_or_none(row: RowMapping, key: str) -> datetime | None:
    value = row[key] if key in row else None
    return value if isinstance(value, datetime) else None


@router.get("/jobs", response_model=list[JobListItem])
def list_jobs(
    request: Request,
    status: JobStatus | None = None,
    project_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[JobListItem]:
    services = services_from(request)
    user = current_user_from(request)
    with services.engine.connect() as conn:
        if project_id is not None:
            _require_project_access(conn, project_id, user.id)

        filters = [jobs.c.owner_user_id == user.id]
        if status is not None:
            filters.append(jobs.c.status == status.value)
        if project_id is not None:
            filters.append(jobs.c.project_id == project_id)

        rows = (
            conn.execute(
                select(
                    jobs.c.id,
                    jobs.c.kind,
                    jobs.c.status,
                    jobs.c.created_at,
                    jobs.c.started_at,
                    jobs.c.finished_at,
                    jobs.c.error,
                    jobs.c.error_code,
                )
                .select_from(_jobs_visible_to_user(user.id))
                .where(_project_access_filter(user.id))
                .where(and_(*filters))
                .order_by(jobs.c.created_at.desc(), jobs.c.id.desc())
                .limit(limit)
                .offset(offset)
            )
            .mappings()
            .all()
        )
    return [_job_list_item(row) for row in rows]


@router.get("/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str, request: Request) -> JobResponse:
    row = _job_for_current_user(request, job_id)
    return _job_response(row)


@router.get("/jobs/{job_id}/progress", response_model=JobProgressResponse)
def get_job_progress(
    job_id: str,
    request: Request,
    after_version: int = Query(default=0, ge=0),
) -> JobProgressResponse:
    """Return job and spool progress without loading Parquet result rows."""

    services = services_from(request)
    row = _job_for_current_user(request, job_id)
    status = JobStatus(str(row["status"]))
    terminal = status in {
        JobStatus.SUCCESS,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
        JobStatus.TIMEOUT,
    }
    result_set_id = _result_set_id_from_payload(row)
    manifest = (
        _spool_manifest_or_none(services, result_set_id) if result_set_id is not None else None
    )
    loaded_rows = _int_from_manifest(manifest, "loaded_rows") or 0
    result_version = _int_from_manifest(manifest, "result_version") or 0
    columns_ready = bool(_columns_from_manifest(manifest))
    first_batch_at = _manifest_datetime(manifest, "first_batch_at")
    first_batch_ready = first_batch_at is not None or (terminal and columns_ready)
    has_new_result = result_version > after_version
    error_code = _job_error_code(row)
    return JobProgressResponse(
        job_id=job_id,
        result_set_id=result_set_id,
        status=status,
        loaded_rows=loaded_rows,
        result_version=result_version,
        columns_ready=columns_ready,
        first_batch_ready=first_batch_ready,
        terminal=terminal,
        error="Job failed" if status in {JobStatus.FAILED, JobStatus.TIMEOUT} else None,
        error_code=error_code,
        # job 路径的调速规则一字不改(0/1000/2000);`queued` 快档是会话语句
        # 独有的(排队在 lane mailbox 里,毫秒级就轮到)。
        retry_after_ms=poll_retry_after_ms(terminal=terminal, fresh=has_new_result),
        has_new_result=has_new_result,
        truncated=_bool_from_manifest(manifest, "truncated"),
        has_more=_bool_from_manifest(manifest, "has_more"),
        pagination_mode=_manifest_pagination_mode(manifest),
        pagination_reason=_manifest_optional_str(manifest, "pagination_reason"),
        timings=_job_stage_timings(row),
        execution=_job_execution_metrics(row, manifest),
    )


@router.get("/jobs/{job_id}/result", response_model=JobResultResponse)
def get_job_result(
    job_id: str,
    request: Request,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
) -> JobResultResponse:
    services = services_from(request)
    row = _job_for_current_user(request, job_id)
    status = JobStatus(str(row["status"]))
    if status not in {JobStatus.PENDING, JobStatus.RUNNING, JobStatus.SUCCESS}:
        raise ApiError(409, "job_not_successful", "Job result is not ready")
    result_set_id = _result_set_id_from_payload(row)
    if result_set_id is None:
        raise ApiError(404, "not_found", "Job result not found")
    rows = services.result_store.fetch_range(result_set_id, offset, limit)
    preview_rows, preview_truncated_cells = _preview_result_rows(rows)
    manifest = _spool_manifest_or_none(services, result_set_id)
    loaded_rows = _int_from_manifest(manifest, "loaded_rows")
    total_rows = _result_set_total_rows(services, result_set_id)
    state = _result_set_state(services, result_set_id)
    truncated = _bool_from_manifest(manifest, "truncated")
    has_more = bool(
        state == "streaming"
        or truncated
        or _bool_from_manifest(manifest, "has_more")
        or (loaded_rows is not None and offset + len(rows) < loaded_rows)
    )
    return JobResultResponse(
        job_id=job_id,
        result_set_id=result_set_id,
        offset=offset,
        limit=limit,
        columns=_columns_from_manifest(manifest),
        rows=[RowResponse(values=row.values) for row in preview_rows],
        loaded_rows=loaded_rows,
        total_rows=total_rows,
        state=state,
        truncated=truncated,
        has_more=has_more,
        page_size=_job_payload_int(row, "page_size", default=100),
        max_result_rows=_job_payload_int(
            row,
            "max_result_rows",
            fallback_key="max_rows",
            default=1000,
        ),
        preview_truncated_cells=preview_truncated_cells,
        pagination_mode=_manifest_pagination_mode(manifest),
        pagination_reason=_manifest_optional_str(manifest, "pagination_reason"),
    )


@router.post(
    "/jobs/{job_id}/pages",
    response_model=JobPageResponse,
    status_code=202,
)
def request_job_page(
    job_id: str,
    body: JobPageRequest,
    request: Request,
) -> JobPageResponse:
    """Enqueue one ordered, stateless database continuation page.

    Each page is a fresh read with the user's top-level ORDER BY and an AST
    LIMIT/OFFSET window. No cursor, connection, or credential remains live
    while the browser is idle. Queries without a top-level ORDER BY fail closed
    rather than returning silently unstable pages.
    """

    services = services_from(request)
    user = current_user_from(request)
    source = _job_for_current_user(request, job_id)
    root = _pagination_root_job(request, source)
    if str(root["kind"]) != JobKind.SQL_QUERY.value:
        raise ApiError(409, "pagination_unavailable", "Only SQL query results can be paged")
    root_payload = root["payload"]
    if not isinstance(root_payload, dict):
        raise ApiError(409, "pagination_unavailable", "Query pagination metadata is missing")
    result_set_id = _result_set_id_from_payload(root)
    if result_set_id is None:
        raise ApiError(404, "not_found", "Job result not found")
    manifest = _spool_manifest_or_none(services, result_set_id)
    if manifest is None:
        raise ApiError(409, "result_not_ready", "Query result is not ready")
    loaded_rows = _int_from_manifest(manifest, "loaded_rows") or 0
    if body.offset < loaded_rows:
        return JobPageResponse(
            job_id=str(source["id"]),
            result_set_id=result_set_id,
            offset=body.offset,
            cached=True,
        )
    if body.offset != loaded_rows:
        raise ApiError(409, "nonsequential_page", "Pages must be requested sequentially")
    if not _bool_from_manifest(manifest, "has_more"):
        raise ApiError(409, "no_more_rows", "No more rows are available")
    if manifest.get("pagination_mode") != "ordered_offset":
        raise ApiError(
            409,
            "stable_order_required",
            "Add a top-level ORDER BY before requesting another database page",
        )
    max_result_rows = _job_payload_int(
        root,
        "max_result_rows",
        fallback_key="max_rows",
        default=1000,
    )
    if body.offset >= max_result_rows:
        raise ApiError(409, "result_limit_reached", "The result safety limit was reached")

    root_job_id = str(root["id"])
    page_job_id = _pagination_job_id(root_job_id, body.offset)
    existing = services.job_backend.get_job(page_job_id)
    if existing is not None:
        return JobPageResponse(
            job_id=existing.id,
            result_set_id=result_set_id,
            offset=body.offset,
        )
    page_payload = dict(root_payload)
    page_payload.update(
        {
            "page_offset": body.offset,
            "pagination_root_job_id": root_job_id,
        }
    )
    page_job = Job(
        id=page_job_id,
        kind=JobKind.SQL_QUERY,
        status=JobStatus.PENDING,
        owner_user_id=user.id,
        project_id=str(root["project_id"]),
        datasource_ids=list(root["datasource_ids"] or []),
        priority=int(root["priority"]),
        timeout_seconds=int(root["timeout_seconds"]),
        resource_profile=ResourceProfile.model_validate(root["resource_profile"] or {}),
        audit_id=new_id(),
        payload=page_payload,
    )
    try:
        services.job_backend.enqueue(page_job)
    except IntegrityError:
        existing = services.job_backend.get_job(page_job_id)
        if existing is None:
            raise
    _audit_business(
        services,
        request,
        user_id=user.id,
        project_id=str(root["project_id"]),
        action="sql_page_request",
        resource_type="job",
        resource_id=page_job_id,
        result="accepted",
        detail={"root_job_id": root_job_id, "offset": body.offset},
    )
    return JobPageResponse(
        job_id=page_job_id,
        result_set_id=result_set_id,
        offset=body.offset,
    )


@router.post("/jobs/{job_id}/export", response_model=ExportCreateResponse, status_code=202)
def create_job_export(
    job_id: str,
    body: ExportCreateRequest,
    request: Request,
) -> ExportCreateResponse:
    services = services_from(request)
    user = current_user_from(request)
    source_row = _job_for_current_user(request, job_id)
    _require_exportable_source(source_row)
    source_result_set_id = _result_set_id_from_payload(source_row)
    if source_result_set_id is None:
        raise ApiError(404, "not_found", "Source job result not found")
    manifest = _spool_manifest_or_none(services, source_result_set_id)
    if manifest is None:
        raise ApiError(404, "not_found", "Source job result not found")
    _enforce_export_rate_limit(services, user.id)

    export_job_id = new_id()
    filename = _export_filename(job_id, body.format)
    content_type = export_content_type(body.format)
    download_token = token_urlsafe(32)
    token_hash = _download_token_hash(download_token)
    expires_at = datetime.now(UTC) + timedelta(seconds=services.download_url_ttl_seconds)
    source_db_type = _source_job_db_type(services, source_row)
    job = Job(
        id=export_job_id,
        kind=JobKind.RESULT_EXPORT,
        status=JobStatus.PENDING,
        owner_user_id=user.id,
        project_id=str(source_row["project_id"]),
        datasource_ids=_datasource_ids_from_row(source_row),
        priority=0,
        timeout_seconds=300,
        resource_profile=ResourceProfile(),
        audit_id=new_id(),
        payload={
            "source_job_id": job_id,
            "source_result_set_id": source_result_set_id,
            "format": body.format,
            "filename": filename,
            "table_name": body.table_name,
            "source_db_type": source_db_type,
        },
    )
    services.job_backend.enqueue(job)
    with services.engine.begin() as conn:
        conn.execute(
            insert(export_download_tokens).values(
                token_hash=token_hash,
                job_id=export_job_id,
                owner_user_id=user.id,
                format=body.format,
                filename=filename,
                content_type=content_type,
                expires_at=expires_at,
            )
        )
    _audit_business(
        services,
        request,
        user_id=user.id,
        project_id=str(source_row["project_id"]),
        action="sql_export",
        resource_type="job",
        resource_id=export_job_id,
        result="accepted",
        detail={"source_job_id": job_id, "format": body.format},
    )
    return ExportCreateResponse(
        job_id=export_job_id,
        download_token=download_token,
        expires_at=expires_at,
        format=body.format,
        filename=filename,
    )


@router.get("/exports/{token}")
def download_export(token: str, request: Request) -> StreamingResponse:
    services = services_from(request)
    user = current_user_from(request)
    now = datetime.now(UTC)
    token_hash = _download_token_hash(token)
    with services.engine.begin() as conn:
        row = (
            conn.execute(
                select(
                    export_download_tokens.c.token_hash,
                    export_download_tokens.c.owner_user_id,
                    export_download_tokens.c.filename,
                    export_download_tokens.c.content_type,
                    export_download_tokens.c.expires_at,
                    export_download_tokens.c.consumed_at,
                    jobs.c.id.label("job_id"),
                    jobs.c.status,
                    jobs.c.result_ref,
                    jobs.c.finished_at,
                )
                .select_from(
                    export_download_tokens.join(
                        jobs,
                        export_download_tokens.c.job_id == jobs.c.id,
                    )
                )
                .where(export_download_tokens.c.token_hash == token_hash)
            )
            .mappings()
            .one_or_none()
        )
        if row is None or str(row["owner_user_id"]) != user.id:
            raise ApiError(404, "not_found", "Export not found")
        if row["consumed_at"] is not None:
            raise ApiError(410, "download_token_consumed", "Download token has been used")
        expires_at = row["expires_at"]
        if not isinstance(expires_at, datetime):
            raise ApiError(410, "download_token_expired", "Download token has expired")
        # The download-token TTL clock runs from the export job's completion, not
        # from token creation. A long-running export could otherwise burn the whole
        # window before the file is even downloadable (backlog #96 #3). We only ever
        # extend the stored expiry (never shorten it), so fast exports are unchanged.
        deadline = _as_utc(expires_at)
        finished_at = row["finished_at"]
        if isinstance(finished_at, datetime):
            completion_deadline = _as_utc(finished_at) + timedelta(
                seconds=services.download_url_ttl_seconds
            )
            deadline = max(deadline, completion_deadline)
        if deadline <= now:
            raise ApiError(410, "download_token_expired", "Download token has expired")
        status = JobStatus(str(row["status"]))
        if status in {JobStatus.PENDING, JobStatus.RUNNING}:
            raise ApiError(409, "export_not_ready", "Export job is not ready")
        if status is not JobStatus.SUCCESS:
            raise ApiError(409, "export_failed", "Export job did not complete successfully")
        result_ref_payload = row["result_ref"]
        if not isinstance(result_ref_payload, dict):
            raise ApiError(404, "not_found", "Export file not found")
        result_ref = ResultRef.model_validate(result_ref_payload)
        try:
            stream = services.result_store.open_download(result_ref)
        except FileNotFoundError as exc:
            raise ApiError(404, "not_found", "Export file not found") from exc
        result = conn.execute(
            update(export_download_tokens)
            .where(export_download_tokens.c.token_hash == token_hash)
            .where(export_download_tokens.c.consumed_at.is_(None))
            .values(consumed_at=now)
        )
        if result.rowcount == 0:
            stream.close()
            raise ApiError(410, "download_token_consumed", "Download token has been used")

    filename = str(row["filename"])
    headers = {"Content-Disposition": _content_disposition(filename)}
    return StreamingResponse(
        stream,
        media_type=str(row["content_type"]),
        headers=headers,
        background=BackgroundTask(stream.close),
    )


@router.post("/jobs/{job_id}/cancel", response_model=CancelResponse)
def cancel_job(job_id: str, request: Request) -> CancelResponse:
    services = services_from(request)
    _job_for_current_user(request, job_id)
    services.job_backend.request_cancel(job_id)
    return CancelResponse(cancelled=True)


@router.get("/license/status", response_model=LicenseStatusResponse)
def get_license_status(request: Request) -> LicenseStatusResponse:
    services = services_from(request)
    with services.engine.connect() as conn:
        row = (
            conn.execute(select(license_state).where(license_state.c.id == 1))
            .mappings()
            .one_or_none()
        )
    if row is None:
        return LicenseStatusResponse(
            mode="trial",
            trial_days_remaining=30,
            license_enforcement_enabled=_license_enforcement_enabled(services),
        )

    expires_at = row["expires_at"] if isinstance(row["expires_at"], datetime) else None
    mode = str(row["mode"])
    return LicenseStatusResponse(
        mode=mode,
        edition=_optional_str(row["edition"]),
        customer=_optional_str(row["customer"]),
        expires_at=expires_at,
        features=list(row["features"] or []),
        repair_reason=_optional_str(row["repair_reason"]),
        trial_days_remaining=_trial_days_remaining(mode, expires_at),
        license_enforcement_enabled=_license_enforcement_enabled(services),
    )


def _user_by_username(services: ApiServices, username: str) -> RowMapping | None:
    with services.engine.connect() as conn:
        return (
            conn.execute(select(users).where(users.c.username == username)).mappings().one_or_none()
        )


def _sql_hash(sql: str) -> str:
    digest = sha256(sql.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def poll_retry_after_ms(*, terminal: bool, fresh: bool, queued: bool = False) -> int:
    """轮询调速(Session Broker 设计 §3.2)。

    **一份规则,两条路径共用** —— job progress 与语句 progress 各写一遍就是给
    "两套轮询契约漂移"开门。终态 0(停轮询)、有新版本 1000(跟得上产出)、
    没新版本 2000(退避)。`queued` 是会话语句独有的排队快档 500ms:语句排在
    lane mailbox 里,毫秒级就会轮到,退避到 2s 会让"点了没反应"。
    """

    if terminal:
        return 0
    if queued:
        return 500
    return 1000 if fresh else 2000


def _create_result_set_placeholder(
    services: ApiServices,
    *,
    result_set_id: str,
    job_id: str,
    console_id: str | None,
) -> None:
    now = datetime.now(UTC)
    result_ref = services.result_store.spool_ref(result_set_id)
    with services.engine.begin() as conn:
        conn.execute(
            insert(result_sets).values(
                id=result_set_id,
                execution_id=job_id,
                console_id=console_id,
                storage_ref=result_ref.model_dump(),
                columns=[],
                loaded_rows=0,
                total_rows=None,
                state="streaming",
                created_at=now,
                updated_at=now,
            )
        )


def _evict_console_resultsets(services: ApiServices, console_id: str) -> None:
    stale_ids: list[str] = []
    with services.engine.begin() as conn:
        active_ids = (
            conn.execute(
                select(result_sets.c.id)
                .where(result_sets.c.console_id == console_id)
                .where(result_sets.c.state != "closed")
                .order_by(result_sets.c.updated_at.desc(), result_sets.c.created_at.desc())
            )
            .scalars()
            .all()
        )
        max_active = max(1, services.max_active_resultsets_per_console)
        stale_ids = [str(value) for value in active_ids[max_active:]]
        if stale_ids:
            conn.execute(
                update(result_sets)
                .where(result_sets.c.id.in_(stale_ids))
                .values(state="closed", updated_at=datetime.now(UTC))
            )
    if stale_ids:
        _delete_closed_result_spools(services, stale_ids)


def _delete_closed_result_spools(services: ApiServices, result_set_ids: list[str]) -> None:
    started_at = time.monotonic()
    deleted = 0
    for result_set_id in result_set_ids:
        try:
            if services.result_store.delete_spool(result_set_id):
                deleted += 1
        except Exception:
            logger.exception(
                "closed result spool delete failed",
                result_set_id=result_set_id,
                elapsed_seconds=round(time.monotonic() - started_at, 3),
            )
    logger.info(
        "closed result spools deleted",
        closed_resultsets=len(result_set_ids),
        deleted_spools=deleted,
        elapsed_seconds=round(time.monotonic() - started_at, 3),
    )


def _result_set_row(services: ApiServices, result_set_id: str) -> RowMapping | None:
    """result_sets 整行。会话语句的 `timings/execution` 存在 `storage_ref.metadata`
    里(job 路径存在 `jobs.result_ref`),progress 镜像要从这里取。"""

    with services.engine.connect() as conn:
        return (
            conn.execute(select(result_sets).where(result_sets.c.id == result_set_id))
            .mappings()
            .one_or_none()
        )


def _result_set_metadata_section(row: RowMapping | None, key: str) -> dict[str, Any]:
    if row is None:
        return {}
    storage_ref = row["storage_ref"]
    if not isinstance(storage_ref, dict):
        return {}
    metadata = storage_ref.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    section = metadata.get(key)
    return section if isinstance(section, dict) else {}


def _result_set_state(services: ApiServices, result_set_id: str) -> str | None:
    with services.engine.connect() as conn:
        return conn.execute(
            select(result_sets.c.state).where(result_sets.c.id == result_set_id)
        ).scalar_one_or_none()


def _result_set_total_rows(services: ApiServices, result_set_id: str) -> int | None:
    with services.engine.connect() as conn:
        value = conn.execute(
            select(result_sets.c.total_rows).where(result_sets.c.id == result_set_id)
        ).scalar_one_or_none()
    return int(value) if value is not None else None


class _MetadataRequest:
    """Request-local metadata cache snapshot with one lazily built adapter."""

    def __init__(self, services: ApiServices, datasource_row: RowMapping) -> None:
        self.services = services
        self.datasource_row = datasource_row
        self.datasource_id = str(datasource_row["id"])
        self.now = datetime.now(UTC)
        self._cached: dict[_MetadataCacheKey, list[dict[str, object]]] = {}
        self._looked_up_keys: set[_MetadataCacheKey] = set()
        self._looked_up_levels: set[str] = set()
        self._adapter: DatabaseAdapter | None = None

    def prefetch_keys(self, keys: list[_MetadataCacheKey], *, refresh: bool) -> None:
        pending = list(
            dict.fromkeys(
                key
                for key in keys
                if key not in self._looked_up_keys and key[0] not in self._looked_up_levels
            )
        )
        if not pending:
            return
        self._looked_up_keys.update(pending)
        if refresh:
            return
        self._cached.update(
            _read_metadata_caches_for_keys(
                self.services,
                self.datasource_id,
                pending,
                now=self.now,
            )
        )

    def prefetch_levels(self, levels: list[str], *, refresh: bool) -> None:
        pending = list(
            dict.fromkeys(level for level in levels if level not in self._looked_up_levels)
        )
        if not pending:
            return
        self._looked_up_levels.update(pending)
        if refresh:
            return
        self._cached.update(
            _read_metadata_caches_for_levels(
                self.services,
                self.datasource_id,
                pending,
                now=self.now,
            )
        )

    def cached_payload(
        self, key: _MetadataCacheKey, *, refresh: bool
    ) -> list[dict[str, object]] | None:
        self.prefetch_keys([key], refresh=refresh)
        if refresh:
            return None
        return self._cached.get(key)

    def adapter(self) -> DatabaseAdapter:
        if self._adapter is None:
            self._adapter = _metadata_adapter(self.services, self.datasource_row)
        return self._adapter


def _metadata_payload(
    services: ApiServices,
    datasource_row: RowMapping,
    cache_level: str,
    *,
    schema_name: str,
    table_name: str,
    refresh: bool,
    load: Callable[[DatabaseAdapter], list[dict[str, object]]],
    metadata_request: _MetadataRequest | None = None,
) -> list[dict[str, object]]:
    datasource_id = str(datasource_row["id"])
    now = metadata_request.now if metadata_request is not None else datetime.now(UTC)
    cache_key = (cache_level, schema_name, table_name)
    if not refresh:
        if metadata_request is None:
            cached = _read_metadata_cache(
                services,
                datasource_id,
                cache_level,
                schema_name=schema_name,
                table_name=table_name,
                now=now,
            )
        else:
            cached = metadata_request.cached_payload(cache_key, refresh=False)
        if cached is not None:
            return cached
    try:
        adapter = (
            metadata_request.adapter()
            if metadata_request is not None
            else _metadata_adapter(services, datasource_row)
        )
        payload = load(adapter)
    except UnsupportedDbTypeError as exc:
        raise ApiError(
            400,
            "unsupported_db_type",
            "Metadata introspection supports MySQL and DM datasources",
        ) from exc
    except AdapterConnectionError as exc:
        raise ApiError(
            503,
            "metadata_probe_failed",
            "Datasource metadata probe failed",
        ) from exc
    except ValueError as exc:
        raise ApiError(400, "invalid_metadata_request", "Invalid metadata object name") from exc
    _write_metadata_cache(
        services,
        datasource_id,
        cache_level,
        schema_name=schema_name,
        table_name=table_name,
        payload=payload,
        now=now,
    )
    return payload


def _metadata_cache_payload(
    row: RowMapping,
    *,
    now: datetime,
) -> list[dict[str, object]] | None:
    expires_at = row["expires_at"]
    if isinstance(expires_at, datetime):
        normalized = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=UTC)
        if normalized <= now:
            return None
    payload = row["payload"]
    if not isinstance(payload, list):
        return None
    return [dict(item) for item in payload if isinstance(item, dict)]


def _metadata_cache_rows(
    rows: Sequence[RowMapping],
    *,
    now: datetime,
) -> dict[_MetadataCacheKey, list[dict[str, object]]]:
    cached: dict[_MetadataCacheKey, list[dict[str, object]]] = {}
    for row in rows:
        cache_level = row.get("cache_level")
        schema_name = row.get("schema_name")
        table_name = row.get("table_name")
        if all(isinstance(value, str) for value in (cache_level, schema_name, table_name)):
            key = (str(cache_level), str(schema_name), str(table_name))
        else:
            continue
        payload = _metadata_cache_payload(row, now=now)
        if payload is not None:
            cached[key] = payload
    return cached


def _read_metadata_caches_for_keys(
    services: ApiServices,
    datasource_id: str,
    keys: list[_MetadataCacheKey],
    *,
    now: datetime,
) -> dict[_MetadataCacheKey, list[dict[str, object]]]:
    if not keys:
        return {}
    key_conditions = [
        and_(
            metadata_caches.c.cache_level == cache_level,
            metadata_caches.c.schema_name == schema_name,
            metadata_caches.c.table_name == table_name,
        )
        for cache_level, schema_name, table_name in keys
    ]
    with services.engine.connect() as conn:
        rows = (
            conn.execute(
                select(
                    metadata_caches.c.cache_level,
                    metadata_caches.c.schema_name,
                    metadata_caches.c.table_name,
                    metadata_caches.c.payload,
                    metadata_caches.c.expires_at,
                ).where(
                    and_(
                        metadata_caches.c.datasource_id == datasource_id,
                        or_(*key_conditions),
                    )
                )
            )
            .mappings()
            .all()
        )
    return _metadata_cache_rows(rows, now=now)


def _read_metadata_caches_for_levels(
    services: ApiServices,
    datasource_id: str,
    levels: list[str],
    *,
    now: datetime,
) -> dict[_MetadataCacheKey, list[dict[str, object]]]:
    if not levels:
        return {}
    with services.engine.connect() as conn:
        rows = (
            conn.execute(
                select(
                    metadata_caches.c.cache_level,
                    metadata_caches.c.schema_name,
                    metadata_caches.c.table_name,
                    metadata_caches.c.payload,
                    metadata_caches.c.expires_at,
                ).where(
                    and_(
                        metadata_caches.c.datasource_id == datasource_id,
                        metadata_caches.c.cache_level.in_(levels),
                    )
                )
            )
            .mappings()
            .all()
        )
    return _metadata_cache_rows(rows, now=now)


def _read_metadata_cache(
    services: ApiServices,
    datasource_id: str,
    cache_level: str,
    *,
    schema_name: str,
    table_name: str,
    now: datetime,
) -> list[dict[str, object]] | None:
    entry = _read_metadata_cache_entry(
        services,
        datasource_id,
        cache_level,
        schema_name=schema_name,
        table_name=table_name,
        now=now,
    )
    return None if entry is None else entry[0]


def _read_metadata_cache_entry(
    services: ApiServices,
    datasource_id: str,
    cache_level: str,
    *,
    schema_name: str,
    table_name: str,
    now: datetime,
) -> tuple[list[dict[str, object]], datetime | None] | None:
    """缓存行的 payload **与写入时间**。

    `refreshed_at` 一直在写(`_write_metadata_cache`)却从没人读过。展开 `*` 时
    把它带给用户,是"不去猜表结构变没变、但把判断依据交到用户手上"的那一半。
    """
    with services.engine.connect() as conn:
        row = (
            conn.execute(
                select(
                    metadata_caches.c.payload,
                    metadata_caches.c.expires_at,
                    metadata_caches.c.refreshed_at,
                ).where(
                    and_(
                        metadata_caches.c.datasource_id == datasource_id,
                        metadata_caches.c.cache_level == cache_level,
                        metadata_caches.c.schema_name == schema_name,
                        metadata_caches.c.table_name == table_name,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        return None
    payload = _metadata_cache_payload(row, now=now)
    if payload is None:
        return None
    # 老缓存行(该列加进来之前写的)可能没有值 —— 未知就如实回 None,别编一个时间。
    refreshed_at = row.get("refreshed_at")
    return payload, _as_utc(refreshed_at) if isinstance(refreshed_at, datetime) else None


def _write_metadata_cache(
    services: ApiServices,
    datasource_id: str,
    cache_level: str,
    *,
    schema_name: str,
    table_name: str,
    payload: list[dict[str, object]],
    now: datetime,
) -> None:
    with services.engine.begin() as conn:
        conn.execute(
            delete(metadata_caches).where(
                and_(
                    metadata_caches.c.datasource_id == datasource_id,
                    metadata_caches.c.cache_level == cache_level,
                    metadata_caches.c.schema_name == schema_name,
                    metadata_caches.c.table_name == table_name,
                )
            )
        )
        conn.execute(
            insert(metadata_caches).values(
                id=new_id(),
                datasource_id=datasource_id,
                cache_level=cache_level,
                schema_name=schema_name,
                table_name=table_name,
                payload=payload,
                refreshed_at=now,
                expires_at=now + _METADATA_CACHE_TTL,
                created_at=now,
                updated_at=now,
            )
        )


def _metadata_adapter(services: ApiServices, datasource_row: RowMapping) -> DatabaseAdapter:
    timeout_seconds = services.metadata_probe_timeout_seconds
    return build_database_adapter(
        _datasource_conn_info(datasource_row),
        services.secret_store,
        connect_timeout_seconds=timeout_seconds,
        statement_timeout_seconds=timeout_seconds,
    )


def _datasource_conn_info(row: RowMapping) -> DatasourceConnInfo:
    database_name = row["database_name"]
    return DatasourceConnInfo(
        host=str(row["host"]),
        port=int(row["port"]),
        username=str(row["username"]),
        database=str(database_name) if database_name is not None else None,
        password_ref=SecretRef(
            ref=str(row["password_secret_ref"]),
            kind=SecretKind.DATASOURCE_PASSWORD,
        ),
        db_type=DbType(str(row["db_type"])),
        extra=dict(row["capability_profile"] or {}),
        operation_policy=OperationPolicy.model_validate(row["operation_policy"] or {}),
    )


def _metadata_columns_for_table(
    services: ApiServices,
    datasource_row: RowMapping,
    *,
    schema_name: str,
    table_name: str,
    refresh: bool,
    metadata_request: _MetadataRequest | None = None,
) -> list[Column]:
    payload = _metadata_payload(
        services,
        datasource_row,
        _METADATA_LEVEL_COLUMNS,
        schema_name=schema_name,
        table_name=table_name,
        refresh=refresh,
        load=lambda adapter: [
            _metadata_column_item(column).model_dump(mode="json")
            for column in adapter.list_columns(schema_name, table_name)
        ],
        metadata_request=metadata_request,
    )
    return [Column.model_validate(item) for item in payload]


def _metadata_indexes_for_table(
    services: ApiServices,
    datasource_row: RowMapping,
    *,
    schema_name: str,
    table_name: str,
    refresh: bool,
    metadata_request: _MetadataRequest | None = None,
) -> list[Index]:
    payload = _metadata_payload(
        services,
        datasource_row,
        _METADATA_LEVEL_INDEXES,
        schema_name=schema_name,
        table_name=table_name,
        refresh=refresh,
        load=lambda adapter: [
            _metadata_index_item(index).model_dump(mode="json")
            for index in adapter.list_indexes(schema_name, table_name)
        ],
        metadata_request=metadata_request,
    )
    return [Index.model_validate(item) for item in payload]


def _metadata_all_tables(
    services: ApiServices,
    datasource_row: RowMapping,
    *,
    refresh: bool,
    metadata_request: _MetadataRequest | None = None,
) -> list[Table]:
    request = metadata_request or _MetadataRequest(services, datasource_row)
    request.prefetch_levels(
        [_METADATA_LEVEL_SCHEMAS, _METADATA_LEVEL_TABLES],
        refresh=refresh,
    )
    schema_payload = _metadata_payload(
        services,
        datasource_row,
        _METADATA_LEVEL_SCHEMAS,
        schema_name="",
        table_name="",
        refresh=refresh,
        load=lambda adapter: [
            MetadataSchemaItem(name=item.name).model_dump(mode="json")
            for item in adapter.list_schemas()
        ],
        metadata_request=request,
    )
    tables: list[Table] = []
    for schema_item in schema_payload:
        schema_name = schema_item.get("name")
        if not isinstance(schema_name, str) or not schema_name:
            continue

        def load_tables_for_schema(
            adapter: DatabaseAdapter,
            schema: str = schema_name,
        ) -> list[dict[str, object]]:
            return [
                MetadataTableItem(
                    schema_name=item.schema_name,
                    name=item.name,
                    table_type=item.table_type,
                ).model_dump(mode="json")
                for item in adapter.list_tables(schema)
            ]

        table_payload = _metadata_payload(
            services,
            datasource_row,
            _METADATA_LEVEL_TABLES,
            schema_name=schema_name,
            table_name="",
            refresh=refresh,
            load=load_tables_for_schema,
            metadata_request=request,
        )
        tables.extend(Table.model_validate(item) for item in table_payload)
    return tables


def _metadata_column_item(column: Column) -> MetadataColumnItem:
    return MetadataColumnItem(
        name=column.name,
        type=column.type.value,
        driver_type=column.driver_type,
        nullable=column.nullable,
        primary_key=column.primary_key,
        comment=column.comment,
    )


def _metadata_index_item(index: Index) -> MetadataIndexItem:
    return MetadataIndexItem(
        name=index.name,
        columns=list(index.columns),
        is_unique=index.is_unique,
        is_primary=index.is_primary,
    )


def _audit_metadata(
    request: Request,
    services: ApiServices,
    datasource_row: RowMapping,
    action: str,
    datasource_id: str,
    refresh: bool,
) -> None:
    user = current_user_from(request)
    _audit_business(
        services,
        request,
        user_id=user.id,
        project_id=str(datasource_row["project_id"]),
        action=action,
        resource_type="datasource",
        resource_id=datasource_id,
        result="success",
        detail={"refresh": refresh},
    )


def _sqlglot_dialect(dialect: str) -> str:
    normalized = dialect.lower()
    if normalized in {"mysql", "oracle", "postgres", "postgresql"}:
        return "postgres" if normalized == "postgresql" else normalized
    if normalized == "dm":
        return "oracle"
    raise ApiError(
        400,
        "unsupported_dialect",
        "SQL formatting supports MySQL, DM, Oracle and PostgreSQL",
    )


def _expand_star_sql(
    services: ApiServices,
    datasource_row: RowMapping,
    sql: str,
    *,
    refresh: bool = False,
) -> tuple[str, datetime | None]:
    """展开 `*`,并回报这份列清单**最旧**那张缓存的写入时间。

    多表展开取最旧的一份:提示要按"最不新鲜的那份"说话,否则等于用最新的那张
    表给整条 SQL 背书。全部未知则回 None(旧缓存行可能没有 refreshed_at)。
    """
    dialect = _sqlglot_dialect(str(datasource_row["db_type"]))
    expression = sqlglot.parse_one(sql, read=dialect)
    select_expr = expression.find(exp.Select)
    if select_expr is None:
        raise ApiError(400, "invalid_sql", "Only SELECT SQL can be expanded")
    table_refs = _select_table_refs(datasource_row, select_expr)
    if not table_refs:
        raise ApiError(400, "invalid_sql", "SELECT must reference at least one table")

    changed = False
    oldest_refreshed_at: datetime | None = None
    expanded_selects: list[exp.Expression] = []

    def expand(table_ref: dict[str, str], qualifier: str | None) -> list[exp.Column]:
        nonlocal oldest_refreshed_at
        columns, refreshed_at = _column_expressions_for_ref(
            services, datasource_row, table_ref, qualifier, refresh=refresh
        )
        if refreshed_at is not None and (
            oldest_refreshed_at is None or refreshed_at < oldest_refreshed_at
        ):
            oldest_refreshed_at = refreshed_at
        return columns

    for item in list(select_expr.expressions):
        if isinstance(item, exp.Star):
            changed = True
            for table_ref in table_refs:
                expanded_selects.extend(expand(table_ref, None))
            continue
        if isinstance(item, exp.Column) and isinstance(item.this, exp.Star):
            table_name = item.table
            matched_table_ref = _table_ref_by_qualifier(table_refs, table_name)
            if matched_table_ref is None:
                raise ApiError(400, "unknown_table", "SELECT star qualifier does not match a table")
            changed = True
            expanded_selects.extend(expand(matched_table_ref, table_name))
            continue
        expanded_selects.append(item)
    if not changed:
        raise ApiError(400, "no_star", "SQL does not contain SELECT *")
    select_expr.set("expressions", expanded_selects)
    return expression.sql(dialect=dialect, pretty=True), oldest_refreshed_at


def _select_table_refs(
    datasource_row: RowMapping,
    select_expr: exp.Select,
) -> list[dict[str, str]]:
    default_schema = str(datasource_row["database_name"] or "")
    refs: list[dict[str, str]] = []
    for table in select_expr.find_all(exp.Table):
        schema = table.db or default_schema
        name = table.name
        if not schema or not name:
            continue
        refs.append(
            {
                "datasource_id": str(datasource_row["id"]),
                "schema": schema,
                "table": name,
                "alias": table.alias_or_name,
            }
        )
    return refs


def _table_ref_by_qualifier(
    refs: list[dict[str, str]],
    qualifier: str,
) -> dict[str, str] | None:
    for ref in refs:
        if qualifier in {ref["alias"], ref["table"]}:
            return ref
    return None


def _column_expressions_for_ref(
    services: ApiServices,
    datasource_row: RowMapping,
    table_ref: dict[str, str],
    qualifier: str | None,
    *,
    refresh: bool,
) -> tuple[list[exp.Column], datetime | None]:
    columns, refreshed_at = _metadata_columns_for_expand(
        services,
        datasource_row,
        schema_name=table_ref["schema"],
        table_name=table_ref["table"],
        refresh=refresh,
    )
    table_name = qualifier or table_ref["alias"]
    return (
        [exp.column(str(column["name"]), table=table_name, quoted=True) for column in columns],
        refreshed_at,
    )


def _metadata_columns_for_expand(
    services: ApiServices,
    datasource_row: RowMapping,
    *,
    schema_name: str,
    table_name: str,
    refresh: bool,
) -> tuple[list[dict[str, object]], datetime | None]:
    """展开 `*` 要用的列清单 + 这份清单的缓存写入时间。

    默认**纯缓存**:不建连、不 DESCRIBE,展开要么命中缓存要么 409 —— 这是当初
    把展开做成纯缓存刻意保留的失败模式,不能因为"想更准"就每次多一次建连往返。
    `refresh=True` 是**用户显式要的**那一次重读(此时才可能 503),不是系统自作主张。
    """
    now = datetime.now(UTC)
    if refresh:
        payload = _metadata_payload(
            services,
            datasource_row,
            _METADATA_LEVEL_COLUMNS,
            schema_name=schema_name,
            table_name=table_name,
            refresh=True,
            load=lambda adapter: [
                _metadata_column_item(column).model_dump(mode="json")
                for column in adapter.list_columns(schema_name, table_name)
            ],
        )
        return payload, now
    entry = _read_metadata_cache_entry(
        services,
        str(datasource_row["id"]),
        _METADATA_LEVEL_COLUMNS,
        schema_name=schema_name,
        table_name=table_name,
        now=now,
    )
    if entry is None:
        raise ApiError(
            409,
            "metadata_cache_missing",
            "Column metadata cache is missing; refresh metadata first",
        )
    return entry


def _console_for_current_user(request: Request, console_id: str) -> RowMapping:
    services = services_from(request)
    user = current_user_from(request)
    with services.engine.connect() as conn:
        row = (
            conn.execute(
                select(sql_consoles)
                .where(sql_consoles.c.id == console_id)
                .where(sql_consoles.c.owner_user_id == user.id)
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise ApiError(404, "not_found", "SQL console not found")
    return row


def _sql_console_response(row: RowMapping) -> SqlConsoleResponse:
    return SqlConsoleResponse(
        id=str(row["id"]),
        name=str(row["name"]),
        datasource_id=_optional_str(row["datasource_id"]),
        sql=str(row["sql"]),
        pinned=bool(row["pinned"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _require_admin(role: str) -> None:
    if role != "admin":
        raise ApiError(403, "forbidden", "Admin role required")


def _require_project_exists(conn: Connection, project_id: str) -> None:
    row = (
        conn.execute(select(projects.c.id).where(projects.c.id == project_id))
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise ApiError(404, "not_found", "Project not found")


def _template_for_read(services: ApiServices, template_id: str) -> RowMapping:
    with services.engine.connect() as conn:
        row = (
            conn.execute(select(sql_templates).where(sql_templates.c.id == template_id))
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise ApiError(404, "not_found", "SQL template not found")
    return row


def _sql_template_response(row: RowMapping) -> SqlTemplateResponse:
    return SqlTemplateResponse(
        id=str(row["id"]),
        name=str(row["name"]),
        description=_optional_str(row["description"]),
        sql_text=str(row["sql_text"]),
        variables=_normalize_variables(row["variables"] or []),
        category=str(row["category"]),
        project_id=_optional_str(row["project_id"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _compare_ref_projections(ref: object) -> tuple[CompareSqlProjection, ...]:
    if isinstance(ref, CompareDataRef):
        if ref.kind != "sql":
            return ()
        sql = ref.sql
    elif isinstance(ref, Mapping):
        if ref.get("kind") != "sql":
            return ()
        raw_sql = ref.get("sql")
        sql = raw_sql if isinstance(raw_sql, str) else None
    else:
        return ()
    if not sql:
        return ()
    try:
        return inspect_compare_sql(sql)
    except CompareSqlProjectionError:
        return ()


def _compare_projection_details(
    projections: tuple[CompareSqlProjection, ...],
) -> list[CompareProjectionDetail]:
    return [
        CompareProjectionDetail(
            name=item.name,
            generated=item.generated,
            projection_index=item.projection_index,
            expression=item.expression,
        )
        for item in projections
    ]


def _legacy_compare_aliases(columns: list[object], ref: object) -> dict[str, str]:
    names: list[str] = []
    for column in columns:
        if isinstance(column, Column):
            names.append(column.name)
        elif isinstance(column, Mapping) and isinstance(column.get("name"), str):
            names.append(str(column["name"]))
        else:
            names.append("")
    return legacy_generated_aliases(names, _compare_ref_projections(ref))


def _reject_stale_compare_aliases(columns: list[object], ref: object) -> None:
    if _legacy_compare_aliases(columns, ref):
        raise ApiError(
            409,
            "compare_sql_aliases_stale",
            "Compare SQL columns must be updated before running",
        )


def _compare_task_response(row: RowMapping) -> CompareTaskResponse:
    source_ref = row["source_ref"]
    target_ref = row["target_ref"]
    return CompareTaskResponse(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        name=str(row["name"]),
        source_id=_optional_str(row["source_id"]),
        target_id=_optional_str(row["target_id"]),
        source_ref=source_ref,
        target_ref=target_ref,
        columns=[Column.model_validate(item) for item in row["columns"] or []],
        source_projection_details=_compare_projection_details(_compare_ref_projections(source_ref)),
        target_projection_details=_compare_projection_details(_compare_ref_projections(target_ref)),
        compare_rules=CompareRulesPayload.model_validate(row["compare_rules"] or {}),
        run_limits=CompareRunLimitsPayload.model_validate(row["run_limits"] or {}),
        created_by=_optional_str(row["created_by"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _compare_task_for_current_user(request: Request, task_id: str) -> RowMapping:
    services = services_from(request)
    user = current_user_from(request)
    with services.engine.connect() as conn:
        row = (
            conn.execute(select(compare_tasks).where(compare_tasks.c.id == task_id))
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ApiError(404, "not_found", "Compare task not found")
        _require_project_access(conn, str(row["project_id"]), user.id)
        return row


def _compare_run_for_current_user(request: Request, run_or_job_id: str) -> RowMapping:
    services = services_from(request)
    user = current_user_from(request)
    with services.engine.connect() as conn:
        row = (
            conn.execute(
                select(run_index).where(
                    or_(
                        run_index.c.run_id == run_or_job_id,
                        run_index.c.job_id == run_or_job_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ApiError(404, "not_found", "Compare run not found")
        _require_project_access(conn, str(row["project_id"]), user.id)
        return row


def _validate_compare_task_datasources(
    conn: Connection,
    *,
    project_id: str,
    source_id: str | None,
    target_id: str | None,
    user_id: str,
) -> None:
    # 文件源对比(C-1):file 侧无 datasource(id 为 None),只校验给出的库侧 id。
    _require_project_access(conn, project_id, user_id)
    ds_ids = [ds_id for ds_id in (source_id, target_id) if ds_id is not None]
    if not ds_ids:
        return
    rows = (
        conn.execute(
            select(datasources.c.id, datasources.c.project_id).where(datasources.c.id.in_(ds_ids))
        )
        .mappings()
        .all()
    )
    by_id = {str(row["id"]): str(row["project_id"]) for row in rows}
    if any(by_id.get(ds_id) != project_id for ds_id in ds_ids):
        raise ApiError(404, "not_found", "Compare datasource not found")


def _validate_compare_file_uploads(
    conn: Connection,
    *,
    project_id: str,
    refs: list[CompareDataRef],
) -> None:
    """校验 file kind 的 ref 指向的 upload 属本项目且 purpose=compare_source。

    与 file-preview 端点同口径(所有权 + 用途),防止跨项目引用他人上传件。
    """

    for ref in refs:
        if ref.kind != "file":
            continue
        upload_row = (
            conn.execute(
                select(uploads.c.purpose).where(
                    and_(
                        uploads.c.id == ref.upload_id,
                        uploads.c.project_id == project_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if upload_row is None:
            raise ApiError(404, "not_found", "Compare upload not found")
        if str(upload_row["purpose"]) != "compare_source":
            raise ApiError(400, "invalid_upload_purpose", "Upload purpose must be compare_source")


def _validate_compare_result_snapshot_refs(
    services: ApiServices,
    *,
    project_id: str,
    user_id: str,
    refs: Sequence[object],
    retain_until: datetime,
    columns: Sequence[object] = (),
    compare_rules: Mapping[str, object] | None = None,
) -> dict[str, CompareResultInputDescriptor]:
    """验证 snapshot ref 的 actor/project/状态并续租,不读取业务行。"""

    module = CompareResultInputs(
        services.engine,
        services.result_store,
        ttl_days=services.result_ttl_days,
    )
    descriptors: dict[str, CompareResultInputDescriptor] = {}
    for side_index, ref in enumerate(refs):
        kind: object
        if isinstance(ref, CompareDataRef):
            kind = ref.kind
            input_id = ref.input_id
            allow_partial = ref.allow_partial
        elif isinstance(ref, Mapping):
            kind = ref.get("kind")
            raw_input_id = ref.get("input_id")
            input_id = raw_input_id if isinstance(raw_input_id, str) else None
            allow_partial = ref.get("allow_partial") is True
        else:
            continue
        if kind != "result_snapshot":
            continue
        if not input_id:
            raise ApiError(400, "invalid_result_snapshot_ref", "result_snapshot requires input_id")
        try:
            opened = module.open(
                input_id,
                scope=ActorProject(actor_id=user_id, project_id=project_id),
                batch_size=1,
                retain_until=retain_until,
            )
        except CompareResultInputError as exc:
            raise compare_result_input_api_error(exc) from exc
        descriptor = opened.descriptor
        if (descriptor.truncated or descriptor.has_more) and not allow_partial:
            raise ApiError(
                409,
                "result_snapshot_incomplete",
                "Partial result snapshot requires explicit confirmation",
            )
        _validate_result_snapshot_columns(
            descriptor,
            side_index=side_index,
            columns=columns,
            compare_rules=compare_rules or {},
        )
        descriptors[input_id] = descriptor
    return descriptors


def _validate_compare_effective_side(
    side: str,
    ref: CompareDataRef,
    datasource_id: str | None,
) -> None:
    if ref.kind in {"table", "sql"} and datasource_id is None:
        raise ApiError(
            400,
            "compare_datasource_required",
            f"{side} {ref.kind} ref requires a datasource",
        )
    if ref.kind == "result_snapshot" and datasource_id is not None:
        raise ApiError(
            400,
            "result_snapshot_datasource_forbidden",
            f"{side} result_snapshot must not define a datasource",
        )


def _validate_result_snapshot_columns(
    descriptor: CompareResultInputDescriptor,
    *,
    side_index: int,
    columns: Sequence[object],
    compare_rules: Mapping[str, object],
) -> None:
    configured: list[str] = []
    for item in columns:
        if isinstance(item, Column):
            if item.name:
                configured.append(item.name)
            continue
        if isinstance(item, Mapping):
            name = item.get("name")
            if isinstance(name, str) and name:
                configured.append(name)
    if not configured:
        return
    key_columns = compare_rules.get("key_columns")
    keys = [str(value) for value in key_columns] if isinstance(key_columns, list) else []
    ignore_columns = compare_rules.get("ignore_columns")
    ignored = (
        {str(value) for value in ignore_columns} if isinstance(ignore_columns, list) else set()
    )
    required = set(keys) | {name for name in configured if name not in ignored}
    if side_index == 1:
        raw_mappings = compare_rules.get("column_mappings")
        mappings = raw_mappings if isinstance(raw_mappings, dict) else {}
        required = {str(mappings.get(name, name)) for name in required}
    available = {column.name for column in descriptor.columns}
    missing = sorted(required - available)
    if missing:
        raise ApiError(
            409,
            "result_snapshot_columns_missing",
            "Result snapshot does not contain all configured compare columns",
            extra={"missing_columns": missing},
        )


def _resolve_compare_file_ref(
    conn: Connection,
    *,
    project_id: str,
    ref: dict[str, object],
) -> None:
    """把 file kind 的 ref(dict 形态)就地补上 storage_uri + filename(worker 回读用)。

    校验 upload 属本项目且 purpose=compare_source(与创建/预览同口径)。非 file
    kind 直接跳过。task 只存 upload_id,run 时解析,避免存原始路径(规避穿越)。
    """

    if ref.get("kind") != "file":
        return
    upload_id = ref.get("upload_id")
    if not isinstance(upload_id, str) or not upload_id:
        raise ApiError(400, "invalid_file_ref", "file ref requires upload_id")
    upload_row = (
        conn.execute(
            select(
                uploads.c.storage_uri,
                uploads.c.filename,
                uploads.c.purpose,
            ).where(
                and_(
                    uploads.c.id == upload_id,
                    uploads.c.project_id == project_id,
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if upload_row is None:
        raise ApiError(404, "not_found", "Compare upload not found")
    if str(upload_row["purpose"]) != "compare_source":
        raise ApiError(400, "invalid_upload_purpose", "Upload purpose must be compare_source")
    ref["storage_uri"] = str(upload_row["storage_uri"])
    ref["filename"] = str(upload_row["filename"])


def _compare_datasource_rows_for_project(
    services: ApiServices,
    *,
    project_id: str,
    source_id: str,
    target_id: str,
    user_id: str,
) -> tuple[RowMapping, RowMapping]:
    with services.engine.connect() as conn:
        _require_project_access(conn, project_id, user_id)
        rows = (
            conn.execute(select(datasources).where(datasources.c.id.in_([source_id, target_id])))
            .mappings()
            .all()
        )
    by_id = {str(row["id"]): row for row in rows}
    source_row = by_id.get(source_id)
    target_row = by_id.get(target_id)
    if (
        source_row is None
        or target_row is None
        or str(source_row["project_id"]) != project_id
        or str(target_row["project_id"]) != project_id
    ):
        raise ApiError(404, "not_found", "Compare datasource not found")
    return source_row, target_row


def _compare_inference_draft(
    services: ApiServices,
    body: CompareInferRequest,
    *,
    source_row: RowMapping,
    target_row: RowMapping,
    refresh: bool,
) -> CompareInferenceDraft:
    return infer_compare_draft(
        source_columns=_metadata_columns_for_table(
            services,
            source_row,
            schema_name=body.source_table.schema_name,
            table_name=body.source_table.table_name,
            refresh=refresh,
        ),
        target_columns=_metadata_columns_for_table(
            services,
            target_row,
            schema_name=body.target_table.schema_name,
            table_name=body.target_table.table_name,
            refresh=refresh,
        ),
        source_indexes=_metadata_indexes_for_table(
            services,
            source_row,
            schema_name=body.source_table.schema_name,
            table_name=body.source_table.table_name,
            refresh=refresh,
        ),
        target_indexes=_metadata_indexes_for_table(
            services,
            target_row,
            schema_name=body.target_table.schema_name,
            table_name=body.target_table.table_name,
            refresh=refresh,
        ),
    )


def _compare_infer_response(
    *,
    project_id: str,
    body: CompareInferRequest,
    draft: CompareInferenceDraft,
) -> CompareInferResponse:
    return CompareInferResponse(
        project_id=project_id,
        source_id=body.source_id,
        target_id=body.target_id,
        source_table=body.source_table,
        target_table=body.target_table,
        mappings=draft.mappings,
        pk_candidates=draft.pk_candidates,
        needs_manual_pk=draft.needs_manual_pk,
        compare_rules=CompareRulesPayload.model_validate(
            draft.compare_rules.model_dump(mode="json")
        ),
        columns=draft.columns,
    )


class _SampleUnavailable(Exception):
    """样本不可取(策略禁 SELECT / 标识符非法 / 查询失败);触发 L4 优雅降级为 L2。"""


def _compare_mapping_history(services: ApiServices, *, project_id: str) -> list[dict[str, Any]]:
    """聚合本项目既有 compare 任务已确认的 column_mappings(L2 历史映射决策)。

    为空时返回 [] —— route 层据此如实告知 AI "无历史",不假装有历史(提前交付语义)。
    """
    with services.engine.connect() as conn:
        rows = (
            conn.execute(
                select(compare_tasks.c.compare_rules).where(
                    compare_tasks.c.project_id == project_id
                )
            )
            .mappings()
            .all()
        )
    task_mappings: list[dict[str, str]] = []
    for row in rows:
        rules = row["compare_rules"]
        column_mappings = rules.get("column_mappings") if isinstance(rules, dict) else None
        if isinstance(column_mappings, dict):
            task_mappings.append(
                {
                    str(src): str(tgt)
                    for src, tgt in column_mappings.items()
                    if isinstance(src, str) and isinstance(tgt, str)
                }
            )
    return aggregate_mapping_history(task_mappings)


def _compare_ai_samples(
    services: ApiServices,
    request: Request,
    *,
    source_row: RowMapping,
    source_table: CompareTableRequest,
    residual_source_names: list[str],
    target_row: RowMapping,
    target_table: CompareTableRequest,
    residual_target_names: list[str],
) -> tuple[dict[str, Any] | None, str | None]:
    """拉两侧样本(仅残余列,≤MAX_SAMPLE_ROWS 行)。任一侧不可取 → (None, reason) 降级。"""
    try:
        source_columns, source_rows = _compare_fetch_sample_rows(
            services, request, datasource_row=source_row, table_req=source_table
        )
        target_columns, target_rows = _compare_fetch_sample_rows(
            services, request, datasource_row=target_row, table_req=target_table
        )
    except _SampleUnavailable as exc:
        return None, str(exc)
    payload = {
        "source_samples": project_sample_rows(source_columns, source_rows, residual_source_names),
        "target_samples": project_sample_rows(target_columns, target_rows, residual_target_names),
    }
    return payload, None


def _compare_fetch_sample_rows(
    services: ApiServices,
    request: Request,
    *,
    datasource_row: RowMapping,
    table_req: CompareTableRequest,
) -> tuple[list[str], list[list[Any]]]:
    """取一侧 ≤MAX_SAMPLE_ROWS 行样本(照 compare/preview 路径:秒级持有 cursor,不落库)。"""
    policy = dict(datasource_row["operation_policy"] or {})
    if not bool(policy.get("allow_select", False)):
        raise _SampleUnavailable("select_not_allowed")
    db_type = DbType(str(datasource_row["db_type"]))
    identifier = (
        f"{table_req.schema_name}.{table_req.table_name}"
        if table_req.schema_name
        else table_req.table_name
    )
    try:
        source_expr = quote_identifier(db_type, identifier)
    except ValueError as exc:
        raise _SampleUnavailable("invalid_identifier") from exc
    sql = f"SELECT * FROM {source_expr}{limit_clause(db_type, MAX_SAMPLE_ROWS)}"
    columns: list[str] = []
    adapter = build_database_adapter(
        _datasource_conn_info(datasource_row),
        services.secret_store,
        column_sink=lambda cols: columns.extend(column.name for column in cols),
        connect_timeout_seconds=services.metadata_probe_timeout_seconds,
        statement_timeout_seconds=services.metadata_probe_timeout_seconds,
    )
    rows: list[list[Any]] = []
    try:
        for data_row in adapter.execute_select(sql, {}):
            rows.append([_preview_value(value) for value in data_row.values])
            if len(rows) >= MAX_SAMPLE_ROWS:
                break
    except Exception as exc:
        # R5:driver 原始错误可能含 SQL 片段,不透传,只记类型;样本降级为 L2。
        logger.info(
            "compare ai sample query failed",
            request_id=request_id_from(request),
            error_type=type(exc).__name__,
        )
        raise _SampleUnavailable("sample_query_failed") from exc
    return columns, rows


def _compare_tasks_visible_to_user(user_id: str) -> Any:
    return compare_tasks.join(projects, compare_tasks.c.project_id == projects.c.id).outerjoin(
        project_members,
        and_(
            project_members.c.project_id == projects.c.id,
            project_members.c.user_id == user_id,
        ),
    )


def _compare_bucket_spools(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ApiError(404, "not_found", "Compare run result not found")
    spools: dict[str, str] = {}
    for bucket in COMPARE_BUCKETS:
        result_set_id = value.get(bucket)
        if not isinstance(result_set_id, str) or not result_set_id:
            raise ApiError(404, "not_found", "Compare run result not found")
        spools[bucket] = result_set_id
    return spools


def _compare_bucket_counts(value: object) -> dict[CompareBucket, int]:
    counts: dict[str, int] = empty_bucket_counts()
    if isinstance(value, dict):
        for bucket in COMPARE_BUCKETS:
            raw_count = value.get(bucket)
            if isinstance(raw_count, int):
                counts[bucket] = raw_count
    return cast(dict[CompareBucket, int], counts)


def _compare_progress(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    progress: dict[str, int] = {}
    for key, raw_value in value.items():
        if isinstance(key, str) and isinstance(raw_value, int):
            progress[key] = raw_value
    return progress


def _compare_diff_profile(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): raw_value for key, raw_value in value.items()}


def _compare_sample_result(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {str(key): raw_value for key, raw_value in value.items()}


def _lineage_datasource_for_project(
    conn: Connection,
    *,
    project_id: str,
    datasource_id: str,
    user_id: str,
) -> RowMapping:
    _require_project_access(conn, project_id, user_id)
    row = (
        conn.execute(select(datasources).where(datasources.c.id == datasource_id))
        .mappings()
        .one_or_none()
    )
    if row is None or str(row["project_id"]) != project_id:
        raise ApiError(404, "not_found", "Lineage datasource not found")
    return row


def _lineage_run_for_project(
    conn: Connection,
    *,
    project_id: str,
    run_id: str,
    user_id: str,
) -> RowMapping:
    """定位 run 并校验项目归属;越权 / 不存在一律 404(不泄露存在性)。"""
    _require_project_access(conn, project_id, user_id)
    row = (
        conn.execute(select(lineage_runs).where(lineage_runs.c.id == run_id))
        .mappings()
        .one_or_none()
    )
    if row is None or str(row["project_id"]) != project_id:
        raise ApiError(404, "not_found", "Lineage run not found")
    return row


def _lineage_schema_context(
    conn: Connection,
    datasource_id: str,
    default_schema: str | None,
) -> dict[str, Any]:
    rows = (
        conn.execute(
            select(
                metadata_caches.c.cache_level,
                metadata_caches.c.schema_name,
                metadata_caches.c.table_name,
                metadata_caches.c.payload,
            ).where(
                and_(
                    metadata_caches.c.datasource_id == datasource_id,
                    metadata_caches.c.cache_level == _METADATA_LEVEL_COLUMNS,
                )
            )
        )
        .mappings()
        .all()
    )
    schema_rows = [dict(row) for row in rows]
    schema = schema_from_metadata_cache_rows(schema_rows)
    return {"default_schema": default_schema, "schema": schema}


def _lineage_cached_run(
    conn: Connection,
    *,
    project_id: str,
    datasource_id: str,
    dialect: str,
    source_ref: str,
    sql_hash: str,
) -> RowMapping | None:
    return (
        conn.execute(
            select(lineage_runs).where(
                and_(
                    lineage_runs.c.project_id == project_id,
                    lineage_runs.c.datasource_id == datasource_id,
                    lineage_runs.c.dialect == dialect,
                    lineage_runs.c.source_ref == source_ref,
                    lineage_runs.c.sql_hash == sql_hash,
                    lineage_runs.c.parser_version == LINEAGE_PARSER_VERSION,
                    lineage_runs.c.superseded_at.is_(None),
                )
            )
        )
        .mappings()
        .one_or_none()
    )


def _supersede_lineage_cache(
    conn: Connection,
    *,
    project_id: str,
    datasource_id: str,
    dialect: str,
    source_ref: str,
    sql_hash: str,
    superseded_by: str,
) -> None:
    """0030:refresh 重解析把旧 run 标"已被取代"而不是删除——解析历史永久留痕。

    生效行(superseded_at IS NULL)最多一条(partial unique index),标记后新行才插得进去;
    旧行的边留在表里但被血缘图遍历的 NOT EXISTS 过滤掉,子图/影响/列级追溯行为不变。
    """
    conn.execute(
        update(lineage_runs)
        .where(
            and_(
                lineage_runs.c.project_id == project_id,
                lineage_runs.c.datasource_id == datasource_id,
                lineage_runs.c.dialect == dialect,
                lineage_runs.c.source_ref == source_ref,
                lineage_runs.c.sql_hash == sql_hash,
                lineage_runs.c.parser_version == LINEAGE_PARSER_VERSION,
                lineage_runs.c.superseded_at.is_(None),
            )
        )
        .values(superseded_at=func.now(), superseded_by=superseded_by)
    )


def _insert_lineage_edges(
    conn: Connection,
    *,
    run_id: str,
    project_id: str,
    sql_hash: str,
    graph_edges: list[dict[str, Any]],
    insert_mappings: list[dict[str, Any]],
) -> None:
    table_rows, column_rows = build_lineage_edge_rows(
        run_id=run_id,
        project_id=project_id,
        sql_hash=sql_hash,
        graph_edges=graph_edges,
        insert_mappings=insert_mappings,
        id_factory=new_id,
    )
    if table_rows:
        conn.execute(insert(lineage_edges), table_rows)
    if column_rows:
        conn.execute(insert(lineage_column_edges), column_rows)


_LINEAGE_AI_FALLBACK_PROMPT = (
    "You are given SQL statements that a deterministic SQL lineage parser failed to "
    "parse. String and numeric literals have been masked; only statement structure, "
    "table names and column names remain. Infer data lineage edges. Return strict "
    'JSON only, no prose: {"table_edges": [{"source_table": string, "target_table": '
    'string, "confidence": number}], "column_edges": [{"source_table": string, '
    '"source_column": string, "target_table": string, "target_column": string, '
    '"confidence": number}]}. Confidence is between 0 and 1. Use table names exactly '
    "as written (keep schema qualification). Return empty lists when lineage cannot "
    "be inferred. Never invent tables that do not appear in the statements."
)


def _run_lineage_ai_fallback(
    services: ApiServices,
    request: Request,
    *,
    user_id: str,
    project_id: str,
    run_id: str,
    sql_text: str,
    dialect: str,
    sql_hash: str,
    report: LineageReport,
    parse_summary: dict[str, Any],
) -> LineageAiFallbackResult:
    """AI 兜底解析(设计稿 §2.4 第 7 条)。

    任何失败(配置缺失 / AI 关闭 / gateway 异常 / 响应不可解析)都优雅降级:
    analyze 主路径已提交,这里只返回 executed=False + reason;deterministic
    parse_errors 永不被覆盖,AI 结果只能追加。
    """

    def skipped(reason: str, *, result: str = "skipped") -> LineageAiFallbackResult:
        _audit_business(
            services,
            request,
            user_id=user_id,
            project_id=project_id,
            action="lineage_ai_fallback",
            resource_type="lineage_run",
            resource_id=run_id,
            result=result,
            detail={"reason": reason},
        )
        return LineageAiFallbackResult(requested=True, executed=False, reason=reason)

    if not report.parse_errors:
        return skipped("no_parse_errors")
    try:
        with services.engine.connect() as conn:
            ai_row = _ai_config_row_or_none(conn)
        runtime = _ai_runtime_config(services, ai_row)
    except Exception as exc:
        return skipped(type(exc).__name__, result="failed")
    if not runtime.enabled or runtime.provider in {None, "off"}:
        return skipped("ai_disabled")
    fragments = fallback_statements(
        sql_text=sql_text,
        dialect=dialect,
        parse_errors=report.parse_errors,
    )
    if not fragments:
        return skipped("no_failed_statements")
    # ★ R-AI 姿态同 Compare 先例(profile_for_ai):只送脱敏后的内容,按 L2 出站
    # (§2.7.3 表格:Lineage AI 兜底最高 egress L2);字面量已在 domain 层遮蔽。
    context = AiContext(
        items=[
            ContextItem(
                content=json.dumps(
                    {"dialect": dialect, "statements": fragments},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                egress_level=EgressLevel.L2,
                redacted=True,
            )
        ]
    )
    try:
        ai_response = build_gateway_from_runtime_config(runtime).complete(
            _LINEAGE_AI_FALLBACK_PROMPT,
            context,
            AiOptions(purpose="lineage_ai_fallback", max_tokens=1200),
        )
    except AiGatewayError as exc:
        return skipped(type(exc).__name__, result="failed")
    edges = parse_inferred_edges(ai_response.content)
    if edges is None:
        return skipped("invalid_ai_response", result="failed")
    table_count, column_count = _persist_inferred_lineage_edges(
        services,
        run_id=run_id,
        project_id=project_id,
        sql_hash=sql_hash,
        edges=edges,
        report=report,
        parse_summary=parse_summary,
        provider=ai_response.provider,
        model=ai_response.model,
    )
    _audit_business(
        services,
        request,
        user_id=user_id,
        project_id=project_id,
        action="lineage_ai_fallback",
        resource_type="lineage_run",
        resource_id=run_id,
        result="success",
        detail={
            "provider": ai_response.provider,
            "inferred_table_edge_count": table_count,
            "inferred_column_edge_count": column_count,
        },
    )
    return LineageAiFallbackResult(
        requested=True,
        executed=True,
        inferred_table_edge_count=table_count,
        inferred_column_edge_count=column_count,
        provider=ai_response.provider,
        model=ai_response.model,
    )


def _persist_inferred_lineage_edges(
    services: ApiServices,
    *,
    run_id: str,
    project_id: str,
    sql_hash: str,
    edges: InferredLineageEdges,
    report: LineageReport,
    parse_summary: dict[str, Any],
    provider: str,
    model: str,
) -> tuple[int, int]:
    """落库 AI 推断边(inferred=True / inference_status='inferred')并更新 parse_summary。

    与确定性边去重:AI 返回的边若确定性解析已产出,跳过(不与确定性边同权重混淆)。
    parse_summary 只追加 ai_fallback 键,deterministic parse_errors 原样保留。
    """
    deterministic_table_keys = {
        (str(edge["source_table"]), str(edge["target_table"]))
        for edge in report.graph_edges
        if edge.get("source_table") and edge.get("target_table")
    }
    deterministic_column_keys = {
        (
            str(mapping["source_table"]),
            str(mapping["source_column"]),
            str(mapping["target_table"]),
            str(mapping["target_column"]),
        )
        for mapping in report.insert_mappings
        if (
            mapping.get("source_table")
            and mapping.get("source_column")
            and mapping.get("target_table")
            and mapping.get("target_column")
        )
    }
    table_rows = [
        {
            "id": new_id(),
            "run_id": run_id,
            "project_id": project_id,
            "source_table": edge.source_table,
            "target_table": edge.target_table,
            "edge_kind": "table",
            "inferred": True,
            "inference_status": "inferred",
            "confidence": edge.confidence,
            "sql_hash": sql_hash,
        }
        for edge in edges.table_edges
        if (edge.source_table, edge.target_table) not in deterministic_table_keys
    ]
    column_rows = [
        {
            "id": new_id(),
            "run_id": run_id,
            "project_id": project_id,
            "source_table": edge.source_table,
            "source_column": edge.source_column,
            "target_table": edge.target_table,
            "target_column": edge.target_column,
            "transformation": "DIRECT",
            "transformation_subtype": "DIRECT",
            "inferred": True,
            "inference_status": "inferred",
            "confidence": edge.confidence,
            "sql_hash": sql_hash,
        }
        for edge in edges.column_edges
        if (
            edge.source_table,
            edge.source_column,
            edge.target_table,
            edge.target_column,
        )
        not in deterministic_column_keys
    ]
    summary = dict(parse_summary)
    summary["ai_fallback"] = {
        "executed": True,
        "provider": provider,
        "model": model,
        "inferred_table_edge_count": len(table_rows),
        "inferred_column_edge_count": len(column_rows),
    }
    with services.engine.begin() as conn:
        if table_rows:
            conn.execute(insert(lineage_edges), table_rows)
        if column_rows:
            conn.execute(insert(lineage_column_edges), column_rows)
        conn.execute(
            update(lineage_runs)
            .where(lineage_runs.c.id == run_id)
            .values(parse_summary=summary, updated_at=datetime.now(UTC))
        )
    return len(table_rows), len(column_rows)


def _lineage_edge_row(conn: Connection, edge_id: str) -> tuple[SqlaTable, RowMapping | None]:
    """按 id 在表级 / 列级边表中定位边;返回 (边表, 行或 None)。"""
    row = (
        conn.execute(select(lineage_edges).where(lineage_edges.c.id == edge_id))
        .mappings()
        .one_or_none()
    )
    if row is not None:
        return lineage_edges, row
    column_row = (
        conn.execute(select(lineage_column_edges).where(lineage_column_edges.c.id == edge_id))
        .mappings()
        .one_or_none()
    )
    return lineage_column_edges, column_row


def _lineage_analyze_response(
    conn: Connection, row: RowMapping, *, cached: bool
) -> LineageAnalyzeResponse:
    run_id = str(row["id"])
    table_edge_count = int(
        conn.execute(
            select(func.count()).select_from(lineage_edges).where(lineage_edges.c.run_id == run_id)
        ).scalar_one()
    )
    column_edge_count = int(
        conn.execute(
            select(func.count())
            .select_from(lineage_column_edges)
            .where(lineage_column_edges.c.run_id == run_id)
        ).scalar_one()
    )
    return _lineage_analyze_response_from_counts(
        row,
        cached=cached,
        table_edge_count=table_edge_count,
        column_edge_count=column_edge_count,
    )


def _lineage_analyze_response_from_counts(
    row: RowMapping,
    *,
    cached: bool,
    table_edge_count: int,
    column_edge_count: int,
) -> LineageAnalyzeResponse:
    return LineageAnalyzeResponse(
        run_id=str(row["id"]),
        project_id=str(row["project_id"]),
        datasource_id=str(row["datasource_id"]),
        dialect=str(row["dialect"]),
        source_ref=str(row["source_ref"]),
        sql_hash=str(row["sql_hash"]),
        parser_version=str(row["parser_version"]),
        status=cast(Literal["success", "failed"], str(row["status"])),
        cached=cached,
        parse_summary=dict(row["parse_summary"] or {}),
        table_edge_count=table_edge_count,
        column_edge_count=column_edge_count,
    )


def _lineage_subgraph_response(
    conn: Connection,
    *,
    project_id: str,
    focus: str,
    direction: LineageDirection,
    max_depth: int,
    include_columns: bool,
) -> LineageSubgraphResponse:
    focus_ref = _lineage_focus_ref(conn, project_id, focus, include_columns=include_columns)
    rows: list[dict[str, Any]] = []
    directions: tuple[Literal["upstream", "downstream"], ...]
    if direction == "both":
        directions = ("upstream", "downstream")
    else:
        directions = (direction,)
    for item in directions:
        rows.extend(
            _lineage_walk_rows(
                conn,
                project_id=project_id,
                focus_ref=focus_ref,
                direction=item,
                max_depth=max_depth,
            )
        )
    if include_columns and focus_ref["kind"] == "table":
        rows.extend(
            _lineage_column_expansion_rows(
                conn,
                project_id=project_id,
                table_rows=rows,
                max_depth=max_depth,
            )
        )
    truncated = any(int(row["depth"]) > max_depth for row in rows)
    visible_rows = [row for row in rows if int(row["depth"]) <= max_depth]
    nodes, edges, depth_counts = _lineage_graph_from_rows(
        focus_ref=focus_ref,
        rows=visible_rows,
    )
    return LineageSubgraphResponse(
        project_id=project_id,
        focus=focus,
        direction=direction,
        max_depth=max_depth,
        include_columns=include_columns,
        truncated=truncated,
        node_count=len(nodes),
        edge_count=len(edges),
        depth_counts=depth_counts,
        nodes=nodes,
        edges=edges,
    )


def _lineage_impact_response(
    conn: Connection,
    *,
    project_id: str,
    focus: str,
    max_depth: int,
) -> LineageImpactResponse:
    focus_ref = _lineage_focus_ref(conn, project_id, focus, include_columns=True)
    rows = _lineage_walk_rows(
        conn,
        project_id=project_id,
        focus_ref=focus_ref,
        direction="downstream",
        max_depth=max_depth,
    )
    truncated = any(int(row["depth"]) > max_depth for row in rows)
    grouped: dict[str, LineageImpactItem] = {}
    for row in rows:
        depth = int(row["depth"])
        if depth > max_depth:
            continue
        target = str(row["target"])
        item = grouped.get(target)
        if item is None:
            table, column = _lineage_node_parts(target, focus_ref["kind"])
            item = LineageImpactItem(
                node=target,
                table=table,
                column=column,
                depth=depth,
                paths=[],
            )
            grouped[target] = item
        item.depth = min(item.depth, depth)
        path = _lineage_path(row)
        if path and path not in item.paths:
            item.paths.append(path)
    impacts = sorted(grouped.values(), key=lambda item: (item.depth, item.node))
    return LineageImpactResponse(
        project_id=project_id,
        focus=focus,
        max_depth=max_depth,
        truncated=truncated,
        impact_count=len(impacts),
        impacts=impacts,
    )


_LINEAGE_AI_IMPACT_PROMPT = (
    "You are a data lineage impact assistant. You are given a focus table/column, the "
    "downstream assets it impacts, per-asset reference-frequency statistics over a "
    "recent window, and the project owner — all as aggregate metadata (no row values, "
    "no SQL text). Produce a WEIGHTED, RANKED impact assessment: order the impacted "
    "assets by blast radius, combining graph proximity (smaller depth = higher) with "
    "reference frequency (higher ref_count / more recent = higher). For each ranked "
    "asset give a one-line reason citing its depth and frequency. If frequency data is "
    "sparse (ref_count is 0 for most or all assets), say so explicitly and rank "
    "primarily by depth, flagging lower confidence. Name the project owner as the "
    "person to notify when it is provided. Use only the provided names and statistics; "
    "never invent tables, columns, row values, or SQL. Be concise."
)


def _project_owner_username(conn: Connection, project_id: str) -> str | None:
    """项目负责人(C3 的"负责人"维度)—— 无 per-asset owner,用项目 owner 兜底。"""
    row = (
        conn.execute(
            select(users.c.username)
            .select_from(projects.join(users, users.c.id == projects.c.owner_user_id))
            .where(projects.c.id == project_id)
        )
        .mappings()
        .one_or_none()
    )
    return str(row["username"]) if row is not None else None


def _lineage_impact_signals(
    conn: Connection,
    *,
    project_id: str,
    impacts: list[LineageImpactItem],
    window_days: int,
) -> list[LineageImpactSignal]:
    """给每个受影响资产附上引用频率画像(按表聚合;同表多列共享频率)。"""
    tables = sorted({item.table for item in impacts})
    freq = _lineage_table_reference_frequency(
        conn,
        project_id=project_id,
        tables=tables,
        window_days=window_days,
    )
    now = datetime.now(UTC)
    signals: list[LineageImpactSignal] = []
    for item in impacts:
        stat = freq.get(item.table)
        ref_count = 0
        source_ref_count = 0
        days_ago: int | None = None
        if stat is not None:
            ref_count = int(stat["ref_count"])
            source_ref_count = int(stat["source_ref_count"])
            last = stat["last_referenced_at"]
            if isinstance(last, datetime):
                days_ago = max((now - _as_utc(last)).days, 0)
        signals.append(
            LineageImpactSignal(
                node=item.node,
                table=item.table,
                column=item.column,
                depth=item.depth,
                ref_count=ref_count,
                source_ref_count=source_ref_count,
                last_referenced_days_ago=days_ago,
            )
        )
    return signals


def _lineage_table_reference_frequency(
    conn: Connection,
    *,
    project_id: str,
    tables: list[str],
    window_days: int,
) -> dict[str, dict[str, Any]]:
    """近 window_days 天引用频率(可行路径:锚血缘运行历史,非 jobs 表)。

    一个表被一次 lineage_run 引用 = 该 run 的任一表级边以它为端点。按表聚合:
    - ref_count:去重 sql_hash 数(引用该表的不同 SQL 语句数)
    - source_ref_count:去重 source_ref 数(不同脚本/来源)
    - last_referenced_at:最近一次运行时间
    这是 C3 "运行频率" 维度在 2.4.0 既有数据面上可复现的信号;jobs 表不落
    表级归属,重解析成本高,故不用(PR body 记为裁决项)。
    """
    if not tables:
        return {}
    window_start = datetime.now(UTC) - timedelta(days=window_days)
    edge_refs = union_all(
        select(
            lineage_edges.c.source_table.label("table_name"),
            lineage_edges.c.run_id.label("run_id"),
        ).where(
            and_(
                lineage_edges.c.project_id == project_id,
                lineage_edges.c.source_table.in_(tables),
                lineage_edges.c.inference_status != "rejected",
                _lineage_active_run_clause(lineage_edges),
            )
        ),
        select(
            lineage_edges.c.target_table.label("table_name"),
            lineage_edges.c.run_id.label("run_id"),
        ).where(
            and_(
                lineage_edges.c.project_id == project_id,
                lineage_edges.c.target_table.in_(tables),
                lineage_edges.c.inference_status != "rejected",
                _lineage_active_run_clause(lineage_edges),
            )
        ),
    ).subquery("edge_refs")
    statement = (
        select(
            edge_refs.c.table_name,
            func.count(func.distinct(lineage_runs.c.sql_hash)).label("ref_count"),
            func.count(func.distinct(lineage_runs.c.source_ref)).label("source_ref_count"),
            func.max(lineage_runs.c.created_at).label("last_referenced_at"),
        )
        .select_from(edge_refs.join(lineage_runs, lineage_runs.c.id == edge_refs.c.run_id))
        .where(lineage_runs.c.created_at >= window_start)
        .group_by(edge_refs.c.table_name)
    )
    return {str(row["table_name"]): dict(row) for row in conn.execute(statement).mappings().all()}


def _lineage_export_sheets(
    subgraph: LineageSubgraphResponse,
    impact: LineageImpactResponse,
    *,
    include_columns: bool,
) -> list[tuple[str, list[Column], list[Row]]]:
    """血缘导出三 sheet(公式注入防护由 write_xlsx_workbook 内置)。

    impact 的 paths 每条用 " -> " 连成链,多条路径用 "; " 合并到一格。
    """
    table_columns = [
        Column(name="source"),
        Column(name="target"),
        Column(name="depth"),
        Column(name="direction"),
        Column(name="inferred"),
        Column(name="inference_status"),
        Column(name="confidence"),
    ]
    table_rows = [
        Row(
            values=[
                edge.source,
                edge.target,
                edge.depth,
                edge.direction,
                edge.inferred,
                edge.inference_status,
                edge.confidence,
            ]
        )
        for edge in subgraph.edges
        if edge.edge_kind != "column"
    ]
    sheets: list[tuple[str, list[Column], list[Row]]] = [("table_edges", table_columns, table_rows)]
    if include_columns:
        column_columns = [
            Column(name="source"),
            Column(name="target"),
            Column(name="transformation"),
            Column(name="subtype"),
            Column(name="inferred"),
            Column(name="confidence"),
        ]
        column_rows = [
            Row(
                values=[
                    edge.source,
                    edge.target,
                    edge.transformation,
                    edge.transformation_subtype,
                    edge.inferred,
                    edge.confidence,
                ]
            )
            for edge in subgraph.edges
            if edge.edge_kind == "column"
        ]
        sheets.append(("column_edges", column_columns, column_rows))
    impact_columns = [Column(name="node"), Column(name="depth"), Column(name="paths")]
    impact_rows = [
        Row(
            values=[
                item.node,
                item.depth,
                "; ".join(" -> ".join(path) for path in item.paths),
            ]
        )
        for item in impact.impacts
    ]
    sheets.append(("impact", impact_columns, impact_rows))
    return sheets


def _lineage_column_trace_response(
    conn: Connection,
    *,
    project_id: str,
    focus: str,
    focus_table: str,
    focus_column: str,
    direction: str,
    max_depth: int,
) -> LineageColumnTraceResponse:
    focus_ref = {"kind": "column", "table": focus_table, "column": focus_column, "node": focus}
    directions: list[Literal["upstream", "downstream"]] = (
        ["upstream", "downstream"]
        if direction == "both"
        else [cast(Literal["upstream", "downstream"], direction)]
    )
    truncated = False
    by_depth: dict[int, list[LineageColumnTraceItem]] = {}
    seen: set[tuple[int, str, str, str]] = set()
    for walk_direction in directions:
        rows = _lineage_walk_rows(
            conn,
            project_id=project_id,
            focus_ref=focus_ref,
            direction=walk_direction,
            max_depth=max_depth,
        )
        truncated = truncated or any(int(row["depth"]) > max_depth for row in rows)
        for row in rows:
            depth = int(row["depth"])
            if depth > max_depth:
                continue
            # 链式语义:本跳节点 = 走向端,from = 经由端(上一跳)
            if walk_direction == "downstream":
                node, from_node = str(row["target"]), str(row["source"])
            else:
                node, from_node = str(row["source"]), str(row["target"])
            key = (depth, walk_direction, node, from_node)
            if key in seen:
                continue
            seen.add(key)
            table, column = _lineage_split_column_focus(node)
            by_depth.setdefault(depth, []).append(
                LineageColumnTraceItem(
                    node=node,
                    table=table,
                    column=column or "",
                    from_node=from_node,
                    direction=walk_direction,
                    edge_id=str(row["edge_id"]),
                    transformation=_optional_str(row.get("transformation")),
                    transformation_subtype=_optional_str(row.get("transformation_subtype")),
                    inferred=bool(row.get("inferred")),
                    inference_status=str(row.get("inference_status") or "confirmed"),
                    confidence=float(row.get("confidence") or 1.0),
                )
            )
    hops = [
        LineageColumnTraceHop(
            depth=depth,
            items=sorted(by_depth[depth], key=lambda item: (item.direction, item.node)),
        )
        for depth in sorted(by_depth)
    ]
    return LineageColumnTraceResponse(
        project_id=project_id,
        focus=focus,
        direction=cast(Literal["upstream", "downstream", "both"], direction),
        max_depth=max_depth,
        truncated=truncated,
        hop_count=len(hops),
        hops=hops,
    )


def _lineage_focus_ref(
    conn: Connection,
    project_id: str,
    focus: str,
    *,
    include_columns: bool,
) -> dict[str, str]:
    if include_columns and not _lineage_table_exists(conn, project_id, focus):
        table, column = _lineage_split_column_focus(focus)
        if column is not None:
            return {"kind": "column", "table": table, "column": column, "node": focus}
    return {"kind": "table", "table": focus, "column": "", "node": focus}


def _lineage_active_run_clause(edge_table: SqlaTable) -> Any:
    """0030:边所属 lineage_run 必须"生效中"——已被取代的历史边不参与任何图查询。

    留痕前旧 run 会被 DELETE(边随 CASCADE 消失),留痕后必须显式过滤,否则同一脚本
    改版后新旧两版的边会同时出现在子图 / 影响 / 列级追溯里。
    """
    return ~exists(
        select(literal(1))
        .select_from(lineage_runs)
        .where(
            and_(
                lineage_runs.c.id == edge_table.c.run_id,
                lineage_runs.c.superseded_at.is_not(None),
            )
        )
    )


def _lineage_table_exists(conn: Connection, project_id: str, table_name: str) -> bool:
    exists = conn.execute(
        select(lineage_edges.c.id)
        .where(
            and_(
                lineage_edges.c.project_id == project_id,
                or_(
                    lineage_edges.c.source_table == table_name,
                    lineage_edges.c.target_table == table_name,
                ),
                _lineage_active_run_clause(lineage_edges),
            )
        )
        .limit(1)
    ).scalar_one_or_none()
    if exists is not None:
        return True
    column_exists = conn.execute(
        select(lineage_column_edges.c.id)
        .where(
            and_(
                lineage_column_edges.c.project_id == project_id,
                or_(
                    lineage_column_edges.c.source_table == table_name,
                    lineage_column_edges.c.target_table == table_name,
                ),
                _lineage_active_run_clause(lineage_column_edges),
            )
        )
        .limit(1)
    ).scalar_one_or_none()
    return column_exists is not None


def _lineage_split_column_focus(focus: str) -> tuple[str, str | None]:
    parts = focus.rsplit(".", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return focus, None
    return parts[0], parts[1]


def _lineage_walk_rows(
    conn: Connection,
    *,
    project_id: str,
    focus_ref: dict[str, str],
    direction: Literal["upstream", "downstream"],
    max_depth: int,
) -> list[dict[str, Any]]:
    query_depth = max_depth + 1
    # ADR-0019 red line: lineage graph queries must use focused recursive CTEs.
    # Do not load the whole graph and filter the neighborhood in Python or the frontend.
    if focus_ref["kind"] == "column":
        statement = (
            _LINEAGE_COLUMN_DOWNSTREAM_SQL
            if direction == "downstream"
            else _LINEAGE_COLUMN_UPSTREAM_SQL
        )
        params = {
            "project_id": project_id,
            "focus_table": focus_ref["table"],
            "focus_column": focus_ref["column"],
            "query_depth": query_depth,
            "direction": direction,
        }
    else:
        statement = (
            _LINEAGE_TABLE_DOWNSTREAM_SQL
            if direction == "downstream"
            else _LINEAGE_TABLE_UPSTREAM_SQL
        )
        params = {
            "project_id": project_id,
            "focus_table": focus_ref["table"],
            "query_depth": query_depth,
            "direction": direction,
        }
    return [dict(row) for row in conn.execute(statement, params).mappings().all()]


def _lineage_column_expansion_rows(
    conn: Connection,
    *,
    project_id: str,
    table_rows: list[dict[str, Any]],
    max_depth: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str, str]] = set()
    for table_row in table_rows:
        depth = int(table_row["depth"])
        if depth > max_depth:
            continue
        source_table = str(table_row["source_table"])
        target_table = str(table_row["target_table"])
        direction = str(table_row["direction"])
        pair_key = (source_table, target_table, direction)
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)
        column_rows = (
            conn.execute(
                select(lineage_column_edges).where(
                    and_(
                        lineage_column_edges.c.project_id == project_id,
                        lineage_column_edges.c.source_table == source_table,
                        lineage_column_edges.c.target_table == target_table,
                        lineage_column_edges.c.inference_status != "rejected",
                        _lineage_active_run_clause(lineage_column_edges),
                    )
                )
            )
            .mappings()
            .all()
        )
        for column_row in column_rows:
            source = f"{column_row['source_table']}.{column_row['source_column']}"
            target = f"{column_row['target_table']}.{column_row['target_column']}"
            path = [source, target] if direction == "downstream" else [target, source]
            rows.append(
                {
                    "edge_id": str(column_row["id"]),
                    "source_table": str(column_row["source_table"]),
                    "target_table": str(column_row["target_table"]),
                    "source_column": str(column_row["source_column"]),
                    "target_column": str(column_row["target_column"]),
                    "source": source,
                    "target": target,
                    "edge_kind": "column",
                    "transformation": str(column_row["transformation"]),
                    "transformation_subtype": str(column_row["transformation_subtype"]),
                    "inferred": bool(column_row["inferred"]),
                    "inference_status": str(column_row["inference_status"]),
                    "confidence": column_row["confidence"],
                    "depth": depth,
                    "path": path,
                    "direction": direction,
                }
            )
    return rows


def _lineage_graph_from_rows(
    *,
    focus_ref: dict[str, str],
    rows: list[dict[str, Any]],
) -> tuple[list[LineageSubgraphNode], list[LineageSubgraphEdge], dict[int, int]]:
    node_depths: dict[str, int] = {focus_ref["node"]: 0}
    node_kinds: dict[str, str] = {focus_ref["node"]: focus_ref["kind"]}
    edges_by_key: dict[tuple[str, str], LineageSubgraphEdge] = {}
    for row in rows:
        path = _lineage_path(row)
        row_kind = "column" if row.get("edge_kind") == "column" else "table"
        for depth, path_node in enumerate(path):
            node_depths[path_node] = min(node_depths.get(path_node, depth), depth)
            node_kinds[path_node] = row_kind
        edge = _lineage_edge_from_row(row)
        key = (edge.id, edge.direction)
        existing = edges_by_key.get(key)
        if existing is None or edge.depth < existing.depth:
            edges_by_key[key] = edge
    nodes = [
        _lineage_node(node_id, depth, node_kinds.get(node_id, "table"))
        for node_id, depth in sorted(node_depths.items(), key=lambda item: (item[1], item[0]))
    ]
    edges = sorted(edges_by_key.values(), key=lambda edge: (edge.depth, edge.id))
    depth_counts: dict[int, int] = {}
    for graph_node in nodes:
        depth_counts[graph_node.depth] = depth_counts.get(graph_node.depth, 0) + 1
    return nodes, edges, depth_counts


def _lineage_edge_from_row(row: dict[str, Any]) -> LineageSubgraphEdge:
    return LineageSubgraphEdge(
        id=str(row["edge_id"]),
        source=str(row["source"]),
        target=str(row["target"]),
        source_table=str(row["source_table"]),
        target_table=str(row["target_table"]),
        source_column=_optional_str(row.get("source_column")),
        target_column=_optional_str(row.get("target_column")),
        depth=int(row["depth"]),
        direction=cast(Literal["upstream", "downstream"], str(row["direction"])),
        edge_kind=str(row["edge_kind"]),
        inferred=bool(row["inferred"]),
        inference_status=str(row["inference_status"]),
        confidence=float(row["confidence"]),
        transformation=_optional_str(row.get("transformation")),
        transformation_subtype=_optional_str(row.get("transformation_subtype")),
    )


def _lineage_node(node_id: str, depth: int, kind: str) -> LineageSubgraphNode:
    table, column = _lineage_node_parts(node_id, kind)
    return LineageSubgraphNode(
        id=node_id,
        label=column or table,
        kind=cast(Literal["table", "column"], kind),
        table=table,
        column=column,
        depth=depth,
    )


def _lineage_node_parts(node_id: str, kind: str) -> tuple[str, str | None]:
    if kind != "column":
        return node_id, None
    table, column = _lineage_split_column_focus(node_id)
    return table, column


def _lineage_path(row: dict[str, Any]) -> list[str]:
    value = row.get("path")
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, tuple):
        return [str(item) for item in value]
    return []


def _row_value(row: RowMapping, key: str, default: object) -> object:
    try:
        return row[key]
    except KeyError:
        return default


def _ai_config_row_or_none(conn: Connection) -> RowMapping | None:
    return conn.execute(select(ai_configs).where(ai_configs.c.id == 1)).mappings().one_or_none()


def _ai_runtime_config(services: ApiServices, row: RowMapping | None) -> AiGatewayRuntimeConfig:
    if row is None:
        return AiGatewayRuntimeConfig()
    stored_ref = _optional_str(row["api_key_secret_ref"])
    api_key = None
    if stored_ref is not None:
        api_key = services.secret_store.reveal_secret(
            SecretRef(ref=stored_ref, kind=SecretKind.AI_API_KEY)
        )
    elif os.environ.get(AI_API_KEY_ENV):
        api_key = os.environ[AI_API_KEY_ENV]
    return AiGatewayRuntimeConfig(
        enabled=bool(row["enabled"]) and bool(row["enable_inference"]),
        provider=_optional_str(row["provider"]),
        endpoint=_optional_str(row["base_url"]),
        model=_optional_str(row["model"]),
        api_key=api_key,
        max_auto_egress_level=int(row["max_auto_egress_level"]),
        l4_requires_optin=bool(row["l4_requires_optin"]),
    )


def _normalize_variables(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        name = item.strip()
        if not name or name in seen:
            continue
        out.append(name)
        seen.add(name)
    return out


def _datasource_names(conn: Connection, datasource_ids: set[str]) -> dict[str, str]:
    if not datasource_ids:
        return {}
    rows = (
        conn.execute(
            select(datasources.c.id, datasources.c.name).where(datasources.c.id.in_(datasource_ids))
        )
        .mappings()
        .all()
    )
    return {str(row["id"]): str(row["name"]) for row in rows}


def _history_datasource_id(row: RowMapping, payload: dict[str, object]) -> str | None:
    value = payload.get("datasource_id")
    if isinstance(value, str):
        return value
    datasource_ids = row["datasource_ids"]
    if isinstance(datasource_ids, list) and datasource_ids:
        first = datasource_ids[0]
        return first if isinstance(first, str) else None
    return None


def _datasource_for_current_user(request: Request, datasource_id: str) -> RowMapping:
    services = services_from(request)
    user = current_user_from(request)
    with services.engine.connect() as conn:
        row = (
            conn.execute(select(datasources).where(datasources.c.id == datasource_id))
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ApiError(404, "not_found", "Datasource not found")
        _require_project_access(conn, str(row["project_id"]), user.id)
        return row


def _datasource_for_current_user_response(
    request: Request,
    datasource_id: str,
) -> DatasourceResponse:
    return _datasource_response(_datasource_for_current_user(request, datasource_id))


def _job_for_current_user(request: Request, job_id: str) -> RowMapping:
    services = services_from(request)
    user = current_user_from(request)
    with services.engine.connect() as conn:
        row = conn.execute(select(jobs).where(jobs.c.id == job_id)).mappings().one_or_none()
        if row is None:
            raise ApiError(404, "not_found", "Job not found")
        _require_project_access(conn, str(row["project_id"]), user.id)
        return row


def _require_project_access(conn: Connection, project_id: str, user_id: str) -> None:
    row = (
        conn.execute(
            select(projects.c.id)
            .outerjoin(
                project_members,
                and_(
                    project_members.c.project_id == projects.c.id,
                    project_members.c.user_id == user_id,
                ),
            )
            .where(projects.c.id == project_id)
            .where(or_(projects.c.owner_user_id == user_id, project_members.c.user_id == user_id))
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise ApiError(404, "not_found", "Project not found")


def _datasources_visible_to_user(user_id: str) -> Any:
    return datasources.join(projects, datasources.c.project_id == projects.c.id).outerjoin(
        project_members,
        and_(
            project_members.c.project_id == projects.c.id,
            project_members.c.user_id == user_id,
        ),
    )


def _jobs_visible_to_user(user_id: str) -> Any:
    return jobs.join(projects, jobs.c.project_id == projects.c.id).outerjoin(
        project_members,
        and_(
            project_members.c.project_id == projects.c.id,
            project_members.c.user_id == user_id,
        ),
    )


def _project_access_filter(user_id: str) -> Any:
    return or_(projects.c.owner_user_id == user_id, project_members.c.user_id == user_id)


def _wait_for_job_terminal(services: ApiServices, job_id: str) -> Job | None:
    deadline = time.monotonic() + services.job_wait_timeout_seconds
    while time.monotonic() < deadline:
        job = services.job_backend.get_job(job_id)
        if job and job.status in {JobStatus.SUCCESS, JobStatus.FAILED, JobStatus.CANCELLED}:
            return job
        time.sleep(0.2)
    services.job_backend.request_cancel(job_id)
    return None


def _datasource_response(row: RowMapping) -> DatasourceResponse:
    return DatasourceResponse(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        name=str(row["name"]),
        db_type=DbType(str(row["db_type"])),
        host=str(row["host"]),
        port=int(row["port"]),
        username=str(row["username"]),
        database=_optional_str(row["database_name"]),
        environment=str(row["environment"]),
        extra=dict(row["capability_profile"] or {}),
        operation_policy=OperationPolicy.model_validate(row["operation_policy"] or {}),
    )


def _datasource_list_item(row: RowMapping) -> DatasourceListItem:
    return DatasourceListItem(
        id=str(row["id"]),
        name=str(row["name"]),
        db_type=DbType(str(row["db_type"])),
        host=str(row["host"]),
        port=int(row["port"]),
        environment=str(row["environment"]),
        environment_verified=bool(row["environment_verified"]),
        database=row["database_name"] if row["database_name"] is not None else None,
        operation_policy=OperationPolicy.model_validate(row["operation_policy"] or {}),
        created_at=row["created_at"],
    )


def _datasource_test_error_code(job: Job | None) -> DatasourceTestErrorCode:
    if job is None or (
        job.status is JobStatus.FAILED and job.error == "worker heartbeat timed out"
    ):
        return "timeout"
    if job.status is JobStatus.CANCELLED:
        return "timeout"
    if job.status is JobStatus.FAILED:
        if job.error in _DATASOURCE_TEST_ERROR_CODES:
            return job.error
        return "unknown"
    return "unknown"


def _elapsed_ms(started_at: float) -> int:
    return max(0, round((time.monotonic() - started_at) * 1000))


def _metadata_str(metadata: dict[str, Any], key: str) -> str | None:
    value = metadata.get(key)
    return value if isinstance(value, str) else None


def _metadata_int(metadata: dict[str, Any], key: str) -> int | None:
    value = metadata.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _job_response(row: RowMapping) -> JobResponse:
    status = JobStatus(str(row["status"]))
    error_code = _job_error_code(row)
    return JobResponse(
        id=str(row["id"]),
        kind=str(row["kind"]),
        status=status,
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        result_set_id=_result_set_id_from_payload(row),
        error=error_code if status is JobStatus.FAILED else None,
        error_code=error_code,
        message="Job failed" if status is JobStatus.FAILED else None,
        timings=_job_stage_timings(row),
    )


def _job_stage_timings(row: RowMapping) -> JobStageTimings | None:
    created_at = row["created_at"]
    started_at = row["started_at"]
    finished_at = row["finished_at"]
    timings: dict[str, int | None] = {
        "queue_ms": _datetime_delta_ms(created_at, started_at),
        "connect_ms": None,
        "execute_first_row_ms": None,
        "fetch_ms": None,
        "spool_ms": None,
        "total_ms": _datetime_delta_ms(created_at, finished_at),
    }
    result_ref = row.get("result_ref")
    if isinstance(result_ref, dict):
        metadata = result_ref.get("metadata")
        raw_timings = metadata.get("timings") if isinstance(metadata, dict) else None
        if isinstance(raw_timings, dict):
            for key in ("connect_ms", "execute_first_row_ms", "fetch_ms", "spool_ms"):
                value = raw_timings.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    timings[key] = value
    if not any(value is not None for value in timings.values()):
        return None
    return JobStageTimings(**timings)


def _job_execution_metrics(
    row: RowMapping,
    manifest: dict[str, Any] | None,
) -> JobExecutionMetrics:
    payload = row["payload"] if isinstance(row["payload"], dict) else {}
    execution = _job_result_metadata_section(row, "execution")
    timings = _job_stage_timings(row)
    loaded_rows = _int_from_manifest(manifest, "loaded_rows")
    return JobExecutionMetrics(
        queued_at=_row_datetime(row, "created_at"),
        claimed_at=_row_datetime(row, "started_at"),
        connect_started_at=_dict_datetime(execution, "connect_started_at"),
        connected_at=_dict_datetime(execution, "connected_at"),
        execute_started_at=_dict_datetime(execution, "execute_started_at"),
        first_row_at=_dict_datetime(execution, "first_row_at"),
        first_batch_at=(
            _manifest_datetime(manifest, "first_batch_at")
            or _dict_datetime(execution, "first_batch_at")
        ),
        finished_reading_at=_dict_datetime(execution, "finished_reading_at"),
        finished_at=_row_datetime(row, "finished_at"),
        queue_ms=timings.queue_ms if timings else None,
        connect_ms=timings.connect_ms if timings else None,
        execute_to_first_row_ms=timings.execute_first_row_ms if timings else None,
        fetch_ms=timings.fetch_ms if timings else None,
        spool_ms=timings.spool_ms if timings else None,
        total_ms=timings.total_ms if timings else None,
        rows_read=_dict_int(execution, "rows_read"),
        rows_returned=_dict_int(execution, "rows_returned", fallback=loaded_rows),
        max_rows=_payload_positive_int(payload, "max_result_rows", fallback_key="max_rows"),
        limit_pushdown=_dict_bool_or_payload(execution, payload, "limit_pushdown"),
        limit_pushdown_reason=_dict_str_or_payload(execution, payload, "limit_pushdown_reason"),
        output_limit_applied=_dict_bool_or_payload(execution, payload, "output_limit_applied"),
        query_shape=_dict_str_or_payload(execution, payload, "query_shape"),
        effective_sql_hash=_dict_str(execution, "effective_sql_hash"),
        db_type=_dict_str_or_payload(execution, payload, "db_type"),
        worker_id=_row_optional_str(row, "worker_id"),
    )


def _job_result_metadata_section(row: RowMapping, key: str) -> dict[str, Any]:
    result_ref = row.get("result_ref")
    if not isinstance(result_ref, dict):
        return {}
    metadata = result_ref.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    value = metadata.get(key)
    return value if isinstance(value, dict) else {}


def _row_datetime(row: RowMapping, key: str) -> datetime | None:
    return _datetime_from_value(row.get(key))


def _row_optional_str(row: RowMapping, key: str) -> str | None:
    value = row.get(key)
    return value if isinstance(value, str) and value else None


def _dict_datetime(values: dict[str, Any], key: str) -> datetime | None:
    return _datetime_from_value(values.get(key))


def _manifest_datetime(manifest: dict[str, Any] | None, key: str) -> datetime | None:
    return _datetime_from_value(manifest.get(key) if manifest else None)


def _datetime_from_value(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, UTC)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _dict_int(values: dict[str, Any], key: str, *, fallback: int | None = None) -> int | None:
    value = values.get(key)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return fallback


def _payload_positive_int(
    payload: dict[str, Any],
    key: str,
    *,
    fallback_key: str | None = None,
) -> int | None:
    value = payload.get(key)
    if value is None and fallback_key is not None:
        value = payload.get(fallback_key)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _dict_bool_or_payload(
    values: dict[str, Any],
    payload: dict[str, Any],
    key: str,
) -> bool | None:
    value = values.get(key, payload.get(key))
    return value if isinstance(value, bool) else None


def _dict_bool(values: dict[str, Any], key: str) -> bool | None:
    value = values.get(key)
    return value if isinstance(value, bool) else None


def _dict_str(values: dict[str, Any], key: str) -> str | None:
    value = values.get(key)
    return value if isinstance(value, str) and value else None


def _dict_str_or_payload(
    values: dict[str, Any],
    payload: dict[str, Any],
    key: str,
) -> str | None:
    value = values.get(key, payload.get(key))
    return value if isinstance(value, str) and value else None


def _datetime_delta_ms(start: object, end: object) -> int | None:
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        return None
    return max(0, round((end - start).total_seconds() * 1000))


def _require_exportable_source(row: RowMapping) -> None:
    if str(row["kind"]) != JobKind.SQL_QUERY.value:
        raise ApiError(409, "unsupported_export_source", "Only SQL query jobs can be exported")
    if JobStatus(str(row["status"])) is not JobStatus.SUCCESS:
        raise ApiError(409, "job_not_successful", "Source job must complete before export")


def _require_compare_run_exportable(row: RowMapping) -> None:
    if JobStatus(str(row["status"])) is not JobStatus.SUCCESS:
        raise ApiError(409, "compare_run_not_successful", "Compare run must complete before export")


def _compare_run_export_max_rows(services: ApiServices, run_row: RowMapping) -> int | None:
    """读取关联 compare_task 的 run_limits.export_max_rows,供导出 job cap 每 sheet 行数。

    task 已删(task_id SET NULL)或未配置该项时返回 None,worker 侧回退到 Excel 单
    sheet 硬顶,保证不会产出超过 1,048,576 行的非法 xlsx。
    """
    task_id = run_row.get("task_id")
    if not task_id:
        return None
    with services.engine.connect() as conn:
        limits = conn.execute(
            select(compare_tasks.c.run_limits).where(compare_tasks.c.id == task_id)
        ).scalar_one_or_none()
    if not isinstance(limits, dict):
        return None
    value = limits.get("export_max_rows")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _enforce_export_rate_limit(services: ApiServices, user_id: str) -> None:
    since = datetime.now(UTC) - timedelta(hours=1)
    with services.engine.connect() as conn:
        count = conn.execute(
            select(func.count())
            .select_from(jobs)
            .where(jobs.c.owner_user_id == user_id)
            .where(jobs.c.kind == JobKind.RESULT_EXPORT.value)
            .where(jobs.c.created_at >= since)
        ).scalar_one()
    if int(count) >= services.export_per_user_per_hour:
        raise ApiError(429, "export_rate_limited", "Export rate limit exceeded")


def _download_token_hash(token: str) -> str:
    return sha256(token.encode("utf-8")).hexdigest()


def _export_filename(source_job_id: str, export_format: ExportFormat) -> str:
    if export_format not in _EXPORT_FORMATS:
        raise ApiError(400, "unsupported_export_format", "Unsupported export format")
    return f"{source_job_id}.{export_extension(export_format)}"


# 文件名词干上限。留足余量给时间戳 + run_id 短码 + 扩展名,避免 Windows MAX_PATH。
_EXPORT_STEM_MAX_CHARS = 60
# 控制字符(含 CR/LF —— 响应头注入)、路径分隔符、以及 Windows 文件名非法字符。
_EXPORT_ILLEGAL_CHARS = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')
_EXPORT_WHITESPACE = re.compile(r"\s+")
# Windows 保留设备名:即便带扩展名也不可用作文件名。
_WINDOWS_RESERVED_STEMS = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


def _safe_export_stem(raw: str | None, *, fallback: str) -> str:
    """把用户起的任务名收敛成可安全落盘、可安全进响应头的文件名词干。

    任务名是用户自由输入,直接拼进文件名有两处风险:落盘时的路径穿越,以及
    Content-Disposition 里的响应头注入(CR/LF、引号)。这里一次性收敛掉。
    保留中文等非 ASCII —— 可读性正是本次改动的目的,响应头侧用 RFC 5987 承载。
    """
    text_value = (raw or "").strip()
    text_value = _EXPORT_ILLEGAL_CHARS.sub("_", text_value)
    text_value = _EXPORT_WHITESPACE.sub("_", text_value)
    # 首尾的点/空格在 Windows 上非法;首点还会变成隐藏文件
    text_value = text_value.strip("._ ")
    text_value = text_value[:_EXPORT_STEM_MAX_CHARS].strip("._ ")
    if not text_value or text_value.lower() in _WINDOWS_RESERVED_STEMS:
        return fallback
    return text_value


def _content_disposition(filename: str) -> str:
    """attachment 响应头。

    同时给 ASCII 回退与 RFC 5987 的 filename*:老客户端取前者,现代浏览器取后者
    从而拿到中文原名。ASCII 回退里的引号/反斜杠必须转义 —— 否则用户在任务名里
    写个引号就能截断响应头。
    """
    ascii_name = filename.encode("ascii", "ignore").decode("ascii")
    ascii_name = _EXPORT_ILLEGAL_CHARS.sub("_", ascii_name).replace('"', "").replace("\\", "")
    if not ascii_name.strip("._ "):
        ascii_name = "export"
    encoded = quote(filename, safe="")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded}"


def _compare_export_filename(run_row: RowMapping, task_name: str | None) -> str:
    """对比导出文件名:任务名 + 时间戳 + run 短码。

    此前是裸 run_id(``d26a65e4-...-04806f01b233.xlsx``),下载一堆之后完全
    分不清谁是谁。时间戳让同一任务的多次导出可排序,run 短码保证不撞名且能
    追回具体那次 run。
    """
    run_id = str(run_row["run_id"])
    stem = _safe_export_stem(task_name, fallback="compare")
    created = run_row["created_at"] if "created_at" in run_row else None
    moment = created if isinstance(created, datetime) else datetime.now(UTC)
    stamp = moment.strftime("%Y%m%d-%H%M%S")
    return f"{stem}_{stamp}_{run_id[:8]}.xlsx"


def _datasource_ids_from_row(row: RowMapping) -> list[str]:
    value = row["datasource_ids"] if "datasource_ids" in row else []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item is not None]
    return []


def _source_job_db_type(services: ApiServices, row: RowMapping) -> str:
    datasource_ids = _datasource_ids_from_row(row)
    if not datasource_ids:
        return DbType.MYSQL.value
    with services.engine.connect() as conn:
        ds_row = (
            conn.execute(select(datasources.c.db_type).where(datasources.c.id == datasource_ids[0]))
            .mappings()
            .one_or_none()
        )
    if ds_row is None:
        return DbType.MYSQL.value
    return str(ds_row["db_type"])


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _job_list_item(row: RowMapping) -> JobListItem:
    status = JobStatus(str(row["status"]))
    return JobListItem(
        id=str(row["id"]),
        kind=str(row["kind"]),
        status=status,
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        error=_safe_job_error(status=status, kind=str(row["kind"]), row=row),
        error_code=_job_error_code(row),
    )


def _safe_job_error(*, status: JobStatus, kind: str, row: RowMapping) -> str | None:
    error_code = _job_error_code(row)
    if error_code is not None:
        return error_code
    stored_error = _optional_str(row["error"])
    if status is JobStatus.CANCELLED:
        return "cancelled"
    if status is JobStatus.TIMEOUT:
        return "query_timeout"
    if stored_error == "worker heartbeat timed out":
        return "query_timeout"
    if status is not JobStatus.FAILED:
        return None
    if kind == JobKind.TEST_CONNECTION.value:
        return "datasource_connection_failed"
    if kind == JobKind.SQL_QUERY.value:
        return "sql_execution_failed"
    if kind == JobKind.RESULT_EXPORT.value:
        return "export_failed"
    return "job_failed"


def _job_error_code(row: RowMapping) -> JobErrorCode | None:
    value = row["error_code"] if "error_code" in row else None
    return JobErrorCode(str(value)) if value is not None else None


def _datasource_job_references(
    conn: Connection,
    datasource_id: str,
) -> list[DatasourceReferenceItem]:
    rows = (
        conn.execute(
            select(jobs.c.id, jobs.c.kind, jobs.c.status)
            .where(
                and_(
                    jobs.c.datasource_ids.any(datasource_id),
                    or_(
                        jobs.c.kind != JobKind.TEST_CONNECTION.value,
                        jobs.c.status.notin_(
                            [
                                JobStatus.SUCCESS.value,
                                JobStatus.FAILED.value,
                                JobStatus.CANCELLED.value,
                                JobStatus.TIMEOUT.value,
                            ]
                        ),
                    ),
                )
            )
            .order_by(jobs.c.created_at.desc(), jobs.c.id.desc())
            .limit(20)
        )
        .mappings()
        .all()
    )
    return [
        DatasourceReferenceItem(
            job_id=str(row["id"]),
            kind=str(row["kind"]),
            status=JobStatus(str(row["status"])),
        )
        for row in rows
    ]


def _datasource_compare_task_references(
    conn: Connection,
    datasource_id: str,
) -> list[dict[str, str]]:
    rows = (
        conn.execute(
            select(compare_tasks.c.id, compare_tasks.c.name)
            .where(
                or_(
                    compare_tasks.c.source_id == datasource_id,
                    compare_tasks.c.target_id == datasource_id,
                )
            )
            .order_by(compare_tasks.c.updated_at.desc(), compare_tasks.c.id.desc())
            .limit(20)
        )
        .mappings()
        .all()
    )
    return [
        {
            "task_id": str(row["id"]),
            "kind": "compare_task",
            "name": str(row["name"]),
        }
        for row in rows
    ]


def _result_set_id_from_payload(row: RowMapping) -> str | None:
    payload = row["payload"]
    if not isinstance(payload, dict):
        return None
    value = payload.get("result_set_id")
    return value if isinstance(value, str) else None


def _job_payload_int(
    row: RowMapping,
    key: str,
    *,
    fallback_key: str | None = None,
    default: int,
) -> int:
    payload = row["payload"]
    if not isinstance(payload, dict):
        return default
    value = payload.get(key)
    if value is None and fallback_key is not None:
        value = payload.get(fallback_key)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return default


def _preview_result_rows(rows: list[Row]) -> tuple[list[Row], int]:
    preview_rows: list[Row] = []
    truncated_cells = 0
    for row in rows:
        values: list[Any] = []
        for value in row.values:
            preview, truncated = _preview_result_value(value)
            values.append(preview)
            truncated_cells += int(truncated)
        preview_rows.append(Row(values=values))
    return preview_rows, truncated_cells


def _preview_result_value(value: Any) -> tuple[Any, bool]:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<binary {len(value)} bytes; preview omitted>", True
    if isinstance(value, str) and len(value) > _RESULT_CELL_PREVIEW_MAX_CHARS:
        omitted = len(value) - _RESULT_CELL_PREVIEW_MAX_CHARS
        return f"{value[:_RESULT_CELL_PREVIEW_MAX_CHARS]}… <{omitted} chars omitted>", True
    return value, False


def _spool_manifest_or_none(
    services: ApiServices,
    result_set_id: str,
) -> dict[str, Any] | None:
    try:
        if not services.result_store.spool_exists(result_set_id):
            return None
        return services.result_store.get_spool_manifest(result_set_id)
    except Exception:
        logger.info("spool manifest unavailable", result_set_id=result_set_id, exc_info=True)
        return None


def _int_from_manifest(manifest: dict[str, Any] | None, key: str) -> int | None:
    value = manifest.get(key) if manifest else None
    return value if isinstance(value, int) else None


def _bool_from_manifest(manifest: dict[str, Any] | None, key: str) -> bool | None:
    value = manifest.get(key) if manifest else None
    return value if isinstance(value, bool) else None


def _manifest_optional_str(manifest: dict[str, Any] | None, key: str) -> str | None:
    value = manifest.get(key) if manifest else None
    return value if isinstance(value, str) else None


def _manifest_pagination_mode(
    manifest: dict[str, Any] | None,
) -> Literal["ordered_offset", "unavailable"] | None:
    value = _manifest_optional_str(manifest, "pagination_mode")
    if value in {"ordered_offset", "unavailable"}:
        return cast(Literal["ordered_offset", "unavailable"], value)
    return None


def _pagination_root_job(request: Request, source: RowMapping) -> RowMapping:
    payload = source["payload"]
    root_job_id = payload.get("pagination_root_job_id") if isinstance(payload, dict) else None
    if isinstance(root_job_id, str) and root_job_id:
        return _job_for_current_user(request, root_job_id)
    return source


def _pagination_job_id(root_job_id: str, offset: int) -> str:
    digest = sha256(f"{root_job_id}:{offset}".encode()).hexdigest()
    return f"page-{digest[:31]}"


def _columns_from_manifest(manifest: dict[str, Any] | None) -> list[Column]:
    raw_columns = manifest.get("columns") if manifest else None
    if not isinstance(raw_columns, list):
        return []
    return [Column.model_validate(column) for column in raw_columns if isinstance(column, dict)]


def _optional_str(value: object) -> str | None:
    return str(value) if value is not None else None


def _access_token_ttl_seconds(services: ApiServices) -> int:
    method = getattr(services, "access_token_ttl_seconds", None)
    return int(method()) if callable(method) else 3600


def _license_enforcement_enabled(services: ApiServices) -> bool:
    method = getattr(services, "license_enforcement_enabled", None)
    return bool(method()) if callable(method) else True


def _optional_stripped(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _trial_days_remaining(mode: str, expires_at: datetime | None) -> int | None:
    if mode != "trial":
        return None
    if expires_at is None:
        return 30
    now = datetime.now(UTC)
    normalized = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=UTC)
    remaining_seconds = (normalized - now).total_seconds()
    seconds_per_day = 24 * 60 * 60
    return max(0, int((remaining_seconds + seconds_per_day - 1) // seconds_per_day))


def _audit_business(
    services: ApiServices,
    request: Request,
    *,
    user_id: str | None,
    project_id: str | None,
    action: str,
    resource_type: str | None,
    resource_id: str | None,
    result: str,
    detail: dict[str, object] | None = None,
) -> None:
    client_ip = request.client.host if request.client else None
    services.write_audit(
        user_id=user_id,
        project_id=project_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        result=result,
        request_id=request_id_from(request),
        ip=client_ip,
        user_agent=request.headers.get("user-agent"),
        detail=detail,
    )
