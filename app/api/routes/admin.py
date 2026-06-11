from __future__ import annotations

import os
import secrets
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import and_, delete, func, insert, select, update
from sqlalchemy.engine import Connection, RowMapping

from app.api.dependencies import current_user_from, request_id_from, services_from
from app.api.errors import ApiError
from app.api.schemas import (
    AdminAiConfigResponse,
    AdminAiConfigTestResponse,
    AdminAiConfigUpdateRequest,
    AdminForceLogoutResponse,
    AdminProjectCreateRequest,
    AdminProjectDeleteImpactResponse,
    AdminProjectItem,
    AdminProjectPatchRequest,
    AdminResetPasswordResponse,
    AdminUserCreateRequest,
    AdminUserItem,
    AdminUserPatchRequest,
    AiApiKeySource,
    AiProvider,
    AuditLogItem,
    LicenseStatusResponse,
    LicenseUploadRequest,
    ProjectResponse,
)
from app.api.services import ApiServices
from app.db.models import (
    ai_configs,
    audit_logs,
    datasources,
    jobs,
    license_state,
    mfa_recovery_codes,
    project_members,
    projects,
    users,
)
from app.domain.ai import AiContext, AiOptions
from app.domain.secret import SecretKind, SecretRef
from app.infrastructure.license import read_license_limits, verify_license
from app.services.ai import (
    AI_API_KEY_ENV,
    AiGatewayError,
    AiGatewayRuntimeConfig,
    build_gateway_from_runtime_config,
)

router = APIRouter(prefix="/admin")


@router.get("/license", response_model=LicenseStatusResponse)
def get_admin_license(request: Request) -> LicenseStatusResponse:
    _require_admin(request)
    return _current_license_response(services_from(request))


@router.put("/license", response_model=LicenseStatusResponse)
def put_admin_license(body: LicenseUploadRequest, request: Request) -> LicenseStatusResponse:
    user = _require_admin(request)
    services = services_from(request)
    path = _license_file_path()
    _write_license_file(path, body.license_text.encode("utf-8"))
    state = verify_license(path)
    _upsert_license_state(services, state)
    _audit_admin(
        services,
        request,
        action="admin_license_update",
        resource_type="license",
        resource_id="license.lic",
        detail={"mode": state.mode.value, "repair_reason": state.repair_reason},
        user_id=user.id,
    )
    return _current_license_response(services)


@router.get("/users", response_model=list[AdminUserItem])
def list_admin_users(request: Request) -> list[AdminUserItem]:
    _require_admin(request)
    services = services_from(request)
    with services.engine.connect() as conn:
        rows = (
            conn.execute(
                select(
                    users.c.id,
                    users.c.username,
                    users.c.role,
                    users.c.mfa_secret_ref,
                    users.c.created_at,
                ).order_by(users.c.created_at.desc(), users.c.id.desc())
            )
            .mappings()
            .all()
        )
    return [_user_item(row) for row in rows]


@router.post("/users", response_model=AdminUserItem, status_code=201)
def create_admin_user(body: AdminUserCreateRequest, request: Request) -> AdminUserItem:
    actor = _require_admin(request)
    services = services_from(request)
    user_id = str(uuid4())
    password_hash = services.secret_store.hash_password(body.initial_password).ref
    with services.engine.begin() as conn:
        conn.execute(
            insert(users).values(
                id=user_id,
                username=body.username,
                password_hash=password_hash,
                role=body.role,
            )
        )
        row = _user_row(conn, user_id)
    _audit_admin(
        services,
        request,
        action="admin_user_create",
        resource_type="user",
        resource_id=user_id,
        detail={"role": body.role},
        user_id=actor.id,
    )
    return _user_item(row)


