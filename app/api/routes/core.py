from __future__ import annotations

import time
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

import structlog
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import and_, delete, func, insert, or_, select, update
from sqlalchemy.engine import Connection, RowMapping

from app.api.dependencies import current_user_from, request_id_from, services_from
from app.api.errors import ApiError
from app.api.routes.account import consume_recovery_code, verify_user_totp
from app.api.schemas import (
    CancelResponse,
    DatasourceCreateRequest,
    DatasourceDeleteBlockedResponse,
    DatasourceListItem,
    DatasourceReferenceItem,
    DatasourceResponse,
    DatasourceTestErrorCode,
    DatasourceTestResponse,
    DatasourceUpdateRequest,
    JobListItem,
    JobResponse,
    JobResultResponse,
    LicenseStatusResponse,
    LoginRequest,
    ProjectResponse,
    RowResponse,
    SqlConsoleCreateRequest,
    SqlConsoleResponse,
    SqlConsoleUpdateRequest,
    SqlExecuteRequest,
    SqlExecuteResponse,
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
    datasources,
    jobs,
    license_state,
    project_members,
    projects,
    result_sets,
    sql_consoles,
    sql_templates,
    users,
)
from app.dbclients.sql_guard import SqlGuardError, validate_readonly_sql
from app.domain.datasource import DbType, OperationPolicy
from app.domain.job import Job, JobErrorCode, JobKind, JobStatus
from app.domain.resource import ResourceProfile
from app.domain.schema import Column
from app.domain.secret import HashedRef, SecretKind, SecretRef

logger = structlog.get_logger(__name__)

_DATASOURCE_TEST_ERROR_CODES: tuple[DatasourceTestErrorCode, ...] = (
    "auth_failed",
    "host_unreachable",
    "timeout",
    "permission_denied",
    "unknown",
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
        if references:
            payload = DatasourceDeleteBlockedResponse(
                error="datasource_in_use",
                message="Datasource is referenced by existing jobs",
                references=references,
            )
            return JSONResponse(status_code=409, content=payload.model_dump(mode="json"))
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
        result_set_id=_result_set_id_from_payload(row),
        error=error_code if status is JobStatus.FAILED else None,
        error_code=error_code,
        message="Job failed" if status is JobStatus.FAILED else None,
    )


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
