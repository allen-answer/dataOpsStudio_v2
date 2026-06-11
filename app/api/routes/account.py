from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.engine import Connection, RowMapping

from app.api.dependencies import current_user_from, request_id_from, services_from
from app.api.errors import ApiError
from app.api.schemas import (
    AccountPasswordChangeRequest,
    AccountPasswordChangeResponse,
    AccountSecurityStatusResponse,
    MfaDisableRequest,
    MfaDisableResponse,
    MfaEnrollResponse,
    MfaVerifyRequest,
    MfaVerifyResponse,
    RecoveryCodesRegenerateRequest,
    RecoveryCodesResponse,
)
from app.api.services import ApiServices
from app.db.models import mfa_recovery_codes, users
from app.domain.secret import HashedRef, SecretKind, SecretRef
from app.infrastructure.secretstore.totp import (
    accepted_totp_counter,
    generate_recovery_codes,
    generate_totp_seed,
    provisioning_uri,
)

router = APIRouter(prefix="/account")


@router.get("/security", response_model=AccountSecurityStatusResponse)
def get_account_security(request: Request) -> AccountSecurityStatusResponse:
    user = current_user_from(request)
    services = services_from(request)
    with services.engine.connect() as conn:
        row = _require_user(conn, user.id)
        counts = _recovery_code_counts(conn, user.id)
    return AccountSecurityStatusResponse(
        mfa_enabled=row["mfa_secret_ref"] is not None,
        recovery_codes_total=counts["total"],
        recovery_codes_used=counts["used"],
    )


@router.post("/password", response_model=AccountPasswordChangeResponse)
def change_account_password(
    body: AccountPasswordChangeRequest,
    request: Request,
) -> AccountPasswordChangeResponse:
    user = current_user_from(request)
    services = services_from(request)
    with services.engine.begin() as conn:
        row = _require_user(conn, user.id)
        if not services.secret_store.verify_password(
            body.old_password,
            HashedRef(ref=str(row["password_hash"]), algorithm="bcrypt"),
        ):
            raise ApiError(401, "invalid_password", "Invalid current password")
        password_hash = services.secret_store.hash_password(body.new_password).ref
        conn.execute(
            update(users)
            .where(users.c.id == user.id)
            .values(password_hash=password_hash, updated_at=datetime.now(UTC))
        )
    _audit_account(
        services,
        request,
        user_id=user.id,
        action="account_password_change",
        result="success",
    )
    return AccountPasswordChangeResponse(changed=True)


@router.post("/mfa/enroll", response_model=MfaEnrollResponse)
def enroll_account_mfa(request: Request) -> MfaEnrollResponse:
    user = current_user_from(request)
    services = services_from(request)
    with services.engine.begin() as conn:
        row = _require_user(conn, user.id)
        if row["mfa_secret_ref"] is not None:
            raise ApiError(409, "mfa_already_enabled", "MFA is already enabled")
        old_pending_ref = _optional_str(row["mfa_pending_secret_ref"])
        seed = generate_totp_seed()
        ref = services.secret_store.store_secret(seed, SecretKind.MFA_TOTP_SEED)
        conn.execute(
            update(users)
            .where(users.c.id == user.id)
            .values(mfa_pending_secret_ref=ref.ref, updated_at=datetime.now(UTC))
        )
    if old_pending_ref is not None:
        services.secret_store.delete_secret(
            SecretRef(ref=old_pending_ref, kind=SecretKind.MFA_TOTP_SEED)
        )
    _audit_account(
        services, request, user_id=user.id, action="account_mfa_enroll", result="success"
    )
    return MfaEnrollResponse(
        secret=seed,
        otpauth_uri=provisioning_uri(secret=seed, username=user.id),
    )


@router.post("/mfa/verify", response_model=MfaVerifyResponse)
def verify_account_mfa(body: MfaVerifyRequest, request: Request) -> MfaVerifyResponse:
    user = current_user_from(request)
    services = services_from(request)
    with services.engine.begin() as conn:
        row = _require_user(conn, user.id)
        pending_ref = _optional_str(row["mfa_pending_secret_ref"])
        if row["mfa_secret_ref"] is not None:
            raise ApiError(409, "mfa_already_enabled", "MFA is already enabled")
        if pending_ref is None:
            raise ApiError(409, "mfa_enroll_required", "Start MFA enrollment first")
        seed = services.secret_store.reveal_secret(
            SecretRef(ref=pending_ref, kind=SecretKind.MFA_TOTP_SEED)
        )
        counter = accepted_totp_counter(secret=seed, code=body.code)
        if counter is None:
            raise ApiError(401, "invalid_mfa_code", "Invalid MFA code")
        codes = _replace_recovery_codes(conn, services, user.id)
        conn.execute(
            update(users)
            .where(users.c.id == user.id)
            .values(
                mfa_secret_ref=pending_ref,
                mfa_pending_secret_ref=None,
                last_used_totp_counter=counter,
                updated_at=datetime.now(UTC),
            )
        )
    _audit_account(
        services, request, user_id=user.id, action="account_mfa_enable", result="success"
    )
    return MfaVerifyResponse(enabled=True, recovery_codes=codes)