@router.patch("/users/{user_id}", response_model=AdminUserItem)
def patch_admin_user(
    user_id: str,
    body: AdminUserPatchRequest,
    request: Request,
) -> AdminUserItem:
    actor = _require_admin(request)
    services = services_from(request)
    with services.engine.begin() as conn:
        _require_user(conn, user_id)
        conn.execute(update(users).where(users.c.id == user_id).values(role=body.role))
        row = _user_row(conn, user_id)
    _audit_admin(
        services,
        request,
        action="admin_user_update",
        resource_type="user",
        resource_id=user_id,
        detail={"role": body.role},
        user_id=actor.id,
    )
    return _user_item(row)


@router.post("/users/{user_id}/reset-password", response_model=AdminResetPasswordResponse)
def reset_admin_user_password(user_id: str, request: Request) -> AdminResetPasswordResponse:
    actor = _require_admin(request)
    services = services_from(request)
    temporary_password = secrets.token_urlsafe(18)
    password_hash = services.secret_store.hash_password(temporary_password).ref
    with services.engine.begin() as conn:
        _require_user(conn, user_id)
        conn.execute(update(users).where(users.c.id == user_id).values(password_hash=password_hash))
    _audit_admin(
        services,
        request,
        action="admin_user_reset_password",
        resource_type="user",
        resource_id=user_id,
        user_id=actor.id,
    )
    return AdminResetPasswordResponse(temporary_password=temporary_password)


@router.post("/users/{user_id}/force-logout", response_model=AdminForceLogoutResponse)
def force_logout_admin_user(user_id: str, request: Request) -> AdminForceLogoutResponse:
    actor = _require_admin(request)
    services = services_from(request)
    with services.engine.connect() as conn:
        _require_user(conn, user_id)
    try:
        revoked_after = services.revoke_user_tokens(user_id=user_id)
    except ValueError as exc:
        raise ApiError(404, "not_found", "User not found") from exc
    _audit_admin(
        services,
        request,
        action="admin_user_force_logout",
        resource_type="user",
        resource_id=user_id,
        user_id=actor.id,
    )
    return AdminForceLogoutResponse(user_id=user_id, revoked_after=revoked_after)


@router.post("/users/{user_id}/mfa/disable", response_model=AdminUserItem)
def disable_admin_user_mfa(user_id: str, request: Request) -> AdminUserItem:
    actor = _require_admin(request)
    services = services_from(request)
    with services.engine.begin() as conn:
        existing = _require_user(conn, user_id)
        mfa_ref = _optional_str(existing["mfa_secret_ref"])
        pending_ref = _optional_str(existing["mfa_pending_secret_ref"])
        conn.execute(delete(mfa_recovery_codes).where(mfa_recovery_codes.c.user_id == user_id))
        conn.execute(
            update(users)
            .where(users.c.id == user_id)
            .values(mfa_secret_ref=None, mfa_pending_secret_ref=None)
        )
        row = _user_row(conn, user_id)
    for ref_value in [mfa_ref, pending_ref]:
        if ref_value is not None:
            services.secret_store.delete_secret(
                SecretRef(ref=ref_value, kind=SecretKind.MFA_TOTP_SEED)
            )
    _audit_admin(
        services,
        request,
        action="admin_user_mfa_disable",
        resource_type="user",
        resource_id=user_id,
        user_id=actor.id,
    )
    return _user_item(row)


@router.delete("/users/{user_id}")
def delete_admin_user(user_id: str, request: Request) -> Any:
    actor = _require_admin(request)
    services = services_from(request)
    with services.engine.begin() as conn:
        owned = _owned_projects(conn, user_id)
        if owned:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "user_owns_projects",
                    "message": "User still owns projects",
                    "owned_projects": [item.model_dump() for item in owned],
                },
            )
        _require_user(conn, user_id)
        conn.execute(delete(project_members).where(project_members.c.user_id == user_id))
        conn.execute(delete(users).where(users.c.id == user_id))
    _audit_admin(
        services,
        request,
        action="admin_user_delete",
        resource_type="user",
        resource_id=user_id,
        user_id=actor.id,
    )
    return {"deleted": True}


