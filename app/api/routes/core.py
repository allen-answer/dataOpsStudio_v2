from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from secrets import token_urlsafe
from typing import Any, Literal, cast

import sqlglot
import structlog
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
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
    CompareInferRequest,
    CompareInferResponse,
    CompareResultRow,
    CompareRulesPayload,
    CompareRunCreateResponse,
    CompareRunLimitsPayload,
    CompareRunProfileResponse,
    CompareRunResultResponse,
    CompareSuggestedTaskCreateRequest,
    CompareTaskCreateRequest,
    CompareTaskResponse,
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
    LineageAnalyzeRequest,
    LineageAnalyzeResponse,
    LineageDirection,
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
    users,
)
from app.dbclients.factory import UnsupportedDbTypeError, build_database_adapter
from app.dbclients.protocol import AdapterConnectionError, DatabaseAdapter
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
    analyze_sql_lineage,
    lineage_sql_hash,
    schema_from_metadata_cache_rows,
)
from app.domain.resource import ResourceProfile
from app.domain.result import ResultRef
from app.domain.schema import Column, Index, Table
from app.domain.secret import HashedRef, SecretKind, SecretRef
from app.infrastructure.jobbackend.postgres import PostgresJobBackend
from app.infrastructure.result_export import export_content_type, export_extension
from app.services.ai.default_gateway import (
    AI_API_KEY_ENV,
    AiGatewayRuntimeConfig,
    build_gateway_from_runtime_config,
)
from app.services.ai.errors import AiGatewayError

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
_COMPARE_BUCKET_QUERY = Query(default="diff")
_LINEAGE_FOCUS_QUERY = Query(min_length=1)
_LINEAGE_DIRECTION_QUERY = Query(default="downstream")
_LINEAGE_DEPTH_QUERY = Query(default=3, ge=1, le=5)
_LINEAGE_INCLUDE_COLUMNS_QUERY = Query(default=False)

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

    job_id = new_id()
    run_id = new_id()
    bucket_spools = {bucket: new_id() for bucket in COMPARE_BUCKETS}
    timeout_seconds = min(int(limits.get("query_timeout_seconds") or 1800), 3600)
    job = Job(
        id=job_id,
        kind=JobKind.COMPARE_RUN,
        status=JobStatus.PENDING,
        owner_user_id=user.id,
        project_id=str(row["project_id"]),
        datasource_ids=[str(row["source_id"]), str(row["target_id"])],
        priority=0,
        timeout_seconds=timeout_seconds,
        resource_profile=ResourceProfile(timeout_seconds=timeout_seconds),
        audit_id=new_id(),
        payload={
            "task_id": task_id,
            "run_id": run_id,
            "source_id": str(row["source_id"]),
            "target_id": str(row["target_id"]),
            "source_ref": dict(row["source_ref"] or {}),
            "target_ref": dict(row["target_ref"] or {}),
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
                return _lineage_analyze_response(conn, cached, cached=True)
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
        source_id=str(row["source_id"]),
        target_id=str(row["target_id"]),
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
    source_id: str,
    target_id: str,
    user_id: str,
) -> None:
    _require_project_access(conn, project_id, user_id)
    rows = (
        conn.execute(
            select(datasources.c.id, datasources.c.project_id).where(
                datasources.c.id.in_([source_id, target_id])
            )
        )
        .mappings()
        .all()
    )
    by_id = {str(row["id"]): str(row["project_id"]) for row in rows}
    if by_id.get(source_id) != project_id or by_id.get(target_id) != project_id:
        raise ApiError(404, "not_found", "Compare datasource not found")


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
            .where(jobs.c.datasource_ids.any(datasource_id))
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
