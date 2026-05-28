from __future__ import annotations

import signal
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol

import structlog
from sqlalchemy import URL, create_engine, select
from sqlalchemy.engine import Engine

from app.config import Settings, load_settings
from app.db.models import datasources
from app.dbclients.mysql_adapter import MySQLAdapter
from app.domain.datasource import DatasourceConnInfo, DbType
from app.domain.job import Job, JobKind
from app.domain.result import ResultRef
from app.domain.schema import Row
from app.domain.secret import SecretKind, SecretRef
from app.infrastructure.bootstrap.local_file import LocalFileBootstrapSecrets
from app.infrastructure.jobbackend.postgres import PostgresJobBackend
from app.infrastructure.resultstore.local_fs import LocalFsResultStore
from app.infrastructure.secretstore.local_file import LocalFileSecretStore
from app.observability.logging import configure_logging

logger = structlog.get_logger(__name__)


class JobCancelled(RuntimeError):
    """Job was cooperatively cancelled."""


class UnsupportedJobKindError(RuntimeError):
    """2.0.0 worker only supports sql_query."""


class BackendLike(Protocol):
    def claim_next(self, worker_id: str) -> Job | None: ...

    def complete(self, job_id: str, result_ref: ResultRef) -> None: ...

    def fail(self, job_id: str, error: str) -> None: ...

    def heartbeat(self, job_id: str, worker_id: str) -> None: ...

    def mark_cancelled(self, job_id: str, reason: str = "cancelled") -> None: ...

    def is_cancel_requested(self, job_id: str) -> bool: ...

    def reap_stale_running_jobs(
        self,
        heartbeat_timeout_seconds: int,
        *,
        limit: int = 100,
    ) -> object: ...


class ResultStoreLike(Protocol):
    def append_spool(self, result_set_id: str, rows: list[Row]) -> None: ...

    def spool_ref(self, result_set_id: str) -> ResultRef: ...


class DatabaseAdapterLike(Protocol):
    def execute_select(self, sql: str, params: dict[str, object]) -> Iterable[Row]: ...


DatasourceLoader = Callable[[str], DatasourceConnInfo]
AdapterFactory = Callable[[DatasourceConnInfo, Callable[[], bool]], DatabaseAdapterLike]


@dataclass(frozen=True)
class WorkerRunnerConfig:
    worker_id: str
    heartbeat_interval_seconds: int = 15
    heartbeat_timeout_seconds: int = 600
    poll_interval_seconds: float = 1.0
    sql_spool_batch_size: int = 1000
    cancel_check_row_interval: int = 5000


class WorkerRunner:
    def __init__(
        self,
        backend: BackendLike,
        result_store: ResultStoreLike,
        datasource_loader: DatasourceLoader,
        adapter_factory: AdapterFactory,
        config: WorkerRunnerConfig,
    ) -> None:
        if config.sql_spool_batch_size <= 0:
            raise ValueError("sql_spool_batch_size must be positive")
        if config.cancel_check_row_interval <= 0:
            raise ValueError("cancel_check_row_interval must be positive")
        self._backend = backend
        self._result_store = result_store
        self._datasource_loader = datasource_loader
        self._adapter_factory = adapter_factory
        self._config = config
        self._stop_requested = False

    def request_stop(self) -> None:
        self._stop_requested = True

    def run_forever(self) -> None:
        while not self._stop_requested:
            did_work = self.run_once()
            if not did_work:
                time.sleep(self._config.poll_interval_seconds)

    def run_once(self) -> bool:
        self._backend.reap_stale_running_jobs(self._config.heartbeat_timeout_seconds)
        job = self._backend.claim_next(self._config.worker_id)
        if job is None:
            return False
        self._execute_claimed_job(job)
        return True

    def _execute_claimed_job(self, job: Job) -> None:
        try:
            self._check_cancel(job.id)
            if job.kind is not JobKind.SQL_QUERY:
                raise UnsupportedJobKindError(f"Unsupported job kind: {job.kind.value}")
            result_ref = self._execute_sql_query(job)
        except JobCancelled as exc:
            self._backend.mark_cancelled(job.id, str(exc))
        except Exception as exc:
            self._backend.fail(job.id, str(exc))
            logger.exception("worker job failed", job_id=job.id, kind=job.kind.value)
        else:
            self._backend.complete(job.id, result_ref)

    def _execute_sql_query(self, job: Job) -> ResultRef:
        payload = job.payload
        sql = _required_payload_str(payload, "sql")
        params = _payload_params(payload)
        datasource_id = _payload_datasource_id(job)
        result_set_id = str(payload.get("result_set_id") or job.id)
        datasource = self._datasource_loader(datasource_id)
        adapter = self._adapter_factory(
            datasource,
            lambda: self._backend.is_cancel_requested(job.id),
        )

        batch: list[Row] = []
        rows_since_cancel_check = 0
        cancel_check_row_interval = self._config.cancel_check_row_interval
        last_heartbeat = time.monotonic()
        for row in adapter.execute_select(sql, params):
            rows_since_cancel_check += 1
            if rows_since_cancel_check >= cancel_check_row_interval:
                self._check_cancel(job.id)
                rows_since_cancel_check = 0
            batch.append(row)
            if len(batch) >= self._config.sql_spool_batch_size:
                self._flush_batch(job.id, result_set_id, batch)
                batch = []
                last_heartbeat = self._heartbeat_if_due(job.id, last_heartbeat, force=True)
            else:
                last_heartbeat = self._heartbeat_if_due(job.id, last_heartbeat)

        if batch:
            self._flush_batch(job.id, result_set_id, batch)
        self._backend.heartbeat(job.id, self._config.worker_id)
        return self._result_store.spool_ref(result_set_id)

    def _flush_batch(self, job_id: str, result_set_id: str, batch: list[Row]) -> None:
        # Cancel during append_spool is caught at the next batch or row interval.
        self._check_cancel(job_id)
        self._result_store.append_spool(result_set_id, batch)

    def _heartbeat_if_due(
        self,
        job_id: str,
        last_heartbeat: float,
        *,
        force: bool = False,
    ) -> float:
        now = time.monotonic()
        if force or now - last_heartbeat >= self._config.heartbeat_interval_seconds:
            self._backend.heartbeat(job_id, self._config.worker_id)
            return now
        return last_heartbeat

    def _check_cancel(self, job_id: str) -> None:
        if self._backend.is_cancel_requested(job_id):
            raise JobCancelled("cancel requested")