@router.get("/projects", response_model=list[AdminProjectItem])
def list_admin_projects(request: Request) -> list[AdminProjectItem]:
    _require_admin(request)
    services = services_from(request)
    with services.engine.connect() as conn:
        rows = conn.execute(_projects_with_counts()).mappings().all()
    return [_project_item(row) for row in rows]


@router.post("/projects", response_model=AdminProjectItem, status_code=201)
def create_admin_project(body: AdminProjectCreateRequest, request: Request) -> AdminProjectItem:
    actor = _require_admin(request)
    services = services_from(request)
    project_id = str(uuid4())
    with services.engine.begin() as conn:
        _require_user(conn, body.owner_user_id)
        conn.execute(
            insert(projects).values(
                id=project_id,
                name=body.name,
                description=body.description,
                owner_user_id=body.owner_user_id,
            )
        )
        _replace_project_members(conn, project_id, body.owner_user_id, body.members)
        row = (
            conn.execute(_projects_with_counts().where(projects.c.id == project_id))
            .mappings()
            .one()
        )
    _audit_admin(
        services,
        request,
        action="admin_project_create",
        resource_type="project",
        resource_id=project_id,
        user_id=actor.id,
    )
    return _project_item(row)


@router.patch("/projects/{project_id}", response_model=AdminProjectItem)
def patch_admin_project(
    project_id: str,
    body: AdminProjectPatchRequest,
    request: Request,
) -> AdminProjectItem:
    actor = _require_admin(request)
    services = services_from(request)
    with services.engine.begin() as conn:
        current = _require_project(conn, project_id)
        owner_user_id = body.owner_user_id or str(current["owner_user_id"])
        _require_user(conn, owner_user_id)
        values: dict[str, object] = {"owner_user_id": owner_user_id}
        if body.name is not None:
            values["name"] = body.name
        if body.description is not None:
            values["description"] = body.description
        conn.execute(update(projects).where(projects.c.id == project_id).values(**values))
        if body.members is not None or body.owner_user_id is not None:
            _replace_project_members(conn, project_id, owner_user_id, body.members or [])
        row = (
            conn.execute(_projects_with_counts().where(projects.c.id == project_id))
            .mappings()
            .one()
        )
    _audit_admin(
        services,
        request,
        action="admin_project_update",
        resource_type="project",
        resource_id=project_id,
        user_id=actor.id,
    )
    return _project_item(row)


@router.get("/projects/{project_id}/impact", response_model=AdminProjectDeleteImpactResponse)
def get_admin_project_delete_impact(
    project_id: str,
    request: Request,
) -> AdminProjectDeleteImpactResponse:
    _require_admin(request)
    services = services_from(request)
    with services.engine.connect() as conn:
        _require_project(conn, project_id)
        return _project_delete_impact(conn, project_id)


@router.delete("/projects/{project_id}")
def delete_admin_project(project_id: str, request: Request) -> Any:
    actor = _require_admin(request)
    services = services_from(request)
    with services.engine.begin() as conn:
        _require_project(conn, project_id)
        impact = _project_delete_impact(conn, project_id)
        conn.execute(delete(projects).where(projects.c.id == project_id))
    _audit_admin(
        services,
        request,
        action="admin_project_delete",
        resource_type="project",
        resource_id=project_id,
        detail=impact.model_dump(),
        user_id=actor.id,
    )
    return {"deleted": True, "impact": impact.model_dump()}


