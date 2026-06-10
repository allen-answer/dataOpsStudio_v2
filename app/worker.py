from __future__ import annotations

import signal
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

import structlog
from sqlalchemy import URL, create_engine, delete, insert, select
from sqlalchemy.engine import Engine

from app.config import Settings, load_settings
from app.db.models import datasources, result_sets
from app.dbclients.dm_adapter import DMAdapter
from app.dbclients.mysql_adapter import MySQLAdapter
from app.domain.datasource import DatasourceConnInfo, DbType
from app.domain.job import Job, JobKind
from app.domain.result import ResultRef
from app.domain.schema import Column, Row
from app.domain.secret import SecretKind, SecretRef
from app.infrastructure.bootstrap.local_file import LocalFileBootstrapSecrets
from app.infrastructure.jobbackend.postgres import PostgresJobBackend
from app.infrastructure.resultstore.local_fs import LocalFsResultStore
from app.infrastructure.secretstore.local_file import LocalFileSecretStore
from app.infrastructure.secretstore.protocol import SecretStore
from app.observability.logging import configure_logging

logger = structlog.get_logger(__name__)


class JobCancelled(RuntimeError):
    """Job was cooperatively cancelled."""


class UnsupportedJobKindError(RuntimeError):
    """2.0.0 worker supports only the job kinds wired in this module."""


class UnsupportedDbTypeError(RuntimeError):
    """2.0.0 worker supports only datasource db types with registered adapters."""


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

    def set_spool_columns(self, result_set_id: str, columns: list[Column]) -> None: ...

    def get_spool_manifest(self, result_set_id: str) -> dict[str, Any]: ...

    def spool_ref(self, result_set_id: str) -> ResultRef: ...


class DatabaseAdapterLike(Protocol):
    def execute_select(self, sql: str, params: dict[str, object]) -> Iterable[Row]: ...

    def test_connection(self) -> bool: ...


class ResultSetCatalogLike(Protocol):
    def write_complete(
        self,
        *,
        result_set_id: str,
        execution_id: str,
        storage_ref: ResultRef,
        columns: list[Column],
        loaded_rows: int,
        truncated: bool,
    ) -> None: ...


DatasourceLoader = Callable[[str], DatasourceConnInfo]
AdapterFactory = Callable[
    [DatasourceConnInfo, Callable[[], bool], Callable[[list[Column]], None]],
    DatabaseAdapterLike,
]


@dataclass(frozen=True)
class WorkerRunnerConfig:
    worker_id: str
    heartbeat_interval_seconds: int = 15
    heartbeat_timeout_seconds: int = 600
    poll_interval_seconds: float = 1.0
    sql_spool_batch_size: int = 1000
    cancel_check_row_interval: int = 5000


@dataclass(frozen=True)
class _ExecutionOutcome:
    result_ref: ResultRef
    loaded_rows: int | None = None