@router.post("/mfa/disable", response_model=MfaDisableResponse)
def disable_account_mfa(body: MfaDisableRequest, request: Request) -> MfaDisableResponse:
    user = current_user_from(request)
    services = services_from(request)
    with services.engine.begin() as conn:
        row = _require_user(conn, user.id)
        mfa_ref = _optional_str(row["mfa_secret_ref"])
        if mfa_ref is None:
            raise ApiError(409, "mfa_not_enabled", "MFA is not enabled")
        if not verify_user_totp(conn, services, user_id=user.id, mfa_ref=mfa_ref, code=body.code):
            raise ApiError(401, "invalid_mfa_code", "Invalid MFA code")
        conn.execute(delete(mfa_recovery_codes).where(mfa_recovery_codes.c.user_id == user.id))
        conn.execute(
            update(users)
            .where(users.c.id == user.id)
            .values(mfa_secret_ref=None, mfa_pending_secret_ref=None, updated_at=datetime.now(UTC))
        )
    services.secret_store.delete_secret(SecretRef(ref=mfa_ref, kind=SecretKind.MFA_TOTP_SEED))
    _audit_account(
        services, request, user_id=user.id, action="account_mfa_disable", result="success"
    )
    return MfaDisableResponse()


@router.post("/recovery-codes/regenerate", response_model=RecoveryCodesResponse)
def regenerate_account_recovery_codes(
    body: RecoveryCodesRegenerateRequest,
    request: Request,
) -> RecoveryCodesResponse:
    user = current_user_from(request)
    services = services_from(request)
    with services.engine.begin() as conn:
        row = _require_user(conn, user.id)
        mfa_ref = _optional_str(row["mfa_secret_ref"])
        if mfa_ref is None:
            raise ApiError(409, "mfa_not_enabled", "MFA is not enabled")
        if not verify_user_totp(conn, services, user_id=user.id, mfa_ref=mfa_ref, code=body.code):
            raise ApiError(401, "invalid_mfa_code", "Invalid MFA code")
        codes = _replace_recovery_codes(conn, services, user.id)
    _audit_account(
        services,
        request,
        user_id=user.id,
        action="account_recovery_codes_regenerate",
        result="success",
    )
    return RecoveryCodesResponse(recovery_codes=codes)


def verify_user_totp(
    conn: Connection,
    services: ApiServices,
    *,
    user_id: str,
    mfa_ref: str,
    code: str,
) -> bool:
    seed = services.secret_store.reveal_secret(
        SecretRef(ref=mfa_ref, kind=SecretKind.MFA_TOTP_SEED)
    )
    counter = accepted_totp_counter(secret=seed, code=code)
    if counter is None:
        return False
    row = (
        conn.execute(
            select(users.c.last_used_totp_counter).where(users.c.id == user_id).with_for_update()
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return False
    last_counter = row["last_used_totp_counter"]
    if isinstance(last_counter, int) and counter <= last_counter:
        return False
    conn.execute(
        update(users)
        .where(users.c.id == user_id)
        .values(last_used_totp_counter=counter, updated_at=datetime.now(UTC))
    )
    return True


def consume_recovery_code(
    conn: Connection,
    services: ApiServices,
    *,
    user_id: str,
    code: str,
) -> bool:
    rows = (
        conn.execute(
            select(mfa_recovery_codes.c.id, mfa_recovery_codes.c.code_hash)
            .where(mfa_recovery_codes.c.user_id == user_id)
            .where(mfa_recovery_codes.c.used_at.is_(None))
            .order_by(mfa_recovery_codes.c.id.asc())
        )
        .mappings()
        .all()
    )
    for row in rows:
        if services.secret_store.verify_password(
            code,
            HashedRef(ref=str(row["code_hash"]), algorithm="bcrypt"),
        ):
            conn.execute(
                update(mfa_recovery_codes)
                .where(mfa_recovery_codes.c.id == row["id"])
                .where(mfa_recovery_codes.c.used_at.is_(None))
                .values(used_at=datetime.now(UTC))
            )
            return True
    return False


def _replace_recovery_codes(conn: Connection, services: ApiServices, user_id: str) -> list[str]:
    codes = generate_recovery_codes()
    conn.execute(delete(mfa_recovery_codes).where(mfa_recovery_codes.c.user_id == user_id))
    conn.execute(
        insert(mfa_recovery_codes),
        [
            {
                "user_id": user_id,
                "code_hash": services.secret_store.hash_password(code).ref,
            }
            for code in codes
        ],
    )
    return codes


def _require_user(conn: Connection, user_id: str) -> RowMapping:
    row = conn.execute(select(users).where(users.c.id == user_id)).mappings().one_or_none()
    if row is None:
        raise ApiError(404, "not_found", "User not found")
    return row


def _recovery_code_counts(conn: Connection, user_id: str) -> dict[str, int]:
    row = (
        conn.execute(
            select(
                func.count().label("total"),
                func.count(mfa_recovery_codes.c.used_at).label("used"),
            ).where(mfa_recovery_codes.c.user_id == user_id)
        )
        .mappings()
        .one()
    )
    return {"total": int(row["total"]), "used": int(row["used"])}


def _audit_account(
    services: ApiServices,
    request: Request,
    *,
    user_id: str,
    action: str,
    result: str,
    detail: dict[str, object] | None = None,
) -> None:
    services.write_audit(
        user_id=user_id,
        project_id=None,
        action=action,
        resource_type="user",
        resource_id=user_id,
        result=result,
        request_id=request_id_from(request),
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        detail=detail,
    )


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