class PostgresDatasourceLoader:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def __call__(self, datasource_id: str) -> DatasourceConnInfo:
        with self._engine.connect() as conn:
            row = (
                conn.execute(select(datasources).where(datasources.c.id == datasource_id))
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise LookupError(f"Datasource not found: {datasource_id}")

        database_name = row["database_name"]
        if database_name is None:
            raise ValueError(f"Datasource database_name is required: {datasource_id}")

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
        )


def build_worker_runner(settings: Settings | None = None) -> WorkerRunner:
    actual_settings = settings or load_settings()
    configure_logging(actual_settings.logging.level)
    bootstrap = LocalFileBootstrapSecrets(
        master_key_file=actual_settings.bootstrap.master_key_file,
        pg_app_password_file=actual_settings.bootstrap.pg_app_password_file,
        pg_superuser_password_file=actual_settings.bootstrap.pg_superuser_password_file,
        license_file=actual_settings.bootstrap.license_file,
    )
    engine = _create_metadata_engine(actual_settings, bootstrap.get_pg_app_password())
    backend = PostgresJobBackend(engine, worker_id=actual_settings.worker.worker_id)
    secret_store = LocalFileSecretStore(engine, bootstrap)
    result_store = LocalFsResultStore(
        actual_settings.result_store.local_root,
        spool_max_rows=actual_settings.result_store.spool_max_rows,
        spool_max_bytes=actual_settings.result_store.spool_max_bytes,
        result_ttl_days=actual_settings.result_store.result_ttl_days,
    )
    datasource_loader = PostgresDatasourceLoader(engine)

    def adapter_factory(
        conn_info: DatasourceConnInfo,
        cancel_check: Callable[[], bool],
    ) -> DatabaseAdapterLike:
        if conn_info.db_type is not DbType.MYSQL:
            raise UnsupportedJobKindError(
                f"Unsupported datasource db_type: {conn_info.db_type.value}"
            )
        return MySQLAdapter(
            conn_info,
            secret_store,
            cancel_check=cancel_check,
            cursor_max_hold_seconds=actual_settings.result_store.cursor_max_hold_seconds,
            statement_timeout_seconds=actual_settings.worker.heartbeat_timeout_seconds,
        )

    return WorkerRunner(
        backend,
        result_store,
        datasource_loader,
        adapter_factory,
        WorkerRunnerConfig(
            worker_id=actual_settings.worker.worker_id,
            heartbeat_interval_seconds=actual_settings.worker.heartbeat_interval_seconds,
            heartbeat_timeout_seconds=actual_settings.worker.heartbeat_timeout_seconds,
            cancel_check_row_interval=actual_settings.worker.cancel_check_row_interval,
        ),
    )


def main() -> None:
    runner = build_worker_runner()

    def handle_signal(signum: int, frame: object) -> None:
        runner.request_stop()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    runner.run_forever()


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


def _required_payload_str(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Job payload requires non-empty string field: {key}")
    return value


def _payload_params(payload: dict[str, object]) -> dict[str, object]:
    value = payload.get("params", {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Job payload params must be an object")
    return dict(value)


def _payload_datasource_id(job: Job) -> str:
    value = job.payload.get("datasource_id")
    if isinstance(value, str) and value:
        return value
    if job.datasource_ids:
        return job.datasource_ids[0]
    raise ValueError("Job payload requires datasource_id or job.datasource_ids")


if __name__ == "__main__":
    main()
