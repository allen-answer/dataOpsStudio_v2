from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from uuid import uuid4

import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from app.api.errors import error_response
from app.api.security import CurrentUser, JwtError, bearer_token, decode_access_token
from app.api.services import ApiServices
from app.domain.license import LicenseMode

logger = structlog.get_logger(__name__)

_PUBLIC_PATHS = frozenset({"/api/auth/login", "/healthz"})
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_LICENSE_UPDATE_PATHS = frozenset({"/api/admin/license", "/api/admin/license/upload"})
_DIAGNOSTIC_EXPORT_PATHS = frozenset({"/api/admin/diagnostics", "/api/admin/diagnostics/export"})
_BACKUP_PATHS = frozenset({"/api/admin/backups", "/api/admin/backups/export"})
_RESTORE_PATHS = frozenset({"/api/admin/backups/restore"})
_BUSINESS_EXPORT_PREFIX = "/api/exports/"
_AUDIT_RESOURCE_ID_MAX_LENGTH = 64
_AUDIT_RESOURCE_ID_HASH_LENGTH = 12


class CrossCuttingMiddleware(BaseHTTPMiddleware):
    """RequestId → AuthN → License → RateLimit → Audit → handler → Audit."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        services: ApiServices,
        serve_frontend: bool = False,
    ) -> None:
        super().__init__(app)
        self._services = services
        # 当 API 进程同时伺服前端 SPA(DATAOPS_FRONTEND_DIST 配置)时,前端静态
        # 资源 / SPA 路由是公开 GET 内容,不走鉴权 / license / 限流 / 审计;只有
        # /api/* 与 /healthz 仍走完整管线。默认 False = 纯 API,行为与现状一致。
        self._serve_frontend = serve_frontend

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid4())
        request.state.request_id = request_id
        request.state.user = None
        path = request.url.path

        if self._is_frontend_request(path):
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            return response

        auth_response = self._authenticate(request)
        if auth_response is not None:
            auth_response.headers["X-Request-ID"] = request_id
            return auth_response

        license_response = self._check_license(request)
        if license_response is not None:
            license_response.headers["X-Request-ID"] = request_id
            return license_response

        rate_response = self._rate_limit(request)
        if rate_response is not None:
            rate_response.headers["X-Request-ID"] = request_id
            return rate_response

        user = _request_user(request)
        client_ip = request.client.host if request.client else None
        user_agent = request.headers.get("user-agent")
        http_resource_id = _http_resource_id(request.method, path)
        self._services.write_audit(
            user_id=user.id if user else None,
            project_id=None,
            action="api_request_start",
            resource_type="http",
            resource_id=http_resource_id,
            result="started",
            request_id=request_id,
            ip=client_ip,
            user_agent=user_agent,
        )

        try:
            response = await call_next(request)
        except Exception:
            self._services.write_audit(
                user_id=user.id if user else None,
                project_id=None,
                action="api_request_end",
                resource_type="http",
                resource_id=http_resource_id,
                result="error",
                request_id=request_id,
                ip=client_ip,
                user_agent=user_agent,
            )
            logger.exception("api request failed", request_id=request_id, path=path)
            raise

        result = "success" if response.status_code < 400 else "denied"
        self._services.write_audit(
            user_id=user.id if user else None,
            project_id=None,
            action="api_request_end",
            resource_type="http",
            resource_id=http_resource_id,
            result=result,
            request_id=request_id,
            ip=client_ip,
            user_agent=user_agent,
            detail={"status_code": response.status_code},
        )
        response.headers["X-Request-ID"] = request_id
        return response

    def _authenticate(self, request: Request) -> Response | None:
        if request.url.path in _PUBLIC_PATHS:
            return None
        try:
            token = bearer_token(request.headers.get("authorization"))
            claims = decode_access_token(token, secret=self._services.jwt_secret)
        except JwtError:
            return error_response(401, "unauthorized", "Authentication required")
        if self._services.is_token_revoked(
            user_id=claims.user_id,
            issued_at=claims.issued_at,
            expires_at=claims.expires_at,
            jti=claims.jti,
        ):
            return error_response(401, "unauthorized", "Authentication required")
        request.state.user = CurrentUser(id=claims.user_id, role=claims.role)
        return None

    def _check_license(self, request: Request) -> Response | None:
        if request.url.path in _PUBLIC_PATHS:
            return None
        license_enabled = getattr(self._services, "license_enforcement_enabled", None)
        if callable(license_enabled) and not license_enabled():
            return None
        mode = self._services.current_license_mode()
        if mode is LicenseMode.REPAIR and _is_repair_restricted(request):
            return error_response(
                403,
                "license_repair_mode",
                "This action is disabled while license repair is required",
            )
        if mode is LicenseMode.IN_GRACE and _is_in_grace_restricted(request):
            return error_response(
                403,
                "license_in_grace",
                "This action is disabled while the license is in grace period",
            )
        return None

    def _rate_limit(self, request: Request) -> Response | None:
        key = request.client.host if request.client else "unknown"
        if self._services.rate_limiter.allow(key):
            return None
        return error_response(429, "rate_limited", "Too many requests")

    def _is_frontend_request(self, path: str) -> bool:
        """前端 SPA 静态 / 路由请求(伺服前端时跳过鉴权管线)。

        仅 /api/* 与 /healthz 走完整鉴权管线;其余路径在 serve_frontend 开启时
        当作前端公开内容。serve_frontend 关闭(默认)→ 永远 False,行为不变。
        """

        if not self._serve_frontend:
            return False
        if path == "/healthz" or path == "/api" or path.startswith("/api/"):
            return False
        return True


def _is_repair_restricted(request: Request) -> bool:
    method = request.method.upper()
    path = request.url.path
    if _is_business_export_download(method, path):
        return True
    if method in _SAFE_METHODS:
        return False
    if _is_license_update(method, path):
        return False
    if _is_backup_operation(method, path):
        return False
    if _is_restore_operation(method, path):
        return False
    if _is_diagnostic_export(method, path):
        return False
    return True


def _is_in_grace_restricted(request: Request) -> bool:
    method = request.method.upper()
    path = request.url.path
    if _is_business_export_download(method, path):
        return True
    if method in _SAFE_METHODS:
        return False
    if _is_license_update(method, path):
        return False
    if _is_backup_operation(method, path):
        return False
    return True


def _is_license_update(method: str, path: str) -> bool:
    return method in {"POST", "PUT"} and path in _LICENSE_UPDATE_PATHS


def _is_backup_operation(method: str, path: str) -> bool:
    return method == "POST" and path in _BACKUP_PATHS


def _is_restore_operation(method: str, path: str) -> bool:
    return method == "POST" and path in _RESTORE_PATHS


def _is_diagnostic_export(method: str, path: str) -> bool:
    return method == "POST" and path in _DIAGNOSTIC_EXPORT_PATHS


def _is_business_export_download(method: str, path: str) -> bool:
    return method == "GET" and path.startswith(_BUSINESS_EXPORT_PREFIX)


def _request_user(request: Request) -> CurrentUser | None:
    user = getattr(request.state, "user", None)
    return user if isinstance(user, CurrentUser) else None


def _http_resource_id(method: str, path: str) -> str:
    value = f"{method} {path}"
    if len(value) <= _AUDIT_RESOURCE_ID_MAX_LENGTH:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:_AUDIT_RESOURCE_ID_HASH_LENGTH]
    suffix = f"#{digest}"
    prefix_length = _AUDIT_RESOURCE_ID_MAX_LENGTH - len(suffix)
    return f"{value[:prefix_length]}{suffix}"
