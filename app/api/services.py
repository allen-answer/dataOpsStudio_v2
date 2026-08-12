from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

import structlog
from sqlalchemy import URL, create_engine, delete, insert, select, update
from sqlalchemy.engine import Engine

from app.config import Settings, load_settings
from app.db.models import audit_logs, license_state, revoked_tokens, system_settings, users
from app.domain.license import LicenseMode
from app.infrastructure.bootstrap.local_file import LocalFileBootstrapSecrets
from app.infrastructure.jobbackend.postgres import PostgresJobBackend
from app.infrastructure.resultstore.local_fs import LocalFsResultStore
from app.infrastructure.secretstore.local_file import LocalFileSecretStore
from app.observability.logging import configure_logging

logger = structlog.get_logger(__name__)

SETTING_ACCESS_TOKEN_TTL_SECONDS = "auth.access_token_ttl_seconds"
SETTING_LICENSE_ENFORCEMENT_ENABLED = "license.enforcement_enabled"
DEFAULT_ACCESS_TOKEN_TTL_SECONDS = 3600
DEFAULT_LICENSE_ENFORCEMENT_ENABLED = True


@dataclass(frozen=True)
class RateLimitRule:
    limit: int
    window_seconds: int = 60


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: float = 0.0


def _default_rate_limit_policies() -> dict[str, RateLimitRule]:
    return {
        "auth": RateLimitRule(limit=10),
        "sensitive_write": RateLimitRule(limit=60),
        "sql_control": RateLimitRule(limit=30),
        "job_read": RateLimitRule(limit=300),
        "general_read": RateLimitRule(limit=120),
    }


@dataclass
class RateLimiter:
    """Small in-process sliding-window limiter with independent policy buckets.

    ``limit`` remains an explicit all-groups override for integration fixtures and
    callers which used the pre-grouped limiter. Production defaults use ``policies``.
    """

    limit: int | None = None
    window_seconds: int = 60
    policies: dict[str, RateLimitRule] = field(default_factory=_default_rate_limit_policies)
    _hits: dict[tuple[str, str], list[float]] = field(default_factory=dict)

    def check(
        self,
        key: str,
        *,
        group: str = "general_read",
        now: float | None = None,
    ) -> RateLimitDecision:
        current = time.monotonic() if now is None else now
        rule = self._rule(group)
        bucket = (group, key)
        cutoff = current - rule.window_seconds
        hits = [item for item in self._hits.get(bucket, []) if item >= cutoff]
        if len(hits) >= rule.limit:
            self._hits[bucket] = hits
            retry_after = max(0.001, hits[0] + rule.window_seconds - current)
            return RateLimitDecision(allowed=False, retry_after_seconds=retry_after)
        hits.append(current)
        self._hits[bucket] = hits
        return RateLimitDecision(allowed=True)

    def allow(self, key: str, *, now: float | None = None) -> bool:
        """Compatibility entrypoint for existing tests and injected callers."""

        return self.check(key, now=now).allowed

    def _rule(self, group: str) -> RateLimitRule:
        if self.limit is not None:
            return RateLimitRule(limit=self.limit, window_seconds=self.window_seconds)
        return self.policies.get(group, self.policies["general_read"])