@router.get("/audit-logs", response_model=list[AuditLogItem])
def list_admin_audit_logs(
    request: Request,
    start: datetime | None = None,
    end: datetime | None = None,
    user_id: str | None = None,
    action: str | None = None,
    result: str | None = None,
    resource_type: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> list[AuditLogItem]:
    _require_admin(request)
    services = services_from(request)
    filters = []
    if start is not None:
        filters.append(audit_logs.c.ts >= start)
    if end is not None:
        filters.append(audit_logs.c.ts <= end)
    if user_id is not None:
        filters.append(audit_logs.c.user_id == user_id)
    if action is not None:
        filters.append(audit_logs.c.action == action)
    if result is not None:
        filters.append(audit_logs.c.result == result)
    if resource_type is not None:
        filters.append(audit_logs.c.resource_type == resource_type)

    statement = select(audit_logs).order_by(audit_logs.c.ts.desc()).limit(limit).offset(offset)
    if filters:
        statement = statement.where(and_(*filters))
    with services.engine.connect() as conn:
        rows = conn.execute(statement).mappings().all()
    return [_audit_item(row) for row in rows]


@router.get("/ai-config", response_model=AdminAiConfigResponse)
def get_admin_ai_config(request: Request) -> AdminAiConfigResponse:
    _require_admin(request)
    services = services_from(request)
    with services.engine.connect() as conn:
        row = _ai_config_row_or_none(conn)
    return _ai_config_response(row)


@router.put("/ai-config", response_model=AdminAiConfigResponse)
def put_admin_ai_config(
    body: AdminAiConfigUpdateRequest,
    request: Request,
) -> AdminAiConfigResponse:
    actor = _require_admin(request)
    services = services_from(request)
    if body.api_key is not None and body.clear_api_key:
        raise ApiError(400, "invalid_ai_config", "Choose either api_key or clear_api_key")
    _validate_ai_config_update(body)

    stored_ref: str | None = None
    old_ref: str | None = None
    with services.engine.begin() as conn:
        existing = _ai_config_row_or_none(conn)
        old_ref = _optional_str(existing["api_key_secret_ref"]) if existing is not None else None
        stored_ref = _update_ai_secret_ref(services, old_ref=old_ref, body=body)
        values = _ai_config_values(body, stored_ref)
        if existing is None:
            conn.execute(insert(ai_configs).values(id=1, **values))
        else:
            conn.execute(update(ai_configs).where(ai_configs.c.id == 1).values(**values))
        row = _ai_config_row(conn)

    if body.clear_api_key and old_ref is not None:
        services.secret_store.delete_secret(SecretRef(ref=old_ref, kind=SecretKind.AI_API_KEY))
    _audit_admin(
        services,
        request,
        action="admin_ai_config_update",
        resource_type="ai_config",
        resource_id="1",
        user_id=actor.id,
        detail={
            "provider": body.provider,
            "enabled": body.enabled,
            "api_key_changed": body.api_key is not None,
            "api_key_cleared": body.clear_api_key,
        },
    )
    return _ai_config_response(row)


@router.post("/ai-config/test", response_model=AdminAiConfigTestResponse)
def test_admin_ai_config(request: Request) -> AdminAiConfigTestResponse:
    actor = _require_admin(request)
    services = services_from(request)
    started = time.monotonic()
    with services.engine.connect() as conn:
        row = _ai_config_row_or_none(conn)
    runtime = _ai_runtime_config(services, row)
    if not runtime.enabled or runtime.provider in {None, "off"}:
        return _ai_test_response(runtime, started, ok=False, error="ai_disabled")
    if runtime.provider not in {"mock", "openai_compatible"}:
        return _ai_test_response(runtime, started, ok=False, error="unsupported_provider")
    if runtime.provider == "openai_compatible" and (not runtime.endpoint or not runtime.api_key):
        return _ai_test_response(runtime, started, ok=False, error="missing_provider_config")

    try:
        gateway = build_gateway_from_runtime_config(runtime)
        response = gateway.complete(
            "ping",
            AiContext(),
            AiOptions(purpose="admin_ai_config_test", max_tokens=8),
        )
    except AiGatewayError as exc:
        result = _ai_test_response(runtime, started, ok=False, error=type(exc).__name__)
    else:
        result = _ai_test_response(
            runtime,
            started,
            ok=True,
            provider=response.provider,
            model=response.model,
        )
    _audit_admin(
        services,
        request,
        action="admin_ai_config_test",
        resource_type="ai_config",
        resource_id="1",
        user_id=actor.id,
        detail={"provider": runtime.provider, "ok": result.ok},
    )
    return result


def _require_admin(request: Request) -> Any:
    user = current_user_from(request)
    if user.role != "admin":
        raise ApiError(403, "forbidden", "Admin role required")
    return user


def _validate_ai_config_update(body: AdminAiConfigUpdateRequest) -> None:
    if body.provider == "off" and body.enabled:
        raise ApiError(400, "invalid_ai_config", "provider=off cannot be enabled")
    if body.enabled and body.provider not in {"mock", "openai_compatible"}:
        raise ApiError(400, "unsupported_provider", "Provider is not implemented in 2.0.0")
    if body.enabled and body.provider == "openai_compatible" and not body.base_url:
        raise ApiError(400, "invalid_ai_config", "base_url is required")


def _ai_config_values(
    body: AdminAiConfigUpdateRequest,
    api_key_secret_ref: str | None,
) -> dict[str, object]:
    now = datetime.now(UTC)
    return {
        "enabled": body.enabled,
        "provider": body.provider,
        "model": body.model,
        "base_url": body.base_url,
        "api_key_secret_ref": api_key_secret_ref,
        "max_auto_egress_level": body.max_auto_egress_level,
        "l4_requires_optin": body.l4_requires_optin,
        "enable_inference": body.enable_inference,
        "enable_auto_translation": body.enable_auto_translation,
        "updated_at": now,
    }


def _update_ai_secret_ref(
    services: ApiServices,
    *,
    old_ref: str | None,
    body: AdminAiConfigUpdateRequest,
) -> str | None:
    if body.clear_api_key:
        return None
    if body.api_key is None:
        return old_ref
    if old_ref is not None:
        services.secret_store.rotate_secret(
            SecretRef(ref=old_ref, kind=SecretKind.AI_API_KEY),
            body.api_key,
        )
        return old_ref
    return services.secret_store.store_secret(body.api_key, SecretKind.AI_API_KEY).ref


def _ai_config_row_or_none(conn: Connection) -> RowMapping | None:
    return conn.execute(select(ai_configs).where(ai_configs.c.id == 1)).mappings().one_or_none()


def _ai_config_row(conn: Connection) -> RowMapping:
    row = _ai_config_row_or_none(conn)
    if row is None:
        raise ApiError(404, "not_found", "AI config not found")
    return row


def _ai_config_response(row: RowMapping | None) -> AdminAiConfigResponse:
    if row is None:
        return AdminAiConfigResponse(
            enabled=False,
            provider="off",
            max_auto_egress_level=0,
            l4_requires_optin=True,
            enable_inference=False,
            enable_auto_translation=False,
            api_key_source=_api_key_source(None),
            has_stored_api_key=False,
        )
    stored_ref = _optional_str(row["api_key_secret_ref"])
    return AdminAiConfigResponse(
        enabled=bool(row["enabled"]),
        provider=cast(AiProvider, str(row["provider"])),
        model=_optional_str(row["model"]),
        base_url=_optional_str(row["base_url"]),
        max_auto_egress_level=int(row["max_auto_egress_level"]),
        l4_requires_optin=bool(row["l4_requires_optin"]),
        enable_inference=bool(row["enable_inference"]),
        enable_auto_translation=bool(row["enable_auto_translation"]),
        api_key_source=_api_key_source(stored_ref),
        has_stored_api_key=stored_ref is not None,
        updated_at=row["updated_at"] if isinstance(row["updated_at"], datetime) else None,
    )


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
        enabled=bool(row["enabled"]),
        provider=_optional_str(row["provider"]),
        endpoint=_optional_str(row["base_url"]),
        model=_optional_str(row["model"]),
        api_key=api_key,
        max_auto_egress_level=int(row["max_auto_egress_level"]),
        l4_requires_optin=bool(row["l4_requires_optin"]),
    )


