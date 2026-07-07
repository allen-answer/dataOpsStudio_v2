from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from secrets import token_urlsafe
from typing import Any, BinaryIO, Literal, cast

import sqlglot
import structlog
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import ValidationError
from sqlalchemy import Table as SqlaTable
from sqlalchemy import and_, delete, func, insert, or_, select, text, update
from sqlalchemy.engine import Connection, RowMapping
from sqlglot import exp
from sqlglot.errors import ParseError
from starlette.background import BackgroundTask

from app.api.dependencies import current_user_from, request_id_from, services_from
from app.api.errors import ApiError
from app.api.routes.account import consume_recovery_code, verify_user_totp
from app.api.schemas import (
    CancelResponse,
    CompareAiAttributionResponse,
    CompareBucket,
    CompareDataRef,
    CompareExportCreateRequest,
    CompareFilePreviewRequest,
    CompareFilePreviewResponse,
    CompareInferRequest,
    CompareInferResponse,
    ComparePreviewRequest,
    ComparePreviewResponse,
    CompareResultRow,
    CompareRulesPayload,
    CompareRunCreateResponse,
    CompareRunLimitsPayload,
    CompareRunProfileResponse,
    CompareRunResultResponse,
    CompareSuggestedTaskCreateRequest,
    CompareTaskCreateRequest,
    CompareTaskResponse,
    CompareTaskRunItem,
    CompareTaskRunsResponse,
    CompareTaskSuggestionResponse,
    CompareTaskUpdateRequest,
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
    JobListItem,
    JobResponse,
    JobResultResponse,
    LicenseStatusResponse,
    LineageAiFallbackResult,
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
    LineageSubgraphEdge,
    LineageSubgraphNode,
    LineageSubgraphResponse,
    LoginRequest,
    MetadataColumnItem,
    MetadataIndexItem,
    MetadataSchemaItem,
    MetadataTableItem,
    ProjectResponse,
    RowResponse,
    SqlConsoleCreateRequest,
    SqlConsoleResponse,
    SqlConsoleUpdateRequest,
    SqlExecuteRequest,
    SqlExecuteResponse,
    SqlExpandStarRequest,
    SqlExpandStarResponse,
    SqlFormatRequest,
    SqlFormatResponse,
    SqlHistoryItem,
    SqlTemplateCreateRequest,
    SqlTemplateRenderRequest,
    SqlTemplateRenderResponse,
    SqlTemplateResponse,
    SqlTemplateUpdateRequest,
    TokenResponse,
    UploadPurpose,
    UploadResponse,
    WorkflowCreateRequest,
    WorkflowListItem,
    WorkflowResponse,
    WorkflowRunCreateResponse,
    WorkflowRunNodeItem,
    WorkflowRunStatusResponse,
    WorkflowUpdateRequest,
)
from app.api.security import create_access_token
from app.api.services import ApiServices, new_id
from app.db.models import (
    ai_configs,
    compare_tasks,
    datasources,
    export_download_tokens,
    jobs,
    license_state,
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
from app.dbclients.sql_build import limit_clause, quote_identifier
from app.dbclients.sql_guard import SqlGuardError, validate_readonly_sql
from app.domain.ai import AiContext, AiOptions, ContextItem, EgressLevel
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
from app.domain.datasource import DatasourceConnInfo, DbType, OperationPolicy
from app.domain.job import Job, JobErrorCode, JobKind, JobStatus
from app.domain.lineage import (
    LINEAGE_PARSER_VERSION,
    LineageParseRequest,
    LineageReport,
    analyze_sql_lineage,
    lineage_sql_hash,
    schema_from_metadata_cache_rows,
)
from app.domain.lineage.ai_fallback import (
    InferredLineageEdges,
    fallback_statements,
    parse_inferred_edges,
)
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
from app.domain.workflow import WorkflowSpec
from app.domain.workflow_execution import (
    TERMINAL_NODE_STATUSES,
    WorkflowChildJob,
    WorkflowNodeExecStatus,
    plan_workflow_step,
)
from app.domain.workflow_when import builtin_when_variables, when_variables_from_payload
from app.infrastructure.jobbackend.postgres import PostgresJobBackend
from app.infrastructure.result_export import (
    ExportSizeLimitExceeded,
    export_content_type,
    export_extension,
    write_xlsx_workbook,
)
from app.services.ai.default_gateway import (
    AI_API_KEY_ENV,
    AiGatewayRuntimeConfig,
    build_gateway_from_runtime_config,
)
from app.services.ai.errors import AiGatewayError
from app.services.lineage_batch_export import batch_export_sheets

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
_EXPORT_FORMATS = frozenset({"csv", "excel", "json", "sql"})
# 血缘导出同步生成(子图 depth<=5 数据量小),不复用 worker 的 export_limit_mb 配置;
# 50MB 上限只是防御性护栏,正常子图导出远小于此。
_LINEAGE_EXPORT_LIMIT_BYTES = 50 * 1024 * 1024
_COMPARE_BUCKET_QUERY = Query(default="diff")
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
        "forbidden_node_kind",
        "unsupported_node_kind",
        "unsupported_on_failure",
        "invalid_node_id",
        "invalid_when",
        "invalid_cron",
        "duplicate_node_id",
        "unknown_edge_node",
        "self_loop",
        "cycle_detected",
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
    with services.engine.begin() as conn:
        _require_project_access(conn, body.project_id, user.id)
        secret_ref = services.secret_store.store_secret(
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
                database_name=body.database,
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
        database=body.database,
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
        if body.database is not None:
            values["database_name"] = body.database
        if body.environment is not None:
            values["environment"] = body.environment
        if body.extra is not None:
            values["capability_profile"] = body.extra
        if body.operation_policy is not None:
            values["operation_policy"] = body.operation_policy.model_dump()
        if body.password:
            secret_ref = services.secret_store.store_secret(
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
        load=lambda: [
            MetadataSchemaItem(name=item.name).model_dump(mode="json")
            for item in _metadata_adapter(services, row).list_schemas()
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
        load=lambda: [
            MetadataTableItem(
                schema_name=item.schema_name,
                name=item.name,
                table_type=item.table_type,
            ).model_dump(mode="json")
            for item in _metadata_adapter(services, row).list_tables(schema)
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
        load=lambda: [
            _metadata_column_item(column).model_dump(mode="json")
            for column in _metadata_adapter(services, row).list_columns(schema, table)
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
        load=lambda: [
            _metadata_index_item(index).model_dump(mode="json")
            for index in _metadata_adapter(services, row).list_indexes(schema, table)
        ],
    )
    _audit_metadata(request, services, row, "metadata_indexes_list", datasource_id, refresh)
    return [MetadataIndexItem.model_validate(item) for item in payload]


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
        expanded = _expand_star_sql(services, row, body.sql)
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
        detail={"sql_len": len(body.sql)},
    )
    return SqlExpandStarResponse(expanded_sql=expanded)


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
    services = services_from(request)
    user = current_user_from(request)
    filters = [jobs.c.owner_user_id == user.id, jobs.c.kind == JobKind.SQL_QUERY.value]
    if datasource_id is not None:
        filters.append(jobs.c.datasource_ids.any(datasource_id))
    if created_after is not None:
        filters.append(jobs.c.created_at >= created_after)
    if created_before is not None:
        filters.append(jobs.c.created_at <= created_before)
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
        datasource_ids = {
            item for row in rows for item in (row["datasource_ids"] or []) if isinstance(item, str)
        }
        datasource_names = _datasource_names(conn, datasource_ids)
    out: list[SqlHistoryItem] = []
    seen: set[str] = set()
    for row in rows:
        payload = row["payload"] if isinstance(row["payload"], dict) else {}
        sql = payload.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            continue
        digest = payload.get("sql_hash")
        sql_hash = digest if isinstance(digest, str) else _sql_hash(sql)
        if sql_hash in seen:
            continue
        seen.add(sql_hash)
        row_datasource_id = _history_datasource_id(row, payload)
        row_datasource_name = (
            datasource_names.get(row_datasource_id) if row_datasource_id is not None else None
        )
        out.append(
            SqlHistoryItem(
                job_id=str(row["id"]),
                datasource_id=row_datasource_id,
                datasource_name=row_datasource_name,
                sql=sql,
                sql_hash=sql_hash,
                status=JobStatus(str(row["status"])),
                created_at=row["created_at"],
                finished_at=row["finished_at"],
                result_set_id=_result_set_id_from_payload(row),
            )
        )
        if len(out) >= limit:
            break
    return out


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
    now = datetime.now(UTC)
    task_id = new_id()
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
    if body.ref.kind == "sql":
        try:
            inner_sql = validate_readonly_sql(body.ref.sql or "")
        except SqlGuardError as exc:
            raise ApiError(400, "invalid_sql", "Only read-only SELECT/WITH SQL is allowed") from exc
        # 与 worker _compare_table_expression 同形:子查询别名不带引用
        source_expr = f"({inner_sql}) DATAOPS_PREVIEW"
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
        rows=rows_out,
        row_count=len(rows_out),
        truncated=truncated,
    )


def _preview_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


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
        source_id = body.source_id or str(row["source_id"])
        target_id = body.target_id or str(row["target_id"])
        if body.source_id is not None or body.target_id is not None:
            _validate_compare_task_datasources(
                conn,
                project_id=project_id,
                source_id=source_id,
                target_id=target_id,
                user_id=user.id,
            )
        changed_refs = [ref for ref in (body.source_ref, body.target_ref) if ref is not None]
        if changed_refs:
            _validate_compare_file_uploads(conn, project_id=project_id, refs=changed_refs)
        values: dict[str, object] = {}
        if body.name is not None:
            values["name"] = body.name
        if body.source_id is not None:
            values["source_id"] = body.source_id
        if body.target_id is not None:
            values["target_id"] = body.target_id
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
    # 文件侧:把 upload_id 解析成 storage_uri + filename 注入 ref(worker 回读用),
    # 与 lineage_batch 同范式(API 侧解析上传件,worker 不直连 uploads 表)。
    with services.engine.connect() as conn:
        _resolve_compare_file_ref(conn, project_id=project_id, ref=source_ref)
        _resolve_compare_file_ref(conn, project_id=project_id, ref=target_ref)

    job_id = new_id()
    run_id = new_id()
    bucket_spools = {bucket: new_id() for bucket in COMPARE_BUCKETS}
    timeout_seconds = min(int(limits.get("query_timeout_seconds") or 1800), 3600)
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
    now = datetime.now(UTC)
    with services.engine.begin() as conn:
        _enqueue_compare_run_job(conn, services, job)
        conn.execute(
            insert(run_index).values(
                run_id=run_id,
                kind=JobKind.COMPARE_RUN.value,
                job_id=job_id,
                owner_user_id=user.id,
                project_id=str(row["project_id"]),
                task_id=task_id,
                status=JobStatus.PENDING.value,
                result_path=f"compare/{run_id}",
                bucket_spools=bucket_spools,
                bucket_counts=empty_bucket_counts(),
                progress={},
                created_at=now,
                updated_at=now,
            )
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
    filename = _compare_export_filename(str(run_row["run_id"]))
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
        dialect = (body.dialect or str(datasource_row["db_type"])).lower()
        schema_context = _lineage_schema_context(conn, body.datasource_id, body.default_schema)
        sql_hash = lineage_sql_hash(
            sql_text=body.sql_text,
            dialect=dialect,
            schema_context=schema_context,
            parser_version=LINEAGE_PARSER_VERSION,
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
        else:
            _delete_lineage_cache(
                conn,
                project_id=project_id,
                datasource_id=body.datasource_id,
                dialect=dialect,
                source_ref=body.source_ref,
                sql_hash=sql_hash,
            )

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
        parse_summary = dict(report.report or {})
        parse_summary["parser_version"] = LINEAGE_PARSER_VERSION
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
                enabled=True,
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
    spec = _validated_workflow_spec(body.spec)
    with services.engine.begin() as conn:
        _require_project_access(conn, project_id, user.id)
        _workflow_row_for_project(conn, project_id=project_id, workflow_id=workflow_id)
        _require_workflow_name_available(
            conn,
            project_id=project_id,
            name=body.name,
            exclude_workflow_id=workflow_id,
        )
        conn.execute(
            update(workflows)
            .where(workflows.c.id == workflow_id)
            .values(
                name=body.name,
                dag_jsonb=spec.model_dump(mode="json"),
                schedule_cron=spec.schedule.cron if spec.schedule else None,
                schedule_enabled=spec.schedule.enabled if spec.schedule else False,
                updated_at=datetime.now(UTC),
            )
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


def _validated_workflow_spec(payload: dict[str, Any]) -> WorkflowSpec:
    """R7 门禁落地点:WorkflowSpec 构造期校验错误 → 结构化 4xx。

    forbidden_node_kind(R7 红线,永不开放)与 unsupported_node_kind
    (白名单内但首版未实现)必须可区分(ADR-0009 Consequences)。
    """
    try:
        return WorkflowSpec.model_validate(payload)
    except ValidationError as exc:
        code, message = _workflow_spec_error(exc)
        raise ApiError(400, code, message) from exc


def _workflow_spec_error(exc: ValidationError) -> tuple[str, str]:
    # 领域层 ValueError 约定 "code: 说明";pydantic 包装后位于 errors()[i]["msg"]
    # 且带 "Value error, " 前缀。取第一条可识别 code(按节点声明顺序,确定性)。
    for error in exc.errors():
        message = str(error.get("msg", "")).removeprefix(_PYDANTIC_VALUE_ERROR_PREFIX)
        code, sep, _ = message.partition(":")
        if sep and code in _WORKFLOW_SPEC_ERROR_CODES:
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
) -> RowMapping:
    row = (
        conn.execute(
            select(workflows)
            .where(workflows.c.id == workflow_id)
            .where(workflows.c.project_id == project_id)
        )
        .mappings()
        .one_or_none()
    )
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
) -> WorkflowRunCreateResponse:
    """手动触发:enqueue 一个 workflow_run job(WorkflowRun 本身是 job,ADR-0009)。

    spec 快照进 payload:执行期不再回读 workflows 表,触发后改定义不影响在途 run。
    """
    services = services_from(request)
    user = current_user_from(request)
    with services.engine.begin() as conn:
        _require_project_access(conn, project_id, user.id)
        row = _workflow_row_for_project(conn, project_id=project_id, workflow_id=workflow_id)
        if not bool(row["enabled"]):
            raise ApiError(409, "workflow_disabled", "Workflow is disabled")
        # R7 enqueue 期再校验(ADR-0009 Consequences:创建 + enqueue 双门禁)
        spec = _validated_workflow_spec(dict(row["dag_jsonb"] or {}))
        run_id = new_id()
        timeout_seconds = sum(node.timeout_seconds for node in spec.nodes) + 600
        job = Job(
            id=run_id,
            kind=JobKind.WORKFLOW_RUN,
            status=JobStatus.PENDING,
            owner_user_id=user.id,
            project_id=project_id,
            datasource_ids=[],
            priority=0,
            timeout_seconds=timeout_seconds,
            resource_profile=ResourceProfile(timeout_seconds=timeout_seconds),
            audit_id=new_id(),
            payload={
                "workflow_id": workflow_id,
                "workflow_name": str(row["name"]),
                "trigger": "manual",
                "spec": spec.model_dump(mode="json"),
                # when 变量在触发时刻冻结:执行器每步推进与状态查询共用这份
                # 快照,when 决策在 run 生命周期内确定(跨午夜不翻转)
                "when_variables": builtin_when_variables(datetime.now(UTC)),
            },
        )
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
        return [
            WorkflowRunNodeItem(
                node_id=str(child["payload"].get("workflow_node_id") or child["id"]),
                job_kind=str(child["kind"]),
                status=str(child["status"]),
                job_id=str(child["id"]),
                attempts=int(child["retry_count"] or 0),
                error=_optional_str(child["error"]),
            )
            for child in children
            if isinstance(child["payload"], dict)
        ]
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
            )
        )
    return items


def _workflow_child_snapshots_from_rows(
    children: list[RowMapping],
) -> dict[str, WorkflowChildJob]:
    snapshots: dict[str, WorkflowChildJob] = {}
    for child in children:
        child_payload = child["payload"] if isinstance(child["payload"], dict) else {}
        node_id = child_payload.get("workflow_node_id")
        if not isinstance(node_id, str) or not node_id:
            continue
        finished_at = child["finished_at"]
        snapshots[node_id] = WorkflowChildJob(
            node_id=node_id,
            job_id=str(child["id"]),
            status=JobStatus(str(child["status"])),
            retry_count=int(child["retry_count"] or 0),
            finished_at=finished_at if isinstance(finished_at, datetime) else None,
            error=_optional_str(child["error"]),
        )
    return snapshots


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
    manifest = _spool_manifest_or_none(services, result_set_id)
    return JobResultResponse(
        job_id=job_id,
        result_set_id=result_set_id,
        offset=offset,
        limit=limit,
        columns=_columns_from_manifest(manifest),
        rows=[RowResponse(values=row.values) for row in rows],
        loaded_rows=_int_from_manifest(manifest, "loaded_rows"),
        total_rows=_result_set_total_rows(services, result_set_id),
        state=_result_set_state(services, result_set_id),
        truncated=_bool_from_manifest(manifest, "truncated"),
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
        if not isinstance(expires_at, datetime) or _as_utc(expires_at) <= now:
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
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
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
        return LicenseStatusResponse(mode="trial", trial_days_remaining=30)

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
    )


def _user_by_username(services: ApiServices, username: str) -> RowMapping | None:
    with services.engine.connect() as conn:
        return (
            conn.execute(select(users).where(users.c.username == username)).mappings().one_or_none()
        )


def _sql_hash(sql: str) -> str:
    digest = sha256(sql.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


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


def _metadata_payload(
    services: ApiServices,
    datasource_row: RowMapping,
    cache_level: str,
    *,
    schema_name: str,
    table_name: str,
    refresh: bool,
    load: Callable[[], list[dict[str, object]]],
) -> list[dict[str, object]]:
    datasource_id = str(datasource_row["id"])
    now = datetime.now(UTC)
    if not refresh:
        cached = _read_metadata_cache(
            services,
            datasource_id,
            cache_level,
            schema_name=schema_name,
            table_name=table_name,
            now=now,
        )
        if cached is not None:
            return cached
    try:
        payload = load()
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


def _read_metadata_cache(
    services: ApiServices,
    datasource_id: str,
    cache_level: str,
    *,
    schema_name: str,
    table_name: str,
    now: datetime,
) -> list[dict[str, object]] | None:
    with services.engine.connect() as conn:
        row = (
            conn.execute(
                select(metadata_caches.c.payload, metadata_caches.c.expires_at).where(
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
    expires_at = row["expires_at"]
    if isinstance(expires_at, datetime):
        normalized = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=UTC)
        if normalized <= now:
            return None
    payload = row["payload"]
    if not isinstance(payload, list):
        return None
    return [dict(item) for item in payload if isinstance(item, dict)]


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
    if database_name is None:
        raise ApiError(400, "invalid_datasource", "Datasource database is required")
    return DatasourceConnInfo(
        host=str(row["host"]),
        port=int(row["port"]),
        username=str(row["username"]),
        database=str(database_name),
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
) -> list[Column]:
    payload = _metadata_payload(
        services,
        datasource_row,
        _METADATA_LEVEL_COLUMNS,
        schema_name=schema_name,
        table_name=table_name,
        refresh=refresh,
        load=lambda: [
            _metadata_column_item(column).model_dump(mode="json")
            for column in _metadata_adapter(services, datasource_row).list_columns(
                schema_name, table_name
            )
        ],
    )
    return [Column.model_validate(item) for item in payload]


def _metadata_indexes_for_table(
    services: ApiServices,
    datasource_row: RowMapping,
    *,
    schema_name: str,
    table_name: str,
    refresh: bool,
) -> list[Index]:
    payload = _metadata_payload(
        services,
        datasource_row,
        _METADATA_LEVEL_INDEXES,
        schema_name=schema_name,
        table_name=table_name,
        refresh=refresh,
        load=lambda: [
            _metadata_index_item(index).model_dump(mode="json")
            for index in _metadata_adapter(services, datasource_row).list_indexes(
                schema_name, table_name
            )
        ],
    )
    return [Index.model_validate(item) for item in payload]


def _metadata_all_tables(
    services: ApiServices,
    datasource_row: RowMapping,
    *,
    refresh: bool,
) -> list[Table]:
    schema_payload = _metadata_payload(
        services,
        datasource_row,
        _METADATA_LEVEL_SCHEMAS,
        schema_name="",
        table_name="",
        refresh=refresh,
        load=lambda: [
            MetadataSchemaItem(name=item.name).model_dump(mode="json")
            for item in _metadata_adapter(services, datasource_row).list_schemas()
        ],
    )
    tables: list[Table] = []
    for schema_item in schema_payload:
        schema_name = schema_item.get("name")
        if not isinstance(schema_name, str) or not schema_name:
            continue

        def load_tables_for_schema(schema: str = schema_name) -> list[dict[str, object]]:
            return [
                MetadataTableItem(
                    schema_name=item.schema_name,
                    name=item.name,
                    table_type=item.table_type,
                ).model_dump(mode="json")
                for item in _metadata_adapter(services, datasource_row).list_tables(schema)
            ]

        table_payload = _metadata_payload(
            services,
            datasource_row,
            _METADATA_LEVEL_TABLES,
            schema_name=schema_name,
            table_name="",
            refresh=refresh,
            load=load_tables_for_schema,
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
) -> str:
    dialect = _sqlglot_dialect(str(datasource_row["db_type"]))
    expression = sqlglot.parse_one(sql, read=dialect)
    select_expr = expression.find(exp.Select)
    if select_expr is None:
        raise ApiError(400, "invalid_sql", "Only SELECT SQL can be expanded")
    table_refs = _select_table_refs(datasource_row, select_expr)
    if not table_refs:
        raise ApiError(400, "invalid_sql", "SELECT must reference at least one table")

    changed = False
    expanded_selects: list[exp.Expression] = []
    for item in list(select_expr.expressions):
        if isinstance(item, exp.Star):
            changed = True
            for table_ref in table_refs:
                expanded_selects.extend(_column_expressions_for_ref(services, table_ref, None))
            continue
        if isinstance(item, exp.Column) and isinstance(item.this, exp.Star):
            table_name = item.table
            matched_table_ref = _table_ref_by_qualifier(table_refs, table_name)
            if matched_table_ref is None:
                raise ApiError(400, "unknown_table", "SELECT star qualifier does not match a table")
            changed = True
            expanded_selects.extend(
                _column_expressions_for_ref(services, matched_table_ref, table_name)
            )
            continue
        expanded_selects.append(item)
    if not changed:
        raise ApiError(400, "no_star", "SQL does not contain SELECT *")
    select_expr.set("expressions", expanded_selects)
    return expression.sql(dialect=dialect, pretty=True)


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
    table_ref: dict[str, str],
    qualifier: str | None,
) -> list[exp.Column]:
    columns = _metadata_columns_from_cache(
        services,
        table_ref["datasource_id"],
        schema_name=table_ref["schema"],
        table_name=table_ref["table"],
    )
    table_name = qualifier or table_ref["alias"]
    return [exp.column(str(column["name"]), table=table_name, quoted=True) for column in columns]


def _metadata_columns_from_cache(
    services: ApiServices,
    datasource_id: str,
    *,
    schema_name: str,
    table_name: str,
) -> list[dict[str, object]]:
    payload = _read_metadata_cache(
        services,
        datasource_id,
        _METADATA_LEVEL_COLUMNS,
        schema_name=schema_name,
        table_name=table_name,
        now=datetime.now(UTC),
    )
    if payload is None:
        raise ApiError(
            409,
            "metadata_cache_missing",
            "Column metadata cache is missing; refresh metadata first",
        )
    return payload


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


def _compare_task_response(row: RowMapping) -> CompareTaskResponse:
    return CompareTaskResponse(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        name=str(row["name"]),
        source_id=_optional_str(row["source_id"]),
        target_id=_optional_str(row["target_id"]),
        source_ref=row["source_ref"],
        target_ref=row["target_ref"],
        columns=[Column.model_validate(item) for item in row["columns"] or []],
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
                )
            )
        )
        .mappings()
        .one_or_none()
    )


def _delete_lineage_cache(
    conn: Connection,
    *,
    project_id: str,
    datasource_id: str,
    dialect: str,
    source_ref: str,
    sql_hash: str,
) -> None:
    conn.execute(
        delete(lineage_runs).where(
            and_(
                lineage_runs.c.project_id == project_id,
                lineage_runs.c.datasource_id == datasource_id,
                lineage_runs.c.dialect == dialect,
                lineage_runs.c.source_ref == source_ref,
                lineage_runs.c.sql_hash == sql_hash,
                lineage_runs.c.parser_version == LINEAGE_PARSER_VERSION,
            )
        )
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
    table_rows = [
        {
            "id": new_id(),
            "run_id": run_id,
            "project_id": project_id,
            "source_table": str(edge["source_table"]),
            "target_table": str(edge["target_table"]),
            "edge_kind": "table",
            "inferred": False,
            "inference_status": "confirmed",
            "confidence": 1.0,
            "sql_hash": sql_hash,
        }
        for edge in graph_edges
        if edge.get("source_table") and edge.get("target_table")
    ]
    if table_rows:
        conn.execute(insert(lineage_edges), table_rows)
    column_rows = [
        {
            "id": new_id(),
            "run_id": run_id,
            "project_id": project_id,
            "source_table": str(mapping["source_table"]),
            "source_column": str(mapping["source_column"]),
            "target_table": str(mapping["target_table"]),
            "target_column": str(mapping["target_column"]),
            "transformation": str(mapping.get("transformation") or "DIRECT"),
            "transformation_subtype": str(mapping.get("transformation_subtype") or "DIRECT"),
            "inferred": False,
            "inference_status": "confirmed",
            "confidence": 1.0,
            "sql_hash": sql_hash,
        }
        for mapping in insert_mappings
        if (
            mapping.get("source_table")
            and mapping.get("source_column")
            and mapping.get("target_table")
            and mapping.get("target_column")
        )
    ]
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
        database=str(row["database_name"]),
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
        finished_at=row["finished_at"],
        result_set_id=_result_set_id_from_payload(row),
        error=error_code if status is JobStatus.FAILED else None,
        error_code=error_code,
        message="Job failed" if status is JobStatus.FAILED else None,
    )


def _require_exportable_source(row: RowMapping) -> None:
    if str(row["kind"]) != JobKind.SQL_QUERY.value:
        raise ApiError(409, "unsupported_export_source", "Only SQL query jobs can be exported")
    if JobStatus(str(row["status"])) is not JobStatus.SUCCESS:
        raise ApiError(409, "job_not_successful", "Source job must complete before export")


def _require_compare_run_exportable(row: RowMapping) -> None:
    if JobStatus(str(row["status"])) is not JobStatus.SUCCESS:
        raise ApiError(409, "compare_run_not_successful", "Compare run must complete before export")


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


def _compare_export_filename(run_id: str) -> str:
    return f"{run_id}.xlsx"


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


def _columns_from_manifest(manifest: dict[str, Any] | None) -> list[Column]:
    raw_columns = manifest.get("columns") if manifest else None
    if not isinstance(raw_columns, list):
        return []
    return [Column.model_validate(column) for column in raw_columns if isinstance(column, dict)]


def _optional_str(value: object) -> str | None:
    return str(value) if value is not None else None


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