@dataclass
class ApiServices:
    engine: Engine
    job_backend: PostgresJobBackend
    secret_store: LocalFileSecretStore
    result_store: LocalFsResultStore
    jwt_secret: str
    rate_limiter: RateLimiter = field(default_factory=RateLimiter)
    job_wait_timeout_seconds: float = 10.0
    max_active_resultsets_per_console: int = 3
    metadata_probe_timeout_seconds: int = 5
    export_per_user_per_hour: int = 10
    download_url_ttl_seconds: int = 300
    upload_max_mb: int = 100
    # workflow run 状态查询推进节点状态时,RetryPolicy=None 节点继承的默认重试次数。
    # 与 worker 推进器同源(Settings.worker.workflow_node_default_max_retries),
    # 避免状态展示与实际执行口径漂移。
    workflow_node_default_max_retries: int = 0

    def access_token_ttl_seconds(self) -> int:
        value = self._system_setting(SETTING_ACCESS_TOKEN_TTL_SECONDS)
        if value is None:
            return DEFAULT_ACCESS_TOKEN_TTL_SECONDS
        try:
            ttl = int(value)
        except ValueError:
            return DEFAULT_ACCESS_TOKEN_TTL_SECONDS
        return ttl if 300 <= ttl <= 2_592_000 else DEFAULT_ACCESS_TOKEN_TTL_SECONDS

    def license_enforcement_enabled(self) -> bool:
        value = self._system_setting(SETTING_LICENSE_ENFORCEMENT_ENABLED)
        if value is None:
            return DEFAULT_LICENSE_ENFORCEMENT_ENABLED
        return value.strip().lower() in {"1", "true", "yes", "on"}

    def _system_setting(self, key: str) -> str | None:
        with self.engine.connect() as conn:
            value = conn.execute(
                select(system_settings.c.value).where(system_settings.c.key == key)
            ).scalar_one_or_none()
        return str(value) if value is not None else None

    def current_license_mode(self) -> LicenseMode:
        with self.engine.connect() as conn:
            row = (
                conn.execute(select(license_state.c.mode).where(license_state.c.id == 1))
                .mappings()
                .one_or_none()
            )
        if row is None:
            return LicenseMode.TRIAL
        return LicenseMode(str(row["mode"]))

    def is_token_revoked(
        self,
        *,
        user_id: str,
        issued_at: int,
        expires_at: int,
        jti: str | None,
    ) -> bool:
        del expires_at
        now = datetime.now(UTC)
        issued_at_dt = datetime.fromtimestamp(issued_at, UTC)
        with self.engine.connect() as conn:
            row = (
                conn.execute(select(users.c.tokens_revoked_after).where(users.c.id == user_id))
                .mappings()
                .one_or_none()
            )
            if row is None:
                return True
            revoked_after = row["tokens_revoked_after"]
            if isinstance(revoked_after, datetime):
                if revoked_after.tzinfo is None:
                    revoked_after = revoked_after.replace(tzinfo=UTC)
                if issued_at_dt <= revoked_after:
                    return True
            if jti is None:
                return False
            revoked_row = (
                conn.execute(
                    select(revoked_tokens.c.jti)
                    .where(revoked_tokens.c.jti == jti)
                    .where(revoked_tokens.c.expires_at > now)
                )
                .mappings()
                .one_or_none()
            )
            if revoked_row is None:
                return False
            return True

    def revoke_user_tokens(self, *, user_id: str) -> datetime:
        revoked_after = datetime.now(UTC)
        with self.engine.begin() as conn:
            conn.execute(delete(revoked_tokens).where(revoked_tokens.c.expires_at <= revoked_after))
            result = conn.execute(
                update(users)
                .where(users.c.id == user_id)
                .values(tokens_revoked_after=revoked_after, updated_at=revoked_after)
            )
            if result.rowcount == 0:
                raise ValueError("user not found")
        return revoked_after

    def write_audit(
        self,
        *,
        user_id: str | None,
        project_id: str | None,
        action: str,
        resource_type: str | None,
        resource_id: str | None,
        result: str,
        request_id: str | None,
        ip: str | None,
        user_agent: str | None,
        detail: dict[str, object] | None = None,
    ) -> None:
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    insert(audit_logs).values(
                        user_id=user_id,
                        project_id=project_id,
                        action=action,
                        resource_type=resource_type,
                        resource_id=resource_id,
                        result=result,
                        request_id=request_id,
                        ip=ip,
                        user_agent=user_agent,
                        detail=detail or {},
                    )
                )
        except Exception:
            logger.exception("api audit write failed", request_id=request_id, action=action)


def build_api_services(settings: Settings | None = None) -> ApiServices:
    actual_settings = settings or load_settings()
    configure_logging(actual_settings.logging.level)
    bootstrap = LocalFileBootstrapSecrets(
        master_key_file=actual_settings.bootstrap.master_key_file,
        pg_app_password_file=actual_settings.bootstrap.pg_app_password_file,
        pg_superuser_password_file=actual_settings.bootstrap.pg_superuser_password_file,
        jwt_secret_file=actual_settings.bootstrap.jwt_secret_file,
        license_file=actual_settings.bootstrap.license_file,
    )
    engine = _create_metadata_engine(actual_settings, bootstrap.get_pg_app_password())
    return ApiServices(
        engine=engine,
        job_backend=PostgresJobBackend(engine),
        secret_store=LocalFileSecretStore(engine, bootstrap),
        result_store=LocalFsResultStore(
            actual_settings.result_store.local_root,
            spool_max_rows=actual_settings.result_store.spool_max_rows,
            spool_max_bytes=actual_settings.result_store.spool_max_bytes,
            result_ttl_days=actual_settings.result_store.result_ttl_days,
            sql_export_ttl_hours=actual_settings.result_store.sql_export_ttl_hours,
            upload_ttl_hours=actual_settings.result_store.upload_ttl_hours,
        ),
        jwt_secret=bootstrap.get_jwt_secret(),
        max_active_resultsets_per_console=(
            actual_settings.result_store.max_active_resultsets_per_console
        ),
        metadata_probe_timeout_seconds=actual_settings.api.metadata_probe_timeout_seconds,
        export_per_user_per_hour=actual_settings.api.export_per_user_per_hour,
        download_url_ttl_seconds=actual_settings.api.download_url_ttl_seconds,
        upload_max_mb=actual_settings.result_store.upload_max_mb,
        workflow_node_default_max_retries=(
            actual_settings.worker.workflow_node_default_max_retries
        ),
    )


def new_id() -> str:
    return str(uuid4())


def _create_metadata_engine(settings: Settings, password: str) -> Engine:
    url = URL.create(
        "postgresql+psycopg",
        username=settings.db.user,
        password=password,
        host=settings.db.host,
        port=settings.db.port,
        database=settings.db.database,
    )
    return create_engine(
        url,
        pool_size=settings.db.pool_size,
        max_overflow=settings.db.max_overflow,
        pool_recycle=settings.db.pool_recycle_seconds,
        pool_pre_ping=settings.db.pool_pre_ping,
    )
