from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

import structlog
from sqlalchemy import URL, create_engine, delete, insert, select, update
from sqlalchemy.engine import Engine

from app.config import Settings, load_settings
from app.db.models import audit_logs, license_state, revoked_tokens, users
from app.domain.license import LicenseMode
from app.infrastructure.bootstrap.local_file import LocalFileBootstrapSecrets
from app.infrastructure.jobbackend.postgres import PostgresJobBackend
from app.infrastructure.resultstore.local_fs import LocalFsResultStore
from app.infrastructure.secretstore.local_file import LocalFileSecretStore
from app.observability.logging import configure_logging

logger = structlog.get_logger(__name__)


@dataclass
class RateLimiter:
    limit: int = 120
    window_seconds: int = 60
    _hits: dict[str, list[float]] = field(default_factory=dict)

    def allow(self, key: str, *, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        cutoff = current - self.window_seconds
        hits = [item for item in self._hits.get(key, []) if item >= cutoff]
        if len(hits) >= self.limit:
            self._hits[key] = hits
            return False
        hits.append(current)
        self._hits[key] = hits
        return True


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