def _api_key_source(stored_ref: str | None) -> AiApiKeySource:
    if stored_ref is not None:
        return "stored"
    if os.environ.get(AI_API_KEY_ENV):
        return "env"
    return "none"


def _ai_test_response(
    runtime: AiGatewayRuntimeConfig,
    started: float,
    *,
    ok: bool,
    error: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> AdminAiConfigTestResponse:
    return AdminAiConfigTestResponse(
        ok=ok,
        provider=provider or runtime.provider or "off",
        model=model or runtime.model,
        latency_ms=max(0, round((time.monotonic() - started) * 1000)),
        error=error,
    )


def _current_license_response(services: ApiServices) -> LicenseStatusResponse:
    with services.engine.connect() as conn:
        row = (
            conn.execute(select(license_state).where(license_state.c.id == 1))
            .mappings()
            .one_or_none()
        )
    if row is None:
        return LicenseStatusResponse(mode="trial", limits={}, trial_days_remaining=30)
    expires_at = row["expires_at"] if isinstance(row["expires_at"], datetime) else None
    mode = str(row["mode"])
    return LicenseStatusResponse(
        mode=mode,
        edition=_optional_str(row["edition"]),
        customer=_optional_str(row["customer"]),
        expires_at=expires_at,
        limits=read_license_limits(_license_file_path()),
        features=list(row["features"] or []),
        repair_reason=_optional_str(row["repair_reason"]),
        trial_days_remaining=_trial_days_remaining(mode, expires_at),
    )


def _upsert_license_state(services: ApiServices, state: Any) -> None:
    now = datetime.now(UTC)
    values = {
        "id": 1,
        "edition": state.edition,
        "customer": state.customer,
        "expires_at": state.expires_at,
        "features": state.features,
        "signature_verified_at": now if state.mode.value in {"valid", "in_grace"} else None,
        "grace_started_at": now if state.mode.value == "in_grace" else None,
        "mode": state.mode.value,
        "repair_reason": state.repair_reason,
        "updated_at": now,
    }
    with services.engine.begin() as conn:
        existing = (
            conn.execute(select(license_state.c.id).where(license_state.c.id == 1))
            .mappings()
            .one_or_none()
        )
        if existing is None:
            conn.execute(insert(license_state).values(**values))
        else:
            conn.execute(update(license_state).where(license_state.c.id == 1).values(**values))


def _license_file_path() -> Path:
    return Path(os.environ.get("DATAOPS_BOOTSTRAP_LICENSE_FILE", "config/license.lic"))


def _write_license_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    if _is_windows():
        tmp.write_bytes(content)
    else:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        tmp.chmod(0o600)
    tmp.replace(path)


def _user_row(conn: Connection, user_id: str) -> RowMapping:
    row = (
        conn.execute(
            select(
                users.c.id,
                users.c.username,
                users.c.role,
                users.c.mfa_secret_ref,
                users.c.mfa_pending_secret_ref,
                users.c.created_at,
            ).where(users.c.id == user_id)
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise ApiError(404, "not_found", "User not found")
    return row


def _require_user(conn: Connection, user_id: str) -> RowMapping:
    return _user_row(conn, user_id)


def _owned_projects(conn: Connection, user_id: str) -> list[ProjectResponse]:
    rows = (
        conn.execute(
            select(projects.c.id, projects.c.name, projects.c.description)
            .where(projects.c.owner_user_id == user_id)
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


def _require_project(conn: Connection, project_id: str) -> RowMapping:
    row = conn.execute(select(projects).where(projects.c.id == project_id)).mappings().one_or_none()
    if row is None:
        raise ApiError(404, "not_found", "Project not found")
    return row


def _replace_project_members(
    conn: Connection,
    project_id: str,
    owner_user_id: str,
    members: list[str],
) -> None:
    member_ids = list(dict.fromkeys([owner_user_id, *members]))
    for member_id in member_ids:
        _require_user(conn, member_id)
    conn.execute(delete(project_members).where(project_members.c.project_id == project_id))
    conn.execute(
        insert(project_members),
        [
            {
                "project_id": project_id,
                "user_id": member_id,
                "role": "owner" if member_id == owner_user_id else "member",
            }
            for member_id in member_ids
        ],
    )


def _projects_with_counts() -> Any:
    member_count = (
        select(func.count())
        .select_from(project_members)
        .where(project_members.c.project_id == projects.c.id)
        .scalar_subquery()
    )
    datasource_count = (
        select(func.count())
        .select_from(datasources)
        .where(datasources.c.project_id == projects.c.id)
        .scalar_subquery()
    )
    job_count = (
        select(func.count())
        .select_from(jobs)
        .where(jobs.c.project_id == projects.c.id)
        .scalar_subquery()
    )
    return select(
        projects.c.id,
        projects.c.name,
        projects.c.description,
        projects.c.owner_user_id,
        projects.c.created_at,
        member_count.label("member_count"),
        datasource_count.label("datasource_count"),
        job_count.label("job_count"),
    ).order_by(projects.c.created_at.desc(), projects.c.id.desc())


def _project_delete_impact(conn: Connection, project_id: str) -> AdminProjectDeleteImpactResponse:
    datasource_count = conn.execute(
        select(func.count()).select_from(datasources).where(datasources.c.project_id == project_id)
    ).scalar_one()
    job_count = conn.execute(
        select(func.count()).select_from(jobs).where(jobs.c.project_id == project_id)
    ).scalar_one()
    return AdminProjectDeleteImpactResponse(
        datasource_count=int(datasource_count),
        job_count=int(job_count),
    )


def _user_item(row: RowMapping) -> AdminUserItem:
    return AdminUserItem(
        id=str(row["id"]),
        username=str(row["username"]),
        role=str(row["role"]),
        mfa_enabled=row["mfa_secret_ref"] is not None,
        created_at=row["created_at"],
    )


def _project_item(row: RowMapping) -> AdminProjectItem:
    return AdminProjectItem(
        id=str(row["id"]),
        name=str(row["name"]),
        description=_optional_str(row["description"]),
        owner_user_id=str(row["owner_user_id"]),
        member_count=int(row["member_count"]),
        datasource_count=int(row["datasource_count"]),
        job_count=int(row["job_count"]),
        created_at=row["created_at"],
    )


def _audit_item(row: RowMapping) -> AuditLogItem:
    detail = row["detail"] if isinstance(row["detail"], dict) else None
    return AuditLogItem(
        id=int(row["id"]),
        ts=row["ts"],
        user_id=_optional_str(row["user_id"]),
        project_id=_optional_str(row["project_id"]),
        action=str(row["action"]),
        resource_type=_optional_str(row["resource_type"]),
        resource_id=_optional_str(row["resource_id"]),
        result=str(row["result"]),
        request_id=_optional_str(row["request_id"]),
        detail=detail,
    )


def _audit_admin(
    services: ApiServices,
    request: Request,
    *,
    action: str,
    resource_type: str,
    resource_id: str,
    user_id: str,
    detail: dict[str, object] | None = None,
) -> None:
    client_ip = request.client.host if request.client else None
    services.write_audit(
        user_id=user_id,
        project_id=None,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        result="success",
        request_id=request_id_from(request),
        ip=client_ip,
        user_agent=request.headers.get("user-agent"),
        detail=detail,
    )


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


def _optional_str(value: object) -> str | None:
    return str(value) if value is not None else None


def _is_windows() -> bool:
    return os.name == "nt" or sys.platform.startswith("win")