class WorkerRunner:
    def __init__(
        self,
        backend: BackendLike,
        result_store: ResultStoreLike,
        datasource_loader: DatasourceLoader,
        adapter_factory: AdapterFactory,
        config: WorkerRunnerConfig,
        result_set_catalog: ResultSetCatalogLike | None = None,
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
        self._result_set_catalog = result_set_catalog
        self._stop_requested = False

    def request_stop(self) -> None:
        self._stop_requested = True

    def run_forever(self) -> None:
        while not self._stop_requested:
            did_work = self.run_once()
            if not did_work:
                time.sleep(self._config.poll_interval_seconds)

    def run_once(self) -> bool:
        reap_report = self._backend.reap_stale_running_jobs(self._config.heartbeat_timeout_seconds)
        _log_reap_report(reap_report)
        job = self._backend.claim_next(self._config.worker_id)
        if job is None:
            return False
        logger.info(
            "worker job claimed",
            job_id=job.id,
            kind=job.kind.value,
            worker_id=self._config.worker_id,
        )
        self._execute_claimed_job(job)
        return True

    def _execute_claimed_job(self, job: Job) -> None:
        started_at = time.monotonic()
        logger.info(
            "worker job start",
            job_id=job.id,
            kind=job.kind.value,
            worker_id=self._config.worker_id,
        )
        try:
            self._check_cancel(job.id)
            if job.kind is JobKind.TEST_CONNECTION:
                outcome = self._execute_test_connection(job)
            elif job.kind is JobKind.SQL_QUERY:
                outcome = self._execute_sql_query(job)
            else:
                raise UnsupportedJobKindError(f"Unsupported job kind: {job.kind.value}")
        except JobCancelled as exc:
            self._backend.mark_cancelled(job.id, str(exc))
            logger.info(
                "worker job cancelled",
                job_id=job.id,
                kind=job.kind.value,
                elapsed_seconds=_elapsed_seconds(started_at),
            )
        except Exception as exc:
            self._backend.fail(job.id, str(exc))
            logger.exception(
                "worker job failed",
                job_id=job.id,
                kind=job.kind.value,
                error_type=type(exc).__name__,
                elapsed_seconds=_elapsed_seconds(started_at),
            )
        else:
            self._backend.complete(job.id, outcome.result_ref)
            logger.info(
                "worker job complete",
                job_id=job.id,
                kind=job.kind.value,
                loaded_rows=outcome.loaded_rows,
                elapsed_seconds=_elapsed_seconds(started_at),
            )

    def _execute_sql_query(self, job: Job) -> _ExecutionOutcome:
        payload = job.payload
        sql = _required_payload_str(payload, "sql")
        params = _payload_params(payload)
        datasource_id = _payload_datasource_id(job)
        result_set_id = str(payload.get("result_set_id") or job.id)
        datasource = self._datasource_loader(datasource_id)
        columns: list[Column] | None = None

        def capture_columns(emitted_columns: list[Column]) -> None:
            nonlocal columns
            assert columns is None, "adapter emitted result columns more than once"
            columns = _copy_columns(emitted_columns)
            self._result_store.set_spool_columns(result_set_id, columns)

        adapter = self._adapter_factory(
            datasource,
            lambda: self._backend.is_cancel_requested(job.id),
            capture_columns,
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
        self._heartbeat(job.id)
        result_ref = self._result_store.spool_ref(result_set_id)
        self._write_result_set_catalog(job, result_set_id, result_ref, columns or [])
        manifest = self._result_store.get_spool_manifest(result_set_id)
        return _ExecutionOutcome(
            result_ref=result_ref,
            loaded_rows=_manifest_int(manifest, "loaded_rows"),
        )

    def _execute_test_connection(self, job: Job) -> _ExecutionOutcome:
        datasource_id = _payload_datasource_id(job)
        datasource = self._datasource_loader(datasource_id)
        adapter = self._adapter_factory(
            datasource,
            lambda: self._backend.is_cancel_requested(job.id),
            lambda columns: None,
        )
        started_at = time.monotonic()
        if not adapter.test_connection():
            error_summary = getattr(adapter, "last_connection_error", None)
            logger.warning(
                "datasource test_connection failed",
                job_id=job.id,
                datasource_id=datasource_id,
                error_summary=error_summary if isinstance(error_summary, str) else None,
            )
            raise RuntimeError("datasource connection test failed")
        self._heartbeat(job.id)
        metadata: dict[str, object] = {"latency_ms": _elapsed_ms(started_at)}
        server_version = getattr(adapter, "last_server_version", None)
        if isinstance(server_version, str) and server_version:
            metadata["server_version"] = server_version
        return _ExecutionOutcome(
            ResultRef(
                backend="connection_test",
                uri=f"test_connection/{job.id}",
                metadata=metadata,
            )
        )

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
            self._heartbeat(job_id)
            return now
        return last_heartbeat

    def _heartbeat(self, job_id: str) -> None:
        self._backend.heartbeat(job_id, self._config.worker_id)
        logger.debug(
            "worker job heartbeat",
            job_id=job_id,
            worker_id=self._config.worker_id,
        )

    def _check_cancel(self, job_id: str) -> None:
        if self._backend.is_cancel_requested(job_id):
            raise JobCancelled("cancel requested")

    def _write_result_set_catalog(
        self,
        job: Job,
        result_set_id: str,
        result_ref: ResultRef,
        columns: list[Column],
    ) -> None:
        if self._result_set_catalog is None:
            return
        manifest = self._result_store.get_spool_manifest(result_set_id)
        self._result_set_catalog.write_complete(
            result_set_id=result_set_id,
            execution_id=job.id,
            storage_ref=result_ref,
            columns=columns,
            loaded_rows=_manifest_int(manifest, "loaded_rows"),
            truncated=_manifest_bool(manifest, "truncated"),
        )


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


class PostgresResultSetCatalog:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def write_complete(
        self,
        *,
        result_set_id: str,
        execution_id: str,
        storage_ref: ResultRef,
        columns: list[Column],
        loaded_rows: int,
        truncated: bool,
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(delete(result_sets).where(result_sets.c.id == result_set_id))
            conn.execute(
                insert(result_sets).values(
                    id=result_set_id,
                    execution_id=execution_id,
                    storage_ref=storage_ref.model_dump(),
                    columns=[column.model_dump() for column in columns],
                    loaded_rows=loaded_rows,
                    total_rows=None if truncated else loaded_rows,
                    state="complete",
                )
            )


def build_database_adapter(
    conn_info: DatasourceConnInfo,
    secret_store: SecretStore,
    *,
    cancel_check: Callable[[], bool] | None = None,
    column_sink: Callable[[list[Column]], None] | None = None,
    cursor_max_hold_seconds: int = 300,
    statement_timeout_seconds: int = 300,
) -> DatabaseAdapterLike:
    """db_type → DatabaseAdapter 分发(2.0.0 Certified:MySQL + DM)。

    其余方言尚无 adapter → UnsupportedDbTypeError(调用方按此 fail job)。
    抽成模块级纯函数,便于单测 dispatch 而无需起 bootstrap/PG。
    """
    if conn_info.db_type is DbType.MYSQL:
        return MySQLAdapter(
            conn_info,
            secret_store,
            cancel_check=cancel_check,
            column_sink=column_sink,
            cursor_max_hold_seconds=cursor_max_hold_seconds,
            statement_timeout_seconds=statement_timeout_seconds,
        )
    if conn_info.db_type is DbType.DM:
        return DMAdapter(
            conn_info,
            secret_store,
            cancel_check=cancel_check,
            column_sink=column_sink,
            cursor_max_hold_seconds=cursor_max_hold_seconds,
            statement_timeout_seconds=statement_timeout_seconds,
        )
    raise UnsupportedDbTypeError(f"Unsupported datasource db_type: {conn_info.db_type.value}")


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
        column_sink: Callable[[list[Column]], None],
    ) -> DatabaseAdapterLike:
        return build_database_adapter(
            conn_info,
            secret_store,
            cancel_check=cancel_check,
            column_sink=column_sink,
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
            poll_interval_seconds=actual_settings.worker.poll_interval_seconds,
            cancel_check_row_interval=actual_settings.worker.cancel_check_row_interval,
        ),
        PostgresResultSetCatalog(engine),
    )


def main() -> None:
    runner = build_worker_runner()
    logger.info("worker starting")

    def handle_signal(signum: int, frame: object) -> None:
        logger.info("worker stop requested", signal=signum)
        del frame
        runner.request_stop()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    runner.run_forever()
    logger.info("worker stopped")


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


def _copy_columns(columns: list[Column]) -> list[Column]:
    return [Column.model_validate(column.model_dump()) for column in columns]


def _elapsed_seconds(started_at: float) -> float:
    return round(time.monotonic() - started_at, 3)


def _elapsed_ms(started_at: float) -> int:
    return max(0, round((time.monotonic() - started_at) * 1000))


def _log_reap_report(report: object) -> None:
    requeued = getattr(report, "requeued", 0)
    failed = getattr(report, "failed", 0)
    if not isinstance(requeued, int):
        requeued = 0
    if not isinstance(failed, int):
        failed = 0
    if requeued or failed:
        logger.info("worker reaped stale jobs", requeued=requeued, failed=failed)


def _manifest_int(manifest: dict[str, Any], key: str) -> int:
    value = manifest.get(key, 0)
    return value if isinstance(value, int) else 0


def _manifest_bool(manifest: dict[str, Any], key: str) -> bool:
    value = manifest.get(key, False)
    return value if isinstance(value, bool) else False


if __name__ == "__main__":
    main()
